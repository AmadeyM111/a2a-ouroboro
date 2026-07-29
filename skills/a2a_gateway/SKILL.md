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
- `send_message` — send a message to another agent;
- `list_inbox` — receive messages for the local agent;
- `ack` — acknowledge processing;
- `reply` — reply to the sender.

The plugin registers these short names intentionally. `PluginAPI.register_tool()`
applies the host namespace automatically; the plugin must not pass canonical
`ext_13_r_a2a_gateway_*` names or they would be namespaced twice.

Runtime names are namespaced by the host extension loader to prevent collisions.
For this skill the canonical tool names are:

- `ext_13_r_a2a_gateway_health`
- `ext_13_r_a2a_gateway_send_message`
- `ext_13_r_a2a_gateway_list_inbox`
- `ext_13_r_a2a_gateway_ack`
- `ext_13_r_a2a_gateway_reply`

The short names above are the plugin-local names only. Agent/tool-registry
surfaces must use the canonical names; `ack` is intentionally separate from
`list_inbox` so the caller can durably process a decrypted message first.

The sender identity is taken from trusted local settings and cannot be supplied
by the model.
