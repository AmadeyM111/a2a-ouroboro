"""Ouroboros extension entry point for controlled A2A messaging."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .client import A2AClient, ClientError
from .crypto import LocalCrypto


SETTING_KEYS = (
    "A2A_AGENT_ID",
    "A2A_KEY_ID",
    "A2A_GATEWAY_URL",
    "A2A_GATEWAY_TOKEN",
    "A2A_X25519_PRIVATE_KEY_PATH",
    "A2A_ED25519_PRIVATE_KEY_PATH",
    "A2A_PUBLIC_REGISTRY_PATH",
    "A2A_RECIPIENT_KEY_IDS_JSON",
)

MAX_MESSAGE_CHARS = 16_000
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_IDENTIFIER_CHARS = 128


class ExtensionConfigurationError(RuntimeError):
    """The reviewed A2A extension settings are incomplete or invalid."""


def _json_result(**values: Any) -> str:
    return json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _required_string(settings: dict[str, Any], key: str) -> str:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExtensionConfigurationError(f"missing setting: {key}")
    return value.strip()


def _optional_identifier(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{field} is invalid")
    return value


def _required_identifier(value: Any, field: str) -> str:
    parsed = _optional_identifier(value, field)
    if parsed is None:
        raise ValueError(f"{field} is required")
    return parsed


def _content(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("content is required")
    if len(value) > MAX_MESSAGE_CHARS:
        raise ValueError("content is too large")
    return value


def _idempotency_key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_IDEMPOTENCY_KEY_CHARS:
        raise ValueError("idempotency_key is invalid")
    return value


def _recipient_mapping(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtensionConfigurationError(
            "A2A_RECIPIENT_KEY_IDS_JSON is invalid"
        ) from exc
    if (
        not isinstance(value, dict)
        or not value
        or any(
            not isinstance(agent_id, str)
            or not isinstance(key_id, str)
            or not agent_id
            or not key_id
            for agent_id, key_id in value.items()
        )
    ):
        raise ExtensionConfigurationError(
            "A2A_RECIPIENT_KEY_IDS_JSON is invalid"
        )
    return value


def _public_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return delivery metadata without exposing ciphertext or signatures."""
    fields = (
        "message_id",
        "conversation_id",
        "reply_to_message_id",
        "sender_id",
        "recipient_id",
        "message_type",
        "idempotency_key",
        "status",
        "created_at",
        "delivered_at",
        "acknowledged_at",
    )
    return {field: envelope.get(field) for field in fields}


def register(api: Any) -> None:
    """Register the pull-based A2A tool surface with PluginAPI 1.3."""

    client: A2AClient | None = None
    client_lock = threading.Lock()

    def get_client() -> A2AClient:
        nonlocal client
        with client_lock:
            if client is not None:
                return client

            settings = api.get_settings(SETTING_KEYS)
            agent_id = _required_string(settings, "A2A_AGENT_ID")
            key_id = _required_string(settings, "A2A_KEY_ID")
            crypto = LocalCrypto.from_files(
                agent_id=agent_id,
                key_id=key_id,
                x25519_private_path=_required_string(
                    settings, "A2A_X25519_PRIVATE_KEY_PATH"
                ),
                ed25519_private_path=_required_string(
                    settings, "A2A_ED25519_PRIVATE_KEY_PATH"
                ),
                public_registry_path=_required_string(
                    settings, "A2A_PUBLIC_REGISTRY_PATH"
                ),
            )
            client = A2AClient(
                base_url=_required_string(settings, "A2A_GATEWAY_URL"),
                bearer_token=_required_string(settings, "A2A_GATEWAY_TOKEN"),
                crypto=crypto,
                recipient_key_ids=_recipient_mapping(
                    _required_string(settings, "A2A_RECIPIENT_KEY_IDS_JSON")
                ),
            )
            return client

    def guarded(handler: Callable[..., dict[str, Any]]) -> Callable[..., str]:
        def run(**kwargs: Any) -> str:
            try:
                return _json_result(ok=True, **handler(**kwargs))
            except (ClientError, ExtensionConfigurationError, ValueError) as exc:
                return _json_result(ok=False, error=str(exc))
            except Exception:
                api.log("error", "A2A tool failed", tool=handler.__name__)
                return _json_result(ok=False, error="internal A2A extension error")

        return run

    @guarded
    def health() -> dict[str, Any]:
        gateway = get_client().health()
        return {
            "configured": True,
            "gateway": {
                "status": gateway["status"],
                "service": gateway["service"],
            },
        }

    @guarded
    def send_message(
        recipient_id: Any,
        content: Any,
        idempotency_key: Any = None,
        conversation_id: Any = None,
    ) -> dict[str, Any]:
        envelope = get_client().send_message(
            recipient_id=_required_identifier(recipient_id, "recipient_id"),
            content=_content(content),
            idempotency_key=_idempotency_key(idempotency_key),
            conversation_id=_optional_identifier(
                conversation_id, "conversation_id"
            ),
        )
        return {"message": _public_envelope(envelope)}

    @guarded
    def list_inbox(limit: Any = 20) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        messages = get_client().list_inbox(limit=limit, acknowledge=False)
        return {"messages": messages, "count": len(messages)}

    @guarded
    def reply(
        message_id: Any,
        conversation_id: Any,
        recipient_id: Any,
        content: Any,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        envelope = get_client().reply(
            message_id=_required_identifier(message_id, "message_id"),
            conversation_id=_required_identifier(
                conversation_id, "conversation_id"
            ),
            recipient_id=_required_identifier(recipient_id, "recipient_id"),
            content=_content(content),
            idempotency_key=_idempotency_key(idempotency_key),
        )
        return {"message": _public_envelope(envelope)}

    api.register_tool(
        "health",
        health,
        description="Check local A2A configuration and Gateway availability.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        timeout_sec=15,
    )
    api.register_tool(
        "send_message",
        send_message,
        description="Send an encrypted message to an allowed agent.",
        schema={
            "type": "object",
            "properties": {
                "recipient_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "content": {"type": "string", "minLength": 1, "maxLength": 16000},
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "conversation_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
            },
            "required": ["recipient_id", "content"],
            "additionalProperties": False,
        },
        timeout_sec=15,
    )
    api.register_tool(
        "list_inbox",
        list_inbox,
        description="List decrypted inbox messages without acknowledging them.",
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100}
            },
            "additionalProperties": False,
        },
        timeout_sec=15,
    )
    api.register_tool(
        "reply",
        reply,
        description="Send an encrypted reply to an existing A2A message.",
        schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "conversation_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "recipient_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "content": {"type": "string", "minLength": 1, "maxLength": 16000},
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
            },
            "required": [
                "message_id",
                "conversation_id",
                "recipient_id",
                "content",
            ],
            "additionalProperties": False,
        },
        timeout_sec=15,
    )
