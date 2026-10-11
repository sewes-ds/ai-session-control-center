from __future__ import annotations

import json
from typing import Any


KNOWN_OUTER_TYPES = {
    "session_meta",
    "turn_context",
    "event_msg",
    "response_item",
    "token_usage_record",
    "ghost_snapshot",
    "compacted",
    "world_state",
}

KNOWN_EVENT_TYPES = {
    "agent_message",
    "context_compacted",
    "error",
    "exec_command_begin",
    "exec_command_end",
    "item_completed",
    "item_started",
    "mcp_startup_complete",
    "mcp_startup_update",
    "patch_apply_begin",
    "patch_apply_end",
    "plan_update",
    "reasoning",
    "stream_error",
    "task_complete",
    "task_started",
    "thread_settings_applied",
    "token_count",
    "turn_complete",
    "turn_started",
    "user_message",
    "warning",
    "web_search_begin",
    "web_search_end",
}

KNOWN_RESPONSE_TYPES = {
    "computer_call",
    "custom_tool_call",
    "custom_tool_call_output",
    "file_search_call",
    "function_call",
    "function_call_output",
    "image_generation_call",
    "local_shell_call",
    "mcp_call",
    "mcp_list_tools",
    "message",
    "reasoning",
    "web_search_call",
}

TOKEN_USAGE_ID_FIELDS = {
    "thread_id",
    "turn_id",
    "session_id",
    "root_turn_id",
    "response_id",
}

TOKEN_USAGE_FIELDS = {
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
}


def _valid_token_usage(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    required = (value.get(field) for field in TOKEN_USAGE_FIELDS)
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in required):
        return False
    cache_write = value.get("cache_write_input_tokens", 0)
    return isinstance(cache_write, int) and not isinstance(cache_write, bool)


def _valid_token_usage_record(payload: dict[str, Any]) -> bool:
    if not all(isinstance(payload.get(field), str) for field in TOKEN_USAGE_ID_FIELDS):
        return False
    return all(
        _valid_token_usage(payload.get(field))
        for field in ("usage", "turn_token_usage", "thread_token_usage")
    )


def rollout_schema_failure(record: object) -> str | None:
    """Return an explicit failure for every unrecognised persisted shape."""

    if not isinstance(record, dict):
        return "MALFORMED_ROLLOUT_RECORD"
    outer = record.get("type")
    payload = record.get("payload")
    if not isinstance(outer, str) or outer not in KNOWN_OUTER_TYPES:
        return "UNKNOWN_ROLLOUT_SCHEMA"
    if not isinstance(payload, dict):
        return "MALFORMED_ROLLOUT_RECORD"
    if outer == "event_msg":
        event_type = payload.get("type")
        if not isinstance(event_type, str) or event_type not in KNOWN_EVENT_TYPES:
            return "UNKNOWN_ROLLOUT_SCHEMA"
    elif outer == "response_item":
        response_type = payload.get("type")
        if not isinstance(response_type, str) or response_type not in KNOWN_RESPONSE_TYPES:
            return "UNKNOWN_ROLLOUT_SCHEMA"
    elif outer == "token_usage_record" and not _valid_token_usage_record(payload):
        return "MALFORMED_ROLLOUT_RECORD"
    return None


def validate_rollout_records(records: object) -> list[str]:
    if not isinstance(records, list):
        return ["MALFORMED_ROLLOUT_DOCUMENT"]
    failures: list[str] = []
    for record in records:
        failure = rollout_schema_failure(record)
        if failure is not None and failure not in failures:
            failures.append(failure)
    return failures


def candidate_backend_failure(record: dict[str, Any]) -> str | None:
    """Classify the known 0.148 Sol backend rejection from structured error events."""

    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") not in {"error", "stream_error"}:
        return None
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()
    if "invalid_parameter" in normalized and "prompt_cache_retention" in normalized:
        return "INVALID_PARAMETER_PROMPT_CACHE_RETENTION"
    return None


def is_task_complete(record: dict[str, Any]) -> bool:
    return (
        record.get("type") == "event_msg"
        and isinstance(record.get("payload"), dict)
        and record["payload"].get("type") in {"task_complete", "turn_complete"}
    )
