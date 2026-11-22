"""投递缺口定位领域服务。

把采集快照（data_version）与投递回执（delivery_receipt）关联成「投递缺口定位结果」。

关键不变量：
1. 缺口按（接收批次, 数据段）唯一认定：重复定位不会制造重复缺口，只刷新未确认缺口的候选；
2. 已确认/已关闭缺口与它绑定的源快照冻结，后续查询（含正式版本迟到）不得覆盖；
3. 标签相同的不同快照仍凭版本 id 与原始行范围区分，候选切片携带每条快照的身份信息；
4. 正式版本迟到后重新定位，未确认缺口自动切换到时间最新的可用快照承载。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.delivery_receipt import DeliveryReceiptService
from app.services.gap_logic import (
    find_covering_snapshots,
    format_ranges,
    range_size,
    subtract_ranges,
    to_int,
)
from app.store import store

OPEN_STATUSES = ("待定位", "已定位", "补投中")


class DeliveryGapService(BaseModuleService):
    MODULE = "delivery_gap"
    ENTITY = "投递缺口"
    OPERATION_TERM = "定位"
    LIST_FIELDS = [
        "缺口编号", "接收批次", "接收方", "数据段", "期望行范围", "已确认行范围",
        "缺失行范围", "候选快照切片", "源快照编号", "数据来源", "字段范围",
        "缺失行数", "最近定位时间",
    ]
    REQUIRED_FIELDS = ["接收批次", "数据段", "期望行范围"]
    STATUS_ORDER = ["待定位", "已定位", "补投中", "已确认", "已关闭"]
    ACTION_RULES = {
        "发起定位": "已定位",
        "创建补投": "补投中",
        "确认缺口": "已确认",
        "关闭缺口": "已关闭",
    }
    NEGATIVE_ACTIONS = ["关闭缺口"]

    # ---------- 缺口定位：快照 × 回执 的关联检索 ----------

    def locate_gaps(
        self,
        *,
        batch: str,
        receiver: str,
        segments: list[dict],
        source: str | None = None,
        locate_time: str | None = None,
    ) -> dict:
        """按接收批次定位每个数据段的投递缺口，并为缺失区间匹配可补投快照切片。

        segments: [{"数据段": ..., "起始行": int, "结束行": int, "字段范围": ...}]
        """
        receipt_service = DeliveryReceiptService()
        existing = {
            (row.get("接收批次"), row.get("数据段")): row
            for row in store.rows(self.MODULE)
            if row.get("接收批次") == batch
        }
        results: list[dict] = []
        for spec in segments:
            segment = str(spec["数据段"])
            expect_start = to_int(spec.get("起始行"))
            expect_end = to_int(spec.get("结束行"))
            fields = str(spec.get("字段范围", ""))
            key = (batch, segment)
            current = existing.get(key)

            # 不变量2：已确认/已关闭缺口冻结，后续查询不覆盖。
            if current is not None and current.get("status") in ("已确认", "已关闭"):
                results.append(self._view(current, locked=True))
                continue

            covered = receipt_service.confirmed_ranges(batch, segment)
            missing = subtract_ranges((expect_start, expect_end), covered)
            snapshots = self._snapshots_for(batch, source)
            candidates = find_covering_snapshots(snapshots, missing, prefer="latest")
            payload = {
                "接收批次": batch,
                "接收方": receiver,
                "数据段": segment,
                "数据来源": source or self._infer_source(snapshots),
                "字段范围": fields,
                "期望行范围": f"{expect_start}-{expect_end}",
                "已确认行范围": format_ranges(covered),
                "缺失行范围": format_ranges(missing),
                "缺失行数": range_size(missing),
                "候选快照切片": candidates,
                "源快照编号": self._primary_snapshot(candidates),
                "最近定位时间": locate_time or "2026-11-22 10:00",
            }

            if current is None:
                # 不变量1：按批次+数据段唯一创建。
                entry = self._new_gap(payload)
            else:
                # 未确认缺口：刷新候选与缺失范围，保留缺口编号与补投联动痕迹。
                entry = current
                entry.update(payload)
            if not missing:
                entry["status"] = "已确认"
                entry["pending"] = False
            elif entry.get("status") == "待定位":
                entry["status"] = "已定位"
                entry["pending"] = True
            results.append(self._view(entry))
        return {"batch": batch, "receiver": receiver, "located": len(results), "items": results}

    def confirm_gap(self, entry_id: int) -> tuple[dict | None, str]:
        """操作员确认缺口：冻结缺口与其源快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        entry["status"] = "已确认"
        entry["pending"] = False
        for code in self._snapshot_codes(entry):
            DeliveryReceiptService()  # 保持服务构造一致性
            from app.services.data_version import DataVersionService
            DataVersionService().sync_receipt_status(code, "已确认")
        return entry, "缺口已确认，数据段与源快照已冻结，后续查询不再覆盖"

    def mark_redelivering(self, entry_id: int, task_code: str) -> dict | None:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "补投中"
        entry["pending"] = True
        entry["关联补投任务"] = task_code
        return entry

    # ---------- 查询 ----------

    def gaps_by_batch(self, batch: str) -> dict:
        rows = [row for row in store.rows(self.MODULE) if row.get("接收批次") == batch]
        rows.sort(key=lambda row: str(row.get("数据段", "")))
        return {"batch": batch, "total": len(rows), "items": [self._view(row) for row in rows]}

    def open_gaps(self, *, batch: str | None = None) -> dict:
        rows = [
            row for row in store.rows(self.MODULE)
            if row.get("status") in OPEN_STATUSES and (batch is None or row.get("接收批次") == batch)
        ]
        return {"total": len(rows), "items": [self._view(row) for row in rows]}

    # ---------- 内部工具 ----------

    def _snapshots_for(self, batch: str, source: str | None) -> list[dict]:
        rows = [
            row for row in store.rows("data_version")
            if row.get("接收批次") == batch and row.get("status") == "可用"
        ]
        if source:
            rows = [row for row in rows if row.get("数据来源") == source]
        return rows

    @staticmethod
    def _infer_source(snapshots: list[dict]) -> str:
        return str(snapshots[0].get("数据来源", "")) if snapshots else ""

    @staticmethod
    def _primary_snapshot(candidates: list[dict]) -> str:
        for cand in candidates:
            if cand.get("版本编号"):
                return str(cand["版本编号"])
        return ""

    @staticmethod
    def _snapshot_codes(entry: dict) -> list[str]:
        codes = []
        for cand in entry.get("候选快照切片", []) or []:
            code = cand.get("版本编号") if isinstance(cand, dict) else None
            if code and code not in codes:
                codes.append(code)
        if entry.get("源快照编号") and entry["源快照编号"] not in codes:
            codes.append(entry["源快照编号"])
        return codes

    def _new_gap(self, payload: dict) -> dict:
        rows = store.rows(self.MODULE)
        next_id = max((to_int(row.get("id"), 0) for row in rows), default=0) + 1
        entry = {"id": next_id, "abnormal": False}
        entry.update(payload)
        entry["缺口编号"] = f"GAP-{payload['接收批次'].replace('BATCH-', '')}-{next_id:02d}"
        entry["关联补投任务"] = ""
        entry["status"] = "待定位"
        entry["pending"] = True
        rows.append(entry)
        return entry

    @staticmethod
    def _view(entry: dict, *, locked: bool | None = None) -> dict:
        view = dict(entry)
        if locked is None:
            locked = entry.get("status") in ("已确认", "已关闭")
        view["已冻结"] = locked
        return view
