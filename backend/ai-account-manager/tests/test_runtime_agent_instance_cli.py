from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ai_account_manager.core.runtime_control import read_runtime_control_store
from ai_account_manager.runtime_agent_instance import main


def _descriptor() -> dict[str, object]:
    return {
        "agent_instance_id": "ta_staging_01",
        "pinned_upstream_sha": "0" * 40,
        "telegram_bot_identity": "staging-working-bot",
        "telegram_bot_token_fingerprint": "sha256:" + "1" * 64,
        "forum_chat_id": -100123,
        "runtime_socket": "/run/user/1000/ai-agent-runtime/ta_staging_01/runtime.sock",
        "active": True,
    }


def test_agent_instance_cli_is_dry_run_by_default_and_commit_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    descriptor = tmp_path / "agent.json"
    descriptor.write_text(json.dumps(_descriptor()), encoding="utf-8")
    store = tmp_path / "runtime-control.json"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cus-runtime-agent-instance",
            "--descriptor",
            str(descriptor),
            "--store",
            str(store),
        ],
    )
    main()
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["status"] == "DRY_RUN_OK"
    assert dry_run["contains_bot_token"] is False
    assert dry_run["change_required"] is True
    assert not store.exists()

    monkeypatch.setattr(sys, "argv", [*sys.argv, "--commit"])
    main()
    committed = json.loads(capsys.readouterr().out)
    assert committed["status"] == "COMMITTED"
    first_revision = committed["store_revision"]
    assert read_runtime_control_store(store).agent_instances["ta_staging_01"].active is True

    main()
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["change_required"] is False
    assert repeated["store_revision"] == first_revision


def test_agent_instance_cli_rejects_symlink_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_descriptor()), encoding="utf-8")
    descriptor = tmp_path / "agent.json"
    descriptor.symlink_to(source)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cus-runtime-agent-instance",
            "--descriptor",
            str(descriptor),
            "--store",
            str(tmp_path / "runtime-control.json"),
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 2
