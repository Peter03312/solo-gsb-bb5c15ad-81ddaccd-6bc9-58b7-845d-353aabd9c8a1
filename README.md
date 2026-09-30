# 线下越野赛计时核算服务

基于 **Python 3.11 + FastAPI + PostgreSQL** 的越野赛计时核算 HTTP 服务。
支持离线扫描报文乱序上传、报文幂等与冲突拒绝、检查点顺序核算、
起终点耗时排名（同耗时并列）、未完赛原因、撤销重算，以及成绩版本的
固化发布（迟到记录与撤销只影响下一版，旧版永久可查；并发发布版本连续一致）。

---

## 1. 一键启动（Docker Compose）

前置条件：已安装 Docker 与 Docker Compose v2。

```bash
cd /workspace
docker compose up --build -d
```

启动后：

- API 基地址：<http://localhost:8080>
- 交互式文档（Swagger UI）：<http://localhost:8080/docs>
- OpenAPI 描述：<http://localhost:8080/openapi.json>
- 健康检查：<http://localhost:8080/health>
- PostgreSQL：容器内 `db:5432`，宿主机默认映射到 `localhost:5432`

容器启动顺序由 healthcheck 保证：API 会等待数据库就绪，然后在启动时
**幂等执行 `db/schema.sql`** 自动建表并初始化默认赛事（`id=1`），
无需手工初始化。首次启动即可直接调用 API。

停止与清理：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时删除数据库数据卷（彻底重置）
```

---

## 2. 配置（环境变量）

所有配置都有默认值，可在项目根目录创建 `.env` 文件覆盖
（可参考 `.env.example`，`docker compose` 会自动读取）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8080` | API 映射到宿主机的端口（容器内固定 8080） |
| `DB_PORT` | `5432` | PostgreSQL 映射到宿主机的端口 |
| `POSTGRES_USER` | `timer` | 数据库用户名 |
| `POSTGRES_PASSWORD` | `timer` | 数据库密码 |
| `POSTGRES_DB` | `timing` | 数据库名 |
| `RACE_ID` | `1` | 固定赛事 ID（服务为单赛事设计） |
| `DATABASE_URL` | `postgresql://timer:timer@db:5432/timing` | 应用使用的 libpq 连接串（非容器直跑时可覆盖） |

### 不用 Docker，本地直接运行

```bash
python3 -m pip install -r requirements.txt
# 准备一个空 PostgreSQL 库后：
export DATABASE_URL="postgresql://timer:timer@localhost:5432/timing"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080
# 表结构同样在启动时自动初始化
```

---

## 3. 核算规则

1. **首次有效过点**：扫描按事件时间（UTC）升序排序，从第 1 个检查点
   （起点）开始，依次要求“检查点顺序一致 + 事件时间严格递增”；
   每个检查点取满足条件的**第一次**过点。乱序/超前/回溯/重复/
   同刻扫描均被忽略，不影响已确认过点。
2. **完赛与排名**：取得全部检查点（含终点）有效过点即为完赛，
   成绩 = 终点事件时间 − 起点事件时间；按耗时升序做标准竞赛排名
   （同耗时并列，例如两人并列第 1，下一人为第 3）。
3. **未完赛原因**：列出当前已确认的最后过点与第一个缺失的检查点，
   例如 `缺少检查点 CP2（2号站，第 3 位）的有效过点记录；
   最后有效过点为 CP1 于 …`。
4. **报文语义**：`message_id`（UUID）全局唯一。同 ID 同内容重传返回
   `200 idempotent`；同 ID 内容不同返回 `409 message_conflict`；
   未知参赛者/检查点返回 404；事件时间必须带时区（服务端统一转 UTC）。
5. **撤销**：`POST /scans/{id}/revoke` 将报文标记撤销（重复撤销幂等），
   之后草稿与下一版立即重算；撤销不存在的 ID 返回 404。已撤销的
   报文 ID 不能再次写入（需用新 ID）。
6. **版本固化**：发布把当时的检查点顺序、名次、耗时及每条依据过点
   （报文 ID + 事件时间）完整存入新版本 JSONB 快照。之后新增迟到记录
   或撤销**只影响草稿与下一版**，旧版内容不变且可随时查询。
7. **并发发布**：发布事务对赛事行加 `FOR UPDATE` 行锁串行化，
   版本号在同一事务快照内 `max(version)+1` 后连快照一起提交，
   因此并发发布会形成 `1,2,3,…` **连续无空洞**且各自自洽的版本。

---

## 4. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（含数据库连通性） |
| GET | `/checkpoints` | 查看检查点顺序 |
| POST | `/checkpoints` | 追加/插入一个检查点（省略 `position` 则追加末尾） |
| PUT | `/checkpoints` | 整体替换检查点顺序（已有生效扫描时拒绝） |
| GET | `/participants` | 参赛者列表 |
| POST | `/participants` | 新增参赛者（重复返回 409） |
| PUT | `/participants` | 批量录入/更新参赛者（按 id upsert） |
| PUT | `/scans/{message_id}` | 录入一条扫描；重传幂等、冲突 409 |
| POST | `/scans/batch` | 批量录入（逐条独立返回结果） |
| POST | `/scans/{message_id}/revoke` | 撤销扫描并使后续核算重算 |
| GET | `/scans` | 扫描列表（可按 `revoked`、`participant_id` 过滤） |
| GET | `/results` | 实时草稿成绩（未固化，`version=null`） |
| POST | `/results/publish` | 固化发布一个新版本 |
| GET | `/results/versions` | 已发布版本清单 |
| GET | `/results/versions/{version}` | 查询指定历史版本（固化内容） |

错误响应统一为：

```json
{ "error": { "code": "message_conflict", "message": "报文 ID ... 已存在但内容不同" } }
```

---

## 5. 调用示例（curl）

下例构造一场 4 检查点赛事：张三完赛 3 小时、李四 3 小时（并列）、
王五只到 CP1（未完赛），演示乱序上传、幂等、撤销、发布与版本查询。
报文 ID 使用固定 UUID 以便重传；真实系统由采集设备生成并保证全局唯一。

```bash
BASE=http://localhost:8080

# 5.1 检查点顺序（position 从 1 开始，1=起点）
curl -s -X PUT $BASE/checkpoints -H 'Content-Type: application/json' -d '{
  "checkpoints": [
    {"code": "START",  "name": "起点",   "position": 1},
    {"code": "CP1",    "name": "1号站",  "position": 2},
    {"code": "CP2",    "name": "2号站",  "position": 3},
    {"code": "FINISH", "name": "终点",   "position": 4}
  ]
}'

# 5.2 参赛者（可批量）
curl -s -X PUT $BASE/participants -H 'Content-Type: application/json' -d '{
  "participants": [
    {"id": "p1", "bib": "A001", "name": "张三"},
    {"id": "p2", "bib": "A002", "name": "李四"},
    {"id": "p3", "bib": "A003", "name": "王五"}
  ]
}'

# 5.3 扫描报文（演示张三的“终点”先于其他点到达，即乱序上传）
curl -s -X PUT $BASE/scans/10000000-0000-4000-8000-000000000001 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p1","checkpoint_code":"FINISH","event_time":"2026-09-29T04:00:00Z"}'
# -> 201 {"status":"created", ...}

curl -s -X PUT $BASE/scans/10000000-0000-4000-8000-000000000002 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p1","checkpoint_code":"START","event_time":"2026-09-29T01:00:00Z"}'
curl -s -X PUT $BASE/scans/10000000-0000-4000-8000-000000000003 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p1","checkpoint_code":"CP1","event_time":"2026-09-29T02:00:00Z"}'
curl -s -X PUT $BASE/scans/10000000-0000-4000-8000-000000000004 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p1","checkpoint_code":"CP2","event_time":"2026-09-29T03:00:00Z"}'

# 李四同样 3 小时（01:10 -> 04:10）
curl -s -X PUT $BASE/scans/20000000-0000-4000-8000-000000000001 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p2","checkpoint_code":"START","event_time":"2026-09-29T01:10:00Z"}'
curl -s -X PUT $BASE/scans/20000000-0000-4000-8000-000000000002 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p2","checkpoint_code":"CP1","event_time":"2026-09-29T02:00:00Z"}'
curl -s -X PUT $BASE/scans/20000000-0000-4000-8000-000000000003 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p2","checkpoint_code":"CP2","event_time":"2026-09-29T02:30:00Z"}'
curl -s -X PUT $BASE/scans/20000000-0000-4000-8000-000000000004 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p2","checkpoint_code":"FINISH","event_time":"2026-09-29T04:10:00Z"}'

# 王五只经过起点与 CP1（将被判未完赛，原因：缺少 CP2）
curl -s -X PUT $BASE/scans/30000000-0000-4000-8000-000000000001 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p3","checkpoint_code":"START","event_time":"2026-09-29T01:05:00Z"}'
curl -s -X PUT $BASE/scans/30000000-0000-4000-8000-000000000002 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p3","checkpoint_code":"CP1","event_time":"2026-09-29T02:05:00Z"}'

# 5.4 幂等重传（同 ID 同内容）-> 200 {"status":"idempotent"}
curl -s -X PUT $BASE/scans/10000000-0000-4000-8000-000000000002 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p1","checkpoint_code":"START","event_time":"2026-09-29T01:00:00Z"}'

# 5.5 冲突重传（同 ID 改内容）-> 409 message_conflict
curl -i -X PUT $BASE/scans/10000000-0000-4000-8000-000000000002 \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"p1","checkpoint_code":"CP1","event_time":"2026-09-29T01:00:00Z"}'

# 5.6 实时草稿成绩（张三、李四并列第 1；王五在 unfinished 中给出原因）
curl -s $BASE/results | python3 -m json.tool

# 5.7 发布第 1 版（名次与依据报文一并固化）
curl -s -X POST $BASE/results/publish | python3 -m json.tool

# 5.8 撤销张三的 CP2 报文后重算：草稿中张三变为未完赛
curl -s -X POST $BASE/scans/10000000-0000-4000-8000-000000000004/revoke
curl -s $BASE/results | python3 -m json.tool

# 5.9 撤销只影响下一版：旧版 v1 仍显示张三完赛
curl -s $BASE/results/versions/1 | python3 -m json.tool
# 发布第 2 版后，v2 中张三未完赛，v1 保持不变
curl -s -X POST $BASE/results/publish | python3 -m json.tool
curl -s $BASE/results/versions | python3 -m json.tool
```

### Python 调用示例（标准库，无需第三方包）

```python
import json, urllib.request

BASE = "http://localhost:8080"

def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.load(resp)

status, _ = api("PUT", "/scans/40000000-0000-4000-8000-000000000001",
                {"participant_id": "p3", "checkpoint_code": "CP2",
                 "event_time": "2026-09-29T03:05:00Z"})  # 迟到补点
print(status)  # 201

_, v3 = api("POST", "/results/publish")                  # 第 3 版：王五可能完赛
print(v3["version"], [(f["rank"], f["participant"]["bib"]) for f in v3["finished"]])
```

---

## 6. 项目结构与测试

```
.
├── app/
│   ├── main.py         # FastAPI 路由与事务边界
│   ├── scoring.py      # 纯核算逻辑（选点/排名/未完赛原因），无 IO 依赖
│   ├── db.py           # 连接池、启动建表、快照读写
│   ├── repository.py   # 检查点/参赛者/扫描数据访问与校验
│   ├── schemas.py      # 请求模型与时间校验
│   ├── config.py       # 环境变量配置
│   └── errors.py       # 统一业务错误
├── db/schema.sql       # 幂等建表 + 默认赛事初始化
├── tests/
│   ├── test_scoring.py       # 纯逻辑单元测试（8 个场景）
│   └── integration_api.py    # 对真实 HTTP+PostgreSQL 的端到端测试
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh       # 等待数据库 -> 启动 API（建表自动完成）
└── requirements.txt
```

运行测试：

```bash
python3 -m unittest tests.test_scoring          # 核算纯逻辑
# 端到端（需已启动 API 与空数据库）：
BASE_URL=http://localhost:8080 \
DATABASE_URL=postgresql://timer:timer@localhost:5432/timing \
python3 tests/integration_api.py
```

集成测试覆盖：乱序归一、严格递增/同刻拒绝、并列名次、缺点原因、
幂等、冲突、未知目标 404、撤销重算、已撤销 ID 拒绝、迟到记录只影响
下一版、旧版可查、8 路并发发布形成连续版本 1..10。
