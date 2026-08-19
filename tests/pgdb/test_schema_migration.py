"""老库升级：`CREATE TABLE IF NOT EXISTS` **不会**给已存在的表加列。

PG 侧没有 SQLite 那套 ALTER 阶梯（`common/pgdb/schema.py` 文件头解释了为什么
不移植），所以新列要靠 `DDL_ALTERS` 里的幂等 ALTER 补。**漏掉那一条，
生产库（表早已存在）就永远拿不到新列** —— 而新建库、以及所有用
`scratch_database` 建的测试库都是全新的，一个都照不出来。

本文件专门造出"表已存在但少一列"的状态来照它。
"""
from __future__ import annotations

import pytest

from common.pgdb.schema import DDL_ALTERS, EXPECTED_COLUMNS


def _alter_added_columns():
    """`DDL_ALTERS` 声称要补的 asin_data 列。"""
    import re
    cols = []
    for stmt in DDL_ALTERS:
        m = re.search(
            r'ALTER TABLE asin_data ADD COLUMN IF NOT EXISTS\s+"?(\w+)"?', stmt)
        if m:
            cols.append(m.group(1))
    return cols


@pytest.mark.asyncio
async def test_alter_ladder_is_not_empty():
    """至少有一条 —— 空表说明有人把迁移删了，或者抽取正则失配了。

    单独一条用例是因为下面那条会 `pytest.skip` 掉空表的情况，
    而"迁移列表空了"和"这个环境没 PG"是两件完全不同的事。
    """
    assert _alter_added_columns(), (
        "DDL_ALTERS 里抽不到任何 asin_data 的 ADD COLUMN。"
        "要么迁移被删了，要么语句写法变了而这里的正则没跟上")


@pytest.mark.asyncio
async def test_existing_table_gets_the_new_columns_back(pgdb):
    """**这一条对应"漏写迁移"那个 bug。**

    造法：把 `DDL_ALTERS` 补的列一个个 DROP 掉（模拟升级前的老库），
    再跑一次 `init_tables()`，列必须全部回来、且 `verify_schema` 重新通过。

    实测（本 PR 开发时）：把那条 ALTER 从 `DDL_ALTERS` 里删掉 -> 本条转红；
    只靠源码级守卫时它是**全绿**的。
    """
    conn = pgdb._write_conn
    cols = _alter_added_columns()

    for col in cols:
        await conn.execute(f"ALTER TABLE asin_data DROP COLUMN IF EXISTS {col}")

    actual = await _columns(conn)
    for col in cols:
        assert col not in actual, f"前置条件不成立：{col} 没被 DROP 掉"
    # 老库状态下 verify_schema 必须**拦下来**（少列就是少列，不许放行）
    problems = await pgdb.verify_schema(strict=False)
    assert problems, "少了列却说 schema 没问题 —— verify_schema 形同虚设"

    await pgdb.init_tables()

    actual = await _columns(conn)
    for col in cols:
        assert col in actual, (
            f"{col} 没被补回来 —— CREATE TABLE IF NOT EXISTS 对已存在的表是 "
            "no-op，必须在 DDL_ALTERS 里有一条对应的 ADD COLUMN IF NOT EXISTS")
    assert actual == EXPECTED_COLUMNS["asin_data"], (
        "补回来了但列序不对。ALTER 只能追加到末尾，所以新列在 DDL 里也必须"
        "写在末尾，否则新建库与升级库的列序会分叉")
    await pgdb.verify_schema(strict=True)


@pytest.mark.asyncio
async def test_alter_ladder_is_idempotent(pgdb):
    """反复跑不炸、也不重复加列 —— init_tables 每次启动都会跑一遍。"""
    before = await _columns(pgdb._write_conn)
    await pgdb.init_tables()
    await pgdb.init_tables()
    assert await _columns(pgdb._write_conn) == before


@pytest.mark.asyncio
async def test_migration_preserves_existing_rows(pgdb):
    """升级不许动老数据：补列之后老行还在，新列是 NULL。"""
    conn = pgdb._write_conn
    cols = _alter_added_columns()
    for col in cols:
        await conn.execute(f"ALTER TABLE asin_data DROP COLUMN IF EXISTS {col}")
    await conn.execute(
        "INSERT INTO asin_data (asin, title) VALUES ('B0OLDROW01', '升级前就有的')")

    await pgdb.init_tables()

    row = await conn.fetchrow(
        "SELECT asin, title, " + ", ".join(cols) + " FROM asin_data "
        "WHERE asin = 'B0OLDROW01'")
    assert row["title"] == "升级前就有的", "老行被改了"
    for col in cols:
        assert row[col] is None, f"{col} 应当是 NULL（老行没有这个值）"


async def _columns(conn):
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='asin_data' "
        "ORDER BY ordinal_position")
    return [r["column_name"] for r in rows]
