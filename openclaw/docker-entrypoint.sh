#!/bin/sh
# Runs once at container start, before openclaw/supervisord.
#
# The repo-root deploy key (see ../docker-compose.yml SSH_HOST_DIR) is
# bind-mounted read-only at /root/.ssh-src so OpenClaw's own git operations
# can share the same key as the main gateway image without it ever being
# baked into this image. It can't be used from there directly: a read-only
# mount can't be chmod'd in place, and OpenSSH refuses key files that are
# group/world-readable — a real risk on a Windows host, where NTFS
# permissions don't map cleanly to POSIX modes through Docker's bind mount.
#
# So: copy it into a normal, writable, container-owned ~/.ssh and fix
# permissions before anything else runs.
set -e

if [ -d /root/.ssh-src ] && [ "$(ls -A /root/.ssh-src 2>/dev/null)" ]; then
    mkdir -p /root/.ssh
    cp -f /root/.ssh-src/* /root/.ssh/ 2>/dev/null || true
    chmod 700 /root/.ssh
    chmod 600 /root/.ssh/* 2>/dev/null || true
fi

exec "$@"
