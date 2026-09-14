"""tests/pgdb/test_change_detection_switch.py —— 变动检测总开关。

2026-09 起 `config.CHANGE_DETECTION_ENABLED` 默认 **False**：本部署没有消费方，
而它的代价全落在写锁内（每次保存一次 baseline 比对 + 有变动时 1~2 条 INSERT，
`asin_changes` 曾涨到约 140 万行，持续写入不断把可见性图打脏）。

这个文件有两个职责，缺一不可：

  1. 证明关掉之后**真的不写** —— 否则"关了"只是个说法；
  2. 把开关拨到 True 再跑一遍检测逻辑 —— 生产上这条路径不再执行，如果没有
     用例覆盖它，它会在后续重构里静默腐烂，而等到哪天想重新打开时已经坏了。

⚠ 第 2 点是本文件存在的主要理由。只写第 1 点的话，"删掉整段检测代码"这个变更
  也能让测试全绿。
"""
from __future__ import annotations

import pytest

pytest.importorskip("asyncpg", reason="tests/pgdb 需要 asyncpg")

from common import config


async def _seed_with_baseline(db, asin="B0CHANGE01"):
    """入库一行并把 baseline 写成已知值（变动检测要求 baseline 存在）。"""
    await db.save_result({
        "asin": asin, "title": "Widget One", "brand": "AcmeBrand",
        "current_price": "10.00", "buybox_price": "10.00",
        "stock_count": "5", "stock_status": "In Stock",
    })
    await db._write_conn.execute(
        "UPDATE asin_data SET baseline_price='10.00', baseline_buybox_price='10.00',"
        " baseline_stock_count='5', baseline_stock_status='In Stock'"
        " WHERE asin=$1", asin)
    return asin


async def _changes(db, asin):
    rows = await db._write_conn.fetch(
        "SELECT change_type FROM asin_changes WHERE asin=$1", asin)
    return sorted(r["change_type"] for r in rows)


@pytest.mark.asyncio
async def test_disabled_by_default_writes_nothing(pgdb, monkeypatch):
    """默认配置下：价格明显变了，也不该有任何 asin_changes 行。"""
    monkeypatch.setattr(config, "CHANGE_DETECTION_ENABLED", False)
    asin = await _seed_with_baseline(pgdb)

    await pgdb.save_result({
        "asin": asin, "title": "Widget One", "brand": "AcmeBrand",
        "current_price": "99.99", "buybox_price": "99.99",
        "stock_count": "0", "stock_status": "Out of Stock",
    })

    assert await _changes(pgdb, asin) == [], "开关关着却写了变动记录"
    # 主表当前值**照旧要更新** —— 关掉的是"记变动"，不是"存数据"
    row = await pgdb._write_conn.fetchrow(
        "SELECT current_price FROM asin_data WHERE asin=$1", asin)
    assert row["current_price"] == "99.99"


@pytest.mark.asyncio
async def test_enabling_it_restores_price_stock_detection(pgdb, monkeypatch):
    """开关拨到 True —— 检测逻辑必须还活着。

    这条是防腐烂的那一条：把整段检测代码删掉，上面那条用例依然全绿，这条会红。
    """
    monkeypatch.setattr(config, "CHANGE_DETECTION_ENABLED", True)
    asin = await _seed_with_baseline(pgdb, "B0CHANGE02")

    await pgdb.save_result({
        "asin": asin, "title": "Widget One", "brand": "AcmeBrand",
        "current_price": "99.99", "buybox_price": "99.99",
        "stock_count": "0", "stock_status": "Out of Stock",
    })

    assert await _changes(pgdb, asin) == ["price_stock"]


@pytest.mark.asyncio
async def test_enabling_it_restores_title_bullets_detection(pgdb, monkeypatch):
    monkeypatch.setattr(config, "CHANGE_DETECTION_ENABLED", True)
    asin = "B0CHANGE03"
    await pgdb.save_result({
        "asin": asin, "title": "Old Title", "brand": "AcmeBrand",
        "current_price": "10.00", "bullet_points": "a|b",
    })
    row = await pgdb._write_conn.fetchrow(
        "SELECT title_bullets_hash FROM asin_data WHERE asin=$1", asin)
    await pgdb._write_conn.execute(
        "UPDATE asin_data SET baseline_price='10.00',"
        " baseline_title_bullets_hash=$2 WHERE asin=$1", asin, row["title_bullets_hash"])

    await pgdb.save_result({
        "asin": asin, "title": "Brand New Title", "brand": "AcmeBrand",
        "current_price": "10.00", "bullet_points": "a|b",
    })

    assert "title_bullets" in await _changes(pgdb, asin)


@pytest.mark.asyncio
async def test_new_asin_record_is_not_gated(pgdb, monkeypatch):
    """change_type='new' 走 INSERT 分支，**不受这个开关管**。

    这不是漏掉：'new' 不是一次"比对出来的变动"，它是入库事实本身，成本是每个
    ASIN 一次、不随重采增长。把它一起关掉会让 change_filter=new 永久失效，而
    那与"停止比对"是两件事。这条用例把这个边界钉住。
    """
    monkeypatch.setattr(config, "CHANGE_DETECTION_ENABLED", False)
    asin = "B0CHANGE04"
    # ⚠ 必须带 batch_id：INSERT 分支的 'new' 记录**只在 batch_id 为真时**才写
    #   （results_write.py 文件头第 94 行记着这条）。不带的话这条用例会以
    #   "开关把 new 也关掉了" 的假象失败，而真正的原因是夹具少了参数。
    bid = await pgdb._write_conn.fetchval(
        "INSERT INTO batches (name) VALUES ('b-new') RETURNING id")
    await pgdb.save_result(
        {"asin": asin, "title": "Fresh", "current_price": "1.00"}, batch_id=bid)
    assert await _changes(pgdb, asin) == ["new"]
