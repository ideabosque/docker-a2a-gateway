# Local backups (not tracked except this file)

Everything under `backups/` is gitignored: these artefacts are
environment-specific and may contain credentials. The portable artefact is
the logical dump; the copies of the live configuration stay local.

## `postgres/` — logical dumps (`pg_dump -Fc`)

| File | Source database | When |
|---|---|---|
| `silvaengine_20261020.dump` | `silvaengine` | 2026-10-20 |
| `silvaengine_20261021.dump` | `silvaengine` | 2026-10-21 |
| `silvaengine_restore2_<date>.dump` | `silvaengine_restore2` (the live A2A database) | after the 2026-11-10 recovery |
| `iog_e2e_scratch_<date>.dump` | `iog_e2e_scratch` | idem |

Restore into a fresh database:

```bash
docker exec -i a2a-postgres createdb -U silvaengine restore_check
docker exec -i a2a-postgres pg_restore -U silvaengine -d restore_check \
    --no-owner --no-privileges < backups/postgres/<file>.dump
```

Always verify a dump before relying on it — a restored copy must match the
source row counts (`a2a_agents`, `a2a_tasks`, `a2a_messages`,
`a2a_settings`).

## `env/` — copies of the deployment configuration

`docker-a2a-gateway.env.<date>` is a copy of the gitignored `.env`. It
holds credentials (admin token, database password, provider keys): keep it
out of git and out of shared storage.

## `postgres_data_pre_reset_<date>/` and `postgres_data_good_<date>/`

Physical copies of the database directory taken around a `pg_resetwal`
recovery:

* `pre_reset` — taken **before** the reset (the state that could not start);
* `good` — the known-good copy taken **after** the recovery.

Both are ignored by `postgres_data_*/` in `.gitignore`. They are a last-resort
safety net; the logical dump above is the portable artefact.
