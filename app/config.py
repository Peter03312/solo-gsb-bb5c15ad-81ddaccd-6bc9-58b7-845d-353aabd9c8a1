"""运行期配置：全部来自环境变量，均可通过 Compose 覆盖。"""
import os


def _required(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"缺少必需环境变量: {name}")
    return value


# libpq 连接串，例如 postgresql://timer:timer@db:5432/timing
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://timer:timer@localhost:5432/timing"
)

# 本服务为单赛事设计；赛事行由 db/schema.sql 初始化，ID 固定为 1
RACE_ID = int(os.getenv("RACE_ID", "1"))

# uvicorn 监听地址/端口（compose 与 Dockerfile 之外的直接运行场景使用）
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
