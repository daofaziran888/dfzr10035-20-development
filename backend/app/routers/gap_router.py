"""投递缺口定位专题路由。

把采集快照检索、投递回执确认、缺口定位、补投任务编排成一条闭环链路。
固定路径必须先于通用资源路由 /api/{module} 注册（见 main.py）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.data_version import DataVersionService
from app.services.delivery_gap import DeliveryGapService
from app.services.delivery_receipt import DeliveryReceiptService
from app.services.redelivery_task import RedeliveryTaskService

router = APIRouter(prefix="/api/gap", tags=["投递缺口定位"])


class SegmentSpec(BaseModel):
    数据段: str
    起始行: int
    结束行: int
    字段范围: str = ""
    接收方: str | None = None


class LocatePayload(BaseModel):
    batch: str
    receiver: str
    source: str | None = None
    locate_time: str | None = None
    segments: list[SegmentSpec]


class CreateRedeliveryPayload(BaseModel):
    create_time: str | None = None


class InterruptPayload(BaseModel):
    delivered_end: int
    event_time: str | None = None


class RetryPayload(BaseModel):
    start_time: str | None = None
    end_time: str | None = None
    event_time: str | None = None


class SnapshotCreatePayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


# ---------- 采集快照检索 ----------

@router.get("/snapshots")
def snapshot_search_list(
    batch: str | None = None,
    source: str | None = None,
    label: str | None = None,
):
    """快照检索清单：含每条快照的已确认行范围与未交付行数（回执确认结果同步视图）。"""
    return DataVersionService().snapshot_search_list(batch=batch, source=source, label=label)


@router.get("/snapshots/candidates")
def snapshot_candidates(
    start_time: str,
    end_time: str,
    source: str | None = None,
    batch: str | None = None,
    label: str | None = None,
    snapshot_type: str | None = None,
):
    """按时间范围检索可补投候选版本（补投中断后重试入口）。"""
    return DataVersionService().search_candidates(
        start_time=start_time,
        end_time=end_time,
        source=source,
        batch=batch,
        label=label,
        snapshot_type=snapshot_type,
    )


@router.post("/snapshots")
def register_snapshot(payload: SnapshotCreatePayload):
    """登记迟到的正式采集快照（data_version 通用登记的专题别名）。"""
    entry, missing = DataVersionService().create_entry(payload.values)
    if missing:
        return {"ok": False, "message": f"缺少必填字段：{'、'.join(missing)}"}
    return {"ok": True, "message": "采集快照已登记", "entry": entry}


# ---------- 投递回执 ----------

@router.get("/receipts/summary")
def receipt_summary(batch: str):
    return DeliveryReceiptService().receipt_summary(batch)


@router.post("/receipts/{receipt_id}/confirm")
def confirm_receipt(receipt_id: int):
    """回执确认：确认结果同步到快照检索清单。"""
    entry, message = DeliveryReceiptService().confirm_receipt(receipt_id)
    return {"ok": entry is not None, "message": message, "entry": entry}


# ---------- 缺口定位 ----------

@router.post("/locate")
def locate_gaps(payload: LocatePayload):
    """按接收批次把采集快照与投递回执关联成投递缺口定位结果。"""
    return DeliveryGapService().locate_gaps(
        batch=payload.batch,
        receiver=payload.receiver,
        source=payload.source,
        locate_time=payload.locate_time,
        segments=[item.model_dump() for item in payload.segments],
    )


@router.get("/gaps")
def gaps_by_batch(batch: str):
    return DeliveryGapService().gaps_by_batch(batch)


@router.get("/gaps/open")
def open_gaps(batch: str | None = None):
    return DeliveryGapService().open_gaps(batch=batch)


@router.post("/gaps/{gap_id}/confirm")
def confirm_gap(gap_id: int):
    entry, message = DeliveryGapService().confirm_gap(gap_id)
    return {"ok": entry is not None, "message": message, "entry": entry}


# ---------- 补投任务 ----------

@router.post("/gaps/{gap_id}/redelivery")
def create_redelivery(gap_id: int, payload: CreateRedeliveryPayload):
    """数据投递按缺口定位结果创建补投请求。"""
    entry, message = RedeliveryTaskService().create_from_gap(
        gap_id, create_time=payload.create_time
    )
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/redelivery/{task_id}/start")
def start_redelivery(task_id: int):
    entry, message = RedeliveryTaskService().run_action(task_id, "启动补投")
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/redelivery/{task_id}/interrupt")
def interrupt_redelivery(task_id: int, payload: InterruptPayload):
    entry, message = RedeliveryTaskService().interrupt(
        task_id, delivered_end=payload.delivered_end, event_time=payload.event_time
    )
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.get("/redelivery/{task_id}/candidates")
def retry_candidates(task_id: int, start_time: str, end_time: str):
    return RedeliveryTaskService().retry_candidates(
        task_id, start_time=start_time, end_time=end_time
    )


@router.post("/redelivery/{task_id}/retry")
def retry_redelivery(task_id: int, payload: RetryPayload):
    entry, message = RedeliveryTaskService().retry(
        task_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        event_time=payload.event_time,
    )
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/redelivery/{task_id}/complete")
def complete_redelivery(task_id: int):
    entry, message = RedeliveryTaskService().complete(task_id)
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.get("/redelivery/{task_id}/reconstruct")
def reconstruct_redelivery(task_id: int):
    """按版本及行范围复原任务实际补了哪些记录，并串联回执清单。"""
    return RedeliveryTaskService().reconstruct(task_id)
