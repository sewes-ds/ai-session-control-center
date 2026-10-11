from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from telegram_bot.core.services import codex_rollout
from telegram_bot.core.services.codex_rollout import extract_codex_assistant_output

from ai_account_manager.providers.codex.upgrade_rollout_contract import (
    is_task_complete,
    validate_rollout_records,
)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    try:
        records = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 2
    if not isinstance(records, list):
        return 2
    failure_codes = validate_rollout_records(records)
    finals: list[str] = []
    commentary: list[str] = []
    seen: set[str] = set()
    ignored = 0
    completed = False
    for record in records:
        if isinstance(record, dict) and is_task_complete(record):
            completed = True
        output = extract_codex_assistant_output(record)
        if output is None:
            ignored += 1
            continue
        identity = output.identity or hashlib.sha256(output.text.encode("utf-8")).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        if output.phase == "final_answer":
            finals.append(output.text)
        else:
            commentary.append(output.text)
    if completed and not finals and "COMPLETION_WITHOUT_FINAL" not in failure_codes:
        failure_codes.append("COMPLETION_WITHOUT_FINAL")
    payload = {
        "finals": finals,
        "commentary": commentary,
        "ignored": ignored,
        "import_origin": str(Path(codex_rollout.__file__).resolve()),
        "machine_verdict": "CHECK_FAILED" if failure_codes else "PASS",
        "failure_codes": failure_codes,
        "completion_seen": completed,
    }
    target.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    target.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
