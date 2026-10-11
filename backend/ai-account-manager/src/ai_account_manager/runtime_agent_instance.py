"""Operator CLI for validated AgentInstance registration and lifecycle changes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ai_account_manager.core.runtime_control import (
    RuntimeControlStoreError,
    register_agent_instance,
    validate_agent_instance_registration,
)
from ai_account_manager.schemas.runtime_control import AgentInstanceRecord, canonical_digest


def _read_descriptor(path: Path) -> AgentInstanceRecord:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024:
        raise RuntimeControlStoreError(
            "agent_instance_descriptor_invalid",
            "AgentInstance descriptor must be a bounded regular non-symlink JSON file",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return AgentInstanceRecord.model_validate(value)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeControlStoreError(
            "agent_instance_descriptor_invalid",
            "AgentInstance descriptor is invalid",
        ) from exc


def main() -> None:
    parser = argparse.ArgumentParser(prog="cus-runtime-agent-instance")
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    namespace = parser.parse_args()
    try:
        record = _read_descriptor(namespace.descriptor.expanduser())
        before = validate_agent_instance_registration(namespace.store, record)
        after_revision: int | None = None
        changed = before.agent_instances.get(record.agent_instance_id) != record
        if namespace.commit:
            after = register_agent_instance(namespace.store, record)
            after_revision = after.store_revision
        print(
            json.dumps(
                {
                    "status": "COMMITTED" if namespace.commit else "DRY_RUN_OK",
                    "agent_instance_id": record.agent_instance_id,
                    "forum_chat_id": record.forum_chat_id,
                    "active": record.active,
                    "descriptor_digest": canonical_digest(record.model_dump()),
                    "change_required": changed,
                    "store_revision": after_revision,
                    "contains_bot_token": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
    except RuntimeControlStoreError as exc:
        print(f"cus-runtime-agent-instance: {exc.code}: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
