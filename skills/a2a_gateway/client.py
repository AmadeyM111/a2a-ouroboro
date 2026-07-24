"""Authenticated HTTP client for the encrypted Bank A2A Gateway."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .crypto import CryptoError, LocalCrypto

MAX_RESPONSE_BYTES = 256_000
MAX_INBOX_LIMIT = 100
DEFAULT_TIMEOUT_SECONDS = 10.0
RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})


class ClientError(RuntimeError):
    """Stable, non-secret A2A client failure."""


class ClientConfigurationError(ClientError):
    """Trusted client configuration is invalid."""


class GatewayError(ClientError):
    """The Gateway rejected a request or returned an invalid response."""


class GatewayUnavailable(ClientError):
    """The Gateway could not be reached within the configured policy."""


@dataclass(frozen=True)
class GatewayResponse:
    status: int
    body: Any


class A2AClient:
    """Encrypt locally and exchange opaque envelopes with the Gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        crypto: LocalCrypto,
        recipient_key_ids: Mapping[str, str],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = 2,
        opener: Any | None = None,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ClientConfigurationError("A2A Gateway URL is invalid")
        if parsed.query or parsed.fragment:
            raise ClientConfigurationError(
                "A2A Gateway URL must not contain query or fragment"
            )
        if not isinstance(bearer_token, str) or not bearer_token.strip():
            raise ClientConfigurationError("A2A bearer token is missing")
        if not 0 < timeout_seconds <= 30:
            raise ClientConfigurationError("A2A timeout is out of range")
        if not 1 <= max_attempts <= 3:
            raise ClientConfigurationError("A2A max_attempts is out of range")
        keys = dict(recipient_key_ids)
        if crypto.agent_id in keys:
            keys.pop(crypto.agent_id)
        if not keys or any(
            not isinstance(agent_id, str)
            or not isinstance(key_id, str)
            or not agent_id
            or not key_id
            for agent_id, key_id in keys.items()
        ):
            raise ClientConfigurationError("recipient key mapping is invalid")

        self.base_url = base_url.rstrip("/")
        self._token = bearer_token.strip()
        self.crypto = crypto
        self._recipient_key_ids = keys
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = max_attempts
        self._opener = opener or urllib.request.build_opener()

    def health(self) -> dict[str, Any]:
        response = self._request("GET", "/health", authenticated=False)
        if (
            not isinstance(response.body, dict)
            or response.body.get("status") != "ok"
            or response.body.get("service") != "a2a-gateway"
        ):
            raise GatewayError("A2A Gateway health response is invalid")
        return response.body

    def send_message(
        self,
        *,
        recipient_id: str,
        content: str,
        idempotency_key: str | None = None,
        message_type: str = "request",
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        envelope = self.crypto.encrypt_message(
            recipient_id=recipient_id,
            recipient_key_id=self._recipient_key_id(recipient_id),
            content=content,
            idempotency_key=key,
            message_type=message_type,
            conversation_id=conversation_id,
        )
        response = self._request("POST", "/v1/messages", payload=envelope)
        return self._validate_envelope(response.body)

    def list_inbox(
        self, *, limit: int = 20, acknowledge: bool = True
    ) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ClientError("inbox limit must be an integer")
        if not 1 <= limit <= MAX_INBOX_LIMIT:
            raise ClientError("inbox limit is out of range")
        response = self._request("GET", f"/v1/inbox?limit={limit}")
        if not isinstance(response.body, list) or len(response.body) > limit:
            raise GatewayError("A2A Gateway inbox response is invalid")

        decrypted: list[dict[str, Any]] = []
        for raw in response.body:
            envelope = self._validate_envelope(raw)
            try:
                message = self.crypto.decrypt_message(envelope)
            except CryptoError as exc:
                raise GatewayError("inbox message failed cryptographic validation") from exc
            if acknowledge:
                ack = self.acknowledge(message["message_id"])
                message["acknowledged_at"] = ack.get("acknowledged_at")
            decrypted.append(message)
        return decrypted

    def reply(
        self,
        *,
        message_id: str,
        conversation_id: str,
        recipient_id: str,
        content: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        envelope = self.crypto.encrypt_message(
            recipient_id=recipient_id,
            recipient_key_id=self._recipient_key_id(recipient_id),
            content=content,
            idempotency_key=key,
            message_type="response",
            conversation_id=conversation_id,
            reply_to_message_id=message_id,
        )
        response = self._request("POST", "/v1/messages", payload=envelope)
        return self._validate_envelope(response.body)

    def acknowledge(self, message_id: str) -> dict[str, Any]:
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
            raise ClientError("message_id is invalid")
        quoted = urllib.parse.quote(message_id, safe="")
        response = self._request(
            "POST", f"/v1/messages/{quoted}/ack", payload=None
        )
        envelope = self._validate_envelope(response.body)
        if envelope["message_id"] != message_id:
            raise GatewayError("A2A Gateway ACK response does not match request")
        return envelope

    def _recipient_key_id(self, recipient_id: str) -> str:
        try:
            return self._recipient_key_ids[recipient_id]
        except (KeyError, TypeError) as exc:
            raise ClientError("recipient is not in the local allowlist") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> GatewayResponse:
        body_bytes = None
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        if payload is not None:
            body_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"

        # Build bytes once and reuse them verbatim after an ambiguous transport
        # failure. Re-encrypting would violate Gateway idempotency semantics.
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body_bytes,
            headers=headers,
            method=method,
        )
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with self._opener.open(
                    request, timeout=self.timeout_seconds
                ) as raw:
                    response_body = self._read_json(raw)
                    return GatewayResponse(status=raw.status, body=response_body)
            except urllib.error.HTTPError as exc:
                detail = self._read_error_detail(exc)
                if (
                    exc.code in RETRYABLE_STATUS_CODES
                    and attempt + 1 < self.max_attempts
                ):
                    last_error = exc
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise GatewayError(
                    f"A2A Gateway rejected request ({exc.code}): {detail}"
                ) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                break
        raise GatewayUnavailable("A2A Gateway is unavailable") from last_error

    @staticmethod
    def _read_json(raw: Any) -> Any:
        length_header = raw.headers.get("Content-Length")
        if length_header:
            try:
                if int(length_header) > MAX_RESPONSE_BYTES:
                    raise GatewayError("A2A Gateway response is too large")
            except ValueError as exc:
                raise GatewayError(
                    "A2A Gateway response length is invalid"
                ) from exc
        data = raw.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise GatewayError("A2A Gateway response is too large")
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("A2A Gateway returned invalid JSON") from exc

    @staticmethod
    def _read_error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read(4097)
            if len(raw) > 4096:
                return "error response too large"
            value = json.loads(raw.decode("utf-8"))
            detail = value.get("detail") if isinstance(value, dict) else None
            if isinstance(detail, str) and 0 < len(detail) <= 256:
                return detail
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        return "request rejected"

    @staticmethod
    def _validate_envelope(value: Any) -> dict[str, Any]:
        required = {
            "message_id",
            "conversation_id",
            "reply_to_message_id",
            "sender_id",
            "recipient_id",
            "message_type",
            "idempotency_key",
            "encryption_version",
            "algorithm",
            "sender_key_id",
            "recipient_key_id",
            "ephemeral_public_key",
            "nonce",
            "ciphertext",
            "signature",
            "ciphertext_sha256",
            "status",
            "created_at",
            "delivered_at",
            "acknowledged_at",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise GatewayError("A2A Gateway message schema is invalid")
        return value
