#!/opt/venv/bin/python
"""Seed default a2a_agents records for both Hermes and OpenClaw bridges.

Runs after the gateway is healthy. Idempotent — uses insertUpdateA2aAgent
(upsert), so it's safe to run on every container start.

Registers:
  - a2a-hermes-agent  → agent_type: "hermes"
  - a2a-openclaw-agent → agent_type: "openclaw"

Both with metadata containing agent_type so the A2A executor resolves
the handler via AGENT_TYPE_MAP without relying on env-var fallbacks.
"""
import json
import os
import sys
import time

import requests

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8765")
ENDPOINT_ID = os.environ.get("SEED_ENDPOINT_ID", "a2a")
PART_ID = os.environ.get("SEED_PART_ID", "default")
UPDATED_BY = os.environ.get("SEED_UPDATED_BY", "system")

# Auth: use ADMIN_STATIC_TOKEN if set, otherwise mint from JWT_SECRET_KEY.
STATIC_TOKEN = os.environ.get("ADMIN_STATIC_TOKEN", "").strip()
if STATIC_TOKEN:
    TOKEN = STATIC_TOKEN
else:
    import hmac
    import hashlib
    import base64
    secret = os.environ.get("JWT_SECRET_KEY", "CHANGEME")
    payload = json.dumps({"username": "a2a-seed", "role": "admin", "perm": True})
    header = json.dumps({"alg": "HS256", "typ": "JWT"})
    b64_header = base64.urlsafe_b64encode(header.encode()).rstrip(b"=").decode()
    b64_payload = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    signing_input = f"{b64_header}.{b64_payload}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    TOKEN = f"{b64_header}.{b64_payload}.{b64_sig}"

AGENTS = [
    {
        "agentId": os.environ.get("A2A_HERMES_AGENT_UUID", "a2a-hermes-agent"),
        "agentName": os.environ.get("A2A_HERMES_AGENT_NAME", "Hermes Agent"),
        "metadata": {
            "agent_type": os.environ.get("A2A_HERMES_AGENT_TYPE", "hermes"),
        },
    },
    {
        "agentId": os.environ.get("A2A_OPENCLAW_AGENT_UUID", "a2a-openclaw-agent"),
        "agentName": os.environ.get("A2A_OPENCLAW_AGENT_NAME", "OpenClaw Agent"),
        "metadata": {
            "agent_type": os.environ.get("A2A_OPENCLAW_AGENT_TYPE", "openclaw"),
        },
    },
]


def wait_for_gateway(timeout: int = 120):
    """Poll /health until the gateway is ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{GATEWAY_URL}/health", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def seed_agent(agent: dict) -> bool:
    """Upsert an agent record via GraphQL."""
    mutation = """
    mutation SeedAgent($agentId: String, $agentName: String!, $endpointId: String!, $endpointUrl: String!, $partId: String!, $updatedBy: String!, $metadata: JSON) {
      insertUpdateA2aAgent(
        agentId: $agentId
        agentName: $agentName
        endpointId: $endpointId
        endpointUrl: $endpointUrl
        partId: $partId
        updatedBy: $updatedBy
        metadata: $metadata
      ) {
        a2aAgent {
          agentId
          agentName
        }
      }
    }
    """
    variables = {
        "agentId": agent["agentId"],
        "agentName": agent["agentName"],
        "endpointId": ENDPOINT_ID,
        "endpointUrl": f"{GATEWAY_URL}/{ENDPOINT_ID}/a2a",
        "partId": PART_ID,
        "updatedBy": UPDATED_BY,
        "metadata": agent["metadata"],
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Part-Id": PART_ID,
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(
            f"{GATEWAY_URL}/{ENDPOINT_ID}/a2a_core_graphql",
            headers=headers,
            json={"query": mutation, "variables": variables},
            timeout=15,
        )
        data = r.json()
        if "errors" in data:
            print(f"[seed_agents] ERROR for {agent['agentId']}: {data['errors']}", file=sys.stderr)
            return False
        agent_id = data.get("data", {}).get("insertUpdateA2aAgent", {}).get("a2aAgent", {}).get("agentId", "?")
        print(f"[seed_agents] OK: {agent['agentId']} -> {agent_id}")
        return True
    except Exception as e:
        print(f"[seed_agents] FAILED for {agent['agentId']}: {e}", file=sys.stderr)
        return False


def main():
    if not wait_for_gateway():
        print("[seed_agents] Gateway not reachable, skipping agent seeding.", file=sys.stderr)
        sys.exit(0)

    ok = True
    for agent in AGENTS:
        if not seed_agent(agent):
            ok = False
    if ok:
        print("[seed_agents] All agents seeded successfully.")
    else:
        print("[seed_agents] Some agents failed to seed — check errors above.", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()