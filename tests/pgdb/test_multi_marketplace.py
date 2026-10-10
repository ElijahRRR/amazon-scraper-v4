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


# ================================================================== 批次读写路径（blocker 3）
#
# 与 tests/test_results_marketplace.py 同一组场景的 PG 孪生（那边走 HTTP、跟着
# DB_BACKEND 跑；这里直接调库，只在 PG 上跑）。批次 -> 站点的唯一真源是 tasks 行，
# 批次读写路径必须按它钉住 asin_data 的站点。

_X, _Y, _Z = "B0DUPE0001", "B0USONLY02", "B0CAONLY03"
_CA, _US, _CA_ZIP = "amazon.ca", "amazon.com", "M5V 3L9"


async def _batch(db, name, asins, *, marketplace=None, zip_code="10001",
                 needs_screenshot=False, expand_variants=False):
    bid = await db.create_batch(name, needs_screenshot, expand_variants=expand_variants)
    await db.create_tasks(bid, asins, zip_code, needs_screenshot, marketplace=marketplace)
    return bid


async def _pulled(db, worker_id):
    return {t["asin"]: t for t in await db.pull_tasks(worker_id, 50)}


async def _accept(db, task, worker_id, price, marketplace="<omit>", **extra):
    data = _payload(task["asin"], marketplace=marketplace, price=price)
    if marketplace == "<omit>":
        del data["marketplace"]                 # 老 worker：根本不交这个键
    data.update(extra)
    return await db.accept_success_result(task["id"], worker_id, task["lease_epoch"],
                                          data, task["batch_id"])


async def _two_sites(db, needs_screenshot=False):
    """us_b=[X,Y] 美国站采齐；ca_b=[X,Y,Z] 加拿大站只采到 X、Z（Y 处理中）。"""
    us = await _batch(db, "us_b", [_X, _Y], needs_screenshot=needs_screenshot)
    t = await _pulled(db, "w-us")
    await _accept(db, t[_X], "w-us", "$10.00", variation_asins=f"{_X},B0USFAM001")
    await _accept(db, t[_Y], "w-us", "$20.00")
    ca = await _batch(db, "ca_b", [_X, _Y, _Z], marketplace=_CA, zip_code=_CA_ZIP,
                      needs_screenshot=needs_screenshot)
    t = await _pulled(db, "w-ca")
    await _accept(db, t[_X], "w-ca", "$13.50", marketplace=_CA)
    await _accept(db, t[_Z], "w-ca", "$30.00", marketplace=_CA)
    return us, ca


def _keys(items):
    return sorted((i["asin"], i["marketplace"]) for i in items)


async def test_batch_results_are_pinned_to_the_task_site(pgdb):
    us, ca = await _two_sites(pgdb)
    for kw in ({}, {"change_filter": "new"}, {"search": "B0"},
               {"search": "B0DUPE,B0CAONLY"}):        # 最后一个走 bset CTE
        got = await pgdb.get_results(batch_id=ca, **kw)
        assert _keys(got["items"]) == sorted([(_X, _CA), (_Z, _CA)]), kw
        assert got["total"] == 2, kw
        got = await pgdb.get_results(batch_id=us, **kw)
        want = ([(_X, _US)] if kw.get("search") == "B0DUPE,B0CAONLY"
                else sorted([(_X, _US), (_Y, _US)]))
        assert _keys(got["items"]) == want, kw


async def test_batch_export_has_one_row_per_batch_asin_for_any_page_size(pgdb):
    us, ca = await _two_sites(pgdb)
    for bid, want in ((us, [(_X, _US), (_Y, _US)]),
                      (ca, [(_Z, _CA), (_X, _CA), (_Y, None)])):   # ba.asin 升序
        for bs in (1, 2, 500):
            got = [(r["asin"], r["marketplace"])
                   async for r in pgdb.iter_results(bid, batch_size=bs)]
            assert got == want, (bid, bs)


async def test_result_by_asin_prefers_the_us_row(pgdb):
    await _two_sites(pgdb)
    assert (await pgdb.get_result_by_asin(_X))["marketplace"] == _US
    assert (await pgdb.get_result_by_asin(_X, marketplace=_CA))["current_price"] == "$13.50"
    assert await pgdb.get_result_by_asin(_Z) is None   # 不传站点 = 美国站，加拿大独有不返回
    assert (await pgdb.get_result_by_asin(_Z, marketplace=_CA))["marketplace"] == _CA
    assert await pgdb.get_result_by_asin(_Y, marketplace=_CA) is None
    with pytest.raises(ValueError):
        await pgdb.get_result_by_asin(_X, marketplace="amazon.xx")


async def test_screenshots_do_not_cross_sites(pgdb):
    us, ca = await _two_sites(pgdb, needs_screenshot=True)
    assert await pgdb.update_screenshot_status(_X, us, "done", "/p/us.png") is True
    rows = await _rows(pgdb, "SELECT marketplace, screenshot_path FROM asin_data "
                             "WHERE asin = ? ORDER BY marketplace", (_X,))
    assert rows == [{"marketplace": _CA, "screenshot_path": None},
                    {"marketplace": _US, "screenshot_path": "/p/us.png"}]
    # 读路径的兜底也按站点：加拿大行不借美国截图
    assert await pgdb._get_done_screenshot_paths([_X], marketplace=_CA) == {}
    assert await pgdb._get_done_screenshot_paths([_X], marketplace=_US) == {_X: "/p/us.png"}
    assert (await pgdb.get_result_by_asin(_X, marketplace=_CA))["screenshot_path"] is None
    assert await pgdb.update_screenshot_status(_X, ca, "done", "/p/ca.png") is True
    rows = await _rows(pgdb, "SELECT marketplace, screenshot_path FROM asin_data "
                             "WHERE asin = ? ORDER BY marketplace", (_X,))
    assert rows == [{"marketplace": _CA, "screenshot_path": "/p/ca.png"},
                    {"marketplace": _US, "screenshot_path": "/p/us.png"}]


async def test_variant_expansion_stays_on_the_batch_site(pgdb):
    await _two_sites(pgdb)                               # 美国 X 的家族：B0USFAM001
    ca = await _batch(pgdb, "ca_fam", [_X], marketplace=_CA, zip_code=_CA_ZIP,
                      expand_variants=True)
    t = (await _pulled(pgdb, "w-fam"))[_X]
    # 加拿大行没有家族：不许借美国家族
    await _accept(pgdb, t, "w-fam", "$13.50", marketplace=_CA)
    assert await pgdb.expand_batch_variants(ca) == 0
    # 加拿大行有了自己的家族：只展开它，兄弟款带同一条任务行的站点与邮编
    await pgdb.save_result({**_payload(_X, marketplace=_CA, price="$13.50"),
                            "variation_asins": f"{_X},B0CAFAM001"})
    assert await pgdb.expand_batch_variants(ca) == 1
    rows = await _rows(pgdb, "SELECT asin, zip_code, marketplace FROM tasks "
                             "WHERE batch_id = ? ORDER BY id", (ca,))
    assert rows == [{"asin": _X, "zip_code": _CA_ZIP, "marketplace": _CA},
                    {"asin": "B0CAFAM001", "zip_code": _CA_ZIP, "marketplace": _CA}]
    assert await pgdb.expand_batch_variants(ca) == 0     # 收敛


async def test_old_worker_cannot_write_a_ca_task_into_the_us_row(pgdb):
    await _two_sites(pgdb)
    ca = await _batch(pgdb, "ca_old", [_X], marketplace=_CA, zip_code=_CA_ZIP)
    t = (await _pulled(pgdb, "w-old"))[_X]
    before = await _rows(pgdb, "SELECT marketplace, current_price FROM asin_data "
                               "WHERE asin = ? ORDER BY marketplace", (_X,))
    res = await _accept(pgdb, t, "w-old", "$99.99")              # 不带 marketplace
    assert res == {"accepted": True, "saved": False, "server_reject": True}
    assert await _rows(pgdb, "SELECT marketplace, current_price FROM asin_data "
                             "WHERE asin = ? ORDER BY marketplace", (_X,)) == before
    task = (await _rows(pgdb, "SELECT status, error_type, error_detail FROM tasks "
                              "WHERE id = ?", (t["id"],)))[0]
    assert task == {"status": "failed", "error_type": "server_reject",
                    "error_detail": "marketplace_mismatch task=amazon.ca payload=amazon.com"}
    # 批量提交同一条规则
    await pgdb.create_tasks(ca, [_Z], _CA_ZIP, marketplace=_CA)
    t2 = (await _pulled(pgdb, "w-old2"))[_Z]
    data = _payload(_Z, marketplace=_US, price="$1.00")
    res = await pgdb.accept_results_batch([{
        "task_id": t2["id"], "worker_id": "w-old2", "lease_epoch": t2["lease_epoch"],
        "batch_id": ca, "data": data, "success": True}])
    assert res == {"accepted": 0, "stale": 0, "failed": 1}
    assert (await pgdb.get_result_by_asin(_Z, marketplace=_US)) is None


async def test_old_worker_rejection_event_is_attributed_to_the_task_site(pgdb):
    """拒收也是一次终态尝试，进事件流；分组键的站点按**任务行**记（服务端赢）。"""
    import json

    ca = await _batch(pgdb, "ca_ev", [_X], marketplace=_CA, zip_code=_CA_ZIP)
    t = (await _pulled(pgdb, "w-ev"))[_X]
    await _accept(pgdb, t, "w-ev", "$99.99")
    rows = await pgdb._write_conn.fetch(
        "SELECT body FROM scraper.scrape_outbox ORDER BY id")
    bodies = [json.loads(r["body"]) if isinstance(r["body"], str) else r["body"]
              for r in rows]
    mine = [b for b in bodies if b.get("task_id") == t["id"]]
    assert len(mine) == 1, bodies
    assert mine[0]["outcome"] == "parse_failed"
    assert mine[0]["error_type"] == "server_reject"
    assert mine[0]["marketplace"] == _CA
    assert mine[0]["batch_id"] == ca


async def test_search_batch_takes_the_marketplace_from_the_caller(pgdb):
    """HTTP 边界解析过站点就显式传下来，库层不再按 domain 推导（UI-5）。"""
    params = searchurl.normalize_search_params({"domain": "www.amazon.de"})
    bid, n = await pgdb.create_search_batch(
        name="kw_de", keywords=["usb hub"], search_params=params,
        discover_mode="discover_only", marketplace=_US)
    assert n == 1
    assert (await _rows(pgdb, "SELECT marketplace FROM tasks WHERE batch_id = ?",
                        (bid,))) == [{"marketplace": _US}]
    # 不传：老行为（按 domain 推导；注册表外的站点照旧 ValueError）
    with pytest.raises(ValueError):
        await pgdb.create_search_batch(name="kw_de2", keywords=["usb hub"],
                                       search_params=params,
                                       discover_mode="discover_only")


# ================================================================== 对抗评审之后补的（同一批修复）
#
# 1) 结果列表批次连接：先证明单一站点、再钉字面量；证明不了才逐行片段。
#    逐行片段在 PG 上是规划器悬崖（`列 = (SubPlan)` 只能套默认选择性 -> 丢掉
#    「主键倒序、凑够一页就停」-> 每次翻页 Seq Scan 整张 asin_data）。
# 2) 窄投影没点 marketplace 时截图兜底也按行的站点。
# 3) 站点拒收之后，老 worker 随后传来的截图不落地。
# 4) get_asin_changes 可以按站点限定（详情端点按返回行的站点传）。


async def _site(db, bid):
    return await db._batch_site_literal(bid)


async def test_batch_site_probe_pins_only_provably_single_site_batches(pgdb):
    us, ca = await _two_sites(pgdb)
    assert await _site(pgdb, us) == "'amazon.com'"
    assert await _site(pgdb, ca) == "'amazon.ca'"
    empty = await pgdb.create_batch("empty_b")
    assert await _site(pgdb, empty) == "'amazon.com'"         # 没有任务行 -> 美国站
    # 加拿大批次里一条没有任务行的 batch_asins：按逐行片段它是美国站 -> 不能钉
    await pgdb._write_conn.execute(
        "INSERT INTO batch_asins (batch_id, asin, is_new) VALUES ($1, 'B0ORPHAN01', 0)", ca)
    assert await _site(pgdb, ca) is None
    await pgdb._write_conn.execute(
        "INSERT INTO batch_asins (batch_id, asin, is_new) VALUES ($1, 'B0ORPHAN01', 0)", us)
    assert await _site(pgdb, us) == "'amazon.com'"            # 美国批次的孤儿仍是美国站
    # 混合批次（同名合并进来另一个站点）：逐行，行集仍按每行任务的站点
    mixed = await _batch(pgdb, "mixed_b", [_X])
    await pgdb.create_tasks(mixed, [_Z], _CA_ZIP, marketplace=_CA)
    assert await _site(pgdb, mixed) is None
    for kw in ({}, {"search": "B0"}, {"search": "B0DUPE,B0CAONLY"},
               {"columns": ["asin", "current_price"]}):
        got = await pgdb.get_results(batch_id=mixed, **kw)
        assert sorted((i["asin"], i["current_price"]) for i in got["items"]) == \
            sorted([(_X, "$10.00"), (_Z, "$30.00")]), kw
        assert got["total"] == 2, kw


async def test_default_batch_page_keeps_the_early_stop_plan(pgdb, pgconn, monkeypatch):
    """美国批次的首屏 / count 查询：计划里不许有 SubPlan，页查询要走主键倒序早停。

    对抗评审在 PG 16 上实测（30 万行）：逐行片段让页查询变成 Seq Scan asin_data +
    Hash Join + 每个批次行一次 SubPlan，10 万行批次翻页 3.5ms -> 628ms。
    字面量钉法恢复 `Index Scan Backward using asin_data_pkey` + Nested Loop。
    """
    import contextlib
    import json

    n = 30000
    await pgconn.execute(
        "INSERT INTO asin_data (asin, marketplace, title, brand, current_price, "
        "created_at, updated_at) "
        "SELECT 'B0' || lpad(g::text, 8, '0'), 'amazon.com', 'T' || g, 'B', '1', "
        "'2026-01-01', '2026-01-01' FROM generate_series(1, $1) g", n)
    bid = await pgconn.fetchval("INSERT INTO batches (name) VALUES ('big') RETURNING id")
    await pgconn.execute(
        "INSERT INTO batch_asins (batch_id, asin, is_new) "
        "SELECT $1, 'B0' || lpad(g::text, 8, '0'), 0 FROM generate_series(1, $2) g "
        "WHERE g % 3 = 0", bid, n)
    await pgconn.execute(
        "INSERT INTO tasks (batch_id, asin, zip_code, status, marketplace) "
        "SELECT $1, 'B0' || lpad(g::text, 8, '0'), '10001', 'done', 'amazon.com' "
        "FROM generate_series(1, $2) g WHERE g % 3 = 0", bid, n)
    await pgconn.execute("ANALYZE")

    seen = []
    cls = type(pgdb)
    orig_read = cls.read

    @contextlib.asynccontextmanager
    async def recording_read(self):
        async with orig_read(self) as rc:
            class _Rec:
                def execute(_self, sql, params=()):
                    seen.append((sql, list(params)))
                    return rc.execute(sql, params)
            yield _Rec()

    monkeypatch.setattr(cls, "read", recording_read)
    res = await pgdb.get_results(batch_id=bid, limit=50, with_total=True)
    monkeypatch.undo()
    assert res["total"] == n // 3 and len(res["items"]) == 50

    page = next(x for x in seen if "FROM asin_data d" in x[0] and "LIMIT" in x[0])
    count = next(x for x in seen if "COUNT(*)" in x[0])
    plans = {}
    for name, (sql, params) in (("page", page), ("count", count)):
        async with pgdb.read() as rc, rc.execute(
                "EXPLAIN (FORMAT JSON) " + sql, params) as c:
            plans[name] = json.dumps([dict(r) for r in await c.fetchall()])
        assert "SubPlan" not in plans[name], (name, plans[name])
        # 站点谓词确实在（钉的是字面量，不是什么都没钉）
        assert "marketplace" in plans[name], (name, plans[name])
    assert "asin_data_pkey" in plans["page"], plans["page"]
    assert "Backward" in plans["page"], plans["page"]


async def test_narrow_projection_screenshot_follows_the_row_site(pgdb):
    us, ca = await _two_sites(pgdb, needs_screenshot=True)
    assert await pgdb.update_screenshot_status(_X, us, "done", "/p/us.png") is True
    narrow = await pgdb.get_results(batch_id=ca, columns=["asin", "current_price"])
    shots = {i["asin"]: i["screenshot_path"] for i in narrow["items"]}
    assert shots == {_X: None, _Z: None}
    assert all("marketplace" not in i for i in narrow["items"])     # 键集不变
    # 美国批次的窄投影在加拿大截图落地前后不变
    before = await pgdb.get_results(batch_id=us, columns=["asin", "current_price"])
    assert await pgdb.update_screenshot_status(_X, ca, "done", "/p/ca.png") is True
    await pgdb._write_conn.execute(
        "UPDATE asin_data SET screenshot_path = NULL WHERE marketplace = 'amazon.com'")
    after = await pgdb.get_results(batch_id=us, columns=["asin", "current_price"])
    assert [(i["asin"], i["screenshot_path"]) for i in after["items"]] == \
        [(i["asin"], i["screenshot_path"]) for i in before["items"]]
    # 全局窄投影与默认投影对同一行给出同一张截图
    full = {i["id"]: i["screenshot_path"] for i in (await pgdb.get_results())["items"]}
    for item in (await pgdb.get_results(columns=["asin"]))["items"]:
        assert item["screenshot_path"] == full[item["id"]], item


async def test_screenshot_after_a_marketplace_reject_is_refused(pgdb):
    await _two_sites(pgdb)
    ca = await _batch(pgdb, "ca_ss", [_X], marketplace=_CA, zip_code=_CA_ZIP,
                      needs_screenshot=True)
    t = (await _pulled(pgdb, "w-old"))[_X]
    res = await _accept(pgdb, t, "w-old", "$99.99")              # 不带 marketplace
    assert res["server_reject"] is True
    assert await pgdb.update_screenshot_status(_X, ca, "done", "/p/old.png") is False
    rows = await _rows(pgdb, "SELECT marketplace, screenshot_path FROM asin_data "
                             "WHERE asin = ? ORDER BY marketplace", (_X,))
    assert rows == [{"marketplace": _CA, "screenshot_path": None},
                    {"marketplace": _US, "screenshot_path": None}]
    # 重试 -> 新 worker 交对站点 -> 截图照常落地
    await pgdb.retry_failed_tasks(ca)
    t = (await _pulled(pgdb, "w-new"))[_X]
    await _accept(pgdb, t, "w-new", "$13.60", marketplace=_CA)
    assert await pgdb.update_screenshot_status(_X, ca, "done", "/p/ca.png") is True
    assert (await pgdb.get_result_by_asin(_X, marketplace=_CA))["screenshot_path"] == "/p/ca.png"


async def test_asin_changes_can_be_scoped_to_a_site(pgdb):
    us, ca = await _two_sites(pgdb)
    all_changes = await pgdb.get_asin_changes(_X)
    assert {c["batch_id"] for c in all_changes} == {us, ca}       # 不限定：与改动前相同
    assert {c["batch_id"] for c in await pgdb.get_asin_changes(_X, marketplace=_US)} == {us}
    assert {c["batch_id"] for c in await pgdb.get_asin_changes(_X, marketplace="www.amazon.ca")} == {ca}
    with pytest.raises(ValueError):
        await pgdb.get_asin_changes(_X, marketplace="amazon.xx")
