"""tests/pgdb/test_orphan_screenshot_reconcile.py —— 「任务终态失败 -> 截图永远 pending」的对账。

守的是一个**死局**，不是一个显示瑕疵：批次完成判定要求 task 和 screenshot 都
全部终态，所以一条永远 pending 的截图会让批次永远停在 running、回调永不发出。
2026-08 线上抽查 12 个同类批次 12 个全中，最早的卡了 5 天。

每条用例都先把 get_batch_completion_status 的 all_terminal 断言成 False（复现
死局），再断言对账之后变 True —— 不这么写的话，一个「什么都不做」的实现也能
让「status == failed」那类断言通过。
"""
from __future__ import annotations

import pytest

pytest.importorskip("asyncpg", reason="tests/pgdb 需要 asyncpg")


async def _batch(conn, name="b1", needs=1):
    return await conn.fetchval(
        "INSERT INTO batches (name, needs_screenshot, status) "
        "VALUES ($1,$2,'running') RETURNING id", name, needs)


async def _task(conn, bid, asin, status, zip_code=None):
    await conn.execute(
        "INSERT INTO tasks (batch_id, asin, zip_code, status, task_type) "
        "VALUES ($1,$2,$3,$4,'detail')", bid, asin, zip_code, status)


async def _shot(conn, bid, asin, status="pending"):
    await conn.execute(
        "INSERT INTO screenshots (batch_id, asin, status) VALUES ($1,$2,$3)",
        bid, asin, status)


async def _shot_status(conn, bid, asin):
    return await conn.fetchval(
        "SELECT status FROM screenshots WHERE batch_id=$1 AND asin=$2", bid, asin)


@pytest.mark.asyncio
async def test_the_deadlock_is_reproduced_then_broken(pgdb, pgconn):
    """核心用例：复现死局 -> 对账 -> 批次可以完成了。"""
    bid = await _batch(pgconn)
    await _task(pgconn, bid, "B0AAAAAAAA", "done")
    await _shot(pgconn, bid, "B0AAAAAAAA", "done")
    await _task(pgconn, bid, "B0FAILFAIL", "failed")     # 终态失败
    await _shot(pgconn, bid, "B0FAILFAIL", "pending")    # 永远等不到的那张

    before = await pgdb.get_batch_completion_status(bid)
    assert before["tasks"]["open"] == 0, "任务侧应当已经全部终态"
    assert before["screenshots"]["open"] == 1
    assert before["all_terminal"] is False, "死局没复现出来，这条用例失去意义"

    assert await pgdb.reconcile_orphan_screenshots(bid) == 1

    after = await pgdb.get_batch_completion_status(bid)
    assert after["all_terminal"] is True, "对账之后批次仍然完成不了"
    assert await _shot_status(pgconn, bid, "B0FAILFAIL") == "failed"
    # 已经 done 的那张一个字都不能动（它有 file_path，覆盖掉等于凭空丢图）
    assert await _shot_status(pgconn, bid, "B0AAAAAAAA") == "done"


@pytest.mark.asyncio
async def test_in_flight_task_is_never_touched(pgdb, pgconn):
    """任务还在跑 -> 截图仍有可能送上来 -> 一行都不许动。

    判死一个还在途的批次比卡住它更糟：截图随后上传会覆盖不回来。
    """
    bid = await _batch(pgconn)
    for st in ("pending", "processing", "done"):
        await _task(pgconn, bid, f"B0{st.upper()[:8]:X<8}", st)
        await _shot(pgconn, bid, f"B0{st.upper()[:8]:X<8}", "pending")

    assert await pgdb.reconcile_orphan_screenshots(bid) == 0
    snap = await pgdb.get_batch_completion_status(bid)
    assert snap["screenshots"]["open"] == 3


@pytest.mark.asyncio
async def test_predicate_rests_on_the_unique_constraint(pgdb, pgconn):
    """对账的谓词只有一条 EXISTS（"存在一条 failed 任务"），这是**有前提的**。

    前提是 tasks 上的 UNIQUE(batch_id, asin)：一个 (批次, ASIN) 只可能有一行，
    于是"存在一条 failed"等价于"全部 failed"。哪天为了支持一 ASIN 多 zip 把它
    放开，这条等价关系就断了 —— 那时还能拿到的截图会被提前判死，而且**不报错**。

    这条用例把那个前提钉住：约束还在，它就绿；约束被放开，它变红，提醒回来把
    "且没有任何一条任务不是 failed"补进谓词。
    """
    bid = await _batch(pgconn)
    await _task(pgconn, bid, "B0MULTIZIP", "failed", zip_code="10001")
    with pytest.raises(Exception) as ei:
        await _task(pgconn, bid, "B0MULTIZIP", "processing", zip_code="90210")
    assert "unique" in str(ei.value).lower() or "duplicate" in str(ei.value).lower(), (
        f"UNIQUE(batch_id, asin) 似乎已被放开：{ei.value!r}。"
        "请回去给 reconcile_orphan_screenshots 的谓词补上「全部失败」那一条。")


@pytest.mark.asyncio
async def test_only_touches_the_named_batch(pgdb, pgconn):
    """对账是按批次做的，不能顺手改别的批次。"""
    a = await _batch(pgconn, "ba")
    b = await _batch(pgconn, "bb")
    for bid in (a, b):
        await _task(pgconn, bid, "B0SAMEASIN", "failed")
        await _shot(pgconn, bid, "B0SAMEASIN", "pending")

    assert await pgdb.reconcile_orphan_screenshots(a) == 1
    assert await _shot_status(pgconn, a, "B0SAMEASIN") == "failed"
    assert await _shot_status(pgconn, b, "B0SAMEASIN") == "pending"


@pytest.mark.asyncio
async def test_is_idempotent(pgdb, pgconn):
    """兜底扫描每轮都会重新检查同一个批次，第二次必须是 0 行。"""
    bid = await _batch(pgconn)
    await _task(pgconn, bid, "B0FAILFAIL", "failed")
    await _shot(pgconn, bid, "B0FAILFAIL", "pending")

    assert await pgdb.reconcile_orphan_screenshots(bid) == 1
    assert await pgdb.reconcile_orphan_screenshots(bid) == 0


# ------------------------------------------------------------------ SQLite 侧
#
# 上面全部跑在 PG 上。SQLite 侧那份实现是**逐字同源**的（同一条 UPDATE，只换
# 事务写法），但"看起来一样"和"跑得起来"是两回事 —— 相关子查询里引用外层表名
# （`screenshots.batch_id`）两个引擎的支持度并不天然相同。这条用例把 SQLite 那
# 条路径真的执行一遍。

@pytest.mark.asyncio
async def test_sqlite_backend_does_the_same_thing(tmp_path):
    from common.database import Database as SqliteDatabase

    db = SqliteDatabase(str(tmp_path / "t.db"))
    await db.connect()
    try:
        await db._db.execute(
            "INSERT INTO batches (name, needs_screenshot, status) VALUES ('b',1,'running')")
        await db._db.execute(
            "INSERT INTO tasks (batch_id, asin, status, task_type) "
            "VALUES (1,'B0FAILFAIL','failed','detail')")
        await db._db.execute(
            "INSERT INTO tasks (batch_id, asin, status, task_type) "
            "VALUES (1,'B0OKOKOKOK','done','detail')")
        await db._db.execute(
            "INSERT INTO screenshots (batch_id, asin, status) VALUES (1,'B0FAILFAIL','pending')")
        await db._db.execute(
            "INSERT INTO screenshots (batch_id, asin, status) VALUES (1,'B0OKOKOKOK','done')")
        await db._db.commit()

        before = await db.get_batch_completion_status(1)
        assert before["all_terminal"] is False, "死局没复现出来"

        assert await db.reconcile_orphan_screenshots(1) == 1

        after = await db.get_batch_completion_status(1)
        assert after["all_terminal"] is True
        async with db._db.execute(
                "SELECT asin, status FROM screenshots ORDER BY asin") as c:
            got = {r[0]: r[1] for r in await c.fetchall()}
        assert got == {"B0FAILFAIL": "failed", "B0OKOKOKOK": "done"}
        # 幂等：兜底扫描每轮都会再来一次
        assert await db.reconcile_orphan_screenshots(1) == 0
    finally:
        await db.close()
