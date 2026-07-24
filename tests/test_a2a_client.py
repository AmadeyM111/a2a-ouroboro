import io
import json
import urllib.error

import pytest

from skills.a2a_gateway.client import (
    A2AClient,
    ClientError,
    GatewayError,
    GatewayUnavailable,
)
from tests.test_a2a_crypto import build_agents


class Response:
    def __init__(self, body, status=200):
        self.status = status
        self.headers = {}
        self._data = json.dumps(body).encode()

    def read(self, limit=-1):
        return self._data if limit < 0 else self._data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ScriptedOpener:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.handler(request, len(self.requests))


def gateway_message(request_body, *, sender_id="agent1", status="queued"):
    return {
        **request_body,
        "message_id": "message-1",
        "sender_id": sender_id,
        "ciphertext_sha256": "a" * 64,
        "status": status,
        "created_at": "2026-07-23T20:00:00+00:00",
        "delivered_at": None,
        "acknowledged_at": None,
    }


def make_client(tmp_path, opener, agent_id="agent1"):
    load = build_agents(tmp_path)
    return A2AClient(
        base_url="http://gateway:8000",
        bearer_token=f"token-{agent_id}",
        crypto=load(agent_id),
        recipient_key_ids={
            "agent1": "agent1-2026-01",
            "agent2": "agent2-2026-01",
            "agent3": "agent3-2026-01",
        },
        opener=opener,
    ), load


def test_send_encrypts_and_does_not_send_plaintext(tmp_path):
    def handler(request, _):
        body = json.loads(request.data)
        assert "Закрытое поручение" not in request.data.decode()
        assert request.headers["Authorization"] == "Bearer token-agent1"
        return Response(gateway_message(body), status=201)

    opener = ScriptedOpener(handler)
    client, _ = make_client(tmp_path, opener)
    sent = client.send_message(
        recipient_id="agent2",
        content="Закрытое поручение",
        idempotency_key="idem-000000000001",
    )
    assert sent["sender_id"] == "agent1"
    assert len(opener.requests) == 1


def test_retry_reuses_identical_encrypted_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr("skills.a2a_gateway.client.time.sleep", lambda _: None)
    bodies = []

    def handler(request, call_number):
        bodies.append(request.data)
        if call_number == 1:
            raise urllib.error.URLError("connection reset")
        return Response(gateway_message(json.loads(request.data)), status=201)

    opener = ScriptedOpener(handler)
    client, _ = make_client(tmp_path, opener)
    client.send_message(
        recipient_id="agent2",
        content="Повторяемый запрос",
        idempotency_key="idem-000000000001",
    )
    assert bodies[0] == bodies[1]


def test_inbox_decrypts_then_acknowledges(tmp_path):
    state = {}

    def handler(request, call_number):
        if call_number == 1:
            return Response([state["message"]])
        assert request.full_url.endswith("/v1/messages/message-1/ack")
        ack = dict(state["message"])
        ack["status"] = "acknowledged"
        ack["acknowledged_at"] = "2026-07-23T20:01:00+00:00"
        return Response(ack)

    opener = ScriptedOpener(handler)
    client, load = make_client(tmp_path, opener, agent_id="agent2")
    encrypted = load("agent1").encrypt_message(
        recipient_id="agent2",
        recipient_key_id="agent2-2026-01",
        content="Проверь документ",
        idempotency_key="idem-000000000001",
        conversation_id="conversation-1",
    )
    state["message"] = gateway_message(
        encrypted, sender_id="agent1", status="delivered"
    )
    messages = client.list_inbox()
    assert messages[0]["content"] == "Проверь документ"
    assert messages[0]["acknowledged_at"] is not None
    assert len(opener.requests) == 2


def test_tampered_inbox_is_not_acknowledged(tmp_path):
    opener = ScriptedOpener(lambda _request, _number: Response([]))
    client, load = make_client(tmp_path, opener, agent_id="agent2")
    encrypted = load("agent1").encrypt_message(
        recipient_id="agent2",
        recipient_key_id="agent2-2026-01",
        content="Секрет",
        idempotency_key="idem-000000000001",
    )
    message = gateway_message(encrypted, sender_id="agent1", status="delivered")
    message["signature"] = message["signature"][:-2] + "AA"
    opener.handler = lambda _request, _number: Response([message])
    with pytest.raises(GatewayError, match="cryptographic"):
        client.list_inbox()
    assert len(opener.requests) == 1


def test_reply_uses_original_relationship(tmp_path):
    def handler(request, _):
        body = json.loads(request.data)
        assert body["message_type"] == "response"
        assert body["reply_to_message_id"] == "original-1"
        assert body["conversation_id"] == "conversation-1"
        return Response(
            gateway_message(body, sender_id="agent2"), status=201
        )

    opener = ScriptedOpener(handler)
    client, _ = make_client(tmp_path, opener, agent_id="agent2")
    reply = client.reply(
        message_id="original-1",
        conversation_id="conversation-1",
        recipient_id="agent1",
        content="Выполнено",
        idempotency_key="idem-000000000002",
    )
    assert reply["recipient_id"] == "agent1"


def test_unknown_recipient_rejected_before_network(tmp_path):
    opener = ScriptedOpener(lambda *_: Response({}))
    client, _ = make_client(tmp_path, opener)
    with pytest.raises(ClientError, match="allowlist"):
        client.send_message(recipient_id="agent99", content="test")
    assert opener.requests == []


def test_gateway_error_is_stable_and_bounded(tmp_path):
    def handler(request, _):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"detail":"Invalid A2A token"}'),
        )

    client, _ = make_client(tmp_path, ScriptedOpener(handler))
    with pytest.raises(GatewayError, match=r"\(401\): Invalid A2A token"):
        client.list_inbox()


def test_transport_failure_has_no_secret_detail(tmp_path, monkeypatch):
    monkeypatch.setattr("skills.a2a_gateway.client.time.sleep", lambda _: None)
    client, _ = make_client(
        tmp_path,
        ScriptedOpener(
            lambda *_: (_ for _ in ()).throw(
                urllib.error.URLError("token-agent1 leaked detail")
            )
        ),
    )
    with pytest.raises(GatewayUnavailable) as error:
        client.list_inbox()
    assert "token-agent1" not in str(error.value)
