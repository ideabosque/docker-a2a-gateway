# A2A-Gateway E2E Runbook — supplier rating on the docker stack (P-7)

Status prepared 2026-10-15 after the S0/S1 build (E-1/E-2/E-3/E-8, A-1/A-3/A-5/A-6, W-0/W-1/W-2 all landed and committed).

## What the docker stack needs to serve the E2E

1. **Engine module in the image**: `requirements-modules.txt` pins
   `silvaengine_gateway` + `a2a_protocol_plugin` + `silvaengine_daemon` from
   GitHub `@main`. Today's engine work (E-1/E-2/E-3/E-8 + migration `0020`)
   lives only locally until it is committed & pushed. **Commit & push**
   `supplier_rating_engine`, `supplier_rating_agent_system`
   (`supplier_rating_workbench` optional for the served runtime), then rebuild:
   `docker compose build a2a-gateway`.
2. **Settings keys**: `settings.yaml` + the engine module route now pass
   `ACTOR_FROM_PRINCIPAL` / `ENFORCE_ROLES` (defaults `"false"` — flip to
   `"true"` in the stack `.env` for the E2E, with `GATEWAY_AUTH_PROVIDER=local`).
3. **Database**: the engine migrations target the stack's `a2a-postgres`
   (`PG_PORT=5442` on the host). Host-side:
   `DATABASE_URL=postgresql+psycopg2://<PG_USER>:<PG_PASSWORD>@127.0.0.1:5442/<PG_DB>
    python -m alembic -c migration/alembic.ini upgrade head`
   (run from the engine repo so `supplier_rating_engine` imports resolve; the
   engine `Config` also reads `PG_HOST`/`PG_PORT` — when it defaults to the
   compose service name `postgres`, override `PG_HOST=127.0.0.1 PG_PORT=5442`
   in the same command). Then wipe + publish RV-0.7 there:
   `sra rules import --version 0.7` → approve → `sra rules publish`.
4. **Agent runtime**: clone `supplier_rating_agent_system` +
   `data_lookup_toolkit` into `www/projects/`, create a venv
   (`www/projects/.venvs/<runtime>`), `pip install -e` both, link
   `.agents/skills` per runtime, and give `sra` its own `.env` with
   `SRE_GATEWAY_URL=http://a2a-gateway:8765` + a dedicated agent account
   (`G-3`), then `scripts/seed_agents.py`.
5. **Workbench**: `.env.local` `GATEWAY_URL=http://localhost:8765`
   (the stack gateway publishes 8765 on the host), `SRA_WORKBENCH_URL=http://localhost:3000`
   for notification deep links. The local admin token is minted with
   `user_id` + `roles: ["qa-lead"]` (gateway `auth/jwt_local.py`, this repo's
   latest commit) so E-2 authorizes browser decisions.

## E2E flow (QA plan, all decisions in the workbench)

`/decisions` → rule-change (created by import) → approve → `sra rules publish`
→ verify → intake (CLI) → per-item evidence review (CLI) → submit via GUI
`/decisions/<uuid>` → score (CLI `--record`) → official-grade approvals via GUI
→ `record-approval` write-back → case report.

## Gated (reported as unavailable, never guessed)
MR-001~003 (NetSuite SQL views), A-7 reports, A-2/G-4 S3 evidence storage,
Cognito (uat/prod identity).