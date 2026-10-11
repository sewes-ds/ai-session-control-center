from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ai_account_manager.schemas.sessions import ObservedProjectSession


_MAX_SESSION_META_BYTES = 512 * 1024
_MAX_ROLLOUT_SCAN_BYTES = 8 * 1024 * 1024
_MAX_ROLLOUT_SCAN_LINES = 50_000
_ROLLOUT_CHUNK_BYTES = 64 * 1024
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EFFORT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CURRENT_EFFORT_CONFIG_RE = re.compile(
    r'^model_reasoning_effort\s*=\s*"?([a-z0-9][a-z0-9_-]{0,31})"?$'
)
_LEGACY_EFFORT_CONFIG_RE = re.compile(r'^reasoning_effort\s*=\s*"?([a-z0-9][a-z0-9_-]{0,31})"?$')
_TUI_STATUS_MODEL_RE = re.compile(
    r"^\s*│?\s*Model:\s+([A-Za-z0-9][A-Za-z0-9._-]{0,127})\s+"
    r"\(reasoning\s+([a-z0-9][a-z0-9_-]{0,31}),"
)
_TUI_STATUS_SESSION_RE = re.compile(
    r"^\s*│?\s*Session:\s+([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})(?:\s|│|$)"
)
_PER_PROJECT_TIMEOUT_SECONDS = 2.0
_BATCH_TIMEOUT_SECONDS = 10.0
_MAX_CONCURRENT_PROBES = 2


class RuntimeSelectionRefreshBusy(Exception):
    pass


class PaneLike(Protocol):
    pane_pid: int | None
    pane_path: str | None


@dataclass(frozen=True)
class RuntimeSelectionIdentity:
    project_key: str
    session_name: str
    session_id: str
    pane_pid: int


@dataclass(frozen=True)
class RuntimeSelectionProbeContext:
    identity: RuntimeSelectionIdentity
    agent: str
    project_path: str
    pane_path: str
    transcript_path: str | None


@dataclass(frozen=True)
class RuntimeSelectionValue:
    status: str
    model: str | None = None
    reasoning_effort: str | None = None


_UNKNOWN = RuntimeSelectionValue(status="unknown")
_cache: dict[RuntimeSelectionIdentity, RuntimeSelectionValue] = {}
_cache_lock = threading.Lock()
_batch_state_lock = threading.Lock()
_batch_active = False


def _project_key(project: ObservedProjectSession) -> str:
    return project.project_id or project.composite_key or f"topic:{project.thread_id}"


def _state_record(
    project: ObservedProjectSession,
    state_raw: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if project.composite_key is None:
        return None
    raw = state_raw.get(project.composite_key)
    return raw if isinstance(raw, Mapping) else None


def _identity_and_context(
    project: ObservedProjectSession,
    state_raw: Mapping[str, Any],
    panes: Mapping[str, PaneLike],
) -> tuple[RuntimeSelectionIdentity, RuntimeSelectionProbeContext] | None:
    record = _state_record(project, state_raw)
    if record is None or record.get("provider") != project.agent:
        return None
    session_name = record.get("session_name")
    session_id = record.get("session_id")
    transcript_path = record.get("transcript_path")
    state_cwd = record.get("cwd")
    if not all(isinstance(item, str) and item for item in (session_name, session_id, state_cwd)):
        return None
    if transcript_path is not None and not (isinstance(transcript_path, str) and transcript_path):
        return None
    if project.tmux_session_name != session_name or project.project_path != state_cwd:
        return None
    pane = panes.get(session_name)
    if pane is None or not isinstance(pane.pane_pid, int) or pane.pane_pid <= 0:
        return None
    if not isinstance(pane.pane_path, str) or not pane.pane_path:
        return None
    identity = RuntimeSelectionIdentity(
        project_key=_project_key(project),
        session_name=session_name,
        session_id=session_id,
        pane_pid=pane.pane_pid,
    )
    return identity, RuntimeSelectionProbeContext(
        identity=identity,
        agent=project.agent,
        project_path=state_cwd,
        pane_path=pane.pane_path,
        transcript_path=transcript_path,
    )


def _invalidate_other_identities(project_key: str, keep: RuntimeSelectionIdentity | None) -> None:
    with _cache_lock:
        stale = [
            identity
            for identity in _cache
            if identity.project_key == project_key and identity != keep
        ]
        for identity in stale:
            _cache.pop(identity, None)


def _set_project_value(project: ObservedProjectSession, value: RuntimeSelectionValue) -> None:
    project.runtime_selection_status = value.status
    project.current_model = value.model if value.status == "current" else None
    project.current_reasoning_effort = value.reasoning_effort if value.status == "current" else None


def apply_cached_runtime_selections(
    projects: Sequence[ObservedProjectSession],
    state_raw: Mapping[str, Any],
    panes: Mapping[str, PaneLike],
) -> None:
    for project in projects:
        project_key = _project_key(project)
        if project.agent not in _PROVIDER_PROBES:
            _invalidate_other_identities(project_key, None)
            _set_project_value(project, RuntimeSelectionValue(status="unsupported"))
            continue
        if not project.tmux_session_running:
            _invalidate_other_identities(project_key, None)
            _set_project_value(project, RuntimeSelectionValue(status="not_running"))
            continue
        resolved = _identity_and_context(project, state_raw, panes)
        if resolved is None:
            _invalidate_other_identities(project_key, None)
            _set_project_value(project, _UNKNOWN)
            continue
        identity, _context = resolved
        _invalidate_other_identities(project_key, identity)
        with _cache_lock:
            cached = _cache.get(identity, _UNKNOWN)
        _set_project_value(project, cached)


def _read_codex_home(pid: int) -> Path | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator and key == b"CODEX_HOME":
            try:
                decoded = value.decode("utf-8")
            except UnicodeDecodeError:
                return None
            return Path(decoded).expanduser() if decoded else None
    return None


def _child_pids(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return []
    result: list[int] = []
    for item in raw.split():
        try:
            result.append(int(item))
        except ValueError:
            continue
    return result


def _is_codex_process(pid: int) -> bool:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if comm == "codex":
        return True
    try:
        return Path(f"/proc/{pid}/exe").resolve(strict=True).name == "codex"
    except OSError:
        return False


def _exact_codex_process(pane_pid: int) -> tuple[int, Path] | None:
    queue = [(pane_pid, 0)]
    seen: set[int] = set()
    candidates: list[tuple[int, int, Path]] = []
    while queue:
        pid, depth = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        if _is_codex_process(pid):
            codex_home = _read_codex_home(pid)
            if codex_home is not None:
                candidates.append((depth, pid, codex_home))
        queue.extend((child, depth + 1) for child in _child_pids(pid))
    if not candidates:
        return None
    nearest_depth = min(depth for depth, _pid, _home in candidates)
    nearest = [(pid, home) for depth, pid, home in candidates if depth == nearest_depth]
    if len(nearest) != 1:
        return None
    pid, home = nearest[0]
    try:
        return pid, home.resolve(strict=True)
    except OSError:
        return None


def _exact_codex_home(pane_pid: int) -> Path | None:
    process = _exact_codex_process(pane_pid)
    return process[1] if process is not None else None


def _selection_from_process(pane_pid: int) -> RuntimeSelectionValue | None:
    process = _exact_codex_process(pane_pid)
    if process is None:
        return None
    pid, _home = process
    try:
        argv = [
            item.decode("utf-8")
            for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if item
        ]
    except (OSError, UnicodeDecodeError):
        return None
    models: list[str] = []
    efforts: list[str] = []
    for index, item in enumerate(argv):
        if item in {"--model", "-m"}:
            if index + 1 >= len(argv):
                return None
            candidate = argv[index + 1]
            if not _MODEL_RE.fullmatch(candidate):
                return None
            models.append(candidate)
        if item in {"--config", "-c"}:
            if index + 1 >= len(argv):
                return None
            config = argv[index + 1]
            current = _CURRENT_EFFORT_CONFIG_RE.fullmatch(config)
            legacy = _LEGACY_EFFORT_CONFIG_RE.fullmatch(config)
            if current:
                efforts.append(current.group(1))
            elif legacy:
                # Read-only compatibility for sessions launched before the
                # corrected outbound contract was deployed. New launches must
                # always use model_reasoning_effort.
                efforts.append(legacy.group(1))
    if len(models) != 1 or len(efforts) != 1:
        return None
    return RuntimeSelectionValue(
        status="current",
        model=models[0],
        reasoning_effort=efforts[0],
    )


def _parse_tui_status(stdout: str, *, session_id: str) -> RuntimeSelectionValue | None:
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        model_match = _TUI_STATUS_MODEL_RE.match(lines[index])
        if model_match is None:
            continue
        for candidate in lines[index + 1 : index + 16]:
            session_match = _TUI_STATUS_SESSION_RE.match(candidate)
            if session_match is None:
                continue
            if session_match.group(1) != session_id:
                return None
            return RuntimeSelectionValue(
                status="current",
                model=model_match.group(1),
                reasoning_effort=model_match.group(2),
            )
    return None


def _selection_from_tui_status(
    session_name: str, *, session_id: str
) -> RuntimeSelectionValue | None:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_name, "-S", "-120"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_tui_status(result.stdout, session_id=session_id)


def _parse_json_object(raw_line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw_line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_session_meta(path: Path) -> tuple[str, str] | None:
    try:
        with path.open("rb") as stream:
            remaining = _MAX_SESSION_META_BYTES
            while remaining > 0:
                line = stream.readline(min(remaining, _ROLLOUT_CHUNK_BYTES))
                if not line:
                    break
                remaining -= len(line)
                value = _parse_json_object(line)
                if value is None or value.get("type") != "session_meta":
                    continue
                payload = value.get("payload")
                if not isinstance(payload, dict):
                    return None
                session_id = payload.get("id")
                cwd = payload.get("cwd")
                if isinstance(session_id, str) and isinstance(cwd, str):
                    return session_id, cwd
                return None
    except OSError:
        return None
    return None


def _reverse_rollout_lines(path: Path) -> Iterator[bytes]:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            position = size
            buffered = b""
            scanned_bytes = 0
            yielded_lines = 0
            while position > 0 and scanned_bytes < _MAX_ROLLOUT_SCAN_BYTES:
                read_size = min(
                    _ROLLOUT_CHUNK_BYTES,
                    position,
                    _MAX_ROLLOUT_SCAN_BYTES - scanned_bytes,
                )
                position -= read_size
                stream.seek(position)
                chunk = stream.read(read_size)
                scanned_bytes += len(chunk)
                parts = (chunk + buffered).split(b"\n")
                buffered = parts[0]
                for line in reversed(parts[1:]):
                    yielded_lines += 1
                    if yielded_lines > _MAX_ROLLOUT_SCAN_LINES:
                        return
                    yield line
            if position == 0 and buffered and yielded_lines < _MAX_ROLLOUT_SCAN_LINES:
                yield buffered
    except OSError:
        return


def _selection_from_rollout(path: Path) -> RuntimeSelectionValue:
    try:
        before = path.stat()
    except OSError:
        return _UNKNOWN
    result = _UNKNOWN
    for raw_line in _reverse_rollout_lines(path):
        value = _parse_json_object(raw_line)
        if value is None:
            continue
        payload = value.get("payload")
        if not isinstance(payload, dict):
            continue
        model: object
        effort: object
        if value.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if not isinstance(settings, dict):
                continue
            model = settings.get("model")
            effort = settings.get("reasoning_effort")
        elif value.get("type") == "turn_context":
            model = payload.get("model")
            effort = payload.get("effort")
        else:
            continue
        if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
            break
        if effort is not None and (not isinstance(effort, str) or not _EFFORT_RE.fullmatch(effort)):
            break
        result = RuntimeSelectionValue(
            status="current",
            model=model,
            reasoning_effort=effort,
        )
        break
    try:
        after = path.stat()
    except OSError:
        return _UNKNOWN
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        return _UNKNOWN
    return result


def _probe_codex(context: RuntimeSelectionProbeContext) -> RuntimeSelectionValue:
    try:
        project_path = Path(context.project_path).expanduser().resolve(strict=True)
        pane_path = Path(context.pane_path).expanduser().resolve(strict=True)
    except OSError:
        return _UNKNOWN
    if not project_path.is_dir() or pane_path != project_path:
        return _UNKNOWN
    codex_home = _exact_codex_home(context.identity.pane_pid)
    if codex_home is None:
        return _UNKNOWN
    process_selection = _selection_from_process(context.identity.pane_pid)
    if process_selection is None:
        return _UNKNOWN
    if context.transcript_path is None:
        tui_selection = _selection_from_tui_status(
            context.identity.session_name,
            session_id=context.identity.session_id,
        )
        return tui_selection if tui_selection == process_selection else _UNKNOWN
    try:
        sessions_root = (codex_home / "sessions").resolve(strict=True)
        rollout_raw = Path(context.transcript_path).expanduser()
        if rollout_raw.is_symlink() or not rollout_raw.is_file():
            return _UNKNOWN
        rollout = rollout_raw.resolve(strict=True)
        rollout.relative_to(sessions_root)
    except (OSError, ValueError):
        return _UNKNOWN
    meta = _read_session_meta(rollout)
    if meta is None:
        return _UNKNOWN
    session_id, meta_cwd_raw = meta
    if session_id != context.identity.session_id:
        return _UNKNOWN
    try:
        meta_cwd = Path(meta_cwd_raw).expanduser().resolve(strict=True)
    except OSError:
        return _UNKNOWN
    if meta_cwd != project_path:
        return _UNKNOWN
    selection = _selection_from_rollout(rollout)
    if selection.status != "current" or process_selection.model != selection.model:
        return _UNKNOWN
    if selection.reasoning_effort is not None:
        # Persisted turn/settings metadata is the actual runtime truth. The
        # process argv proves the exact process identity and launch selection,
        # but must not overwrite a different effort reported by the rollout.
        return selection
    return RuntimeSelectionValue(
        status="current",
        model=selection.model,
        reasoning_effort=process_selection.reasoning_effort,
    )


ProviderProbe = Callable[[RuntimeSelectionProbeContext], RuntimeSelectionValue]
_PROVIDER_PROBES: dict[str, ProviderProbe] = {"codex": _probe_codex}


async def refresh_runtime_selections(
    projects: Sequence[ObservedProjectSession],
    state_raw: Mapping[str, Any],
    panes: Mapping[str, PaneLike],
) -> None:
    global _batch_active
    with _batch_state_lock:
        if _batch_active:
            raise RuntimeSelectionRefreshBusy
        _batch_active = True
    try:
        contexts: list[tuple[ObservedProjectSession, RuntimeSelectionProbeContext]] = []
        for project in projects:
            project_key = _project_key(project)
            if project.agent not in _PROVIDER_PROBES:
                _invalidate_other_identities(project_key, None)
                _set_project_value(project, RuntimeSelectionValue(status="unsupported"))
                continue
            if not project.tmux_session_running:
                _invalidate_other_identities(project_key, None)
                _set_project_value(project, RuntimeSelectionValue(status="not_running"))
                continue
            resolved = _identity_and_context(project, state_raw, panes)
            if resolved is None:
                _invalidate_other_identities(project_key, None)
                _set_project_value(project, _UNKNOWN)
                continue
            identity, context = resolved
            _invalidate_other_identities(project_key, identity)
            contexts.append((project, context))

        if not contexts:
            return

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)

        async def probe_one(
            project: ObservedProjectSession,
            context: RuntimeSelectionProbeContext,
        ) -> tuple[ObservedProjectSession, RuntimeSelectionIdentity, RuntimeSelectionValue]:
            probe = _PROVIDER_PROBES.get(context.agent)
            if probe is None:
                return project, context.identity, RuntimeSelectionValue(status="unsupported")
            async with semaphore:
                try:
                    value = await asyncio.wait_for(
                        asyncio.to_thread(probe, context),
                        timeout=_PER_PROJECT_TIMEOUT_SECONDS,
                    )
                except Exception:
                    value = _UNKNOWN
            return project, context.identity, value

        tasks = [asyncio.create_task(probe_one(project, context)) for project, context in contexts]
        done, pending = await asyncio.wait(tasks, timeout=_BATCH_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        completed_by_identity: dict[RuntimeSelectionIdentity, RuntimeSelectionValue] = {}
        for task in done:
            if task.cancelled():
                continue
            try:
                _project, identity, value = task.result()
            except Exception:
                continue
            completed_by_identity[identity] = value
        for project, context in contexts:
            value = completed_by_identity.get(context.identity, _UNKNOWN)
            with _cache_lock:
                _cache[context.identity] = value
            _set_project_value(project, value)
    finally:
        with _batch_state_lock:
            _batch_active = False


def clear_runtime_selection_cache() -> None:
    with _cache_lock:
        _cache.clear()
