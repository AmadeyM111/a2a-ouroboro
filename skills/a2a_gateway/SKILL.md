---
name: a2a_gateway
description: Controlled agent-to-agent messaging through an authenticated gateway.
version: 0.1.0
type: extension
entry: plugin.py
permissions: [net, tool, read_settings]
env_from_settings:
  - A2A_AGENT_ID
  - A2A_KEY_ID
  - A2A_GATEWAY_URL
  - A2A_GATEWAY_TOKEN
  - A2A_X25519_PRIVATE_KEY_PATH
  - A2A_ED25519_PRIVATE_KEY_PATH
  - A2A_PUBLIC_REGISTRY_PATH
  - A2A_RECIPIENT_KEY_IDS_JSON
---

# A2A Gateway

Provides authenticated communication between independent Ouroboros agents.

Tools:

- `health` — check Gateway availability;
- `send` — send a message to another agent;
- `inbox` — receive messages for the local agent;
- `ack` — acknowledge processing;
- `reply` — reply to the sender.

The sender identity is taken from trusted local settings and cannot be supplied
by the model.
