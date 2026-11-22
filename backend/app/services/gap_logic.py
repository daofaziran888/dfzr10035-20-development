"""行范围与缺口计算工具。

缺口定位的核心约定：
- 每个采集快照（data_version 行）持有「原始行起始/原始行结束」，即便采集快照标签相同，
  不同快照仍可凭原始行范围区分；
- 投递回执按（接收批次, 数据段）记录已确认的行范围；
- 缺口 = 期望行范围 − 同批次同数据段已确认回执的行范围并集；
- 区间统一用半整数端点的闭区间 [start, end] 表达。
"""
from __future__ import annotations

from typing import Any


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把若干闭区间合并成互不相交、按起点排序的区间列表。"""
    cleaned = sorted((s, e) for s, e in ranges if s <= e)
    merged: list[tuple[int, int]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_ranges(
    base: tuple[int, int], covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从单个区间 base 中减去 covered 的并集，返回剩余区间列表。"""
    remaining = [base]
    for cov_start, cov_end in merge_ranges(covered):
        updated: list[tuple[int, int]] = []
        for start, end in remaining:
            if cov_end < start or cov_start > end:
                updated.append((start, end))
                continue
            if cov_start > start:
                updated.append((start, min(end, cov_start - 1)))
            if cov_end < end:
                updated.append((max(start, cov_end + 1), end))
        remaining = updated
    return merge_ranges(remaining)


def intersect_ranges(
    left: tuple[int, int], right: tuple[int, int]
) -> tuple[int, int] | None:
    start = max(left[0], right[0])
    end = min(left[1], right[1])
    return (start, end) if start <= end else None


def range_size(ranges: list[tuple[int, int]]) -> int:
    return sum(end - start + 1 for start, end in ranges)


def format_ranges(ranges: list[tuple[int, int]]) -> str:
    """区间列表的稳定字符串形式，便于落库与展示。"""
    if not ranges:
        return ""
    return ";".join(f"{start}-{end}" for start, end in ranges)


def parse_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in str(text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            ranges.append((to_int(start), to_int(end)))
        else:
            point = to_int(part)
            ranges.append((point, point))
    return merge_ranges(ranges)


def find_covering_snapshots(
    rows: list[dict[str, Any]],
    missing: list[tuple[int, int]],
    *,
    prefer: str = "latest",
) -> list[dict[str, Any]]:
    """为缺口区间挑选可补投的快照切片。

    每条候选记录保留快照身份（id/版本编号/标签/类型/快照时间）和它实际覆盖到的原始行范围。
    同一标签的不同快照因 id 与原始行范围不同而天然可区分。

    prefer=latest 时按快照时间（其次 id）选每个缺口点上最新的可用快照；
    这样正式版本迟到后重新定位，未确认缺口会自动改由正式快照承载，
    而已经确认的数据段与源快照由调用方冻结、不参与重算。
    """
    candidates: list[dict[str, Any]] = []
    snapshots = [
        row for row in rows
        if to_int(row.get("原始行结束")) >= to_int(row.get("原始行起始"))
    ]
    snapshots.sort(
        key=lambda row: (str(row.get("快照时间", "")), to_int(row.get("id"))),
        reverse=(prefer == "latest"),
    )
    for start, end in missing:
        cursor = start
        while cursor <= end:
            chosen = None
            for snap in snapshots:
                snap_range = (
                    to_int(snap.get("原始行起始")),
                    to_int(snap.get("原始行结束")),
                )
                hit = intersect_ranges((cursor, end), snap_range)
                if hit is not None:
                    chosen = (snap, hit)
                    break
            if chosen is None:
                # 没有任何快照覆盖该尾部区间，记录为悬空缺口交由人工处理。
                candidates.append({
                    "snapshot_id": None,
                    "版本编号": "",
                    "采集快照标签": "",
                    "快照类型": "",
                    "快照时间": "",
                    "切片行范围": f"{cursor}-{end}",
                    "起始行": cursor,
                    "结束行": end,
                    "行数": end - cursor + 1,
                    "可补投": False,
                })
                break
            snap, hit = chosen
            candidates.append({
                "snapshot_id": to_int(snap.get("id")),
                "版本编号": snap.get("版本编号", ""),
                "采集快照标签": snap.get("采集快照标签", ""),
                "快照类型": snap.get("快照类型", ""),
                "快照时间": snap.get("快照时间", ""),
                "切片行范围": f"{hit[0]}-{hit[1]}",
                "起始行": hit[0],
                "结束行": hit[1],
                "行数": hit[1] - hit[0] + 1,
                "可补投": True,
            })
            cursor = hit[1] + 1
    return candidates
