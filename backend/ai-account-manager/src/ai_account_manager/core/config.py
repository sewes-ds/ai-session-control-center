from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


_DEFAULT_HOME = Path.home()
_DEFAULT_RUNTIME_ROOT = _DEFAULT_HOME / ".local" / "share" / "ai-session-control-center"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _env_path_list(name: str, default: str) -> tuple[Path, ...]:
    raw = _env_str(name, default)
    parts = [part.strip() for part in raw.split(":") if part.strip()]
    if not parts:
        parts = [part.strip() for part in default.split(":") if part.strip()]
    return tuple(Path(part).expanduser() for part in parts)


def _env_float_range(name: str, default: str, *, greater_than: float, maximum: float) -> float:
    value = float(_env_str(name, default))
    if value <= greater_than or value > maximum:
        raise ValueError(f"{name} must be greater than {greater_than} and at most {maximum}")
    return value


def _project_root() -> Path:
    return Path(__file__).resolve().parents[5]


@dataclass(frozen=True)
class AppSettings:
    app_env: str
    app_host: str
    app_port: int
    ai_profiles_root: Path
    ai_quota_cache_root: Path
    runtime_data_root: Path
    project_bindings_path: Path
    telegram_ai_agent_root: Path
    telegram_ai_agent_topic_config_path: Path
    telegram_ai_agent_tmux_state_path: Path
    telegram_ai_agent_compatibility_manifest: Path
    codex_usage_endpoint: str
    codex_binary: Path
    codex_auth_enrollment_root: Path
    codex_auth_enrollment_ttl_seconds: int
    codex_auth_stale_after_seconds: int
    enable_provider_codex: bool
    enable_provider_claude_code: bool
    enable_provider_gemini_cli: bool
    codex_openviking_seed_account: str = ""
    telegram_ai_agent_control_socket: Path = Path(
        _DEFAULT_RUNTIME_ROOT / "telegram-ai-agent-control.sock"
    )
    ai_project_roots: tuple[Path, ...] = ()
    miniapp_static_root: Path = Path("frontend/telegram-miniapp")
    preview_access_token: str = ""
    telegram_bot_token: str = ""
    cus_telegram_bot_token: str = ""
    cus_bot_webhook_secret: str = ""
    cus_onboarding_token_key: str = ""
    cus_miniapp_deep_link_base: str = ""
    cus_allowed_forum_chat_ids: tuple[int, ...] = ()
    cus_onboarding_token_ttl_seconds: int = 1800
    telegram_allowed_user_ids: tuple[int, ...] = ()
    telegram_init_data_max_age_seconds: int = 3600
    codex_capabilities_cache_root: Path = Path(
        _DEFAULT_RUNTIME_ROOT / "codex_capabilities"
    )
    codex_app_server_timeout_seconds: float = 30.0
    project_settings_path: Path = Path(
        _DEFAULT_RUNTIME_ROOT / "project_settings_v2.json"
    )
    runtime_launch_specs_path: Path = Path(
        _DEFAULT_RUNTIME_ROOT / "runtime_launch_specs_v1.json"
    )
    project_inventory_path: Path = Path(
        _DEFAULT_RUNTIME_ROOT / "project_inventory_v1.json"
    )
    project_activity_path: Path = Path(
        _DEFAULT_RUNTIME_ROOT / "project_activity_v1.json"
    )
    upgrade_runtime_root: Path = Path(_DEFAULT_RUNTIME_ROOT / "codex-upgrade-mvp")
    upgrade_gate_runner: Path = Path("/usr/local/libexec/cus-codex-compatibility-gate")
    upgrade_engineering_runner: Path = Path("/usr/local/libexec/cus-codex-engineering-session")
    upgrade_activation_runner: Path = Path("/usr/local/libexec/cus-codex-activation")
    upgrade_runner_config_path: Path = Path("/etc/cus/codex-upgrade.json")
    upgrade_runner_timeout_seconds: float = 300.0
    upgrade_max_repair_cycles: int = 5
    upgrade_release_refresh_ttl_seconds: int = 900
    runtime_control_path: Path = Path(
        _DEFAULT_RUNTIME_ROOT / "runtime_control_v1.json"
    )
    runtime_connector_timeout_seconds: float = 10.0
    runtime_reconcile_enabled: bool = False
    runtime_reconcile_interval_seconds: float = 15.0
    runtime_reconcile_batch_size: int = 50

    def __post_init__(self) -> None:
        paths = {
            "runtime_control_path": self.runtime_control_path,
            "upgrade_runtime_root": self.upgrade_runtime_root,
            "upgrade_gate_runner": self.upgrade_gate_runner,
            "upgrade_engineering_runner": self.upgrade_engineering_runner,
            "upgrade_activation_runner": self.upgrade_activation_runner,
            "upgrade_runner_config_path": self.upgrade_runner_config_path,
        }
        for name, path in paths.items():
            if not path.is_absolute() or ".." in path.parts or path == Path("/"):
                raise ValueError(f"{name} must be an absolute non-root path without traversal")
        if self.upgrade_max_repair_cycles != 5:
            raise ValueError("UPGRADE_MAX_REPAIR_CYCLES must be exactly 5")
        if self.codex_openviking_seed_account and (
            not self.codex_openviking_seed_account[0].isalnum()
            or len(self.codex_openviking_seed_account) > 81
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for character in self.codex_openviking_seed_account
            )
        ):
            raise ValueError("CODEX_OPENVIKING_SEED_ACCOUNT is invalid")
        if not 60 <= self.upgrade_release_refresh_ttl_seconds <= 86400:
            raise ValueError("UPGRADE_RELEASE_REFRESH_TTL_SECONDS must be between 60 and 86400")
        if (
            self.cus_onboarding_token_ttl_seconds < 60
            or self.cus_onboarding_token_ttl_seconds > 86400
        ):
            raise ValueError("CUS_ONBOARDING_TOKEN_TTL_SECONDS must be between 60 and 86400")
        if any(chat_id >= 0 for chat_id in self.cus_allowed_forum_chat_ids):
            raise ValueError("CUS_ALLOWED_FORUM_CHAT_IDS must contain supergroup IDs")
        if self.cus_miniapp_deep_link_base and not self.cus_miniapp_deep_link_base.startswith(
            "https://t.me/"
        ):
            raise ValueError("CUS_MINIAPP_DEEP_LINK_BASE must be an https://t.me URL")
        if not 1.0 <= self.runtime_reconcile_interval_seconds <= 3600.0:
            raise ValueError("RUNTIME_RECONCILE_INTERVAL_SECONDS must be in [1, 3600]")
        if not 1 <= self.runtime_reconcile_batch_size <= 500:
            raise ValueError("RUNTIME_RECONCILE_BATCH_SIZE must be in [1, 500]")
        if self.runtime_reconcile_enabled:
            required = {
                "CUS_TELEGRAM_BOT_TOKEN": self.cus_telegram_bot_token,
                "CUS_BOT_WEBHOOK_SECRET": self.cus_bot_webhook_secret,
                "CUS_ONBOARDING_TOKEN_KEY": self.cus_onboarding_token_key,
                "CUS_MINIAPP_DEEP_LINK_BASE": self.cus_miniapp_deep_link_base,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError("RUNTIME_RECONCILE_ENABLED requires " + ", ".join(sorted(missing)))
            if len(self.cus_bot_webhook_secret) < 32 or len(self.cus_onboarding_token_key) < 32:
                raise ValueError(
                    "CUS Bot webhook secret and onboarding token key must be at least 32 characters"
                )
            if not self.cus_allowed_forum_chat_ids:
                raise ValueError("RUNTIME_RECONCILE_ENABLED requires CUS_ALLOWED_FORUM_CHAT_IDS")

    @property
    def codex_profiles_root(self) -> Path:
        return self.ai_profiles_root / "codex"

    @property
    def claude_code_profiles_root(self) -> Path:
        return self.ai_profiles_root / "claude_code"

    @property
    def gemini_cli_profiles_root(self) -> Path:
        return self.ai_profiles_root / "gemini_cli"

    @property
    def codex_quota_cache_root(self) -> Path:
        return self.ai_quota_cache_root / "codex"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "app_env": self.app_env,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "ai_profiles_root": str(self.ai_profiles_root),
            "ai_project_roots": [str(path) for path in self.ai_project_roots],
            "ai_quota_cache_root": str(self.ai_quota_cache_root),
            "runtime_data_root": str(self.runtime_data_root),
            "project_settings_path": str(self.project_settings_path),
            "runtime_launch_specs_path": str(self.runtime_launch_specs_path),
            "project_inventory_path": str(self.project_inventory_path),
            "project_activity_path": str(self.project_activity_path),
            "runtime_control_path": str(self.runtime_control_path),
            "telegram_ai_agent_root": str(self.telegram_ai_agent_root),
            "telegram_ai_agent_control_socket": str(self.telegram_ai_agent_control_socket),
            "miniapp_static_root": str(self.miniapp_static_root),
            "preview_access_enabled": bool(self.preview_access_token),
            "telegram_miniapp_auth_enabled": bool(
                self.cus_telegram_bot_token or self.telegram_bot_token
            ),
            "cus_telegram_bot_configured": bool(self.cus_telegram_bot_token),
            "codex_auth_stale_after_seconds": self.codex_auth_stale_after_seconds,
            "codex_auth_enrollment_ttl_seconds": self.codex_auth_enrollment_ttl_seconds,
            "enabled_providers": {
                "codex": self.enable_provider_codex,
                "claude_code": self.enable_provider_claude_code,
                "gemini_cli": self.enable_provider_gemini_cli,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    runtime_root = Path(_env_str("RUNTIME_DATA_ROOT", str(_DEFAULT_RUNTIME_ROOT))).expanduser()
    bot_root = Path(
        _env_str("TELEGRAM_AI_AGENT_ROOT", str(_DEFAULT_HOME / "telegram-ai-agent"))
    ).expanduser()
    compatibility_manifest = Path(
        _env_str(
            "TELEGRAM_AI_AGENT_COMPATIBILITY_MANIFEST",
            "/etc/ai-session-control-center/compatibility.json",
        )
    ).expanduser()
    default_miniapp_root = _project_root() / "frontend" / "telegram-miniapp"
    return AppSettings(
        app_env=_env_str("APP_ENV", "development"),
        app_host=_env_str("APP_HOST", "127.0.0.1"),
        app_port=int(_env_str("APP_PORT", "8710")),
        ai_profiles_root=Path(
            _env_str("AI_PROFILES_ROOT", str(_DEFAULT_HOME / ".local/share/ai-profiles"))
        ).expanduser(),
        ai_project_roots=_env_path_list(
            "AI_PROJECT_ROOTS",
            str(_DEFAULT_HOME / "projects"),
        ),
        ai_quota_cache_root=Path(
            _env_str("AI_QUOTA_CACHE_ROOT", str(runtime_root / "quota_snapshots"))
        ).expanduser(),
        runtime_data_root=runtime_root,
        project_bindings_path=Path(
            _env_str("PROJECT_BINDINGS_PATH", str(runtime_root / "project_bindings.json"))
        ).expanduser(),
        telegram_ai_agent_root=bot_root,
        telegram_ai_agent_topic_config_path=Path(
            _env_str("TELEGRAM_AI_AGENT_TOPIC_CONFIG_PATH", str(bot_root / "topic_config.json"))
        ).expanduser(),
        telegram_ai_agent_tmux_state_path=Path(
            _env_str(
                "TELEGRAM_AI_AGENT_TMUX_STATE_PATH",
                str(bot_root / "tmux_sessions" / "state.json"),
            )
        ).expanduser(),
        telegram_ai_agent_compatibility_manifest=compatibility_manifest,
        telegram_ai_agent_control_socket=Path(
            _env_str(
                "TELEGRAM_AI_AGENT_CONTROL_SOCKET",
                str(runtime_root / "telegram-ai-agent-control.sock"),
            )
        ).expanduser(),
        codex_usage_endpoint=_env_str(
            "CODEX_USAGE_ENDPOINT", "https://chatgpt.com/backend-api/wham/usage"
        ),
        codex_binary=Path(
            _env_str("CODEX_BINARY", str(_DEFAULT_HOME / ".local/bin/codex"))
        ).expanduser(),
        codex_auth_enrollment_root=Path(
            _env_str(
                "CODEX_AUTH_ENROLLMENT_ROOT",
                str(runtime_root / "codex_auth_enrollments"),
            )
        ).expanduser(),
        codex_auth_enrollment_ttl_seconds=int(_env_str("CODEX_AUTH_ENROLLMENT_TTL_SECONDS", "900")),
        codex_auth_stale_after_seconds=int(_env_str("CODEX_AUTH_STALE_AFTER_SECONDS", "691200")),
        codex_openviking_seed_account=_env_str("CODEX_OPENVIKING_SEED_ACCOUNT", "codex_a"),
        codex_capabilities_cache_root=Path(
            _env_str(
                "CODEX_CAPABILITIES_CACHE_ROOT",
                str(runtime_root / "codex_capabilities"),
            )
        ).expanduser(),
        codex_app_server_timeout_seconds=_env_float_range(
            "CODEX_APP_SERVER_TIMEOUT_SECONDS",
            "30.0",
            greater_than=0.0,
            maximum=120.0,
        ),
        project_settings_path=Path(
            _env_str("PROJECT_SETTINGS_PATH", str(runtime_root / "project_settings_v2.json"))
        ).expanduser(),
        runtime_launch_specs_path=Path(
            _env_str(
                "RUNTIME_LAUNCH_SPECS_PATH",
                str(runtime_root / "runtime_launch_specs_v1.json"),
            )
        ).expanduser(),
        project_inventory_path=Path(
            _env_str("PROJECT_INVENTORY_PATH", str(runtime_root / "project_inventory_v1.json"))
        ).expanduser(),
        project_activity_path=Path(
            _env_str("PROJECT_ACTIVITY_PATH", str(runtime_root / "project_activity_v1.json"))
        ).expanduser(),
        runtime_control_path=Path(
            _env_str("RUNTIME_CONTROL_PATH", str(runtime_root / "runtime_control_v1.json"))
        ).expanduser(),
        runtime_connector_timeout_seconds=_env_float_range(
            "RUNTIME_CONNECTOR_TIMEOUT_SECONDS",
            "10.0",
            greater_than=0.0,
            maximum=120.0,
        ),
        upgrade_runtime_root=Path(
            _env_str("UPGRADE_RUNTIME_ROOT", str(runtime_root / "codex-upgrade-mvp"))
        ).expanduser(),
        upgrade_gate_runner=Path(
            _env_str("UPGRADE_GATE_RUNNER", "/usr/local/libexec/cus-codex-compatibility-gate")
        ).expanduser(),
        upgrade_engineering_runner=Path(
            _env_str(
                "UPGRADE_ENGINEERING_RUNNER",
                "/usr/local/libexec/cus-codex-engineering-session",
            )
        ).expanduser(),
        upgrade_activation_runner=Path(
            _env_str("UPGRADE_ACTIVATION_RUNNER", "/usr/local/libexec/cus-codex-activation")
        ).expanduser(),
        upgrade_runner_config_path=Path(
            _env_str("UPGRADE_RUNNER_CONFIG_PATH", "/etc/cus/codex-upgrade.json")
        ).expanduser(),
        upgrade_runner_timeout_seconds=_env_float_range(
            "UPGRADE_RUNNER_TIMEOUT_SECONDS",
            "300.0",
            greater_than=0.0,
            maximum=900.0,
        ),
        upgrade_max_repair_cycles=int(_env_str("UPGRADE_MAX_REPAIR_CYCLES", "5")),
        upgrade_release_refresh_ttl_seconds=int(
            _env_str("UPGRADE_RELEASE_REFRESH_TTL_SECONDS", "900")
        ),
        miniapp_static_root=Path(
            _env_str("MINIAPP_STATIC_ROOT", str(default_miniapp_root))
        ).expanduser(),
        preview_access_token=_env_str("CUS_PREVIEW_TOKEN", ""),
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN", ""),
        cus_telegram_bot_token=_env_str("CUS_TELEGRAM_BOT_TOKEN", ""),
        cus_bot_webhook_secret=_env_str("CUS_BOT_WEBHOOK_SECRET", ""),
        cus_onboarding_token_key=_env_str("CUS_ONBOARDING_TOKEN_KEY", ""),
        cus_miniapp_deep_link_base=_env_str("CUS_MINIAPP_DEEP_LINK_BASE", ""),
        cus_allowed_forum_chat_ids=tuple(
            int(item.strip())
            for item in _env_str("CUS_ALLOWED_FORUM_CHAT_IDS", "").split(",")
            if item.strip()
        ),
        cus_onboarding_token_ttl_seconds=int(_env_str("CUS_ONBOARDING_TOKEN_TTL_SECONDS", "1800")),
        runtime_reconcile_enabled=_env_bool("RUNTIME_RECONCILE_ENABLED", False),
        runtime_reconcile_interval_seconds=_env_float_range(
            "RUNTIME_RECONCILE_INTERVAL_SECONDS",
            "15.0",
            greater_than=0.0,
            maximum=3600.0,
        ),
        runtime_reconcile_batch_size=int(_env_str("RUNTIME_RECONCILE_BATCH_SIZE", "50")),
        telegram_allowed_user_ids=tuple(
            int(item.strip())
            for item in _env_str("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
            if item.strip()
        ),
        telegram_init_data_max_age_seconds=int(
            _env_str("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "3600")
        ),
        enable_provider_codex=_env_bool("ENABLE_PROVIDER_CODEX", True),
        enable_provider_claude_code=_env_bool("ENABLE_PROVIDER_CLAUDE_CODE", False),
        enable_provider_gemini_cli=_env_bool("ENABLE_PROVIDER_GEMINI_CLI", False),
    )
