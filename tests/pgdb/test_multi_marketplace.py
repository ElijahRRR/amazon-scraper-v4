"""F-012 多站点采集：端到端的库行为。

------------------------------------------------------------------------
这份文件回答的问题
------------------------------------------------------------------------
「同一个 ASIN 在美国站和加拿大站，两份数据会不会互相覆盖？」

改造前答案是**会**，而且不报错：``asin_data.asin`` 是 ``UNIQUE``，
每个 ASIN 全库只有一行快照，后采的盖掉先采的。
``tests/test_multi_zip_same_asin.py`` 把这条写成了成文事实（第 2 条硬事实）。

对邮编来说那个行为是可以接受的取舍（同一个商品、不同配送区的价格）；
对站点**不行** —— 美加两站的同一个 ASIN 是两件不同的商品数据：
币种不同、卖家不同、库存不同、配送不同。盖掉就是数据错误。

F-012 把唯一键扩成 ``(asin, marketplace)``。本文件逐条钉住扩完之后的行为，
以及「站点从建批次一路流到详情任务」那条链路 —— 那是改造前最隐蔽的一条
静默错误的现场。

------------------------------------------------------------------------
只在 PostgreSQL 上跑
------------------------------------------------------------------------
与 tests/pgdb 下其余文件同一口径：夹具连不上 PG 就整类 skip。
"""
from __future__ import annotations

import pytest

from common.core import searchurl

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- 小助手

async def _rows(db, sql, params=()):
    async with db.read() as rc, rc.execute(sql, params) as c:
        return [dict(r) for r in await c.fetchall()]


def _payload(asin, *, marketplace, price, title="T"):
    """一条最小的、能通过 _is_parse_failure 的提交体。

    ``stock_count='0'`` 不是凑数：服务端的 ``_is_parse_failure`` 要求
    current_price/buybox_price/stock_count/stock_status/brand **全部**落在
    _NA_VALUES 里才判解析失败，而被判失败的提交**一个字段都不落库**。
    """
    return {
        "asin": asin,
        "title": title,
        "brand": "B",
        "current_price": price,
        "buybox_price": price,
        "stock_count": "0",
        "stock_status": "In Stock",
        "marketplace": marketplace,
    }


# ================================================================== 存储维度

async def test_same_asin_on_two_sites_does_not_overwrite(pgdb):
    """**本次改造的核心**：同一个 ASIN 在两个站点各占一行，互不覆盖。"""
    await pgdb.save_result(_payload("B0SAME00001", marketplace="amazon.com",
                                    price="$19.99", title="US title"))
    await pgdb.save_result(_payload("B0SAME00001", marketplace="amazon.ca",
                                    price="$27.99", title="CA title"))

    rows = await _rows(pgdb,
                       "SELECT asin, marketplace, title, current_price "
                       "FROM asin_data WHERE asin = ? ORDER BY marketplace",
                       ("B0SAME00001",))
    assert len(rows) == 2, "两个站点必须各占一行 —— 这正是改造前会丢的那一行"
    assert rows[0]["marketplace"] == "amazon.ca"
    assert rows[0]["current_price"] == "$27.99"
    assert rows[0]["title"] == "CA title"
    assert rows[1]["marketplace"] == "amazon.com"
    assert rows[1]["current_price"] == "$19.99"
    assert rows[1]["title"] == "US title", "美国站那一行必须没被加拿大站碰过"


async def test_second_submit_on_the_same_site_still_updates_in_place(pgdb):
    """同站点重复提交仍然是**更新**，不是插第二行。

    反向哨兵：唯一键扩维之后最容易写错的方向是"每次采集都插新行"，
    那会让 asin_data 从"最新态快照"变成一张没有版本语义的追加表。
    """
    await pgdb.save_result(_payload("B0UPD000001", marketplace="amazon.com",
                                    price="$10.00"))
    await pgdb.save_result(_payload("B0UPD000001", marketplace="amazon.com",
                                    price="$12.00"))
    rows = await _rows(pgdb, "SELECT current_price FROM asin_data WHERE asin = ?",
                       ("B0UPD000001",))
    assert len(rows) == 1
    assert rows[0]["current_price"] == "$12.00"


async def test_missing_marketplace_falls_back_to_us(pgdb):
    """老 worker 不提交这个字段 —— 它们采的确实是美国站。"""
    data = _payload("B0LEGACY001", marketplace="amazon.com", price="$5.00")
    del data["marketplace"]
    await pgdb.save_result(data)
    rows = await _rows(pgdb, "SELECT marketplace FROM asin_data WHERE asin = ?",
                       ("B0LEGACY001",))
    assert rows[0]["marketplace"] == "amazon.com"


async def test_unknown_marketplace_is_rejected_loudly(pgdb):
    """站点不在注册表里就**整条提交失败**，不静默落进美国站那一行。

    静默回退会把一条来路不明的数据写进美国站、覆盖真数据，
    而且不报错、值还长得像真的 —— 那正是 F-012 要消灭的故障形态。
    """
    with pytest.raises(ValueError):
        await pgdb.save_result(_payload("B0BADMKT001", marketplace="walmart.com",
                                        price="$1.00"))
    rows = await _rows(pgdb, "SELECT 1 FROM asin_data WHERE asin = ?",
                       ("B0BADMKT001",))
    assert rows == [], "被拒的提交不许留下任何行"


# ================================================================== 任务链路

async def test_create_tasks_carries_the_marketplace(pgdb):
    await pgdb._db.execute("INSERT INTO batches (name) VALUES ('ca_batch')")
    bid = (await _rows(pgdb, "SELECT id FROM batches WHERE name='ca_batch'"))[0]["id"]
    await pgdb.create_tasks(bid, ["B0TASK00001"], zip_code="M5V 3L9",
                            marketplace="amazon.ca")
    rows = await _rows(pgdb, "SELECT asin, zip_code, marketplace FROM tasks")
    assert rows[0]["marketplace"] == "amazon.ca"
    assert rows[0]["zip_code"] == "M5V 3L9"


async def test_pull_tasks_hands_the_marketplace_to_the_worker(pgdb):
    """worker 靠这个字段决定去哪个站点抓 —— 漏了它就等于没做这次改造。"""
    await pgdb._db.execute("INSERT INTO batches (name) VALUES ('ca_pull')")
    bid = (await _rows(pgdb, "SELECT id FROM batches WHERE name='ca_pull'"))[0]["id"]
    await pgdb.create_tasks(bid, ["B0PULL00001"], zip_code="M5V 3L9",
                            marketplace="amazon.ca")
    tasks = await pgdb.pull_tasks("w-ca", 10)
    assert len(tasks) == 1
    assert tasks[0]["marketplace"] == "amazon.ca"


async def test_search_batch_derives_the_marketplace_from_the_domain(pgdb):
    """``domain=www.amazon.ca`` 必须决定**整条链路**的站点，不只是搜索页 URL。

    这是改造前那条静默错误的入口：domain 只影响 worker 翻页的 URL，
    建出来的任务没有站点字段。
    """
    params = searchurl.normalize_search_params({"domain": "www.amazon.ca"})
    bid, n = await pgdb.create_search_batch(
        name="kw_ca", keywords=["wireless mouse"], search_params=params,
        discover_mode="with_detail", zip_code="M5V 3L9")
    assert n == 1
    rows = await _rows(pgdb, "SELECT task_type, marketplace FROM tasks "
                             "WHERE batch_id = ?", (bid,))
    assert rows[0]["task_type"] == "discover_search"
    assert rows[0]["marketplace"] == "amazon.ca"


async def test_derived_detail_tasks_inherit_the_marketplace(pgdb):
    """**改造前那条静默错误的现场**。

    加拿大站关键词批次发现出来的 ASIN，派生的详情任务必须仍然是加拿大站。
    改造前这里派出来的是美国站任务：批次名、进度、发现数全部正常，
    只有数据是另一个国家的，两侧都不会响。
    """
    params = searchurl.normalize_search_params({"domain": "www.amazon.ca"})
    bid, _ = await pgdb.create_search_batch(
        name="kw_ca_detail", keywords=["usb hub"], search_params=params,
        discover_mode="with_detail", zip_code="M5V 3L9")

    t = (await _rows(pgdb, "SELECT id, lease_epoch FROM tasks "
                           "WHERE batch_id = ? AND task_type = 'discover_search'",
                     (bid,)))[0]
    await pgdb._db.execute(
        "UPDATE tasks SET status='processing', worker_id='w1' WHERE id=?", (t["id"],))
    res = await pgdb.accept_search_discovery_result(
        task_id=t["id"], worker_id="w1", lease_epoch=t["lease_epoch"],
        batch_id=bid, keyword="usb hub",
        items=[{"asin": "B0DERIVED01"}], meta={})
    assert res["accepted"] and res["detail_tasks_created"] == 1

    detail = (await _rows(pgdb, "SELECT asin, zip_code, marketplace FROM tasks "
                                "WHERE batch_id = ? AND task_type = 'asin'",
                          (bid,)))[0]
    assert detail["marketplace"] == "amazon.ca", (
        "派生详情任务丢了站点 —— 这批数据会从 amazon.com 采回来")
    assert detail["zip_code"] == "M5V 3L9"


async def test_is_new_is_judged_per_marketplace(pgdb):
    """一个 ASIN 在加拿大站是**新的**，哪怕美国站早就采过。

    ``is_new`` 驱动的是「这批里哪些是首次见到」，而两个站点的同一个 ASIN
    是两件不同的商品数据。
    """
    await pgdb.save_result(_payload("B0NEWJUDGE1", marketplace="amazon.com",
                                    price="$9.99"))
    await pgdb._db.execute("INSERT INTO batches (name) VALUES ('ca_new')")
    bid = (await _rows(pgdb, "SELECT id FROM batches WHERE name='ca_new'"))[0]["id"]
    await pgdb.create_tasks(bid, ["B0NEWJUDGE1"], zip_code="M5V 3L9",
                            marketplace="amazon.ca")
    rows = await _rows(pgdb, "SELECT is_new FROM batch_asins WHERE batch_id = ?",
                       (bid,))
    assert rows[0]["is_new"] == 1, (
        "美国站采过不代表加拿大站采过 —— 按 (asin, marketplace) 判新旧")


# ================================================================== 事件流

async def test_event_stream_accepts_and_preserves_the_marketplace(pgdb):
    """事件的 marketplace 必须是**采集来源站点**，不是写死的 amazon.com。

    asin_data 与 scrape_events 正是要靠这一列 join 起来的（下游 erpAPI 的
    复合主键就是 (marketplace, asin)）。两边不一致就 join 不上。
    """
    import json

    await pgdb.save_result(_payload("B0EVENT0001", marketplace="amazon.ca",
                                    price="$31.50"))
    rows = await pgdb._write_conn.fetch(
        "SELECT body FROM scraper.scrape_outbox ORDER BY id")
    assert rows, "没有事件产出 —— 事件流没接上"
    bodies = [json.loads(r["body"]) if isinstance(r["body"], str) else r["body"]
              for r in rows]
    mine = [b for b in bodies if b.get("asin") == "B0EVENT0001"]
    assert mine, "本次提交没有对应事件"
    assert mine[0]["marketplace"] == "amazon.ca"
    # 提交体里那一份也必须是加拿大站：relay 落库时读的就是它。
    assert mine[0]["result"]["marketplace"] == "amazon.ca"
