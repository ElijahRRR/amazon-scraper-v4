"""tests/pgdb/test_tasks.py —— TasksMixin（common/pgdb/tasks.py）的验收门。

黄金 64 步只覆盖到 create_tasks / pull_tasks 的主干（而且是单线程、单批次），
本文件补的是基线够不到的地方：自增烧号、NULL 排序、lease 门的两个方向、
reclaim 的硬超时兜底、auto_retry 的 error_type 排除、release 的分组与幂等、
以及 asyncpg 严格类型下"worker 发字符串 id"这一类只在 PG 才炸的路径。

每个用例都拿一个全新的临时库（conftest 的 pgdb 夹具），所以 identity 序列
从 1 开始——烧号是被基线钉死的行为，复用脏库测不出来。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from common import config
from common.pgdb._shared import NO_AUTO_RETRY_ERROR_TYPES

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- helpers

async def _write(db, sql, params=()):
    async with db._write_lock:
        await db._db.execute("BEGIN")
        await db._db.execute(sql, params)
        await db._db.execute("COMMIT")


async def _rows(db, sql, params=()):
    async with db.read() as rc, rc.execute(sql, params) as c:
        return [dict(r) for r in await c.fetchall()]


async def _batch(db, name="b1"):
    await _write(db, "INSERT INTO batches (name) VALUES (?)", (name,))
    rows = await _rows(db, "SELECT id FROM batches WHERE name = ?", (name,))
    return rows[0]["id"]


# ---------------------------------------------------------------- create_tasks

async def test_create_tasks_returns_actually_inserted_and_burns_ids(pgdb):
    """自增烧号必须与 SQLite AUTOINCREMENT + INSERT OR IGNORE 逐字一致。

    形状：3 条插入 → 同名重传 3 条全冲突（烧掉 4,5,6）→ 再插 2 条 = 7,8。

    ⚠ 这曾经也被黄金基线覆盖（旧的 upload_batch_a_duplicate 会重跑一次
      create_tasks），撞名改成 409 之后**不再是**：基线里的 task id 现在是
      1,3,4,5。也就是说 create_tasks 的烧号从"两道网"变成"只剩这一道"，
      改这条用例之前先想清楚没有别的东西在替它兜底。
    """
    bid = await _batch(pgdb)
    assert await pgdb.create_tasks(bid, ["X1", "X2", "X3"]) == 3
    assert await pgdb.create_tasks(bid, ["X1", "X2", "X3"]) == 0     # 静默 no-op
    assert await pgdb.create_tasks(bid, ["X4", "X5"]) == 2
    ids = [r["id"] for r in await _rows(pgdb, "SELECT id FROM tasks ORDER BY id")]
    assert ids == [1, 2, 3, 7, 8], "冲突行必须照样烧号，否则下游所有 id 挪位"


async def test_create_tasks_dedupes_strips_and_skips_empty(pgdb):
    bid = await _batch(pgdb)
    assert await pgdb.create_tasks(bid, ["A1", " A1 ", "  ", "", "a1"]) == 2
    asins = [r["asin"] for r in await _rows(pgdb, "SELECT asin FROM tasks ORDER BY id")]
    assert asins == ["A1", "a1"], "只 strip、不 upper（database.py:1106）"
    assert await pgdb.create_tasks(bid, ["", "   "]) == 0


async def test_create_tasks_per_asin_zip_and_batch_asins_is_new(pgdb):
    bid = await _batch(pgdb)
    await _write(pgdb, "INSERT INTO asin_data (asin, title) VALUES (?, ?)", ("KNOWN", "t"))
    n = await pgdb.create_tasks(bid, ["KNOWN", "FRESH"], zip_code="90210",
                                per_asin_zip={"FRESH": "60601"})
    assert n == 2
    zips = {r["asin"]: r["zip_code"]
            for r in await _rows(pgdb, "SELECT asin, zip_code FROM tasks")}
    assert zips == {"KNOWN": "90210", "FRESH": "60601"}
    is_new = {r["asin"]: r["is_new"]
              for r in await _rows(pgdb, "SELECT asin, is_new FROM batch_asins")}
    assert is_new == {"KNOWN": 0, "FRESH": 1}
    assert all(isinstance(v, int) for v in is_new.values()), "is_new 是 int 0/1，不是 bool"


async def test_create_tasks_null_batch_id_raises_known_divergence(pgdb):
    """已知偏离，钉在这里免得将来被当成回归。

    SQLite 的 INSERT OR IGNORE 吞掉**所有**约束冲突（含 NOT NULL），所以
    ``create_tasks(None, [...])`` 静默返回 0；PG 的 ON CONFLICT DO NOTHING
    只吞唯一冲突，NOT NULL 会抛。现有调用方到不了这条路：create_batch 未命中
    返回的是 0 而非 None，而 batch_id=0 两边都正常插入。
    """
    import asyncpg
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await pgdb.create_tasks(None, ["Z1"])
    assert await pgdb.create_tasks(0, ["Z2"]) == 1, "batch_id=0 与 SQLite 一致"


async def test_create_tasks_screenshots_are_created_and_burn(pgdb):
    bid = await _batch(pgdb)
    assert await pgdb.create_tasks(bid, ["S1"], needs_screenshot=True) == 1
    assert await pgdb.create_tasks(bid, ["S1"], needs_screenshot=True) == 0
    ss = await _rows(pgdb, "SELECT id, asin, batch_id, status FROM screenshots ORDER BY id")
    assert [(r["id"], r["asin"], r["status"]) for r in ss] == [(1, "S1", "pending")]
    t = await _rows(pgdb, "SELECT needs_screenshot FROM tasks")
    assert t[0]["needs_screenshot"] == 1, "列里存的是 INTEGER 1，不是 boolean true"


# ---------------------------------------------------------------- pull_tasks

async def test_pull_tasks_shape_and_types(pgdb):
    bid = await _batch(pgdb, "gold")
    await pgdb.create_tasks(bid, ["P1"])
    tasks = await pgdb.pull_tasks("w1", 10)
    assert len(tasks) == 1
    t = tasks[0]
    assert set(t) == {"id", "batch_id", "batch_name", "asin", "zip_code",
                      "retry_count", "priority", "needs_screenshot",
                      "lease_epoch", "task_type", "task_meta", "discover_mode",
                      # F-012：worker 靠它决定去哪个站点抓。
                      "marketplace"}
    assert t["marketplace"] == "amazon.com", "不传站点时必须是美国站（改造前的唯一行为）"
    assert t["needs_screenshot"] is False, "/api/tasks/pull 这里是 Python bool"
    assert t["task_type"] == "asin"
    assert t["batch_name"] == "gold"
    assert isinstance(t["id"], int) and isinstance(t["lease_epoch"], int)
    row = (await _rows(pgdb, "SELECT status, worker_id FROM tasks"))[0]
    assert row["status"] == "processing" and row["worker_id"] == "w1"


async def test_pull_tasks_order_is_zip_then_id_with_nulls_first(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["A", "B", "C", "D"], zip_code="10001",
                            per_asin_zip={"A": "99999", "C": "60601"})
    await _write(pgdb, "UPDATE tasks SET zip_code=NULL WHERE asin='D'")
    got = [(t["asin"], t["zip_code"]) for t in await pgdb.pull_tasks("w", 10)]
    # SQLite 的 ASC 把 NULL 排在最前；PG 默认相反，所以 SQL 里显式写了 NULLS FIRST。
    # NULL 之后按 zip_code 的字节序（'10001' < '60601' < '99999'），列上有 COLLATE "C"。
    assert got == [("D", None), ("B", "10001"), ("C", "60601"), ("A", "99999")]


async def test_pull_tasks_prefer_zip_wins(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["A", "B", "C"], zip_code="10001",
                            per_asin_zip={"C": "99999"})
    got = [t["asin"] for t in await pgdb.pull_tasks("w", 10, prefer_zip="99999")]
    assert got == ["C", "A", "B"]


async def test_pull_tasks_priority_pin_and_screenshot_filter(pgdb):
    b1 = await _batch(pgdb, "b1")
    b2 = await _batch(pgdb, "b2")
    await pgdb.create_tasks(b1, ["N1"])
    await pgdb.create_tasks(b2, ["S1"], needs_screenshot=True)
    await pgdb.prioritize_batch(b2, 10)
    # MAX(priority) 只在 pending 上取：S1 被提到 10，所以第一轮只出 S1；
    # S1 变 processing 之后，剩余 pending 的最高优先级回落到 0，N1 才可拉。
    assert [t["asin"] for t in await pgdb.pull_tasks("w", 10)] == ["S1"]
    assert [t["asin"] for t in await pgdb.pull_tasks("w", 10)] == ["N1"]
    assert await pgdb.pull_tasks("w", 10) == []
    await pgdb.release_tasks("w", [{"task_id": 2, "lease_epoch": 0}])
    assert [t["asin"] for t in await pgdb.pull_tasks("w", 10, needs_screenshot=False)] == []
    assert [t["asin"] for t in await pgdb.pull_tasks("w", 10, needs_screenshot=True)] == ["S1"]


async def test_pull_tasks_empty_returns_list(pgdb):
    assert await pgdb.pull_tasks("w", 10) == []


# ---------------------------------------------------------------- fail_task

async def test_fail_task_lease_gate_both_directions(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["F1"])
    t = (await pgdb.pull_tasks("w1", 1))[0]
    assert await pgdb.fail_task(t["id"], "w1", t["lease_epoch"] + 1, "http", "x") == \
        {"accepted": False, "stale": True}
    assert await pgdb.fail_task(t["id"], "other", t["lease_epoch"], "http", "x") == \
        {"accepted": False, "stale": True}
    assert await pgdb.fail_task(t["id"], "w1", t["lease_epoch"], "http", "x") == \
        {"accepted": True, "stale": False}
    row = (await _rows(pgdb, "SELECT status, retry_count, lease_epoch, error_type FROM tasks"))[0]
    assert (row["status"], row["retry_count"], row["error_type"]) == ("pending", 1, "http")
    assert row["lease_epoch"] == t["lease_epoch"] + 1, "回 pending 必须 bump epoch"


async def test_fail_task_reaches_terminal_at_max_retries(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["F2"])
    for _ in range(config.MAX_RETRIES):
        t = (await pgdb.pull_tasks("w1", 1))[0]
        assert (await pgdb.fail_task(t["id"], "w1", t["lease_epoch"], "http", "x"))["accepted"]
    row = (await _rows(pgdb, "SELECT status, retry_count FROM tasks"))[0]
    assert (row["status"], row["retry_count"]) == ("failed", config.MAX_RETRIES)


async def test_fail_task_variant_offset_is_terminal_immediately(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["F3"])
    t = (await pgdb.pull_tasks("w1", 1))[0]
    await pgdb.fail_task(t["id"], "w1", t["lease_epoch"], "variant_offset", "vo")
    row = (await _rows(pgdb, "SELECT status, retry_count FROM tasks"))[0]
    assert (row["status"], row["retry_count"]) == ("failed", 1), "cap=1，首次失败即终态"


async def test_fail_task_accepts_string_ids_like_sqlite_affinity(pgdb):
    """worker 直接把 request.json() 的值传下来；SQLite 靠列 affinity 命中，
    asyncpg 会 DataError —— 所以 tasks.py 里做了显式强转。"""
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["F4"])
    t = (await pgdb.pull_tasks("w1", 1))[0]
    assert await pgdb.fail_task(str(t["id"]), "w1", str(t["lease_epoch"]), "http", "x") == \
        {"accepted": True, "stale": False}


async def test_fail_task_unknown_id_is_stale(pgdb):
    assert await pgdb.fail_task(999999, "w", 0, "http", "x") == \
        {"accepted": False, "stale": True}


# ---------------------------------------------------------------- release_tasks

async def test_release_tasks_groups_and_is_idempotent(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["R1", "R2", "R3"])
    assert await pgdb.release_tasks("w", []) == 0
    assert isinstance(await pgdb.release_tasks("w", []), int)
    tasks = await pgdb.pull_tasks("w1", 3)
    payload = [{"task_id": t["id"], "lease_epoch": t["lease_epoch"]} for t in tasks]
    assert await pgdb.release_tasks("w1", payload) == 3
    assert await pgdb.release_tasks("w1", payload) == 0, "epoch 已 bump，第二次不该再命中"
    assert await pgdb.release_tasks("nobody", payload) == 0
    rows = await _rows(pgdb, "SELECT status, worker_id, lease_epoch FROM tasks")
    assert all(r["status"] == "pending" and r["worker_id"] is None
               and r["lease_epoch"] == 1 for r in rows)


async def test_release_tasks_string_payload(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["R9"])
    t = (await pgdb.pull_tasks("w1", 1))[0]
    assert await pgdb.release_tasks(
        "w1", [{"task_id": str(t["id"]), "lease_epoch": str(t["lease_epoch"])}]) == 1


# ---------------------------------------------------------------- reclaim

async def test_reclaim_dead_worker_and_hard_timeout(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["C1", "C2"])
    await pgdb.pull_tasks("dead", 1)
    await pgdb.pull_tasks("alive", 1)
    assert await pgdb.reclaim_dead_worker_tasks([]) == 0
    assert await pgdb.reclaim_dead_worker_tasks(["ghost"]) == 0
    assert await pgdb.reclaim_dead_worker_tasks(["dead"]) == 1
    row = (await _rows(pgdb, "SELECT status, worker_id, lease_epoch FROM tasks"
                             " WHERE asin='C1'"))[0]
    assert (row["status"], row["worker_id"], row["lease_epoch"]) == ("pending", None, 1)

    # 硬超时兜底：把 alive 的 updated_at 往前拨
    old = (datetime.utcnow() - timedelta(minutes=config.TASK_TIMEOUT_MINUTES + 5)
           ).strftime('%Y-%m-%d %H:%M:%S')
    await _write(pgdb, "UPDATE tasks SET updated_at=? WHERE worker_id='alive'", (old,))
    assert await pgdb.reclaim_dead_worker_tasks([]) == 1
    assert (await _rows(pgdb, "SELECT status FROM tasks WHERE asin='C2'"))[0]["status"] == "pending"


# ---------------------------------------------------------------- auto_retry

async def test_auto_retry_respects_cycles_delay_and_excluded_types(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["T1", "T2"])
    await _write(pgdb, "UPDATE tasks SET status='failed', error_type='http'"
                       " WHERE asin='T1'")
    await _write(pgdb, "UPDATE tasks SET status='failed', error_type=?"
                       " WHERE asin='T2'", (sorted(NO_AUTO_RETRY_ERROR_TYPES)[0],))

    assert await pgdb.auto_retry_failed_tasks(0, 5) == 0, "max_auto_cycles<=0 直接返回"
    assert await pgdb.auto_retry_failed_tasks(2, 5) == 0, "刚失败，还没过 delay"

    old = (datetime.utcnow() - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
    await _write(pgdb, "UPDATE tasks SET updated_at=? WHERE status='failed'", (old,))
    assert await pgdb.auto_retry_failed_tasks(2, 5) == 1, "被排除的 error_type 不参与"
    rows = {r["asin"]: r for r in await _rows(
        pgdb, "SELECT asin, status, retry_count, auto_retry_count, lease_epoch FROM tasks")}
    assert rows["T1"]["status"] == "pending"
    assert (rows["T1"]["retry_count"], rows["T1"]["auto_retry_count"],
            rows["T1"]["lease_epoch"]) == (0, 1, 1)
    assert rows["T2"]["status"] == "failed"

    # auto_retry_count 到顶后不再重试
    await _write(pgdb, "UPDATE tasks SET status='failed', updated_at=?,"
                       " auto_retry_count=2 WHERE asin='T1'", (old,))
    assert await pgdb.auto_retry_failed_tasks(2, 5) == 0


# ---------------------------------------------------------------- progress

async def test_get_progress_shape_and_rates(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["G1", "G2", "G3", "G4"])
    await _write(pgdb, "UPDATE tasks SET status='done' WHERE asin IN ('G1','G2')")
    await _write(pgdb, "UPDATE tasks SET status='failed' WHERE asin='G3'")
    p = await pgdb.get_progress(bid)
    assert p == {"pending": 1, "processing": 0, "done": 2, "failed": 1, "total": 4,
                 "completion_rate": 50.0, "success_rate": 66.7}
    assert all(isinstance(p[k], int)
               for k in ("pending", "processing", "done", "failed", "total"))
    assert await pgdb.get_progress() == p


async def test_get_progress_empty(pgdb):
    assert await pgdb.get_progress() == {
        "pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0,
        "completion_rate": 0, "success_rate": 0}


async def test_get_progress_leaks_unknown_status_key(pgdb):
    """现状：未知 status 会被**写进** stats（不是 KeyError），且不计入 total。
    与 SQLite 逐字一致，照抄不修。"""
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["U1"])
    await _write(pgdb, "UPDATE tasks SET status='weird'")
    p = await pgdb.get_progress(bid)
    assert p["weird"] == 1 and p["total"] == 0


# ---------------------------------------------------------------- failures

async def test_get_batch_failures_keys_filter_and_null_ordering(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["E1", "E2", "E3", "E4"])
    await _write(pgdb, "UPDATE tasks SET status='failed', error_type='http',"
                       " error_detail='d', worker_id='w', retry_count=3"
                       " WHERE asin IN ('E1','E2','E3')")
    await _write(pgdb, "UPDATE tasks SET updated_at='2030-01-01 00:00:00' WHERE asin='E1'")
    await _write(pgdb, "UPDATE tasks SET updated_at='2020-01-01 00:00:00' WHERE asin='E2'")
    await _write(pgdb, "UPDATE tasks SET updated_at=NULL WHERE asin='E3'")

    rows = await pgdb.get_batch_failures(bid)
    assert [r["asin"] for r in rows] == ["E1", "E2", "E3"], "DESC 时 NULL 排最后"
    assert set(rows[0]) == {"asin", "status", "error_type", "error_detail",
                            "retry_count", "worker_id", "updated_at"}
    assert await pgdb.get_batch_failures(bid, error_types=["nope"]) == []
    assert len(await pgdb.get_batch_failures(bid, error_types=["http", "  "])) == 3
    assert len(await pgdb.get_batch_failures(bid, error_types=[])) == 3
    assert len(await pgdb.get_batch_failures(bid, limit=1)) == 1
    assert len(await pgdb.get_batch_failures(bid, limit=0)) == 3, "limit=0 -> 回落 100000"
    assert await pgdb.get_batch_failures(999999) == []


# ---------------------------------------------------------------- prioritize

async def test_prioritize_batch_only_touches_pending(pgdb):
    bid = await _batch(pgdb)
    await pgdb.create_tasks(bid, ["Y1", "Y2"])
    await pgdb.pull_tasks("w", 1)
    await pgdb.prioritize_batch(bid, 10)
    rows = {r["asin"]: r["priority"] for r in
            await _rows(pgdb, "SELECT asin, priority FROM tasks")}
    assert rows == {"Y1": 0, "Y2": 10}
