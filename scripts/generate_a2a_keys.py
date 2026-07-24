"""Offline key generator for the five Bank A2A agents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from skills.a2a_gateway.crypto import generate_agent_key_material


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--key-suffix",
        default=os.environ.get("A2A_KEY_SUFFIX", "2026-01"),
        help="key-id suffix; defaults to A2A_KEY_SUFFIX or 2026-01",
    )
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry = {"version": 1, "agents": {}}
    for number in range(1, 6):
        agent_id = f"agent{number}"
        key_id = f"{agent_id}-{args.key_suffix}"
        entry = generate_agent_key_material(
            agent_id=agent_id,
            key_id=key_id,
            output_dir=root / agent_id,
        )
        registry["agents"][agent_id] = {
            key_id: {
                "x25519_public": entry["x25519_public"],
                "ed25519_public": entry["ed25519_public"],
            }
        }
    registry_path = root / "public-keys.json"
    registry_path.write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(registry_path, 0o444)
    print(f"Created key material under {root}")
    print("Distribute only public-keys.json; keep private directories separate.")


if __name__ == "__main__":
    main()
