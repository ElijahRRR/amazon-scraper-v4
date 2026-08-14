"""tests/pgdb/test_relay.py —— Phase 2 事件流：DDL / 分区 / gen / relay。

这一组的核心是**游标保证**，而且它必须被**证明**，不是被断言：

    对固定的 gen，若消费者持久化 X = 已见过的最大 seq 且总是请求 seq > X，
    则任何在后续请求时刻已提交且 seq > X 的行，必定在该次或之后某次请求中
    被返回，且按 seq 严格递增。绝不跳过已提交行。

所以 test_cursor_guarantee_under_staggered_commits 里有两样东西缺一不可：

  1. **对照组**（test_naive_cursor_skips_committed_rows）先证明这个测试形状
     真的能观察到危险——同样的交错走裸 bigserial 表，消费者会永久跳过一行。
     没有对照组的"没跳过"是个真空断言：也许写入根本没并发。
  2. **非真空断言**：跑完必须存在一对记录 a、b，满足
     ``outbox_id[a] < outbox_id[b]`` 而 ``seq[a] > seq[b]``。
     那正是裸游标会丢掉的那种倒置；它出现了，才说明危险交错真的发生过。

黄金校验对这一整组是**结构性失明**的（4 个后台协程被 no-op、TestClient 严格
顺序执行），跟 Phase 1 看不到 F1/F3 是同一个原因。别指望它兜底。
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from common.pgdb import relay as R

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ==================================================================
# 0) 纯函数（不碰库，SQLite-only 机器上也会跑）
# ==================================================================

def test_outcome_taxonomy_is_a_closed_set():
    assert R.normalize_outcome("ok") == ("ok", False)
    assert R.normalize_outcome("stale") == ("stale", False)
    # 越界值就地归一化，而不是靠 CHECK 约束——一个脏值不该让整条流停摆
    assert R.normalize_outcome("weird") == ("parse_failed", True)
    assert R.normalize_outcome(None) == ("parse_failed", True)


def test_error_type_mapping():
    assert R.outcome_for_error_type("blocked") == "blocked"
    assert R.outcome_for_error_type("captcha") == "blocked"
    for et in ("parse_error", "variant_offset", "network", "timeout",
               "session_not_ready", "zip_switch_failed", "zip_not_effective",
               "server_reject", "", None, "something_new"):
        assert R.outcome_for_error_type(et) == "parse_failed", et


def test_not_found_is_exact_match_never_a_prefix():
    assert R.classify_success_outcome({"title": R.NOT_FOUND_TITLE}) == "not_found"
    # 审计 §2.11 / 计划 §4.2：绝不能用 startswith("[")
    assert R.classify_success_outcome({"title": "[2-Pack] Storage Bins"}) == "ok"
    assert R.classify_success_outcome({"title": "[页面为空]"}) == "parse_failed"
    assert R.classify_success_outcome({}) == "ok"


def test_marketplace_never_passes_through_parser_site():
    # worker/parser.py:1333 永远发 "site": "US"
    assert R.normalize_marketplace("US") == ("amazon.com", True)
    assert R.normalize_marketplace("amazon.com") == ("amazon.com", False)
    assert R.normalize_marketplace(None) == ("amazon.com", True)


def test_zip_padding_only_restores_lost_leading_zeros():
    assert R.normalize_zip("1001") == ("01001", True)
    assert R.normalize_zip("10001") == ("10001", False)
    assert R.normalize_zip(None) == ("", False)
    # 不认识的形状原样透传：宁可留着，也不要把一个商品的价格序列劈成两组
    assert R.normalize_zip("SW1A 1AA") == ("SW1A 1AA", False)


def test_scrub_nul_is_recursive_and_counts():
    obj = {"a\x00": ["x\x00y", {"b": "\x00\x00"}], "c": 1}
    clean, n = R.scrub_nul(obj)
    assert n == 4
    assert clean == {"a": ["xy", {"b": ""}], "c": 1}
    assert "\x00" not in R.dumps_body(clean)


def test_collected_at_uses_the_worker_utc8_wall_clock():
    fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
    got, fell_back = R.parse_collected_at("2026-08-05 10:00:00", fallback)
    assert not fell_back
    # worker/parser.py:13 _CN_TZ = UTC+8，且 crawl_time 不带偏移标记
    assert got == datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc)
    assert R.parse_collected_at("", fallback) == (fallback, True)
    assert R.parse_collected_at("not a time", fallback) == (fallback, True)


def test_partition_bound_parsing():
    assert R.parse_partition_bound(
        "FOR VALUES FROM (MINVALUE) TO ('20000000')") == (None, 20000000)
    assert R.parse_partition_bound(
        "FOR VALUES FROM ('20000000') TO ('40000000')") == (20000000, 40000000)


def test_hash_ver_string_to_int():
    # common.slowhash.HASH_VER 是 'v1'，scrape_events.hash_ver 是 int
    assert R.parse_hash_ver("v1") == 1
    assert R.parse_hash_ver(2) == 2
    assert R.parse_hash_ver("garbage") == 1


def test_row_fault_classification_never_quarantines_a_partition_overflow():
    class _NoPart(asyncpg.CheckViolationError):
        pass

    exc = _NoPart('no partition of relation "scrape_events" found for row')
    assert R._is_row_fault(exc) is False           # 分区余量的问题，不是行的问题
    assert R._is_row_fault(asyncpg.NotNullViolationError("x")) is True
    assert R._is_row_fault(asyncpg.InterfaceError("conn gone")) is False
    assert R._is_row_fault(asyncio.TimeoutError()) is False


# ==================================================================
# 1) DDL / 分区
# ==================================================================

@pytest.mark.asyncio
async def test_ddl_shape_and_first_partitions(pgdb, pgconn):
    parts = await pgdb._list_partitions(pgconn)
    names = [p[0] for p in parts]
    assert names[0] == "scrape_events_p0"
    assert parts[0][1] is None                       # MINVALUE
    assert len(parts) >= 1 + R.EVENT_FUTURE_PARTITIONS

    # 每个分区都必须自带 source_id 唯一索引。父表上建不了（声明式分区要求唯一约束
    # 必须包含分区键 seq），LIKE 父表又会静默漏掉它 —— 所以这是硬闸门。
    for name, *_ in parts:
        defs = [r["indexdef"] for r in await pgconn.fetch(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='scraper' "
            "AND tablename=$1", name)]
        assert any("UNIQUE" in d and "source_id" in d for d in defs), (name, defs)
        assert any("recorded_at" in d for d in defs), (name, defs)

    # 计划 §2.1 的 UNIQUE INDEX ON <父表> (source_id) 在任何 PG 版本上都不合法
    with pytest.raises(asyncpg.FeatureNotSupportedError):
        await pgconn.execute(
            "CREATE UNIQUE INDEX x_parent_src ON scraper.scrape_events (source_id)")


@pytest.mark.asyncio
async def test_no_default_partition_overflow_fails_loudly_and_loses_nothing(pgdb, pgconn):
    """溢出必须是「吵闹的停摆」，不是「悄悄收下」。"""
    top = max(p[2] for p in await pgdb._list_partitions(pgconn))
    await pgconn.execute(
        "SELECT setval(pg_get_serial_sequence('scraper.scrape_events','seq'), $1)",
        top + 10)

    async with pgdb._write_lock("other"):
        await pgdb._db.execute("BEGIN")
        await pgdb._emit_outbox(outcome="ok", data={"asin": "B0OVERFLOW"})
        await pgdb._db.execute("COMMIT")

    rc = await pgdb._relay_open_conn()
    try:
        with pytest.raises(asyncpg.CheckViolationError) as ei:
            await pgdb._relay_drain_once(rc, 500)
        assert "no partition" in str(ei.value).lower()
        # 事务回滚 -> DELETE 也回滚 -> 行原样留在 outbox。零丢失。
        assert await pgconn.fetchval(
            "SELECT count(*) FROM scraper.scrape_outbox") == 1
        assert await pgconn.fetchval(
            "SELECT count(*) FROM scraper.scrape_events") == 0
        # 而且**不许**被当成毒丸隔离掉
        assert R._is_row_fault(ei.value) is False
    finally:
        await rc.close()


@pytest.mark.asyncio
async def test_partition_rollover_keeps_two_future_partitions(pgdb, pgconn):
    before = await pgdb._list_partitions(pgconn)
    top = max(p[2] for p in before)
    # 把序列推到最高分区的上界之下一点点：未来分区只剩 1 个
    await pgconn.execute(
        "SELECT setval(pg_get_serial_sequence('scraper.scrape_events','seq'), $1)",
        top - R.EVENT_PARTITION_SPAN - 1)

    created = await pgdb.ensure_event_partitions(pgconn)
    assert created >= 1
    after = await pgdb._list_partitions(pgconn)
    assert len(after) > len(before)

    last_value = await pgdb._seq_last_value(pgconn)
    future = [p for p in after if p[1] is not None and p[1] > last_value]
    assert len(future) >= R.EVENT_FUTURE_PARTITIONS

    # 区间必须首尾相接、无缝无叠
    bounds = sorted((p[1] or 0, p[2]) for p in after)
    for (_, hi), (lo2, _) in zip(bounds, bounds[1:]):
        assert hi == lo2, bounds

    # 新分区必须继承 source_id 唯一索引（LIKE 抄父表就会静默漏掉它）
    for name, *_ in after:
        defs = [r["indexdef"] for r in await pgconn.fetch(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='scraper' "
            "AND tablename=$1", name)]
        assert any("UNIQUE" in d and "source_id" in d for d in defs), name


async def _range_checks(conn, partition: str):
    """某个分区身上所有 ``*_range`` CHECK 的 (名字, 定义)。"""
    rows = await conn.fetch("""
        SELECT con.conname, pg_get_constraintdef(con.oid) AS condef
          FROM pg_constraint con
          JOIN pg_class rel   ON rel.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = rel.relnamespace
         WHERE n.nspname='scraper' AND rel.relname=$1 AND con.contype='c'
           AND con.conname LIKE '%%_range'
         ORDER BY con.conname""", partition)
    return [(r["conname"], r["condef"]) for r in rows]


@pytest.mark.asyncio
async def test_every_partition_carries_exactly_its_own_range_check(pgdb, pgconn):
    """B1 —— 从 p2 起每个分区都收不下任何行，**每一个新库上都已经如此**。

    ``LIKE scraper.<上一个分区> INCLUDING ALL`` 蕴含 ``INCLUDING CONSTRAINTS``，
    于是模板分区自己那条 range CHECK 被照抄进新分区。p0 是 ``PARTITION OF`` 建的、
    没有 range CHECK，所以 p1 干净；p1 → p2 抄来了 ``scrape_events_p1_range``，
    此后每个分区都带着一条与自己边界**矛盾**的 CHECK：

        scrape_events_p2  scrape_events_p1_range  CHECK (seq >= 20000000 AND seq < 40000000)
        scrape_events_p2  scrape_events_p2_range  CHECK (seq >= 40000000 AND seq < 60000000)
        p2 seq=40000001 -> CheckViolationError: ... violates "scrape_events_p1_range"

    两条互斥 CHECK ⇒ 该分区永远不可写；而那个 CheckViolationError 的文本里没有
    ``no partition of relation``，``_is_row_fault()`` 会返回 True，于是**完好的行
    被一条一条搬进死信表**——不是设计好的吵闹停摆，是消费侧看不见的静默丢失。

    上一版的硬闸门只核对 source_id 唯一索引，所以这件事一路绿灯。这里补上。
    """
    parts = await pgdb._list_partitions(pgconn)
    assert len(parts) >= 3, "至少要有 p0/p1/p2 才谈得上抄错模板"
    for name, lo, hi, idx in parts:
        got = await _range_checks(pgconn, name)
        if idx == 0:
            assert got == [], f"p0 是 PARTITION OF 建的，不该有 range CHECK：{got}"
            continue
        assert len(got) == 1, f"{name} 的 range CHECK 不止一条：{got}"
        assert got[0][0] == f"{name}_range", f"{name} 挂着别人的 CHECK：{got}"
        assert str(lo) in got[0][1] and str(hi) in got[0][1], (name, lo, hi, got)


@pytest.mark.asyncio
async def test_every_partition_actually_accepts_a_row(pgdb, pgconn):
    """上一个用例的行为侧对照：光看目录不够，真的往每个分区里写一行。

    ``test_partition_rollover_keeps_two_future_partitions`` 验的是边界连续性与
    索引继承，**从来没往新分区里写过一行**——这正是 B1 活到验证阶段的原因。
    """
    for name, lo, hi, idx in await pgdb._list_partitions(pgconn):
        seq = (lo or 0) + 1
        await pgconn.execute(
            "INSERT INTO scraper.scrape_events (seq, source_id, gen, asin,"
            " marketplace, zip_requested, zip_verify, collected_at, recorded_at,"
            " outcome, payload) VALUES ($1,$2,'g','B0X','amazon.com','10001',"
            "'unverified',now(),now(),'ok','{}')", seq, f"probe-{seq}")
        assert await pgconn.fetchval(
            "SELECT count(*) FROM scraper.scrape_events WHERE seq = $1", seq) == 1, name


@pytest.mark.asyncio
async def test_seq_crossing_a_partition_boundary_lands_and_is_readable(
        pgdb, pgconn, monkeypatch):
    """真 relay 把 seq 推**过**两条分区边界；行必须落库、且消费者拉得回来。

    整个套件此前从没跨过一条边界（seq 一直在 p0 的 [MINVALUE, 20000000) 里），
    所以「p2 永远收不下行」这件事在测试里完全不可见。这里把序列直接顶到边界前，
    走完整的 outbox -> relay -> scrape_events -> 游标读回链路。
    """
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    gen = pgdb.event_gen
    parts = await pgdb._list_partitions(pgconn)
    bounds = sorted(p[2] for p in parts if p[2] is not None)[:2]   # p0.hi, p1.hi

    assert await pgdb.start_event_relay() is True
    try:
        for edge in bounds:
            await pgconn.execute(
                "SELECT setval(pg_get_serial_sequence('scraper.scrape_events','seq'),"
                " $1, true)", edge - 3)
            for i in range(6):
                await _emit_direct(pgconn, f"{gen}:x{edge}-{i}", gen, f"B0X{i}")
            for _ in range(300):
                if await pgconn.fetchval(
                        "SELECT count(*) FROM scraper.scrape_outbox") == 0:
                    break
                await asyncio.sleep(0.02)
    finally:
        await pgdb.stop_event_relay()

    placement = await pgconn.fetch(
        "SELECT tableoid::regclass::text AS part, count(*) AS n "
        "FROM scraper.scrape_events GROUP BY 1 ORDER BY 1")
    names = {r["part"].rsplit(".", 1)[-1]: int(r["n"]) for r in placement}
    assert len(names) >= 3, f"没有真的跨过分区边界，行都落在 {names}"

    # 零丢失、零死信、游标能把每一行都读回来
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox_dead") == 0
    total = await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_events")
    assert total == 12, f"12 行只到了 {total} 行（差额是被误当毒丸隔离掉的）"

    x, seen = 0, []
    while True:
        rows = await pgconn.fetch(
            "SELECT seq FROM scraper.scrape_events WHERE seq > $1 "
            "ORDER BY seq LIMIT 100", x)
        if not rows:
            break
        seen += [int(r["seq"]) for r in rows]
        x = seen[-1]
    assert len(seen) == 12 and seen == sorted(seen) and len(set(seen)) == 12


@pytest.mark.asyncio
async def test_a_partition_poisoned_by_the_old_recipe_gets_repaired(pgdb, pgconn):
    """修好 DDL 还不够：**每一个** Phase 2 建过的库里 p2 都已经中毒了。

    p1/p2 在 ``connect()`` 期就建出来了，所以 scraper_dev 以及任何跑过 Phase 2 的
    部署都带着一条 ``scrape_events_p2 · scrape_events_p1_range``。这里把 HEAD 的
    产物原样复现出来，再让分区维护把它就地修好。
    """
    parts = await pgdb._list_partitions(pgconn)
    p1, p2 = parts[1], parts[2]
    await pgconn.execute(
        f'ALTER TABLE scraper."{p2[0]}" ADD CONSTRAINT "{p1[0]}_range" '
        f"CHECK (seq >= {p1[1]} AND seq < {p1[2]})")
    assert len(await _range_checks(pgconn, p2[0])) == 2

    seq = p2[1] + 1
    sql = ("INSERT INTO scraper.scrape_events (seq, source_id, gen, asin,"
           " marketplace, zip_requested, zip_verify, collected_at, recorded_at,"
           " outcome, payload) VALUES ($1,$2,'g','B0X','amazon.com','10001',"
           "'unverified',now(),now(),'ok','{}')")
    with pytest.raises(asyncpg.CheckViolationError) as ei:
        await pgconn.execute(sql, seq, "before")
    assert f"{p1[0]}_range" in str(ei.value)
    # 而且它**不会**被认成分区溢出：relay 会把完好的行当毒丸隔离掉
    assert R._is_row_fault(ei.value) is True

    await pgdb.ensure_event_partitions(pgconn)

    assert await _range_checks(pgconn, p2[0]) == [
        (f"{p2[0]}_range", f"CHECK (((seq >= {p2[1]}) AND (seq < {p2[2]})))")]
    await pgconn.execute(sql, seq, "after")
    assert await pgconn.fetchval(
        "SELECT count(*) FROM scraper.scrape_events WHERE seq = $1", seq) == 1
    assert pgdb.event_relay_metrics()["counters"]["range_checks_repaired"] == 1

    # 修复不许误伤父表的 marketplace CHECK（EXCLUDING CONSTRAINTS 就会——实测
    # 那样连 ATTACH 都过不去：DatatypeMismatchError: child table is missing
    # constraint "scrape_events_marketplace_check"）
    with pytest.raises(asyncpg.CheckViolationError) as ei:
        await pgconn.execute(
            "INSERT INTO scraper.scrape_events (seq, source_id, gen, asin,"
            " marketplace, zip_requested, zip_verify, collected_at, recorded_at,"
            " outcome, payload) VALUES ($1,'mk','g','B0X','walmart.com','10001',"
            "'unverified',now(),now(),'ok','{}')", seq + 1)
    assert "marketplace" in str(ei.value)


@pytest.mark.asyncio
async def test_partition_gate_rejects_a_contradictory_check(pgdb, pgconn):
    """硬闸门本身也要有测试：造一个中毒的新分区，``_create_partition`` 必须炸。"""
    parts = await pgdb._list_partitions(pgconn)
    top = max(p[2] for p in parts)
    nxt = max(p[3] for p in parts) + 1
    name = f"scrape_events_p{nxt}"
    lo, hi = top, top + R.EVENT_PARTITION_SPAN
    # 预先把表建好并挂上一条**别人的** range CHECK，模拟 LIKE 抄错模板
    await pgconn.execute(
        f"CREATE TABLE scraper.{name} (LIKE scraper.scrape_events INCLUDING ALL)")
    await pgconn.execute(
        f"CREATE UNIQUE INDEX {name}_source_id_key ON scraper.{name} (source_id)")
    await pgconn.execute(
        f"ALTER TABLE scraper.{name} ADD CONSTRAINT scrape_events_p1_range "
        f"CHECK (seq >= 1 AND seq < 2)")

    with pytest.raises(RuntimeError, match="range CHECK"):
        await pgdb._create_partition(pgconn, nxt, lo, hi)


@pytest.mark.asyncio
async def test_attach_partition_does_not_block_an_open_writer(pgdb, pgconn):
    """新分区必须走 create+ATTACH：PARTITION OF 拿 ACCESS EXCLUSIVE 会卡死 relay。"""
    other = await asyncpg.connect(pgdb.dsn)
    try:
        await other.execute("BEGIN")
        await other.execute(
            "INSERT INTO scraper.scrape_outbox (body) VALUES ('{}'::jsonb)")
        # 写事务开着的情况下建分区：必须在 3s 内完成
        await asyncio.wait_for(pgdb.ensure_event_partitions(pgconn, floor_seq=10**7),
                               timeout=3.0)
        await other.execute("ROLLBACK")
    finally:
        await other.close()


# ==================================================================
# 2) gen / instance_id
# ==================================================================

@pytest.mark.asyncio
async def test_gen_is_reused_across_reconnects(pgdb):
    """普通发版**不得**改 gen —— 契约 §5.5 把 gen 变化定义成消费侧硬停 + 全量对账。"""
    from common.pgdb import Database

    gen = pgdb.event_gen
    assert gen and len(gen) == 12

    db2 = Database()               # pgdb 夹具已经把 PG_DSN 指到这个临时库
    await db2.connect()
    try:
        assert db2.event_gen == gen
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_rewind_mints_a_new_gen_and_pushes_the_sequence_forward(pgdb, pgconn):
    """T11(a)：只回滚 DB -> 启动检出回退 -> 铸新 gen。"""
    old_gen = pgdb.event_gen
    # 假装曾经发到过 seq=500000，然后库被恢复回了空
    await pgconn.execute(
        "INSERT INTO scraper.sync_meta (k,v) VALUES ('max_seq_ever','500000') "
        "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v")

    await pgdb._bootstrap_identity(pgconn)

    assert pgdb.event_gen != old_gen
    assert await pgconn.fetchval(
        "SELECT v FROM scraper.sync_meta WHERE k='gen'") == pgdb.event_gen
    # 序列被推到已知最高水位之后，实例内 seq 保持单调
    assert await pgdb._seq_last_value(pgconn) >= 500000


@pytest.mark.asyncio
async def test_retention_emptying_the_table_is_not_a_rewind(pgdb, pgconn):
    """B4 —— 保留期把 scrape_events 清空之后，**普通重启不得铸新 gen**。

    原判据是 ``max(seq) < max_seq_ever``。Phase 6 拥有分区 DROP，一旦保留期把表
    清空，``max(seq)`` 归零而 ``max_seq_ever`` 还在 meta 表里，于是每一次普通重启
    都会铸新 gen —— 按契约 §5.5，消费侧对此的响应是**硬停 + 全量对账**。
    verify1 在每个 TRUNCATE 上都误触发过：

        事件流倒退：max(seq)=0 < max_seq_ever=1001 ... 铸新 gen 7b8ef3c5 -> 2974f9fd

    真正要防的是「seq 被重新发一遍」，而序列**不随分区 DROP 回退**。所以判据换成
    序列水位。下面两半必须同时成立，否则这个修复不是收紧就是放松了判据。
    """
    gen0 = pgdb.event_gen
    for i in range(4):
        await _emit_direct(pgconn, f"{gen0}:ret-{i}", gen0, f"B0RET{i}")
    rc = await pgdb._relay_open_conn()
    try:
        await pgdb._relay_drain_once(rc, 500)
    finally:
        await rc.close()
    assert await pgconn.fetchval(
        "SELECT v FROM scraper.sync_meta WHERE k='max_seq_ever'") == "4"

    # (a) 保留期：行没了，序列还在 —— 不是回退
    await pgconn.execute("DELETE FROM scraper.scrape_events")
    assert await pgdb._seq_high_water(pgconn) == 4
    minted_before = pgdb.event_relay_metrics()["counters"]["gen_minted"]
    await pgdb._bootstrap_identity(pgconn)
    assert pgdb.event_gen == gen0, "保留期清空被误判成回退，消费侧会被迫全量对账"
    assert pgdb.event_relay_metrics()["counters"]["gen_minted"] == minted_before
    assert pgdb.event_relay_metrics()["counters"]["rewinds_detected"] == 0

    # (b) 真回退：序列本身退到了历史高水位之下 —— 必须铸新 gen 并把序列推回去
    await pgconn.execute(
        "SELECT setval(pg_get_serial_sequence('scraper.scrape_events','seq'), 1, false)")
    assert await pgdb._seq_high_water(pgconn) == 0
    await pgdb._bootstrap_identity(pgconn)
    assert pgdb.event_gen != gen0
    assert pgdb.event_relay_metrics()["counters"]["rewinds_detected"] == 1
    assert await pgdb._seq_last_value(pgconn) >= 4


@pytest.mark.asyncio
async def test_seq_high_water_does_not_count_an_unused_sequence(pgdb, pgconn):
    """``last_value=1, is_called=false`` 是「一个号都没发过」，不是「发过 1」。

    分区维护要的是"下一个号落在哪"（``_seq_last_value``），回退检测要的是
    "发出去过的最高号"（``_seq_high_water``）。差一个，全新库就会被当成有过 seq=1。
    """
    assert await pgdb._seq_high_water(pgconn) == 0
    assert await pgdb._seq_last_value(pgconn) == 1
    await pgconn.execute(
        "SELECT nextval(pg_get_serial_sequence('scraper.scrape_events','seq'))")
    assert await pgdb._seq_high_water(pgconn) == 1


@pytest.mark.asyncio
async def test_gen_is_stored_on_every_row_not_just_in_meta(pgdb, pgconn):
    """只存 meta 表的话，一次从备份恢复会把全部历史重贴上恢复后的标签。"""
    async with pgdb._write_lock("other"):
        await pgdb._db.execute("BEGIN")
        await pgdb._emit_outbox(outcome="ok", data={"asin": "B0GEN"})
        await pgdb._db.execute("COMMIT")
    rc = await pgdb._relay_open_conn()
    try:
        await pgdb._relay_drain_once(rc, 500)
    finally:
        await rc.close()
    row = await pgconn.fetchrow("SELECT gen, source_id FROM scraper.scrape_events")
    assert row["gen"] == pgdb.event_gen
    assert row["source_id"].startswith(pgdb.event_gen + ":")


# ==================================================================
# 3) 写钩子的事务归属
# ==================================================================

@pytest.mark.asyncio
async def test_emit_joins_the_callers_transaction_and_rolls_back_with_it(pgdb, pgconn):
    async with pgdb._write_lock("other"):
        await pgdb._db.execute("BEGIN")
        oid = await pgdb._emit_outbox(outcome="ok", data={"asin": "B0ROLLBACK"})
        assert oid is not None
        await pgdb._db.execute("ROLLBACK")
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0


@pytest.mark.asyncio
async def test_emit_scrubs_nul_bytes_that_jsonb_cannot_represent(pgdb, pgconn):
    """jsonb 连**正确转义过**的 \\u0000 都拒收（22P05），与 text 列的 22021 还不是同一个。"""
    async with pgdb._write_lock("other"):
        await pgdb._db.execute("BEGIN")
        await pgdb._emit_outbox(outcome="ok",
                                data={"asin": "B0NUL", "title": "bad\x00title"})
        await pgdb._db.execute("COMMIT")
    body = json.loads(await pgconn.fetchval("SELECT body::text FROM scraper.scrape_outbox"))
    assert body["result"]["title"] == "badtitle"
    assert pgdb.event_relay_metrics()["counters"]["emit_nul_stripped"] == 1


# ==================================================================
# 4) 单例（T5）
# ==================================================================

@pytest.mark.asyncio
async def test_second_relay_refuses_to_start(pgdb):
    from common.pgdb import Database

    assert await pgdb.start_event_relay() is True
    db2 = Database()
    await db2.connect()
    try:
        assert await db2.start_event_relay() is False
        assert db2.event_relay_metrics()["relay_state"] == "refused"
        # 第二个进程不许留下任何后台任务
        assert db2._ev()["relay_task"] is None

        # 第一个让位之后，第二个就能接管——滚动部署要靠这条
        await pgdb.stop_event_relay()
        assert await db2.start_event_relay() is True
        await db2.stop_event_relay()
    finally:
        await db2.close()
        await pgdb.stop_event_relay()


@pytest.mark.asyncio
async def test_relay_bootstrap_does_not_ride_on_the_shared_write_transaction(pgdb):
    """relay 的引导 DDL 必须跑在**它自己的连接**上。

    relay 是 lifespan 里 ``create_task`` 起来的，它第一次真正执行的时刻，服务
    很可能已经在处理请求了 —— 也就是 ``_write_conn`` 上正开着别人的事务。
    在那条**裸** asyncpg 连接上发 DDL 有两种死法：``InterfaceError: another
    operation is in progress``，或者更坏 —— DDL 悄悄进了别人的事务，跟着别人
    一起回滚，于是"表建过了"这件事在下一次写钩子触发时才被发现。

    这里把那个前置状态造出来：外人开着事务，此时启动 relay，然后让外人**回滚**。
    事件流的表必须活下来。
    """
    verify = await asyncpg.connect(pgdb.dsn)
    try:
        await verify.execute("DROP SCHEMA scraper CASCADE")
        pgdb._ev()["ready"] = False

        holding = asyncio.Event()
        release = asyncio.Event()

        async def hold_a_foreign_transaction():
            async with pgdb._write_lock("other"):
                await pgdb._db.execute("BEGIN")
                await pgdb._db.execute("CREATE TABLE tmp_holder_marker (x int)")
                holding.set()
                await release.wait()
                await pgdb._db.execute("ROLLBACK")

        holder = asyncio.create_task(hold_a_foreign_transaction())
        await asyncio.wait_for(holding.wait(), timeout=5.0)
        try:
            assert await asyncio.wait_for(pgdb.start_event_relay(), timeout=10.0) is True
        finally:
            release.set()
            await holder
            await pgdb.stop_event_relay()

        # 外人的事务整个回滚了：他的表没了，事件流的表还在 = DDL 独立提交过
        assert await verify.fetchval("SELECT to_regclass('tmp_holder_marker')") is None
        for table in R.EVENT_EXPECTED_COLUMNS:
            assert await verify.fetchval(
                "SELECT to_regclass($1)", f"scraper.{table}") is not None, table
        assert await verify.fetchval(
            "SELECT v FROM scraper.sync_meta WHERE k = 'gen'") == pgdb.event_gen
    finally:
        await verify.close()


# ==================================================================
# 4.5) 活性：一个进程必须**保持**有 relay（B2），死连接必须能恢复（B3）
# ==================================================================
#
# 单例锁保证「不会有两个 relay」。这一节保证的是反面 ——「不会一个都没有」。
# 两个方向都失手过，而且两次的现象都是：HTTP 全绿、outbox 只涨不落、
# relay_state 报健康。

async def _advisory_lock_holders(conn) -> int:
    return int(await conn.fetchval(
        "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
        "WHERE l.locktype = 'advisory' AND a.datname = current_database()") or 0)


async def _holders_are(conn, n: int) -> bool:
    return await _advisory_lock_holders(conn) == n


async def _relay_backend_pid(conn):
    """从**另一条**连接查出 relay 那条会话的后端 pid，查不到返回 None。

    别改成 ``pgdb._ev()["relay_conn"].fetchval("SELECT pg_backend_pid()")``：
    那条连接归 relay 循环所有，而用例把 tick 压到 20ms，asyncpg 又不允许一条连接
    上有两个操作在飞——在快机器上会稳定撞出
    ``InterfaceError: cannot perform operation: another operation is in progress``，
    而且是**测试** flake，产品一点问题没有。单例 advisory 锁本来就唯一标识 relay
    那条会话，从旁路查是等价的，还顺带验证了「锁确实被持有」。
    """
    return await conn.fetchval(
        "SELECT a.pid FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
        "WHERE l.locktype = 'advisory' AND a.datname = current_database() "
        "LIMIT 1")


async def _drained(conn) -> bool:
    return await conn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0


async def _wait_for(fn, timeout=20.0, step=0.05):
    """轮询到 fn() 为真；返回是否等到（不 assert，留给调用方给出好错误信息）。

    ⚠ 传进来的 lambda 要么返回 bool，要么返回**协程**（这里会 await 它）。
    别写 ``lambda: some_coro(x) == 0`` —— 那是拿协程对象跟 0 比，永远 False，
    还会漏一个 never-awaited 警告。
    """
    for _ in range(int(timeout / step)):
        got = fn()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return True
        await asyncio.sleep(step)
    return False


@pytest.mark.asyncio
async def test_rolling_restart_ends_with_exactly_one_relay(pgdb, pgconn, monkeypatch):
    """B2 —— 滚动重启之后必须**还有** relay 在跑，而且只有一个。

    ``run_event_relay`` 原来是「start 返回 False 就 return」。滚动部署时新实例
    起来时老实例还在跑，于是新实例当场放弃，老实例随后停机 —— 事件流从此没有
    写入者，而 HTTP 一路正常、outbox 无界增长。实测（verify4 A3，驱动真入口）：

        t1: NEW.state=refused task.done=True
        t2: OLD 停机。库上持锁数=0
        t3: 等 4s 之后 NEW.state=refused task.done=True   持锁数=0
        >>> 滚动重启之后还有 relay 在跑吗？ 否 —— 事件流已死，outbox 只涨不落

    "拿不到锁是滚动部署时的正常状态"这句话只对了一半：当时正常，但必须继续排队。
    """
    from common.pgdb import Database

    monkeypatch.setattr(R, "RELAY_RETRY_SECONDS", 0.1)
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    gen = pgdb.event_gen

    old_task = asyncio.create_task(pgdb.run_event_relay())
    new = Database()                       # pgdb 夹具把 PG_DSN 指向同一个库
    await new.connect()
    new_task = asyncio.create_task(new.run_event_relay())
    try:
        assert await _wait_for(lambda: pgdb._ev()["relay_state"] == "running")
        # 新实例看到锁被占，必须**留在原地重试**，而不是结束
        assert await _wait_for(lambda: new._ev()["relay_state"] == "refused")
        assert not new_task.done(), "新实例放弃了 —— 老实例一停就没人接班了"
        assert await _advisory_lock_holders(pgconn) == 1

        for i in range(6):                 # 交接期间照常有写入
            await _emit_direct(pgconn, f"{gen}:roll-{i}", gen, f"B0ROLL{i}")

        old_task.cancel()                  # 老实例停机
        try:
            await old_task
        except (asyncio.CancelledError, Exception):  # noqa: B014
            pass
        await pgdb.stop_event_relay()

        assert await _wait_for(lambda: new._ev()["relay_state"] == "running"), (
            f"老实例停了之后新实例仍是 {new._ev()['relay_state']!r} —— 事件流已死")
        assert await _wait_for(lambda: _drained(pgconn)), "接管之后 outbox 没有被抽干"
        assert await _advisory_lock_holders(pgconn) == 1, "接管之后不是恰好一个持锁者"
        assert await pgconn.fetchval(
            "SELECT count(*) FROM scraper.scrape_events") == 6
    finally:
        new_task.cancel()
        for coro in (new_task, new.stop_event_relay(), new.close(),
                     pgdb.stop_event_relay()):
            try:
                await coro
            except (asyncio.CancelledError, Exception):  # noqa: B014, PERF203
                pass


@pytest.mark.asyncio
async def test_a_transient_connect_failure_at_boot_is_retried(pgdb, pgconn,
                                                              monkeypatch):
    """B2 的第二个触发点：启动瞬间连一次库失败 = 本进程余生没有事件流。

    实测（verify4 h4_boot.py）：一条 ``PostgresConnectionError: connection
    refused`` 让 start 抛出去，lifespan 记一句日志就完了 ——
        _relay_open_conn 被调用 1 次；task.done=True  relay_state='failed'
        6 秒后（库早就好了）: {'outbox': 20, 'events': 0}  持锁数=0
    """
    monkeypatch.setattr(R, "RELAY_RETRY_SECONDS", 0.1)
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    gen = pgdb.event_gen

    real = pgdb._relay_open_conn
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise asyncpg.PostgresConnectionError("connection refused")
        return await real()

    monkeypatch.setattr(pgdb, "_relay_open_conn", flaky)
    task = asyncio.create_task(pgdb.run_event_relay())
    try:
        assert await _wait_for(lambda: len(calls) >= 2), "启动失败之后没有重试"
        for i in range(5):
            await _emit_direct(pgconn, f"{gen}:boot-{i}", gen, f"B0BOOT{i}")
        assert await _wait_for(lambda: pgdb._ev()["relay_state"] == "running")
        assert await _wait_for(lambda: _drained(pgconn))
        assert await pgconn.fetchval(
            "SELECT count(*) FROM scraper.scrape_events") == 5
    finally:
        task.cancel()
        for coro in (task, pgdb.stop_event_relay()):
            try:
                await coro
            except (asyncio.CancelledError, Exception):  # noqa: B014, PERF203
                pass


@pytest.mark.asyncio
async def test_relay_reconnects_after_its_own_backend_is_terminated(
        pgdb, pgconn, monkeypatch):
    """B3 —— ``pg_terminate_backend`` 打掉 relay 自己那条会话之后必须自愈。

    修之前（verify1 finding a / verify4 HIGH 3，实测）：

        pg_terminate_backend(9538) -> True
        relay 批次失败：InterfaceError: connection is closed   ×∞
        after kill : events=50 outbox_backlog=70 state='running' tick_errors=14
        advisory locks still held on the singleton key : 0
        instance #1 still reports relay_state='running'

    数据是安全的（认领的行全留在 outbox），但这一条 relay 永远转在一条死连接上，
    而 ``relay_state`` —— 那个专门为"让停摆变响"加出来的字段 —— 报告健康。
    本仓库是单实例部署，所以那等于事件流永久停摆且无人知晓。
    """
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    monkeypatch.setattr(R, "RELAY_ERROR_BACKOFF", 0.05)
    monkeypatch.setattr(R, "RELAY_RECOVER_AFTER", 2)
    gen = pgdb.event_gen

    assert await pgdb.start_event_relay() is True
    try:
        await _emit_direct(pgconn, f"{gen}:pre", gen, "B0PRE")
        assert await _wait_for(lambda: _drained(pgconn))

        pid = await _relay_backend_pid(pgconn)
        assert pid is not None, "relay 没持有单例锁，杀谁都没意义"
        assert await pgconn.fetchval("SELECT pg_terminate_backend($1)", pid) is True
        # 杀完之后单例锁确实没人持有了 —— 这就是"另一个实例可以接管"的窗口
        assert await _wait_for(lambda: _holders_are(pgconn, 0))

        for i in range(8):
            await _emit_direct(pgconn, f"{gen}:post-{i}", gen, f"B0POST{i}")

        assert await _wait_for(
            lambda: pgdb.event_relay_metrics()["counters"]["relay_reconnects"] >= 1,
            timeout=30.0), (
            f"relay 没有重连：state={pgdb._ev()['relay_state']!r} "
            f"counters={pgdb.event_relay_metrics()['counters']}")
        assert await _wait_for(lambda: _drained(pgconn), timeout=30.0)
    finally:
        await pgdb.stop_event_relay()

    m = pgdb.event_relay_metrics()
    assert m["relay_state"] in ("running", "stopped")
    assert m["counters"]["tick_errors"] >= 1, "这个用例必须真的踩到死连接"
    rows = await pgconn.fetch("SELECT seq, source_id FROM scraper.scrape_events "
                             "ORDER BY seq")
    assert len(rows) == 9, "零丢失：pre 1 条 + post 8 条"
    assert len({r["source_id"] for r in rows}) == 9
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox_dead") == 0


@pytest.mark.asyncio
async def test_relay_steps_down_if_someone_else_took_the_lock_meanwhile(
        pgdb, pgconn, monkeypatch):
    """B3 的另一半：重连时锁已经被别人拿走 -> 退位，绝不当第二个写入者。

    两个写入者一旦同时存在，seq 顺序就不再等于提交顺序，游标保证当场失效
    （见 test_two_relays_would_break_the_guarantee）。所以「恢复」必须是
    「重抢锁，抢不到就认输」，而且认输要**说出来**（state='refused'，不是 'running'）。
    """
    from common.pgdb import Database

    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    monkeypatch.setattr(R, "RELAY_ERROR_BACKOFF", 0.05)
    monkeypatch.setattr(R, "RELAY_RECOVER_AFTER", 2)
    gen = pgdb.event_gen

    assert await pgdb.start_event_relay() is True
    other = Database()
    await other.connect()
    try:
        # ⚠ 走 `_relay_backend_pid`（从**旁路**连接查），不要伸手到
        #   `pgdb._ev()["relay_conn"]` 上发查询 —— 那条连接归 relay 循环所有，
        #   本用例把 tick 压到 20ms，asyncpg 不允许一条连接上有两个操作在飞。
        #   实测这一处正是一条 flake：独占连跑 8 遍 2 红，失败恒为
        #   `InterfaceError: cannot perform operation: another operation is in progress`，
        #   且在本轮改造之前的基点上同样复现（1/8）—— 是**测试** flake，产品无问题。
        #   `_relay_backend_pid` 的 docstring 早就写明了这条禁令、兄弟用例
        #   `test_relay_recovers_after_pg_terminate_backend` 也一直照做，
        #   只有这一处漏改。
        pid = await _relay_backend_pid(pgconn)
        assert pid is not None, "relay 没持有单例锁，杀谁都没意义"
        await pgconn.fetchval("SELECT pg_terminate_backend($1)", pid)
        assert await _wait_for(lambda: _holders_are(pgconn, 0))
        assert await other.start_event_relay() is True        # 别人接管了

        for i in range(5):
            await _emit_direct(pgconn, f"{gen}:ho-{i}", gen, f"B0HO{i}")

        assert await _wait_for(
            lambda: pgdb._ev()["relay_state"] == "refused", timeout=30.0), (
            f"被打掉的实例仍自称 {pgdb._ev()['relay_state']!r}")
        assert pgdb._ev()["relay_conn"] is None
        assert await _wait_for(lambda: _drained(pgconn), timeout=30.0)
        assert await _advisory_lock_holders(pgconn) == 1, "退位之后应当只剩接管者"
        assert await pgconn.fetchval(
            "SELECT count(*) FROM scraper.scrape_events") == 5
    finally:
        for coro in (other.stop_event_relay(), other.close(),
                     pgdb.stop_event_relay()):
            try:
                await coro
            except Exception:  # noqa: BLE001, PERF203
                pass


# ==================================================================
# 5) 游标保证（本阶段的核心）
# ==================================================================

async def _emit_direct(conn, source_id, gen, asin, marketplace="amazon.com"):
    """绕过 _emit_outbox 直接插 outbox：测试要自己控制事务的开与提交时机。"""
    body = R.dumps_body({
        "v": 1, "source_id": source_id, "gen": gen, "outcome": "ok",
        "asin": asin, "marketplace": marketplace, "zip_requested": "10001",
        "result": {"asin": asin, "crawl_time": "2026-08-05 10:00:00"},
    })
    return await conn.fetchval(
        "INSERT INTO scraper.scrape_outbox (body) VALUES ($1::jsonb) RETURNING id",
        body)


@pytest.mark.asyncio
async def test_naive_cursor_skips_committed_rows(pgdb, pgconn):
    """**对照组**：证明这个测试形状真的能观察到危险。

    没有它，下面那个 "relay 没跳过" 就是个真空断言。
    """
    await pgconn.execute("CREATE TABLE naive (seq bigserial PRIMARY KEY, tag text)")
    a = await asyncpg.connect(pgdb.dsn)
    b = await asyncpg.connect(pgdb.dsn)
    try:
        await a.execute("BEGIN")
        await b.execute("BEGIN")
        sa = await a.fetchval("INSERT INTO naive (tag) VALUES ('A') RETURNING seq")
        sb = await b.fetchval("INSERT INTO naive (tag) VALUES ('B') RETURNING seq")
        assert sa < sb
        await b.execute("COMMIT")                     # B 后拿到序列号，却先提交

        seen = await pgconn.fetch("SELECT seq, tag FROM naive WHERE seq > 0 ORDER BY seq")
        cursor = max(r["seq"] for r in seen)
        assert [r["tag"] for r in seen] == ["B"]
        assert cursor == sb

        await a.execute("COMMIT")                     # A 现在也提交了
        after = await pgconn.fetch(
            "SELECT seq, tag FROM naive WHERE seq > $1 ORDER BY seq", cursor)

        assert await pgconn.fetchval("SELECT count(*) FROM naive WHERE tag='A'") == 1
        assert after == []          # A 已提交，却被游标永久跳过 —— 这就是要杀掉的东西
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_cursor_guarantee_under_staggered_commits(pgdb, pgconn, monkeypatch):
    """N 个并发写入者 + 刻意反转的提交顺序 + 一个 seq > X 的消费者。

    ╔══════════════════════════════════════════════════════════════════════════╗
    ║  不要把这个用例"简化"成走 Database API / HTTP 的版本。那样做**不是**重构，  ║
    ║  是把整个游标保证从测试覆盖里删掉，而且套件依然全绿。                        ║
    ║                                                                          ║
    ║  理由（verify1 finding b，实测）：D-2 至今让 `_db` 是**一条**写连接，外面还  ║
    ║  套着真锁 `_write_lock`，所以生产上的提交是**串行**的 —— 24 个并发           ║
    ║  `accept_success_result` 打进去，实测：                                    ║
    ║      outbox id order              : [1, 2, 3, ..., 12]                   ║
    ║      write-completion rank        : [0, 1, 2, ..., 11]                   ║
    ║      COMMIT-ORDER INVERSIONS via the real API : 0                        ║
    ║  也就是说：**今天没有任何 API 级 / HTTP 级的测试能制造出这个危险**。         ║
    ║  outbox 是提前修的保险（Phase 1.5 放开写并发时才真正需要），因此**只有**     ║
    ║  这个"多条裸连接直接写 outbox"的形状能当回归护栏。                          ║
    ║                                                                          ║
    ║  黄金校验同样盖不到：4 个后台协程被 no-op、TestClient 严格顺序执行。         ║
    ║  这个用例（连同下面的 test_two_relays_would_break_the_guarantee 反例、      ║
    ║  以及 T-A/T-D/T-E 三个用例）没了，Phase 1.5 放开写并发的那一天，            ║
    ║  游标保证会在一片绿灯里失效。                                              ║
    ╚══════════════════════════════════════════════════════════════════════════╝

    断言（三条缺一不可）：
      1. 消费者每次拿到的 seq 都严格递增且 > 上次游标；
      2. 抽干之后消费者见过的 source_id 集合 == 写入的全集（一条不漏）；
      3. **非真空**：存在 outbox_id[a] < outbox_id[b] 而 seq[a] > seq[b]，
         也就是危险交错真的发生了，只不过被 outbox 接住了。
    """
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    monkeypatch.setattr(R, "RELAY_BATCH", 50)

    n_writers, per_writer = 8, 12
    gen = pgdb.event_gen
    conns = [await asyncpg.connect(pgdb.dsn) for _ in range(n_writers)]
    gates = [asyncio.Event() for _ in range(n_writers + 1)]
    inserted_all = asyncio.Event()
    outbox_id = {}
    n_done = 0
    lock = asyncio.Lock()

    async def writer(k: int):
        nonlocal n_done
        conn = conns[k]
        await gates[k].wait()                 # 串行化「插入」阶段 -> id 顺序 = k 顺序
        await conn.execute("BEGIN")
        for j in range(per_writer):
            sid = f"{gen}:w{k}-{j}"
            outbox_id[sid] = await _emit_direct(conn, sid, gen, f"B0W{k:02d}{j:02d}")
        gates[k + 1].set()
        async with lock:
            n_done += 1
            if n_done == n_writers:
                inserted_all.set()
        await inserted_all.wait()
        # 提交顺序与 id 顺序**完全相反**：writer 0 的 id 最小、提交最晚
        await asyncio.sleep(0.04 * (n_writers - k))
        await conn.execute("COMMIT")

    seen_seq = []
    seen_src = {}
    stop = asyncio.Event()

    # 消费者**只记录，不断言**。断言全部挪到 teardown 之后：一个在后台任务里
    # 抛出的 AssertionError 会让 finally 里的 `await cons` 直接炸掉，于是
    # relay 任务泄漏、事件循环关不掉，一个本该是"红"的用例变成"挂住"。
    # 实测踩过：这个文件曾经 1/6 概率卡死，根因就是这里。
    async def consumer():
        c = await asyncpg.connect(pgdb.dsn)
        cursor = 0
        try:
            while not stop.is_set():
                rows = await c.fetch(
                    "SELECT seq, source_id FROM scraper.scrape_events "
                    "WHERE seq > $1 ORDER BY seq LIMIT 50", cursor)
                for r in rows:
                    polls.append((cursor, r["seq"], r["source_id"]))
                    seen_seq.append(r["seq"])
                    seen_src[r["source_id"]] = r["seq"]
                    cursor = r["seq"]
                await asyncio.sleep(0.01)
        finally:
            await c.close()

    polls = []
    assert await pgdb.start_event_relay() is True
    cons = asyncio.create_task(consumer())
    try:
        gates[0].set()
        await asyncio.gather(*(writer(k) for k in range(n_writers)))
        # 等 outbox 抽干 + 消费者追上
        for _ in range(400):
            depth = await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox")
            if depth == 0 and len(seen_src) == n_writers * per_writer:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        # 每一步都必须跑到，哪怕上一步炸了——漏掉 stop_event_relay 就是泄漏一个
        # 后台任务 + 一条连接，下一个用例会以"莫名其妙的挂起"形式收账。
        for coro in (cons, pgdb.stop_event_relay(), *(c.close() for c in conns)):
            try:
                await coro
            except Exception:                                  # noqa: BLE001, PERF203
                pass

    total = n_writers * per_writer
    assert len(outbox_id) == total
    # (2) 一条不漏
    assert set(seen_src) == set(outbox_id), (
        f"漏了 {set(outbox_id) - set(seen_src)}，多了 {set(seen_src) - set(outbox_id)}")
    # (1) 每一次返回都严格递增、且都 > 请求时的游标
    bad = [p for p in polls if not p[1] > p[0]]
    assert not bad, f"返回了 <= after_seq 的行: {bad[:5]}"
    assert seen_seq == sorted(seen_seq) and len(set(seen_seq)) == len(seen_seq), (
        f"seq 不是严格递增：{seen_seq[:20]} ...")

    # (3) 非真空：确实发生了「先拿到号、后提交」的倒置
    inversions = [
        (a, b) for a in outbox_id for b in outbox_id
        if outbox_id[a] < outbox_id[b] and seen_src[a] > seen_src[b]
    ]
    assert inversions, (
        f"没有观察到任何 outbox_id/seq 倒置（共 {total} 行，seq "
        f"{min(seen_seq)}..{max(seen_seq)}）——说明危险的并发交错根本没发生，"
        f"这个用例是真空的，别信它的绿灯。实测正常应有数千对倒置。")


@pytest.mark.asyncio
async def test_two_relays_would_break_the_guarantee(pgdb, pgconn):
    """单例锁是**承重**的，不是装饰。

    上面那个用例证明了「一个 relay 时不跳行」。这个用例证明反面：只要有第二个
    写入者，同一个游标就会永久跳过已提交行 —— 也就是 pg_try_advisory_lock 拦下
    的到底是什么。两条 relay 事务手工驱动，完全确定，不靠时序运气。
    """
    gen = pgdb.event_gen
    await _emit_direct(pgconn, f"{gen}:A", gen, "B0AAA")      # outbox id 小
    await _emit_direct(pgconn, f"{gen}:B", gen, "B0BBB")      # outbox id 大

    r1 = await pgdb._relay_open_conn()
    r2 = await pgdb._relay_open_conn()
    try:
        await r1.execute("BEGIN")
        rows1 = await r1.fetch(R.SQL_CLAIM, 1)                # 认领 A
        await r2.execute("BEGIN")
        rows2 = await r2.fetch(R.SQL_CLAIM, 1)                # 认领 B（SKIP LOCKED 跳过 A）
        assert rows1[0]["id"] < rows2[0]["id"]

        ev1 = [pgdb._build_event_row(r) for r in rows1]
        ev2 = [pgdb._build_event_row(r) for r in rows2]
        seq_a = (await pgdb._relay_write_events(r1, ev1))[0]["seq"]
        seq_b = (await pgdb._relay_write_events(r2, ev2))[0]["seq"]
        assert seq_a < seq_b                                  # A 先拿到号

        await r2.execute("COMMIT")                            # 却是 B 先提交
        seen = await pgconn.fetch(
            "SELECT seq, source_id FROM scraper.scrape_events WHERE seq > 0 ORDER BY seq")
        cursor = max(r["seq"] for r in seen)
        assert [r["source_id"] for r in seen] == [f"{gen}:B"]

        await r1.execute("COMMIT")                            # A 现在也提交了
        after = await pgconn.fetch(
            "SELECT seq, source_id FROM scraper.scrape_events WHERE seq > $1 ORDER BY seq",
            cursor)
        assert after == [], "两个写入者下居然没跳行？那这个用例失去意义了"
        assert await pgconn.fetchval(
            "SELECT count(*) FROM scraper.scrape_events WHERE source_id = $1",
            f"{gen}:A") == 1                                  # A 在库里，却永远拉不到
    finally:
        await r1.close()
        await r2.close()


# ------------------------------------------------------------------
# 5b) verify1 的 T-A / T-D / T-E，缩小规模移植进来
# ------------------------------------------------------------------
#
# 三条各自覆盖上面那个用例够不到的一条路径，而且每一条都在验证阶段真的抓到
# 或确认过东西。原始脚本（全规模）留在
# scratchpad/cursor_adversarial.py（T-A）/ cursor_churn.py（T-D）/ cursor_gaps.py（T-E）。
#
#   T-A  一个事务横跨几十个 relay tick —— 上面那个用例的事务寿命都在一个 tick 内。
#   T-D  游标**落盘并每次重读**（= 每次轮询都相当于消费者重启一次）+ relay 反复
#        起停。上面那个用例的游标只活在内存里，relay 也从不重启。
#   T-E  relay 在 INSERT **之后**、COMMIT 之前中止 —— 序列号已经烧掉，于是
#        scrape_events 里出现**内部空洞**。自然时序下抓不到（T-D 实测 0 个空洞），
#        必须强制注入。

@pytest.mark.asyncio
async def test_a_transaction_spanning_many_ticks_is_still_delivered(
        pgdb, pgconn, monkeypatch):
    """T-A —— 写侧 id 最小的那一行，在几十个 tick 之后提交，仍然会被投递。

    这是整个设计最直白的一句话：**写侧 id 1 会以 seq 121 出现**。裸游标下它是
    seq 1，而消费者的游标早就越过去了，那一行永远拿不到。

    两条腿必须同时立住：正向（outbox）投递成功，负向（同样交错、裸 bigserial）
    永久跳过。缺了负向，"没跳过"就是个真空断言。
    """
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    gen = pgdb.event_gen
    n_others = 120

    # ---------- 负向对照：同样的交错，裸表 ----------
    await pgconn.execute(
        "CREATE TABLE scraper.naive (seq bigserial PRIMARY KEY, sid text)")
    na = await asyncpg.connect(pgdb.dsn)
    nb = await asyncpg.connect(pgdb.dsn)
    try:
        await na.execute("BEGIN")
        naive_a = await na.fetchval(
            "INSERT INTO scraper.naive (sid) VALUES ('A') RETURNING seq")
        for i in range(n_others):
            await nb.execute("INSERT INTO scraper.naive (sid) VALUES ($1)", f"o{i}")
        naive_cursor = await pgconn.fetchval(
            "SELECT max(seq) FROM scraper.naive WHERE seq > 0")
        await na.execute("COMMIT")
        naive_after = await pgconn.fetch(
            "SELECT seq FROM scraper.naive WHERE seq > $1 ORDER BY seq", naive_cursor)
    finally:
        await na.close()
        await nb.close()
    assert naive_a == 1 and naive_cursor > naive_a
    assert naive_after == [], "负向对照没跳行，这个用例就失去意义了"

    # ---------- 正向：同样的交错，走 outbox ----------
    a = await asyncpg.connect(pgdb.dsn)
    b = await asyncpg.connect(pgdb.dsn)
    assert await pgdb.start_event_relay() is True
    try:
        await a.execute("BEGIN")
        id_a = await _emit_direct(a, f"{gen}:A", gen, "B0AAA")     # outbox id 最小
        ticks0 = pgdb.event_relay_metrics()["counters"]["ticks"]
        for i in range(n_others):
            await _emit_direct(b, f"{gen}:o{i}", gen, f"B0O{i:03d}")
            if i % 30 == 0:
                await asyncio.sleep(0.05)
        assert await _wait_for(
            lambda: pgdb.event_relay_metrics()["counters"]["relayed"] >= n_others)
        cursor = await pgconn.fetchval(
            "SELECT COALESCE(max(seq),0) FROM scraper.scrape_events")
        ticks = pgdb.event_relay_metrics()["counters"]["ticks"] - ticks0
        assert cursor >= n_others and ticks >= 5, (cursor, ticks)

        await a.execute("COMMIT")                                  # 几十个 tick 之后
        assert await _wait_for(lambda: _drained(pgconn))
        seq_a = await pgconn.fetchval(
            "SELECT seq FROM scraper.scrape_events WHERE source_id = $1", f"{gen}:A")
    finally:
        await pgdb.stop_event_relay()
        await a.close()
        await b.close()

    assert id_a == 1, "A 必须是 outbox 里 id 最小的那一行"
    assert seq_a is not None, "A 提交了却没进流"
    assert seq_a > cursor, (
        f"A 的 seq={seq_a} 没有落在消费者游标 {cursor} 之后 —— 它会被永久跳过")
    # 消费者从游标处继续拉，必须看到 A
    after = await pgconn.fetch(
        "SELECT source_id FROM scraper.scrape_events WHERE seq > $1 ORDER BY seq",
        cursor)
    assert f"{gen}:A" in [r["source_id"] for r in after]


@pytest.mark.asyncio
async def test_relay_restart_churn_with_a_persisted_consumer_cursor(
        pgdb, pgconn, monkeypatch, tmp_path):
    """T-D —— relay 反复起停 + 游标**落盘并每次重读**（= 每轮询一次就重启一次消费者）。

    relay 重启是唯一有可能打破「只有一个写入者、串行提交」的事件：如果一个正在
    死去的 relay 的事务还在飞，而新的已经起来了，``scrape_events`` 就短暂地有了
    两个写入者，游标保证会以 test_two_relays_would_break_the_guarantee 的方式失效。

    这里还带一个**实时跳行探测器**：每次轮询之后比对
    ``count(*) WHERE seq <= X`` 与已收集条数，跳行在发生的瞬间就被抓住，
    而不是等到最后才推断。单 relay 下它一次都不该响。
    """
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    gen = pgdb.event_gen
    cursor_file = tmp_path / "consumer_cursor.txt"
    n_writers, per_writer = 3, 30
    total = n_writers * per_writer

    stop = asyncio.Event()
    seen, seqs, skips, dupes, restarts = {}, [], [], [], [0]
    emitted = [0]

    # 重启的触发点按**写入进度**定，不按墙钟。原先混沌猴是固定 sleep(0.25)，
    # 而 writer 的总时长随机器快慢浮动：慢机器上跑 2 秒能重启 7 次，快机器上
    # 0.5 秒跑完只来得及重启 1 次，于是用例自带的空转自检 restarts>=2 直接失败。
    # 按进度定就跟机器速度无关了，而且三次重启保证都落在写入流中途。
    milestones = [total // 4, total // 2, (3 * total) // 4]

    async def _hold_for_restart():
        """写到里程碑就停在这儿，等混沌猴真的把这一次重启做完再继续。

        不 hold 的话快机器上 writer 可能整段跑完混沌猴才轮询到，三次重启会挤在
        写入结束**之后**发生 —— restarts 计数好看，但用例想验的「relay 在批次
        中途被打断」根本没发生，是另一种形式的真空。
        """
        for i, m in enumerate(milestones):
            if emitted[0] >= m and restarts[0] < i + 1:
                await _wait_for(lambda: restarts[0] >= i + 1, timeout=30.0)  # noqa: B023

    async def writer(w: int):
        conn = await asyncpg.connect(pgdb.dsn)
        rng = random.Random(900 + w)
        try:
            for n in range(per_writer):
                await _hold_for_restart()
                tx = conn.transaction()
                await tx.start()
                await _emit_direct(conn, f"{gen}:w{w}-{n}", gen, f"B0W{w}{n:03d}")
                await asyncio.sleep(rng.uniform(0.0, 0.02))     # 提交顺序被打乱
                await tx.commit()
                emitted[0] += 1
                await asyncio.sleep(rng.uniform(0.0, 0.005))
        finally:
            await conn.close()

    async def consumer():
        conn = await asyncpg.connect(pgdb.dsn)
        try:
            while not stop.is_set():
                x = int(cursor_file.read_text()) if cursor_file.exists() else 0
                rows = await conn.fetch(
                    "SELECT seq, source_id FROM scraper.scrape_events "
                    "WHERE seq > $1 ORDER BY seq LIMIT 100", x)
                for r in rows:
                    s, sid = int(r["seq"]), r["source_id"]
                    if sid in seen:
                        dupes.append(sid)
                    seen[sid] = s
                    seqs.append(s)
                if rows:
                    x = int(rows[-1]["seq"])
                    cursor_file.write_text(str(x))
                below = await conn.fetchval(
                    "SELECT count(*) FROM scraper.scrape_events WHERE seq <= $1", x)
                if int(below) != len(seen):
                    skips.append({"cursor": x, "committed_below": int(below),
                                  "collected": len(seen)})
                await asyncio.sleep(0.01)
        finally:
            await conn.close()

    async def chaos():
        for m in milestones:
            if not await _wait_for(lambda: emitted[0] >= m or stop.is_set(),  # noqa: B023
                                   timeout=60.0, step=0.005):
                return
            if stop.is_set():
                return
            await pgdb.stop_event_relay()          # 在批次中途取消
            await asyncio.sleep(0.02)
            await pgdb.start_event_relay()
            # 计数放在重启**完成之后**：_hold_for_restart 等的是"这一次做完了"，
            # 提前自增会让 writer 在 relay 还没起来时就放行。
            restarts[0] += 1

    assert await pgdb.start_event_relay() is True
    cons = asyncio.create_task(consumer())
    monkey = asyncio.create_task(chaos())
    try:
        await asyncio.gather(*(writer(w) for w in range(n_writers)))
        if pgdb._ev()["relay_state"] != "running":
            await pgdb.start_event_relay()
        await _wait_for(lambda: _drained(pgconn), timeout=30.0)
        await _wait_for(lambda: len(seen) == total, timeout=10.0)
    finally:
        stop.set()
        for coro in (cons, monkey, pgdb.stop_event_relay()):
            try:
                await coro
            except Exception:  # noqa: BLE001, PERF203
                pass

    # 里程碑驱动之后重启次数是确定的，所以这里可以要求相等而不是 >=2：
    # 少一次说明混沌猴提前退出了（用例真空），多一次说明触发条件被改坏了。
    assert restarts[0] == len(milestones), \
        f"混沌猴重启了 {restarts[0]} 次，预期 {len(milestones)} 次 —— 用例可能是真空的"
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox_dead") == 0
    assert await pgconn.fetchval(
        "SELECT count(*) FROM scraper.scrape_events") == total
    assert await pgconn.fetchval(
        "SELECT count(DISTINCT source_id) FROM scraper.scrape_events") == total
    assert not skips, f"实时探测到跳行：{skips[:3]}"
    assert not dupes, f"重复投递：{dupes[:5]}"
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert len(seen) == total, f"漏投 {total - len(seen)} 条"


@pytest.mark.asyncio
async def test_interior_seq_gaps_do_not_stall_or_skip_the_cursor(
        pgdb, pgconn, monkeypatch):
    """T-E —— relay 在 INSERT **之后**中止，序列号被烧掉，流里出现内部空洞。

    ``_relay_drain_once`` 的注释说"seq 有空洞是设计的一部分"，但自然时序下这条
    路径根本走不到（T-D 全规模跑下来 0 个空洞），所以它一直没被测过。这里强制
    注入：每第 3 批在 INSERT 返回之后抛异常，于是烧掉的号落在**已投递的行之间**，
    而不是开头的一段偏移。

    要证明的是：空洞是真的、是内部的、对游标无害 —— 消费者在 X=k 处不会因为
    k+1..k+m 不存在而停住或跳过。
    """
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    monkeypatch.setattr(R, "RELAY_ERROR_BACKOFF", 0.02)
    monkeypatch.setattr(R, "RELAY_BATCH", 8)
    pgdb._ev()["batch"] = 8
    gen = pgdb.event_gen
    n = 60

    real = R.EventStreamMixin._relay_write_events
    calls, aborts = [0], [0]

    async def flaky(self, conn, rows):
        res = await real(self, conn, rows)        # 号已经烧掉了，再炸
        calls[0] += 1
        if calls[0] % 3 == 0 and aborts[0] < 6:
            aborts[0] += 1
            raise RuntimeError("injected relay abort after INSERT")
        return res

    monkeypatch.setattr(R.EventStreamMixin, "_relay_write_events", flaky)
    assert await pgdb.start_event_relay() is True
    try:
        for i in range(n):
            await _emit_direct(pgconn, f"{gen}:g-{i}", gen, f"B0G{i:03d}")
            if i % 10 == 0:
                await asyncio.sleep(0.03)
        assert await _wait_for(lambda: _drained(pgconn), timeout=30.0)
    finally:
        await pgdb.stop_event_relay()

    # 契约消费者，从零开始拉一遍
    x, seen, seqs = 0, set(), []
    while True:
        rows = await pgconn.fetch(
            "SELECT seq, source_id FROM scraper.scrape_events "
            "WHERE seq > $1 ORDER BY seq LIMIT 50", x)
        if not rows:
            break
        for r in rows:
            assert r["source_id"] not in seen, f"重复投递 {r['source_id']}"
            seen.add(r["source_id"])
            seqs.append(int(r["seq"]))
        x = seqs[-1]

    gaps = [(seqs[i], seqs[i + 1]) for i in range(len(seqs) - 1)
            if seqs[i + 1] != seqs[i] + 1]
    assert aborts[0] >= 2, "没有真的注入中止，用例是真空的"
    assert gaps, f"没有观察到 seq 空洞（aborts={aborts[0]}），用例是真空的"
    assert gaps[0][0] > seqs[0], "空洞必须在内部，不是开头的一段偏移"
    assert len(seen) == n, f"漏投 {n - len(seen)} 条（空洞把游标卡住了？）"
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox_dead") == 0
    assert await pgconn.fetchval(
        "SELECT count(*) FROM scraper.scrape_events") == n


# ==================================================================
# 6) relay 崩在事务中途（T4 / T7）
# ==================================================================

@pytest.mark.asyncio
async def test_relay_failure_mid_transaction_loses_nothing(pgdb, pgconn):
    gen = pgdb.event_gen
    for i in range(5):
        await _emit_direct(pgconn, f"{gen}:crash-{i}", gen, f"B0CRASH{i}")

    async def boom(self, conn, rows):
        raise RuntimeError("boom inside the relay transaction")

    rc = await pgdb._relay_open_conn()
    try:
        orig = R.EventStreamMixin._relay_write_events
        R.EventStreamMixin._relay_write_events = boom
        try:
            with pytest.raises(RuntimeError):
                await pgdb._relay_drain_once(rc, 500)
        finally:
            R.EventStreamMixin._relay_write_events = orig

        # DELETE 与 INSERT 同事务 -> 一起回滚 -> 认领的 5 行原样留在 outbox
        assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 5
        assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_events") == 0
        # 连接没有被留在事务里
        assert not rc.is_in_transaction()

        # 恢复之后一次抽干，恰好 5 条、零重复
        assert await pgdb._relay_drain_once(rc, 500) == 5
    finally:
        await rc.close()

    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_events") == 5
    assert await pgconn.fetchval(
        "SELECT count(DISTINCT source_id) FROM scraper.scrape_events") == 5
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0


@pytest.mark.asyncio
async def test_relay_cancelled_mid_transaction_then_restarted(pgdb, pgconn, monkeypatch):
    """真·中途杀死：取消正卡在事务里的 relay 任务，再拉一个新的起来。"""
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.02)
    gen = pgdb.event_gen
    for i in range(7):
        await _emit_direct(pgconn, f"{gen}:kill-{i}", gen, f"B0KILL{i}")

    entered = asyncio.Event()

    async def hang(self, conn, rows):
        entered.set()
        await asyncio.sleep(30)               # 抱着事务不放，等着被取消

    orig = R.EventStreamMixin._relay_write_events
    R.EventStreamMixin._relay_write_events = hang
    try:
        assert await pgdb.start_event_relay() is True
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        await pgdb.stop_event_relay()          # 在事务中途取消
    finally:
        R.EventStreamMixin._relay_write_events = orig

    # 零丢失：7 行全部还在 outbox，事件表还是空的
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 7
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_events") == 0
    # 也没有留下 idle-in-transaction 的僵尸会话
    assert await pgconn.fetchval(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND state = 'idle in transaction'") == 0

    assert await pgdb.start_event_relay() is True
    try:
        for _ in range(200):
            if await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0:
                break
            await asyncio.sleep(0.02)
    finally:
        await pgdb.stop_event_relay()

    rows = await pgconn.fetch("SELECT seq, source_id FROM scraper.scrape_events ORDER BY seq")
    assert len(rows) == 7                                   # 零丢失
    assert len({r["source_id"] for r in rows}) == 7         # 零重复
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)


@pytest.mark.asyncio
async def test_duplicate_source_id_is_swallowed_not_raised(pgdb, pgconn):
    """幂等锚点：同一条记录被写第二遍时，无目标 ON CONFLICT DO NOTHING 吃掉它。

    ``ON CONFLICT (source_id)`` 推断不出来（唯一索引在分区上），会直接
    InvalidColumnReferenceError —— 所以只能用无目标形式。
    """
    gen = pgdb.event_gen
    await _emit_direct(pgconn, f"{gen}:dup", gen, "B0DUP")
    rc = await pgdb._relay_open_conn()
    try:
        assert await pgdb._relay_drain_once(rc, 500) == 1
        await _emit_direct(pgconn, f"{gen}:dup", gen, "B0DUP")      # 同一个 source_id
        assert await pgdb._relay_drain_once(rc, 500) == 1           # 认领了 1 行
    finally:
        await rc.close()
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_events") == 1
    assert pgdb.event_relay_metrics()["counters"]["conflicts"] == 1

    with pytest.raises(asyncpg.InvalidColumnReferenceError):
        await pgconn.execute(
            "INSERT INTO scraper.scrape_events "
            "(source_id,gen,asin,marketplace,zip_requested,zip_verify,"
            " collected_at,recorded_at,outcome,payload) "
            "VALUES ('x','g','a','amazon.com','1','unverified',now(),now(),'ok','{}') "
            "ON CONFLICT (source_id) DO NOTHING")


# ==================================================================
# 7) 毒丸不许把整条流焊死
# ==================================================================

@pytest.mark.asyncio
async def test_a_poison_row_is_quarantined_and_the_stream_resumes(pgdb, pgconn,
                                                                  monkeypatch):
    monkeypatch.setattr(R, "RELAY_TICK_SECONDS", 0.01)
    monkeypatch.setattr(R, "RELAY_ERROR_BACKOFF", 0.01)
    monkeypatch.setattr(R, "RELAY_QUARANTINE_AFTER", 2)

    gen = pgdb.event_gen
    poison_id = await _emit_direct(pgconn, f"{gen}:poison", gen, "B0POISON")
    good = [await _emit_direct(pgconn, f"{gen}:good-{i}", gen, f"B0GOOD{i}")
            for i in range(3)]
    assert poison_id < min(good)                 # 毒丸排在队头

    orig = R.EventStreamMixin._build_event_row

    def build(self, claimed):
        row = orig(self, claimed)
        if row["source_id"].endswith(":poison"):
            raise ValueError("deliberately unbuildable row")
        return row

    R.EventStreamMixin._build_event_row = build
    try:
        assert await pgdb.start_event_relay() is True
        for _ in range(600):
            if await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0:
                break
            await asyncio.sleep(0.02)
        await pgdb.stop_event_relay()
    finally:
        R.EventStreamMixin._build_event_row = orig

    # 毒丸被原封不动搬进死信表（不是丢弃），三条好行照常进流
    dead = await pgconn.fetch("SELECT id, body FROM scraper.scrape_outbox_dead")
    assert [r["id"] for r in dead] == [poison_id]
    assert json.loads(dead[0]["body"])["source_id"] == f"{gen}:poison"
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_events") == 3
    assert await pgconn.fetchval("SELECT count(*) FROM scraper.scrape_outbox") == 0
    assert pgdb.event_relay_metrics()["counters"]["quarantined"] == 1


# ==================================================================
# 8) 指标
# ==================================================================

@pytest.mark.asyncio
async def test_metrics_expose_depth_lag_and_rate(pgdb, pgconn):
    gen = pgdb.event_gen
    old = datetime.now(timezone.utc) - timedelta(seconds=30)
    for i in range(4):
        await _emit_direct(pgconn, f"{gen}:m-{i}", gen, f"B0M{i}")
    await pgconn.execute("UPDATE scraper.scrape_outbox SET enqueued_at = $1", old)

    stats = await pgdb.event_stream_stats()
    assert stats["outbox_depth"] == 4
    assert stats["relay_lag_s"] >= 29
    assert stats["max_seq"] == 0 and stats["min_available_seq"] == 0
    assert stats["future_partitions"] >= R.EVENT_FUTURE_PARTITIONS
    assert stats["contract_version"] == R.EVENT_CONTRACT_VERSION
    assert stats["sync_meta"]["gen"] == gen
    assert stats["dead_letters"] == 0

    rc = await pgdb._relay_open_conn()
    try:
        await pgdb._relay_drain_once(rc, 500)
    finally:
        await rc.close()

    m = pgdb.event_relay_metrics()
    assert m["counters"]["relayed"] == 4
    assert m["events_per_minute"] > 0
    assert m["hash_backend"] in (None, "common.slowhash")

    stats = await pgdb.event_stream_stats()
    assert stats["outbox_depth"] == 0
    assert stats["max_seq"] == 4 and stats["min_available_seq"] == 1
    assert stats["sync_meta"]["max_seq_ever"] == "4"
