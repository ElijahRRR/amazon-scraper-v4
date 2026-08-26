"""tests/pgdb/test_total_estimate.py —— 「共 N 条结果」的估算路径。

被测的是 ``get_results(with_total=True)`` 里那个分支：**无筛选**的大库上
``total`` 取自 ``pg_class.reltuples``（一次系统表单行查找）而不是
``SELECT COUNT(*)``（124 万行、可见性图被写脏时实测 683.6 ms）。

这里的核心手法：**先 ANALYZE，再插入更多行，且不再 ANALYZE**。
于是 reltuples 停在旧值、精确 COUNT 是新值，两个数**必然不同** ——
所以每条用例都能分辨"真的读了统计值"和"其实还是数了一遍"，
而不是在两个恰好相等的数上打勾。
"""
from __future__ import annotations

import pytest

pytest.importorskip("asyncpg", reason="tests/pgdb 需要 asyncpg")

import common.pgdb.results_read as rr


async def _seed(pgconn, n, start=0):
    """插 n 行 asin_data（直接走裸连接，绕开写锁与事件流，这里只要行）。"""
    await pgconn.executemany(
        "INSERT INTO asin_data (asin, title, brand, updated_at) VALUES ($1,$2,$3,$4)",
        [(f"B{start + i:09d}", f"Widget {start + i}", "AcmeBrand",
          "2026-01-01 10:00:00") for i in range(n)])


async def _skew(pgconn, analyzed, extra):
    """造出 reltuples=analyzed 而真实行数=analyzed+extra 的**已知偏差**。"""
    await _seed(pgconn, analyzed)
    await pgconn.execute("ANALYZE asin_data")
    await _seed(pgconn, extra, start=analyzed)
    # 故意**不**再 ANALYZE。autovacuum 在临时库上这点行数不会触发，
    # 但即便触发了，下面的断言也只依赖"估算 != 精确"这一个前提，
    # 真被 analyze 了会让用例失败而不是静默通过。
    return analyzed, analyzed + extra


@pytest.mark.asyncio
async def test_unfiltered_total_comes_from_reltuples_not_a_count(pgdb, pgconn, monkeypatch):
    """无筛选 -> total 等于统计值（不等于真实行数），且自报 estimate。"""
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    est, real = await _skew(pgconn, analyzed=40, extra=7)
    assert est != real  # 前提本身要成立，否则下面的断言分辨不出任何东西

    res = await pgdb.get_results(limit=5)
    assert res["total"] == est, "读的不是 reltuples"
    assert res["total_is_estimate"] is True


@pytest.mark.asyncio
async def test_exact_total_forces_the_real_count(pgdb, pgconn, monkeypatch):
    """exact_total=True 是估算的逃生门：数字回到精确值，标记回到 False。"""
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    est, real = await _skew(pgconn, analyzed=40, extra=7)

    res = await pgdb.get_results(limit=5, exact_total=True)
    assert res["total"] == real
    assert res["total_is_estimate"] is False


@pytest.mark.asyncio
async def test_small_table_stays_exact(pgdb, pgconn, monkeypatch):
    """行数低于门槛 -> 精确。

    这条守的是 golden 基线：几十行的夹具上两个后端必须给同一个 total。
    门槛用真实值 100_000，所以这里就是**默认配置下的小库**。
    """
    est, real = await _skew(pgconn, analyzed=40, extra=7)

    res = await pgdb.get_results(limit=5)
    assert res["total"] == real
    assert res["total_is_estimate"] is False


@pytest.mark.asyncio
async def test_never_analyzed_table_stays_exact(pgdb, pgconn, monkeypatch):
    """从没 ANALYZE 过 -> reltuples 是 -1（PG 14+ 的"未知"哨兵）-> 必须退回精确。

    症状是页面显示"共 -1 条"。挡住它的是**门槛那一条比较**（-1 自然低于门槛），
    不是一条单独的 ``est < 0`` —— 这条用例守的就是"把门槛调到 10 也仍然挡得住"，
    也就是门槛不能被写成 ``abs(est) < ...`` 或者只在 est 为正时才比较。
    """
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    await _seed(pgconn, 20)  # 建表后一次 ANALYZE 都没跑过
    reltuples = await pgconn.fetchval(
        "SELECT reltuples FROM pg_class WHERE oid = to_regclass('asin_data')")
    assert reltuples < 0, f"前提不成立：reltuples={reltuples}，这条用例失去意义"

    res = await pgdb.get_results(limit=5)
    assert res["total"] == 20
    assert res["total_is_estimate"] is False


# ------------------------------------------------------------------ 筛选闸门
#
# 估算只对"无筛选"成立 —— 全表的统计值回答不了带谓词的问题。
# 这组用例每一条都在偏差存在的前提下跑，所以"错误地用了估算"会显示成
# total == est 而不是 total == 真实命中数，一眼可辨。

@pytest.mark.asyncio
async def test_search_filter_stays_exact(pgdb, pgconn, monkeypatch):
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    est, real = await _skew(pgconn, analyzed=40, extra=7)

    res = await pgdb.get_results(limit=5, search="Widget 3")
    assert res["total_is_estimate"] is False
    assert res["total"] != est
    assert res["total"] == len([i for i in range(real) if "Widget 3" in f"Widget {i}"])


@pytest.mark.asyncio
async def test_change_filter_stays_exact(pgdb, pgconn, monkeypatch):
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    est, real = await _skew(pgconn, analyzed=40, extra=7)
    await pgconn.execute(
        "INSERT INTO asin_changes (asin, change_type, created_at) "
        "VALUES ('B000000001', 'price_stock', '2026-01-01 10:00:00')")

    res = await pgdb.get_results(limit=5, change_filter="price_stock")
    assert res["total_is_estimate"] is False
    assert res["total"] == 1


@pytest.mark.asyncio
async def test_batch_filter_stays_exact(pgdb, pgconn, monkeypatch):
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    est, real = await _skew(pgconn, analyzed=40, extra=7)
    bid = await pgconn.fetchval(
        "INSERT INTO batches (name) VALUES ('b') RETURNING id")
    await pgconn.executemany(
        "INSERT INTO batch_asins (batch_id, asin) VALUES ($1,$2)",
        [(bid, f"B{i:09d}") for i in range(3)])

    res = await pgdb.get_results(limit=5, batch_id=bid)
    assert res["total_is_estimate"] is False
    assert res["total"] == 3


@pytest.mark.asyncio
async def test_with_total_false_reports_neither(pgdb, pgconn, monkeypatch):
    """不要 total 时，连统计表都不该读；标记也不能被"估算过"污染。"""
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    await _skew(pgconn, analyzed=40, extra=7)

    res = await pgdb.get_results(limit=5, with_total=False)
    assert res["total"] is None
    assert res["total_is_estimate"] is False


@pytest.mark.asyncio
async def test_cursor_page_does_not_estimate_behind_with_total_false(pgdb, pgconn, monkeypatch):
    """翻页（有 cursor）本身不改变估算判据 —— keyset 谓词是在快照之后才追加的。

    也就是说带 cursor 的首屏请求仍然算"无筛选"，仍然可以估算。
    这条把那个语义钉住：它曾经差点被写成"有 cursor 就精确"。
    """
    monkeypatch.setattr(rr, "TOTAL_ESTIMATE_MIN_ROWS", 10)
    est, real = await _skew(pgconn, analyzed=40, extra=7)
    first = await pgdb.get_results(limit=5)

    res = await pgdb.get_results(limit=5, cursor_id=first["next_cursor"])
    assert res["total"] == est
    assert res["total_is_estimate"] is True
