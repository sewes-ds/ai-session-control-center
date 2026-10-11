from pydantic import BaseModel

from ai_account_manager.core.models import ProviderDescriptor


class ProviderPublic(BaseModel):
    provider_id: str
    display_name: str
    status: str
    quota_supported: bool
    launch_supported: bool
    implementation_stage: str
    notes: str

    @classmethod
    def from_descriptor(cls, descriptor: ProviderDescriptor) -> "ProviderPublic":
        return cls(**descriptor.to_public_dict())
