from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable

from ai_account_manager.core.config import AppSettings, get_settings

CODEX_APP_SERVER_BINARY = Path.home() / ".local" / "bin" / "codex"
_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")
_SECRET_ENV_RE = re.compile(r"(TOKEN|COOKIE|AUTH|SECRET|PASSWORD|PASSWD|KEY)", re.IGNORECASE)
_TOKEN_LIKE_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
_MAX_STDOUT_LINE_BYTES = 2 * 1024 * 1024
_MAX_STDERR_BYTES = 20 * 1024
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_MODEL_PAGES = 20
_MAX_MODELS = 500


class CodexAppServerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ProfileAuthMetadata:
    size: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    sha256: str


@dataclass
class MethodResult:
    status: str
    result: Any | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class CodexAppServerProbeResult:
    account_slug: str
    profile_path: Path
    profile_integrity_status: str
    methods: dict[str, MethodResult] = field(default_factory=dict)
    stderr_safe: str = ""
    error_code: str | None = None
    error_message: str | None = None
    request_methods_sent: list[str] = field(default_factory=list)
    temp_home_removed: bool = False


ProcessFactory = Callable[..., subprocess.Popen[bytes]]


def validate_account_slug(account_slug: str) -> str:
    value = account_slug.strip()
    if not _SAFE_SLUG_RE.fullmatch(value):
        raise CodexAppServerError("invalid_slug", "Invalid Codex account slug")
    return value


def resolve_profile(settings: AppSettings, account_slug: str) -> Path:
    slug = validate_account_slug(account_slug)
    profile_dir = settings.codex_profiles_root / slug
    auth_json = profile_dir / "auth.json"
    if not profile_dir.is_dir():
        raise CodexAppServerError("profile_missing", "Codex profile directory does not exist")
    if auth_json.is_symlink() or not auth_json.is_file():
        raise CodexAppServerError("auth_missing", "Codex auth.json is missing for this profile")
    return profile_dir


def auth_metadata(auth_json: Path) -> ProfileAuthMetadata:
    st = auth_json.stat()
    return ProfileAuthMetadata(
        size=st.st_size,
        mode=st.st_mode & 0o777,
        uid=st.st_uid,
        gid=st.st_gid,
        mtime_ns=st.st_mtime_ns,
        sha256=hashlib.sha256(auth_json.read_bytes()).hexdigest(),
    )


def _safe_error(code: str, message: str) -> MethodResult:
    return MethodResult(status="error", error_code=code[:80], error_message=message[:200])


def _sanitize_stderr(raw: bytes) -> str:
    bounded = raw[:_MAX_STDERR_BYTES].decode("utf-8", errors="replace")
    bounded = _TOKEN_LIKE_RE.sub("[REDACTED]", bounded)
    return bounded[-_MAX_STDERR_BYTES:]


def _process_env(codex_home: Path) -> dict[str, str]:
    keep_names = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in keep_names or key.startswith("LC_"):
            if not _SECRET_ENV_RE.search(key):
                env[key] = value
    env["CODEX_HOME"] = str(codex_home)
    env["RUST_LOG"] = "error"
    return env


class _JsonLineClient:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: float,
        request_methods_sent: list[str],
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise CodexAppServerError("process_invalid", "Codex app-server stdio unavailable")
        self.process = process
        self.stdin: BinaryIO = process.stdin
        self.stdout: BinaryIO = process.stdout
        self.timeout_at = time.monotonic() + timeout_seconds
        self.next_id = 1
        self.total_response_bytes = 0
        self.request_methods_sent = request_methods_sent

    def _remaining(self) -> float:
        remaining = self.timeout_at - time.monotonic()
        if remaining <= 0:
            raise CodexAppServerError("timeout", "Codex app-server operation timed out")
        return remaining

    def request(self, method: str, params: object | None = None, *, include_params: bool = True) -> Any:
        rid = self.next_id
        self.next_id += 1
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if include_params:
            payload["params"] = params
        self.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        self.stdin.flush()
        self.request_methods_sent.append(method)
        return self._read_response(rid)

    def notify(self, method: str, params: object | None = None) -> None:
        payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        self.stdin.flush()
        self.request_methods_sent.append(method)

    def _read_response(self, rid: int) -> Any:
        selector = selectors.DefaultSelector()
        selector.register(self.stdout, selectors.EVENT_READ)
        while True:
            remaining = self._remaining()
            if not selector.select(remaining):
                raise CodexAppServerError("timeout", "Codex app-server operation timed out")
            line = self.stdout.readline(_MAX_STDOUT_LINE_BYTES + 1)
            if line == b"":
                raise CodexAppServerError("process_exited", "Codex app-server exited before response")
            if len(line) > _MAX_STDOUT_LINE_BYTES:
                raise CodexAppServerError("stdout_line_too_large", "Codex app-server stdout line too large")
            self.total_response_bytes += len(line)
            if self.total_response_bytes > _MAX_RESPONSE_BYTES:
                raise CodexAppServerError("response_too_large", "Codex app-server response too large")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("id") != rid:
                continue
            if "error" in payload:
                error = payload.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code", "method_error"))
                    message = "Codex app-server method failed"
                else:
                    code = "method_error"
                    message = "Codex app-server method failed"
                raise CodexAppServerError(code, message)
            return payload.get("result")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _start_process(
    codex_home: Path,
    *,
    process_factory: ProcessFactory | None = None,
) -> subprocess.Popen[bytes]:
    factory = process_factory or subprocess.Popen
    return factory(
        [str(CODEX_APP_SERVER_BINARY), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=_process_env(codex_home),
        shell=False,
    )


def run_codex_app_server_probe(
    account_slug: str,
    *,
    settings: AppSettings | None = None,
    process_factory: ProcessFactory | None = None,
) -> CodexAppServerProbeResult:
    settings = settings or get_settings()
    slug = validate_account_slug(account_slug)
    profile_dir = resolve_profile(settings, slug)
    auth_json = profile_dir / "auth.json"
    before = auth_metadata(auth_json)
    result = CodexAppServerProbeResult(
        account_slug=slug,
        profile_path=profile_dir,
        profile_integrity_status="not_checked",
    )
    temp_root: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        temp_root = Path(tempfile.mkdtemp(prefix="cus-codex-app-server-"))
        os.chmod(temp_root, 0o700)
        codex_home = temp_root / "codex_home"
        codex_home.mkdir(mode=0o700)
        shutil.copy2(auth_json, codex_home / "auth.json", follow_symlinks=True)
        os.chmod(codex_home / "auth.json", 0o600)
        process = _start_process(codex_home, process_factory=process_factory)
        client = _JsonLineClient(
            process,
            timeout_seconds=settings.codex_app_server_timeout_seconds,
            request_methods_sent=result.request_methods_sent,
        )
        try:
            result.methods["initialize"] = MethodResult(
                status="ok",
                result=client.request(
                    "initialize",
                    {"clientInfo": {"name": "cus-stage-b1", "version": "1"}, "capabilities": {"experimentalApi": True}},
                ),
            )
            client.notify("initialized")
        except CodexAppServerError as exc:
            result.methods["initialize"] = _safe_error(exc.code, exc.message)
            raise

        try:
            result.methods["account"] = MethodResult(
                status="ok",
                result=client.request(
                    "account/read",
                    {"refreshToken": False},
                    include_params=True,
                ),
            )
        except CodexAppServerError as exc:
            result.methods["account"] = _safe_error(exc.code, exc.message)

        try:
            models: list[object] = []
            seen_cursors: set[str] = set()
            cursor: str | None = None
            next_cursor: str | None = None
            for _ in range(_MAX_MODEL_PAGES):
                params: dict[str, object] = {"includeHidden": False, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                page = client.request("model/list", params)
                if not isinstance(page, dict):
                    raise CodexAppServerError("model_response_invalid", "Codex model/list response invalid")
                data = page.get("data")
                if not isinstance(data, list):
                    raise CodexAppServerError("model_response_invalid", "Codex model/list data invalid")
                models.extend(data)
                if len(models) > _MAX_MODELS:
                    raise CodexAppServerError("model_limit_exceeded", "Codex model/list exceeded model limit")
                raw_cursor = page.get("nextCursor")
                next_cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
                if next_cursor is None:
                    break
                if next_cursor in seen_cursors:
                    raise CodexAppServerError("repeated_cursor", "Codex model/list returned repeated cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise CodexAppServerError("pagination_limit_exceeded", "Codex model/list exceeded page limit")
            if not models:
                raise CodexAppServerError("empty_models", "Codex model/list returned no models")
            result.methods["models"] = MethodResult(
                status="ok", result={"data": models, "nextCursor": next_cursor}
            )
        except CodexAppServerError as exc:
            result.methods["models"] = _safe_error(exc.code, exc.message)

        method_specs: list[tuple[str, str, object | None, bool]] = [
            ("rate_limits", "account/rateLimits/read", None, False),
            ("usage", "account/usage/read", None, False),
        ]
        for key, method, params, include_params in method_specs:
            try:
                result.methods[key] = MethodResult(
                    status="ok", result=client.request(method, params, include_params=include_params)
                )
            except CodexAppServerError as exc:
                result.methods[key] = _safe_error(exc.code, exc.message)
    except CodexAppServerError as exc:
        result.error_code = exc.code
        result.error_message = exc.message
    except (OSError, subprocess.SubprocessError):
        result.error_code = "process_failed"
        result.error_message = "Codex app-server process failed"
    finally:
        if process is not None:
            _terminate(process)
            stderr = b""
            if process.stderr is not None:
                try:
                    stderr = process.stderr.read(_MAX_STDERR_BYTES + 1)
                except Exception:
                    stderr = b""
            result.stderr_safe = _sanitize_stderr(stderr)
        after = auth_metadata(auth_json)
        result.profile_integrity_status = "unchanged" if after == before else "changed"
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
            result.temp_home_removed = not temp_root.exists()
    return result


def call_codex_app_server_account_method(
    account_slug: str,
    *,
    method: str,
    params: object | None = None,
    include_params: bool = True,
    settings: AppSettings | None = None,
    process_factory: ProcessFactory | None = None,
) -> Any:
    settings = settings or get_settings()
    slug = validate_account_slug(account_slug)
    profile_dir = resolve_profile(settings, slug)
    auth_json = profile_dir / "auth.json"
    before = auth_metadata(auth_json)
    temp_root: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        temp_root = Path(tempfile.mkdtemp(prefix="cus-codex-account-method-"))
        os.chmod(temp_root, 0o700)
        codex_home = temp_root / "codex_home"
        codex_home.mkdir(mode=0o700)
        shutil.copy2(auth_json, codex_home / "auth.json", follow_symlinks=True)
        os.chmod(codex_home / "auth.json", 0o600)
        process = _start_process(codex_home, process_factory=process_factory)
        sent: list[str] = []
        client = _JsonLineClient(
            process,
            timeout_seconds=settings.codex_app_server_timeout_seconds,
            request_methods_sent=sent,
        )
        client.request(
            "initialize",
            {"clientInfo": {"name": "cus-c2a-observability", "version": "1"}, "capabilities": {"experimentalApi": True}},
        )
        client.notify("initialized")
        result = client.request(method, params, include_params=include_params)
        after = auth_metadata(auth_json)
        if after != before:
            raise CodexAppServerError(
                "profile_integrity_changed",
                "Codex profile auth metadata changed during app-server method",
            )
        return result
    except CodexAppServerError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexAppServerError("process_failed", "Codex app-server process failed") from exc
    finally:
        if process is not None:
            _terminate(process)
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
