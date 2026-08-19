"""黄金样本场景：一条确定性的完整生命周期，覆盖对外全部主要端点。

为什么是脚本而不是静态用例表：worker 协议是有状态的（拉任务拿到 task_id 与
lease_epoch，才能提交结果），必须按真实顺序串起来。

覆盖面刻意做宽——erpAPI 的端点清单尚未提供，所以「宁可多录」。
"""
from __future__ import annotations

import io
from typing import Any, Dict, List

from .harness import Recorder

WORKER_ID = "w-golden-01"
BATCH_A = "golden_batch_a"
BATCH_B = "golden_batch_b"

# 固定 ASIN，顺序即上传顺序（决定 tasks.id 与 asin_data.id 的分配顺序）
ASINS_A = ["B0GOLDEN01", "B0GOLDEN02", "B0GOLDEN03"]
ASINS_B = ["B0GOLDEN03", "B0GOLDEN04"]  # 与 A 故意重叠一个，覆盖跨批次同 ASIN


def _xlsx_bytes(rows: List[List[Any]]) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _product(asin: str, *, price: str, stock: str, title_suffix: str = "") -> Dict[str, Any]:
    """一条形状完整的采集结果。字段名对齐 ASIN_DATA_FIELDS。

    注意 crawl_time 由 worker 生成（现状是裸 UTC+8），这里固定成常量，
    好让「服务端有没有原样存下来」这件事本身可被比较。
    """
    return {
        "asin": asin,
        "title": f"Golden Test Product {asin}{title_suffix}",
        # ⚠ 副标题必须**有值**，不能留空。2026-08 Amazon 把标题拆成两段
        # （worker/parser.py:_title_differentiator），采集侧既把后半段拼进
        # title 也单独给一份。夹具留空的话这一列在基线里恒为空串，
        # 「有没有被原样存下来 / 有没有进导出」就都测不到 —— 一个恒空的列
        # 与一个根本没落库的列，在基线里长得一模一样。
        # 这正是 slow.variant.theme 那次事故的成因（夹具喂了采集侧从未产出过
        # 的形态，守卫因此一直绿着），别再犯第二次。
        "subtitle": "Snap-in Liner,Heavy Duty,Hotel Grade",
        "brand": "GoldenBrand",
        "product_type": "TestType",
        "manufacturer": "GoldenMfg",
        "model_number": f"MDL-{asin[-2:]}",
        "part_number": f"PN-{asin[-2:]}",
        "country_of_origin": "China",
        "is_customized": "No",
        "best_sellers_rank": "#123 in Test",
        "original_price": "29.99",
        "current_price": price,
        "buybox_price": price,
        "buybox_shipping": "0.00",
        "is_fba": "Yes",
        "stock_count": stock,
        "stock_status": "In Stock",
        "delivery_date": "2026-08-10",
        "delivery_time": "3 days",
        "image_urls": "https://m.media-amazon.com/images/I/71GOLDEN01._AC_SL1500_.jpg",
        "bullet_points": "point one|point two",
        "long_description": "a long description",
        "upc_list": "012345678905",
        "ean_list": "",
        "parent_asin": asin,
        "variation_asins": "",
        # ⚠ 必须用**采集侧真正产出**的形态。worker/parser.py:_parse_twister 拼的是
        #     "; ".join("%s=%s" % (dim, val) ...)  ->  "color_name=Red; size_name=L"
        # 这里以前写的是 "Color:Red"（冒号），而那个形态采集侧从未产出过。
        # 代价是真的：server/api/export_incremental.py 的 _variant() 自带一份按
        # `:` 切的解析，靠这个假夹具一直绿着，而线上 slow.variant.theme 对**所有**
        # 真实记录恒为 null。夹具不真实 = 守卫形同虚设，这是 V2（分隔符）之后的
        # 第二次同类事故。
        "variant_attributes": "color_name=Red; size_name=L",
        "root_category_id": "root123",
        "category_ids": "c1,c2",
        "category_tree": "Home > Test > Sub",
        "first_available_date": "2025-01-01",
        "package_dimensions": "10 x 5 x 2 inches",
        "package_weight": "1.5 pounds",
        "item_dimensions": "9 x 4 x 1 inches",
        "item_weight": "1.2 pounds",
        "product_url": f"https://www.amazon.com/dp/{asin}",
        "site": "US",
        "zip_code": "10001",
        "crawl_time": "2026-08-04 17:11:02",
        "rating": "4.5",
        "review_count": "128",
        "seller_id": "AGOLDENSELLER",
        "seller_name": "Golden Seller",
    }


def run(rec: Recorder) -> None:
    # ---------------- 基础只读端点（空库状态） ----------------
    rec.call("settings_initial", "GET", "/api/settings", expect=200)
    rec.call("export_fields", "GET", "/api/export/fields", expect=200)
    rec.call("batches_empty", "GET", "/api/batches", expect=200)
    rec.call("progress_empty", "GET", "/api/progress", expect=200)
    rec.call("results_empty", "GET", "/api/results", expect=200)
    rec.call("change_stats_empty", "GET", "/api/changes/stats", expect=200)
    rec.call("coordinator_empty", "GET", "/api/coordinator", expect=200)
    rec.call("workers_empty", "GET", "/api/workers", expect=200)
    rec.call("schedules_empty", "GET", "/api/schedules", expect=200)
    # 旧的 /api/auto-scrape/schedules 已删除（与 /api/schedules 读写同一份状态、
    # 只是按下标寻址），这一步改钉合并后的唯一入口。
    rec.call("schedules_empty_again", "GET", "/api/schedules", expect=200)
    rec.call("openapi_schema", "GET", "/openapi.json", expect=200)

    # ---------------- 上传批次 A（xlsx，含 per-ASIN 邮编列） ----------------
    xlsx_a = _xlsx_bytes([
        [ASINS_A[0], "10001"],
        [ASINS_A[1], "90210"],   # B 列指定邮编
        [ASINS_A[2], None],      # 留空 → 用批次邮编
    ])
    rec.call(
        "upload_batch_a", "POST", "/api/upload", expect=200,
        files={"file": ("golden_a.xlsx", xlsx_a,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_name": BATCH_A, "zip_code": "10001",
              "needs_screenshot": "false", "external_id": "ext-golden-a"},
    )

    # 同名批次重传 -> **409 Conflict**（有意的行为改动，基线已随之重录）。
    #
    # 这一步的注释以前写着「静默 no-op（INSERT OR IGNORE）」——**那是错的**，
    # Phase 4.7 已经证明撞名不是 no-op：create_batch 返回既有批次 id，
    # create_tasks 把本次的新 ASIN 悄悄并进上一个批次，而第二次的
    # external_id / callback_url 被整行丢弃（响应回显的却是请求里的值）。
    # 本轮把它改成 409：响应体带既有批次的 batch_id / batch_name / status_url，
    # 调用方直接接着轮询即可。
    #
    # ⚠ 这一步的**副作用**跟着变了，基线里能看见两处连带位移，都是这个改动的
    #   直接后果，不是新 bug：
    #     * 撞名不再走 create_tasks，于是不再烧掉 3 个 task id ——
    #       golden_batch_b 的两个任务从 id 7/8 变成 4/5，落在 pull_tasks 与
    #       pull_tasks_round2 两步上（合计 3 个值），此外无连带位移；
    #     * 批次的自增号**照样烧**（INSERT 照发、只是被 IGNORE），
    #       所以 golden_batch_b 仍然是 batch id 3，没有位移。
    rec.call(
        "upload_batch_a_duplicate", "POST", "/api/upload", expect=409,
        files={"file": ("golden_a.xlsx", xlsx_a,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_name": BATCH_A, "zip_code": "10001", "needs_screenshot": "false"},
    )

    # 上传批次 B（txt 格式，无邮编列）
    rec.call(
        "upload_batch_b", "POST", "/api/upload", expect=200,
        files={"file": ("golden_b.txt", "\n".join(ASINS_B).encode(), "text/plain")},
        data={"batch_name": BATCH_B, "zip_code": "60601", "needs_screenshot": "false"},
    )

    rec.call("batches_after_upload", "GET", "/api/batches", expect=200)
    rec.call("progress_after_upload", "GET", "/api/progress", expect=200)
    rec.call("status_batch_a", "GET", f"/api/batches/{BATCH_A}/status", expect=200)

    # ---------------- worker 心跳 ----------------
    rec.call("worker_sync", "POST", "/api/worker/sync", expect=200, json={
        "worker_id": WORKER_ID,
        "enable_screenshot": False,
        "metrics": {"success_rate": 0.98, "block_rate": 0.01, "latency_p50": 1.2,
                    "inflight": 4, "accepted": 0, "stale": 0},
    })
    rec.call("workers_after_sync", "GET", "/api/workers", expect=200)
    rec.call("coordinator_after_sync", "GET", "/api/coordinator", expect=200)

    # ---------------- 拉任务 → 批量提交结果 ----------------
    pulled = rec.call("pull_tasks", "GET", "/api/tasks/pull", expect=200, params={
        "worker_id": WORKER_ID, "count": 10, "enable_screenshot": "false",
    })
    tasks: List[Dict[str, Any]] = (pulled or {}).get("tasks", [])
    if not tasks:
        raise AssertionError("pull_tasks 返回空，场景无法继续")

    if len(tasks) < 3:
        raise AssertionError(f"场景需要至少 3 个任务，实际 {len(tasks)}")

    # 三组，各覆盖一条路径：
    #   ok_tasks     批量成功
    #   fail_task    失败
    #   lease_probe  【故意不正常提交】——留在 processing 状态，专门测 lease 门。
    #
    # 这里必须用一个仍处于 processing 的任务：lease 校验的 WHERE 同时含
    # `lease_epoch=?` 和 `status='processing'`，若拿一个已 done 的任务去试，
    # status 条件本身就会让 rowcount=0，lease 校验被完全遮蔽——变异测试证实
    # 了这一点：删掉 lease_epoch 条件，用已 done 的任务测，样本毫无反应。
    ok_tasks = tasks[:-2]
    fail_task = tasks[-2]
    lease_probe = tasks[-1]

    rec.call("submit_results_batch", "POST", "/api/tasks/result/batch", expect=200, json={
        "results": [
            {
                "task_id": t["id"], "batch_id": t["batch_id"],
                "worker_id": WORKER_ID, "lease_epoch": t["lease_epoch"],
                "success": True,
                **_product(t["asin"], price="19.99", stock="42"),
            }
            for t in ok_tasks
        ]
    })

    rec.call("submit_result_failed", "POST", "/api/tasks/result", expect=200, json={
        "task_id": fail_task["id"], "batch_id": fail_task["batch_id"],
        "worker_id": WORKER_ID, "lease_epoch": fail_task["lease_epoch"],
        "success": False,
        "error_type": "timeout", "error_detail": "golden synthetic failure",
    })

    # lease 门的双向断言，两步缺一不可：
    # (1) 过期 lease + 任务仍 processing → 必须 stale，且只可能由 lease 校验产生
    rec.call("submit_result_stale_lease", "POST", "/api/tasks/result", expect=200, json={
        "task_id": lease_probe["id"], "batch_id": lease_probe["batch_id"],
        "worker_id": WORKER_ID, "lease_epoch": lease_probe["lease_epoch"] + 99,
        "success": True,
        **_product(lease_probe["asin"], price="1.11", stock="1"),
    })
    # 该 ASIN 此刻必须【没有】被写进去——证明上一步真的被拦住了
    rec.call("result_after_stale_reject", "GET",
             f"/api/results/{lease_probe['asin']}", expect=404)
    # (2) 正确 lease 的同一任务 → 必须被受理。否则「全都拒绝」也能骗过 (1)
    rec.call("submit_result_correct_lease", "POST", "/api/tasks/result", expect=200, json={
        "task_id": lease_probe["id"], "batch_id": lease_probe["batch_id"],
        "worker_id": WORKER_ID, "lease_epoch": lease_probe["lease_epoch"],
        "success": True,
        **_product(lease_probe["asin"], price="33.50", stock="9"),
    })
    rec.call("result_after_correct_lease", "GET",
             f"/api/results/{lease_probe['asin']}", expect=200)

    # ---------------- 查询结果 ----------------
    rec.call("results_page1", "GET", "/api/results", expect=200, params={"limit": 2})
    page1 = rec.call("results_page1_again", "GET", "/api/results",
                     expect=200, params={"limit": 2})
    cursor = (page1 or {}).get("next_cursor")
    if cursor is not None:
        rec.call("results_page2", "GET", "/api/results", expect=200,
                 params={"limit": 2, "cursor": cursor})
        rec.call("results_page_prev", "GET", "/api/results", expect=200,
                 params={"limit": 2, "cursor": cursor, "direction": "prev"})

    rec.call("results_search", "GET", "/api/results", expect=200,
             params={"search": "GoldenBrand"})
    rec.call("results_search_short", "GET", "/api/results", expect=200,
             params={"search": "Go"})  # < 3 字符走 LIKE 慢路径
    rec.call("results_filter_new", "GET", "/api/results", expect=200,
             params={"change_filter": "new"})
    rec.call("result_detail", "GET", f"/api/results/{ok_tasks[0]['asin']}", expect=200)
    rec.call("result_detail_missing", "GET", "/api/results/B0NOTEXIST", expect=404)
    rec.call("change_stats", "GET", "/api/changes/stats", expect=200)

    # ---------------- 第二次采集同一 ASIN（覆盖更新语义） ----------------
    # 这正是 PG 迁移要改掉的行为：第二次观测覆盖第一次。移植阶段必须**保持不变**，
    # 事件流是另加的一层。这一步就是那条不变式的锚点。
    pulled2 = rec.call("pull_tasks_round2", "GET", "/api/tasks/pull", expect=200, params={
        "worker_id": WORKER_ID, "count": 10, "enable_screenshot": "false",
    })
    tasks2: List[Dict[str, Any]] = (pulled2 or {}).get("tasks", [])
    if tasks2:
        rec.call("submit_results_round2", "POST", "/api/tasks/result/batch",
                 expect=200, json={
                     "results": [
                         {
                             "task_id": t["id"], "batch_id": t["batch_id"],
                             "worker_id": WORKER_ID, "lease_epoch": t["lease_epoch"],
                             "success": True,
                             **_product(t["asin"], price="24.99", stock="7",
                                        title_suffix=" v2"),
                         }
                         for t in tasks2
                     ]
                 })
        rec.call("result_detail_after_round2", "GET",
                 f"/api/results/{tasks2[0]['asin']}", expect=200)

    # ---------------- 批次状态 / 错误 ----------------
    status_a_after = rec.call("status_batch_a_after", "GET",
                               f"/api/batches/{BATCH_A}/status", expect=200)
    rec.call("failures_batch_a", "GET",
             f"/api/batches/{status_a_after['batch_id']}/failures", expect=200)
    rec.call("batches_after_results", "GET", "/api/batches", expect=200)
    rec.call("progress_after_results", "GET", "/api/progress", expect=200)
    rec.call("screenshots_progress", "GET",
             f"/api/batches/{BATCH_A}/screenshots/progress", expect=200)

    # ---------------- 导出 ----------------
    rec.call("export_batch_csv", "GET", f"/api/export/{BATCH_A}",
             expect=200, params={"format": "csv"})
    rec.call("export_batch_xlsx", "GET", f"/api/export/{BATCH_A}", expect=200)
    rec.call("export_all_csv", "GET", "/api/export/all",
             expect=200, params={"format": "csv"})
    rec.call("export_selected_fields", "GET", f"/api/export/{BATCH_A}", expect=200,
             params={"format": "csv", "fields": "asin,title,current_price"})
    rec.call("export_missing_batch", "GET", "/api/export/no_such_batch",
             expect=404, params={"format": "csv"})

    # ---------------- 设置 ----------------
    rec.call("settings_update", "PUT", "/api/settings", expect=200,
             json={"zip_code": "94105"})
    rec.call("settings_after_update", "GET", "/api/settings", expect=200)

    # ---------------- 重试 / 优先级 ----------------
    rec.call("retry_batch_a", "POST", f"/api/batches/{BATCH_A}/retry", expect=200)
    rec.call("status_after_retry", "GET", f"/api/batches/{BATCH_A}/status", expect=200)

    # ---------------- 诊断（结构比较，数字忽略） ----------------
    rec.call("diagnostic", "GET", "/api/diagnostic", expect=200)
    rec.call("lock_stats", "GET", "/api/_debug/lock-stats", expect=200)

    # ---------------- HTML 页面（只验渲染得出来、没掉进错误分支） ----------------
    for page, name in (("/", "page_dashboard"), ("/tasks", "page_tasks"),
                       ("/results", "page_results"), ("/workers", "page_workers"),
                       ("/settings", "page_settings")):
        rec.call(name, "GET", page, expect=200)

    # ---------------- 删除（放最后，避免影响前面的样本） ----------------
    rec.call("delete_batch_b", "DELETE", f"/api/batches/{BATCH_B}", expect=200)
    rec.call("batches_after_delete", "GET", "/api/batches", expect=200)
    rec.call("results_final", "GET", "/api/results", expect=200)

    # ---------------- 错误路径（Phase 2.4 追加） ----------------
    # **必须留在文件末尾、`results_final` 之后。** 批次/任务的自增 id
    # （batch id 3、task id 1/3/4/5）被前面的步骤逐值钉死，插在中间会让后面
    # 每一步全漂，diff 从「纯追加」变成几百处差异。
    #
    # 选步的硬筛：新增步骤在两个后端必须逐字节相同。下面每一步都是
    # `HTTPException` 的 `{"detail": "..."}`，由 app.py 在碰任何存储之前 raise
    # （或者打的本来就是不存在的对象），与后端无关。
    # 同样的理由让它们**没有副作用**：不建批次、不落盘、不改 runtime_settings，
    # 所以放在最后也不会回头污染前 64 步。

    # 上传：文件里一个有效 ASIN 都没有 / callback_url 非法（都在 create_batch 之前）
    rec.call("upload_no_valid_asin", "POST", "/api/upload", expect=400,
             files={"file": ("golden_bad.txt", b"not-an-asin\nzzz\n", "text/plain")},
             data={"batch_name": "golden_batch_bad", "needs_screenshot": "false"})
    rec.call("upload_bad_callback_url", "POST", "/api/upload", expect=400,
             files={"file": ("golden_cb.txt", "\n".join(ASINS_A).encode(), "text/plain")},
             data={"batch_name": "golden_batch_cb", "needs_screenshot": "false",
                   "callback_url": "ftp://example.com/hook"})

    # 撞名 + **不同的 callback_url**：钉住「第二次的 callback_url 不会被静默丢弃」。
    #
    # 旧行为下这一发会拿到 200，响应里回显着这个 callback_url，而库里存的仍是
    # 批次 A 原来的值（None）—— 调用方以为回调注册好了，回调永远不触发。
    # 现在是 409，响应体里根本没有 callback_url 这个字段，不存在"回显撒谎"。
    #
    # 「确实没写进去」由**下面第 6 步**的 callback_retry_without_callback_url 兜住：
    # 它对同一个批次 A 打 callback/retry，靠的就是「批次 A 没有 callback_url」
    # 才回 400。这一发若真把 callback_url 写进去了，那一步会变成 200，基线当场红。
    #
    # 放在文件末尾的错误路径节里，是为了不扰动前面被逐值钉死的自增 id：
    # 它唯一的副作用是烧掉一个 batch 自增号，而这之后没有任何一步再读批次 id。
    # callback_url 用**公网 IP 字面量**：_is_safe_callback_url 对 IP 字面量不做
    # DNS 解析，所以这一步不碰网络、两个后端逐字节相同。
    # （文档保留段 192.0.2/198.51.100/203.0.113 在 Python 里 is_private=True，
    #   会被 SSRF 校验挡成 400，用不了。）
    # 这个 URL 永远不会被真的请求：409 意味着它压根没入库，而黄金夹具还把
    # _callback_dispatcher 整个 no-op 掉了 —— 两道，缺一道也不会往外发包。
    rec.call("upload_duplicate_name_new_callback", "POST", "/api/upload", expect=409,
             files={"file": ("golden_dup_cb.txt", "\n".join(ASINS_A).encode(),
                             "text/plain")},
             data={"batch_name": BATCH_A, "needs_screenshot": "false",
                   "callback_url": "http://8.8.8.8/golden-hook",
                   "external_id": "ext-golden-a-second"})

    # 批次不存在：四个端点各写各的 raise，所以要各录一步
    rec.call("screenshots_progress_missing_batch", "GET",
             "/api/batches/no_such_batch/screenshots/progress", expect=404)
    rec.call("status_missing_batch", "GET",
             "/api/batches/no_such_batch/status", expect=404)
    rec.call("retry_missing_batch", "POST",
             "/api/batches/no_such_batch/retry", expect=404)
    rec.call("delete_missing_batch", "DELETE",
             "/api/batches/no_such_batch", expect=404)

    # callback 重发：404（批次不存在）与 400（批次在、但没配 callback_url）
    rec.call("callback_retry_missing_batch", "POST",
             "/api/batches/no_such_batch/callback/retry", expect=404)
    rec.call("callback_retry_without_callback_url", "POST",
             f"/api/batches/{BATCH_A}/callback/retry", expect=400)

    # 批量删除的两条入参 400
    rec.call("delete_bulk_not_a_list", "POST", "/api/batches/delete-bulk",
             expect=400, json={"batch_ids": "1,2,3"})
    rec.call("delete_bulk_empty", "POST", "/api/batches/delete-bulk",
             expect=400, json={"batch_ids": ["abc", None]})

    # JSON 推送（POST /api/batches）的入参 400。
    # 前三条都在 create_batch_if_absent **之前** raise，无落盘副作用、不烧自增号。
    rec.call("json_submit_no_valid_asin", "POST", "/api/batches", expect=400,
             json={"asins": ["not-an-asin"], "batch_name": "golden_json_bad"})
    rec.call("json_submit_body_not_an_object", "POST", "/api/batches",
             expect=400, json=["B0GOLDEN01"])
    rec.call("json_submit_bad_zip", "POST", "/api/batches", expect=400,
             json={"asins": ASINS_A, "zip_code": "abcde",
                   "batch_name": "golden_json_zip"})
    # 撞名 409 必须在 JSON 这条路上也成立 —— 两个端点共用同一份实现，
    # 这一步与上面 upload_duplicate_name_new_callback 是同一个不变量的两条路。
    # 它会烧掉一个 batch 自增号（同那一步），所以同样放在这个尾节里：
    # 这之后没有任何一步再读批次 id。
    rec.call("json_submit_duplicate_name", "POST", "/api/batches", expect=409,
             json={"asins": ASINS_A, "batch_name": BATCH_A})

    # 截图查询与取图的错误路径。取图的四种结局里，200/409/410 需要真的跑完
    # 一轮采集+上传才造得出来，那些钉在 tests/test_screenshot_api.py；
    # 这里录的是不依赖采集状态的两条 400/404。
    rec.call("screenshots_without_batch_selector", "GET", "/api/screenshots",
             expect=400)
    rec.call("screenshots_missing_batch", "GET",
             "/api/screenshots?batch_name=no_such_batch", expect=404)
    rec.call("screenshot_file_missing_batch", "GET",
             "/api/screenshots/no_such_batch/B0GOLDEN01", expect=404)

    # 定时任务：创建时的两条 400。两条都在 os.makedirs/写文件之前 raise，无落盘副作用
    rec.call("schedule_bad_time_format", "POST", "/api/schedules", expect=400,
             files={"file": ("golden_sched.txt", "\n".join(ASINS_A).encode(),
                             "text/plain")},
             data={"name": "golden-sched", "time": "25:61", "interval_days": "1"})
    rec.call("schedule_interval_too_small", "POST", "/api/schedules", expect=400,
             files={"file": ("golden_sched.txt", "\n".join(ASINS_A).encode(),
                             "text/plain")},
             data={"name": "golden-sched", "time": "03:30", "interval_days": "0"})

    # 定时任务：改/删不存在的 id
    rec.call("schedule_update_missing", "PUT", "/api/schedules/no_such_schedule",
             expect=404, json={"enabled": False})
    rec.call("schedule_delete_missing", "DELETE",
             "/api/schedules/no_such_schedule", expect=404)
