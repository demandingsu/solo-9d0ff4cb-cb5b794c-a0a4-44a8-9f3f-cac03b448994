#!/bin/sh
set -e

# 启动前先确保表结构与初始口径就绪（create_app 内也有幂等初始化，双保险）。
python - <<'PY'
from hydrology.db import init_database, resolve_database_path
init_database(resolve_database_path())
print("[entrypoint] database ready at", resolve_database_path())
PY

exec "$@"
