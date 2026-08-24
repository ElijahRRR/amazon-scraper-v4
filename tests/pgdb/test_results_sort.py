"""tests/pgdb/test_results_sort.py —— `sort=recent`（最近采的排前面）。

`asin_data` 是**一 ASIN 一行**、按 asin UPSERT 的，`id` 在首次入库时分配之后
永不改变。所以默认的 `ORDER BY d.id DESC` 是"第一次见到这个 ASIN"的倒序，
**不是**"最近采集"——一个两年前入库、今天刚重采完的 ASIN 仍然沉在最底下。
`sort=recent` 改按 `updated_at DESC, id DESC`。

⚠ 共享夹具（tests/pgdb/test_results_read.py 的 ASIN_ROWS）里 `updated_at`
恰好与 `id` **同序**，两种排序在它上面给出完全一样的结果 —— 直接拿它测
`sort=recent` 是一个永远绿的空转测试。所以这里先把 `updated_at` 打乱，
再比对；``test_recent_actually_differs_from_id`` 专门守着"两种排序确实不同"
这一点，防止有人把排序改回去而测试仍然绿。
"""
import pytest

from common.core.results_sort import (
    DEFAULT_SORT,
    SORT_MODES,
    CursorExpired,
    is_next,
    keyset_predicate,
    normalize_sort,
    order_by,
)
from tests.pgdb.test_results_read import seeded_pg, seeded_sqlite  # noqa: F401

# 打乱后的 updated_at：故意让它与 id 顺序**不一致**，并留一个 NULL。
#   asin        -> updated_at
SHUFFLE = {
    "B0GOLDEN01": "2026-06-01 00:00:00",   # 最早入库，但最近才采 -> 应排最前
    "B0GOLDEN02": "2026-01-01 00:00:00",
    "B0GOLDEN03": "2026-03-01 00:00:00",
    "b0lower04":  "2026-05-01 00:00:00",
    "B0BSLASH05": "2026-02-01 00:00:00",
    "B0ACCENT06": "2026-04-01 00:00:00",
    "B0ACCENT07": None,                    # NULL -> NULLS LAST，两个引擎都排最后
}


async def _shuffle(db):
    for asin, ts in SHUFFLE.items():
        await db._db.execute("UPDATE asin_data SET updated_at = ? WHERE asin = ?", (ts, asin))
    # PG 侧 _db 是专用写连接，SQLite 侧同理；两边都是自动提交（没开事务）


async def _ids(db, **kw):
    kw.setdefault("with_total", False)
    kw.setdefault("limit", 50)
    r = await db.get_results(**kw)
    return [i["id"] for i in r["items"]]


# ==================== 纯函数：规则本身 ====================

def test_order_by_and_keyset_text():
    """两个后端逐字共用这两串文本，所以它们本身就是契约。"""
    assert order_by("id", "next") == "d.id DESC"
    assert order_by("id", "prev") == "d.id ASC"
    # ⚠ 排序键必须是 COALESCE 包过的表达式，不能是裸列。三条理由写在
    #   common/core/results_sort.py 的模块头，最要命的一条：行值比较遇 NULL
    #   恒为 NULL，updated_at 为空的行会在第一页之后**静默消失**。
    assert order_by("recent", "next") == "COALESCE(d.updated_at, '') DESC, d.id DESC"
    assert order_by("recent", "prev") == "COALESCE(d.updated_at, '') ASC, d.id ASC"
    assert "NULLS" not in order_by("recent", "next"), \
        "改用 COALESCE 之后不再需要 NULLS 修饰符；写回去会和索引表达式对不上"

    assert keyset_predicate("id", "next").count("?") == 1
    assert keyset_predicate("recent", "next").count("?") == 2
    assert keyset_predicate("recent", "next") == "(COALESCE(d.updated_at, ''), d.id) < (?, ?)"
    assert keyset_predicate("recent", "prev") == "(COALESCE(d.updated_at, ''), d.id) > (?, ?)"


def test_index_expression_matches_the_query_expression():
    """表达式索引要被用上，**索引里的表达式必须与查询里的逐字一致**。

    写岔一个字的后果不是报错，是索引静默失效 —— 查询结果照样对，只是从
    索引扫描退化成全表排序。所以这里把两个后端的 CREATE INDEX 与
    order_by()/keyset_predicate() 用的表达式钉在一起。
    """
    from common.core.results_sort import sort_key
    from common.pgdb.schema import DDL_INDEXES
    import common.database as sqlite_mod
    import inspect

    expr = sort_key("")                      # COALESCE(updated_at, '')
    assert expr in order_by("recent", "next").replace("d.", "")

    pg = [i for i in DDL_INDEXES if "idx_asin_data_updated_id" in i]
    assert len(pg) == 1, pg
    assert f"{expr} DESC, id DESC" in pg[0], pg[0]

    # SQLite 侧那条 DDL 写在一大段 executescript 的字符串里，拿不到常量，
    # 只能按源码文本核对 —— 这正是"同一份表达式存在第二处副本"的代价。
    src = inspect.getsource(sqlite_mod)
    assert f"ON asin_data({expr} DESC, id DESC)" in src, \
        "SQLite 的 idx_asin_data_updated_id 表达式与 sort_key() 对不上"


def test_direction_only_prev_is_special():
    """既有行为：direction 只有 'prev' 特殊，其余（含非法值）都当 next。"""
    assert is_next("next") and is_next("") and is_next("bogus") and is_next(None)
    assert not is_next("prev")


def test_normalize_sort_never_leaks_arbitrary_text():
    """库层要保证**永远不会**把非法字符串拼进 SQL（HTTP 层另外负责 422）。"""
    for bad in ("bogus", "", None, "id; DROP TABLE asin_data", 1):
        assert normalize_sort(bad) == DEFAULT_SORT
    for good in SORT_MODES:
        assert normalize_sort(good) == good


# ==================== 行为：两个后端逐字一致 ====================

@pytest.mark.asyncio
async def test_recent_actually_differs_from_id(seeded_pg, seeded_sqlite):
    """守住这个测试自己的有效性：两种排序必须给出**不同**的顺序。

    如果哪天有人把 sort=recent 悄悄改回按 id 排，下面那些"两边一致"的断言
    照样全绿 —— 只有这一条会红。
    """
    for db in (seeded_pg, seeded_sqlite):
        await _shuffle(db)
        by_id = await _ids(db, sort="id")
        by_recent = await _ids(db, sort="recent")
        assert sorted(by_id) == sorted(by_recent), "两种排序的行集必须相同"
        assert by_id != by_recent, "打乱 updated_at 之后两种排序不该还一样"


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    dict(sort="recent"),
    dict(sort="recent", direction="prev"),
    dict(sort="recent", limit=3),
    dict(sort="recent", limit=3, cursor_id=5),
    dict(sort="recent", batch_id=1),
    dict(sort="recent", search="GoldenBrand"),
    dict(sort="recent", change_filter="price_stock"),
    dict(sort="recent", with_total=True),
])
async def test_recent_matches_between_backends(seeded_pg, seeded_sqlite, kw):
    await _shuffle(seeded_pg)
    await _shuffle(seeded_sqlite)
    pg = await seeded_pg.get_results(**{**dict(with_total=False, limit=50), **kw})
    sq = await seeded_sqlite.get_results(**{**dict(with_total=False, limit=50), **kw})
    assert [i["id"] for i in pg["items"]] == [i["id"] for i in sq["items"]]
    assert (pg["has_more"], pg["next_cursor"], pg["prev_cursor"], pg["total"]) == \
           (sq["has_more"], sq["next_cursor"], sq["prev_cursor"], sq["total"])


@pytest.mark.asyncio
async def test_null_updated_at_sorts_last_on_both_backends(seeded_pg, seeded_sqlite):
    """NULLS 位置的陷阱：不显式写修饰符，两个引擎会把 NULL 排到相反的两端。"""
    for db in (seeded_pg, seeded_sqlite):
        await _shuffle(db)
        rows = await db.get_results(sort="recent", limit=50, with_total=False)
        items = rows["items"]
        nulls = [i["asin"] for i in items if i["updated_at"] is None]
        assert nulls, "夹具里应当有一行 updated_at IS NULL"
        assert items[-1]["updated_at"] is None, "NULL 必须排在最后（NULLS LAST）"


@pytest.mark.asyncio
async def test_full_pagination_walk_is_complete_and_ordered(seeded_pg, seeded_sqlite):
    """小 limit 逐页翻到底：不丢行、不重复、整体有序。"""
    for db in (seeded_pg, seeded_sqlite):
        await _shuffle(db)
        seen, cur = [], None
        for _ in range(20):
            r = await db.get_results(sort="recent", limit=2, with_total=False, cursor_id=cur)
            seen += [i["id"] for i in r["items"]]
            if not r["has_more"]:
                break
            cur = r["next_cursor"]
        one_shot = await _ids(db, sort="recent", limit=50)
        assert seen == one_shot, f"逐页翻的结果必须与一次大页相同：{seen} != {one_shot}"
        assert len(seen) == len(set(seen)), "翻页出现了重复行"


@pytest.mark.asyncio
async def test_expired_cursor_raises_on_both_backends(seeded_pg, seeded_sqlite):
    """游标行被删 -> CursorExpired（HTTP 层转 422）。

    **不能**悄悄退回按 id 比较：那会在 ORDER BY updated_at 下给出一页语义错误
    的数据，而调用方看不出来。所以这里要的是抛异常，不是"尽力而为"。
    """
    for db in (seeded_pg, seeded_sqlite):
        await _shuffle(db)
        first = await db.get_results(sort="recent", limit=2, with_total=False)
        victim = first["next_cursor"]
        await db._db.execute("DELETE FROM asin_data WHERE id = ?", (victim,))
        with pytest.raises(CursorExpired):
            await db.get_results(sort="recent", limit=2, with_total=False, cursor_id=victim)
        # 对照：sort=id 不需要那一行的 updated_at，同一个游标仍然可用
        ok = await db.get_results(sort="id", limit=2, with_total=False, cursor_id=victim)
        assert isinstance(ok["items"], list)


@pytest.mark.asyncio
async def test_default_sort_is_unchanged(seeded_pg, seeded_sqlite):
    """不传 sort 必须与 sort='id' 逐字相同 —— 默认行为不许动。"""
    for db in (seeded_pg, seeded_sqlite):
        await _shuffle(db)
        assert await _ids(db) == await _ids(db, sort="id")
