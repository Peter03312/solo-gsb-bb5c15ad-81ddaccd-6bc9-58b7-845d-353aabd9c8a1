"""端到端集成测试：通过真实 HTTP API 验证全部业务规则。

运行前提：API 已启动（BASE_URL），并可通过 DATABASE_URL 直连数据库做测试前重置。
    BASE_URL=http://127.0.0.1:8080 \
    DATABASE_URL=postgresql://timer@127.0.0.1:55432/timing \
    python3 tests/integration_api.py
"""
import json
import os
import threading
import urllib.error
import urllib.request
import uuid

import psycopg

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8080")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://timer@127.0.0.1:55432/timing")

NS = uuid.UUID("12345678-1234-5678-1234-567856781234")
MID = lambda label: str(uuid.uuid5(NS, label))  # noqa: E731

_failures = []


def check(name: str, cond: bool, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def call(method: str, path: str, body=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read() or b"{}")
        if expect is not None and exc.code != expect:
            check(f"HTTP {method} {path} 期望 {expect} 实际 {exc.code}", False,
                  json.dumps(payload, ensure_ascii=False))
        return exc.code, payload


def reset_db():
    with psycopg.connect(DATABASE_URL, autocommit=True) as cx, cx.cursor() as cur:
        cur.execute(
            "TRUNCATE result_rows, result_versions, scans, participants, "
            "checkpoints, races RESTART IDENTITY CASCADE"
        )
        cur.execute("INSERT INTO races (id, name) VALUES (1, '集成测试赛')")


def main():
    reset_db()

    # ------------------------------------------------ 0. 健康检查
    status, body = call("GET", "/health")
    check("健康检查", status == 200 and body["status"] == "ok")

    # ------------------------------------------------ 1. 检查点顺序
    cps = [
        {"code": "START", "name": "起点", "position": 1},
        {"code": "CP1", "name": "1号站", "position": 2},
        {"code": "CP2", "name": "2号站", "position": 3},
        {"code": "FINISH", "name": "终点", "position": 4},
    ]
    status, body = call("PUT", "/checkpoints", {"checkpoints": cps})
    check("录入检查点顺序", status == 200 and len(body["checkpoints"]) == 4)

    status, _ = call("POST", "/checkpoints", {"code": "START", "name": "x"})
    check("重复检查点代码被拒绝", status == 409)

    # 顺序号校验
    bad = [
        {"code": "A", "name": "a", "position": 1},
        {"code": "B", "name": "b", "position": 1},
    ]
    # 已有扫描前才能 PUT；此时无扫描
    status, _ = call("PUT", "/checkpoints", {"checkpoints": bad})
    check("重复顺序号被拒绝", status == 422)
    # 恢复正确顺序
    call("PUT", "/checkpoints", {"checkpoints": cps})

    # ------------------------------------------------ 2. 参赛者
    participants = [
        {"id": "p1", "bib": "A001", "name": "张三"},
        {"id": "p2", "bib": "A002", "name": "李四"},
        {"id": "p3", "bib": "A003", "name": "王五"},
        {"id": "p4", "bib": "A004", "name": "赵六"},
    ]
    status, body = call("PUT", "/participants", {"participants": participants})
    check("批量录入参赛者", status == 200 and body["upserted"] == 4)

    # ------------------------------------------------ 3. 扫描：乱序/幂等/冲突/错误目标
    def put_scan(label, pid, cp, ts, expect=None):
        return call("PUT", f"/scans/{MID(label)}",
                    {"participant_id": pid, "checkpoint_code": cp,
                     "event_time": ts}, expect=expect)

    # p1 故意乱序：先传终点
    s, b = put_scan("p1-finish", "p1", "FINISH", "2026-09-29T04:00:00Z")
    check("乱序上传-先到终点", s == 201)
    put_scan("p1-start", "p1", "START", "2026-09-29T01:00:00Z")
    put_scan("p1-cp1", "p1", "CP1", "2026-09-29T02:00:00Z")
    put_scan("p1-cp2", "p1", "CP2", "2026-09-29T03:00:00Z")

    # p2：与 p1 同为 3 小时（01:10 -> 04:10）
    put_scan("p2-start", "p2", "START", "2026-09-29T01:10:00Z")
    put_scan("p2-cp1", "p2", "CP1", "2026-09-29T02:00:00Z")
    put_scan("p2-cp2", "p2", "CP2", "2026-09-29T02:30:00Z")
    put_scan("p2-finish", "p2", "FINISH", "2026-09-29T04:10:00Z")

    # p3：只到 CP1，应未完赛（缺 CP2）
    put_scan("p3-start", "p3", "START", "2026-09-29T01:05:00Z")
    put_scan("p3-cp1", "p3", "CP1", "2026-09-29T02:05:00Z")
    # 一条无效回溯/重复噪声扫描，不应影响结果
    put_scan("p3-noise", "p3", "START", "2026-09-29T05:00:00Z")
    # 非 UTC 带偏移时间应被接受并归一
    s, _ = put_scan("p3-noise2", "p3", "CP1", "2026-09-29T03:05:00+01:00")
    check("带偏移时间被接受", s == 201)

    # 幂等重传：同 ID 同内容
    s, b = put_scan("p1-start", "p1", "START", "2026-09-29T01:00:00Z")
    check("同ID同内容重传幂等", s == 200 and b["status"] == "idempotent")

    # 冲突重传：同 ID 不同内容
    s, b = put_scan("p1-start", "p1", "CP1", "2026-09-29T01:00:00Z", expect=409)
    check("同ID内容冲突拒绝", s == 409 and b["error"]["code"] == "message_conflict")

    # 未知参赛者 / 未知检查点
    s, b = put_scan("x1", "p-NOPE", "CP1", "2026-09-29T01:00:00Z", expect=404)
    check("未知参赛者明确报错", b["error"]["code"] == "unknown_participant")
    s, b = put_scan("x2", "p1", "CPX", "2026-09-29T01:00:00Z", expect=404)
    check("未知检查点明确报错", b["error"]["code"] == "unknown_checkpoint")

    # 不带时区的时间 → 422
    s, b = call("PUT", f"/scans/{MID('naive')}",
                {"participant_id": "p1", "checkpoint_code": "CP1",
                 "event_time": "2026-09-29 01:00:00"}, expect=422)
    check("无时区时间被拒绝", s == 422)

    # ------------------------------------------------ 4. 实时成绩：排名/并列/缺点原因
    s, results = call("GET", "/results")
    finished = {r["participant"]["id"]: r for r in results["finished"]}
    unfinished = {r["participant"]["id"]: r for r in results["unfinished"]}

    check("草稿未固化版本号为 null", results["version"] is None and results["frozen"] is False)
    check("p1 完赛且耗时3小时",
          finished.get("p1", {}).get("elapsed_seconds") == 10800.0,
          json.dumps(finished.get("p1"), ensure_ascii=False))
    check("p1 首次有效过点依据4条且顺序正确",
          [p["checkpoint_code"] for p in finished.get("p1", {}).get("passes", [])]
          == ["START", "CP1", "CP2", "FINISH"])
    check("p1 起点依据为首条报文（乱序归一）",
          finished["p1"]["passes"][0]["message_id"] == MID("p1-start"))
    check("同耗时并列第1（p1）", finished["p1"]["rank"] == 1)
    check("同耗时并列第1（p2）", finished["p2"]["rank"] == 1)
    check("p3 未完赛且原因指向缺失 CP2",
          unfinished.get("p3", {}).get("missing_checkpoint") == "CP2"
          and "CP2" in unfinished["p3"]["reason"])
    check("p3 的噪声回溯扫描未被选入",
          all(p["message_id"] != MID("p3-noise")
              for p in unfinished.get("p3", {}).get("passes", [])))
    check("p4 无任何记录列出未完赛",
          unfinished.get("p4", {}).get("missing_checkpoint") == "START")

    # ------------------------------------------------ 5. 发布 v1 并固化
    s, v1 = call("POST", "/results/publish")
    check("发布 v1", s == 200 and v1["version"] == 1 and v1["frozen"] is True)
    v1_p1 = {r["participant"]["id"]: r for r in v1["finished"]}["p1"]
    check("v1 中 p1 完赛", v1_p1["rank"] == 1)

    # ------------------------------------------------ 6. 撤销 + 迟到记录：只影响下一版
    s, b = call("POST", f"/scans/{MID('p1-cp2')}/revoke")
    check("撤销 p1 的 CP2 扫描", s == 200 and b["revoked"] is True)
    s, b = call("POST", f"/scans/{MID('p1-cp2')}/revoke")
    check("重复撤销幂等", s == 200)
    s, b = call("POST", f"/scans/{uuid.uuid4()}/revoke", expect=404)
    check("撤销未知目标明确报错", b["error"]["code"] == "scan_not_found")

    # 已撤销报文不能以同 ID 重传
    s, b = put_scan("p1-cp2", "p1", "CP2", "2026-09-29T03:00:00Z", expect=409)
    check("已撤销ID重传被拒绝", b["error"]["code"] == "message_revoked")

    s, draft = call("GET", "/results")
    d_unfinished = {r["participant"]["id"]: r for r in draft["unfinished"]}
    check("撤销后草稿 p1 变为未完赛",
          "p1" in d_unfinished and d_unfinished["p1"]["missing_checkpoint"] == "CP2")

    # 迟到记录：p4 完整轨迹，2.5 小时，比所有人快
    for label, cp, ts in (
        ("p4-start", "START", "2026-09-29T00:30:00Z"),
        ("p4-cp1", "CP1", "2026-09-29T01:00:00Z"),
        ("p4-cp2", "CP2", "2026-09-29T01:30:00Z"),
        ("p4-finish", "FINISH", "2026-09-29T03:00:00Z"),
    ):
        put_scan(label, "p4", cp, ts)

    # 旧版 v1 不受撤销/迟到影响
    s, old = call("GET", "/results/versions/1")
    old_finished = {r["participant"]["id"]: r for r in old["finished"]}
    check("旧版 v1 仍可查询且 p1 仍完赛（已固化）",
          "p1" in old_finished and old_finished["p1"]["elapsed_seconds"] == 10800.0)
    check("旧版 v1 不含迟到的 p4", "p4" not in old_finished)
    check("v1 带发布时间", "published_at" in old)

    s, missing = call("GET", "/results/versions/99", expect=404)
    check("查询不存在版本报 404", missing["error"]["code"] == "version_not_found")

    # 发布 v2
    s, v2 = call("POST", "/results/publish")
    v2_finished = {r["participant"]["id"]: r for r in v2["finished"]}
    v2_unfinished = {r["participant"]["id"]: r for r in v2["unfinished"]}
    check("发布 v2（版本连续）", v2["version"] == 2)
    check("v2 迟到者 p4 第1", v2_finished.get("p4", {}).get("rank") == 1)
    check("v2 p4 耗时2.5小时", v2_finished["p4"]["elapsed_seconds"] == 9000.0)
    check("v2 p2 第2", v2_finished.get("p2", {}).get("rank") == 2)
    check("v2 被撤销依据的 p1 未完赛",
          "p1" in v2_unfinished and "CP2" in v2_unfinished["p1"]["reason"])
    check("v2 固化的依据记录完整",
          [p["checkpoint_code"] for p in v2_finished["p4"]["passes"]]
          == ["START", "CP1", "CP2", "FINISH"])

    s, versions = call("GET", "/results/versions")
    vnums = [v["version"] for v in versions["versions"]]
    check("版本列表连续 1,2", vnums == [1, 2], str(vnums))

    # ------------------------------------------------ 7. 并发发布：连续且各自一致
    results_concurrent = []
    errors = []

    def publish():
        try:
            s, b = call("POST", "/results/publish")
            results_concurrent.append((s, b.get("version")))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=publish) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("并发发布无异常", not errors, str(errors))
    ok_versions = sorted(v for s, v in results_concurrent if s == 200)
    check("8 个并发发布全部成功",
          len(ok_versions) == 8, str(results_concurrent))
    check("并发形成连续版本 3..10",
          ok_versions == list(range(3, 11)), str(ok_versions))
    check("并发版本号无重复", len(set(ok_versions)) == 8)
    # 所有发布事务结束后再逐个查询：每个版本均可读取、内容自洽且已固化
    consistent = True
    for n in ok_versions:
        s, b = call("GET", f"/results/versions/{n}")
        if s != 200 or b["version"] != n or b["frozen"] is not True:
            consistent = False
    check("每个并发版本均可独立查询且已固化", consistent)
    s, versions = call("GET", "/results/versions")
    check("最终版本列表 1..10 连续无空洞",
          [v["version"] for v in versions["versions"]] == list(range(1, 11)))

    # ------------------------------------------------ 8. 批量上传（逐条独立结果）
    batch = {
        "scans": [
            {"message_id": str(uuid.uuid4()), "participant_id": "p3",
             "checkpoint_code": "CP2", "event_time": "2026-09-29T03:05:00Z"},
            {"message_id": str(uuid.uuid4()), "participant_id": "p-NO",
             "checkpoint_code": "CP2", "event_time": "2026-09-29T03:05:00Z"},
        ]
    }
    s, b = call("POST", "/scans/batch", batch)
    check("批量上传逐条独立（1 成功 1 拒绝）",
          b["created"] == 1 and b["rejected"] == 1
          and b["results"][1]["error"]["code"] == "unknown_participant",
          json.dumps(b, ensure_ascii=False))

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项: {_failures}")
        raise SystemExit(1)
    print("全部集成测试通过 ✔")


if __name__ == "__main__":
    main()
