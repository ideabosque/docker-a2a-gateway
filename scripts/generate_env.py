#!/usr/bin/env python3
"""Generate or update .env from .env.example with cryptographically random tokens.

Only secrets that should be unique per deployment are generated on the fly.
Everything else (ports, profiles, provider choices, model names, paths) stays
as .env.example defaults for the user to edit.

Safe to re-run with --force: an already-set value in .env (a previously
generated secret, or anything you edited by hand) is always PRESERVED, never
overwritten. Only vars that are missing or blank get a fresh value. This
matters because several of these vars are baked into already-running state —
regenerating PG_PASSWORD after Postgres has initialized its data directory
with the old one breaks the connection; regenerating OPENCLAW_GATEWAY_TOKEN
after OpenClaw has onboarded with the old one breaks the gateway bridge —
so blowing them away on every re-run would be actively harmful, not just
redundant. Re-run --force any time .env.example gains new vars you want
pulled into an existing .env.

Generated (random, only when not already set):
  JWT_SECRET_KEY, ADMIN_PASSWORD, ADMIN_STATIC_TOKEN,
  API_SERVER_KEY (= HERMES_API_KEY),
  OPENCLAW_GATEWAY_TOKEN (= OPENCLAW_API_KEY),
  PG_PASSWORD (also bootstraps the bundled postgres service directly — see
  docker-compose.yml; there is no separate POSTGRES_PASSWORD to alias),
  HERMES_DASHBOARD_BASIC_AUTH_PASSWORD, HERMES_DASHBOARD_BASIC_AUTH_SECRET

Port offsets (+10 from .env.example defaults to avoid sibling conflicts,
only applied when the var isn't already set):
  CONTAINER_PORT, POSTGRES_PORT, HERMES_GATEWAY_PORT,
  HERMES_DASHBOARD_PORT, OPENCLAW_PORT

CLI overrides (always win, even over an already-set value):
  --openai-compat-api-key / --openai-compat-base-url / --openai-compat-model

User must set before first start (unless passed via the flags above):
  OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY / OPENAI_COMPAT_MODEL
  (or a provider-specific API key like ANTHROPIC_API_KEY)
  HERMES_MODEL_PROVIDER (default: anthropic — change to openai for OPENAI_COMPAT_*)
  OPENCLAW_MODEL_PROVIDER (default: empty — set to openai_compat for OPENAI_COMPAT_*)

Usage:
    python scripts/generate_env.py                        # create .env (refuses if it exists)
    python scripts/generate_env.py --force                 # create, or update in place — see above
    python scripts/generate_env.py --force --openai-compat-api-key ollama-... --openai-compat-model glm-5.2
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

# Vars that get random hex tokens (only when not already set — see module docstring)
RANDOM_VARS = [
    "JWT_SECRET_KEY",
    "ADMIN_PASSWORD",
    "API_SERVER_KEY",
    "OPENCLAW_GATEWAY_TOKEN",
    "PG_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
]

# Vars that must copy another var's generated value (only when not already set)
ALIAS_MAP = {
    "HERMES_API_KEY": "API_SERVER_KEY",
    "OPENCLAW_API_KEY": "OPENCLAW_GATEWAY_TOKEN",
}

# Host-published ports to offset (var -> .env.example default), only when not already set
PORT_VARS = {
    "CONTAINER_PORT": 8765,
    "POSTGRES_PORT": 5432,
    "HERMES_GATEWAY_PORT": 8642,
    "HERMES_DASHBOARD_PORT": 9119,
    "OPENCLAW_PORT": 18789,
}

# CLI flag -> env var name, for values that should always override (even an
# already-set one) since the caller passed them explicitly.
CLI_OVERRIDE_VARS = {
    "openai_compat_api_key": "OPENAI_COMPAT_API_KEY",
    "openai_compat_base_url": "OPENAI_COMPAT_BASE_URL",
    "openai_compat_model": "OPENAI_COMPAT_MODEL",
}

VAR_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
COMMENTED_VAR_LINE_RE = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=(.*)$")


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


def load_env_vars(path: Path) -> dict[str, str]:
    """Parse VAR=value lines from an existing .env (comments/blank lines skipped)."""
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = VAR_LINE_RE.match(line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Generate or update .env from .env.example with cryptographically random tokens"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Update an existing .env in place instead of refusing to run. "
             "Already-set values (including previously generated secrets) are "
             "always preserved; only vars that are missing or blank are filled in.",
    )
    parser.add_argument("--openai-compat-api-key", default=None,
                        help="Set OPENAI_COMPAT_API_KEY (overrides any existing value)")
    parser.add_argument("--openai-compat-base-url", default=None,
                        help="Set OPENAI_COMPAT_BASE_URL (overrides any existing value)")
    parser.add_argument("--openai-compat-model", default=None,
                        help="Set OPENAI_COMPAT_MODEL (overrides any existing value)")
    args = parser.parse_args()

    env_path = Path(".env")
    template_path = Path(".env.example")

    if not template_path.exists():
        print(f"ERROR: {template_path} not found", file=sys.stderr)
        sys.exit(1)

    if env_path.exists() and not args.force:
        print(
            f"ERROR: {env_path} already exists. Use --force to update it in place - "
            f"already-set values (including generated secrets) are preserved; only "
            f"vars that are missing or blank are filled in.",
            file=sys.stderr,
        )
        sys.exit(1)

    template = template_path.read_text(encoding="utf-8")

    # Load whatever is already in .env so re-running (--force) never clobbers a
    # value that's already baked into running state (or that the user set by
    # hand) — see the module docstring for why this matters for PG_PASSWORD /
    # OPENCLAW_GATEWAY_TOKEN specifically.
    existing = load_env_vars(env_path) if env_path.exists() else {}

    def current(var: str) -> str:
        return existing.get(var, "").strip()

    cli_overrides: dict[str, str] = {}
    for flag_attr, var_name in CLI_OVERRIDE_VARS.items():
        value = getattr(args, flag_attr)
        if value:
            cli_overrides[var_name] = value

    # Random tokens — reuse the existing value if already set, so a repeat run
    # only fills in what's actually missing.
    generated: dict[str, str] = {}
    fresh: set[str] = set()
    for var in RANDOM_VARS:
        existing_val = current(var)
        if existing_val:
            generated[var] = existing_val
        else:
            generated[var] = generate_token()
            fresh.add(var)

    # ADMIN_STATIC_TOKEN is a JWT minted from JWT_SECRET_KEY.
    existing_token = current("ADMIN_STATIC_TOKEN")
    if existing_token:
        generated["ADMIN_STATIC_TOKEN"] = existing_token
    else:
        generated["ADMIN_STATIC_TOKEN"] = generate_jwt(generated["JWT_SECRET_KEY"])
        fresh.add("ADMIN_STATIC_TOKEN")

    # Aliases (copy from the source var) — reuse the existing value if already set.
    for alias, source in ALIAS_MAP.items():
        existing_val = current(alias)
        if existing_val:
            generated[alias] = existing_val
        else:
            generated[alias] = generated[source]
            fresh.add(alias)

    # Process template lines
    output_lines: list[str] = []
    for line in template.splitlines():
        # Skip the "TEMPLATE" header line
        if "TEMPLATE" in line and "Copy to .env" in line:
            continue

        match = VAR_LINE_RE.match(line)
        if not match:
            # Check for commented-out vars that we need to uncomment + set.
            # Only `generated` (RANDOM_VARS/ALIAS_MAP/ADMIN_STATIC_TOKEN) is
            # checked here, deliberately NOT cli_overrides: cli_overrides
            # target plain vars like OPENAI_COMPAT_API_KEY that already exist
            # as real uncommented lines elsewhere in the template (handled by
            # the branch below) — some of those same var names also appear
            # inline inside prose comments (e.g. "# OPENAI_COMPAT_API_KEY=
            # <your-key> (shared with OpenClaw)"), which this regex would
            # otherwise mistake for a genuine commented-out var and "activate"
            # a second, duplicate line for.
            commented = COMMENTED_VAR_LINE_RE.match(line)
            if commented:
                var_name = commented.group(1)
                if var_name in generated:
                    output_lines.append(f"{var_name}={generated[var_name]}")
                    continue
            output_lines.append(line)
            continue

        var_name = match.group(1)

        if var_name in cli_overrides:
            output_lines.append(f"{var_name}={cli_overrides[var_name]}")
        elif var_name in generated:
            output_lines.append(f"{var_name}={generated[var_name]}")
        elif var_name in PORT_VARS:
            port_val = current(var_name) or str(PORT_VARS[var_name] + PORT_OFFSET)
            output_lines.append(f"{var_name}={port_val}")
        elif var_name in existing:
            # Preserve whatever the user already has for every other var too
            # (LLM keys, COMPOSE_PROFILES, model choices, ...) — the template
            # default only applies to vars that don't exist in .env yet.
            output_lines.append(f"{var_name}={existing[var_name]}")
        else:
            output_lines.append(line)

    env_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    action = "Updated" if existing else "Generated"
    print(f"{action} {env_path}.")
    print()

    secret_vars = RANDOM_VARS + ["ADMIN_STATIC_TOKEN"] + list(ALIAS_MAP)

    fresh_in_order = [v for v in secret_vars if v in fresh]
    if fresh_in_order:
        print("Freshly generated secrets:")
        for var in fresh_in_order:
            val = generated[var]
            masked = val[:8] + "..." + val[-8:] if len(val) > 20 else val
            alias_note = f"  (= {ALIAS_MAP[var]})" if var in ALIAS_MAP else ""
            print(f"  {var} = {masked}{alias_note}")
        print()

    preserved = [v for v in secret_vars if v not in fresh and v in existing]
    if preserved:
        print("Preserved (already set in .env - not regenerated):")
        for var in preserved:
            print(f"  {var}")
        print()

    if cli_overrides:
        print("Set from CLI flags (always overrides):")
        for var, val in cli_overrides.items():
            print(f"  {var} = {val}")
        print()

    if not existing:
        print("Ports offset +10 from .env.example defaults to avoid sibling conflicts.")
        print()

    print("Before first start, edit .env and set:")
    print("  1. COMPOSE_PROFILES           (default: postgres,hermes - add openclaw if needed)")
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
