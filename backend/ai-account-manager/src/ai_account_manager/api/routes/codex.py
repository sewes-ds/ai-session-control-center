from fastapi import APIRouter, HTTPException, status

from ai_account_manager.providers.codex.account_enrollment import (
    CodexAccountEnrollmentError,
    cancel_codex_account_enrollment,
    get_codex_account_enrollment,
    start_codex_account_enrollment,
)
from ai_account_manager.providers.codex.auth_status import (
    CodexAuthStatusError,
    get_codex_auth_status,
)
from ai_account_manager.providers.codex.capabilities import (
    CodexCapabilitiesError,
    read_cached_codex_capabilities,
    refresh_codex_capabilities,
)
from ai_account_manager.providers.codex.profile_discovery import discover_codex_profiles
from ai_account_manager.providers.codex.quota import (
    CodexQuotaError,
    consume_rate_limit_reset_credit,
    get_cached_codex_quota,
    refresh_codex_quota,
)
from ai_account_manager.schemas.codex import (
    CodexAccountEnrollment,
    CodexAccountEnrollmentStartRequest,
    CodexAuthStatus,
    CodexCapabilitiesSnapshot,
    CodexProfileDiscoveryResponse,
    CodexQuotaSnapshot,
    CodexRateLimitResetCreditConsumeRequest,
    CodexRateLimitResetCreditConsumeResponse,
)

router = APIRouter(prefix="/api/providers/codex", tags=["codex"])


def _quota_error_to_http(exc: CodexQuotaError) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    if exc.code in {"auth_missing", "profile_missing", "invalid_slug"}:
        status_code = (
            status.HTTP_404_NOT_FOUND if exc.code in {"auth_missing", "profile_missing"} else status.HTTP_400_BAD_REQUEST
        )
    if exc.code in {"request_failed", "process_failed", "timeout", "unexpected_outcome"}:
        status_code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


def _auth_status_error_to_http(exc: CodexAuthStatusError) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


def _enrollment_error_to_http(exc: CodexAccountEnrollmentError) -> HTTPException:
    mapping = {
        "account_exists": status.HTTP_409_CONFLICT,
        "enrollment_in_progress": status.HTTP_409_CONFLICT,
        "enrollment_missing": status.HTTP_404_NOT_FOUND,
        "enrollment_completed": status.HTTP_409_CONFLICT,
        "codex_binary_missing": status.HTTP_503_SERVICE_UNAVAILABLE,
        "auth_start_failed": status.HTTP_502_BAD_GATEWAY,
    }
    return HTTPException(
        status_code=mapping.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": exc.message},
    )


def _capabilities_error_to_http(exc: CodexCapabilitiesError) -> HTTPException:
    mapping = {
        "invalid_slug": status.HTTP_400_BAD_REQUEST,
        "profile_missing": status.HTTP_404_NOT_FOUND,
        "auth_missing": status.HTTP_404_NOT_FOUND,
        "cache_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    return HTTPException(
        status_code=mapping.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": exc.message},
    )


@router.post(
    "/account-enrollments",
    response_model=CodexAccountEnrollment,
    status_code=status.HTTP_201_CREATED,
)
def start_codex_enrollment(
    request: CodexAccountEnrollmentStartRequest,
) -> CodexAccountEnrollment:
    """Start native Codex device-code authorization for a new account profile.

    CUS never accepts a ChatGPT/OpenAI password. The final profile directory is
    published only after Codex CLI completes official device authorization.
    """
    try:
        return start_codex_account_enrollment(request.account_slug, label=request.label)
    except CodexAccountEnrollmentError as exc:
        raise _enrollment_error_to_http(exc) from exc


@router.get("/account-enrollments/{enrollment_id}", response_model=CodexAccountEnrollment)
def read_codex_enrollment(enrollment_id: str) -> CodexAccountEnrollment:
    try:
        return get_codex_account_enrollment(enrollment_id)
    except CodexAccountEnrollmentError as exc:
        raise _enrollment_error_to_http(exc) from exc


@router.delete("/account-enrollments/{enrollment_id}", response_model=CodexAccountEnrollment)
def cancel_codex_enrollment(enrollment_id: str) -> CodexAccountEnrollment:
    try:
        return cancel_codex_account_enrollment(enrollment_id)
    except CodexAccountEnrollmentError as exc:
        raise _enrollment_error_to_http(exc) from exc


@router.get("/accounts", response_model=CodexProfileDiscoveryResponse)
def list_codex_accounts() -> CodexProfileDiscoveryResponse:
    """Discover local Codex profile directories without reading token contents."""
    return discover_codex_profiles()


@router.get("/accounts/{account_slug}/auth-status", response_model=CodexAuthStatus)
def get_codex_account_auth_status(account_slug: str) -> CodexAuthStatus:
    """Return safe non-secret auth status for a Codex account profile."""
    try:
        return get_codex_auth_status(account_slug)
    except CodexAuthStatusError as exc:
        raise _auth_status_error_to_http(exc) from exc


@router.get("/accounts/{account_slug}/quota", response_model=CodexQuotaSnapshot)
def get_codex_account_quota(account_slug: str) -> CodexQuotaSnapshot:
    """Return the last safe quota snapshot for a Codex account profile."""
    try:
        return get_cached_codex_quota(account_slug)
    except CodexQuotaError as exc:
        raise _quota_error_to_http(exc) from exc


@router.post("/accounts/{account_slug}/quota/refresh", response_model=CodexQuotaSnapshot)
def refresh_codex_account_quota(account_slug: str) -> CodexQuotaSnapshot:
    """Refresh quota using server-side auth.json without exposing tokens."""
    try:
        return refresh_codex_quota(account_slug)
    except CodexQuotaError as exc:
        raise _quota_error_to_http(exc) from exc


@router.post(
    "/accounts/{account_slug}/rate-limit-reset-credits/consume",
    response_model=CodexRateLimitResetCreditConsumeResponse,
)
def consume_codex_rate_limit_reset_credit(
    account_slug: str,
    request: CodexRateLimitResetCreditConsumeRequest,
) -> CodexRateLimitResetCreditConsumeResponse:
    """Consume one official Codex rate-limit reset credit with caller idempotency."""
    try:
        return consume_rate_limit_reset_credit(
            account_slug,
            idempotency_key=request.idempotency_key,
            credit_id=request.credit_id,
        )
    except CodexQuotaError as exc:
        raise _quota_error_to_http(exc) from exc


@router.get("/accounts/{account_slug}/capabilities", response_model=CodexCapabilitiesSnapshot)
def get_codex_account_capabilities(account_slug: str) -> CodexCapabilitiesSnapshot:
    """Return the last account-scoped Codex capability snapshot without provider I/O."""
    try:
        return read_cached_codex_capabilities(account_slug)
    except CodexCapabilitiesError as exc:
        raise _capabilities_error_to_http(exc) from exc


@router.post("/accounts/{account_slug}/capabilities/refresh", response_model=CodexCapabilitiesSnapshot)
def refresh_codex_account_capabilities(account_slug: str) -> CodexCapabilitiesSnapshot:
    """Refresh Codex capabilities via a temporary CODEX_HOME clone."""
    try:
        return refresh_codex_capabilities(account_slug)
    except CodexCapabilitiesError as exc:
        raise _capabilities_error_to_http(exc) from exc
