"""黄金场景 **在 relay 真跑的情况下**重放一遍，然后直查 scrape_events。

为什么需要这一条，而不是靠 tests/pgdb/ 里那 74 个用例：

  * ``tests/golden/harness.py`` 的 ``_PATCHED_LOOPS`` 会把 relay no-op 掉
    （正确的选择——录制期不能有后台任务改状态），于是**常规的 64 步校验从来
    没有在 relay 开着的情况下跑过**。"开着 relay 会不会改 HTTP 响应"因此是一个
    没人验过的问题。这里把它验掉。
  * tests/pgdb 里的用例都是直接调 Database 方法，不经过 FastAPI。
    写钩子 → outbox → relay → scrape_events 这条整链只在这里被端到端走一遍。

跑法：``DB_BACKEND=postgres pytest tests/test_golden_with_relay.py``。
SQLite 下自动 skip —— 事件流是 PG 专属。
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from collections import Counter

from common.dbfactory import is_postgres


def _load_baseline():
    path = os.path.join(os.path.dirname(__file__), "golden", "samples",
                        "baseline.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["steps"]


class GoldenWithLiveRelayTest(unittest.TestCase):
    """黄金场景 + relay 全开。"""

    def setUp(self):
        if not is_postgres():
            self.skipTest("事件流是 PostgreSQL 专属（DB_BACKEND=postgres 才跑）")

    def test_relay_running_changes_no_response_and_fills_the_stream(self):
        import common.config as config
        from tests.golden import harness, scenario

        baseline = _load_baseline()

        # ``LOCK_STATS`` 是**进程级**累加器，真源在 common/core/lockmeter.py，
        # 两个后端共用同一个全局对象（D-3：黄金 step 56 钉死了它的 key 集合，
        # 所以 pgdb 不能拷一份）。
        # 基线是在全新进程里录的；本用例在完整 ``pytest tests/`` 里排在几百个用例
        # 之后，那些用例已经往里灌了样本，``/api/_debug/lock-stats`` 的分位数
        # 因此对不上——实测差异正是 ``['lock_stats']``。清一次等价于"全新进程"。
        # （``tests/golden/test_golden.py`` 不需要这一手纯粹是因为它排在最前面。）
        from common.core import LOCK_STATS
        for key in ("waits", "holds", "stage_timings"):
            LOCK_STATS[key].clear()
        LOCK_STATS["slow_holds"].clear()

        # relay 从 no-op 名单里摘掉 —— 这正是本用例与常规 64 步校验的唯一差别。
        saved_loops = harness._PATCHED_LOOPS
        harness._PATCHED_LOOPS = tuple(x for x in saved_loops
                                       if x != "_scrape_event_relay")
        self.assertNotIn("_scrape_event_relay", harness._PATCHED_LOOPS)
        try:
            with harness.isolated_server() as (client, ctx):
                rec = harness.Recorder(client, ctx.tmp_root, strict=False)
                scenario.run(rec)

                # ---- (1) relay 开着，64 步必须逐字节不变 ----
                self.assertEqual(len(rec.steps), len(baseline))
                diffs = [
                    got.get("name")
                    for got, want in zip(rec.steps, baseline)
                    if json.dumps(got, sort_keys=True, ensure_ascii=False)
                    != json.dumps(want, sort_keys=True, ensure_ascii=False)
                ]
                self.assertEqual(diffs, [], f"relay 开着改变了响应: {diffs}")

                # ---- (2) 账本：从场景响应现算，不写死 ----
                by_name = {s["name"]: s for s in rec.steps}
                n1 = by_name["submit_results_batch"]["response"]["body"]["accepted"]
                n2 = (by_name.get("submit_results_round2", {})
                      .get("response", {}).get("body", {}).get("accepted", 0))
                # submit_result_failed 是 error_type=TIMEOUT、cap=3 的第一次失败
                # -> 重新入队，**不是**终态尝试，一条事件都不该有。
                expected = Counter({"ok": n1 + 1 + n2, "stale": 1})

                stats = asyncio.run(self._drain(config.PG_DSN))

        finally:
            harness._PATCHED_LOOPS = saved_loops

        self.assertEqual(stats["outbox_left"], 0, "relay 没把 outbox 抽干")
        self.assertEqual(stats["dead"], 0, "有行被死信了")
        self.assertEqual(Counter(stats["hist"]), expected,
                         f"outcome 直方图与账本不符: {stats['hist']} != {dict(expected)}")

        # ---- (3) 幂等锚点与顺序 ----
        self.assertEqual(stats["dup_source"], 0, "source_id 有重复")
        self.assertEqual(stats["dup_attempt"], [],
                         f"同一个 (task_id, attempt) 出现多条终态事件: "
                         f"{stats['dup_attempt']}")
        self.assertEqual(stats["seqs"], sorted(stats["seqs"]))
        self.assertEqual(len(set(stats["seqs"])), len(stats["seqs"]))

        # ---- (4) 跨切面规则 9：outcome<>'ok' 两个哈希都必须是 NULL ----
        self.assertEqual(stats["hash_leak"], 0,
                         "有 outcome<>'ok' 的行带了 review_hash / slow_hash")

        # ---- (5) 事件是**独立**的追加日志：批次 B 连同它的 tasks 已经被
        #      scenario 最后一步删掉了，它们的事件必须还在。
        self.assertTrue(stats["events_for_deleted_tasks"] > 0,
                        "删批次把事件一起带走了——事件流不再是独立的追加日志")

        # ---- (6) zip_requested 来自 tasks.zip_code，不是 payload ----
        # 场景里 batch_a=10001 / batch_b=60601，而 lease 探针那条任务的
        # payload 里写的是 10001。
        self.assertIn("60601", {r["zip"] for r in stats["rows"]},
                      "没有一条事件带上 batch_b 的邮编，zip_requested 可能取错了源")

    @staticmethod
    async def _drain(dsn):
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            for _ in range(80):
                if await conn.fetchval(
                        "SELECT count(*) FROM scraper.scrape_outbox") == 0:
                    break
                await asyncio.sleep(0.25)
            rows = await conn.fetch(
                "SELECT seq, outcome, task_id, attempt, zip_requested AS zip, "
                "review_hash, slow_hash FROM scraper.scrape_events ORDER BY seq")
            return {
                "outbox_left": await conn.fetchval(
                    "SELECT count(*) FROM scraper.scrape_outbox"),
                "dead": await conn.fetchval(
                    "SELECT count(*) FROM scraper.scrape_outbox_dead"),
                "hist": dict(Counter(r["outcome"] for r in rows)),
                "seqs": [r["seq"] for r in rows],
                "rows": [{"zip": r["zip"]} for r in rows],
                "dup_source": await conn.fetchval(
                    "SELECT count(*) - count(DISTINCT source_id) "
                    "FROM scraper.scrape_events"),
                "dup_attempt": [
                    (r["task_id"], r["attempt"], r["n"]) for r in await conn.fetch(
                        "SELECT task_id, attempt, count(*) n "
                        "FROM scraper.scrape_events "
                        "WHERE task_id IS NOT NULL AND outcome <> 'stale' "
                        "GROUP BY task_id, attempt HAVING count(*) > 1")],
                "hash_leak": await conn.fetchval(
                    "SELECT count(*) FROM scraper.scrape_events "
                    "WHERE outcome <> 'ok' "
                    "AND (review_hash IS NOT NULL OR slow_hash IS NOT NULL)"),
                "events_for_deleted_tasks": await conn.fetchval(
                    "SELECT count(*) FROM scraper.scrape_events e "
                    "WHERE e.task_id IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM tasks t WHERE t.id = e.task_id)"),
            }
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
