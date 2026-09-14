"""tests/pgdb/test_results_write.py —— agent E 的验收门。

覆盖 common/pgdb/results_write.py 的 6 个方法。黄金 64 步只走到
accept_success_result / accept_results_batch 的成功与 stale 两条主路，
下面这些分支（server_reject、失败分支的 cap、无 task_id 直写、baseline 播种、
is_auto 更新 baseline、截图路径回填、TEXT affinity）全靠本文件兜。

差分对照脚本（同一串调用在 SQLite 与 PG 上逐步比返回值 + 逐行比表）见
scratchpad/cmp_results_write.py；本文件是它的固化子集。
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from common.pgdb._shared import LOCK_STATS, _normalize_screenshot_path


# --------------------------------------------------------------------------
# media.py（agent G）尚未落地时的临时垫背：results_write 只依赖
# _get_done_screenshot_path 一个方法。media 实现后这段自动失效（不再打补丁）。
# --------------------------------------------------------------------------
async def _fallback_get_done_screenshot_path(self, asin, batch_id=None):
    queries = []
    if batch_id is not None:
        queries.append((
            "SELECT file_path FROM screenshots WHERE asin = ? AND batch_id = ? "
            "AND status = 'done' ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1",
            (asin, self.as_int(batch_id)),
        ))
    queries.append((
        "SELECT file_path FROM screenshots WHERE asin = ? AND status = 'done' "
        "ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1",
        (asin,),
    ))
    for sql, params in queries:
        async with self._db.execute(sql, params) as c:
            row = await c.fetchone()
        if row:
            fp = _normalize_screenshot_path(row["file_path"])
            if fp:
                return fp
    return None


@pytest_asyncio.fixture
async def db(pgdb):
    """pgdb，外加"media 未实现时用回退实现"的保护。"""
    try:
        await pgdb._get_done_screenshot_path("B0PROBE000", None)
    except NotImplementedError:
        import types
        pgdb._get_done_screenshot_path = types.MethodType(
            _fallback_get_done_screenshot_path, pgdb)
    return pgdb


def mk(asin, **kw):
    d = {
        "asin": asin, "title": f"Widget {asin}", "brand": "GoldenBrand",
        "current_price": "10.00", "buybox_price": "10.00",
        "stock_count": "5", "stock_status": "In Stock",
        "bullet_points": "a|b", "site": "US", "zip_code": "10001",
    }
    d.update(kw)
    return d


PARSE_FAILURE = {
    "asin": "B0BAD00001", "title": "", "brand": "", "current_price": "N/A",
    "buybox_price": "N/A", "stock_count": "", "stock_status": None,
}


async def _one_task(db, name, asin, **kw):
    bid = await db.create_batch(name, **kw)
    await db.create_tasks(bid, [asin])
    t = [x for x in await db.pull_tasks(f"w-{name}", count=50)
         if x["batch_id"] == bid][0]
    return bid, t


# ==================== save_result / save_results_batch ====================

@pytest.mark.asyncio
async def test_save_result_new_asin_seeds_all_baselines(db):
    bid = await db.create_batch("b_new")
    assert await db.save_result(mk("B0NEW00001"), bid) is True

    row = await db._write_conn.fetchrow(
        "SELECT * FROM asin_data WHERE asin='B0NEW00001'")
    # 首次入库：6 个 baseline 列全部用当前值播种（与 is_auto 无关）
    assert row["baseline_price"] == "10.00"
    assert row["baseline_buybox_price"] == "10.00"
    assert row["baseline_stock_count"] == "5"
    assert row["baseline_stock_status"] == "In Stock"
    assert row["baseline_title_bullets_hash"] == row["title_bullets_hash"]
    assert row["baseline_updated_at"] is not None
    # 时间戳必须是 TEXT '%Y-%m-%d %H:%M:%S'（app.py 会 strptime(x[:19])）
    assert isinstance(row["created_at"], str)
    assert len(row["created_at"]) == 19

    ch = await db._write_conn.fetch(
        "SELECT * FROM asin_changes WHERE asin='B0NEW00001'")
    assert [(c["change_type"], c["change_detail"], c["batch_id"]) for c in ch] == \
        [("new", "first_seen", bid)]


@pytest.mark.asyncio
async def test_save_result_without_batch_id_writes_no_new_change(db):
    assert await db.save_result(mk("B0NOBAT001")) is True
    ch = await db._write_conn.fetch(
        "SELECT 1 FROM asin_changes WHERE asin='B0NOBAT001'")
    assert ch == []


@pytest.mark.asyncio
async def test_save_result_rejects_empty_asin_and_parse_failure(db):
    assert await db.save_result({"asin": "   "}) is False
    assert await db.save_result({}) is False
    assert await db.save_result(PARSE_FAILURE) is False
    assert await db._write_conn.fetchval("SELECT COUNT(*) FROM asin_data") == 0


@pytest.mark.asyncio
async def test_save_results_batch_counts_only_saved(db):
    bid = await db.create_batch("b_bulk")
    n = await db.save_results_batch(
        [mk("B0BULK0001"), mk("B0BULK0002"), {"asin": ""}, PARSE_FAILURE], bid)
    assert n == 2
    assert await db._write_conn.fetchval("SELECT COUNT(*) FROM asin_data") == 2


# ==================== 变动检测 ====================
#
# 2026-09：`config.CHANGE_DETECTION_ENABLED` 默认已改为 **False**（本部署没有
# 消费方，代价全在写锁内）。下面这一族是**检测逻辑的既有守卫**，必须继续覆盖
# 那条路径 —— 生产上不再执行的代码如果没人测，它会在后续重构里静默腐烂，等哪天
# 想重新打开时已经坏了。
#
# 所以这里提供一个夹具把开关拨回 True，而**不是**把用例删掉或改成断言"什么都
# 没发生"。后者会让"整段检测代码被删掉"这个变更也全绿。
#
# ⚠ 故意**不用 autouse**：那会把本文件另外十几条用例（TEXT affinity / 截图路径
#   / accept_* …）也拖到非生产配置下去跑。只有真正关心检测的用例显式声明它。
#
# ⚠ `test_no_baseline_price_disables_change_detection` **也必须带上它**：那条断言
#   的是"没有 baseline 就不检测"，开关关着时它会因为**另一个**原因而通过 ——
#   一次空心的绿。带上夹具之后，它测的才是它声称在测的东西。
#
# `test_is_auto_batch_refreshes_baseline` 不带 —— baseline 的刷新在这个开关
# 之外（开关只包住"算变动"那一段），它与检测开关无关。
#
# 开关自身的行为（关着就不写、'new' 不受开关管）由
# tests/pgdb/test_change_detection_switch.py 覆盖，两个文件分工不重叠。


@pytest.fixture
def change_detection_on(monkeypatch):
    """把变动检测拨回 True（生产默认已关）。"""
    from common import config
    monkeypatch.setattr(config, "CHANGE_DETECTION_ENABLED", True)


@pytest.mark.asyncio
async def test_change_detection_against_baseline_not_current(db, change_detection_on):
    bid = await db.create_batch("b_chg")
    await db.save_result(mk("B0CHG00001"), bid)
    # 手动批次：baseline 不动，所以第二次和第三次都拿 10.00 当基准
    await db.save_result(mk("B0CHG00001", current_price="12.00",
                            stock_count="3"), bid)
    await db.save_result(mk("B0CHG00001", current_price="12.00",
                            stock_count="3"), bid)

    rows = await db._write_conn.fetch(
        "SELECT change_type, change_detail FROM asin_changes "
        "WHERE asin='B0CHG00001' AND change_type='price_stock' ORDER BY id")
    # 两次都相对 baseline=10.00 判定为 up → 两条记录（不是一条）
    assert [r["change_detail"] for r in rows] == \
        ["price:up, stock_qty:down", "price:up, stock_qty:down"]
    # baseline 未被手动采集更新
    assert await db._write_conn.fetchval(
        "SELECT baseline_price FROM asin_data WHERE asin='B0CHG00001'") == "10.00"


@pytest.mark.asyncio
async def test_title_bullets_change(db, change_detection_on):
    bid = await db.create_batch("b_tb")
    await db.save_result(mk("B0TB000001"), bid)
    await db.save_result(mk("B0TB000001", title="RENAMED",
                            bullet_points="x|y"), bid)
    r = await db._write_conn.fetchrow(
        "SELECT * FROM asin_changes WHERE asin='B0TB000001' "
        "AND change_type='title_bullets'")
    assert r["change_detail"] == "title_or_bullets_changed"
    assert r["prev_value"] == "title=Widget B0TB000001"
    assert r["new_value"] == "title=RENAMED"


@pytest.mark.asyncio
async def test_no_baseline_price_disables_change_detection(db, change_detection_on):
    """has_baseline 只看 baseline_price 一列——现状 bug，照抄。"""
    bid = await db.create_batch("b_nobl")
    await db.save_result(mk("B0NOBL0001"), bid)
    await db._write_conn.execute(
        "UPDATE asin_data SET baseline_price=NULL WHERE asin='B0NOBL0001'")
    await db.save_result(mk("B0NOBL0001", current_price="99.00"), bid)
    assert await db._write_conn.fetchval(
        "SELECT COUNT(*) FROM asin_changes WHERE asin='B0NOBL0001' "
        "AND change_type<>'new'") == 0


@pytest.mark.asyncio
async def test_is_auto_batch_refreshes_baseline(db):
    manual = await db.create_batch("b_manual")
    auto = await db.create_batch("b_auto", is_auto=True)
    await db.save_result(mk("B0AUTO0001"), manual)
    await db.save_result(mk("B0AUTO0001", current_price="77.00",
                            stock_count="2", stock_status="Out of Stock"), auto)
    r = await db._write_conn.fetchrow(
        "SELECT * FROM asin_data WHERE asin='B0AUTO0001'")
    assert r["baseline_price"] == "77.00"
    assert r["baseline_stock_count"] == "2"
    assert r["baseline_stock_status"] == "Out of Stock"
    assert r["baseline_updated_at"] is not None


# ==================== TEXT affinity ====================

@pytest.mark.asyncio
async def test_text_affinity_on_non_string_payload(db):
    """asyncpg 对 text 列不做隐式转换；SQLite 的 affinity 必须逐字复刻。"""
    bid = await db.create_batch("b_types")
    assert await db.save_result(
        mk("B0TYPE0001", review_count=128, rating=4.5, is_fba=True,
           seller_name=False, package_weight=0.1 + 0.2, item_weight=1e21), bid) is True
    r = await db._write_conn.fetchrow(
        "SELECT * FROM asin_data WHERE asin='B0TYPE0001'")
    assert r["review_count"] == "128"
    assert r["rating"] == "4.5"
    assert r["is_fba"] == "1"          # 不是 'True'
    assert r["seller_name"] == "0"     # 不是 'False'
    assert r["package_weight"] == "0.3"        # 不是 '0.30000000000000004'
    assert r["item_weight"] == "1.0e+21"       # 不是 '1e+21'


@pytest.mark.asyncio
async def test_empty_string_is_written_none_is_skipped(db):
    """``val is not None`` 才进动态列：空串会落库，None 保持旧值。"""
    bid = await db.create_batch("b_none")
    await db.save_result(mk("B0NONE0001", manufacturer="ACME"), bid)
    await db.save_result(mk("B0NONE0001", manufacturer=None,
                            model_number=""), bid)
    r = await db._write_conn.fetchrow(
        "SELECT * FROM asin_data WHERE asin='B0NONE0001'")
    assert r["manufacturer"] == "ACME"   # None 不覆盖
    assert r["model_number"] == ""       # 空串覆盖


# ==================== 截图路径 ====================

@pytest.mark.asyncio
async def test_screenshot_path_resolution_and_placeholder(db):
    bid = await db.create_batch("b_shot")
    await db._write_conn.execute(
        "INSERT INTO screenshots (asin, batch_id, status, file_path, updated_at) "
        "VALUES ('B0SHOT0001', $1, 'done', '/shots/a.png', '2026-01-01 00:00:01')",
        bid)
    await db._write_conn.execute(
        "INSERT INTO screenshots (asin, batch_id, status, file_path, updated_at) "
        "VALUES ('B0SHOT0002', $1, 'done', 'none', '2026-01-01 00:00:02')", bid)
    await db.save_result(mk("B0SHOT0001"), bid)
    await db.save_result(mk("B0SHOT0002"), bid)
    assert await db._write_conn.fetchval(
        "SELECT screenshot_path FROM asin_data WHERE asin='B0SHOT0001'") == "/shots/a.png"
    # 'none' 占位值被 _normalize_screenshot_path 归一成缺失
    assert await db._write_conn.fetchval(
        "SELECT screenshot_path FROM asin_data WHERE asin='B0SHOT0002'") is None


# ==================== accept_success_result ====================

@pytest.mark.asyncio
async def test_accept_success_result_happy_path(db):
    bid, t = await _one_task(db, "b_ok", "B0OK000001")
    r = await db.accept_success_result(t["id"], "w-b_ok", t["lease_epoch"],
                                       mk("B0OK000001"), bid)
    assert r == {"accepted": True, "saved": True}
    assert await db._write_conn.fetchval(
        "SELECT status FROM tasks WHERE id=$1", t["id"]) == "done"


@pytest.mark.asyncio
async def test_accept_success_result_stale_lease_rolls_back(db):
    bid, t = await _one_task(db, "b_stale", "B0STALE001")
    r = await db.accept_success_result(t["id"], "w-b_stale",
                                       t["lease_epoch"] + 7,
                                       mk("B0STALE001"), bid)
    assert r == {"accepted": False, "stale": True}
    # lease 门拒掉后整个事务回滚：任务仍是 processing，数据没落库
    assert await db._write_conn.fetchval(
        "SELECT status FROM tasks WHERE id=$1", t["id"]) == "processing"
    assert await db._write_conn.fetchval("SELECT COUNT(*) FROM asin_data") == 0

    # 反向断言：worker_id 不匹配同样 stale
    r2 = await db.accept_success_result(t["id"], "someone-else",
                                        t["lease_epoch"], mk("B0STALE001"), bid)
    assert r2 == {"accepted": False, "stale": True}


@pytest.mark.asyncio
async def test_accept_success_result_server_reject(db):
    bid, t = await _one_task(db, "b_rej", "B0BAD00001")
    r = await db.accept_success_result(t["id"], "w-b_rej", t["lease_epoch"],
                                       dict(PARSE_FAILURE), bid)
    assert r == {"accepted": True, "saved": False, "server_reject": True}
    row = await db._write_conn.fetchrow("SELECT * FROM tasks WHERE id=$1", t["id"])
    assert row["status"] == "failed"
    assert row["error_type"] == "server_reject"
    assert row["error_detail"] == "parse_failure_on_server"
    assert await db._write_conn.fetchval("SELECT COUNT(*) FROM asin_data") == 0


@pytest.mark.asyncio
async def test_accept_success_result_accepts_string_ids_from_json(db):
    """HTTP 边界不做校验：task_id / lease_epoch 可能是字符串。"""
    bid, t = await _one_task(db, "b_strid", "B0STR00001")
    r = await db.accept_success_result(str(t["id"]), "w-b_strid",
                                       str(t["lease_epoch"]),
                                       mk("B0STR00001"), bid)
    assert r == {"accepted": True, "saved": True}


# ==================== accept_failed_result ====================

@pytest.mark.asyncio
async def test_accept_failed_result_delegates_to_fail_task(db):
    bid, t = await _one_task(db, "b_fail", "B0FAIL0001")
    r = await db.accept_failed_result(t["id"], "w-b_fail", t["lease_epoch"],
                                      "timeout", "boom")
    assert r == {"accepted": True, "stale": False}
    # 第二次用同一 lease：任务已回 pending 且 epoch 已 bump → stale
    assert await db.accept_failed_result(
        t["id"], "w-b_fail", t["lease_epoch"], "timeout", "boom") == \
        {"accepted": False, "stale": True}


# ==================== accept_results_batch ====================

@pytest.mark.asyncio
async def test_accept_results_batch_all_branches(db):
    bid = await db.create_batch("b_batch")
    await db.create_tasks(bid, ["B0B0000001", "B0B0000002", "B0BAD00001",
                                "B0B0000004"])
    tt = {t["asin"]: t for t in await db.pull_tasks("wb", count=50)
          if t["batch_id"] == bid}
    items = [
        {"task_id": tt["B0B0000001"]["id"], "worker_id": "wb",
         "lease_epoch": tt["B0B0000001"]["lease_epoch"], "batch_id": bid,
         "data": mk("B0B0000001"), "success": True},
        {"task_id": tt["B0B0000002"]["id"], "worker_id": "wb",
         "lease_epoch": tt["B0B0000002"]["lease_epoch"] + 9, "batch_id": bid,
         "data": mk("B0B0000002"), "success": True},
        {"task_id": tt["B0BAD00001"]["id"], "worker_id": "wb",
         "lease_epoch": tt["B0BAD00001"]["lease_epoch"], "batch_id": bid,
         "data": dict(PARSE_FAILURE), "success": True},
        {"task_id": tt["B0B0000004"]["id"], "worker_id": "wb",
         "lease_epoch": tt["B0B0000004"]["lease_epoch"], "batch_id": bid,
         "data": {"error_type": "timeout", "error_detail": "t/o"},
         "success": False},
        {"worker_id": "wb", "batch_id": bid, "data": mk("B0NOTASK01"),
         "success": True},
    ]
    r = await db.accept_results_batch(items)
    # 返回值必须恰好 3 个 int 键（app.py 会 {**result, "total": n} 直接进响应体）
    # accepted = B0B0000001 + 无 task_id 直写；stale = epoch 不匹配那条；
    # failed = server_reject 那条。失败分支回 pending 不计入任何一项。
    assert r == {"accepted": 2, "stale": 1, "failed": 1}
    assert set(r) == {"accepted", "stale", "failed"}
    assert all(type(v) is int for v in r.values())

    # 失败分支未到 cap → 回 pending 且 lease_epoch 自增
    row = await db._write_conn.fetchrow(
        "SELECT * FROM tasks WHERE id=$1", tt["B0B0000004"]["id"])
    assert row["status"] == "pending"
    assert row["retry_count"] == 1
    assert row["lease_epoch"] == tt["B0B0000004"]["lease_epoch"] + 1
    assert row["worker_id"] is None
    # stale 的那条保持 processing
    assert await db._write_conn.fetchval(
        "SELECT status FROM tasks WHERE id=$1",
        tt["B0B0000002"]["id"]) == "processing"


@pytest.mark.asyncio
async def test_accept_results_batch_failure_hits_cap(db):
    """variant_offset 的 _fail_cap 是 1 → 一次就终态 failed。"""
    bid, t = await _one_task(db, "b_cap", "B0CAP00001")
    r = await db.accept_results_batch([
        {"task_id": t["id"], "worker_id": "w-b_cap",
         "lease_epoch": t["lease_epoch"], "batch_id": bid,
         "data": {"error_type": "variant_offset", "error_detail": "vo"},
         "success": False}])
    assert r == {"accepted": 0, "stale": 0, "failed": 1}
    row = await db._write_conn.fetchrow("SELECT * FROM tasks WHERE id=$1", t["id"])
    assert row["status"] == "failed"
    assert row["error_type"] == "variant_offset"


@pytest.mark.asyncio
async def test_accept_results_batch_failure_stale(db):
    bid, t = await _one_task(db, "b_fstale", "B0FST00001")
    r = await db.accept_results_batch([
        {"task_id": t["id"], "worker_id": "w-b_fstale",
         "lease_epoch": t["lease_epoch"] + 3, "batch_id": bid,
         "data": {"error_type": "timeout"}, "success": False}])
    assert r == {"accepted": 0, "stale": 1, "failed": 0}


@pytest.mark.asyncio
async def test_accept_results_batch_empty(db):
    assert await db.accept_results_batch([]) == \
        {"accepted": 0, "stale": 0, "failed": 0}


@pytest.mark.asyncio
async def test_accept_results_batch_item_order_decides_last_writer(db):
    """同一 ASIN 出现两次时，最后一条赢——**不要**给 items 排序。"""
    bid = await db.create_batch("b_dup")
    await db.create_tasks(bid, ["B0DUP00001", "B0DUP00002"])
    tt = {t["asin"]: t for t in await db.pull_tasks("wd", count=50)
          if t["batch_id"] == bid}
    r = await db.accept_results_batch([
        {"task_id": tt["B0DUP00001"]["id"], "worker_id": "wd",
         "lease_epoch": tt["B0DUP00001"]["lease_epoch"], "batch_id": bid,
         "data": mk("B0SAME0001", current_price="1.00"), "success": True},
        {"task_id": tt["B0DUP00002"]["id"], "worker_id": "wd",
         "lease_epoch": tt["B0DUP00002"]["lease_epoch"], "batch_id": bid,
         "data": mk("B0SAME0001", current_price="2.00"), "success": True},
    ])
    assert r == {"accepted": 2, "stale": 0, "failed": 0}
    assert await db._write_conn.fetchval(
        "SELECT current_price FROM asin_data WHERE asin='B0SAME0001'") == "2.00"
    # 首次插入播种 baseline=1.00，第二条相对它判定 price:up
    assert await db._write_conn.fetchval(
        "SELECT baseline_price FROM asin_data WHERE asin='B0SAME0001'") == "1.00"


# ==================== 锁仪表（黄金 step 56 钉死的 key 集合）====================

@pytest.mark.asyncio
async def test_lock_stats_keys_are_recorded(db):
    """accept_results_batch 必须打出 4 个 stage 名和 accept_results_batch 这个 caller。"""
    for k in ("waits", "holds", "stage_timings"):
        LOCK_STATS[k].clear()
    bid, t = await _one_task(db, "b_lock", "B0LOCK0001")
    await db.accept_results_batch([
        {"task_id": t["id"], "worker_id": "w-b_lock",
         "lease_epoch": t["lease_epoch"], "batch_id": bid,
         "data": mk("B0LOCK0001"), "success": True}])
    assert "accept_results_batch" in LOCK_STATS["waits"]
    assert "accept_results_batch" in LOCK_STATS["holds"]
    assert {"update_tasks_lease", "save_result", "commit", "total_in_lock"} \
        <= set(LOCK_STATS["stage_timings"])

    # 裸 _write_lock 的调用点归到 'other'
    for k in ("waits", "holds"):
        LOCK_STATS[k].clear()
    await db.save_result(mk("B0LOCK0002"), bid)
    assert "other" in LOCK_STATS["waits"]
