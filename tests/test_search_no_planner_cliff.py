"""tests/test_search_no_planner_cliff.py —— 批量搜索不再退化成全表扫。

线上症状（2026-08）：搜索框写着「支持批量、逗号分隔」，粘 4 个以上 ASIN 进去
100% 卡满 60 秒返回 500；而 4 个高频词（chair/desk/lamp/sofa）只要 245ms。

根因不是词数本身，是 ``ORDER BY d.id DESC LIMIT 51`` 和扁平 OR 谓词的组合：
OR 分支越多，规划器估计的命中行数越大，越过某个点它就丢掉三个 trgm 索引、
改走"主键倒序扫 + 逐行过滤，凑够 51 行就停"。命中多时这个计划确实快，
命中少时要扫穿整张表 —— 于是"搜不到东西"比"搜得到"贵几千倍。

修法是每个 (词 × 列) 一个分支、各自 ORDER BY + LIMIT，再 UNION 进
MATERIALIZED CTE：每个分支只有一个 LIKE 谓词，估算不再被 OR 数量推高。

这里守的是**结构**（SQL 形状 + 行集正确性），不是墙上时间 —— 计时断言在
CI/沙箱上必然不稳。真实收益记在 results_read.py 的注释里（200k 行实测
5273ms -> 45ms）。
"""
import pytest

from common.pgdb.results_read import _SEARCH_COLUMNS, _TERM_OR, _col_like


def test_term_or_is_built_from_col_like():
    """``_TERM_OR`` 与 CTE 分支必须同源 —— 改一处漏一处 = 表达式 GIN 索引静默失效。"""
    assert _TERM_OR == "(" + " OR ".join(_col_like(c) for c in _SEARCH_COLUMNS) + ")"
    for col in _SEARCH_COLUMNS:
        # 表达式必须与 schema.py 建的 ascii_lower(col) gin_trgm_ops 逐字对得上
        assert f"ascii_lower(d.{col}) LIKE ascii_lower(?)" in _col_like(col)
        # D-16：每一处 LIKE 都要关掉 PG 的转义机制，才和 SQLite 语义一致
        assert _col_like(col).endswith("ESCAPE ''")


def test_search_columns_match_the_trgm_indexes():
    """搜索的列集必须与 schema.py 建的三个 trgm 索引一一对应。"""
    from common.pgdb.schema import DDL_TRGM_INDEXES
    idx = " ".join(DDL_TRGM_INDEXES)
    for col in _SEARCH_COLUMNS:
        assert f"ascii_lower({col})" in idx, f"{col} 没有对应的 trgm 索引"
    assert len(DDL_TRGM_INDEXES) == len(_SEARCH_COLUMNS)


@pytest.mark.asyncio
# ⚠ 这些 search 串里**每个词都 >= 3 字符**，而且长得像真的 ASIN。
#   写成 "aa,bb,cc" 那种 2 字符词是**不真实的夹具**：短词根本不走分支路径
#   （trgm 索引服务不了 1~2 字符的模式，见 test_short_terms_stay_on_the_flat_or_path），
#   拿它测分支形状等于什么都没测。用户真正粘进搜索框的是 10 位 ASIN。
@pytest.mark.parametrize("kw", [
    dict(search="B0AAA11111,B0BBB22222,B0CCC33333,B0DDD44444,B0EEE55555,"
                "B0FFF66666,B0GGG77777,B0HHH88888,B0III99999,B0JJJ00000"),  # 10 个零命中 ASIN
    dict(search="B0AAA11111,B0BBB22222,B0CCC33333,B0DDD44444", batch_id=1),  # + 批次筛选
    dict(search="B0AAA11111,B0BBB22222,B0CCC33333,B0DDD44444", cursor_id=3),  # + 翻页
    dict(search="Golden,B0BBB22222,B0CCC33333,B0DDD44444"),   # 混合：一个高命中 + 三个零命中
])
async def test_search_sql_uses_per_branch_limits(monkeypatch, kw):
    """多词搜索必须走 MATERIALIZED CTE，且**每个分支自带 ORDER BY + LIMIT**。

    分支里少了 LIMIT，CTE 就退化成"物化全部命中行"——高命中词上实测从 45ms
    劣化到 3179ms（拿灾难换平庸）。少了 MATERIALIZED，PG 会把只引用一次的
    CTE 内联回外层，外层的 LIMIT 又能影响分支扫描方式，等于没改。
    """
    # 不连库：把 read() 换成记录器，只看 get_results 拼出来的 SQL 形状。
    # 行集正确性由 tests/pgdb/test_results_read.py 的 97 条 PG↔SQLite 逐字比对守。
    import contextlib

    from common.pgdb import Database as PgDatabase

    seen = {}

    class _FakeCursor:
        def __init__(self): self.rowcount = -1
        async def fetchall(self): return []
        async def fetchone(self): return [0]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeConn:
        def execute(self, sql, params=None):
            seen.setdefault("sqls", []).append((sql, list(params or [])))
            return _FakeCursor()

    @contextlib.asynccontextmanager
    async def fake_read(self):
        yield _FakeConn()

    monkeypatch.setattr(PgDatabase, "read", fake_read, raising=False)
    db = PgDatabase.__new__(PgDatabase)

    async def _noop(*a, **k): return None
    monkeypatch.setattr(PgDatabase, "_hydrate_screenshot_paths", _noop, raising=False)
    monkeypatch.setattr(PgDatabase, "_hydrate_batch_task_status", _noop, raising=False)

    await PgDatabase.get_results(db, limit=50, with_total=False, **kw)

    data_sql = seen["sqls"][0][0]
    assert "WITH search_hit AS MATERIALIZED" in data_sql, data_sql
    n_terms = len([t for t in kw["search"].split(",") if t.strip()])
    # 每个 (词 × 列) 一个分支，每个分支自带 ORDER BY ... LIMIT
    assert data_sql.count("ORDER BY d.id") == n_terms * len(_SEARCH_COLUMNS) + 1
    assert data_sql.count("LIMIT ?") == n_terms * len(_SEARCH_COLUMNS) + 1

    if kw.get("batch_id"):
        # 筛选必须推进**每一个**分支：只留在外层会静默丢行（实测丢 51 行）
        assert data_sql.count("JOIN batch_asins") >= n_terms * len(_SEARCH_COLUMNS)
    if kw.get("cursor_id"):
        assert data_sql.count("d.id < ?") == n_terms * len(_SEARCH_COLUMNS) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("search,why", [
    ("qz",                              "单个短词"),
    ("qz,wx,vy,ju",                     "多个短词"),
    ("qz,wx,vy,ju,km,pn,rb,tf,gh,dl",   "10 个短词"),
    ("chair,qz",                        "混合：一长一短"),
    ("chair,desk,lamp,qz",              "混合：三长一短"),
])
async def test_short_terms_stay_on_the_flat_or_path(monkeypatch, search, why):
    """⚠ 只要有一个词短于 3 字符，就**必须**退回扁平 OR，不许拆分支。

    pg_trgm 索引服务不了 1~2 字符的模式（trigram 的最小单位就是 3 字符）。
    对这种词，"每个 (词 × 列) 一个分支"没有任何索引可用 —— 等于把原来的
    **一次**全表扫变成 **3N 次**全表扫。

    这不是理论推演，是本轮真踩的回归：200k 行上 10 个零命中短词
    12171ms（拆分支）vs 6028ms（扁平 OR，== 改动前）。放到百万行就是 60s
    超时 —— 洞从"粘 ASIN"挪到了"手输短词"，而且没人会想到去测它。

    混合输入也走扁平 OR：OR 里只要有一个分支用不上索引，整条查询就必须
    全表扫，拆分支只会把这一次扫描乘以 3N。
    """
    import contextlib
    from common.pgdb import Database as PgDatabase
    seen = {}

    class _FakeCursor:
        def __init__(self): self.rowcount = -1
        async def fetchall(self): return []
        async def fetchone(self): return [0]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeConn:
        def execute(self, sql, params=None):
            seen.setdefault("sqls", []).append((sql, list(params or [])))
            return _FakeCursor()

    @contextlib.asynccontextmanager
    async def fake_read(self):
        yield _FakeConn()

    monkeypatch.setattr(PgDatabase, "read", fake_read, raising=False)
    async def _noop(*a, **k): return None
    monkeypatch.setattr(PgDatabase, "_hydrate_screenshot_paths", _noop, raising=False)
    monkeypatch.setattr(PgDatabase, "_hydrate_batch_task_status", _noop, raising=False)

    db = PgDatabase.__new__(PgDatabase)
    await PgDatabase.get_results(db, limit=50, with_total=False, search=search)

    data_sql = seen["sqls"][0][0]
    assert "search_hit" not in data_sql, f"{why}：不该走分支 CTE\n{data_sql}"
    assert "MATERIALIZED" not in data_sql, why
    # 谓词仍然在，只是回到扁平 OR：一次扫描
    n_terms = len([t for t in search.split(",") if t.strip()])
    assert data_sql.count("ascii_lower(d.asin) LIKE") == n_terms, data_sql
    assert data_sql.count("ORDER BY") == 1, f"{why}：应当只有一次排序/一次扫描"


@pytest.mark.asyncio
async def test_long_terms_still_use_branches(monkeypatch):
    """反面对照：全部 >= 3 字符时必须还是分支形状（否则第一条修复就白做了）。"""
    import contextlib
    from common.pgdb import Database as PgDatabase
    seen = {}

    class _FakeCursor:
        def __init__(self): self.rowcount = -1
        async def fetchall(self): return []
        async def fetchone(self): return [0]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeConn:
        def execute(self, sql, params=None):
            seen.setdefault("sqls", []).append((sql, list(params or [])))
            return _FakeCursor()

    @contextlib.asynccontextmanager
    async def fake_read(self):
        yield _FakeConn()

    monkeypatch.setattr(PgDatabase, "read", fake_read, raising=False)
    async def _noop(*a, **k): return None
    monkeypatch.setattr(PgDatabase, "_hydrate_screenshot_paths", _noop, raising=False)
    monkeypatch.setattr(PgDatabase, "_hydrate_batch_task_status", _noop, raising=False)

    db = PgDatabase.__new__(PgDatabase)
    await PgDatabase.get_results(db, limit=50, with_total=False, search="chair,desk,lamp,sofa")
    assert "WITH search_hit AS MATERIALIZED" in seen["sqls"][0][0]
