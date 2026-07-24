import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

DATABASE_PATH = os.getenv("A2A_DATABASE_PATH", "/data/gateway.db")
ALLOWED_AGENTS = frozenset(f"agent{number}" for number in range(1, 6))
MAX_B64_LENGTH = 20_000

app = FastAPI(title="Bank A2A Gateway", version="0.2.0")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialise_database() -> None:
    with database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages_v2 (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                reply_to_message_id TEXT,
                sender_id TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                encryption_version INTEGER NOT NULL,
                algorithm TEXT NOT NULL,
                sender_key_id TEXT NOT NULL,
                recipient_key_id TEXT NOT NULL,
                ephemeral_public_key TEXT NOT NULL,
                nonce TEXT NOT NULL,
                ciphertext TEXT NOT NULL,
                signature TEXT NOT NULL,
                ciphertext_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                acknowledged_at TEXT,
                UNIQUE(sender_id, idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_v2_inbox
            ON messages_v2(recipient_id, status, created_at);

            CREATE TABLE IF NOT EXISTS dpa_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_id TEXT,
                outcome TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                message_id TEXT,
                conversation_id TEXT,
                correlation_id TEXT NOT NULL,
                ciphertext_sha256 TEXT
            );
            """
        )


@app.on_event("startup")
def startup() -> None:
    initialise_database()


def configured_tokens() -> dict[str, str]:
    result: dict[str, str] = {}
    for number in range(1, 6):
        token = os.getenv(f"AGENT{number}_A2A_TOKEN")
        if token:
            result[token] = f"agent{number}"
    return result


def authenticated_agent(
    authorization: str | None = Header(default=None),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        record_auth_rejection("missing_bearer")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    for token, agent_id in configured_tokens().items():
        if secrets.compare_digest(supplied, token):
            return agent_id
    record_auth_rejection("invalid_token")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid A2A token",
    )


def record_auth_rejection(reason_code: str) -> None:
    try:
        with database() as connection:
            add_dpa_event(
                connection,
                actor_id="unauthenticated",
                action="authentication",
                target_id="gateway",
                outcome="rejected",
                reason_code=reason_code,
                correlation_id=str(uuid.uuid4()),
            )
    except sqlite3.Error:
        # Authentication must still fail closed if audit storage is unavailable.
        pass


def _valid_auth_actor(request: Request) -> str | None:
    """Resolve only already-valid bearer credentials for validation auditing."""
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    supplied = authorization.removeprefix("Bearer ").strip()
    for token, agent_id in configured_tokens().items():
        if secrets.compare_digest(supplied, token):
            return agent_id
    return None


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Persist safe DPA metadata for malformed authenticated envelopes.

    Invalid/missing credentials are already audited by ``authenticated_agent``;
    skip them here to avoid two rejection records for one request.
    """
    actor_id = _valid_auth_actor(request)
    if actor_id:
        try:
            with database() as connection:
                add_dpa_event(
                    connection,
                    actor_id=actor_id,
                    action="request_validation",
                    target_id=request.url.path,
                    outcome="rejected",
                    reason_code="malformed_envelope",
                    correlation_id=str(uuid.uuid4()),
                )
        except sqlite3.Error:
            # The API rejection remains fail-closed even when audit storage is
            # temporarily unavailable; the error is intentionally not exposed.
            pass
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EncryptedMessageCreate(StrictModel):
    recipient_id: str
    message_type: Literal["request", "response", "notification"] = "request"
    conversation_id: str | None = Field(default=None, max_length=128)
    reply_to_message_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=16, max_length=128)
    encryption_version: Literal[1] = 1
    algorithm: Literal[
        "X25519-HKDF-SHA256+CHACHA20-POLY1305+ED25519"
    ]
    sender_key_id: str = Field(min_length=1, max_length=128)
    recipient_key_id: str = Field(min_length=1, max_length=128)
    ephemeral_public_key: str = Field(min_length=40, max_length=128)
    nonce: str = Field(min_length=16, max_length=64)
    ciphertext: str = Field(min_length=16, max_length=MAX_B64_LENGTH)
    signature: str = Field(min_length=80, max_length=128)

    @field_validator(
        "ephemeral_public_key", "nonce", "ciphertext", "signature"
    )
    @classmethod
    def validate_base64(cls, value: str) -> str:
        import base64
        import binascii

        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64") from exc
        return value


class EncryptedMessage(StrictModel):
    message_id: str
    conversation_id: str
    reply_to_message_id: str | None
    sender_id: str
    recipient_id: str
    message_type: str
    idempotency_key: str
    encryption_version: int
    algorithm: str
    sender_key_id: str
    recipient_key_id: str
    ephemeral_public_key: str
    nonce: str
    ciphertext: str
    signature: str
    ciphertext_sha256: str
    status: str
    created_at: str
    delivered_at: str | None
    acknowledged_at: str | None


def add_dpa_event(
    connection: sqlite3.Connection,
    *,
    actor_id: str,
    action: str,
    target_id: str | None,
    outcome: str,
    reason_code: str,
    correlation_id: str,
    message_id: str | None = None,
    conversation_id: str | None = None,
    ciphertext_sha256: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO dpa_events (
            event_id, occurred_at, actor_id, action, target_id, outcome,
            reason_code, message_id, conversation_id, correlation_id,
            ciphertext_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            utc_now(),
            actor_id,
            action,
            target_id,
            outcome,
            reason_code,
            message_id,
            conversation_id,
            correlation_id,
            ciphertext_sha256,
        ),
    )


def request_fingerprint(payload: EncryptedMessageCreate) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def get_message(
    connection: sqlite3.Connection, message_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM messages_v2 WHERE message_id = ?", (message_id,)
    ).fetchone()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "a2a-gateway", "version": "0.2.0"}


@app.post(
    "/v1/messages",
    response_model=EncryptedMessage,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    payload: EncryptedMessageCreate,
    sender_id: str = Depends(authenticated_agent),
) -> EncryptedMessage:
    correlation_id = str(uuid.uuid4())
    fingerprint = request_fingerprint(payload)
    ciphertext_hash = hashlib.sha256(payload.ciphertext.encode()).hexdigest()

    with database() as connection:
        if payload.recipient_id not in ALLOWED_AGENTS:
            add_dpa_event(
                connection,
                actor_id=sender_id,
                action="message_send_requested",
                target_id=payload.recipient_id,
                outcome="rejected",
                reason_code="unknown_recipient",
                correlation_id=correlation_id,
                ciphertext_sha256=ciphertext_hash,
            )
            connection.commit()
            raise HTTPException(status_code=404, detail="Recipient not found")
        if payload.recipient_id == sender_id:
            add_dpa_event(
                connection,
                actor_id=sender_id,
                action="message_send_requested",
                target_id=payload.recipient_id,
                outcome="rejected",
                reason_code="self_send_forbidden",
                correlation_id=correlation_id,
                ciphertext_sha256=ciphertext_hash,
            )
            connection.commit()
            raise HTTPException(
                status_code=400, detail="Sender and recipient must differ"
            )

        existing = connection.execute(
            """
            SELECT * FROM messages_v2
            WHERE sender_id = ? AND idempotency_key = ?
            """,
            (sender_id, payload.idempotency_key),
        ).fetchone()
        if existing:
            if existing["request_fingerprint"] != fingerprint:
                add_dpa_event(
                    connection,
                    actor_id=sender_id,
                    action="message_send_requested",
                    target_id=payload.recipient_id,
                    outcome="rejected",
                    reason_code="idempotency_conflict",
                    correlation_id=correlation_id,
                    message_id=existing["message_id"],
                    conversation_id=existing["conversation_id"],
                    ciphertext_sha256=ciphertext_hash,
                )
                connection.commit()
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key reused with different payload",
                )
            add_dpa_event(
                connection,
                actor_id=sender_id,
                action="duplicate_suppressed",
                target_id=payload.recipient_id,
                outcome="accepted",
                reason_code="same_request",
                correlation_id=correlation_id,
                message_id=existing["message_id"],
                conversation_id=existing["conversation_id"],
                ciphertext_sha256=existing["ciphertext_sha256"],
            )
            return EncryptedMessage(**dict(existing))

        original = None
        if payload.reply_to_message_id:
            original = get_message(connection, payload.reply_to_message_id)
            if not original:
                add_dpa_event(
                    connection,
                    actor_id=sender_id,
                    action="message_send_requested",
                    target_id=payload.recipient_id,
                    outcome="rejected",
                    reason_code="original_not_found",
                    correlation_id=correlation_id,
                    ciphertext_sha256=ciphertext_hash,
                )
                connection.commit()
                raise HTTPException(
                    status_code=404, detail="Original message not found"
                )
            valid_reply = (
                original["sender_id"] == payload.recipient_id
                and original["recipient_id"] == sender_id
                and payload.message_type == "response"
            )
            if not valid_reply:
                add_dpa_event(
                    connection,
                    actor_id=sender_id,
                    action="message_send_requested",
                    target_id=payload.recipient_id,
                    outcome="rejected",
                    reason_code="invalid_reply_relationship",
                    correlation_id=correlation_id,
                    message_id=original["message_id"],
                    conversation_id=original["conversation_id"],
                    ciphertext_sha256=ciphertext_hash,
                )
                connection.commit()
                raise HTTPException(
                    status_code=403, detail="Invalid reply relationship"
                )

        message_id = str(uuid.uuid4())
        conversation_id = (
            original["conversation_id"]
            if original
            else payload.conversation_id or str(uuid.uuid4())
        )
        created_at = utc_now()
        connection.execute(
            """
            INSERT INTO messages_v2 (
                message_id, conversation_id, reply_to_message_id,
                sender_id, recipient_id, message_type, idempotency_key,
                request_fingerprint, encryption_version, algorithm,
                sender_key_id, recipient_key_id, ephemeral_public_key,
                nonce, ciphertext, signature, ciphertext_sha256,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'queued', ?)
            """,
            (
                message_id,
                conversation_id,
                payload.reply_to_message_id,
                sender_id,
                payload.recipient_id,
                payload.message_type,
                payload.idempotency_key,
                fingerprint,
                payload.encryption_version,
                payload.algorithm,
                payload.sender_key_id,
                payload.recipient_key_id,
                payload.ephemeral_public_key,
                payload.nonce,
                payload.ciphertext,
                payload.signature,
                ciphertext_hash,
                created_at,
            ),
        )
        add_dpa_event(
            connection,
            actor_id=sender_id,
            action=(
                "reply_accepted" if original else "message_accepted"
            ),
            target_id=payload.recipient_id,
            outcome="accepted",
            reason_code="validated",
            correlation_id=correlation_id,
            message_id=message_id,
            conversation_id=conversation_id,
            ciphertext_sha256=ciphertext_hash,
        )
        row = get_message(connection, message_id)
    return EncryptedMessage(**dict(row))


@app.get("/v1/inbox", response_model=list[EncryptedMessage])
def inbox(
    limit: int = Query(default=20, ge=1, le=100),
    recipient_id: str = Depends(authenticated_agent),
) -> list[EncryptedMessage]:
    correlation_id = str(uuid.uuid4())
    delivered_at = utc_now()
    with database() as connection:
        rows = connection.execute(
            """
            SELECT * FROM messages_v2
            WHERE recipient_id = ?
              AND status IN ('queued', 'delivered')
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (recipient_id, limit),
        ).fetchall()
        queued_ids = [
            row["message_id"] for row in rows if row["status"] == "queued"
        ]
        if queued_ids:
            placeholders = ",".join("?" for _ in queued_ids)
            connection.execute(
                f"""
                UPDATE messages_v2
                SET status = 'delivered', delivered_at = ?
                WHERE status = 'queued'
                  AND message_id IN ({placeholders})
                """,
                (delivered_at, *queued_ids),
            )
        selected_ids = [row["message_id"] for row in rows]
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM messages_v2
                WHERE message_id IN ({placeholders})
                ORDER BY created_at ASC
                """,
                selected_ids,
            ).fetchall()
        add_dpa_event(
            connection,
            actor_id=recipient_id,
            action="inbox_read",
            target_id=recipient_id,
            outcome="accepted",
            reason_code=f"returned_{len(rows)}",
            correlation_id=correlation_id,
        )
    return [EncryptedMessage(**dict(row)) for row in rows]


@app.post(
    "/v1/messages/{message_id}/ack",
    response_model=EncryptedMessage,
)
def acknowledge(
    message_id: str,
    recipient_id: str = Depends(authenticated_agent),
) -> EncryptedMessage:
    correlation_id = str(uuid.uuid4())
    with database() as connection:
        row = get_message(connection, message_id)
        if not row:
            add_dpa_event(
                connection,
                actor_id=recipient_id,
                action="message_acknowledged",
                target_id=message_id,
                outcome="rejected",
                reason_code="message_not_found",
                correlation_id=correlation_id,
                message_id=message_id,
            )
            connection.commit()
            raise HTTPException(status_code=404, detail="Message not found")
        if row["recipient_id"] != recipient_id:
            add_dpa_event(
                connection,
                actor_id=recipient_id,
                action="message_acknowledged",
                target_id=row["sender_id"],
                outcome="rejected",
                reason_code="ack_recipient_mismatch",
                correlation_id=correlation_id,
                message_id=row["message_id"],
                conversation_id=row["conversation_id"],
                ciphertext_sha256=row["ciphertext_sha256"],
            )
            connection.commit()
            raise HTTPException(status_code=403, detail="Access denied")
        if row["status"] != "acknowledged":
            connection.execute(
                """
                UPDATE messages_v2
                SET status = 'acknowledged', acknowledged_at = ?
                WHERE message_id = ?
                """,
                (utc_now(), message_id),
            )
        add_dpa_event(
            connection,
            actor_id=recipient_id,
            action="message_acknowledged",
            target_id=row["sender_id"],
            outcome="accepted",
            reason_code="acknowledged",
            correlation_id=correlation_id,
            message_id=message_id,
            conversation_id=row["conversation_id"],
            ciphertext_sha256=row["ciphertext_sha256"],
        )
        row = get_message(connection, message_id)
    return EncryptedMessage(**dict(row))
