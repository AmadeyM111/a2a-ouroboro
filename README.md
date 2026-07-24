# Ouroboros 6.40.0 — A2A E2E Gateway, slice 1

This package updates only the external FastAPI Gateway. It does not modify
Ouroboros core.

## Apply on Fedora

From `/opt/ouroboro/a2a-ouroboro`:

```bash
cp gateway/app/main.py \
  "gateway/app/main.py.before-e2e-$(date -u +%Y%m%dT%H%M%SZ)"
```

Copy the supplied `gateway/app/main.py` over the repository file, then compile:

```bash
python -m py_compile gateway/app/main.py
```

The new implementation creates `messages_v2` and `dpa_events`. It deliberately
leaves the old `messages` table unchanged so the first migration is recoverable.
Old plaintext messages are not returned by the v2 API.

Rebuild only the Gateway service, replacing `a2a-gateway` below if the Compose
service has another name:

```bash
docker compose config --services
docker compose up -d --build --no-deps a2a-gateway
docker compose logs --tail=100 a2a-gateway
curl --fail --silent http://127.0.0.1:8000/health
```

Use the actual published Gateway port instead of `8000`.

## Run the contract tests

Run them in the Gateway image or its Python environment:

```bash
python -m pip install pytest httpx
python -m pytest -q tests/test_gateway_e2e_contract.py
```

Do not install test packages in the production image. Prefer a test stage or
temporary virtual environment.

## Security properties in this slice

- Authenticated identity determines `sender_id`.
- The client cannot submit `sender_id` or `message_id`.
- The Gateway stores ciphertext, cryptographic metadata, and ciphertext hash,
  but no plaintext body.
- A sender-scoped idempotency key suppresses identical retries and rejects a
  conflicting payload.
- Inbox returns only queued or delivered, unacknowledged messages.
- DPA is append-only and contains metadata, not message bodies or credentials.
- Unknown schema fields, malformed Base64, self-send, unknown recipients,
  invalid replies, and invalid credentials are rejected.

TLS/mTLS remains mandatory for transport even with E2E content encryption:
Bearer credentials and routing metadata are otherwise exposed in transit.

## Local E2E crypto (slice 2)

`skills/a2a_gateway/crypto.py` performs X25519/HKDF-SHA256 key derivation,
ChaCha20-Poly1305 encryption, and Ed25519 signing inside each agent.

Install the agent-side dependency:

```bash
python -m pip install 'cryptography>=46,<47'
```

Generate five key sets once, offline:

```bash
cd /opt/ouroboro/a2a-ouroboro
umask 077
python scripts/generate_a2a_keys.py \
  --output /opt/ouroboro/a2a-keys \
  --key-suffix 2026-01
```

Do not commit `/opt/ouroboro/a2a-keys`. Mount `public-keys.json` read-only into
all agents. Mount only `agentN/x25519-private.pem` and
`agentN/ed25519-private.pem` into AgentN. Never mount private keys into the
Gateway.

Verify:

```bash
python -m py_compile \
  skills/a2a_gateway/crypto.py \
  scripts/generate_a2a_keys.py
python -m pytest -q tests/test_a2a_crypto.py
```

## Encrypted Gateway client (slice 3)

`skills/a2a_gateway/client.py` connects `LocalCrypto` to the Gateway without
adding third-party HTTP dependencies. It implements:

- authenticated health checks;
- local encryption followed by `POST /v1/messages`;
- bounded inbox reads, signature verification, decryption, then ACK;
- encrypted replies that preserve conversation and reply relationships;
- bounded timeouts, response sizes, stable errors, and recipient allowlisting;
- transport retries that reuse the exact serialized encrypted envelope.

Verify:

```bash
python -m py_compile skills/a2a_gateway/client.py
PYTHONPATH="$PWD" python -m pytest -q \
  tests/test_a2a_crypto.py \
  tests/test_a2a_client.py
```

`plugin.py` and `SKILL.md` are still required before Ouroboros can expose the
client as agent tools. Until that registration is complete, do not send real
user content through the extension.
