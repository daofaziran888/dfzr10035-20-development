"""投递回执领域服务。

回执按（接收批次, 数据段）登记接收方实际确认入库的行范围，是缺口定位的「已交付」基线。
确认动作会把确认结果同步回采集快照（data_version）的检索清单字段。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.data_version import DataVersionService
from app.services.gap_logic import format_ranges, merge_ranges, parse_ranges, to_int
from app.store import store


class DeliveryReceiptService(BaseModuleService):
    MODULE = "delivery_receipt"
    ENTITY = "投递回执"
    OPERATION_TERM = "登记"
    LIST_FIELDS = [
        "回执编号", "接收批次", "接收方", "数据段", "源快照编号", "版本编号",
        "起始行", "结束行", "字段范围", "回执时间", "确认结果", "关联补投任务",
    ]
    REQUIRED_FIELDS = ["回执编号", "接收批次", "接收方", "数据段", "起始行", "结束行"]
    STATUS_ORDER = ["待确认", "已确认", "确认驳回"]
    ACTION_RULES = {"提交确认": "已确认", "驳回回执": "确认驳回"}
    NEGATIVE_ACTIONS = ["驳回回执"]

    def run_action(self, entry_id: int, action: str):
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "提交确认":
            self._sync_snapshot(entry, "已确认")
            message += "，确认结果已同步到快照检索清单"
        elif action == "驳回回执":
            self._sync_snapshot(entry, "确认驳回")
        return entry, message

    def _sync_snapshot(self, receipt: dict, status: str) -> None:
        snapshot_code = receipt.get("源快照编号") or receipt.get("版本编号")
        if snapshot_code:
            DataVersionService().sync_receipt_status(snapshot_code, status)

    def confirmed_ranges(self, batch: str, segment: str) -> list[tuple[int, int]]:
        """同批次同数据段下，所有已确认回执的行范围并集。"""
        ranges = [
            (to_int(row.get("起始行")), to_int(row.get("结束行")))
            for row in store.rows(self.MODULE)
            if row.get("接收批次") == batch
            and row.get("数据段") == segment
            and row.get("status") == "已确认"
        ]
        return merge_ranges(ranges)

    def create_from_redelivery(
        self,
        *,
        batch: str,
        receiver: str,
        segment: str,
        snapshot_code: str,
        start_row: int,
        end_row: int,
        fields: str,
        task_code: str,
        receipt_time: str,
        result: str,
    ) -> dict:
        """补投任务中断/完成时由领域服务自动生成回执。"""
        rows = store.rows(self.MODULE)
        seq = max((to_int(row.get("id"), 0) for row in rows), default=0) + 1
        entry = {
            "id": seq,
            "回执编号": f"RECP-{batch.replace('BATCH-', '')}-{seq:02d}",
            "接收批次": batch,
            "接收方": receiver,
            "数据段": segment,
            "源快照编号": snapshot_code,
            "版本编号": snapshot_code,
            "起始行": start_row,
            "结束行": end_row,
            "字段范围": fields,
            "回执时间": receipt_time,
            "确认结果": result,
            "关联补投任务": task_code,
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        return entry

    def confirm_receipt(self, entry_id: int) -> tuple[dict | None, str]:
        """供路由直接调用的确认入口，等价于动作「提交确认」。"""
        return self.run_action(entry_id, "提交确认")

    def receipt_summary(self, batch: str) -> dict:
        receipts = [
            row for row in store.rows(self.MODULE) if row.get("接收批次") == batch
        ]
        segments: dict[str, dict] = {}
        for row in receipts:
            seg = segments.setdefault(row.get("数据段", ""), {
                "数据段": row.get("数据段", ""),
                "接收方": row.get("接收方", ""),
                "已确认行范围": "",
                "回执数": 0,
            })
            seg["回执数"] += 1
        for seg, info in segments.items():
            info["已确认行范围"] = format_ranges(self.confirmed_ranges(batch, seg))
        return {
            "batch": batch,
            "receipt_count": len(receipts),
            "confirmed": sum(1 for row in receipts if row.get("status") == "已确认"),
            "segments": list(segments.values()),
        }

    # 兼容工具：部分场景以分号串保存多区间
    @staticmethod
    def _parse(text: str):
        return parse_ranges(text)
