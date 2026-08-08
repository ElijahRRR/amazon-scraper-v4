"""裸 ``BEGIN`` 块的回滚路径（F3）+ 三处不确定 ``ORDER BY``（D4/D5）+ create_batch(None)（D6）。

黄金夹具**结构性**看不到这一整组问题：
  * 64 步场景里没有任何一步让事务中途失败，所以"块里少一个 ROLLBACK"永远不发作；
  * ``get_pending_screenshots`` 和 ``create_batch(None)`` 一步都没覆盖。

所以这些不变式只能靠本文件守。

历史（修复前实测，两个后端都跑过）：
  F3  server/app.py 的 7 个裸 BEGIN 块里有 6 个、common/pgdb/tasks.py 里有 3 个
      没有回滚路径。事务中途失败一次 → PG 下垫片的事务槽再也不清 → 之后每一次
      BEGIN 都撞"嵌套 BEGIN"，**整条写路径永久焊死**，而读还是 200，
      健康检查全绿。SQLite 侧同样会 wedge（cannot start a transaction within a
      transaction），只是那些语句在 SQLite 下基本不会失败。
  D4  ``get_batch_failures``（`/api/batches/{batch_id}/failures` 背后那个函数，
      旧接口 `/api/batches/{batch_name}/errors` 已删除）的
      ``ORDER BY updated_at DESC LIMIT 200`` 没有 tiebreaker 的话，而
      updated_at 是秒级精度、accept_results_batch 又给整次提交盖同一个时间戳：
      260 个任务一起失败时，返回的**行集合**两个后端差 60 行。
  D5  ``get_pending_screenshots`` 是 LIMIT 没有 ORDER BY：20 行里 churn 掉 8 行、
      limit=5 → sqlite [S001..S005] / pg [S009..S013]。
  D6  ``create_batch(None)``：sqlite 返回 0（INSERT OR IGNORE 吞掉 NOT NULL），
      pg 抛 NotNullViolationError（ON CONFLICT DO NOTHING 只吞唯一冲突）。
"""
from __future__ import annotations

import os
import re

import pytest

NUL = "\x00"

_APP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "server", "app.py")


def _app_source() -> str:
    """server/app.py 的源码，空白压平。

    D-4 禁止把 app.py 的裸 SQL 抽成方法（那要改 common/database.py），所以这三条
    SQL 只能留在 app.py 里、两个后端共用。于是本文件对它们的守护分两层：
      1) 断言 app.py 里确实带着 tiebreaker（下面的 _app_has）——防止有人删掉；
      2) 把同一条 SQL 真的跑一遍，断言它在并列数据上是全序。
    只做第 2 层是假的：SQL 文本写在用例里，app.py 改回去用例照样绿。
    """
    with open(_APP_PY, encoding="utf-8") as f:
        return re.sub(r"\s+", " ", f.read())


def _app_has(fragment: str) -> bool:
    return re.sub(r"\s+", " ", fragment) in _app_source()


# --------------------------------------------------------------------------
# F3 —— common/pgdb/tasks.py 的三个裸 BEGIN 块
# --------------------------------------------------------------------------

async def _seed(pgdb, name, n=4):
    bid = await pgdb.create_batch(name)
    await pgdb.create_tasks(bid, [f"B0RB{i:06d}" for i in range(n)])
    tasks = await pgdb.pull_tasks("W1", count=n)
    return bid, tasks


async def _write_path_is_alive(pgdb, tag):
    """修复前这几行会全部抛 RuntimeError('嵌套 BEGIN：上一个事务还没结束')。"""
    assert await pgdb.create_batch(f"{tag}_after") > 0
    await pgdb.pull_tasks("W9", count=1)
    await pgdb.prioritize_batch(1, 7)
    await pgdb.reclaim_dead_worker_tasks(["W_none"])
    # 垫片和服务端两侧都必须真的回到"没有事务"
    assert pgdb._db._tx is None
    assert pgdb._write_conn.is_in_transaction() is False


@pytest.mark.asyncio
async def test_reclaim_dead_worker_tasks_rolls_back_on_failure(pgdb):
    _bid, _tasks = await _seed(pgdb, "rb_reclaim")
    with pytest.raises(Exception):
        # NUL 字节：PG 的服务端错误，事务就地 abort
        await pgdb.reclaim_dead_worker_tasks([f"dead{NUL}w"])
    await _write_path_is_alive(pgdb, "rb_reclaim")


@pytest.mark.asyncio
async def test_release_tasks_rolls_back_on_failure(pgdb):
    _bid, tasks = await _seed(pgdb, "rb_release")
    rel = [{"task_id": t["id"], "lease_epoch": t["lease_epoch"]} for t in tasks]
    with pytest.raises(Exception):
        await pgdb.release_tasks(f"w{NUL}x", rel)
    await _write_path_is_alive(pgdb, "rb_release")


@pytest.mark.asyncio
async def test_prioritize_batch_rolls_back_on_failure(pgdb):
    bid, _tasks = await _seed(pgdb, "rb_prio")
    with pytest.raises(Exception):
        # priority 是 int 列，2**70 在绑定期就被 asyncpg 拒掉
        await pgdb.prioritize_batch(bid, 2 ** 70)
    await _write_path_is_alive(pgdb, "rb_prio")


@pytest.mark.asyncio
async def test_release_tasks_rolls_back_on_cancellation(pgdb):
    """客户端断开 → Starlette 取消请求协程 → CancelledError。

    捕 ``Exception`` 是不够的（``CancelledError`` 继承 ``BaseException``），
    所以这三个块写的是 ``except BaseException``。这条用例守的就是那个选择。
    """
    import asyncio

    _bid, tasks = await _seed(pgdb, "rb_cancel")
    rel = [{"task_id": t["id"], "lease_epoch": t["lease_epoch"]} for t in tasks]

    orig = pgdb._db.execute
    hits = {"n": 0}

    async def slow(sql, params=None):
        hits["n"] += 1
        if hits["n"] == 2:          # 第 2 条 = 事务里的那条 UPDATE
            await asyncio.sleep(5)
        return await (orig(sql) if params is None else orig(sql, params))

    pgdb._db.execute = slow
    try:
        t = asyncio.ensure_future(pgdb.release_tasks("W1", rel))
        await asyncio.sleep(0.2)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    finally:
        pgdb._db.execute = orig

    await _write_path_is_alive(pgdb, "rb_cancel")


# --------------------------------------------------------------------------
# D4 —— ORDER BY 并列时返回的是不同的**行集合**
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_failures_order_is_a_total_order(pgdb):
    """``get_batch_failures``（`/api/batches/{batch_id}/failures` 背后那个函数）的
    ``ORDER BY updated_at DESC NULLS LAST, id DESC`` 必须落在确定的行集合上。

    updated_at 全部相同 → 只有 ``, id DESC`` 能把它变成全序。

    历史：这条不变式原来在 ``/api/batches/{batch_name}/errors``（旧接口，已删除）
    的一份重复内联 SQL 上单独测过一遍——那份 SQL 现在不存在了，直接测
    ``get_batch_failures`` 本身。
    """
    bid = await pgdb.create_batch("d4_failures")
    n = 30
    await pgdb.create_tasks(bid, [f"B0TIE{i:05d}" for i in range(1, n + 1)])
    tasks = await pgdb.pull_tasks("W1", count=n)
    # variant_offset 的失败上限是 1，所以一次提交就全部终态失败，
    # 而且整批共用**同一个** now → updated_at 全并列（这正是 D4 的前提）
    res = await pgdb.accept_results_batch([
        {"task_id": t["id"], "worker_id": "W1", "lease_epoch": t["lease_epoch"],
         "batch_id": bid, "success": False,
         "data": {"error_type": "variant_offset", "error_detail": "d"}}
        for t in tasks
    ])
    assert res["failed"] == n

    failures = await pgdb.get_batch_failures(bid, limit=200)
    rows = [r["asin"] for r in failures]

    # 一次提交盖同一个秒级时间戳 → 全部并列 → 顺序完全由 id DESC 决定
    assert rows == [f"B0TIE{i:05d}" for i in range(n, 0, -1)]


@pytest.mark.asyncio
async def test_running_batches_rescan_order_is_a_total_order(pgdb):
    """server/app.py:341 的兜底完成扫描：LIMIT 30 必须落在确定的 30 行上。"""
    ids = [await pgdb.create_batch(f"d4r_{i:03d}") for i in range(40)]
    async with pgdb._write_lock:
        await pgdb._db.execute("BEGIN")
        try:
            await pgdb._db.execute(
                "UPDATE batches SET status='running', updated_at='2026-01-01 00:00:00'")
            # 让前 15 行的新版本挪到堆尾，破坏"堆序 == 插入序"
            for _ in range(2):
                await pgdb._db.execute(
                    "UPDATE batches SET callback_last_error='x' "
                    "WHERE id = ANY(?::bigint[])", (ids[:15],))
            await pgdb._db.execute("COMMIT")
        except BaseException:
            try:
                await pgdb._db.execute("ROLLBACK")
            except BaseException:
                pass
            raise

    sql = ("SELECT id FROM batches WHERE status='running' "
           "ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 30")
    assert _app_has("ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 30"), \
        "server/app.py 的兜底完成扫描丢了 tiebreaker"
    async with pgdb.read() as rc, rc.execute(sql) as c:
        got = [r[0] for r in await c.fetchall()]

    assert got == sorted(ids, reverse=True)[:30]


# --------------------------------------------------------------------------
# D5 / D6
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_pending_screenshots_is_id_ordered(pgdb):
    """LIMIT 必须配 ORDER BY：SQLite 走 rowid（= id）序，PG 走堆序，UPDATE 后就漂。"""
    bid = await pgdb.create_batch("d5_ss", needs_screenshot=True)
    asins = [f"S{i:03d}" for i in range(1, 21)]
    await pgdb.create_tasks(bid, asins, "10001", True)
    # done → pending 的 churn（截图重试的真实路径）把前 8 行挪到堆尾
    for a in asins[:8]:
        await pgdb.update_screenshot_status(a, bid, "done", file_path=f"/x/{a}.png")
    for a in asins[:8]:
        await pgdb.update_screenshot_status(a, bid, "pending")

    got = await pgdb.get_pending_screenshots(bid, limit=5)
    assert [r["asin"] for r in got] == ["S001", "S002", "S003", "S004", "S005"]
    assert [r["id"] for r in got] == sorted(r["id"] for r in got)


@pytest.mark.asyncio
async def test_create_batch_none_returns_zero_like_sqlite(pgdb):
    """SQLite 的 INSERT OR IGNORE 吞掉 NOT NULL 违例 → 返回 0，不是抛异常。"""
    assert await pgdb.create_batch(None) == 0
    # 连接必须还能用，而且 identity **照样烧号**（两个后端实测一致）
    assert await pgdb.create_batch("d6_ok") == 2
    assert await pgdb.create_batch("d6_ok2") == 3
