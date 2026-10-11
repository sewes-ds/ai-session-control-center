from __future__ import annotations

import base64
import hashlib
import hmac


def verify_webhook_secret(supplied: str, expected: str) -> bool:
    return bool(supplied and expected) and hmac.compare_digest(supplied, expected)


def deterministic_onboarding_token(
    *,
    token_key: str,
    update_id: int,
    chat_id: int,
    thread_id: int,
) -> str:
    if len(token_key.encode()) < 32:
        raise ValueError("CUS onboarding token key must contain at least 32 bytes")
    material = f"v1\0{update_id}\0{chat_id}\0{thread_id}".encode()
    digest = hmac.new(token_key.encode(), material, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
