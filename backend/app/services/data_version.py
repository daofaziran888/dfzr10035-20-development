"""数据版本领域服务：管理数据快照版本，与 data_delivery、subscriber、schema_diff、govern_report 联动。

投递缺口定位专题中，数据版本同时充当「采集快照」：每条快照持有接收批次、采集快照标签、
快照时间、快照类型和原始行范围。标签相同的不同快照依靠版本编号 id 与原始行范围区分。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.gap_logic import intersect_ranges, to_int
from app.store import store


class DataVersionService(BaseModuleService):
    MODULE = "data_version"
    ENTITY = "数据快照"
    OPERATION_TERM = "采集"
    LIST_FIELDS = [
        "版本编号", "数据来源", "行数统计", "版本标签", "Schema摘要", "创建原因",
        "依赖版本", "创建时间", "接收批次", "采集快照标签", "快照类型", "快照时间",
        "原始行起始", "原始行结束", "回执状态",
    ]
    REQUIRED_FIELDS = ["版本编号", "数据来源", "行数统计"]
    STATUS_ORDER = ["创建中", "可用", "标注中", "已废弃"]
    ACTION_RULES = {"创建快照": "可用", "标注版本": "标注中", "废弃版本": "已废弃"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str):
        """创建快照时同步投递管道和订阅方；标注版本时联动治理报告；废弃版本时审计。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "创建快照":
            self._sync_delivery_and_subscribers(entry)
            self._log_version_event(entry, "created")
            message += "，已同步投递管道和订阅方"
        elif action == "标注版本":
            self._create_govern_report(entry)
            message += "，已联动治理报告"
        elif action == "废弃版本":
            self._audit_obsolescence(entry)
            message += "，已记录审计日志"
        return entry, message

    def _sync_delivery_and_subscribers(self, version: dict):
        """通知投递管道和订阅方版本已可用。"""
        source = version.get("数据来源", "")
        for delivery in store.rows("data_delivery"):
            if delivery.get("数据来源") == source:
                delivery["status"] = "投递中"
                delivery["pending"] = True
        for sub in store.rows("subscriber"):
            if sub.get("数据主题") == source:
                sub["消费状态"] = "待消费"
                sub["最后推送"] = "2026-11-17"

    def _log_version_event(self, version: dict, event: str):
        log_rows = store.rows("call_log")
        vid = str(version["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-VER-{vid}",
            "接口端点": version.get("数据来源", ""),
            "请求参数": f"version_event:{event}",
            "响应状态": "200",
            "响应耗时(ms)": 30,
            "返回行数": version.get("行数统计", 0),
            "错误信息": "",
            "调用时间": "2026-11-17",
            "status": "成功", "pending": False, "abnormal": False,
        })

    def _create_govern_report(self, version: dict):
        gov_rows = store.rows("govern_report")
        vid = str(version["id"]).zfill(4)
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-VER-{vid}",
            "评估范围": f"数据版本标注:{version.get('数据来源', '')}",
            "合规指标": "版本标注规范",
            "当前得分": 90.0,
            "改进事项": f"版本{vid}已标注为{version.get('版本标签', '')}",
            "评估周期": "2026-Q4",
            "负责人": "数据标注组",
            "生成时间": "2026-11-17",
            "status": "已生成", "pending": False, "abnormal": False,
        })

    def _audit_obsolescence(self, version: dict):
        audit_rows = store.rows("pipeline_audit")
        vid = str(version["id"]).zfill(4)
        audit_rows.append({
            "id": max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1,
            "记录编号": f"PIPE-VER-{vid}",
            "管道名称": f"data_version:{vid}",
            "操作人": "system",
            "操作类型": "废弃",
            "变更摘要": f"版本{vid}({version.get('版本标签', '')})已废弃",
            "操作时间": "2026-11-17",
            "来源节点": "data_version",
            "审计结论": "已记录",
            "status": "已记录", "pending": False, "abnormal": False,
        })

    def check_version_lineage(self, source: str) -> dict:
        """跨模块版本谱系检查。"""
        versions = [v for v in store.rows("data_version") if v.get("数据来源") == source]
        deliveries = [d for d in store.rows("data_delivery") if d.get("数据来源") == source]
        subscribers = [s for s in store.rows("subscriber") if s.get("数据主题") == source]
        return {
            "source": source,
            "versions": len(versions),
            "active_versions": sum(1 for v in versions if v.get("status") == "可用"),
            "delivery_pipelines": len(deliveries),
            "subscribers": len(subscribers),
        }

    # ---------- 投递缺口定位专题：采集快照检索 ----------

    def create_entry(self, values: dict):
        """登记快照时补齐缺口定位所需字段，默认按行数统计铺满原始行范围。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        entry.setdefault("接收批次", values.get("接收批次", ""))
        entry.setdefault("采集快照标签", values.get("采集快照标签") or entry.get("版本标签", ""))
        entry.setdefault("快照类型", values.get("快照类型", "临时版本"))
        entry.setdefault("快照时间", values.get("快照时间") or entry.get("创建时间", ""))
        # 专题登记的采集快照直接可用，避免停留在「创建中」无法参与候选检索。
        entry["status"] = "可用"
        entry["pending"] = True
        entry["回执状态"] = values.get("回执状态") or "未投递"
        if entry.get("依赖版本") is None:
            entry["依赖版本"] = ""
        if not entry.get("原始行起始") or not entry.get("原始行结束"):
            row_count = to_int(entry.get("行数统计"), 0)
            entry["原始行起始"] = to_int(values.get("原始行起始"), 1)
            entry["原始行结束"] = to_int(values.get("原始行结束"), row_count)
        return entry, []

    def snapshot_search_list(
        self,
        *,
        batch: str | None = None,
        source: str | None = None,
        label: str | None = None,
    ) -> dict:
        """快照检索清单：回执确认结果同步到快照上，操作员据此重新定位未交付的字段和行范围。"""
        rows = list(store.rows("data_version"))
        if batch:
            rows = [row for row in rows if row.get("接收批次") == batch]
        if source:
            rows = [row for row in rows if row.get("数据来源") == source]
        if label:
            rows = [row for row in rows if row.get("采集快照标签") == label]
        receipts = store.rows("delivery_receipt")
        items: list[dict] = []
        for row in rows:
            snap_range = (to_int(row.get("原始行起始")), to_int(row.get("原始行结束")))
            confirmed: list[tuple[int, int]] = []
            for receipt in receipts:
                if receipt.get("源快照编号") != row.get("版本编号"):
                    continue
                if receipt.get("status") != "已确认":
                    continue
                hit = intersect_ranges(
                    snap_range,
                    (to_int(receipt.get("起始行")), to_int(receipt.get("结束行"))),
                )
                if hit is not None:
                    confirmed.append(hit)
            confirmed = self._merge(confirmed)
            total = max(snap_range[1] - snap_range[0] + 1, 0)
            delivered = sum(end - start + 1 for start, end in confirmed)
            items.append({
                "id": row.get("id"),
                "版本编号": row.get("版本编号"),
                "接收批次": row.get("接收批次", ""),
                "数据来源": row.get("数据来源", ""),
                "采集快照标签": row.get("采集快照标签", ""),
                "快照类型": row.get("快照类型", ""),
                "快照时间": row.get("快照时间", ""),
                "原始行起始": snap_range[0],
                "原始行结束": snap_range[1],
                "原始行范围": f"{snap_range[0]}-{snap_range[1]}",
                "已确认行范围": self._format(confirmed),
                "已确认行数": delivered,
                "未交付行数": total - delivered,
                "回执状态": row.get("回执状态", "未投递"),
                "status": row.get("status"),
            })
        items.sort(key=lambda item: str(item.get("快照时间", "")))
        return {"items": items, "total": len(items)}

    def search_candidates(
        self,
        *,
        start_time: str,
        end_time: str,
        source: str | None = None,
        batch: str | None = None,
        label: str | None = None,
        snapshot_type: str | None = None,
    ) -> dict:
        """按时间范围检索可补投的候选快照版本（补投中断后重试入口）。

        字符串时间按字典序比较即可覆盖 ISO 风格时间戳；迟到的正式快照只要落在时间窗内
        就会出现在候选列表，并通过 快照类型/快照时间 与临时版本区分。
        """
        rows = [
            row for row in store.rows("data_version")
            if row.get("status") == "可用"
            and str(row.get("快照时间", "")) >= start_time
            and str(row.get("快照时间", "")) <= end_time
        ]
        if source:
            rows = [row for row in rows if row.get("数据来源") == source]
        if batch:
            rows = [row for row in rows if row.get("接收批次") == batch]
        if label:
            rows = [row for row in rows if row.get("采集快照标签") == label]
        if snapshot_type:
            rows = [row for row in rows if row.get("快照类型") == snapshot_type]
        rows.sort(key=lambda row: (str(row.get("快照时间", "")), to_int(row.get("id"))))
        items = [
            {
                "id": row.get("id"),
                "版本编号": row.get("版本编号"),
                "接收批次": row.get("接收批次", ""),
                "数据来源": row.get("数据来源", ""),
                "采集快照标签": row.get("采集快照标签", ""),
                "快照类型": row.get("快照类型", ""),
                "快照时间": row.get("快照时间", ""),
                "原始行范围": f"{row.get('原始行起始')}-{row.get('原始行结束')}",
                "行数统计": row.get("行数统计"),
                "回执状态": row.get("回执状态", "未投递"),
            }
            for row in rows
        ]
        return {
            "time_range": [start_time, end_time],
            "filters": {"source": source, "batch": batch, "label": label, "snapshot_type": snapshot_type},
            "items": items,
            "total": len(items),
        }

    def sync_receipt_status(self, snapshot_code: str, status: str) -> None:
        """回执确认结果同步到快照检索清单（持久化在快照行上）。"""
        for row in store.rows("data_version"):
            if row.get("版本编号") == snapshot_code:
                row["回执状态"] = status

    @staticmethod
    def _merge(ranges):
        from app.services.gap_logic import merge_ranges
        return merge_ranges(ranges)

    @staticmethod
    def _format(ranges) -> str:
        from app.services.gap_logic import format_ranges
        return format_ranges(ranges)