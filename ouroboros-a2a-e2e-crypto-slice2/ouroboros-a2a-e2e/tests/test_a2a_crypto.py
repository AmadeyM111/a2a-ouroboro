import base64
import json

import pytest

from skills.a2a_gateway.crypto import (
    ConfigurationError,
    EnvelopeError,
    LocalCrypto,
    generate_agent_key_material,
)


def build_agents(tmp_path):
    entries = []
    for number in (1, 2, 3):
        agent_id = f"agent{number}"
        key_id = f"{agent_id}-2026-01"
        entries.append(
            generate_agent_key_material(
                agent_id=agent_id,
                key_id=key_id,
                output_dir=tmp_path / agent_id,
            )
        )
    registry = {
        "version": 1,
        "agents": {
            item["agent_id"]: {
                item["key_id"]: {
                    "x25519_public": item["x25519_public"],
                    "ed25519_public": item["ed25519_public"],
                }
            }
            for item in entries
        },
    }
    registry_path = tmp_path / "public-keys.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    def load(agent_id):
        return LocalCrypto.from_files(
            agent_id=agent_id,
            key_id=f"{agent_id}-2026-01",
            x25519_private_path=tmp_path / agent_id / "x25519-private.pem",
            ed25519_private_path=tmp_path / agent_id / "ed25519-private.pem",
            public_registry_path=registry_path,
        )

    return load


def gateway_response(request, *, sender_id):
    return {
        **request,
        "message_id": "message-1",
        "sender_id": sender_id,
        "conversation_id": request["conversation_id"] or "conversation-1",
    }


def test_agent1_encrypts_and_agent2_decrypts(tmp_path):
    load = build_agents(tmp_path)
    encrypted = load("agent1").encrypt_message(
        recipient_id="agent2",
        recipient_key_id="agent2-2026-01",
        content="Закрытое поручение",
        idempotency_key="idem-000000000001",
        conversation_id="conversation-1",
    )
    assert "content" not in encrypted
    decrypted = load("agent2").decrypt_message(
        gateway_response(encrypted, sender_id="agent1")
    )
    assert decrypted["content"] == "Закрытое поручение"
    assert decrypted["sender_id"] == "agent1"


@pytest.mark.parametrize("field", ["ciphertext", "signature"])
def test_tampering_is_rejected(tmp_path, field):
    load = build_agents(tmp_path)
    encrypted = load("agent1").encrypt_message(
        recipient_id="agent2",
        recipient_key_id="agent2-2026-01",
        content="Секрет",
        idempotency_key="idem-000000000001",
    )
    raw = bytearray(base64.b64decode(encrypted[field]))
    raw[0] ^= 1
    encrypted[field] = base64.b64encode(raw).decode()
    with pytest.raises(EnvelopeError):
        load("agent2").decrypt_message(
            gateway_response(encrypted, sender_id="agent1")
        )


def test_routing_metadata_tampering_is_rejected(tmp_path):
    load = build_agents(tmp_path)
    encrypted = load("agent1").encrypt_message(
        recipient_id="agent2",
        recipient_key_id="agent2-2026-01",
        content="Секрет",
        idempotency_key="idem-000000000001",
        conversation_id="conversation-1",
    )
    response = gateway_response(encrypted, sender_id="agent1")
    response["conversation_id"] = "substituted-conversation"
    with pytest.raises(EnvelopeError, match="signature"):
        load("agent2").decrypt_message(response)


def test_wrong_recipient_cannot_decrypt(tmp_path):
    load = build_agents(tmp_path)
    encrypted = load("agent1").encrypt_message(
        recipient_id="agent2",
        recipient_key_id="agent2-2026-01",
        content="Секрет",
        idempotency_key="idem-000000000001",
    )
    with pytest.raises(EnvelopeError, match="another recipient"):
        load("agent3").decrypt_message(
            gateway_response(encrypted, sender_id="agent1")
        )


def test_private_key_must_match_trusted_identity(tmp_path):
    load = build_agents(tmp_path)
    load("agent1")
    with pytest.raises(ConfigurationError, match="does not match registry"):
        LocalCrypto.from_files(
            agent_id="agent2",
            key_id="agent2-2026-01",
            x25519_private_path=tmp_path / "agent1" / "x25519-private.pem",
            ed25519_private_path=tmp_path / "agent1" / "ed25519-private.pem",
            public_registry_path=tmp_path / "public-keys.json",
        )


def test_key_generation_refuses_overwrite(tmp_path):
    generate_agent_key_material(
        agent_id="agent1",
        key_id="agent1-2026-01",
        output_dir=tmp_path / "agent1",
    )
    with pytest.raises(ConfigurationError, match="refusing to overwrite"):
        generate_agent_key_material(
            agent_id="agent1",
            key_id="agent1-2026-01",
            output_dir=tmp_path / "agent1",
        )
