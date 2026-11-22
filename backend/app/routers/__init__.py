"""业务路由汇总：专题固定路径先注册，通用模块资源路由兜底。"""
from __future__ import annotations

from app.routers import gap_router, resource

ROUTERS = [gap_router, resource]
