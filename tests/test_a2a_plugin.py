import json

from skills.a2a_gateway import plugin


class _FakeAPI:
    def __init__(self):
        self.tools = {}

    def get_settings(self, _keys):
        return {
            "A2A_AGENT_ID": "agent1",
            "A2A_KEY_ID": "agent1-key",
            "A2A_GATEWAY_URL": "http://gateway",
            "A2A_GATEWAY_TOKEN": "token",
            "A2A_X25519_PRIVATE_KEY_PATH": "/tmp/x25519",
            "A2A_ED25519_PRIVATE_KEY_PATH": "/tmp/ed25519",
            "A2A_PUBLIC_REGISTRY_PATH": "/tmp/registry",
            "A2A_RECIPIENT_KEY_IDS_JSON": '{"agent2":"agent2-key"}',
        }

    def register_tool(self, name, handler, **_kwargs):
        self.tools[name] = handler

    def log(self, *_args, **_kwargs):
        pass


class _FakeCrypto:
    @classmethod
    def from_files(cls, **_kwargs):
        return cls()


class _FakeClient:
    def __init__(self, **_kwargs):
        pass

    def acknowledge(self, message_id):
        return {
            "message_id": message_id,
            "conversation_id": "conversation-1",
            "sender_id": "agent2",
            "recipient_id": "agent1",
            "message_type": "request",
            "idempotency_key": "idem-000000000001",
            "status": "acknowledged",
            "created_at": "2026-07-24T00:00:00+00:00",
            "delivered_at": "2026-07-24T00:00:01+00:00",
            "acknowledged_at": "2026-07-24T00:00:02+00:00",
        }


def test_plugin_registers_ack_tool_and_forwards_message_id(monkeypatch):
    monkeypatch.setattr(plugin, "LocalCrypto", _FakeCrypto)
    monkeypatch.setattr(plugin, "A2AClient", _FakeClient)
    api = _FakeAPI()

    plugin.register(api)

    assert {"health", "send_message", "list_inbox", "ack", "reply"} <= set(api.tools)
    result = json.loads(api.tools["ack"](message_id="message-42"))
    assert result["ok"] is True
    assert result["message"]["message_id"] == "message-42"
    assert result["message"]["status"] == "acknowledged"


def test_plugin_ack_rejects_non_string_message_id(monkeypatch):
    monkeypatch.setattr(plugin, "LocalCrypto", _FakeCrypto)
    monkeypatch.setattr(plugin, "A2AClient", _FakeClient)
    api = _FakeAPI()
    plugin.register(api)

    result = json.loads(api.tools["ack"](message_id=42))
    assert result == {"error": "message_id is invalid", "ok": False}
