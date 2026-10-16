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
4. **Agent runtime (Goose, done 2026-10-16)**:
   - `git clone` `supplier_rating_agent_system` and `data_lookup_toolkit` into
     `www/projects/` (git-ignored here; clones carry no `.env` or `data/`).
   - The `goose` service mounts `PROJECTS_FOLDER` at `/workspace/projects` and puts
     `/workspace/projects/.venvs/goose/bin` first on `PATH`; the image has
     `python3-venv`. In the container:
     `python3 -m venv /workspace/projects/.venvs/goose` and
     `pip install -e ./data_lookup_toolkit -e ./supplier_rating_agent_system`.
   - `GOOSE_CWD=/workspace/projects/supplier_rating_agent_system`: goose 1.53 loads
     `.agents/skills/` from its working directory (and `~/.agents/skills/` globally),
     so the repo's skills need no copy or link.
   - Agent account `sra-agent` (role `agent`) in `data/sra-users.json`
     (`LOCAL_USER_FILE=/app/data/sra-users.json`); its password lives only in
     `www/projects/supplier_rating_agent_system/.env` (`SRE_GATEWAY_URL=http://a2a-gateway:8765`).
     Needs the silvaengine_gateway fixes (users-file tokens carry `user_id`;
     bcrypt checked directly).
   - `GOOSE_PERMISSION_MODE=ask`, so every command Goose wants to run waits for an
     approval; the workbench chat cannot answer approvals yet.
   - After editing `.env`, recreate the service (`docker compose up -d --no-deps <svc>`);
     `docker restart` keeps the old environment.
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