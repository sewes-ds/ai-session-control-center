from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class BotControlError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def request_bot_control(
    socket_path: Path, payload: dict[str, Any], *, timeout_seconds: float = 50.0
) -> dict[str, Any]:
    if not socket_path.exists():
        raise BotControlError(
            "control_socket_unavailable",
            "Интеграция управления тмукс-ботом не запущена.",
        )
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(encoded) > 65536:
        raise BotControlError("control_request_too_large", "Запрос управления слишком большой.")

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            client.sendall(encoded)
            response = bytearray()
            while b"\n" not in response:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 65536:
                    raise BotControlError(
                        "control_response_too_large",
                        "Ответ интеграции управления слишком большой.",
                    )
    except BotControlError:
        raise
    except (OSError, TimeoutError) as exc:
        raise BotControlError(
            "control_socket_failed",
            "Не удалось выполнить команду в тмукс-боте.",
        ) from exc

    try:
        parsed = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BotControlError(
            "control_response_invalid",
            "Тмукс-бот вернул некорректный ответ.",
        ) from exc
    if not isinstance(parsed, dict):
        raise BotControlError("control_response_invalid", "Тмукс-бот вернул некорректный ответ.")
    if not parsed.get("ok"):
        raise BotControlError(
            str(parsed.get("code") or "control_rejected"),
            str(parsed.get("message") or "Тмукс-бот отклонил переключение."),
        )
    return parsed
