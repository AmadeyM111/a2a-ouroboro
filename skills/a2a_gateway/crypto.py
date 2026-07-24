"""Local end-to-end cryptography for the Bank A2A extension."""

from __future__ import annotations

import base64
import binascii
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ALGORITHM = "X25519-HKDF-SHA256+CHACHA20-POLY1305+ED25519"
ENCRYPTION_VERSION = 1
HKDF_INFO = b"bank-a2a/e2e/v1"
MAX_CONTENT_UTF8_BYTES = 32_000
MAX_CIPHERTEXT_BYTES = 48_000
ALLOWED_MESSAGE_TYPES = frozenset({"request", "response", "notification"})


class CryptoError(ValueError):
    """Base class for stable, non-secret cryptographic failures."""


class ConfigurationError(CryptoError):
    """Trusted key material or identity configuration is invalid."""


class EnvelopeError(CryptoError):
    """An encrypted envelope is malformed or fails verification."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(name: str, value: Any, *, expected: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise EnvelopeError(f"{name} must be a base64 string")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EnvelopeError(f"{name} is not valid base64") from exc
    if expected is not None and len(decoded) != expected:
        raise EnvelopeError(f"{name} has an invalid length")
    return decoded


def _raw_public_key(key: X25519PublicKey | Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _derive_message_key(shared_secret: bytes, nonce: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=nonce, info=HKDF_INFO
    ).derive(shared_secret)


def _protected_header(
    *,
    sender_id: str,
    recipient_id: str,
    message_type: str,
    conversation_id: str | None,
    reply_to_message_id: str | None,
    idempotency_key: str,
    sender_key_id: str,
    recipient_key_id: str,
) -> dict[str, Any]:
    return {
        "algorithm": ALGORITHM,
        "conversation_id": conversation_id,
        "encryption_version": ENCRYPTION_VERSION,
        "idempotency_key": idempotency_key,
        "message_type": message_type,
        "recipient_id": recipient_id,
        "recipient_key_id": recipient_key_id,
        "reply_to_message_id": reply_to_message_id,
        "sender_id": sender_id,
        "sender_key_id": sender_key_id,
    }


def _identifier(name: str, value: Any, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= 128:
        raise EnvelopeError(f"{name} has an invalid length")
    return value


@dataclass(frozen=True)
class PublicKeyRecord:
    x25519: X25519PublicKey
    ed25519: Ed25519PublicKey


class PublicKeyRegistry:
    """Reviewed mapping of agent and key IDs to raw public keys."""

    def __init__(self, records: Mapping[tuple[str, str], PublicKeyRecord]):
        self._records = dict(records)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "PublicKeyRegistry":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError("public key registry cannot be loaded") from exc
        if not isinstance(raw, dict) or set(raw) != {"version", "agents"}:
            raise ConfigurationError("public key registry schema is invalid")
        if raw["version"] != 1 or not isinstance(raw["agents"], dict):
            raise ConfigurationError("public key registry version is unsupported")
        records: dict[tuple[str, str], PublicKeyRecord] = {}
        for agent_id, keys in raw["agents"].items():
            if not isinstance(agent_id, str) or not isinstance(keys, dict):
                raise ConfigurationError("public key registry entry is invalid")
            for key_id, value in keys.items():
                if (
                    not isinstance(key_id, str)
                    or not isinstance(value, dict)
                    or set(value) != {"x25519_public", "ed25519_public"}
                ):
                    raise ConfigurationError("public key registry entry is invalid")
                try:
                    x_public = X25519PublicKey.from_public_bytes(
                        _b64decode("x25519_public", value["x25519_public"], expected=32)
                    )
                    e_public = Ed25519PublicKey.from_public_bytes(
                        _b64decode("ed25519_public", value["ed25519_public"], expected=32)
                    )
                except EnvelopeError as exc:
                    raise ConfigurationError(str(exc)) from exc
                records[(agent_id, key_id)] = PublicKeyRecord(x_public, e_public)
        return cls(records)

    def get(self, agent_id: str, key_id: str) -> PublicKeyRecord:
        try:
            return self._records[(agent_id, key_id)]
        except KeyError as exc:
            raise EnvelopeError("agent key is not trusted") from exc


class LocalCrypto:
    """Encrypt/sign outbound and verify/decrypt inbound messages locally."""

    def __init__(
        self,
        *,
        agent_id: str,
        key_id: str,
        x25519_private_key: X25519PrivateKey,
        ed25519_private_key: Ed25519PrivateKey,
        registry: PublicKeyRegistry,
    ):
        self.agent_id = _identifier("agent_id", agent_id)
        self.key_id = _identifier("key_id", key_id)
        self._x25519_private = x25519_private_key
        self._ed25519_private = ed25519_private_key
        self._registry = registry
        local = registry.get(agent_id, key_id)
        if _raw_public_key(x25519_private_key.public_key()) != _raw_public_key(
            local.x25519
        ):
            raise ConfigurationError("local X25519 private key does not match registry")
        if _raw_public_key(ed25519_private_key.public_key()) != _raw_public_key(
            local.ed25519
        ):
            raise ConfigurationError("local Ed25519 private key does not match registry")

    @classmethod
    def from_files(
        cls,
        *,
        agent_id: str,
        key_id: str,
        x25519_private_path: str | os.PathLike[str],
        ed25519_private_path: str | os.PathLike[str],
        public_registry_path: str | os.PathLike[str],
    ) -> "LocalCrypto":
        try:
            x_private = serialization.load_pem_private_key(
                Path(x25519_private_path).read_bytes(), password=None
            )
            e_private = serialization.load_pem_private_key(
                Path(ed25519_private_path).read_bytes(), password=None
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigurationError("local private keys cannot be loaded") from exc
        if not isinstance(x_private, X25519PrivateKey):
            raise ConfigurationError("X25519 private key has the wrong type")
        if not isinstance(e_private, Ed25519PrivateKey):
            raise ConfigurationError("Ed25519 private key has the wrong type")
        return cls(
            agent_id=agent_id,
            key_id=key_id,
            x25519_private_key=x_private,
            ed25519_private_key=e_private,
            registry=PublicKeyRegistry.from_file(public_registry_path),
        )

    def encrypt_message(
        self,
        *,
        recipient_id: str,
        recipient_key_id: str,
        content: str,
        idempotency_key: str,
        message_type: Literal["request", "response", "notification"] = "request",
        conversation_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        recipient_id = _identifier("recipient_id", recipient_id)
        recipient_key_id = _identifier("recipient_key_id", recipient_key_id)
        idempotency_key = _identifier(
            "idempotency_key", idempotency_key, minimum=16
        )
        if recipient_id == self.agent_id:
            raise EnvelopeError("sender and recipient must differ")
        if message_type not in ALLOWED_MESSAGE_TYPES:
            raise EnvelopeError("message_type is invalid")
        if reply_to_message_id and message_type != "response":
            raise EnvelopeError("a reply must use message_type=response")
        if not isinstance(content, str) or not content:
            raise EnvelopeError("content must not be empty")
        if len(content.encode("utf-8")) > MAX_CONTENT_UTF8_BYTES:
            raise EnvelopeError("content is too large")
        if conversation_id is None:
            conversation_id = str(uuid.uuid4())
        else:
            conversation_id = _identifier("conversation_id", conversation_id)
        if reply_to_message_id is not None:
            reply_to_message_id = _identifier(
                "reply_to_message_id", reply_to_message_id
            )

        recipient = self._registry.get(recipient_id, recipient_key_id)
        ephemeral_private = X25519PrivateKey.generate()
        ephemeral_public = _raw_public_key(ephemeral_private.public_key())
        nonce = os.urandom(12)
        header = _protected_header(
            sender_id=self.agent_id,
            recipient_id=recipient_id,
            message_type=message_type,
            conversation_id=conversation_id,
            reply_to_message_id=reply_to_message_id,
            idempotency_key=idempotency_key,
            sender_key_id=self.key_id,
            recipient_key_id=recipient_key_id,
        )
        plaintext = _canonical_json(
            {
                "content": content,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "protected": header,
            }
        )
        shared_secret = ephemeral_private.exchange(recipient.x25519)
        ciphertext = ChaCha20Poly1305(
            _derive_message_key(shared_secret, nonce)
        ).encrypt(nonce, plaintext, _canonical_json(header))
        signature_input = (
            b"bank-a2a/signature/v1\0"
            + _canonical_json(header)
            + ephemeral_public
            + nonce
            + ciphertext
        )
        signature = self._ed25519_private.sign(signature_input)
        return {
            "recipient_id": recipient_id,
            "message_type": message_type,
            "conversation_id": conversation_id,
            "reply_to_message_id": reply_to_message_id,
            "idempotency_key": idempotency_key,
            "encryption_version": ENCRYPTION_VERSION,
            "algorithm": ALGORITHM,
            "sender_key_id": self.key_id,
            "recipient_key_id": recipient_key_id,
            "ephemeral_public_key": _b64encode(ephemeral_public),
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
            "signature": _b64encode(signature),
        }

    def decrypt_message(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "message_id", "conversation_id", "reply_to_message_id",
            "sender_id", "recipient_id", "message_type", "idempotency_key",
            "encryption_version", "algorithm", "sender_key_id",
            "recipient_key_id", "ephemeral_public_key", "nonce",
            "ciphertext", "signature",
        }
        if required.difference(envelope):
            raise EnvelopeError("encrypted envelope is missing required fields")
        if envelope["recipient_id"] != self.agent_id:
            raise EnvelopeError("message belongs to another recipient")
        if envelope["recipient_key_id"] != self.key_id:
            raise EnvelopeError("message targets another local key")
        if (
            envelope["encryption_version"] != ENCRYPTION_VERSION
            or envelope["algorithm"] != ALGORITHM
        ):
            raise EnvelopeError("encryption algorithm is unsupported")

        sender_id = _identifier("sender_id", envelope["sender_id"])
        sender_key_id = _identifier("sender_key_id", envelope["sender_key_id"])
        sender = self._registry.get(sender_id, sender_key_id)
        ephemeral_raw = _b64decode(
            "ephemeral_public_key", envelope["ephemeral_public_key"], expected=32
        )
        nonce = _b64decode("nonce", envelope["nonce"], expected=12)
        ciphertext = _b64decode("ciphertext", envelope["ciphertext"])
        signature = _b64decode("signature", envelope["signature"], expected=64)
        if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
            raise EnvelopeError("ciphertext is too large")
        header = _protected_header(
            sender_id=sender_id,
            recipient_id=envelope["recipient_id"],
            message_type=envelope["message_type"],
            conversation_id=envelope["conversation_id"],
            reply_to_message_id=envelope["reply_to_message_id"],
            idempotency_key=envelope["idempotency_key"],
            sender_key_id=sender_key_id,
            recipient_key_id=envelope["recipient_key_id"],
        )
        signature_input = (
            b"bank-a2a/signature/v1\0"
            + _canonical_json(header)
            + ephemeral_raw
            + nonce
            + ciphertext
        )
        try:
            sender.ed25519.verify(signature, signature_input)
        except InvalidSignature as exc:
            raise EnvelopeError("sender signature verification failed") from exc
        try:
            shared_secret = self._x25519_private.exchange(
                X25519PublicKey.from_public_bytes(ephemeral_raw)
            )
            plaintext = ChaCha20Poly1305(
                _derive_message_key(shared_secret, nonce)
            ).decrypt(nonce, ciphertext, _canonical_json(header))
        except (ValueError, InvalidTag) as exc:
            raise EnvelopeError("message decryption failed") from exc
        try:
            inner = json.loads(plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnvelopeError("decrypted payload is invalid") from exc
        if (
            not isinstance(inner, dict)
            or set(inner) != {"content", "created_at", "protected"}
            or inner["protected"] != header
            or not isinstance(inner["content"], str)
            or not isinstance(inner["created_at"], str)
        ):
            raise EnvelopeError("decrypted payload does not match routing metadata")
        return {
            "message_id": envelope["message_id"],
            "conversation_id": envelope["conversation_id"],
            "reply_to_message_id": envelope["reply_to_message_id"],
            "sender_id": sender_id,
            "recipient_id": self.agent_id,
            "message_type": envelope["message_type"],
            "content": inner["content"],
            "created_at": inner["created_at"],
        }


def generate_agent_key_material(
    *, agent_id: str, key_id: str, output_dir: str | os.PathLike[str]
) -> dict[str, str]:
    """Create one agent's keys without overwriting existing material."""
    _identifier("agent_id", agent_id)
    _identifier("key_id", key_id)
    destination = Path(output_dir)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = {
        "x_private": destination / "x25519-private.pem",
        "e_private": destination / "ed25519-private.pem",
        "x_public": destination / "x25519-public.pem",
        "e_public": destination / "ed25519-public.pem",
    }
    if any(path.exists() for path in paths.values()):
        raise ConfigurationError("refusing to overwrite existing key material")
    x_private = X25519PrivateKey.generate()
    e_private = Ed25519PrivateKey.generate()
    private_args = (
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_args = (
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    paths["x_private"].write_bytes(x_private.private_bytes(*private_args))
    paths["e_private"].write_bytes(e_private.private_bytes(*private_args))
    paths["x_public"].write_bytes(x_private.public_key().public_bytes(*public_args))
    paths["e_public"].write_bytes(e_private.public_key().public_bytes(*public_args))
    for path in (paths["x_private"], paths["e_private"]):
        os.chmod(path, 0o400)
    for path in (paths["x_public"], paths["e_public"]):
        os.chmod(path, 0o444)
    return {
        "agent_id": agent_id,
        "key_id": key_id,
        "x25519_public": _b64encode(_raw_public_key(x_private.public_key())),
        "ed25519_public": _b64encode(_raw_public_key(e_private.public_key())),
    }
