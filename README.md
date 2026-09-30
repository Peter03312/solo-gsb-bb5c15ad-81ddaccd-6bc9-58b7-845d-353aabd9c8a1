# 越野赛计时核算服务（Trail Race Timing Service）

面向线下越野赛的计时核算后端：录入赛事检查点顺序与参赛者，接收含唯一报文 ID 的离线扫描记录，按规则计算成绩，并支持成绩版本的发布、冻结与回溯查询。

- 技术栈：Python 3.11 · FastAPI · SQLAlchemy 2 · PostgreSQL 16
- 服务地址：`http://localhost:8000`（交互式文档 `http://localhost:8000/docs`）
- 数据库：`localhost:5432`（Compose 内部网络主机名为 `db`）

## 一键启动

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 与应用两个容器：应用等待数据库健康检查后自动建表初始化，无需手工迁移。首次启动后即可访问 `http://localhost:8000/health`。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://timing:timing@db:5432/timing`（Compose 内） | SQLAlchemy 数据库连接串 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库的重试次数 |
| `DB_CONNECT_RETRY_DELAY_SECONDS` | `1` | 每次重试间隔（秒） |

端口映射：应用 `8000:8000`，数据库 `5432:5432`（见 `docker-compose.yml`）。

## 计时与排名规则

- 检查点按顺序定义，**第一个为起点、最后一个为终点**。
- 每位参赛者按检查点顺序依次选取"首次有效过点"：在该检查点所有扫描中，取事件时间**严格晚于**上一已接受过点的最早一条；找不到即未完赛（DNF），并给出原因（缺某个检查点 / 该点无严格晚于前一点的记录 / 无任何扫描）。
- 完赛者按**起点到终点耗时**升序排名；耗时相同者**并列同一名次**（竞赛排名：1、2、2、4）。
- 扫描记录可**乱序上传**；以 `message_id` 幂等：同 ID 同内容重传返回原记录（200），同 ID 不同内容拒绝（409 `scan_conflict`）。
- 撤销扫描后实时成绩立即重算；**已发布版本不受影响**。发布即固化名次与依据记录（各检查点所采纳的扫描报文），此后迟到的记录或撤销只进入下一版；历史版本永久可查。
- 并发发布通过赛事行级锁（`SELECT ... FOR UPDATE`）串行化：版本号严格连续（1、2、3…），且每个版本各自一致。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/races` | 创建赛事 |
| GET | `/races/{race_id}` | 赛事详情（含检查点顺序） |
| PUT | `/races/{race_id}/checkpoints` | 整体设置有序检查点（首=起点，末=终点） |
| POST | `/races/{race_id}/participants` | 报名（同号码簿同姓名幂等） |
| GET | `/races/{race_id}/participants` | 参赛者列表 |
| POST | `/races/{race_id}/scans` | 上传单条扫描记录（幂等/冲突检测） |
| POST | `/races/{race_id}/scans/batch` | 批量上传（逐条独立处理，返回逐条结果） |
| GET | `/races/{race_id}/scans` | 扫描记录列表（含撤销状态） |
| POST | `/races/{race_id}/scans/{message_id}/revoke` | 撤销指定扫描 |
| GET | `/races/{race_id}/standings` | 实时成绩（未发布，按当前有效扫描现算） |
| POST | `/races/{race_id}/results/publish` | 发布成绩，生成连续版本号并冻结 |
| GET | `/races/{race_id}/results` | 最新已发布版本 |
| GET | `/races/{race_id}/results/versions` | 版本列表 |
| GET | `/races/{race_id}/results/versions/{n}` | 查询历史版本 |
| GET | `/health` | 健康检查 |

错误统一为 `{"error": {"code": "...", "message": "..."}}`。主要错误码：`race_not_found`、`participant_not_found`、`checkpoint_not_found`、`scan_not_found`、`scan_conflict`、`participant_conflict`、`checkpoint_in_use`、`version_not_found`、`no_published_results`、`validation_error`。

## 调用示例（curl）

```bash
BASE=http://localhost:8000

# 1. 创建赛事
curl -s -X POST $BASE/races -H 'Content-Type: application/json' \
  -d '{"name": "Qingfeng Trail 50K"}'
# => {"id":1,"name":"Qingfeng Trail 50K",...}

# 2. 设置有序检查点（首=起点，末=终点）
curl -s -X PUT $BASE/races/1/checkpoints -H 'Content-Type: application/json' \
  -d '{"checkpoints":[{"code":"START"},{"code":"CP1"},{"code":"CP2"},{"code":"FINISH"}]}'

# 3. 报名
curl -s -X POST $BASE/races/1/participants -H 'Content-Type: application/json' \
  -d '{"bib":"001","name":"Alice"}'
curl -s -X POST $BASE/races/1/participants -H 'Content-Type: application/json' \
  -d '{"bib":"002","name":"Bob"}'

# 4. 上传扫描记录（可乱序；event_time 须带时区，建议 UTC）
curl -s -X POST $BASE/races/1/scans -H 'Content-Type: application/json' -d '{
  "message_id": "msg-001-fin",
  "participant_bib": "001",
  "checkpoint_code": "FINISH",
  "event_time": "2026-09-30T11:02:00Z"
}'
# => 201 {"status":"created","scan":{...}}

# 同 ID 同内容重传：幂等
# => 200 {"status":"duplicate",...}
# 同 ID 不同内容：拒绝
# => 409 {"error":{"code":"scan_conflict",...}}

# 批量上传（离线设备补传）
curl -s -X POST $BASE/races/1/scans/batch -H 'Content-Type: application/json' -d '{
  "scans": [
    {"message_id":"msg-001-start","participant_bib":"001","checkpoint_code":"START","event_time":"2026-09-30T10:00:00Z"},
    {"message_id":"msg-001-cp1","participant_bib":"001","checkpoint_code":"CP1","event_time":"2026-09-30T10:20:00Z"},
    {"message_id":"msg-001-cp2","participant_bib":"001","checkpoint_code":"CP2","event_time":"2026-09-30T10:45:00Z"}
  ]
}'

# 5. 实时成绩（含并列名次与未完赛原因）
curl -s $BASE/races/1/standings
# entries 示例：
# {"rank":1,"bib":"002","status":"finished","elapsed_ms":3540000,"reason":null,"basis":[...]}
# {"rank":2,"bib":"001","status":"finished","elapsed_ms":3720000,...}
# {"rank":null,"bib":"003","status":"dnf","reason":"missing checkpoint 'CP2'",...}

# 6. 撤销某条扫描（实时成绩随之重算）
curl -s -X POST $BASE/races/1/scans/msg-001-cp1/revoke

# 7. 发布成绩：固化名次与依据记录，版本号连续递增
curl -s -X POST $BASE/races/1/results/publish
# => 201 {"race_id":1,"version_no":1,"published_at":"...","entries":[...]}

# 8. 迟到记录/撤销只影响下一版；历史版本仍可查
curl -s $BASE/races/1/results                 # 最新版
curl -s $BASE/races/1/results/versions        # 版本列表
curl -s $BASE/races/1/results/versions/1      # 第 1 版（冻结内容不变）
```

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx pgserver   # 测试依赖（pgserver 提供用户态 PostgreSQL）
python3 -m pytest tests/ -q
```

测试使用真实 PostgreSQL（pgserver 在用户态拉起，无需 Docker/root），覆盖：乱序上传、幂等重传、内容冲突、严格递增选点、并列排名、未完赛原因、撤销重算、版本冻结与回溯、并发发布版本连续性。

## 项目结构

```
app/
  main.py       # FastAPI 入口、统一错误处理、启动初始化
  routes.py     # HTTP 路由
  services.py   # 业务逻辑：扫描幂等/冲突、撤销、发布（行锁串行化）
  timing.py     # 纯函数计时核心：首次有效过点选取、排名、DNF 原因
  models.py     # SQLAlchemy 模型（scans / result_versions / result_entries ...）
  schemas.py    # Pydantic 请求/响应模型
  db.py         # 引擎、会话、重试建表
  config.py     # 环境变量配置
tests/          # 端到端测试（真实 PostgreSQL）
Dockerfile      # 应用镜像
docker-compose.yml
```
