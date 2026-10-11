"""Operator CLI for dry-run/commit of a reviewed Runtime migration plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_account_manager.core.runtime_migration import (
    RuntimeMigrationError,
    commit_runtime_migration,
    prepare_runtime_migration,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="cus-runtime-migration-import")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    namespace = parser.parse_args()
    try:
        prepared = prepare_runtime_migration(
            plan_path=namespace.plan,
            store_path=namespace.store,
        )
        store_revision: int | None = None
        if namespace.commit:
            store = commit_runtime_migration(namespace.store, prepared)
            store_revision = store.store_revision
        print(
            json.dumps(
                {
                    "status": "COMMITTED" if namespace.commit else "DRY_RUN_OK",
                    "plan_digest": prepared.plan_digest,
                    "source_inventory_digest": prepared.source_inventory_digest,
                    "agent_instance_id": prepared.agent_instance_id,
                    "generation_id": prepared.generation_id,
                    "managed_count": prepared.managed_count,
                    "unmanaged_count": prepared.unmanaged_count,
                    "excluded_count": prepared.excluded_count,
                    "store_revision": store_revision,
                },
                indent=2,
                sort_keys=True,
            )
        )
    except RuntimeMigrationError as exc:
        print(f"cus-runtime-migration-import: {exc.code}: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
