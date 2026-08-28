#!/bin/sh
# Runs once at container start, before supervisord/the gateway. Merges any
# drop-in addon module manifests under ./addons/ into the single file that
# routes.yaml's permanent `!include data/_addons_generated.yaml` line points
# at — see scripts/merge_addon_routes.py and addons/README.md.
#
# This is the ONLY place that decides how the container starts — do not add
# a `command:` override in docker-compose.yml, or it will silently bypass
# this script (that exact mistake broke this once already).
set -e

/opt/venv/bin/python /app/scripts/merge_addon_routes.py

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
