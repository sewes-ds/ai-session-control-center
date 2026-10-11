from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram.enums import ChatType
from ai_account_manager.integrations.runtime_selection import (
    RuntimeSelectionIdentity,
    RuntimeSelectionProbeContext,
    _probe_codex,
)
from ai_account_manager.providers.codex.managed_profile_update import (
    reconcile_disposable_project_trust,
)
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    assert_private_directory,
    atomic_write_bytes,
    atomic_write_model,
    canonical_path,
    clean_environment,
    file_sha256,
    read_private_model,
    safe_executable,
)
from ai_account_manager.providers.codex.upgrade_rollout_contract import (
    candidate_backend_failure,
    is_task_complete,
    rollout_schema_failure,
)
from ai_account_manager.providers.codex.upgrade_telegram_e2e_contract import (
    TelegramE2ERequest,
    TelegramE2EResult,
)
from telegram_bot import __file__ as telegram_package_file
from telegram_bot.__main__ import process_queue_item
from telegram_bot.core.services import codex_rollout
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.providers import CodexAdapter


@dataclass
class _TransportItem:
    message_id: int
    text: str


class _DisposableTransport:
    """A deliberately non-network transport: it only appends local values."""

    def __init__(self) -> None:
        self.items: list[_TransportItem] = []

    async def deliver(self, text: str) -> _TransportItem:
        item = _TransportItem(message_id=len(self.items) + 1, text=text)
        self.items.append(item)
        return item


class _Message:
    def __init__(self, transport: _DisposableTransport, message_id: int) -> None:
        self._transport = transport
        self.message_id = message_id
        self.chat = SimpleNamespace(id=71001, type=ChatType.PRIVATE)
        self.bot = None

    async def answer(self, text: str, **_kwargs: object) -> _TransportItem:
        return await self._transport.deliver(text)


class _Bot:
    async def send_message(self, _chat_id: int, text: str, **_kwargs: object) -> _TransportItem:
        raise RuntimeError(f"network transport unavailable: {text[:20]}")


class _SessionManager:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.records: list[tuple[int, str]] = []

    def get_current_session_id(self, _channel: tuple[int, int]) -> str | None:
        return self.session_id

    def consume_fresh_start(self, _channel: tuple[int, int]) -> bool:
        return False

    async def override_session(self, _channel: tuple[int, int], session_id: str) -> None:
        self.session_id = session_id

    def record_message(
        self,
        message_id: int,
        session_id: str,
        _channel: tuple[int, int],
        **_kwargs: object,
    ) -> None:
        self.records.append((message_id, session_id))

    def get_mode(self, _channel: tuple[int, int]) -> str:
        return "project"

    async def cancel(self, _channel: tuple[int, int]) -> bool:
        return False


def _run_tmux(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        shell=False,
        check=False,
        umask=0o077,
    )


def _load_project_state(path: Path) -> tuple[str, set[str]]:
    digest = file_sha256(path, code="PROJECT_STATE_INVALID")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationalError("PROJECT_STATE_INVALID") from exc
    projects = raw.get("projects") if isinstance(raw, dict) else None
    if not isinstance(projects, dict):
        raise OperationalError("PROJECT_STATE_INVALID")
    stopped = {
        str(key)
        for key, value in projects.items()
        if isinstance(value, dict)
        and (value.get("before") == "stopped" or value.get("lifecycle") == "stopped")
    }
    return digest, stopped


def _session_meta(path: Path, cwd: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if value.get("type") != "session_meta":
                    continue
                payload = value.get("payload")
                if not isinstance(payload, dict) or payload.get("cwd") != str(cwd):
                    return None
                session_id = payload.get("id")
                return session_id if isinstance(session_id, str) else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


class _TmuxCandidate:
    def __init__(self, request: TelegramE2ERequest, root: Path) -> None:
        self.request = request
        self.root = root
        self.binary = safe_executable(
            Path(request.candidate_binary), code="CANDIDATE_BINARY_UNSAFE"
        )
        if file_sha256(self.binary, code="CANDIDATE_BINARY_UNSAFE") != request.candidate_sha256:
            raise OperationalError("CANDIDATE_DIGEST_MISMATCH")
        self.profile = assert_private_directory(
            Path(request.profile), create=True, code="DEDICATED_PROFILE_UNSAFE"
        )
        self.cwd = canonical_path(Path(request.cwd), code="CANARY_CWD_UNSAFE")
        reconcile_disposable_project_trust(self.profile, self.cwd)
        self.candidate_home = assert_private_directory(
            root / "candidate-home", create=True, code="CANARY_HOME_UNSAFE"
        )
        token = hashlib.sha256((str(root) + request.candidate_sha256).encode()).hexdigest()[:12]
        self.socket = f"cus-e2e-{token}"
        self.session_name = f"cus-e2e-{os.getpid()}-{token[:6]}"
        self.tmux = safe_executable(Path("/usr/bin/tmux"), code="TMUX_UNAVAILABLE")
        self.env = clean_environment(root / "tmux-home")
        self.adapter = CodexAdapter()
        self.rollout: Path | None = None
        self.session_id: str | None = None
        self.offset = 0
        self.pane_pid: int | None = None
        self.pane_start_command: str | None = None
        self._seen_identities: set[str] = set()
        self._seen_digests: set[str] = set()
        self._existing_rollouts: set[Path] = set()
        reasoning = f'model_reasoning_effort="{request.reasoning_effort}"'
        self.argv = [
            str(self.binary),
            "--no-alt-screen",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--model",
            request.model,
            "-c",
            reasoning,
            "--cd",
            str(self.cwd),
        ]
        if request.fixture_scenario is not None:
            self.argv.extend(["--fixture-scenario", request.fixture_scenario])

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return _run_tmux(
            [str(self.tmux), "-L", self.socket, "-f", "/dev/null", *args],
            cwd=self.cwd,
            env=self.env,
        )

    def start(self) -> None:
        sessions = self.profile / "sessions"
        self._existing_rollouts = (
            {path.resolve() for path in sessions.rglob("*.jsonl")} if sessions.exists() else set()
        )
        command = [
            str(self.tmux),
            "-L",
            self.socket,
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            self.session_name,
            "-x",
            "200",
            "-y",
            "50",
            "/usr/bin/env",
            f"HOME={self.candidate_home}",
            f"CODEX_HOME={self.profile}",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            *self.argv,
        ]
        result = _run_tmux(command, cwd=self.cwd, env=self.env)
        if result.returncode != 0:
            raise OperationalError("TELEGRAM_E2E_TMUX_START_FAILED")
        deadline = time.monotonic() + 60
        ready_since: float | None = None
        fixture = self.request.fixture_scenario is not None
        ready_marker = "›" if fixture else "› Ask Codex to do anything"
        stable_seconds = 0.0 if fixture else 10.0
        while time.monotonic() < deadline:
            pane = self._tmux("capture-pane", "-p", "-t", f"={self.session_name}:")
            pane_text = pane.stdout.decode("utf-8", errors="replace")
            if "Do you trust the contents of this directory?" in pane_text:
                raise OperationalError("TELEGRAM_E2E_TRUST_NOT_PREPARED")
            if pane.returncode == 0 and ready_marker in pane_text:
                ready_since = ready_since or time.monotonic()
                if time.monotonic() - ready_since >= stable_seconds:
                    break
            else:
                ready_since = None
            time.sleep(0.1)
        else:
            raise OperationalError("TELEGRAM_E2E_TMUX_READY_TIMEOUT")
        pane = self._tmux(
            "display-message",
            "-p",
            "-t",
            f"={self.session_name}:0.0",
            "#{pane_pid}|#{pane_start_command}",
        )
        try:
            pane_text = pane.stdout.decode("utf-8", errors="strict").strip()
            raw_pid, self.pane_start_command = pane_text.split("|", 1)
            self.pane_pid = int(raw_pid) if pane.returncode == 0 else None
        except (UnicodeDecodeError, ValueError):
            self.pane_pid = None
            self.pane_start_command = None
        atomic_write_model(
            self.root / "tmux-observation.json",
            {
                "schema_version": 1,
                "socket": self.socket,
                "session_name": self.session_name,
                "pane_pid": self.pane_pid,
                "pane_start_command": self.pane_start_command,
                "display_returncode": pane.returncode,
                "display_stdout": pane.stdout.decode("utf-8", errors="replace"),
                "display_stderr": pane.stderr.decode("utf-8", errors="replace"),
                "candidate": str(self.binary),
            },
        )
        # Codex versions differ on whether the rollout file appears at startup
        # or only after the first submitted turn.  Observe the eager form here,
        # but let turn() discover the lazy form after it submits the prompt.
        self._discover_rollout(timeout_seconds=0.5)

    def _discover_rollout(self, *, timeout_seconds: float) -> bool:
        sessions = self.profile / "sessions"
        deadline = time.monotonic() + timeout_seconds
        while True:
            candidates = (
                [
                    path.resolve()
                    for path in sessions.rglob("*.jsonl")
                    if path.resolve() not in self._existing_rollouts and not path.is_symlink()
                ]
                if sessions.exists()
                else []
            )
            matches = [(path, _session_meta(path, self.cwd)) for path in candidates]
            matches = [(path, session_id) for path, session_id in matches if session_id]
            if len(matches) == 1:
                self.rollout, self.session_id = matches[0]
                return True
            if len(matches) > 1:
                raise OperationalError("TELEGRAM_E2E_ROLLOUT_AMBIGUOUS")
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def close(self) -> None:
        self._tmux("kill-session", "-t", f"={self.session_name}")

    def _submit(self, prompt: str) -> None:
        prompt_path = self.root / "prompt.txt"
        atomic_write_bytes(prompt_path, prompt.encode("utf-8"), mode=0o600)
        buffer_name = "cus-e2e-prompt"
        loaded = self._tmux("load-buffer", "-b", buffer_name, str(prompt_path))
        if loaded.returncode != 0:
            raise OperationalError("TELEGRAM_E2E_PROMPT_REJECTED")
        pasted = self._tmux(
            "paste-buffer", "-p", "-d", "-b", buffer_name, "-t", f"={self.session_name}:"
        )
        entered = self._tmux("send-keys", "-t", f"={self.session_name}:", "Enter")
        if pasted.returncode != 0 or entered.returncode != 0:
            raise OperationalError("TELEGRAM_E2E_PROMPT_REJECTED")

    async def turn(self, prompt: str, on_event: Any) -> None:
        self._seen_identities.clear()
        self._seen_digests.clear()
        self._submit(prompt)
        if self.rollout is None and not self._discover_rollout(timeout_seconds=30):
            raise OperationalError("TELEGRAM_E2E_ROLLOUT_MISSING")
        if self.rollout is None:
            raise OperationalError("TELEGRAM_E2E_ROLLOUT_MISSING")
        final_seen = False
        completed = False
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            with self.rollout.open("r", encoding="utf-8") as stream:
                stream.seek(self.offset)
                lines = stream.readlines()
                self.offset = stream.tell()
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OperationalError("MALFORMED_ROLLOUT_RECORD") from exc
                failure = rollout_schema_failure(record)
                if failure is not None:
                    raise OperationalError(failure)
                backend_failure = candidate_backend_failure(record)
                if backend_failure is not None:
                    raise OperationalError(backend_failure)
                parsed = self.adapter.parse_tui_event(line)
                if parsed.session_id:
                    self.session_id = parsed.session_id
                output = parsed.assistant_output
                duplicate = False
                if output is not None:
                    identity = output.identity
                    digest = hashlib.sha256(
                        f"{output.phase}\0{output.text}".encode("utf-8")
                    ).hexdigest()
                    duplicate = (
                        identity is not None and identity in self._seen_identities
                    ) or digest in self._seen_digests
                    if identity is not None:
                        self._seen_identities.add(identity)
                    self._seen_digests.add(digest)
                if not duplicate:
                    for event in parsed.events:
                        result = on_event(event)
                        if asyncio.iscoroutine(result):
                            await result
                        if event.type == "result_message":
                            final_seen = True
                if is_task_complete(record):
                    completed = True
            if completed:
                if not final_seen:
                    raise OperationalError("COMPLETION_WITHOUT_FINAL")
                return
            await asyncio.sleep(0.05)
        raise OperationalError("TELEGRAM_E2E_TIMEOUT")

    def runtime_truth(self) -> tuple[str, str | None, str | None]:
        if self.rollout is None or self.session_id is None or self.pane_pid is None:
            return "unknown", None, None
        identity = RuntimeSelectionIdentity(
            project_key="qualification-canary",
            session_name=self.session_name,
            session_id=self.session_id,
            pane_pid=self.pane_pid,
        )
        value = _probe_codex(
            RuntimeSelectionProbeContext(
                identity=identity,
                agent="codex",
                project_path=str(self.cwd),
                pane_path=str(self.cwd),
                transcript_path=str(self.rollout),
            )
        )
        return value.status, value.model, value.reasoning_effort

    def process_truth(self) -> tuple[str, list[str], str]:
        if self.pane_pid is None:
            raise OperationalError("F4_CANDIDATE_PROCESS_MISSING")
        queue = [self.pane_pid]
        seen: set[int] = set()
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            try:
                raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                argv = [
                    item.decode("utf-8", errors="strict")
                    for item in raw_cmdline.split(b"\0")
                    if item
                ]
            except (OSError, UnicodeDecodeError):
                argv = []
            matched = False
            for item in argv:
                try:
                    if Path(item).resolve(strict=True) == self.binary:
                        matched = True
                        break
                except OSError:
                    continue
            if not matched:
                try:
                    matched = Path(f"/proc/{pid}/exe").resolve(strict=True) == self.binary
                except OSError:
                    matched = False
            if matched:
                return (
                    str(self.binary),
                    argv,
                    file_sha256(self.binary, code="F4_CANDIDATE_BINARY_UNSAFE"),
                )
            try:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
            except OSError:
                children = ""
            for raw in children.split():
                try:
                    queue.append(int(raw))
                except ValueError:
                    continue
        probe = self._tmux("has-session", "-t", f"={self.session_name}")
        if probe.returncode == 0 and self.pane_start_command is not None:
            try:
                start_argv = shlex.split(self.pane_start_command)
            except ValueError as exc:
                raise OperationalError("F4_CANDIDATE_PROCESS_MISSING") from exc
            if str(self.binary) in start_argv:
                return (
                    str(self.binary),
                    start_argv,
                    file_sha256(self.binary, code="F4_CANDIDATE_BINARY_UNSAFE"),
                )
        raise OperationalError("F4_CANDIDATE_PROCESS_MISSING")


class _TmuxFacade:
    def __init__(self, candidate: _TmuxCandidate) -> None:
        self.candidate = candidate

    def is_active(self, _channel: tuple[int, int]) -> bool:
        return True

    async def send_stream(self, _channel: tuple[int, int], prompt: str, on_event: Any) -> str:
        await self.candidate.turn(prompt, on_event)
        return ""

    def get_session_id(self, _channel: tuple[int, int]) -> str | None:
        return self.candidate.session_id

    def get_session_snapshot(
        self, _channel: tuple[int, int]
    ) -> tuple[str, str | None, str | None] | None:
        if self.candidate.session_id is None:
            return None
        return self.candidate.session_id, "codex", self.candidate.request.model

    def live_buffer_available(self) -> bool:
        return False


async def _queue_turn(
    queue: MessageQueue,
    channel: tuple[int, int],
    prompt: str,
    message_id: int,
    message: _Message,
    error: list[BaseException],
) -> None:
    queue.enqueue(channel, prompt, message_id, message)
    deadline = time.monotonic() + 200
    while queue.is_busy(channel) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    if queue.is_busy(channel):
        error.append(OperationalError("TELEGRAM_E2E_QUEUE_TIMEOUT"))


async def _execute_async(request: TelegramE2ERequest, root: Path) -> TelegramE2EResult:
    overlay = canonical_path(Path(request.overlay_root), code="OVERLAY_ROOT_UNSAFE")
    package_origin = Path(str(telegram_package_file)).resolve()
    rollout_origin = Path(str(codex_rollout.__file__)).resolve()
    if not package_origin.is_relative_to(overlay) or not rollout_origin.is_relative_to(overlay):
        raise OperationalError("TELEGRAM_IMPORT_ORIGIN_MISMATCH")
    state_path = canonical_path(Path(request.project_state_path), code="PROJECT_STATE_INVALID")
    state_before, stopped_before = _load_project_state(state_path)
    transport = _DisposableTransport()
    session_manager = _SessionManager()
    candidate = _TmuxCandidate(request, root)
    errors: list[BaseException] = []
    candidate.start()
    facade = _TmuxFacade(candidate)

    async def callback(
        channel: tuple[int, int],
        prompt: str,
        messages: list[_Message],
        target_session_id: str | None,
    ) -> None:
        try:
            await process_queue_item(
                channel,
                prompt,
                messages,  # type: ignore[arg-type]
                target_session_id,
                bot=_Bot(),  # type: ignore[arg-type]
                session_manager=session_manager,  # type: ignore[arg-type]
                tmux_manager=facade,  # type: ignore[arg-type]
            )
        except BaseException as exc:
            errors.append(exc)
            raise

    queue = MessageQueue(_Bot(), session_manager, callback)  # type: ignore[arg-type]
    channel = (71001, 91001)
    fixture = request.fixture_scenario is not None
    final_prompt = (
        f"Return only MARKER[{request.final_marker}] as the final answer."
        if fixture
        else f"Return only the exact token {request.final_marker} as the final answer."
    )
    resume_prompt = (
        f"Resume this exact session and return only MARKER[{request.resume_marker}]."
        if fixture
        else f"Resume this exact session and return only the exact token {request.resume_marker}."
    )
    try:
        await _queue_turn(
            queue,
            channel,
            final_prompt,
            1,
            _Message(transport, 1),
            errors,
        )
        first_session = candidate.session_id
        if not errors and request.fixture_scenario in {None, "normal", "lazy"}:
            await _queue_turn(
                queue,
                channel,
                resume_prompt,
                2,
                _Message(transport, 2),
                errors,
            )
        resumed_session = candidate.session_id
        runtime_status, observed_model, observed_effort = candidate.runtime_truth()
        observed_command, observed_argv, observed_digest = candidate.process_truth()
    finally:
        await queue.shutdown()
        candidate.close()
    state_after, stopped_after = _load_project_state(state_path)
    final_count = sum(item.text == request.final_marker for item in transport.items)
    resume_count = sum(item.text == request.resume_marker for item in transport.items)
    failure_codes: list[str] = []
    for error in errors:
        code = error.code if isinstance(error, OperationalError) else "TELEGRAM_E2E_RUNNER_FAILURE"
        if code not in failure_codes:
            failure_codes.append(code)
    if request.fixture_scenario == "missing" and "COMPLETION_WITHOUT_FINAL" not in failure_codes:
        failure_codes.append("MISSING_FINAL_NOT_REJECTED")
    if request.fixture_scenario == "unknown" and "UNKNOWN_ROLLOUT_SCHEMA" not in failure_codes:
        failure_codes.append("UNKNOWN_SCHEMA_NOT_REJECTED")
    normal = request.fixture_scenario in {None, "normal", "lazy"}
    if normal:
        if final_count != 1 or resume_count != 1:
            failure_codes.append("TELEGRAM_FINAL_DELIVERY_INVALID")
        if first_session is None or resumed_session != first_session:
            failure_codes.append("TELEGRAM_SESSION_RESUME_INVALID")
        if runtime_status != "current":
            failure_codes.append("F4_RUNTIME_SELECTION_UNKNOWN")
        if observed_model != request.model or observed_effort != request.reasoning_effort:
            failure_codes.append("F4_MODEL_EFFORT_MISMATCH")
    stopped_started = stopped_before != stopped_after or state_before != state_after
    if stopped_started:
        failure_codes.append("STOPPED_PROJECT_STARTED")
    return TelegramE2EResult(
        verdict="CHECK_FAILED" if failure_codes else "PASS",
        failure_codes=list(dict.fromkeys(failure_codes)),
        queue_accepted=not any(code == "TELEGRAM_E2E_QUEUE_TIMEOUT" for code in failure_codes),
        send_lifecycle_completed=normal and not errors and final_count == 1 and resume_count == 1,
        final_marker_deliveries=final_count,
        resume_marker_deliveries=resume_count,
        duplicate_final_suppressed=normal and final_count == 1,
        completion_without_final_rejected=(
            request.fixture_scenario != "missing" or "COMPLETION_WITHOUT_FINAL" in failure_codes
        ),
        unknown_schema_rejected=(
            request.fixture_scenario != "unknown" or "UNKNOWN_ROLLOUT_SCHEMA" in failure_codes
        ),
        session_id=first_session,
        resumed_session_id=resumed_session,
        tmux_socket=candidate.socket,
        tmux_session_name=candidate.session_name,
        pane_pid=candidate.pane_pid,
        candidate_command=observed_command,
        candidate_argv=observed_argv,
        candidate_sha256=observed_digest,
        observed_model=observed_model,
        observed_reasoning_effort=observed_effort,
        runtime_selection_status=runtime_status,
        stopped_projects_started=stopped_started,
        project_state_before_sha256=state_before,
        project_state_after_sha256=state_after,
        telegram_import_origin=str(package_origin),
        rollout_import_origin=str(rollout_origin),
    )


def execute(request: TelegramE2ERequest, root: Path) -> TelegramE2EResult:
    return asyncio.run(_execute_async(request, root))


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    try:
        request = read_private_model(
            request_path, TelegramE2ERequest, code="TELEGRAM_E2E_REQUEST_INVALID"
        )
        root = assert_private_directory(
            result_path.parent / "process-root", create=True, code="TELEGRAM_E2E_ROOT_UNSAFE"
        )
        atomic_write_model(result_path, execute(request, root))
        return 0
    except OperationalError as exc:
        atomic_write_model(
            result_path,
            {
                "schema_version": 1,
                "verdict": "CHECK_FAILED",
                "failure_codes": [exc.code],
                "transport": "IN_MEMORY_NON_NETWORK",
                "network_capable": False,
                "queue_accepted": False,
                "send_lifecycle_completed": False,
                "final_marker_deliveries": 0,
                "resume_marker_deliveries": 0,
                "duplicate_final_suppressed": False,
                "completion_without_final_rejected": False,
                "unknown_schema_rejected": False,
                "session_id": None,
                "resumed_session_id": None,
                "tmux_socket": "not-started",
                "tmux_session_name": "not-started",
                "pane_pid": None,
                "candidate_command": "unavailable",
                "candidate_argv": ["unavailable"] * 7,
                "candidate_sha256": "0" * 64,
                "observed_model": None,
                "observed_reasoning_effort": None,
                "runtime_selection_status": "unknown",
                "stopped_projects_started": False,
                "project_state_before_sha256": "0" * 64,
                "project_state_after_sha256": "0" * 64,
                "telegram_import_origin": str(Path(str(telegram_package_file)).resolve()),
                "rollout_import_origin": str(Path(str(codex_rollout.__file__)).resolve()),
            },
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
