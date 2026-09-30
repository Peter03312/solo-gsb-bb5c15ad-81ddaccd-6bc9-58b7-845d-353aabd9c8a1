#!/bin/sh
# 等待 PostgreSQL 可达后启动 API。建表/初始化由 app.main 启动时
# 幂等执行 db/schema.sql 完成。
set -e

python - <<'PY'
import os, time, psycopg

url = os.environ["DATABASE_URL"]
for i in range(60):
    try:
        with psycopg.connect(url, connect_timeout=3) as cx, cx.cursor() as cur:
            cur.execute("SELECT 1")
        print("database is ready")
        break
    except Exception as exc:
        print(f"waiting for database ({i + 1}/60): {exc}")
        time.sleep(2)
else:
    raise SystemExit("database not reachable")
PY

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
