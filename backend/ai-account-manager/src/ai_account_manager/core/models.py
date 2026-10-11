from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    status: str
    quota_supported: bool
    launch_supported: bool
    implementation_stage: str
    notes: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "status": self.status,
            "quota_supported": self.quota_supported,
            "launch_supported": self.launch_supported,
            "implementation_stage": self.implementation_stage,
            "notes": self.notes,
        }
