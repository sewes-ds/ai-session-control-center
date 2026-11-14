from fastapi import APIRouter, HTTPException, status

from ai_account_manager.providers.claude_code.deepseek import (
    DeepSeekProviderError,
    deepseek_capabilities,
    discover_claude_code_profiles,
    read_cached_balance,
    refresh_balance,
)
from ai_account_manager.schemas.claude_code import (
    ClaudeCodeCapabilitiesSnapshot,
    ClaudeCodeProfileDiscoveryResponse,
    DeepSeekBalanceSnapshot,
)

router = APIRouter(prefix="/api/providers/claude_code", tags=["claude_code"])


def _error(exc: DeepSeekProviderError) -> HTTPException:
    mapping = {
        "profile_missing": status.HTTP_404_NOT_FOUND,
        "missing_key": status.HTTP_404_NOT_FOUND,
        "invalid_key_file": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "invalid_key": status.HTTP_400_BAD_REQUEST,
        "auth_failed": status.HTTP_401_UNAUTHORIZED,
        "cache_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "request_failed": status.HTTP_502_BAD_GATEWAY,
        "invalid_response": status.HTTP_502_BAD_GATEWAY,
    }
    return HTTPException(
        status_code=mapping.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/accounts", response_model=ClaudeCodeProfileDiscoveryResponse)
def list_claude_code_accounts() -> ClaudeCodeProfileDiscoveryResponse:
    return discover_claude_code_profiles()


@router.get(
    "/accounts/{account_slug}/capabilities",
    response_model=ClaudeCodeCapabilitiesSnapshot,
)
def get_claude_code_capabilities(account_slug: str) -> ClaudeCodeCapabilitiesSnapshot:
    try:
        return deepseek_capabilities(account_slug)
    except DeepSeekProviderError as exc:
        raise _error(exc) from exc


@router.post(
    "/accounts/{account_slug}/capabilities/refresh",
    response_model=ClaudeCodeCapabilitiesSnapshot,
)
def refresh_claude_code_capabilities(account_slug: str) -> ClaudeCodeCapabilitiesSnapshot:
    return get_claude_code_capabilities(account_slug)


@router.get(
    "/accounts/{account_slug}/balance",
    response_model=DeepSeekBalanceSnapshot,
)
def get_claude_code_balance(account_slug: str) -> DeepSeekBalanceSnapshot:
    try:
        return read_cached_balance(account_slug)
    except DeepSeekProviderError as exc:
        raise _error(exc) from exc


@router.post(
    "/accounts/{account_slug}/balance/refresh",
    response_model=DeepSeekBalanceSnapshot,
)
def refresh_claude_code_balance(account_slug: str) -> DeepSeekBalanceSnapshot:
    try:
        return refresh_balance(account_slug)
    except DeepSeekProviderError as exc:
        raise _error(exc) from exc
