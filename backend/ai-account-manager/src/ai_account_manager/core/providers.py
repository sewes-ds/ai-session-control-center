from ai_account_manager.core.errors import not_found
from ai_account_manager.core.models import ProviderDescriptor


_PROVIDER_REGISTRY = [
    ProviderDescriptor(
        provider_id="codex",
        display_name="Codex",
        status="planned_first",
        quota_supported=True,
        launch_supported=True,
        implementation_stage="wp01_foundation",
        notes="First target provider. Quota implementation is planned for a later WP.",
    ),
    ProviderDescriptor(
        provider_id="claude_code",
        display_name="Claude Code",
        status="planned",
        quota_supported=False,
        launch_supported=True,
        implementation_stage="placeholder",
        notes="Launcher/profile slot exists. Quota support must not be claimed before audit.",
    ),
    ProviderDescriptor(
        provider_id="gemini_cli",
        display_name="Gemini CLI",
        status="planned",
        quota_supported=False,
        launch_supported=True,
        implementation_stage="placeholder",
        notes="Launcher/profile slot exists. Quota support must not be claimed before audit.",
    ),
]


def provider_registry() -> list[ProviderDescriptor]:
    return list(_PROVIDER_REGISTRY)


def get_provider(provider_id: str) -> ProviderDescriptor:
    for provider in _PROVIDER_REGISTRY:
        if provider.provider_id == provider_id:
            return provider
    raise not_found(f"Unknown provider: {provider_id}")
