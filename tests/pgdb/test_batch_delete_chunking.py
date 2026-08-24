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
async def test_child_table_deletes_are_one_statement_per_batch(pgdb):
    """三个批次 × 四张子表 = 12 条独立 DELETE，每条只带一个 batch_id。"""
    ids = [await pgdb.create_batch(f"del-{i}") for i in range(3)]
    sqls = await _collect_delete_sqls(pgdb, ids)

    for tbl in TABLES:
        hits = [(s, p) for s, p in sqls if f"FROM {tbl} " in s]
        assert len(hits) == len(ids), f"{tbl}: 期望每批次一条，实际 {len(hits)} 条"
        for sql, params in hits:
            # ⚠ 这两条断言是这个修复的全部要害：不是 IN (...)，而是 = ? 且只带一个参数。
            #   放松成 "IN 也行" 等于把 60s 超时那条路重新打开。
            assert "batch_id = ?" in sql or "batch_id = $1" in sql, sql
            assert " IN (" not in sql, sql
            assert len(params) == 1, (sql, params)
        assert sorted(p[0] for _, p in hits) == sorted(ids)


@pytest.mark.asyncio
async def test_batches_table_itself_stays_one_statement(pgdb):
    """batches 一行一个批次，不需要拆——拆了只是白白多几次往返。"""
    ids = [await pgdb.create_batch(f"keep-{i}") for i in range(3)]
    sqls = await _collect_delete_sqls(pgdb, ids)
    hits = [(s, p) for s, p in sqls if "FROM batches " in s]
    assert len(hits) == 1, hits
    assert sorted(hits[0][1]) == sorted(ids)


@pytest.mark.asyncio
async def test_delete_is_still_atomic(pgdb):
    """分块只改语句粒度，**不改事务边界**：仍然要么全删、要么全不删。"""
    ids = [await pgdb.create_batch(f"atomic-{i}") for i in range(3)]
    sqls_seen = []
    real = type(pgdb._db).execute
    boom = ids[-1]

    def spy(self, sql, params=None, _real=real):
        s = str(sql)
        sqls_seen.append(s)
        # 在最后一个批次的 asin_changes 上炸，模拟中途失败
        if "asin_changes" in s and params and list(params)[:1] == [boom]:
            raise RuntimeError("模拟中途失败")
        return _real(self, sql, params)

    type(pgdb._db).execute = spy
    try:
        with pytest.raises(RuntimeError):
            await pgdb.delete_batches(ids)
    finally:
        type(pgdb._db).execute = real

    assert any("ROLLBACK" in s.upper() for s in sqls_seen), "失败路径必须回滚"
    remaining = {b["id"] for b in await pgdb.get_batches()}
    assert set(ids) <= remaining, "回滚之后三个批次都该还在"
