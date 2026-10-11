from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.launcher import build_project_launch_spec
from ai_account_manager.core.project_settings import read_project_settings_store
from ai_account_manager.core.runtime_launch_specs import reconcile_runtime_launch_spec_store


def _utc_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reconcile_reasoning_contract_specs(
    settings: AppSettings | None = None,
) -> dict[str, Any]:
    """Regenerate every configured Codex spec without starting any session."""

    settings = settings or get_settings()
    desired, _migration_required = read_project_settings_store(settings)
    records: list[dict[str, Any]] = []
    for project_id, raw in sorted(desired.get("projects", {}).items()):
        if not isinstance(raw, dict) or raw.get("provider_id") != "codex":
            continue
        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            continue
        spec = build_project_launch_spec(
            project_id=project_id,
            expected_settings_revision=revision,
            allow_last_known_good=False,
            settings=settings,
        )
        records.append(
            {
                "project_id": spec.project_id,
                "project_path": spec.project_path,
                "provider_id": spec.provider_id,
                "account_profile": spec.account_profile,
                "model": spec.model,
                "reasoning_effort": spec.reasoning_effort,
                "settings_revision": spec.settings_revision,
                "spec_sha256": spec.spec_sha256,
                "applied_at": _utc_iso(),
                "source": "project_settings_v2",
            }
        )
    return reconcile_runtime_launch_spec_store(settings.runtime_launch_specs_path, records)


def main() -> int:
    result = reconcile_reasoning_contract_specs()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
