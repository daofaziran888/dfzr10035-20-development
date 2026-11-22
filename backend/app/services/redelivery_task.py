"""补投任务领域服务：数据投递按缺口定位结果创建补投请求。

生命周期：待补投 → 补投中 →（中断 → 重试中 → 补投中…）→ 已完成；异常可置 补投失败。
- 创建只接受处于开放状态的缺口定位结果，并把缺口候选快照切片复制为任务切片；
- 中断按实际完成行范围切割任务切片，并为已投递部分生成投递回执；
- 重试支持按时间范围重新检索候选快照版本（正式版本迟到场景）；
- 复原接口按版本编号 + 行范围还原任务实际补了哪些记录。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.data_version import DataVersionService
from app.services.delivery_gap import DeliveryGapService
from app.services.delivery_receipt import DeliveryReceiptService
from app.services.gap_logic import (
    find_covering_snapshots,
    format_ranges,
    intersect_ranges,
    merge_ranges,
    parse_ranges,
    range_size,
    subtract_ranges,
    to_int,
)
from app.store import store

ACTIVE_STATUSES = ("待补投", "补投中", "重试中")


class RedeliveryTaskService(BaseModuleService):
    MODULE = "redelivery_task"
    ENTITY = "补投任务"
    OPERATION_TERM = "创建"
    LIST_FIELDS = [
        "任务编号", "接收批次", "接收方", "数据段", "关联缺口", "计划行范围",
        "任务切片", "已补投行范围", "剩余行范围", "重试次数", "最后重试时间", "创建时间",
    ]
    REQUIRED_FIELDS = ["接收批次", "数据段"]
    STATUS_ORDER = ["待补投", "补投中", "重试中", "已完成", "补投失败"]
    ACTION_RULES = {
        "启动补投": "补投中",
        "中断补投": "重试中",
        "重试补投": "补投中",
        "完成补投": "已完成",
        "标记失败": "补投失败",
    }
    NEGATIVE_ACTIONS = ["标记失败"]

    # ---------- 创建：必须基于缺口定位结果 ----------

    def create_from_gap(
        self,
        gap_id: int,
        *,
        create_time: str | None = None,
    ) -> tuple[dict | None, str]:
        gap = store.find("delivery_gap", gap_id)
        if gap is None:
            return None, f"投递缺口 {gap_id} 不存在，无法创建补投请求"
        if gap.get("status") in ("已确认", "已关闭"):
            return None, f"缺口 {gap_id} 已冻结，不再接受补投请求"
        slices = [dict(item) for item in gap.get("候选快照切片", []) if item.get("可补投")]
        if not slices:
            return None, f"缺口 {gap_id} 没有可补投的快照切片"
        rows = store.rows(self.MODULE)
        next_id = max((to_int(row.get("id"), 0) for row in rows), default=0) + 1
        batch = gap.get("接收批次", "")
        plan_ranges = merge_ranges(
            [(to_int(item["起始行"]), to_int(item["结束行"])) for item in slices]
        )
        entry = {
            "id": next_id,
            "任务编号": f"REDL-{batch.replace('BATCH-', '')}-{next_id:02d}",
            "接收批次": batch,
            "接收方": gap.get("接收方", ""),
            "数据段": gap.get("数据段", ""),
            "关联缺口": gap.get("缺口编号", ""),
            "关联缺口ID": gap_id,
            "数据来源": gap.get("数据来源", ""),
            "字段范围": gap.get("字段范围", ""),
            "计划行范围": format_ranges(plan_ranges),
            "任务切片": slices,
            "已补投行范围": "",
            "剩余行范围": gap.get("缺失行范围", ""),
            "重试次数": 0,
            "最后重试时间": "",
            "创建时间": create_time or "2026-11-22 10:10",
            "回执编号": [],
            "status": "待补投",
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        DeliveryGapService().mark_redelivering(gap_id, entry["任务编号"])
        return entry, f"已按缺口定位结果创建补投请求 {entry['任务编号']}"

    # ---------- 中断：按实际完成行切割切片并生成回执 ----------

    def interrupt(
        self,
        task_id: int,
        *,
        delivered_end: int,
        event_time: str | None = None,
    ) -> tuple[dict | None, str]:
        entry = store.find(self.MODULE, task_id)
        if entry is None:
            return None, f"{self.ENTITY} {task_id} 不存在"
        if entry.get("status") not in ("补投中", "重试中"):
            return None, f"任务 {entry.get('任务编号')} 当前状态 {entry.get('status')} 不允许中断"
        plan = parse_ranges(entry["计划行范围"])
        plan_start = min((start for start, _ in plan), default=delivered_end)
        delivered = intersect_ranges(
            (plan_start, delivered_end),
            (plan_start, max((end for _, end in plan), default=delivered_end)),
        )
        if delivered is None:
            return None, "中断点未落在计划行范围内"
        self._apply_progress(entry, [delivered], event_time or "2026-11-22 10:30", partial=True)
        entry["status"] = "重试中"
        entry["pending"] = True
        entry["abnormal"] = True
        return entry, f"补投中断于第 {delivered_end} 行，已生成部分回执，剩余范围等待重试"

    # ---------- 重试：可按时间范围重新检索候选版本 ----------

    def retry(
        self,
        task_id: int,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        event_time: str | None = None,
    ) -> tuple[dict | None, str]:
        entry = store.find(self.MODULE, task_id)
        if entry is None:
            return None, f"{self.ENTITY} {task_id} 不存在"
        if entry.get("status") not in ("重试中", "补投失败"):
            return None, f"任务 {entry.get('任务编号')} 当前状态 {entry.get('status')} 不允许重试"
        remaining = parse_ranges(entry["剩余行范围"])
        if not remaining:
            return None, "任务没有剩余行范围需要重试"

        snapshots = [
            row for row in store.rows("data_version")
            if row.get("接收批次") == entry.get("接收批次")
            and row.get("status") == "可用"
            and (not entry.get("数据来源") or row.get("数据来源") == entry.get("数据来源"))
        ]
        if start_time:
            snapshots = [row for row in snapshots if str(row.get("快照时间", "")) >= start_time]
        if end_time:
            snapshots = [row for row in snapshots if str(row.get("快照时间", "")) <= end_time]
        candidates = find_covering_snapshots(snapshots, remaining, prefer="latest")
        if not any(item.get("可补投") for item in candidates):
            return None, "时间范围内没有可覆盖剩余行范围的候选快照"

        # 已补投部分切片按实际已补投行范围裁剪保留，避免与新候选切片重叠而重复出回执。
        done = parse_ranges(entry["已补投行范围"])
        kept: list[dict] = []
        if done:
            done_span = (min(s for s, _ in done), max(e for _, e in done))
            for item in entry["任务切片"]:
                if not item.get("可补投"):
                    continue
                hit = intersect_ranges(
                    (to_int(item["起始行"]), to_int(item["结束行"])), done_span
                )
                if hit is None:
                    continue
                clipped = dict(item)
                clipped["起始行"], clipped["结束行"] = hit
                clipped["切片行范围"] = f"{hit[0]}-{hit[1]}"
                clipped["行数"] = hit[1] - hit[0] + 1
                kept.append(clipped)
        entry["任务切片"] = kept + candidates
        entry["重试次数"] = to_int(entry.get("重试次数")) + 1
        entry["最后重试时间"] = event_time or "2026-11-22 11:00"
        entry["status"] = "补投中"
        entry["pending"] = True
        entry["abnormal"] = False
        return entry, "已按时间范围重新检索候选版本并恢复补投"

    # ---------- 完成：补齐剩余行、生成回执、回写缺口 ----------

    def complete(
        self,
        task_id: int,
        *,
        event_time: str | None = None,
    ) -> tuple[dict | None, str]:
        entry = store.find(self.MODULE, task_id)
        if entry is None:
            return None, f"{self.ENTITY} {task_id} 不存在"
        if entry.get("status") not in ("补投中", "重试中"):
            return None, f"任务 {entry.get('任务编号')} 当前状态 {entry.get('status')} 不允许完成"
        remaining = parse_ranges(entry["剩余行范围"])
        if remaining:
            self._apply_progress(entry, remaining, event_time or "2026-11-22 12:00", partial=False)
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False
        # 补投完成即按缺口定位结果闭环：缺口与源快照冻结，等待回执确认只是接收方侧动作。
        gap_id = entry.get("关联缺口ID")
        if gap_id is not None:
            gap = store.find("delivery_gap", to_int(gap_id))
            if gap is not None:
                gap["status"] = "已确认"
                gap["pending"] = False
                for code in DeliveryGapService()._snapshot_codes(gap):
                    DataVersionService().sync_receipt_status(code, "已确认")
        return entry, "补投完成，投递回执已生成，缺口状态已回写"

    # ---------- 复原：按版本 + 行范围还原实际补了哪些记录 ----------

    def reconstruct(self, task_id: int) -> dict:
        entry = store.find(self.MODULE, task_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {task_id} 不存在"}
        by_version: dict[str, dict] = {}
        for item in entry.get("任务切片", []):
            if not item.get("可补投"):
                continue
            code = item["版本编号"]
            bucket = by_version.setdefault(code, {
                "版本编号": code,
                "snapshot_id": item.get("snapshot_id"),
                "采集快照标签": item.get("采集快照标签"),
                "快照类型": item.get("快照类型"),
                "快照时间": item.get("快照时间"),
                "行范围": [],
                "行数": 0,
            })
            bucket["行范围"].append((to_int(item["起始行"]), to_int(item["结束行"])))
        records: list[dict] = []
        for code, bucket in by_version.items():
            merged = merge_ranges(bucket["行范围"])
            bucket["行范围"] = [f"{start}-{end}" for start, end in merged]
            bucket["行数"] = range_size(merged)
            records.append(bucket)
        records.sort(key=lambda item: item["版本编号"])
        receipts = [
            row for row in store.rows("delivery_receipt")
            if row.get("关联补投任务") == entry.get("任务编号")
        ]
        return {
            "ok": True,
            "任务编号": entry.get("任务编号"),
            "接收批次": entry.get("接收批次"),
            "接收方": entry.get("接收方"),
            "数据段": entry.get("数据段"),
            "status": entry.get("status"),
            "计划行范围": entry.get("计划行范围"),
            "已补投行范围": entry.get("已补投行范围"),
            "实际补投记录": records,
            "总补投行数": range_size(parse_ranges(entry.get("已补投行范围", ""))),
            "回执清单": [
                {
                    "回执编号": row.get("回执编号"),
                    "版本编号": row.get("版本编号"),
                    "起始行": row.get("起始行"),
                    "结束行": row.get("结束行"),
                    "status": row.get("status"),
                }
                for row in receipts
            ],
        }

    def retry_candidates(
        self,
        task_id: int,
        *,
        start_time: str,
        end_time: str,
    ) -> dict:
        """只检索不落地：中断后重试前预览时间窗内的候选版本。"""
        entry = store.find(self.MODULE, task_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {task_id} 不存在"}
        result = DataVersionService().search_candidates(
            start_time=start_time,
            end_time=end_time,
            batch=entry.get("接收批次"),
            source=entry.get("数据来源"),
        )
        result["ok"] = True
        result["剩余行范围"] = entry.get("剩余行范围")
        return result

    # ---------- 内部工具 ----------

    def _apply_progress(
        self, entry: dict, progress: list[tuple[int, int]], event_time: str, *, partial: bool
    ) -> None:
        done = merge_ranges(parse_ranges(entry.get("已补投行范围", "")) + progress)
        plan = parse_ranges(entry["计划行范围"])
        remaining = []
        for base in plan:
            remaining.extend(subtract_ranges(base, done))
        remaining = merge_ranges(remaining)

        # 中断重试后，已生成过回执的行范围不能在完成时重复出回执。
        already_receipted = merge_ranges(
            [
                (to_int(row.get("起始行")), to_int(row.get("结束行")))
                for row in store.rows("delivery_receipt")
                if row.get("关联补投任务") == entry.get("任务编号")
            ]
        )
        fresh_ranges: list[tuple[int, int]] = []
        for base in progress:
            fresh_ranges.extend(subtract_ranges(base, already_receipted))
        fresh_ranges = merge_ranges(fresh_ranges)

        entry["已补投行范围"] = format_ranges(done)
        entry["剩余行范围"] = format_ranges(remaining)

        # 按切片身份（版本 + 原始行范围）为本次新增推进部分生成回执。
        receipts = DeliveryReceiptService()
        for start, end in fresh_ranges:
            for item in entry["任务切片"]:
                if not item.get("可补投"):
                    continue
                hit = intersect_ranges(
                    (start, end),
                    (to_int(item["起始行"]), to_int(item["结束行"])),
                )
                if hit is None:
                    continue
                receipt = receipts.create_from_redelivery(
                    batch=entry["接收批次"],
                    receiver=entry["接收方"],
                    segment=entry["数据段"],
                    snapshot_code=item["版本编号"],
                    start_row=hit[0],
                    end_row=hit[1],
                    fields=entry.get("字段范围", ""),
                    task_code=entry["任务编号"],
                    receipt_time=event_time,
                    result="部分补投已送达，待接收方确认" if partial else "补投完成，待接收方确认",
                )
                entry.setdefault("回执编号", []).append(receipt["回执编号"])
