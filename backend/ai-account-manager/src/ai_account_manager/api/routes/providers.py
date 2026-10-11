from fastapi import APIRouter

from ai_account_manager.core.providers import get_provider, provider_registry
from ai_account_manager.schemas.providers import ProviderPublic

router = APIRouter(tags=["providers"])


@router.get("/providers", response_model=list[ProviderPublic])
def list_providers() -> list[ProviderPublic]:
    return [ProviderPublic.from_descriptor(provider) for provider in provider_registry()]


@router.get("/providers/{provider_id}", response_model=ProviderPublic)
def read_provider(provider_id: str) -> ProviderPublic:
    return ProviderPublic.from_descriptor(get_provider(provider_id))
