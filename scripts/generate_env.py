#!/usr/bin/env python3
"""Generate a fresh .env from .env.example with cryptographically random tokens.

Only secrets that should be unique per deployment are generated on the fly.
Everything else (ports, profiles, provider choices, model names, paths) stays
as .env.example defaults for the user to edit.

Generated (random):
  JWT_SECRET_KEY, ADMIN_PASSWORD, ADMIN_STATIC_TOKEN,
  API_SERVER_KEY (= HERMES_API_KEY),
  OPENCLAW_GATEWAY_TOKEN (= OPENCLAW_API_KEY),
  PG_PASSWORD (= POSTGRES_PASSWORD),
  HERMES_DASHBOARD_BASIC_AUTH_PASSWORD, HERMES_DASHBOARD_BASIC_AUTH_SECRET

Port offsets (+10 from .env.example defaults to avoid sibling conflicts):
  CONTAINER_PORT, POSTGRES_PORT, HERMES_GATEWAY_PORT,
  HERMES_DASHBOARD_PORT, OPENCLAW_PORT

User must set before first start:
  OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY / OPENAI_COMPAT_MODEL
  (or a provider-specific API key like ANTHROPIC_API_KEY)
  HERMES_MODEL_PROVIDER (default: anthropic — change to openai for OPENAI_COMPAT_*)
  OPENCLAW_MODEL_PROVIDER (default: empty — set to openai_compat for OPENAI_COMPAT_*)

Usage:
    python scripts/generate_env.py              # writes .env (refuses to overwrite)
    python scripts/generate_env.py --force       # overwrite existing .env
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
from pathlib import Path

PORT_OFFSET = 10

# Vars that get random hex tokens
RANDOM_VARS = [
    "JWT_SECRET_KEY",
    "ADMIN_PASSWORD",
    "API_SERVER_KEY",
    "OPENCLAW_GATEWAY_TOKEN",
    "PG_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
]

# Vars that must copy another var's generated value
# (resolved after RANDOM_VARS are generated)
ALIAS_MAP = {
    "HERMES_API_KEY": "API_SERVER_KEY",
    "OPENCLAW_API_KEY": "OPENCLAW_GATEWAY_TOKEN",
    "POSTGRES_PASSWORD": "PG_PASSWORD",
}

# Host-published ports to offset (var -> .env.example default)
PORT_VARS = {
    "CONTAINER_PORT": 8765,
    "POSTGRES_PORT": 5432,
    "HERMES_GATEWAY_PORT": 8642,
    "HERMES_DASHBOARD_PORT": 9119,
    "OPENCLAW_PORT": 18789,
}


def generate_token() -> str:
    return secrets.token_hex(32)


def generate_jwt(secret: str) -> str:
    header = json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":"))
    payload = json.dumps(
        {"username": "admin", "role": "admin", "perm": True},
        separators=(",", ":"),
    )
    b64h = base64.urlsafe_b64encode(header.encode()).rstrip(b"=").decode()
    b64p = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    signing_input = f"{b64h}.{b64p}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    b64s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{b64h}.{b64p}.{b64s}"


def main():
    parser = argparse.ArgumentParser(
        description="Generate a fresh .env with random tokens from .env.example"
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing .env")
    args = parser.parse_args()

    env_path = Path(".env")
    template_path = Path(".env.example")

    if not template_path.exists():
        print(f"ERROR: {template_path} not found", file=sys.stderr)
        sys.exit(1)

    if env_path.exists() and not args.force:
        print(f"ERROR: {env_path} already exists. Use --force to overwrite.", file=sys.stderr)
        sys.exit(1)

    template = template_path.read_text(encoding="utf-8")

    # Generate random tokens
    generated: dict[str, str] = {}
    for var in RANDOM_VARS:
        generated[var] = generate_token()

    # ADMIN_STATIC_TOKEN is a JWT minted from JWT_SECRET_KEY
    generated["ADMIN_STATIC_TOKEN"] = generate_jwt(generated["JWT_SECRET_KEY"])

    # Aliases (copy from the source var)
    for alias, source in ALIAS_MAP.items():
        generated[alias] = generated[source]

    # Process template lines
    output_lines: list[str] = []
    for line in template.splitlines():
        # Skip the "TEMPLATE" header line
        if "TEMPLATE" in line and "Copy to .env" in line:
            continue

        # Match VAR=value lines (not comments)
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not match:
            # Check for commented-out vars that we need to uncomment + set
            commented = re.match(r"^#\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
            if commented:
                var_name = commented.group(1)
                if var_name in generated:
                    output_lines.append(f"{var_name}={generated[var_name]}")
                    continue
            output_lines.append(line)
            continue

        var_name = match.group(1)

        if var_name in generated:
            output_lines.append(f"{var_name}={generated[var_name]}")
        elif var_name in PORT_VARS:
            output_lines.append(f"{var_name}={PORT_VARS[var_name] + PORT_OFFSET}")
        else:
            output_lines.append(line)

    env_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    print(f"Generated {env_path} with fresh random tokens.")
    print()
    print("Generated secrets (saved in .env):")
    for var in RANDOM_VARS + ["ADMIN_STATIC_TOKEN"]:
        val = generated[var]
        masked = val[:8] + "..." + val[-8:] if len(val) > 20 else val
        print(f"  {var} = {masked}")
    for alias, source in ALIAS_MAP.items():
        val = generated[alias]
        masked = val[:8] + "..." + val[-8:] if len(val) > 20 else val
        print(f"  {alias} = {masked}  (= {source})")
    print()
    print("Ports offset +10 from .env.example defaults to avoid sibling conflicts.")
    print()
    print("Before first start, edit .env and set:")
    print("  1. COMPOSE_PROFILES           (default: postgres,hermes — add openclaw if needed)")
    print("  2. LLM credentials:")
    print("     OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY / OPENAI_COMPAT_MODEL")
    print("     (or a provider-specific key like ANTHROPIC_API_KEY)")
    print("  3. HERMES_MODEL_PROVIDER       (set to 'openai' to use OPENAI_COMPAT_*)")
    print("  4. OPENCLAW_MODEL_PROVIDER     (set to 'openai_compat' to use OPENAI_COMPAT_*)")
    print("  5. HERMES_DASHBOARD_BASIC_AUTH_USERNAME (set to 'admin' if dashboard is on)")
    print()
    print("Then:")
    print("  mkdir -p www/hermes www/openclaw www/projects www/logs postgres_data postgres_logs")
    print("  docker compose build && docker compose up -d")


if __name__ == "__main__":
    main()