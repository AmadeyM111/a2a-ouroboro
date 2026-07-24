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

## Next slice

Add `skills/a2a_gateway/crypto.py`, `client.py`, `plugin.py`, and `SKILL.md`.
The sender will encrypt and sign locally; the receiver will verify and decrypt
locally. The signed associated data must bind the outer routing metadata to the
encrypted inner payload.
