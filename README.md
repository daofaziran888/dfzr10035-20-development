# 网关工坊 · API接口采集与数据网关平台

面向第三方API接入注册、接口鉴权编排、限流熔断、响应解析、schema映射、结果缓存、重放恢复与交付订阅的API接口采集与数据网关平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 接口提供方 | `api_provider` | API提供方 | 提供方编号、提供方名称、认证方式 |
| 鉴权配置 | `auth_profile` | 鉴权方案 | 方案编号、提供方、鉴权类型 |
| 接口定义 | `endpoint_def` | 接口端点 | 端点编号、提供方、请求路径 |
| 限流熔断 | `rate_limit` | 限流策略 | 策略编号、目标端点、QPS上限 |
| 请求批次 | `request_batch` | 调用批次 | 批次编号、关联端点、参数模板 |
| 响应解析 | `response_parse` | 解析规则 | 规则编号、目标端点、JSON路径 |
| Schema差异 | `schema_diff` | 差异记录 | 记录编号、关联端点、变更字段 |
| 结果缓存 | `data_cache` | 缓存策略 | 策略编号、目标端点、缓存TTL |
| 重放队列 | `retry_queue` | 重放任务 | 任务编号、失败端点、失败原因 |
| 数据投递 | `data_delivery` | 投递管道 | 管道编号、数据来源、目标地址 |
| 订阅管理 | `subscriber` | 订阅方 | 订阅编号、订阅方、数据主题 |
| 接口监控 | `api_monitor` | 监控指标 | 指标编号、目标端点、监控类型 |
| Webhook路由 | `webhook_router` | 回调规则 | 规则编号、触发事件、目标URL |
| 传输加密 | `data_encrypt` | 加密配置 | 配置编号、数据范围、加密算法 |
| 调用日志 | `call_log` | 调用记录 | 记录编号、接口端点、请求参数 |
| 数据版本 | `data_version` | 数据快照 | 版本编号、数据来源、行数统计 |
| 管道审计 | `pipeline_audit` | 审计记录 | 记录编号、管道名称、操作人 |
| 治理报告 | `govern_report` | 治理报告 | 报告编号、评估范围、合规指标 |
| 增量追踪 | `incr_tracker` | 增量标记 | 标记编号、目标端点、增量字段 |
| 抽样校验 | `sample_check` | 抽样计划 | 计划编号、目标端点、抽样比例 |
| 用量计费 | `cost_account` | 用量账单 | 账单编号、提供方、计费周期 |
| 任务调度 | `task_schedule` | 调度计划 | 计划编号、关联端点、Cron表达式 |
| 投递回执 | `delivery_receipt` | 投递回执 | 回执编号、接收批次、数据段、起始行、结束行 |
| 投递缺口定位 | `delivery_gap` | 投递缺口 | 缺口编号、接收批次、数据段、缺失行范围、候选快照切片 |
| 补投任务 | `redelivery_task` | 补投任务 | 任务编号、关联缺口、计划行范围、已补投行范围、重试次数 |

## 投递缺口定位专题（采集快照 × 投递回执 → 补投闭环）

`/api/gap/*` 下的固定路径把采集快照检索、回执确认、缺口定位与补投任务串成闭环，
领域逻辑分别落在 `data_version`、`delivery_receipt`、`delivery_gap`、`redelivery_task` 四个服务：

| 接口 | 作用 |
| --- | --- |
| `GET /api/gap/snapshots` | 快照检索清单：按批次/来源/标签查询，含每条快照的已确认行范围与未交付行数 |
| `GET /api/gap/snapshots/candidates` | 按时间范围检索可补投候选版本（补投中断后重试入口） |
| `POST /api/gap/snapshots` | 登记采集快照（含迟到的正式版本），标签相同的快照凭原始行范围区分 |
| `POST /api/gap/receipts/{id}/confirm` | 回执确认，结果同步到快照检索清单 |
| `POST /api/gap/locate` | 按接收批次关联快照与回执，产出缺口定位结果（批次+数据段唯一，已确认段冻结） |
| `POST /api/gap/gaps/{id}/redelivery` | 数据投递按缺口定位结果创建补投请求 |
| `POST /api/gap/redelivery/{id}/interrupt` | 补投中断，按实际完成行切割并生成部分回执 |
| `POST /api/gap/redelivery/{id}/retry` | 按时间范围重新检索候选版本后重试 |
| `GET /api/gap/redelivery/{id}/reconstruct` | 按版本及行范围复原任务实际补了哪些记录，串联回执清单 |

端到端验证脚本：`e2e/gap_delivery_e2e.py`（标准库实现，`python -I` 运行）。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
