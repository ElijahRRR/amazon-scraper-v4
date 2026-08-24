"""tests/test_batch_delete_chunking.py —— 删批次按批次分块，别撞命令超时。

线上症状（2026-08）：勾选历史批次删除，提示「删除失败」。

两个独立的坑，都在这一条路径上：

1. **前端把原因吞了**（本文件不覆盖，见 base.html 的 ``window.apiErrText``）：
   单条删除 ``await fetch(...); location.reload();`` —— 完全不看响应，被 401
   挡下时页面照样刷新、批次还在原地、一句提示都没有；批量删除只读
   ``data.detail``，而 authz 中间件的 401 响应体是 ``{"error", "message"}``，
   于是那句写好的中文说明永远显示不出来，用户看到的是「删除失败：401」。

2. **一条 DELETE 删掉全部选中批次**：超时是**按语句**计的
   （asyncpg 的 command_timeout，见 config.PG_COMMAND_TIMEOUT，默认 60s）。
   ``DELETE ... WHERE batch_id IN (...500 个 id...)`` 在百万行量级的库上是
   一次无上界的操作，删得越多越久，某天就跨过 60s -> 整个事务回滚 -> 500。

本文件守第 2 条：每张子表 × 每个批次一条语句，语句的工作量被"单个批次的
行数"钉住，与一次选了多少个批次无关。
"""
import pytest

from common.core.dbtables import BATCH_DELETE_CHUNK

TABLES = ("tasks", "batch_asins", "screenshots", "asin_changes")


async def _collect_delete_sqls(db, batch_ids):
    """跑一次 delete_batches，收集它发出的所有 DELETE 语句。"""
    sqls = []
    real = type(db._db).execute

    def spy(self, sql, params=None, _real=real):
        if "DELETE" in str(sql).upper():
            sqls.append((str(sql), list(params or [])))
        return _real(self, sql, params)

    type(db._db).execute = spy
    try:
        await db.delete_batches(batch_ids)
    finally:
        type(db._db).execute = real
    return sqls


@pytest.mark.asyncio
async def test_child_table_deletes_are_chunked(pgdb):
    """子表 DELETE 按 BATCH_DELETE_CHUNK 个批次一组，**既不是一条也不是每批次一条**。

    两个方向都要守：
      * 一条 `IN (全部 id)` -> 单条语句工作量无上界 -> 撞 60s 命令超时 -> 整事务回滚；
      * 每批次一条          -> 语句数 4N，而 statement_cache_size=0（D-7）让每条
                              都要重新 Parse/Bind/Execute，这些往返全在写锁内。
                              实测 500 批 × 每表 20 行：一条 28ms、每批次 264ms、
                              50 个一组 29ms。

    用 **CHUNK+3 个批次** 才测得到分块本身 —— 少于一组的话两种写法看起来一样。
    """
    n = BATCH_DELETE_CHUNK + 3
    ids = [await pgdb.create_batch(f"del-{i}") for i in range(n)]
    sqls = await _collect_delete_sqls(pgdb, ids)

    import math
    expect_stmts = math.ceil(n / BATCH_DELETE_CHUNK)
    for tbl in TABLES:
        hits = [(s, p) for s, p in sqls if f"FROM {tbl} " in s]
        assert len(hits) == expect_stmts, \
            f"{tbl}: {n} 个批次应发 {expect_stmts} 条（每组 {BATCH_DELETE_CHUNK}），实际 {len(hits)}"
        covered = []
        for sql, params in hits:
            # 单条语句的工作量被"一组批次的行数"钉死 —— 这是防 60s 超时的全部要害
            assert len(params) <= BATCH_DELETE_CHUNK, (len(params), sql)
            covered += list(params)
        # 每个 id 被覆盖且只覆盖一次
        assert sorted(covered) == sorted(ids), (len(covered), len(ids))


@pytest.mark.asyncio
async def test_batches_table_itself_stays_one_statement(pgdb):
    """batches 一行一个批次，不需要拆——拆了只是白白多几次往返。"""
    ids = [await pgdb.create_batch(f"keep-{i}") for i in range(3)]
    sqls = await _collect_delete_sqls(pgdb, ids)
    hits = [(s, p) for s, p in sqls if "FROM batches " in s]
    assert len(hits) == 1, hits
    assert sorted(hits[0][1]) == sorted(ids)


async def _seed_child_rows(db, batch_ids):
    """给每个批次在四张子表里各插一行 —— 原子性断言必须**看得见子表**。"""
    for b in batch_ids:
        await db._db.execute(
            "INSERT INTO tasks (batch_id, asin, status, created_at, updated_at)"
            " VALUES (?, ?, 'pending', '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (b, f"B0ATOM{b:05d}"))
        await db._db.execute(
            "INSERT INTO batch_asins (batch_id, asin, is_new) VALUES (?, ?, 0)",
            (b, f"B0ATOM{b:05d}"))
        await db._db.execute(
            "INSERT INTO screenshots (batch_id, asin, status, file_path,"
            " created_at, updated_at) VALUES (?, ?, 'done', ?, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (b, f"B0ATOM{b:05d}", f"/shots/{b}.png"))
        await db._db.execute(
            "INSERT INTO asin_changes (asin, batch_id, change_type, change_detail)"
            " VALUES (?, ?, 'new', 'x')", (f"B0ATOM{b:05d}", b))


async def _child_counts(db, batch_ids):
    out = {}
    ph = ",".join("?" * len(batch_ids))
    for tbl in TABLES:
        async with db.read() as rc, rc.execute(
                f"SELECT count(*) FROM {tbl} WHERE batch_id IN ({ph})",
                list(batch_ids)) as c:
            out[tbl] = (await c.fetchone())[0]
    return out


@pytest.mark.asyncio
async def test_delete_is_still_atomic(pgdb):
    """分块只改语句粒度，**不改事务边界**：仍然要么全删、要么全不删。

    ⚠ 这个用例原来只造了 3 个**空**批次，四张子表一行都没有。于是它的两条
    断言（有没有发过 ROLLBACK、batches 表还在不在）都碰不到它自己命名的那个
    不变量 —— batches 的 DELETE 是最后一条语句，中途失败时压根没执行过。

    对抗式评审用一个变异证明了它是空转的：在逐表循环末尾插一句
    `COMMIT; BEGIN`（"每张表提交一次"这种把分组顺手做成分事务的改法），
    中途失败时 tasks/batch_asins/screenshots 已经提交、只有 asin_changes
    回滚 —— 批次半删，用户看到 500 但数据已经缺了一半，而这个用例照样绿。

    现在四张子表都预先插了行，断言"回滚之后一行都不许少"。
    """
    ids = [await pgdb.create_batch(f"atomic-{i}") for i in range(3)]
    await _seed_child_rows(pgdb, ids)
    before = await _child_counts(pgdb, ids)
    assert all(v == len(ids) for v in before.values()), before

    sqls_seen = []
    real = type(pgdb._db).execute
    boom = ids[-1]

    def spy(self, sql, params=None, _real=real):
        s = str(sql)
        sqls_seen.append(s)
        # 在最后一张子表（asin_changes）上炸，模拟中途失败
        if "asin_changes" in s and params and boom in list(params):
            raise RuntimeError("模拟中途失败")
        return _real(self, sql, params)

    type(pgdb._db).execute = spy
    try:
        with pytest.raises(RuntimeError):
            await pgdb.delete_batches(ids)
    finally:
        # ⚠ 必须在 finally 里还原：这是替换**类属性**，漏掉一次就会污染
        # 同进程后续所有用例（而且症状是别处莫名其妙地红）。
        type(pgdb._db).execute = real

    assert any("ROLLBACK" in s.upper() for s in sqls_seen), "失败路径必须回滚"
    remaining = {b["id"] for b in await pgdb.get_batches()}
    assert set(ids) <= remaining, "回滚之后三个批次都该还在"
    after = await _child_counts(pgdb, ids)
    assert after == before, f"回滚之后子表行数变了：{before} -> {after}"


# ==================== SQLite 侧：本文件原来一条都没覆盖 ====================

@pytest.mark.asyncio
async def test_sqlite_delete_is_chunked_too(tmp_path):
    """⚠ 本文件三条用例全用 `pgdb` 夹具，**只跑 PostgreSQL**。

    SQLite 侧 common/database.py 做了逐字相同的分组改造（同一段 60s 超时论证
    写在两处注释里），但对抗式评审证明：把它改回 `IN (全部 id)`，全套测试
    照样全绿 —— tests/pgdb/test_admin.py 那条跨后端用例比的是返回的截图路径
    与关联表清理面（而且 sorted() 过），对**语句粒度**完全不敏感。

    于是"修好了 60s 超时"这件事在 SQLite 后端上可以被悄悄退回去。这条网守它。
    """
    import math
    from common.database import Database as SqliteDatabase

    db = SqliteDatabase(str(tmp_path / "data" / "scraper.db"))
    await db.connect()
    try:
        n = BATCH_DELETE_CHUNK + 3
        ids = [await db.create_batch(f"sq-{i}") for i in range(n)]
        sqls = await _collect_delete_sqls(db, ids)

        expect_stmts = math.ceil(n / BATCH_DELETE_CHUNK)
        for tbl in TABLES:
            hits = [(s, p) for s, p in sqls if f"FROM {tbl} " in s]
            assert len(hits) == expect_stmts, \
                f"{tbl}: SQLite 侧 {n} 个批次应发 {expect_stmts} 条，实际 {len(hits)}"
            covered = []
            for sql, params in hits:
                assert len(params) <= BATCH_DELETE_CHUNK, (len(params), sql)
                covered += list(params)
            assert sorted(covered) == sorted(ids)
        # batches 本身仍是一条
        assert len([1 for s, _ in sqls if "FROM batches " in s]) == 1
    finally:
        await db.close()
