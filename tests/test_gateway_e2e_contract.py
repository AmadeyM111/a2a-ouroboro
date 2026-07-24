import base64
import importlib
import os

from fastapi.testclient import TestClient


def b64(length: int) -> str:
    return base64.b64encode(b"x" * length).decode()


def envelope(**overrides):
    value = {
        "recipient_id": "agent2",
        "message_type": "request",
        "idempotency_key": "idem-000000000001",
        "encryption_version": 1,
        "algorithm": "X25519-HKDF-SHA256+CHACHA20-POLY1305+ED25519",
        "sender_key_id": "agent1-2026-01",
        "recipient_key_id": "agent2-2026-01",
        "ephemeral_public_key": b64(32),
        "nonce": b64(12),
        "ciphertext": b64(32),
        "signature": b64(64),
    }
    value.update(overrides)
    return value


def test_send_inbox_ack_reply_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_DATABASE_PATH", str(tmp_path / "gateway.db"))
    monkeypatch.setenv("AGENT1_A2A_TOKEN", "token-agent1")
    monkeypatch.setenv("AGENT2_A2A_TOKEN", "token-agent2")

    import gateway.app.main as main

    importlib.reload(main)
    main.initialise_database()
    client = TestClient(main.app)
    h1 = {"Authorization": "Bearer token-agent1"}
    h2 = {"Authorization": "Bearer token-agent2"}

    sent = client.post("/v1/messages", headers=h1, json=envelope())
    assert sent.status_code == 201
    message = sent.json()
    assert message["sender_id"] == "agent1"
    assert "content" not in message

    duplicate = client.post("/v1/messages", headers=h1, json=envelope())
    assert duplicate.status_code == 201
    assert duplicate.json()["message_id"] == message["message_id"]

    received = client.get("/v1/inbox", headers=h2).json()
    assert [item["message_id"] for item in received] == [message["message_id"]]

    ack = client.post(
        f"/v1/messages/{message['message_id']}/ack", headers=h2
    )
    assert ack.status_code == 200
    assert ack.json()["status"] == "acknowledged"
    assert client.get("/v1/inbox", headers=h2).json() == []

    reply_payload = envelope(
        recipient_id="agent1",
        message_type="response",
        idempotency_key="idem-000000000002",
        reply_to_message_id=message["message_id"],
        sender_key_id="agent2-2026-01",
        recipient_key_id="agent1-2026-01",
    )
    reply = client.post("/v1/messages", headers=h2, json=reply_payload)
    assert reply.status_code == 201
    assert reply.json()["conversation_id"] == message["conversation_id"]
    assert len(client.get("/v1/inbox", headers=h1).json()) == 1


def test_negative_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_DATABASE_PATH", str(tmp_path / "gateway.db"))
    monkeypatch.setenv("AGENT1_A2A_TOKEN", "token-agent1")

    import gateway.app.main as main

    importlib.reload(main)
    main.initialise_database()
    client = TestClient(main.app)
    h1 = {"Authorization": "Bearer token-agent1"}

    assert client.get("/v1/inbox").status_code == 401
    assert client.post(
        "/v1/messages",
        headers=h1,
        json=envelope(recipient_id="agent99"),
    ).status_code == 404
    assert client.post(
        "/v1/messages",
        headers=h1,
        json=envelope(recipient_id="agent1"),
    ).status_code == 400
    assert client.post(
        "/v1/messages",
        headers=h1,
        json=envelope(sender_id="agent5"),
    ).status_code == 422
    assert client.post(
        "/v1/messages",
        headers=h1,
        json=envelope(ciphertext="not-base64"),
    ).status_code == 422
    with main.database() as connection:
        assert connection.execute(
            "SELECT 1 FROM dpa_events WHERE reason_code = 'malformed_envelope'"
        ).fetchone() is not None


def test_rejected_reply_and_ack_are_written_to_dpa(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_DATABASE_PATH", str(tmp_path / "gateway.db"))
    monkeypatch.setenv("AGENT1_A2A_TOKEN", "token-agent1")
    monkeypatch.setenv("AGENT2_A2A_TOKEN", "token-agent2")

    import gateway.app.main as main

    importlib.reload(main)
    main.initialise_database()
    client = TestClient(main.app)
    h1 = {"Authorization": "Bearer token-agent1"}
    h2 = {"Authorization": "Bearer token-agent2"}

    original = client.post("/v1/messages", headers=h1, json=envelope()).json()
    invalid_reply = envelope(
        recipient_id="agent2",
        message_type="response",
        idempotency_key="idem-000000000002",
        reply_to_message_id=original["message_id"],
    )
    assert client.post("/v1/messages", headers=h1, json=invalid_reply).status_code == 403

    assert client.post("/v1/messages/missing-message/ack", headers=h1).status_code == 404
    assert client.post(
        f"/v1/messages/{original['message_id']}/ack", headers=h1
    ).status_code == 403

    with main.database() as connection:
        rows = connection.execute(
            "SELECT reason_code FROM dpa_events WHERE outcome = 'rejected' ORDER BY rowid"
        ).fetchall()
    assert {row["reason_code"] for row in rows} >= {
        "invalid_reply_relationship",
        "message_not_found",
        "ack_recipient_mismatch",
    }
