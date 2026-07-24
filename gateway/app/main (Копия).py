import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

DATABASE_PATH = os.getenv("A2A_DATABASE_PATH", "/data/gateway.db")
ALLOWED_AGENTS = {"agent1", "agent2", "agent3", "agent4", "agent5"}

app = FastAPI(
    title="Bank A2A Gateway",
    version="0.1.0",
)

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        yield connection
        connection.commit()
    finally:
        connection.close()

def initialise_database() -> None:
    with database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                sender_id TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                acknowledged_at TEXT
            )
            """
        )

@app.on_event("startup")
def startup() -> None:
    initialise_database()

def tokens() -> dict[str, str]:
    result = {}

    for number in range(1, 6):
        agent_id = f"agent{number}"
        token = os.getenv(f"AGENT{number}_A2A_TOKEN")

        if token:
            result[token] = agent_id

    return result

def authenticated_agent(
    authorization: str | None = Header(default=None),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )

    token = authorization.removeprefix("Bearer ").strip()
    agent_id = tokens().get(token)

    if not agent_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid A2A token",
        )

    return agent_id

class MessageCreate(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    causation_id: str | None = None
    recipient_id: str
    message_type: Literal["request", "response", "notification"] = "request"
    content: str = Field(min_length=1, max_length=10_000)

class Message(BaseModel):
    message_id: str
    correlation_id: str
    causation_id: str | None
    sender_id: str
    recipient_id: str
    message_type: str
    content: str
    status: str
    created_at: str
    delivered_at: str | None
    acknowledged_at: str | None

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "a2a-gateway"}

@app.post(
    "/v1/messages",
    response_model=Message,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    payload: MessageCreate,
    sender_id: str = Depends(authenticated_agent),
) -> Message:
    if payload.recipient_id not in ALLOWED_AGENTS:
        raise HTTPException(status_code=404, detail="Recipient not found")

    if payload.recipient_id == sender_id:
        raise HTTPException(
            status_code=400,
            detail="Sender and recipient must differ",
        )

    created_at = utc_now()

    try:
        with database() as connection:
            connection.execute(
                """
                INSERT INTO messages (
                    message_id,
                    correlation_id,
                    causation_id,
                    sender_id,
                    recipient_id,
                    message_type,
                    content,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)
		""",
                (
                    payload.message_id,
                    payload.correlation_id,
                    payload.causation_id,
                    sender_id,
                    payload.recipient_id,
                    payload.message_type,
                    payload.content,
                    created_at,
                ),
            )

            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (payload.message_id,),
            ).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="message_id already exists",
        )

    return Message(**dict(row))

@app.get("/v1/inbox", response_model=list[Message])
def inbox(
    limit: int = Query(default=20, ge=1, le=100),
    recipient_id: str = Depends(authenticated_agent),
) -> list[Message]:
    delivered_at = utc_now()

    with database() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM messages
            WHERE recipient_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (recipient_id, limit),
        ).fetchall()

        queued_ids = [
            row["message_id"]
            for row in rows
            if row["status"] == "queued"
        ]

        if queued_ids:
            placeholders = ",".join("?" for _ in queued_ids)
            connection.execute(
                f"""
                UPDATE messages
                SET status = 'delivered', delivered_at = ?
                WHERE message_id IN ({placeholders})
                """,
                (delivered_at, *queued_ids),
            )

            rows = connection.execute(
                f"""
                SELECT *
                FROM messages
                WHERE message_id IN ({placeholders})
                ORDER BY created_at ASC
                """,
                queued_ids,
            ).fetchall()

    return [Message(**dict(row)) for row in rows]

@app.post("/v1/messages/{message_id}/ack", response_model=Message)
def acknowledge(
    message_id: str,
    recipient_id: str = Depends(authenticated_agent),
) -> Message:
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Message not found")

        if row["recipient_id"] != recipient_id:
            raise HTTPException(status_code=403, detail="Access denied")

        connection.execute(
            """
            UPDATE messages
            SET status = 'acknowledged', acknowledged_at = ?
            WHERE message_id = ?
            """,
            (utc_now(), message_id),
        )

        row = connection.execute(
            "SELECT * FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    return Message(**dict(row))
