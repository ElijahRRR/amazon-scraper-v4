"""tests/pgdb/test_write_hooks.py —— Phase 2 事件流**写钩子**的验收门。

守的是设计规格 §1（钩子位置 / body 内容）与 §2（outcome 分类学 + §2.5 的不变式）。
relay 自己的行为（认领、seq 单调、分区、死信）由 tests/pgdb/test_relay.py 守，
这里一律只看 `scraper.scrape_outbox`。

**黄金结构性看不到这里的任何一条**：TestClient 严格顺序、4 个后台协程被 no-op，
而这些用例要的是"注入一次失败之后事务里还剩什么""同一条任务的多次终态尝试"
这类状态。Phase 1 对 F1/F2/F3 也是同样的处境。
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from common.pgdb.outbox import PAYLOAD_STATS, json_safe, safe_payload

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# 夹具与小工具
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(pgdb):
    """已经跑过 init_event_stream 的 pgdb（connect() 里就跑过了，这里只断言）。"""
    assert pgdb._ev()["ready"], "init_event_stream 应该在 connect() 期就跑过"
    return pgdb


def mk(asin, **kw):
    """一条形状完整的采集结果（字段名对齐 ASIN_DATA_FIELDS）。"""
    d = {
        "asin": asin, "title": f"Widget {asin}", "brand": "GoldenBrand",
        "current_price": "10.00", "buybox_price": "10.00",
        "stock_count": "5", "stock_status": "In Stock",
        "bullet_points": "a|b", "site": "US", "zip_code": "10001",
        "crawl_time": "2026-08-04 17:11:02",
    }
    d.update(kw)
    return d


async def outbox(db):
    """outbox 里的全部 body，按 id 升序。"""
    rows = await db._write_conn.fetch(
        "SELECT id, enqueued_at, body FROM scraper.scrape_outbox ORDER BY id")
    return [json.loads(r["body"]) for r in rows]


async def outbox_count(db) -> int:
    return await db._write_conn.fetchval("SELECT count(*) FROM scraper.scrape_outbox")


async def one_task(db, name, asin, zip_code="10001"):
    bid = await db.create_batch(name)
    await db.create_tasks(bid, [asin], zip_code=zip_code)
    t = [x for x in await db.pull_tasks(f"w-{name}", count=50)
         if x["batch_id"] == bid][0]
    return bid, t


# ==========================================================================
# 1) 原子性 —— 本阶段最硬的一条要求
# ==========================================================================

async def test_result_committed_implies_exactly_one_outbox_row(db):
    """正向：结果提交成功 ⇒ 恰好一行 outbox，且与结果/租约**同一个事务**。

    xmin 相同就是同事务证书：PG 给每一行记下写它的那个事务 id。
    """
    bid, t = await one_task(db, "atomic_ok", "B0ATOMIC01")
    r = await db.accept_success_result(t["id"], "w-atomic_ok", t["lease_epoch"],
                                       mk("B0ATOMIC01"), bid)
    assert r == {"accepted": True, "saved": True}
    assert await outbox_count(db) == 1

    xmins = await db._write_conn.fetchrow("""
        SELECT (SELECT xmin::text FROM scraper.scrape_outbox LIMIT 1) AS ob,
               (SELECT xmin::text FROM asin_data WHERE asin = 'B0ATOMIC01') AS ad,
               (SELECT xmin::text FROM tasks WHERE id = $1)                 AS tk
    """, t["id"])
    assert xmins["ob"] == xmins["ad"] == xmins["tk"], dict(xmins)


async def test_rollback_after_result_write_leaves_no_outbox_row(db):
    """反向：钩子发过之后再让写入失败 ⇒ outbox 一行不剩，任务退回 processing。

    注入点选在钩子**之后**：把 `_get_done_screenshot_path` 换成会抛的版本，
    它正好是 §1.1 位置之后的第一个 await。
    """
    bid, t = await one_task(db, "atomic_fail", "B0ATOMIC02")

    import types
    orig = db._get_done_screenshot_path

    async def boom(self, asin, batch_id=None):
        raise RuntimeError("boom after hook")

    db._get_done_screenshot_path = types.MethodType(boom, db)
    try:
        with pytest.raises(RuntimeError, match="boom after hook"):
            await db.accept_success_result(t["id"], "w-atomic_fail",
                                           t["lease_epoch"], mk("B0ATOMIC02"), bid)
    finally:
        db._get_done_screenshot_path = orig

    assert await outbox_count(db) == 0, "钩子发了，结果写失败了，事件必须一起消失"
    row = await db._write_conn.fetchrow(
        "SELECT status FROM tasks WHERE id = $1", t["id"])
    assert row["status"] == "processing", "租约 UPDATE 也必须一起回滚"
    assert await db._write_conn.fetchval(
        "SELECT count(*) FROM asin_data WHERE asin = 'B0ATOMIC02'") == 0

    # 同一条任务重放一次：这回该成功，且**只有一行**事件（不是两行）。
    r = await db.accept_success_result(t["id"], "w-atomic_fail", t["lease_epoch"],
                                       mk("B0ATOMIC02"), bid)
    assert r == {"accepted": True, "saved": True}
    assert await outbox_count(db) == 1


async def test_hook_fires_before_the_cross_time_merge(db):
    """payload 是"这一次采集"，不是 asin_data 里那条跨时间合并出来的行。

    第一次带 seller_id，第二次不带：库里的行会把 seller_id 结转下来，
    而第二条事件的 payload 里**根本没有这个键**（缺席 ≠ null ≠ 旧值）。
    """
    bid, t = await one_task(db, "merge", "B0MERGE001")
    await db.accept_success_result(t["id"], "w-merge", t["lease_epoch"],
                                   mk("B0MERGE001", seller_id="ASELLER1",
                                      rating="4.5"), bid)
    # 第二次采集要另起一个批次（tasks 上 (batch_id, asin) 唯一）
    bid2, t2 = await one_task(db, "merge2", "B0MERGE001")
    await db.accept_success_result(t2["id"], "w-merge2", t2["lease_epoch"],
                                   mk("B0MERGE001", current_price="99.00"), bid2)

    bodies = await outbox(db)
    assert len(bodies) == 2
    assert bodies[0]["result"]["seller_id"] == "ASELLER1"
    assert "seller_id" not in bodies[1]["result"], "事件不许回读 asin_data"
    assert "rating" not in bodies[1]["result"]
    # 库里那一行确实结转了 —— 这正是事件不能从库里取值的原因。
    row = await db._write_conn.fetchrow(
        "SELECT seller_id, current_price FROM asin_data WHERE asin='B0MERGE001'")
    assert row["seller_id"] == "ASELLER1"
    assert row["current_price"] == "99.00"
    # 钩子在两处 hash 赋值之后：body 带的 hash 与落库的行一致。
    assert bodies[1]["result"]["content_hash"] == await db._write_conn.fetchval(
        "SELECT content_hash FROM asin_data WHERE asin='B0MERGE001'")


# ==========================================================================
# 2) outcome 分类学（规格 §2.1 / §2.2 / §2.4）
# ==========================================================================

async def test_success_is_ok(db):
    bid, t = await one_task(db, "ok", "B0OKOK0001")
    await db.accept_success_result(t["id"], "w-ok", t["lease_epoch"],
                                   mk("B0OKOK0001"), bid)
    b = (await outbox(db))[0]
    assert b["outcome"] == "ok"
    assert b["asin"] == "B0OKOK0001"
    assert b["marketplace"] == "amazon.com"      # 绝不是 payload 里的 site="US"
    assert b["result"]["site"] == "US"           # 但 payload 原样保留
    assert b["task_id"] == t["id"] and b["batch_id"] == bid
    assert b["worker_id"] == "w-ok"
    assert b["lease_epoch"] == t["lease_epoch"]
    assert b["attempt"] == 0
    assert b["error_type"] is None and b["error_detail"] is None
    assert b["source_id"].startswith(db.event_gen + ":")
    assert b["gen"] == db.event_gen


async def test_404_sentinel_title_is_not_found(db):
    """404 分支：worker 用 success=True 提交 `[商品不存在]`，且过不了
    `_is_parse_failure`（stock_count='0' 不在 _NA_VALUES 里）。"""
    bid, t = await one_task(db, "nf", "B0NOTFOUND")
    data = mk("B0NOTFOUND", title="[商品不存在]", brand="N/A",
              current_price="N/A", buybox_price="N/A", stock_count="0",
              stock_status="N/A")
    r = await db.accept_success_result(t["id"], "w-nf", t["lease_epoch"], data, bid)
    assert r["saved"] is True, "这条确实被写进了 asin_data（占位符覆盖）"
    assert (await outbox(db))[0]["outcome"] == "not_found"


async def test_real_title_with_bracket_is_still_ok(db):
    """`[2-Pack] Storage Bins` 是真标题：全等匹配，绝不能 startswith('[')。"""
    bid, t = await one_task(db, "brk", "B0BRACKET1")
    await db.accept_success_result(t["id"], "w-brk", t["lease_epoch"],
                                   mk("B0BRACKET1", title="[2-Pack] Storage Bins"), bid)
    assert (await outbox(db))[0]["outcome"] == "ok"


async def test_server_reject_is_parse_failed(db):
    bid, t = await one_task(db, "sr", "B0REJECT01")
    bad = {"asin": "B0REJECT01", "title": "", "brand": "", "current_price": "N/A",
           "buybox_price": "N/A", "stock_count": "", "stock_status": None}
    r = await db.accept_success_result(t["id"], "w-sr", t["lease_epoch"], bad, bid)
    assert r == {"accepted": True, "saved": False, "server_reject": True}
    b = (await outbox(db))[0]
    assert b["outcome"] == "parse_failed"
    assert b["error_type"] == "server_reject"
    assert b["error_detail"] == "parse_failure_on_server"
    assert await db._write_conn.fetchval(
        "SELECT status FROM tasks WHERE id=$1", t["id"]) == "failed"


async def test_empty_asin_emits_parse_failed(db):
    """S4：`_save_result_inner_unlocked` 返回 False 的**唯一**分支。
    任务照样是 done（终态），所以必须有一条事件。"""
    bid, t = await one_task(db, "noasin", "B0NOASIN01")
    r = await db.accept_success_result(t["id"], "w-noasin", t["lease_epoch"],
                                       {"asin": "  ", "title": "x", "brand": "y",
                                        "current_price": "1"}, bid)
    assert r == {"accepted": True, "saved": False}
    b = (await outbox(db))[0]
    assert (b["outcome"], b["asin"], b["error_detail"]) == ("parse_failed", "", "empty_asin")


@pytest.mark.parametrize("error_type,expected", [
    ("blocked", "blocked"),
    ("captcha", "blocked"),
    ("parse_error", "parse_failed"),
    ("variant_offset", "parse_failed"),
    ("network", "parse_failed"),
    ("timeout", "parse_failed"),
    ("zip_not_effective", "parse_failed"),
    ("", "parse_failed"),
    ("something_new_from_the_future", "parse_failed"),
])
async def test_terminal_failure_outcome_mapping(db, error_type, expected):
    """F2：终态失败按 error_type 映射；原始 error_type 无损留在自己的字段里。

    variant_offset 的 cap 是 1，其余是 MAX_RETRIES=3 —— 所以这里直接把
    retry_count 顶到 cap-1，让下一次失败就是终态。
    """
    asin = "B0FAIL%04d" % (abs(hash(error_type)) % 10000)
    bid, t = await one_task(db, "fail_" + (error_type or "empty"), asin)
    await db._write_conn.execute(
        "UPDATE tasks SET retry_count = 2 WHERE id = $1", t["id"])
    r = await db.fail_task(t["id"], f"w-fail_{error_type or 'empty'}",
                           t["lease_epoch"], error_type, "detail here")
    assert r == {"accepted": True, "stale": False}
    b = (await outbox(db))[0]
    assert b["outcome"] == expected
    assert b["error_type"] == (error_type or None)
    assert b["error_detail"] == "detail here"
    assert b["attempt"] == 3, "attempt 是**已经自增过**的 retry_count"
    assert b["asin"] == asin, "失败 payload 里没有 asin，只能来自 tasks 行"
    assert b["batch_id"] == bid


# ==========================================================================
# 3) stale —— 会发，而且发的是完整记录（规格 §2.3）
# ==========================================================================

async def test_stale_lease_emits_in_its_own_transaction(db):
    """S1：租约门拒收之后主事务已经回滚，事件走独立事务。"""
    bid, t = await one_task(db, "stale1", "B0STALE001")
    r = await db.accept_success_result(t["id"], "w-stale1", t["lease_epoch"] + 99,
                                       mk("B0STALE001", current_price="1.11"), bid)
    assert r == {"accepted": False, "stale": True}
    b = (await outbox(db))[0]
    assert b["outcome"] == "stale"
    assert b["asin"] == "B0STALE001"
    assert b["lease_epoch"] == t["lease_epoch"] + 99, "记的是这次提交声明的 epoch"
    assert b["result"]["current_price"] == "1.11", "stale 事件带完整 payload"
    # 主事务确实回滚了：结果没落库，任务还在 processing。
    assert await db._write_conn.fetchval(
        "SELECT count(*) FROM asin_data WHERE asin='B0STALE001'") == 0
    assert await db._write_conn.fetchval(
        "SELECT status FROM tasks WHERE id=$1", t["id"]) == "processing"


async def test_fail_task_stale_emits(db):
    """F1：fail_task 的 SELECT 没命中（epoch 被 reclaim bump 过）。"""
    bid, t = await one_task(db, "stale2", "B0STALE002")
    r = await db.fail_task(t["id"], "w-stale2", t["lease_epoch"] + 1,
                           "network", "late")
    assert r == {"accepted": False, "stale": True}
    b = (await outbox(db))[0]
    assert b["outcome"] == "stale"
    assert b["asin"] == "B0STALE002", "asin 只能从 tasks 行来"
    assert b["batch_id"] == bid
    assert b["error_type"] == "network"


async def test_stale_emit_failure_does_not_break_the_response(db, monkeypatch):
    """独立事务里的 stale 事件写失败**必须**被吞掉。

    否则一次本该 200 `{"ok": false, "stale": true}` 的提交会变成 500 ——
    那是黄金基线钉死的响应（step `submit_result_stale_lease`）。
    """
    bid, t = await one_task(db, "stale3", "B0STALE003")
    before = PAYLOAD_STATS["stale_emit_failed"]

    import common.pgdb.outbox as ob

    async def boom(*a, **kw):
        raise RuntimeError("outbox is on fire")

    monkeypatch.setattr(ob, "emit_stale_event", boom)
    r = await db.accept_success_result(t["id"], "w-stale3", t["lease_epoch"] + 5,
                                       mk("B0STALE003"), bid)
    assert r == {"accepted": False, "stale": True}
    assert PAYLOAD_STATS["stale_emit_failed"] == before + 1
    assert await outbox_count(db) == 0
    # 写连接没被留在事务里 —— 下一次写照常。
    r2 = await db.accept_success_result(t["id"], "w-stale3", t["lease_epoch"],
                                        mk("B0STALE003"), bid)
    assert r2 == {"accepted": True, "saved": True}


# ==========================================================================
# 4) §2.5 的不变式：每次终态尝试恰好一行，重新入队一行都没有
# ==========================================================================

async def test_requeue_is_silent_then_success_carries_attempt(db):
    """规格 §2.5 用例 3：失败两次（cap=3，都重新入队）再成功 = **恰好一条**事件。

    若重新入队的分支漏发，这里会是 3 条。
    """
    bid, t = await one_task(db, "requeue", "B0REQUEUE1")
    tid = t["id"]
    epoch = t["lease_epoch"]
    for i in range(2):
        r = await db.fail_task(tid, f"w-requeue", epoch, "network", f"try{i}")
        assert r == {"accepted": True, "stale": False}
        assert await outbox_count(db) == 0, "重新入队不是终态尝试"
        t2 = [x for x in await db.pull_tasks("w-requeue", count=50)
              if x["id"] == tid][0]
        epoch = t2["lease_epoch"]

    await db.accept_success_result(tid, "w-requeue", epoch, mk("B0REQUEUE1"), bid)
    bodies = await outbox(db)
    assert len(bodies) == 1
    assert bodies[0]["outcome"] == "ok"
    assert bodies[0]["attempt"] == 2, "两次失败之后，这是第 3 次尝试"


async def test_one_event_per_terminal_attempt_mixed_batch(db):
    """规格 §2.5 用例 1：一批混合路径，逐 outcome 对账 + source_id 全不重复。"""
    bid = await db.create_batch("mixed")
    asins = [f"B0MIX{i:05d}" for i in range(6)]
    await db.create_tasks(bid, asins)
    tasks = {x["asin"]: x for x in await db.pull_tasks("w-mixed", count=50)
             if x["batch_id"] == bid}
    assert len(tasks) == 6

    ok1, ok2, reject, term_fail, requeue, stale = [tasks[a] for a in asins]
    # 终态失败要顶到 cap
    await db._write_conn.execute(
        "UPDATE tasks SET retry_count = 2 WHERE id = $1", term_fail["id"])

    items = [
        {"task_id": ok1["id"], "worker_id": "w-mixed",
         "lease_epoch": ok1["lease_epoch"], "batch_id": bid, "success": True,
         "data": mk(ok1["asin"])},
        {"task_id": ok2["id"], "worker_id": "w-mixed",
         "lease_epoch": ok2["lease_epoch"], "batch_id": bid, "success": True,
         "data": mk(ok2["asin"], title="[商品不存在]", brand="N/A",
                    current_price="N/A", buybox_price="N/A", stock_count="0",
                    stock_status="N/A")},
        {"task_id": reject["id"], "worker_id": "w-mixed",
         "lease_epoch": reject["lease_epoch"], "batch_id": bid, "success": True,
         "data": {"asin": reject["asin"], "title": "", "brand": "",
                  "current_price": "N/A", "buybox_price": "N/A",
                  "stock_count": "", "stock_status": None}},
        {"task_id": term_fail["id"], "worker_id": "w-mixed",
         "lease_epoch": term_fail["lease_epoch"], "batch_id": bid, "success": False,
         "data": {"error_type": "blocked", "error_detail": "captcha wall"}},
        {"task_id": requeue["id"], "worker_id": "w-mixed",
         "lease_epoch": requeue["lease_epoch"], "batch_id": bid, "success": False,
         "data": {"error_type": "network", "error_detail": "reset"}},
        {"task_id": stale["id"], "worker_id": "w-mixed",
         "lease_epoch": stale["lease_epoch"] + 7, "batch_id": bid, "success": True,
         "data": mk(stale["asin"])},
        # 无 task_id 的直写（B1）
        {"task_id": None, "worker_id": "w-mixed", "lease_epoch": 0,
         "batch_id": bid, "success": True, "data": mk("B0DIRECT01")},
        # 非成功 + 无 task_id（B2）：什么都没发生，也就没有事件
        {"task_id": None, "worker_id": "w-mixed", "lease_epoch": 0,
         "batch_id": bid, "success": False, "data": {"error_type": "network"}},
    ]
    res = await db.accept_results_batch(items)
    assert res == {"accepted": 3, "stale": 1, "failed": 2}

    bodies = await outbox(db)
    hist = {}
    for b in bodies:
        hist[b["outcome"]] = hist.get(b["outcome"], 0) + 1
    assert hist == {"ok": 2, "not_found": 1, "parse_failed": 1,
                    "blocked": 1, "stale": 1}, hist
    assert len(bodies) == 6, "重新入队的那条与 B2 都不发"
    assert len({b["source_id"] for b in bodies}) == 6, "source_id 必须全不重复"

    # 终态任务数（done + failed）== 非 stale 事件数
    n_terminal = await db._write_conn.fetchval(
        "SELECT count(*) FROM tasks WHERE batch_id=$1 AND status IN ('done','failed')",
        bid)
    assert n_terminal == len([b for b in bodies if b["outcome"] != "stale"]) - 1, \
        "减 1 是那条无 task_id 的直写（B1，没有 tasks 行）"

    # (task_id, lease_epoch) 是一次终态尝试的自然键，不许有重复
    keys = [(b["task_id"], b["lease_epoch"]) for b in bodies
            if b["task_id"] is not None]
    assert len(keys) == len(set(keys)), keys


async def test_batch_failure_item_stale_emits(db):
    """B7：批量失败项的行没找到（租约不匹配）。"""
    bid, t = await one_task(db, "b7", "B0BATCH7001")
    res = await db.accept_results_batch([{
        "task_id": t["id"], "worker_id": "w-b7", "lease_epoch": t["lease_epoch"] + 3,
        "batch_id": bid, "success": False,
        "data": {"error_type": "timeout", "error_detail": "slow"},
    }])
    assert res == {"accepted": 0, "stale": 1, "failed": 0}
    b = (await outbox(db))[0]
    assert (b["outcome"], b["asin"], b["error_type"]) == ("stale", "B0BATCH7001", "timeout")


# ==========================================================================
# 5) 采集参数：zip_requested 必须来自 tasks，不是 payload
# ==========================================================================

async def test_zip_requested_comes_from_tasks_not_payload(db):
    """`data['zip_code']` 是**观测到的**邮编（worker/parser.py:111/:847 会用
    页面上 glow-ingress-line1 的值覆盖它）。拿它当分组键会把同一个商品的价格
    序列悄悄劈成两组 —— 这个错误没有任何外部症状，只能靠用例守。"""
    bid, t = await one_task(db, "zip", "B0ZIPTEST1", zip_code="90210")
    await db.accept_success_result(t["id"], "w-zip", t["lease_epoch"],
                                   mk("B0ZIPTEST1", zip_code="10001"), bid)
    b = (await outbox(db))[0]
    assert b["zip_requested"] == "90210", "请求值来自 tasks.zip_code"
    assert b["zip_requested_source"] == "task"
    assert b["result"]["zip_code"] == "10001", "观测值原样留在 payload 里"


async def test_no_task_path_marks_zip_source_payload(db):
    """B1 / save_result：没有 tasks 行可读，退回 payload 并**标注来源**。"""
    bid = await db.create_batch("notask")
    assert await db.save_result(mk("B0NOTASK01", zip_code="60601"), bid) is True
    b = (await outbox(db))[0]
    assert b["task_id"] is None
    assert b["zip_requested"] == "60601"
    assert b["zip_requested_source"] == "payload"
    assert b["attempt"] == 0


async def test_save_result_parse_failure_early_return_emits_nothing(db):
    """`save_result` 的解析失败早退：没有任务、也没写库 —— 不发事件。

    ⚠ 与 B1 不对称（accept_results_batch 的无 task_id 分支**不做**这个检测），
    这是从 SQLite 版忠实继承的现状，用例把它钉住，别顺手"修"。
    """
    bid = await db.create_batch("notask2")
    bad = {"asin": "B0BADNOASK", "title": "", "brand": "", "current_price": "N/A",
           "buybox_price": "N/A", "stock_count": "", "stock_status": None}
    assert await db.save_result(bad, bid) is False
    assert await outbox_count(db) == 0
    # 同一份 payload 走 B1 反而**会**有事件（不对称的证据）
    await db.accept_results_batch([{
        "task_id": None, "worker_id": "w", "lease_epoch": 0,
        "batch_id": bid, "success": True, "data": bad,
    }])
    assert await outbox_count(db) == 1


# ==========================================================================
# 6) D-20：写钩子不许把一次今天成功的提交变成失败
# ==========================================================================

@pytest.mark.parametrize("payload_json,in_payload,in_asin_data", [
    ('{"review_count": NaN}', None, None),
    ('{"review_count": Infinity}', "Inf", "Inf"),
    ('{"review_count": -Infinity}', "-Inf", "-Inf"),
    ('{"review_count": 1e400}', "Inf", "Inf"),
    # -0.0 是普通的有限浮点：payload 原样留着，落库走 text_affinity 的 '0.0'。
    ('{"review_count": -0.0}', -0.0, "0.0"),
])
async def test_nonfinite_numbers_do_not_break_the_submit(
        db, payload_json, in_payload, in_asin_data):
    """`json.loads` 接受 NaN/Infinity 字面量，`1e400` 解析出来就是 inf。
    实测这三种值 `json.dumps` 出来是 `NaN`/`Infinity`，jsonb 直接
    `InvalidTextRepresentationError` —— 会把整个写事务打死。

    D-20 之后 `text_affinity` 对它们的处理是**落库**（nan->NULL、inf->'Inf'），
    也就是说这类提交今天是成功的；写钩子必须保持它成功。
    非有限值在 payload 里按同一口径定形（jsonb 表示不了它们），有限值原样保留。
    """
    extra = json.loads(payload_json)     # 复刻 request.json() 的解析
    bid, t = await one_task(db, "nonfinite", "B0NONFIN01")
    r = await db.accept_success_result(t["id"], "w-nonfinite", t["lease_epoch"],
                                       mk("B0NONFIN01", **extra), bid)
    assert r == {"accepted": True, "saved": True}
    b = (await outbox(db))[0]
    assert b["outcome"] == "ok"
    assert b["result"]["review_count"] == in_payload
    assert await db._write_conn.fetchval(
        "SELECT review_count FROM asin_data WHERE asin='B0NONFIN01'") == in_asin_data


async def test_huge_int_does_not_break_the_submit(db):
    """> 4300 位的整数：`json.dumps` **自己**就抛 ValueError（Python 3.11），
    根本到不了 PG。它出现在非 ASIN_DATA_FIELDS 的键上时，今天的提交是成功的。
    """
    bid, t = await one_task(db, "hugeint", "B0HUGEINT1")
    data = mk("B0HUGEINT1")
    data["_internal_counter"] = 10 ** 5000       # 不在 ASIN_DATA_FIELDS 里
    r = await db.accept_success_result(t["id"], "w-hugeint", t["lease_epoch"],
                                       data, bid)
    assert r == {"accepted": True, "saved": True}
    b = (await outbox(db))[0]
    assert b["result"]["_internal_counter"].startswith("<int:")


async def test_nul_in_payload_is_stripped_not_fatal(db, monkeypatch):
    """NUL：jsonb **连转义过的 \\u0000 都不收**（22P05），而 text 列是 22021。
    `PG_STRIP_NUL=1` 时整条提交本该成功，写钩子不能把它打回来。
    """
    import common.pgdb.pool as pool
    monkeypatch.setattr(pool, "PG_STRIP_NUL", True)
    bid, t = await one_task(db, "nul", "B0NULTEST1")
    r = await db.accept_success_result(t["id"], "w-nul", t["lease_epoch"],
                                       mk("B0NULTEST1", title="bad\x00title"), bid)
    assert r == {"accepted": True, "saved": True}
    b = (await outbox(db))[0]
    assert "\x00" not in b["result"]["title"]
    assert b["result"]["title"] == "badtitle"


async def test_non_string_error_type_does_not_break_the_submit(db):
    """`error_type` 也来自未经校验的 JSON。

    两个坑：`outcome_for_error_type(True)` 会 `AttributeError`
    （`(True or "").strip()`），而把 jsonb 布尔塞进 body 会让 relay 绑 text 列时
    `DataError`。事件里的值必须与 `tasks.error_type` **逐字相同**。
    """
    bid, t = await one_task(db, "ettype", "B0ETTYPE01")
    await db._write_conn.execute(
        "UPDATE tasks SET retry_count = 2 WHERE id = $1", t["id"])
    r = await db.fail_task(t["id"], "w-ettype", t["lease_epoch"], True, 42)
    assert r == {"accepted": True, "stale": False}
    b = (await outbox(db))[0]
    row = await db._write_conn.fetchrow(
        "SELECT error_type, error_detail FROM tasks WHERE id=$1", t["id"])
    assert (b["error_type"], b["error_detail"]) == ("1", "42") == \
        (row["error_type"], row["error_detail"])
    assert b["outcome"] == "parse_failed"


async def test_unbindable_error_type_on_the_stale_path_stays_a_200(db):
    """B7 的 stale 分支今天**根本不碰** `text_affinity`（`stale += 1; continue`），
    所以一个 list 型 error_type 在那里是成功的。写钩子不能把它变成 500。"""
    bid, t = await one_task(db, "etlist", "B0ETLIST01")
    res = await db.accept_results_batch([{
        "task_id": t["id"], "worker_id": "w-etlist",
        "lease_epoch": t["lease_epoch"] + 4, "batch_id": bid, "success": False,
        "data": {"error_type": [1, 2], "error_detail": {"k": "v"}},
    }])
    assert res == {"accepted": 0, "stale": 1, "failed": 0}
    b = (await outbox(db))[0]
    assert b["outcome"] == "stale"
    assert isinstance(b["error_type"], str), "绑 text 列的字段必须是字符串"
    assert isinstance(b["error_detail"], str)


async def test_golden_scenario_payload_round_trips(db):
    """直接拿黄金场景那条 payload 过一遍钩子：键一个不少、一个不多。"""
    from tests.golden.scenario import _product

    bid, t = await one_task(db, "golden", "B0GOLDEN01")
    data = _product("B0GOLDEN01", price="19.99", stock="42")
    data["worker_id"] = "w-golden"
    submitted = dict(data)
    r = await db.accept_success_result(t["id"], "w-golden", t["lease_epoch"],
                                       data, bid)
    assert r == {"accepted": True, "saved": True}
    b = (await outbox(db))[0]
    # data 被就地写回了两个 hash（database.py:1827-1828 的现状，照抄），
    # F-012 之后再加一个 marketplace。
    #
    # 为什么 marketplace 也要就地写回、而且必须写在 emit_result_event **之前**：
    # 事件的 body 用的就是这份 data，而 scrape_events 有自己的 marketplace 列。
    # 归一化晚于发事件的话，事件里带的是 worker 交上来的原始写法
    # （'www.amazon.ca' / 'AMAZON.CA' / 缺席），而 asin_data 里落的是规范键
    # —— 同一次采集在两张表里记成两个站点，而这两张表正是要靠 marketplace
    # join 起来的。
    expected_added = {"content_hash", "title_bullets_hash", "marketplace"}
    assert set(b["result"]) == set(submitted) | expected_added
    assert b["result"]["marketplace"] == "amazon.com", (
        "老 worker 不提交这个字段，必须落到美国站默认值 —— 它们采的确实是美国站")
    for k, v in submitted.items():
        assert b["result"][k] == v, k
    assert b["result"]["crawl_time"] == "2026-08-04 17:11:02"


# ==========================================================================
# 7) 纯函数
# ==========================================================================
# 本模块的 pytestmark 是 asyncio（仓库是 strict 模式），所以下面几条纯函数用例
# 也写成 async —— 它们不 await 任何东西，只是为了不触发"标了 asyncio 却不是
# 协程"的告警。

async def test_json_safe_returns_the_same_object_when_nothing_to_do():
    """常见路径零拷贝：没有可疑值时返回**原对象**。"""
    d = {"a": "x", "b": 1, "c": None, "d": True, "e": 1.5, "f": ["p", "q"]}
    assert json_safe(d) is d
    assert safe_payload(d) is d


async def test_json_safe_normalizes_the_three_killers():
    out = json_safe({"nan": float("nan"), "inf": float("inf"),
                     "ninf": float("-inf"), "big": 2 ** 64, "n": -0.0})
    assert out["nan"] is None          # text_affinity: nan -> NULL
    assert out["inf"] == "Inf"
    assert out["ninf"] == "-Inf"
    assert out["big"] == "<int:65bit>"
    assert out["n"] == -0.0            # 合法 JSON 数字，原样留着
    assert json.dumps(out, allow_nan=False)   # 这一步以前会抛


async def test_json_safe_is_total():
    """任何输入都不抛：非 JSON 原生对象、超深嵌套、非 str 键。"""
    class Weird:
        def __str__(self):
            raise ValueError("nope")

    deep = cur = {}
    for _ in range(300):
        cur["k"] = {}
        cur = cur["k"]
    out = json_safe({"w": Weird(), 1: "int key", "deep": deep,
                     "s": {"a", "b"}, "t": ("x", "y")})
    assert out["w"] == "<unserializable>"
    assert out["1"] == "int key"
    assert sorted(out["s"]) == ["a", "b"]
    assert out["t"] == ["x", "y"]
    assert json.dumps(out, allow_nan=False)


async def test_safe_payload_never_mutates_the_input():
    d = {"x": float("nan")}
    out = safe_payload(d)
    assert d["x"] != d["x"], "入参必须原样不动"
    assert out["x"] is None
    assert out is not d
