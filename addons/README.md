# Addon modules (drop-in)

Drop a `*.yaml` (or `*.yml`) file in this folder to register an additional
`silvaengine_gateway` module — without touching `../routes.yaml`.

At container startup, `scripts/merge_addon_routes.py` scans this folder and
merges every file it finds into `data/_addons_generated.yaml`, which
`../routes.yaml` includes via a single permanent line:

```yaml
- !include data/_addons_generated.yaml
```

Edit or add files here, then restart the container — no image rebuild, and
no edits to `routes.yaml`:

```bash
docker compose restart a2a-gateway
# or: make restart
```

## File format

Each file must contain either **one module map**, or a **YAML list of module
maps** — the exact same shape as an entry in `routes.yaml`'s `modules:` list.
See `example.module.yaml.disabled` in this folder, or `a2a_daemon_engine.yaml`
(this image's own core module — registered the same drop-in way as everything
else here), for the full field reference.

## Naming rules

- `*.yaml` / `*.yml` files are picked up; anything else is ignored.
- Files starting with `_` are reserved (that's where the generated output
  lives) and are skipped.
- Files ending in `.example`, `.disabled`, or `.bak` are skipped — handy for
  keeping a template or a temporarily-disabled module in this folder without
  it being loaded.
- `*.local.yaml` / `*.local.yml` ARE loaded (they're active addon config) but
  are gitignored — use this for a personal or work-in-progress addon (e.g.
  one whose package isn't in `requirements.txt` yet) that you don't want to
  commit. Shared addon config is a plain `<name>.yaml` (tracked normally,
  same as `routes.yaml` itself) — once your local addon is ready to share,
  just rename it, dropping `.local`.

## A module that fails to import doesn't break the gateway

If a module's `package` (or a route's `dispatch`) can't actually be imported
(e.g. its code isn't installed yet), the gateway logs a warning/error for
that module or route and skips it — it does not crash the whole gateway. A2A
keeps working regardless. Check
`docker exec a2a-gateway cat /var/log/supervisor/silvaengine-gateway.log`
if an addon doesn't seem to register.

## Prerequisites

Registering routes here only wires up the HTTP surface. The module's Python
package still needs to be **importable** inside the container: add it to
`../requirements-modules.txt` as a `--no-deps` git install, its real deps to
`../requirements.txt`, then `docker compose build`.

## Example

```yaml
# addons/my_engine.yaml
name: my_engine
package: my_engine
transport: graphql
config_class: "my_engine.handlers.config:Config"
config_init_style: dict
routes:
  - path: "/{endpoint_id}/my_engine_graphql"
    handler_type: graphql
    dispatch: "my_engine.main:dispatch_graphql"
    methods: ["POST"]
    auth: true
```
