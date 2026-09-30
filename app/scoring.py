"""成绩核算纯逻辑（无数据库依赖，便于测试）。

选点规则（按题目要求）：
  扫描可乱序到达；按事件时间排序后，从起点检查点开始，
  依次要求检查点顺序一致、相邻事件时间严格递增；
  每个检查点取满足条件的首次过点。不满足条件的扫描（中间乱点、
  回溯点、同时间重复点）一律忽略，不影响此前已确认的过点；
  因缺失而无法继续时，列出明确的未完赛原因。
排名规则：
  完赛者按起终点耗时升序，标准竞赛排名（同耗时并列，名次不跳号
  的并列形式：并列者同名次，下一名为 人数+1）。未完赛者列原因。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class Checkpoint:
    code: str
    name: str
    position: int


@dataclass(frozen=True)
class Participant:
    id: str
    bib: str
    name: str


@dataclass(frozen=True)
class Scan:
    message_id: str
    participant_id: str
    checkpoint_code: str
    event_time: datetime  # 统一为带时区的 UTC 时间


@dataclass(frozen=True)
class Pass:
    """一名参赛者在某个检查点的“首次有效过点”。"""

    checkpoint_code: str
    position: int
    event_time: datetime
    message_id: str


def compute(
    checkpoints: Iterable[Checkpoint],
    participants: Iterable[Participant],
    scans: Iterable[Scan],
) -> dict:
    """计算当前（草稿）成绩。

    返回结构同时服务于实时接口与发布固化：
      finished:  [{participant, rank, elapsed_seconds, passes:[...]}]
      unfinished:[{participant, reason, passed:[...], missing:...}]
    """
    cps = sorted(checkpoints, key=lambda c: c.position)
    cp_by_code = {c.code: c for c in cps}
    participants = list(participants)

    # 按参赛者归集扫描，再按 (事件时间, 报文ID) 排序：
    # 乱序上传在此被归一化；报文ID 仅用于同刻扫描的稳定排序。
    by_participant: dict[str, list[Scan]] = {p.id: [] for p in participants}
    for scan in scans:
        if scan.participant_id in by_participant:
            by_participant[scan.participant_id].append(scan)
    for rows in by_participant.values():
        rows.sort(key=lambda s: (s.event_time, s.message_id))

    finished: list[dict] = []
    unfinished: list[dict] = []

    for participant in participants:
        passes = _select_passes(cps, cp_by_code, by_participant[participant.id])

        if len(passes) == len(cps):
            start = passes[0].event_time
            finish = passes[-1].event_time
            finished.append(
                {
                    "participant": participant,
                    "elapsed_seconds": (finish - start).total_seconds(),
                    "start_time": start,
                    "finish_time": finish,
                    "passes": passes,
                }
            )
        else:
            reached = len(passes)
            if reached == 0:
                first = cps[0]
                reason = (
                    f"缺少检查点 {first.code}（{first.name}，第 1 位）的有效过点记录"
                )
            else:
                last_passed = passes[-1]
                missing = cp_by_code[cps[reached].code]
                reason = (
                    f"缺少检查点 {missing.code}（{missing.name}，第 {missing.position} 位）"
                    f"的有效过点记录；最后有效过点为 {last_passed.checkpoint_code} "
                    f"于 {last_passed.event_time.isoformat()}"
                )
            unfinished.append(
                {
                    "participant": participant,
                    "reason": reason,
                    "passed_count": reached,
                    "missing_checkpoint": cps[reached].code if cps else None,
                    "passes": passes,
                }
            )

    # 标准竞赛排名：rank = 1 + 严格更快者人数 → 同耗时并列
    finished.sort(key=lambda r: (r["elapsed_seconds"], r["participant"].bib))
    for idx, row in enumerate(finished):
        elapsed = row["elapsed_seconds"]
        row["rank"] = 1 + sum(
            1 for other in finished[:idx] if other["elapsed_seconds"] < elapsed
        )
    unfinished.sort(key=lambda r: (r["passed_count"], r["participant"].bib))

    return {"checkpoints": cps, "finished": finished, "unfinished": unfinished}


def _select_passes(
    cps: list[Checkpoint],
    cp_by_code: dict[str, Checkpoint],
    ordered_scans: list[Scan],
) -> list[Pass]:
    """贪心选取顺序一致且时间严格递增的首次有效过点。"""
    passes: list[Pass] = []
    expected_index = 0
    last_time: datetime | None = None

    for scan in ordered_scans:
        cp = cp_by_code.get(scan.checkpoint_code)
        if cp is None:
            continue  # 未知检查点在写入前已拒绝，此处再防御一次

        # 已确认过的检查点：重复/回溯扫描一律忽略
        if cp.position < expected_index + 1:
            continue

        # 顺序超前（中间有未确认检查点）：该点本次不能确认，
        # 但后续可能先出现被跳过点的更晚扫描，故继续处理。
        if cp.position > expected_index + 1:
            continue

        # 下一个应到检查点：要求事件时间严格递增
        if last_time is not None and scan.event_time <= last_time:
            continue

        passes.append(
            Pass(
                checkpoint_code=cp.code,
                position=cp.position,
                event_time=scan.event_time,
                message_id=scan.message_id,
            )
        )
        last_time = scan.event_time
        expected_index += 1

        if expected_index == len(cps):
            break

    return passes
