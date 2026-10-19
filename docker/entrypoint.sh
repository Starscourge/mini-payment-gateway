#!/bin/sh
# Runs before the container's command. The api service sets RUN_MIGRATIONS=1 so the schema is
# current before it takes traffic; the worker and mock bank share the image and skip this.
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    alembic upgrade head
fi

exec "$@"
