"""tests/pgdb/test_results_read.py —— common/pgdb/results_read.py 的验收门。

做法：同一批夹具数据分别灌进一个临时 PG 库和一个临时 SQLite 库，然后把
7 个读方法在两侧各跑一遍逐字段 diff。**SQLite 侧是唯一裁判**——不写死期望值，
免得把"我以为的正确"写成契约。

media.py（_hydrate_screenshot_paths / _get_done_screenshot_paths）此刻可能还是
NotImplementedError，所以这里在**实例上**补一份逐字照抄 SQLite 版的实现。
等 media.py 落地后这两个补丁会被真实实现取代（instance attr 优先，去掉即可）。
"""
from __future__ import annotations

import os
import tempfile
import types

import pytest
import pytest_asyncio

from common.core import _normalize_screenshot_path
from common.database import Database as SqliteDatabase

pytest.importorskip("asyncpg", reason="tests/pgdb 需要 asyncpg")


# ==================================================================== 夹具数据
ASIN_COLS = ["asin", "title", "brand", "current_price", "stock_status",
             "screenshot_path", "site", "zip_code", "crawl_time",
             "created_at", "updated_at"]

ASIN_ROWS = [
    ("B0GOLDEN01", "GoldenBrand Widget", "GoldenBrand", "19.99", "In Stock",
     None, "US", "10001", "2026-01-01 00:00:01", "2026-01-01 00:00:01", "2026-01-01 10:00:00"),
    ("B0GOLDEN02", "goldenbrand lower widget", "goldenbrand", "29.99", "In Stock",
     "/shots/b2.png", "US", "10001", "2026-01-01 00:00:02", "2026-01-01 00:00:02", "2026-01-01 10:00:01"),
    ("B0GOLDEN03", "GOLDENBRAND UPPER", "GOLDENBRAND", "39.99", None,
     "none", "US", "90210", "2026-01-01 00:00:03", "2026-01-01 00:00:03", "2026-01-01 10:00:02"),
    ("b0lower04", "Wireless Thing", "Acme", None, "Out of Stock",
     "", "US", "60601", "2026-01-01 00:00:04", "2026-01-01 00:00:04", "2026-01-01 10:00:03"),
    ("B0BSLASH05", "back\\slash item", "Back\\Brand", "9.99", "In Stock",
     None, "US", "10001", "2026-01-01 00:00:05", "2026-01-01 00:00:05", "2026-01-01 10:00:04"),
    ("B0ACCENT06", "ÉCLAIR CAFÉ", "ÉclairBrand", "5.00", "In Stock",
     None, "US", "10001", "2026-01-01 00:00:06", "2026-01-01 00:00:06", "2026-01-01 10:00:05"),
    ("B0ACCENT07", "éclair café", "éclairbrand", "6.00", "In Stock",
     None, "US", "10001", "2026-01-01 00:00:07", "2026-01-01 00:00:07", "2026-01-01 10:00:06"),
    ("B0PCT08", "Gol%rand pct", "Pct_Brand", "7.00", "In Stock",
     None, "US", "10001", "2026-01-01 00:00:08", "2026-01-01 00:00:08", "2026-01-01 10:00:07"),
]

BATCH_ROWS = [
    ("golden_batch_a", 0, 0, "running", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
    ("golden_batch_b", 1, 0, "running", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
]

BATCH_ASINS = [
    (1, "B0GOLDEN01", 1),
    (1, "B0GOLDEN02", 0),
    (1, "B0GOLDEN03", 1),
    (1, "b0lower04", 0),
    (1, "B0MISSING99", 0),        # batch_asins 有、asin_data 没有 -> LEFT JOIN 的 NULL 侧
    (2, "B0BSLASH05", 1),
]

TASK_ROWS = [
    (1, "B0GOLDEN01", "done", 0, 0, "w1", 0, 0, None, None, "2026-01-01 00:00:00", "2026-01-01 00:01:00"),
    (1, "B0GOLDEN02", "failed", 0, 0, "w1", 2, 1, "parse_error", "boom", "2026-01-01 00:00:00", "2026-01-01 00:02:00"),
    (1, "B0GOLDEN03", "pending", 0, 0, None, 0, 0, None, None, "2026-01-01 00:00:00", None),
    (1, "b0lower04", "processing", 0, 0, "w2", 0, 0, None, None, "2026-01-01 00:00:00", "2026-01-01 00:03:00"),
]

CHANGE_ROWS = [
    ("B0GOLDEN01", 1, "new", "first_seen", None, None, "2026-01-01 00:10:00"),
    ("B0GOLDEN01", 1, "price_stock", "price", "18.99", "19.99", "2026-01-01 00:11:00"),
    ("B0GOLDEN02", 1, "title_bullets", "title", "old", "new", "2026-01-01 00:12:00"),
    ("B0GOLDEN02", 1, "price_stock", "stock", "0", "5", "2026-01-01 00:13:00"),
    ("B0GOLDEN03", 2, "price_stock", "price", "1", "2", "2026-01-01 00:14:00"),
    ("b0lower04", None, "new", "first_seen", None, None, "2026-01-01 00:15:00"),
    # ⚠ 这一行是**判别行**，不是凑数的。B0MISSING99 在 batch_asins 里有、在
    # asin_data 里没有（见 BATCH_ASIN_ROWS 的 (1,"B0MISSING99",0)），所以它是
    # 批次导出那条 `batch_asins ba LEFT JOIN asin_data d` 的 NULL 侧。
    #
    # 批次导出的 change_filter 谓词写 `ac.asin = ba.asin`（正确）还是
    # `ac.asin = d.asin`（错误）**只在这种行上分岔** —— d.asin 为 NULL 时
    # 后者恒不成立，这一行会被静默丢掉。审计实测：
    #     ba.asin 写法 -> ['B0GOLDEN01','B0GOLDEN02','B0MISSING99']
    #     d.asin  写法 -> ['B0GOLDEN01','B0GOLDEN02']            少一行
    #
    # 补这一行之前，把 ba.asin 改成 d.asin 跑完整 tests/pgdb 是 **415 passed**
    # —— 那处改写完全没有回归网，只由评审保着。
    ("B0MISSING99", 1, "price_stock", "price", "1", "2", "2026-01-01 00:16:00"),
]

SHOT_ROWS = [
    ("B0GOLDEN01", 1, "done", 0, None, "/shots/a1_old.png", "2026-01-01 00:00:00", "2026-01-01 01:00:00"),
    ("B0GOLDEN01", 2, "done", 0, None, "/shots/a1_new.png", "2026-01-01 00:00:00", "2026-01-01 02:00:00"),
    ("B0GOLDEN03", 1, "done", 0, None, "/shots/a3.png", "2026-01-01 00:00:00", "2026-01-01 01:00:00"),
    ("b0lower04", 1, "failed", 1, "nope", None, "2026-01-01 00:00:00", "2026-01-01 01:00:00"),
]


async def _seed(conn):
    """conn 是 aiosqlite 连接或 pgdb 的 ConnProxy —— 两边都吃 ``?`` 占位符。"""
    ph = ",".join("?" * len(ASIN_COLS))
    for r in ASIN_ROWS:
        await conn.execute(f"INSERT INTO asin_data ({','.join(ASIN_COLS)}) VALUES ({ph})", r)
    for r in BATCH_ROWS:
        await conn.execute(
            "INSERT INTO batches (name, needs_screenshot, is_auto, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)", r)
    for r in BATCH_ASINS:
        await conn.execute("INSERT INTO batch_asins (batch_id, asin, is_new) VALUES (?,?,?)", r)
    for r in TASK_ROWS:
        await conn.execute(
            "INSERT INTO tasks (batch_id, asin, status, priority, needs_screenshot, worker_id,"
            " retry_count, auto_retry_count, error_type, error_detail, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)
    for r in CHANGE_ROWS:
        await conn.execute(
            "INSERT INTO asin_changes (asin, batch_id, change_type, change_detail,"
            " prev_value, new_value, created_at) VALUES (?,?,?,?,?,?,?)", r)
    for r in SHOT_ROWS:
        await conn.execute(
            "INSERT INTO screenshots (asin, batch_id, status, retry_count, error_detail,"
            " file_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)", r)


# ------------------------ media.py 的两个欠账方法（实例级补丁，逐字照抄 SQLite 版）
async def _get_done_screenshot_paths(self, asins, batch_id=None):
    if not asins:
        return {}
    remaining = list(dict.fromkeys(asins))
    mapping = {}

    async def load(batch_filter=None):
        nonlocal remaining
        if not remaining:
            return
        placeholders = ",".join("?" * len(remaining))
        sql = ("SELECT asin, file_path FROM screenshots "
               f"WHERE status = 'done' AND asin IN ({placeholders})")
        params = list(remaining)
        if batch_filter is not None:
            sql += " AND batch_id = ?"
            params.append(batch_filter)
        sql += " ORDER BY updated_at DESC, id DESC"
        async with self.read() as rc, rc.execute(sql, params) as c:
            rows = await c.fetchall()
        for row in rows:
            fp = _normalize_screenshot_path(row["file_path"])
            if fp and row["asin"] not in mapping:
                mapping[row["asin"]] = fp
        remaining = [a for a in remaining if a not in mapping]

    if batch_id is not None:
        await load(batch_id)
    await load()
    return mapping


async def _hydrate_screenshot_paths(self, items, batch_id=None):
    missing = []
    for item in items:
        normalized = _normalize_screenshot_path(item.get("screenshot_path"))
        item["screenshot_path"] = normalized
        if normalized is None:
            missing.append(item["asin"])
    fallback = await self._get_done_screenshot_paths(missing, batch_id) if missing else {}
    for item in items:
        if item["screenshot_path"] is None:
            item["screenshot_path"] = fallback.get(item["asin"])


def _patch_media(db):
    db._get_done_screenshot_paths = types.MethodType(_get_done_screenshot_paths, db)
    db._hydrate_screenshot_paths = types.MethodType(_hydrate_screenshot_paths, db)
    return db


# ==================================================================== 夹具
@pytest_asyncio.fixture
async def seeded_pg(pgdb):
    _patch_media(pgdb)
    await _seed(pgdb._db)
    return pgdb


@pytest_asyncio.fixture
async def seeded_sqlite(tmp_path):
    db = SqliteDatabase(os.path.join(str(tmp_path), "cmp.db"))
    await db.connect()
    await _seed(db._db)
    try:
        yield db
    finally:
        await db.close()


# ==================================================================== 工具
async def _call(db, name, *args, **kw):
    """('ok', 返回值) 或 ('raise',) —— 崩溃行为本身也是契约（决策 D-8）。

    异常**类型**不比：sqlite3.ProgrammingError 与 asyncpg.InterfaceError 在
    HTTP 层都是同一个 500。比的是"崩不崩"。
    """
    try:
        return ("ok", await getattr(db, name)(*args, **kw))
    except Exception:                                          # noqa: BLE001
        return ("raise",)


async def _iter(db, **kw):
    try:
        return ("ok", [row async for row in db.iter_results(**kw)])
    except Exception:                                          # noqa: BLE001
        return ("raise",)


# ==================================================================== 用例
@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("get_total_asins", ()),
    ("get_all_asins", ()),
    ("get_change_stats", ()),
    ("get_change_stats", (1,)),
    ("get_change_stats", (2,)),
    ("get_change_stats", (999,)),
    ("get_asin_changes", ("B0GOLDEN01",)),
    ("get_asin_changes", ("nope",)),
    ("get_result_by_asin", ("B0GOLDEN01",)),   # 自身 NULL -> screenshots 回填
    ("get_result_by_asin", ("B0GOLDEN02",)),   # 自带路径
    ("get_result_by_asin", ("B0GOLDEN03",)),   # 'none' 字面量 -> 归一成 None
    ("get_result_by_asin", ("b0lower04",)),    # '' -> 归一；且无 done 截图
    ("get_result_by_asin", ("MISSING",)),
])
async def test_simple_reads_match_sqlite(seeded_pg, seeded_sqlite, name, args):
    assert await _call(seeded_pg, name, *args) == await _call(seeded_sqlite, name, *args)


GET_RESULTS_CASES = [
    dict(batch_id=b, change_filter=cf)
    for cf in ("all", "price_stock", "title_bullets", "new")
    for b in (None, 1, 2)
] + [
    dict(limit=3),
    dict(limit=3, cursor_id=6),
    dict(limit=3, cursor_id=3, direction="prev"),
    dict(direction="prev"),
    dict(direction="prev", limit=2),
    dict(limit=100),
    dict(limit=0),
    # 限长：>10 个词只取前 10；>500 字符先整体截断；单词 >100 字符再截
    dict(search=",".join(f"t{i}" for i in range(15))),
    dict(search="GoldenBrand," + ",".join(f"zz{i}" for i in range(20))),
    dict(search="x" * 600),
    dict(search="GoldenBrand" + "z" * 200),
    dict(search="a" * 120 + ",Wireless"),
    # ASCII 大小写折叠（SQLite 的 LIKE 和 FTS5 trigram 都折 ASCII）
    dict(search="GoldenBrand"), dict(search="goldenbrand"),
    dict(search="GOLDENBRAND"), dict(search="GoLdEnBrAnD"),
    # < 3 字符走"慢路径"分支
    dict(search="Go"), dict(search="b0"), dict(search="B0"),
    # 非 ASCII：SQLite 大小写**敏感**，ILIKE 会多匹配 —— 这三条就是 D-5 的判据
    dict(search="ÉCLAIR"), dict(search="éclair"), dict(search="Éclair"),
    # 反斜杠：SQLite LIKE 无转义字符，PG 默认反斜杠转义
    dict(search="back\\slash"), dict(search="\\"), dict(search="\\\\"),
    # % / _ 是活的通配符，两个引擎一致，这个"bug"要免费保留
    dict(search="Gol%rand"), dict(search="Gol_en"), dict(search="%"),
    dict(search="___"), dict(search="%%%"),
    # 空词条 -> 完全不加 where 子句
    dict(search="   "), dict(search=",,,"), dict(search=""),
    dict(search="Wireless,GoldenBrand"), dict(search="Go,%%%"),
    dict(search="zzz-no-match"),
    dict(search="GoldenBrand", batch_id=1),
    dict(search="GoldenBrand", change_filter="price_stock"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", GET_RESULTS_CASES,
                         ids=lambda k: ",".join(f"{a}={b}" for a, b in k.items()) or "default")
async def test_get_results_matches_sqlite(seeded_pg, seeded_sqlite, kw):
    assert await _call(seeded_pg, "get_results", **kw) == \
           await _call(seeded_sqlite, "get_results", **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    dict(search="GoldenBrand", cursor_id=5),
    dict(search="GoldenBrand", cursor_id=5, batch_id=1),
    dict(search="Wireless,GoldenBrand", cursor_id=5),
])
async def test_search_with_cursor_no_longer_500s(seeded_pg, seeded_sqlite, kw):
    """决策 D-8 **已修**：``search`` + ``cursor`` 不再 500，两个后端一起。

    ⚠ 这个测试原名 ``test_count_bug_is_reproduced``，断言的是"两边都必须
    抛异常"——那时崩溃本身是被刻意复现的既有行为（详见 results_read.py
    模块头第二节）。D-8 自己写着"留给 Phase 1.5 修"，本轮修了：

      根因不是搜索谓词，是 count 查询靠 ``"d.id" not in p`` **猜**哪个谓词
      是 keyset 游标。搜索快路径的谓词文本里也含 "d.id"，于是被一并剔掉，
      而 count_params 里仍留着它的 3N 个参数 -> 参数个数对不上 -> 500。
      现在 count 的谓词与参数在同一时刻快照（keyset 追加之前），从结构上
      不可能对不齐，也不再依赖任何文本匹配。

    保留三个原参数组合：它们正是当年触发崩溃的那三条路径。
    """
    sq = await _call(seeded_sqlite, "get_results", **kw)
    assert sq[0] == "ok", sq
    assert await _call(seeded_pg, "get_results", **kw) == sq


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    dict(search="Go", cursor_id=5),
    dict(search="b0", cursor_id=5),
])
async def test_short_term_search_with_cursor_still_works(seeded_pg, seeded_sqlite, kw):
    """反面：< 3 字符的谓词文本里没有 "d.id"，不会被剔，今天是 200。"""
    sq = await _call(seeded_sqlite, "get_results", **kw)
    assert sq[0] == "ok"
    assert await _call(seeded_pg, "get_results", **kw) == sq


ITER_CASES = [
    dict(batch_id=b, change_filter=cf)
    for cf in ("all", "price_stock", "title_bullets", "new")
    for b in (None, 1, 2, 999)
] + [
    dict(batch_size=1), dict(batch_size=2), dict(batch_id=1, batch_size=1),
    dict(columns=["asin", "title", "current_price"]),
    dict(batch_id=1, columns=["asin", "title", "current_price"]),
    dict(batch_id=1, columns=["title"]),     # 投影里没有 asin：ba.asin 覆盖
    dict(columns=["title"]),                 # 非批次路径要自己补 d.id 做游标
    dict(columns=["nosuchcol"]),             # 与已知列集交集为空 -> 回退 d.*
    dict(batch_id=1, columns=["nosuchcol"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", ITER_CASES,
                         ids=lambda k: ",".join(f"{a}={b}" for a, b in k.items()) or "default")
async def test_iter_results_matches_sqlite(seeded_pg, seeded_sqlite, kw):
    assert await _iter(seeded_pg, **kw) == await _iter(seeded_sqlite, **kw)


@pytest.mark.asyncio
async def test_iter_results_is_async_generator(seeded_pg):
    """调用方是 ``async for``：必须是 async generator，不是 coroutine。"""
    import inspect
    assert inspect.isasyncgenfunction(type(seeded_pg).iter_results)
    gen = seeded_pg.iter_results()
    assert inspect.isasyncgen(gen)
    await gen.aclose()


@pytest.mark.asyncio
async def test_wire_types_are_sqlite_shaped(seeded_pg):
    """黄金夹具抓不到的一类：类型。时间戳是 str、布尔是 int、null 不是 ''。"""
    page = await seeded_pg.get_results(limit=1)
    item = page["items"][0]
    assert type(item["created_at"]) is str
    assert type(item["updated_at"]) is str
    assert type(item["id"]) is int
    assert type(page["total"]) is int
    assert type(page["has_more"]) is bool

    rows = [r async for r in seeded_pg.iter_results(batch_id=1)]
    assert type(rows[0]["batch_is_new"]) is int          # 不是 True/False
    assert type(rows[0]["batch_has_asin_data"]) is int

    # screenshot_path 的 null 必须是 None，不能被"防御性" COALESCE 变成 ''
    assert (await seeded_pg.get_result_by_asin("b0lower04"))["screenshot_path"] is None


@pytest.mark.asyncio
async def test_batch_path_adds_eleven_alias_keys(seeded_pg):
    rows = [r async for r in seeded_pg.iter_results(batch_id=1)]
    expected = {
        "batch_requested_asin", "batch_is_new", "batch_task_status",
        "batch_error_type", "batch_error_detail", "batch_retry_count",
        "batch_auto_retry_count", "batch_worker_id", "batch_task_updated_at",
        "batch_asin_data_updated_at", "batch_has_asin_data",
    }
    assert expected <= set(rows[0])
    # batch_asins 有、asin_data 没有的行：d.* 全 NULL，但 asin 被 ba.asin 覆盖
    orphan = [r for r in rows if r["asin"] == "B0MISSING99"]
    assert len(orphan) == 1
    assert orphan[0]["batch_has_asin_data"] == 0
    assert orphan[0]["title"] is None


@pytest.mark.asyncio
async def test_search_uses_the_trgm_expression_index(seeded_pg):
    """表达式 GIN 索引必须和查询里的表达式对得上，否则搜索会退化成顺序扫描。

    夹具只有 8 行，规划器当然选顺序扫描；关掉 seqscan 才看得出索引可用。
    """
    conn = seeded_pg._write_conn
    await conn.execute("SET enable_seqscan = off")
    try:
        plan = "\n".join(r[0] for r in await conn.fetch(
            "EXPLAIN SELECT d.id FROM asin_data d "
            "WHERE ascii_lower(d.title) LIKE ascii_lower($1)", "%goldenbrand%"))
    finally:
        await conn.execute("SET enable_seqscan = on")
    assert "idx_asin_data_title_trgm" in plan, plan
