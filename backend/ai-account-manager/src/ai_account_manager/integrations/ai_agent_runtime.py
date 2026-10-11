from __future__ import annotations

import asyncio
import json
import os
import stat
import struct
import uuid
from pathlib import Path
from typing import Any

from ai_account_manager.schemas.runtime_control import (
    ProjectRelocationRecord,
    RuntimeBindingRecord,
    canonical_digest,
)

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
MAX_FRAME_SIZE = 1024 * 1024
_HEADER = struct.Struct("!I")


class RuntimeConnectorError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class RuntimeConnector:
    def __init__(self, socket_path: Path, *, timeout: float = 10.0) -> None:
        if not socket_path.is_absolute() or ".." in socket_path.parts:
            raise ValueError("Runtime socket path must be absolute and traversal-free")
        if timeout <= 0 or timeout > 120:
            raise ValueError("Runtime connector timeout must be in (0, 120]")
        self.socket_path = socket_path
        self.timeout = timeout

    async def apply_binding(
        self,
        binding: RuntimeBindingRecord,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "apply_binding",
            request_id=request_id,
            binding=binding.runtime_binding_payload(),
            desired=binding.desired.runtime_payload(),
        )

    async def apply_snapshot(
        self,
        *,
        agent_instance_id: str,
        generation_id: str,
        bindings: tuple[RuntimeBindingRecord, ...],
        project_relocations: tuple[ProjectRelocationRecord, ...] = (),
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not agent_instance_id or not generation_id or not bindings:
            raise ValueError("Runtime snapshot requires namespace, generation, and bindings")
        active: list[dict[str, object]] = []
        unmanaged: list[dict[str, object]] = []
        retired: list[str] = []
        seen: set[str] = set()
        for binding in sorted(bindings, key=lambda record: record.binding_id):
            if binding.binding_id in seen:
                raise ValueError("Runtime snapshot contains duplicate binding ids")
            seen.add(binding.binding_id)
            if binding.agent_instance_id != agent_instance_id:
                raise ValueError("Runtime snapshot AgentInstance mismatch")
            if binding.lifecycle_status == "UNCONFIGURED":
                raise ValueError("Unconfigured bindings cannot be sent to Runtime")
            if binding.lifecycle_status == "ARCHIVED":
                retired.append(binding.binding_id)
                continue
            if binding.desired.generation_id != generation_id:
                raise ValueError("Runtime snapshot desired generation mismatch")
            item: dict[str, object] = {
                "binding": binding.runtime_binding_payload(),
                "desired": binding.desired.runtime_payload(),
            }
            if binding.lifecycle_status == "UNMANAGED":
                unmanaged.append(item)
            else:
                active.append(item)
        material: dict[str, object] = {
            "agent_instance_id": agent_instance_id,
            "generation_id": generation_id,
            "bindings": active,
            "unmanaged_bindings": unmanaged,
            "retired_binding_ids": retired,
        }
        if project_relocations:
            relocation_payloads: list[dict[str, object]] = []
            for relocation in sorted(
                project_relocations,
                key=lambda record: record.relocation_id,
            ):
                relocation_binding_ids = tuple(
                    binding.binding_id
                    for binding in bindings
                    if binding.agent_instance_id == agent_instance_id
                    and binding.project_id == relocation.project_id
                    and binding.lifecycle_status != "ARCHIVED"
                )
                relocation_payloads.append(
                    relocation.runtime_payload(
                        agent_instance_id=agent_instance_id,
                        binding_ids=relocation_binding_ids,
                    )
                )
            material["project_relocations"] = relocation_payloads
        return await self._request(
            "apply_snapshot",
            request_id=request_id,
            **material,
            snapshot_digest=canonical_digest(material),
        )

    async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        response = await self._request("outbox_pull", limit=limit)
        events = response.get("events")
        if not isinstance(events, list):
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_RESPONSE",
                "Runtime outbox response is malformed",
                retryable=False,
            )
        return tuple(event for event in events if isinstance(event, dict))

    async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
        await self._request("outbox_ack", event_id=event_id, ack_state=ack_state)

    async def status(self) -> dict[str, Any]:
        return await self._request("status")

    async def control_plane_heartbeat(self, agent_instance_id: str) -> dict[str, Any]:
        if not agent_instance_id:
            raise ValueError("Runtime control-plane heartbeat requires AgentInstance identity")
        return await self._request(
            "control_plane_heartbeat",
            agent_instance_id=agent_instance_id,
        )

    async def _request(
        self,
        action: str,
        *,
        request_id: str | None = None,
        **payload: object,
    ) -> dict[str, Any]:
        request = {
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
            "action": action,
            "request_id": request_id or str(uuid.uuid4()),
            **payload,
        }
        try:
            _validate_runtime_socket(self.socket_path)
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                try:
                    await _write_frame(writer, request)
                    response = await _read_frame(reader)
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (TimeoutError, OSError, EOFError, asyncio.IncompleteReadError) as exc:
            raise RuntimeConnectorError(
                "RUNTIME_UNAVAILABLE", "Runtime is unavailable", retryable=True
            ) from exc
        if response.get("protocol_major") != PROTOCOL_MAJOR:
            raise RuntimeConnectorError(
                "PROTOCOL_MISMATCH", "Runtime protocol major mismatch", retryable=False
            )
        if response.get("request_id") != request["request_id"]:
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_RESPONSE", "Runtime request id mismatch", retryable=False
            )
        if response.get("ok") is not True:
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else "RUNTIME_REJECTED"
            detail = error.get("detail") if isinstance(error, dict) else "Runtime rejected request"
            raise RuntimeConnectorError(str(code), str(detail), retryable=False)
        return response


def _validate_runtime_socket(path: Path) -> None:
    """Authenticate the local Runtime endpoint before opening a connection."""

    parent = path.parent
    parent_metadata = parent.lstat()
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeConnectorError(
            "UNSAFE_RUNTIME_SOCKET",
            "Runtime socket directory ownership/type/mode is unsafe",
            retryable=False,
        )
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeConnectorError(
            "UNSAFE_RUNTIME_SOCKET",
            "Runtime socket ownership/type/mode is unsafe",
            retryable=False,
        )


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(_HEADER.size)
    (size,) = _HEADER.unpack(header)
    if size == 0 or size > MAX_FRAME_SIZE:
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_RESPONSE", "Runtime frame size is invalid", retryable=False
        )
    decoded = json.loads(await reader.readexactly(size))
    if not isinstance(decoded, dict):
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_RESPONSE", "Runtime response must be an object", retryable=False
        )
    return decoded


async def _write_frame(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) == 0 or len(encoded) > MAX_FRAME_SIZE:
        raise ValueError("Runtime request frame size is invalid")
    writer.write(_HEADER.pack(len(encoded)) + encoded)
    await writer.drain()
