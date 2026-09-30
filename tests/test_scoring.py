"""scoring 纯逻辑测试：乱序、严格递增、并列、缺点原因、撤销影响。"""
import unittest
from datetime import datetime, timezone, timedelta

from app.scoring import Checkpoint, Participant, Scan, compute

UTC = timezone.utc
T = lambda h, m=0: datetime(2026, 9, 29, h, m, tzinfo=UTC)  # noqa: E731


def base_fixture():
    cps = [
        Checkpoint("START", "起点", 1),
        Checkpoint("CP1", "1号点", 2),
        Checkpoint("CP2", "2号点", 3),
        Checkpoint("FINISH", "终点", 4),
    ]
    participants = [
        Participant("p1", "A001", "张三"),
        Participant("p2", "A002", "李四"),
        Participant("p3", "A003", "王五"),
    ]
    return cps, participants


class ScoringTests(unittest.TestCase):
    def test_out_of_order_upload_normalized(self):
        cps, participants = base_fixture()
        # 故意乱序录入
        scans = [
            Scan("m4", "p1", "FINISH", T(3)),
            Scan("m1", "p1", "START", T(1)),
            Scan("m3", "p1", "CP2", T(2, 30)),
            Scan("m2", "p1", "CP1", T(2)),
        ]
        result = compute(cps, participants, scans)
        finished = {f["participant"].id: f for f in result["finished"]}
        self.assertIn("p1", finished)
        # 首次有效过点按事件时间而非录入顺序
        self.assertEqual(
            [p.message_id for p in finished["p1"]["passes"]],
            ["m1", "m2", "m3", "m4"],
        )
        self.assertEqual(finished["p1"]["elapsed_seconds"], 2 * 3600)

    def test_strictly_increasing_required(self):
        cps, participants = base_fixture()
        scans = [
            Scan("s1", "p1", "START", T(1)),
            Scan("s2", "p1", "CP1", T(2)),
            # CP2 时间早于 CP1：该 CP2 无效
            Scan("s3", "p1", "CP2", T(1, 30)),
            Scan("s4", "p1", "FINISH", T(3)),
        ]
        result = compute(cps, participants, scans)
        dnf = {u["participant"].id: u for u in result["unfinished"]}
        self.assertNotIn("p1", {f["participant"].id for f in result["finished"]})
        self.assertIn("p1", dnf)
        self.assertEqual(dnf["p1"]["passed_count"], 2)
        self.assertEqual(dnf["p1"]["missing_checkpoint"], "CP2")
        self.assertIn("CP2", dnf["p1"]["reason"])

    def test_equal_times_are_not_strictly_increasing(self):
        cps, participants = base_fixture()
        scans = [
            Scan("s1", "p1", "START", T(1)),
            Scan("s2", "p1", "CP1", T(2)),
            Scan("s3", "p1", "CP2", T(2)),       # 与 CP1 同一刻 -> 忽略
            Scan("s4", "p1", "FINISH", T(3)),
        ]
        result = compute(cps, participants, scans)
        dnf = {u["participant"].id: u for u in result["unfinished"]}
        self.assertIn("p1", dnf)
        self.assertEqual(dnf["p1"]["missing_checkpoint"], "CP2")

    def test_duplicate_and_backtrack_scans_ignored(self):
        cps, participants = base_fixture()
        scans = [
            Scan("s1", "p1", "START", T(1)),
            Scan("s1-dup", "p1", "START", T(1, 5)),   # 起点重复
            Scan("s2", "p1", "CP1", T(2)),
            Scan("back", "p1", "START", T(2, 10)),    # 回溯
            Scan("s3", "p1", "CP2", T(2, 30)),
            Scan("s4", "p1", "FINISH", T(3)),
        ]
        result = compute(cps, participants, scans)
        finished = {f["participant"].id: f for f in result["finished"]}
        self.assertEqual(
            [p.checkpoint_code for p in finished["p1"]["passes"]],
            ["START", "CP1", "CP2", "FINISH"],
        )
        # 起点取首次
        self.assertEqual(finished["p1"]["passes"][0].message_id, "s1")

    def test_tied_elapsed_share_rank_competition_style(self):
        cps, participants = base_fixture()
        scans = []
        for pid, finish_offset in (("p1", 0), ("p2", 0), ("p3", 1)):
            scans += [
                Scan(f"{pid}-1", pid, "START", T(1)),
                Scan(f"{pid}-2", pid, "CP1", T(1, 30)),
                Scan(f"{pid}-3", pid, "CP2", T(2)),
                Scan(f"{pid}-4", pid, "FINISH",
                     T(3) + timedelta(seconds=finish_offset)),
            ]
        result = compute(cps, participants, scans)
        ranks = {f["participant"].id: f["rank"] for f in result["finished"]}
        # p1、p2 同耗时并列第 1，p3 第 3
        self.assertEqual(ranks["p1"], 1)
        self.assertEqual(ranks["p2"], 1)
        self.assertEqual(ranks["p3"], 3)

    def test_missing_start_reason(self):
        cps, participants = base_fixture()
        scans = [
            Scan("x2", "p1", "CP1", T(2)),
            Scan("x3", "p1", "CP2", T(2, 30)),
        ]
        result = compute(cps, participants, scans)
        dnf = {u["participant"].id: u for u in result["unfinished"]}
        self.assertEqual(dnf["p1"]["passed_count"], 0)
        self.assertEqual(dnf["p1"]["missing_checkpoint"], "START")

    def test_no_scans_at_all(self):
        cps, participants = base_fixture()
        result = compute(cps, participants, [])
        self.assertEqual(result["finished"], [])
        self.assertEqual(len(result["unfinished"]), 3)

    def test_ahead_scan_then_later_middle_scan_completes(self):
        # 先出现超前的 FINISH，随后中间点补齐，FINISH 的更晚扫描仍可被选
        cps, participants = base_fixture()
        scans = [
            Scan("s1", "p1", "START", T(1)),
            Scan("sf", "p1", "FINISH", T(1, 5)),   # 超前：忽略
            Scan("s2", "p1", "CP1", T(2)),
            Scan("s3", "p1", "CP2", T(2, 30)),
            Scan("s4", "p1", "FINISH", T(3)),      # 有效
        ]
        result = compute(cps, participants, scans)
        finished = {f["participant"].id: f for f in result["finished"]}
        self.assertIn("p1", finished)
        self.assertEqual(finished["p1"]["passes"][-1].message_id, "s4")


if __name__ == "__main__":
    unittest.main()
