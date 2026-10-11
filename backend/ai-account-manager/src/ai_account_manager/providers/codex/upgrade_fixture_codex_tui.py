from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _set_process_name() -> None:
    try:
        libc = ctypes.CDLL(None)
        libc.prctl(15, b"codex", 0, 0, 0)
    except (AttributeError, OSError):
        pass


def _append(path: Path, value: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _marker(prompt: str) -> str:
    match = re.search(r"MARKER\[([A-Z0-9_-]{8,160})\]", prompt)
    return match.group(1) if match else "FIXTURE_MARKER_MISSING"


def _turn(
    path: Path,
    prompt: str,
    *,
    turn: int,
    model: str,
    effort: str,
    scenario: str,
) -> None:
    marker = _marker(prompt)
    _append(path, {"type": "event_msg", "payload": {"type": "turn_started"}})
    _append(
        path,
        {
            "type": "turn_context",
            "payload": {"model": model, "effort": effort},
        },
    )
    if scenario == "unknown":
        _append(path, {"type": "future_unknown_schema", "payload": {}})
        _append(path, {"type": "event_msg", "payload": {"type": "task_complete"}})
        return
    if scenario == "missing":
        _append(path, {"type": "event_msg", "payload": {"type": "task_complete"}})
        return
    if turn == 1:
        _append(
            path,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "id": "fixture-commentary-1",
                    "content": [{"type": "output_text", "text": "fixture commentary"}],
                },
            },
        )
        _append(path, {"type": "event_msg", "payload": {"type": "reasoning"}})
        _append(
            path,
            {"type": "response_item", "payload": {"type": "function_call", "name": "tool"}},
        )
        _append(
            path,
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "system", "content": []},
            },
        )
    final = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "id": f"fixture-final-{turn}",
            "content": [{"type": "output_text", "text": marker}],
        },
    }
    _append(path, final)
    _append(path, final)
    _append(path, {"type": "event_msg", "payload": {"type": "task_complete"}})


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("codex-cli 0.148.0")
        return 0
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", default="fixture-model")
    parser.add_argument("--cd", default=os.getcwd())
    parser.add_argument("--fixture-scenario", default="normal")
    parser.add_argument("-c", action="append", default=[])
    args, _unknown = parser.parse_known_args()
    effort = "medium"
    for value in args.c:
        match = re.fullmatch(r'model_reasoning_effort="([a-z0-9_-]+)"', value)
        if match:
            effort = match.group(1)
    profile = Path(os.environ["CODEX_HOME"])
    now = datetime.now(UTC)
    session_id = str(uuid.uuid4())
    rollout: Path | None = None

    def ensure_rollout() -> Path:
        nonlocal rollout
        if rollout is not None:
            return rollout
        sessions = profile / "sessions" / now.strftime("%Y/%m/%d")
        sessions.mkdir(parents=True, mode=0o700, exist_ok=True)
        rollout = sessions / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
        _append(
            rollout,
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(Path(args.cd).resolve()),
                    "originator": "codex-tui",
                    "source": "cli",
                },
            },
        )
        return rollout

    if args.fixture_scenario != "lazy":
        ensure_rollout()
    _set_process_name()
    turn = 0
    while True:
        print("› ", end="", flush=True)
        raw = sys.stdin.readline()
        if not raw:
            return 0
        prompt = raw.rstrip("\r\n")
        if not prompt.strip() or prompt.strip() == ".":
            continue
        turn += 1
        _turn(
            ensure_rollout(),
            prompt,
            turn=turn,
            model=args.model,
            effort=effort,
            scenario="normal" if args.fixture_scenario == "lazy" else args.fixture_scenario,
        )


if __name__ == "__main__":
    raise SystemExit(main())
