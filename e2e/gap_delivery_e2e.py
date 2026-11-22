"""投递缺口定位专题端到端验证（标准库 urllib，python -I 运行）。

覆盖题面全部约束：
A. 按接收批次定位缺口（采集快照 × 投递回执关联），缺口按批次+数据段唯一；
B. 提交前正式版本迟到，只覆盖未确认部分，已确认数据段与源快照不被后续查询覆盖；
C. 标签相同的不同快照凭原始行范围区分；
D. 补投中断后按时间范围检索候选版本并重试；
E. 回执确认同步到快照检索清单；
F. 补投任务按缺口创建；按版本+行范围复原实际补投记录（B002：两段数据、正式快照晚到）。
"""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api"
PASS = 0
FAIL = 0


def call(method: str, path: str, payload=None):
    if "?" in path:
        head, query = path.split("?", 1)
        path = head + "?" + urllib.parse.quote(query, safe="=&%:/,")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check(name: str, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def find(items, **kw):
    for item in items:
        if all(item.get(k) == v for k, v in kw.items()):
            return item
    return None


print("== 场景一 BATCH-B001：三段数据，第一段已确认，正式版本迟到 ==")

# 1. 快照检索清单（回执确认结果同步视图）
snap_list = call("GET", "/gap/snapshots?batch=BATCH-B001")
check("快照清单返回4条B001快照（3临时+1正式）", snap_list["total"] == 4, snap_list["total"])
s1 = find(snap_list["items"], **{"版本编号": "SNAP-B001-TEMP-1"})
check("已确认快照显示已确认行范围1-1000", s1["已确认行范围"] == "1-1000" and s1["未交付行数"] == 0, s1)
s2 = find(snap_list["items"], **{"版本编号": "SNAP-B001-TEMP-2"})
check("未投递快照未交付行数=1000", s2["未交付行数"] == 1000 and s2["已确认行范围"] == "", s2)
labels = {item["版本编号"]: item["原始行范围"] for item in snap_list["items"]}
check("相同标签batch_B001的不同快照原始行范围可区分",
      labels["SNAP-B001-TEMP-1"] == "1-1000"
      and labels["SNAP-B001-TEMP-2"] == "1001-2000"
      and labels["SNAP-B001-TEMP-3"] == "2001-2800", labels)

# 2. 按接收批次定位缺口（期望三段，回执只确认了第一段）
located = call("POST", "/gap/locate", {
    "batch": "BATCH-B001", "receiver": "华东数据仓", "source": "订单中心/order_api",
    "locate_time": "2026-11-22 09:40",
    "segments": [
        {"数据段": "order_segment_1", "起始行": 1, "结束行": 1000,
         "字段范围": "order_id,customer_id,amount,status"},
        {"数据段": "order_segment_2", "起始行": 1001, "结束行": 2000,
         "字段范围": "order_id,customer_id,amount,status"},
        {"数据段": "order_segment_3", "起始行": 2001, "结束行": 2800,
         "字段范围": "order_id,customer_id,amount,status"},
    ],
})
check("定位返回3个数据段", located["located"] == 3, located)
g1 = find(located["items"], **{"数据段": "order_segment_1"})
g2 = find(located["items"], **{"数据段": "order_segment_2"})
g3 = find(located["items"], **{"数据段": "order_segment_3"})
check("第一段无缺失直接已确认", g1["缺失行数"] == 0 and g1["status"] == "已确认" and g1["已冻结"] is True, g1)
check("第二段缺失1001-2000", g2["缺失行范围"] == "1001-2000" and g2["缺失行数"] == 1000, g2)
check("第三段缺失2001-2800", g3["缺失行范围"] == "2001-2800" and g3["缺失行数"] == 800, g3)
check("第二段候选切片绑定临时快照TEMP-2",
      g2["候选快照切片"][0]["版本编号"] == "SNAP-B001-TEMP-2", g2["候选快照切片"])

# 3. 缺口唯一性：重复定位不产生重复缺口
again = call("POST", "/gap/locate", {
    "batch": "BATCH-B001", "receiver": "华东数据仓", "source": "订单中心/order_api",
    "segments": [
        {"数据段": "order_segment_1", "起始行": 1, "结束行": 1000},
        {"数据段": "order_segment_2", "起始行": 1001, "结束行": 2000},
        {"数据段": "order_segment_3", "起始行": 2001, "结束行": 2800},
    ],
})
gaps_view = call("GET", "/gap/gaps?batch=BATCH-B001")
check("重复定位后缺口仍只有3条（批次+数据段唯一）", gaps_view["total"] == 3, gaps_view["total"])
g1_re = find(again["items"], **{"数据段": "order_segment_1"})
check("已确认缺口重复定位保持冻结", g1_re["已冻结"] is True and g1_re["缺口编号"] == g1["缺口编号"], g1_re)

# 4. 正式版本迟到：覆盖第二段 1001-2000（在补投提交前登记）
late = call("POST", "/gap/snapshots", {"values": {
    "版本编号": "SNAP-B001-FORMAL-2", "数据来源": "订单中心/order_api", "行数统计": 1000,
    "版本标签": "batch_B001", "接收批次": "BATCH-B001", "采集快照标签": "batch_B001",
    "快照类型": "正式版本", "快照时间": "2026-11-22 09:55", "创建时间": "2026-11-22 09:55",
    "原始行起始": 1001, "原始行结束": 2000, "Schema摘要": "order-schema-v2", "创建原因": "迟到正式版本",
}})
check("迟到正式快照登记成功", late["ok"] and late["entry"]["status"] == "可用", late)

relocated = call("POST", "/gap/locate", {
    "batch": "BATCH-B001", "receiver": "华东数据仓", "source": "订单中心/order_api",
    "locate_time": "2026-11-22 09:58",
    "segments": [
        {"数据段": "order_segment_1", "起始行": 1, "结束行": 1000},
        {"数据段": "order_segment_2", "起始行": 1001, "结束行": 2000},
        {"数据段": "order_segment_3", "起始行": 2001, "结束行": 2800},
    ],
})
g2_new = find(relocated["items"], **{"数据段": "order_segment_2"})
g3_new = find(relocated["items"], **{"数据段": "order_segment_3"})
g1_new = find(relocated["items"], **{"数据段": "order_segment_1"})
check("未确认缺口改由迟到正式快照承载",
      g2_new["候选快照切片"][0]["版本编号"] == "SNAP-B001-FORMAL-2"
      and g2_new["候选快照切片"][0]["快照类型"] == "正式版本", g2_new)
check("正式快照不覆盖第三段（行范围不重叠，仍用TEMP-3）",
      g3_new["候选快照切片"][0]["版本编号"] == "SNAP-B001-TEMP-3", g3_new)
check("已确认第一段源快照不受迟到版本影响",
      g1_new["已冻结"] is True and g1_new["缺失行数"] == 0, g1_new)

# 5. 按缺口创建补投任务（第二段），中断后按时间范围重试
gap2_id = g2_new["id"]
created = call("POST", f"/gap/gaps/{gap2_id}/redelivery", {"create_time": "2026-11-22 10:00"})
check("按缺口定位结果创建补投请求", created["ok"] and created["entry"]["任务切片"][0]["版本编号"] == "SNAP-B001-FORMAL-2", created)
task1_id = created["entry"]["id"]
call("POST", f"/gap/redelivery/{task1_id}/start", {})
interrupted = call("POST", f"/gap/redelivery/{task1_id}/interrupt",
                   {"delivered_end": 1500, "event_time": "2026-11-22 10:20"})
check("中断后任务状态=重试中，剩余1501-2000",
      interrupted["entry"]["status"] == "重试中"
      and interrupted["entry"]["剩余行范围"] == "1501-2000"
      and interrupted["entry"]["已补投行范围"] == "1001-1500", interrupted["entry"])

# 中断产生的回执（待确认），确认后同步快照清单
receipts = call("GET", "/gap/receipts/summary?batch=BATCH-B001")
check("回执汇总含两个数据段", len(receipts["segments"]) == 2, receipts)
pending_receipts = call("GET", "/delivery_receipt?status=待确认")
rid = pending_receipts["items"][0]["id"]
confirmed = call("POST", f"/gap/receipts/{rid}/confirm", {})
check("回执确认成功并同步快照检索清单", confirmed["ok"], confirmed)
snap_again = call("GET", "/gap/snapshots?batch=BATCH-B001")
formal2 = find(snap_again["items"], **{"版本编号": "SNAP-B001-FORMAL-2"})
check("正式快照2回执状态同步为已确认", formal2["回执状态"] == "已确认", formal2)

# 重试：按时间范围检索候选版本（窗口包含迟到正式版本）
preview = call("GET", f"/gap/redelivery/{task1_id}/candidates?start_time=2026-11-20%2000:00&end_time=2026-11-22%2023:59")
codes = [item["版本编号"] for item in preview["items"]]
check("时间范围候选检索包含迟到正式版本", "SNAP-B001-FORMAL-2" in codes, codes)
retried = call("POST", f"/gap/redelivery/{task1_id}/retry",
               {"start_time": "2026-11-20 00:00", "end_time": "2026-11-22 23:59",
                "event_time": "2026-11-22 11:00"})
check("重试后恢复补投中，剩余切片仍由正式快照承载",
      retried["entry"]["status"] == "补投中"
      and retried["entry"]["重试次数"] == 1
      and any(s["版本编号"] == "SNAP-B001-FORMAL-2" for s in retried["entry"]["任务切片"]),
      retried["entry"])
completed = call("POST", f"/gap/redelivery/{task1_id}/complete", {"event_time": "2026-11-22 12:00"})
check("补投完成", completed["entry"]["status"] == "已完成", completed["entry"])
recon1 = call("GET", f"/gap/redelivery/{task1_id}/reconstruct")
check("复原：正式快照2实际补投1001-2000共1000行",
      len(recon1["实际补投记录"]) == 1
      and recon1["实际补投记录"][0]["版本编号"] == "SNAP-B001-FORMAL-2"
      and recon1["实际补投记录"][0]["行范围"] == ["1001-2000"]
      and recon1["总补投行数"] == 1000, recon1)
check("复原结果串联两条回执（中断+完成）", len(recon1["回执清单"]) == 2, recon1["回执清单"])

print()
print("== 场景二 BATCH-B002：同一接收方两段数据，一段需补投，按时间搜索时正式快照晚到 ==")

# 两段：segment_a 已全量确认；segment_b 全缺，需要补投
seed_receipt = call("POST", "/delivery_receipt", {"values": {
    "回执编号": "RECP-B002-01", "接收批次": "BATCH-B002", "接收方": "华南数据仓",
    "数据段": "inv_segment_a", "源快照编号": "SNAP-B002-TEMP-1", "版本编号": "SNAP-B002-TEMP-1",
    "起始行": 1, "结束行": 500, "字段范围": "sku_id,stock,warehouse",
    "回执时间": "2026-11-21 11:00", "确认结果": "A段全部入库",
}})
rid_a = seed_receipt["entry"]["id"]
call("POST", f"/gap/receipts/{rid_a}/confirm", {})

loc2 = call("POST", "/gap/locate", {
    "batch": "BATCH-B002", "receiver": "华南数据仓", "source": "库存中心/inventory_api",
    "locate_time": "2026-11-22 14:00",
    "segments": [
        {"数据段": "inv_segment_a", "起始行": 1, "结束行": 500, "字段范围": "sku_id,stock,warehouse"},
        {"数据段": "inv_segment_b", "起始行": 501, "结束行": 1000, "字段范围": "sku_id,stock,warehouse"},
    ],
})
ga = find(loc2["items"], **{"数据段": "inv_segment_a"})
gb = find(loc2["items"], **{"数据段": "inv_segment_b"})
check("A段无需补投（已确认）", ga["status"] == "已确认" and ga["缺失行数"] == 0, ga)
check("B段定位为缺口501-1000", gb["缺失行范围"] == "501-1000" and gb["缺失行数"] == 500, gb)

# B段快照：先按较早时间窗只看到临时版本；正式版本时间更晚
early_window = call("GET", "/gap/snapshots/candidates?start_time=2026-11-21%2000:00&end_time=2026-11-21%2010:00&batch=BATCH-B002")
check("早时间窗只检索到临时快照",
      [i["版本编号"] for i in early_window["items"]] == ["SNAP-B002-TEMP-1"], early_window)
full_window = call("GET", "/gap/snapshots/candidates?start_time=2026-11-21%2000:00&end_time=2026-11-21%2023:59&batch=BATCH-B002")
check("完整时间窗中正式快照晚于临时快照",
      [i["版本编号"] for i in full_window["items"]] == ["SNAP-B002-TEMP-1", "SNAP-B002-FORMAL-1"],
      full_window["items"])

# 正式快照范围仅1-500，无法覆盖501-1000 -> 缺口候选标记不可补投，需扩大正式快照后重试
gapb_id = gb["id"]
no_cover = call("POST", f"/gap/gaps/{gapb_id}/redelivery", {})
check("无快照覆盖时拒绝创建补投请求", no_cover["ok"] is False, no_cover)

# 迟到的正式快照补齐 B 段（再次迟到：正式版本覆盖临时版本对应部分数据）
late2 = call("POST", "/gap/snapshots", {"values": {
    "版本编号": "SNAP-B002-FORMAL-2", "数据来源": "库存中心/inventory_api", "行数统计": 500,
    "版本标签": "batch_B002", "接收批次": "BATCH-B002", "采集快照标签": "batch_B002",
    "快照类型": "正式版本", "快照时间": "2026-11-22 14:20", "创建时间": "2026-11-22 14:20",
    "原始行起始": 501, "原始行结束": 1000, "Schema摘要": "inventory-schema-v1", "创建原因": "B段迟到正式版本",
}})
check("B段迟到正式快照登记成功", late2["ok"], late2)
loc2b = call("POST", "/gap/locate", {
    "batch": "BATCH-B002", "receiver": "华南数据仓", "source": "库存中心/inventory_api",
    "segments": [
        {"数据段": "inv_segment_a", "起始行": 1, "结束行": 500},
        {"数据段": "inv_segment_b", "起始行": 501, "结束行": 1000},
    ],
})
gb2 = find(loc2b["items"], **{"数据段": "inv_segment_b"})
check("重新定位后B段由迟到正式快照承载",
      gb2["候选快照切片"][0]["版本编号"] == "SNAP-B002-FORMAL-2", gb2)
check("A段已确认源快照仍冻结（TEMP-1不受正式版本影响）",
      find(loc2b["items"], **{"数据段": "inv_segment_a"})["已冻结"] is True, loc2b)

created2 = call("POST", f"/gap/gaps/{gb2['id']}/redelivery", {"create_time": "2026-11-22 14:30"})
check("B段补投任务创建成功", created2["ok"], created2)
task2_id = created2["entry"]["id"]
call("POST", f"/gap/redelivery/{task2_id}/start", {})
# 中断在 700 行（相对原始行 501-1000）
it2 = call("POST", f"/gap/redelivery/{task2_id}/interrupt",
           {"delivered_end": 700, "event_time": "2026-11-22 14:45"})
check("B段中断：已补501-700，剩余701-1000",
      it2["entry"]["已补投行范围"] == "501-700" and it2["entry"]["剩余行范围"] == "701-1000", it2["entry"])
rt2 = call("POST", f"/gap/redelivery/{task2_id}/retry",
           {"start_time": "2026-11-21 00:00", "end_time": "2026-11-22 23:59",
            "event_time": "2026-11-22 15:10"})
check("B段重试恢复", rt2["entry"]["status"] == "补投中", rt2["entry"])
done2 = call("POST", f"/gap/redelivery/{task2_id}/complete", {"event_time": "2026-11-22 15:30"})
check("B段补投完成", done2["entry"]["status"] == "已完成", done2["entry"])
recon2 = call("GET", f"/gap/redelivery/{task2_id}/reconstruct")
check("复原B段：正式快照2补501-1000共500行，回执2条",
      recon2["总补投行数"] == 500
      and recon2["实际补投记录"][0]["版本编号"] == "SNAP-B002-FORMAL-2"
      and recon2["实际补投记录"][0]["行范围"] == ["501-1000"]
      and len(recon2["回执清单"]) == 2, recon2)

# 任务与回执联合视图：通用资源接口也能查到
tasks = call("GET", "/redelivery_task")
check("补投任务列表共2条且均已完成",
      tasks["total"] == 2 and all(i["status"] == "已完成" for i in tasks["items"]), tasks["total"])
gaps_final = call("GET", "/gap/gaps/open")
check("开放缺口仅剩未安排补投的B001第三段",
      gaps_final["total"] == 1
      and gaps_final["items"][0]["数据段"] == "order_segment_3", gaps_final)

print()
print(f"==== 结果：{PASS} 通过 / {FAIL} 失败 ====")
raise SystemExit(1 if FAIL else 0)
