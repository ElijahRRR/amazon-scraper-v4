"""common/pgdb/results_write.py —— 结果写入路径（系统里最热、也最要命的一段）。

OWNS（6 个方法）:
    _save_result_inner_unlocked  database.py:1816   ← 私有但承重
    accept_success_result        database.py:1655
    accept_results_batch         database.py:1702
    accept_failed_result         database.py:1811   ← 委托 self.fail_task
    save_result                  database.py:1987
    save_results_batch           database.py:2084   ← 当前无外部调用方，仍须存在

依赖别人:
    tasks.py   -> fail_task(...)
    media.py   -> _get_done_screenshot_path(asin, batch_id)

--------------------------------------------------------------------------
一、lease 门：逐字移植，不要"加固"
--------------------------------------------------------------------------
    UPDATE tasks SET status='done', updated_at=?
     WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'
    -> rowcount == 0 即 stale

这是全系统安全性最高的一条谓词（README 记录：把它删掉会产生 45 处基线差异，
夹具专门留了一个仍处于 processing 的探针任务做双向断言）。

* rowcount 必须是 **int**。asyncpg 的 execute 返回 ``'UPDATE 0'`` 这种字符串，
  ``'UPDATE 0' == 0`` 为 False，会静默放行所有过期结果。
  ConnProxy 已经帮你把它解析成 int 了；如果你绕开代理直接用 asyncpg，
  必须自己过 ``pool.rowcount_from_tag``。
* **不要**给它加 ``SELECT ... FOR UPDATE``。PG 在 READ COMMITTED 下会阻塞、
  然后用 EvalPlanQual 对新版本重算自己的 WHERE——结果与 SQLite 一致。
* 紧随其后的 server_reject 降级 UPDATE（database.py:1682/1751）**没有** lease 谓词。
  那是对的：上面的 CAS 已经在同一事务里证明了归属。必须留在同一个事务里。

--------------------------------------------------------------------------
二、accept_results_batch：返回值恰好 3 个 int 键
--------------------------------------------------------------------------
    {"accepted": int, "stale": int, "failed": int}
app.py:1580 会 ``return {**result, "total": len(results)}`` —— 多一个键就多泄
一个字段进 HTTP 响应体。

* 五处 ``record_stage(...)`` 必须原地保留，stage 名一字不改：
  ``update_tasks_lease`` / ``save_result`` / ``commit`` / ``total_in_lock``。
  基线 step 56（/api/_debug/lock-stats）钉死了 stage_timings 的这四个 key，
  而 ``_summary`` 对空样本返回的是形状不同的 ``{"count": 0}``。
* 外层必须是 ``async with self._write_lock("accept_results_batch"):``
  —— caller 名同样被基线钉死（waits/holds 的三个 key 之一）。
* 失败分支的 SELECT retry_count 之后的 UPDATE 只有 ``WHERE id=?``，
  既没有 lease 谓词也没有 rowcount 检查——**是 bug，照抄**，只给那条 SELECT
  加 ``FOR UPDATE`` 把读写窗口关上（SQLite 靠全局锁免费拿到的）。
  在源码里留个注释指向 .agent/catalog_sync_audit.md:167。
* item 循环顺序**不要**排序。谁最后写进 asin_data 由循环顺序决定，
  按 task_id 排序会改语义。

--------------------------------------------------------------------------
三、_save_result_inner_unlocked：SELECT → 分支 INSERT / UPDATE
--------------------------------------------------------------------------
两个分支不可互换：INSERT 分支会用当前值播种全部 6 个 baseline_* 列并写一条
change_type='new' 的 asin_changes；UPDATE 分支做变动检测、且只在 is_auto 时
才动 baseline_*。所以**不许**改写成 ``ON CONFLICT DO UPDATE``。

* 保留 ``if not asin: return False`` 的前置早退（database.py:1822）。
* 类型强转（**必做**）：40 多个 TEXT 列的值直接来自 ``await request.json()``。
  SQLite 有 TEXT affinity 会静默转换，asyncpg 直接 DataError。
  每一个绑到 TEXT 列的值都要过 ``common.pgdb.pool.text_affinity``：
  True->'1'、0.1+0.2->'0.3'、1e21->'1.0e+21'。裸 ``str()`` 是**错的**
  （会得到 'True' / '0.30000000000000004' / '1e+21'）。
  作用点：database.py:1909-1912、1921-1932、1948-1951、1963-1967。
  黄金夹具抓不到这一类（自带 worker 和夹具都全 str 化了）。
* 动态列拼接：本文件是唯一会生成"每种非 None 字段组合一份 SQL 文本"的地方。
  pool 已把 statement_cache_size 设成 0，所以不会有预备语句抖动，
  可以放心逐字移植 ``f"{col} = ?"`` 的写法（``?`` 由 translate_sql 统一编号，
  join_params + where_params 的顺序陷阱不存在）。
* 单写连接 + 真写锁的前提下**不需要** ``pg_advisory_xact_lock(hashtext(asin))``。
  多进程部署时需要；已记进 OWNERSHIP.md 的 Phase 1.5 清单。
* 内容 hash 与标题 hash 一律用 _shared 里再导出的函数，禁止本地复制字段表。

--------------------------------------------------------------------------
四、本次移植中与 SQLite 版**逐字一致**的取舍清单（实现者自查用）
--------------------------------------------------------------------------
* ``data["content_hash"] / data["title_bullets_hash"]`` 仍然**原地写回入参 dict**
  （database.py:1827-1828）。accept_results_batch 的调用方会看到被改过的 dict，
  这是现状，照抄。
  **P4-3 的例外**：判定为 not_found 时这两个键**既不算也不写**（连键都不留）。
  仓库里没有任何调用方在写库之后读它们（全仓 grep：只有 `_shared` 的再导出、
  pool.py 的一句注释、以及本文件自己），所以「原地写回」这条约定的观察者只剩
  `_save_result_inner_unlocked` 自己和事件流 payload —— 后者留空正是想要的语义
  （relay 对 outcome != 'ok' 的两个哈希本来也一律写 NULL）。
* ``has_baseline = bl_price is not None`` —— 只看 baseline_price 一列。
  baseline_price 为 NULL 时，即使其余五个 baseline 列都有值也**不做**变动检测。
  这是现状，照抄。
* 变动检测比的是 **baseline_\\* 而非当前值**；``bl_tb_hash and new_tb_hash`` 的
  空串短路也照抄（空 hash 不触发 title_bullets 变动）。
* UPDATE 分支写 asin_changes 时 ``batch_id`` 可能是 NULL；
  INSERT 分支的 'new'/'first_seen' 记录**只在 batch_id 为真时**才写。
* ``val is not None`` 才进动态列 —— 空串 ``""`` 会被写进去，不是跳过。

--------------------------------------------------------------------------
五、Phase 2 事件流写钩子（本文件里所有 ``emit_*`` 调用）
--------------------------------------------------------------------------
实现见 ``common/pgdb/outbox.py``（模块级函数，不是 mixin —— 私有方法重名在 MRO
下会被静默遮蔽，而 relay 由另一个人写）。SQLite 后端上本文件根本不在调用链里，
所以"no-op"是结构性的，不是运行期守卫。

不变式（规格 §2.5）：**每一次终态尝试恰好一行 outbox，且与它所描述的状态变更在
同一个事务里**（stale 除外，见下）。重新入队的分支一行都不发。

  路径          终态                       outcome              发射点
  S1  租约不匹配                          stale                ROLLBACK 之后，独立事务
  S2  server_reject                        parse_failed         COMMIT 之前，同一事务
  S3  写入成功                             ok / not_found       §1.1 钩子（inner 函数内）
  S4  写入返回 False（空 asin）             parse_failed         调用点，同一事务
  B1  无 task_id 直写                      ok / not_found       §1.1 钩子
  B3  批量·租约不匹配                      stale                内联，批事务
  B4  批量·server_reject                   parse_failed         内联，批事务
  B5  批量·写入成功                        ok / not_found       §1.1 钩子
  B6  批量·写入返回 False                   parse_failed         内联，批事务
  B7  批量·失败项找不到行                   stale                内联，批事务
  B8  批量·失败项达上限                     由 error_type 映射    内联，批事务
  B9  批量·失败项重新入队                   —— 不发
  save_result 的解析失败早退                —— 不发（无任务、未写库；与 B1 不对称，
                                              是从 SQLite 忠实继承的现状）

三条不许碰的红线：
  * 租约 UPDATE（:140 / :219）**绝不加 RETURNING**。加了之后 ConnProxy 走
    ``returns_rows`` 分支返回 ``rowcount=-1``，``-1 == 0`` 为 False，
    租约门会放行每一条过期结果。要 ``attempt`` 就另发一条 SELECT
    （``outbox.task_facts``，走 PK 索引，事务持有者自己的读不改道）。
  * **不加** ``record_stage("save_record", ...)``，也不加新的 ``_write_lock(name)``：
    黄金 step 56 钉死了 stage_timings 的 4 个 key 与 waits/holds 的 3 个 caller key。
  * body 一律由**提交上来的 data** 构造，绝不回读 ``asin_data``——那一行是跨时间
    合并出来的，事件必须是"一次完整采集"。
"""
from __future__ import annotations

import logging
import time
from common import config
from common.core.timeutil import now_ts
# F-012：站点注册表。按模块导入（而不是 `from ... import get`）——
# `get` 这个名字在本模块里太容易和 dict.get 混起来，读的人会看错。
from common.core import marketplace as _marketplace
from typing import List

from common.pgdb._shared import (  # noqa: F401
    ASIN_DATA_FIELDS,
    ASIN_DELETE_CHUNK,
    ASIN_DELETE_TABLES,
    _ASIN_DATA_COLUMN_SET,
    _compare_price,
    _compare_stock_qty,
    _compare_stock_status,
    _compute_content_hash,
    _compute_title_bullets_hash,
    _fail_cap,
    _is_parse_failure,
    _normalize_screenshot_path,
    record_stage,
)
from common.pgdb.outbox import (
    emit,
    emit_result_event,
    emit_stale_event,
    emit_stale_event_own_tx,
    result_context,
    scoped_context,
)
from common.pgdb.relay import outcome_for_error_type, payload_says_not_found
from common.pgdb.pool import text_affinity  # noqa: F401

logger = logging.getLogger(__name__)


# ==========================================================================
# P4-3：not_found 提交体不许碰目录层（服务端侧的那一半）
# ==========================================================================
# worker 侧（worker/engine.py 的 `_build_not_found_result`）已经**不提交**这些
# 字段了，靠的是写入循环的 `val = data.get(f); if val is not None:` —— 键不在
# dict 里就不进 SET 子句。那为什么服务端还要再拦一道？三个理由，每一个都实测过：
#
#  1. **老 worker 还在线。** worker 与 server 独立部署，灰度期一定存在
#     「新 server + 老 worker」的窗口，而老 worker 的 404 提交体是
#     `_default_result()` 全套 30/40 个 "N/A" + `title='[商品不存在]'`。
#     实测（scratchpad/p43_probe.py 的对照组）：那份 payload 把 title / brand /
#     category_tree / upc_list / image_urls / manufacturer 全部抹成占位符。
#     服务端这一道是**唯一**能覆盖那半个机队的防线。
#
#  2. **两个哈希是服务端自己算的，worker 拦不住。**
#     `_save_result_inner_unlocked` 无条件 `data["content_hash"] = ...`，
#     所以它们永远 `is not None`、永远进 SET 子句。实测（新 worker 的提交体，
#     目录列全都保住了的那一轮）：
#         content_hash        f80a6c44227f… -> f71511fd9a5f…
#         title_bullets_hash  8780e130b238… -> b99834bc19bb…
#     后者立刻产出一条**假的**变动记录：
#         title_bullets  title_or_bullets_changed  'title=Anker…' -> 'title='
#     ——正是契约 §6.5 要防的「占位符进/出触发复审」模式。
#
#  3. **is_auto 批次上还会污染 baseline。** 同一次实测：
#         baseline_price 29.99 -> N/A   baseline_stock_count 12 -> 0
#         baseline_title_bullets_hash 8780e130… -> b99834bc…（空值的哈希）
#     baseline 是变动检测的基准，被 404 写坏之后，**下一次成功采集**还会再产出
#     一条假变动（占位符 -> 真值）。一次 404 = 两次误报。
#
# 字段集与 worker 侧同源：`common.slowhash.SLOW_HASH_FIELDS`（慢变/身份层的定义
# 真源）+ 三个同族目录字段，再加上服务端自己算的两个哈希列。
# 跨模块一致性由 tests/pgdb/test_phase4_fields.py 的对表用例看守。
#
# 快变字段（价格/库存/配送/BSR/评分/卖家）**照旧写占位值**：404 时它们确实不可得，
# 留着上一次的价格比写 N/A 危险得多。采集参数（crawl_time/zip_code/site/
# product_url）同理照写——它们描述的是**这一次采集**。
try:                                        # pragma: no cover - 导入期分支
    from common.slowhash import SLOW_HASH_FIELDS as _SLOW_FIELDS
except Exception:                           # noqa: BLE001
    # 单文件被裁掉时不该让写路径崩掉；退化成显式清单（与 §4.1 的字段集同值）。
    _SLOW_FIELDS = ()
    logger.error("common.slowhash 不可导入，P4-3 的目录层保护退化成显式清单")

NOT_FOUND_PRESERVED_FIELDS = frozenset(
    # image_ids 是 image_urls 归约出来的派生键（slowhash.extract_image_ids），
    # asin_data 里的列叫 image_urls。
    (set(_SLOW_FIELDS) - {"image_ids"}) | {
        "title", "brand", "product_type", "manufacturer", "model_number",
        "part_number", "country_of_origin", "is_customized", "long_description",
        "upc_list", "parent_asin", "package_dimensions", "package_weight",
        "item_dimensions", "item_weight", "first_available_date",
        "bullet_points", "category_tree", "root_category_id",
        "variant_attributes",
        "image_urls",
        "category_ids",      # 与 category_tree 同源（同一段面包屑的 href 与文本）
        "ean_list",          # 与 upc_list 同族的 listing 资产
        "variation_asins",   # 变体家族
        # 服务端自己算的两个派生列（见上面第 2 条）
        "content_hash", "title_bullets_hash",
    })


class ResultsWriteMixin:
    """只定义方法，绝不定义 __init__。"""

    # ==================== 结果操作（含变动检测）====================

    async def accept_success_result(self, task_id: int, worker_id: str, lease_epoch: int,
                                    data: dict, batch_id: int = None) -> dict:
        """原子事务：校验 lease → 写数据 → 标 done

        Returns: {"accepted": True, "saved": True} 正常
                 {"accepted": True, "saved": False, "server_reject": True} 解析失败
                 {"accepted": False, "stale": True} lease 不匹配
        """
        # asyncpg 不做隐式参数转换：这些值全部来自未经校验的 request.json()。
        # SQLite 靠列 affinity 把 '1' 当 1 用，PG 会直接 DataError。
        tid = self.as_int(task_id)
        wid = self.text_affinity(worker_id)
        epoch = self.as_int(lease_epoch)

        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # Step 1: 校验 lease（原子 gate）
                cursor = await self._db.execute(
                    "UPDATE tasks SET status='done', updated_at=? "
                    "WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                    (now_ts(),
                     tid, wid, epoch)
                )
                if cursor.rowcount == 0:
                    await self._db.execute("ROLLBACK")
                    # S1：stale 也是一次终态尝试，必须进流（规格 §2.3）。
                    # reclaim_dead_worker_tasks 对"只是慢"的 worker 同样 bump epoch，
                    # 于是这里丢掉的是一次**完整的、真实的**采集，不是重复提交。
                    # 上面那条 ROLLBACK 一个字没改，事件另开一个事务（仍在写锁内）。
                    await emit_stale_event_own_tx(
                        self, task_id=tid, worker_id=wid, lease_epoch=epoch,
                        data=data, batch_id=batch_id)
                    return {"accepted": False, "stale": True}

                # 事件流要的服务端事实（attempt / zip_requested / asin）。
                # 必须在租约门**之后**取：门没过就不该读，也不该发。
                ev = await result_context(self, task_id=tid, worker_id=wid,
                                          lease_epoch=epoch, data=data,
                                          batch_id=batch_id)

                # Step 2: 解析失败检测
                if _is_parse_failure(data):
                    asin = data.get("asin", "")
                    logger.warning(f"server_reject: {asin} 解析失败数据")
                    # 降级为 failed（不回 pending 重试，Worker 已重试过）
                    await self._db.execute(
                        "UPDATE tasks SET status='failed', error_type='server_reject', "
                        "error_detail='parse_failure_on_server' WHERE id=?",
                        (tid,)
                    )
                    # S2：与那条降级 UPDATE 同一个事务
                    await emit(
                        self, outcome="parse_failed",
                        asin=(asin.strip() if isinstance(asin, str) else asin),
                        data=data, batch_id=batch_id,
                        error_type="server_reject",
                        error_detail="parse_failure_on_server",
                        **(ev.as_kwargs() if ev is not None else {}))
                    await self._db.execute("COMMIT")
                    return {"accepted": True, "saved": False, "server_reject": True}

                # Step 3: 写入 asin_data（事务内，不拿锁不开新事务）
                # S3 的事件在 _save_result_inner_unlocked 内部发（规格 §1.1），
                # ctx 只能走实例属性传进去——那个方法的签名被
                # test_signatures_match_sqlite 逐字钉死。
                with scoped_context(self, ev):
                    saved = await self._save_result_inner_unlocked(data, batch_id)

                if not saved:
                    # S4：`saved is False` ⟺ payload 里没有 asin ⟺ §1.1 的钩子没发。
                    # 这是 _save_result_inner_unlocked 唯一返回 False 的分支，判定精确。
                    # 任务照样是 done（终态），所以必须有一条事件。
                    await emit(
                        self, outcome="parse_failed", asin="",
                        data=data, batch_id=batch_id,
                        error_detail="empty_asin",
                        **(ev.as_kwargs() if ev is not None else {}))

                await self._db.execute("COMMIT")
                return {"accepted": True, "saved": saved}
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def accept_results_batch(self, items: list) -> dict:
        """单事务批量处理结果（减少锁争用）

        items: [{"task_id", "worker_id", "lease_epoch", "data", "batch_id", "success"}, ...]
        Returns: {"accepted": int, "stale": int, "failed": int}
        """
        accepted = 0
        stale = 0
        failed = 0
        now = now_ts()

        async with self._write_lock("accept_results_batch"):
            # SQLite 版用 BEGIN IMMEDIATE 显式抢写锁；PG 没有这个概念，
            # 垫片会把它当普通 BEGIN（'BEGIN IMMEDIATE' 在 PG 里是语法错误）。
            _t_total = time.monotonic()
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                # ⚠ 循环顺序不排序：同一 ASIN 多条时"谁最后写进 asin_data"由这里决定。
                for item in items:
                    task_id = item.get("task_id")
                    worker_id = item.get("worker_id", "")
                    lease_epoch = item.get("lease_epoch", 0)
                    batch_id = item.get("batch_id")
                    data = item.get("data", {})
                    is_success = item.get("success", True)

                    # 真值判断用**原始值**（SQLite 版就是这样），只有绑参才强转
                    tid = self.as_int(task_id)
                    wid = self.text_affinity(worker_id)
                    epoch = self.as_int(lease_epoch)

                    if not task_id:
                        # 无 task_id 的直接写入
                        if is_success and data:
                            # B1：没有 tasks 行可读，zip_requested 只能退回 payload
                            # 里的 zip_code，并在 body 里标 zip_requested_source。
                            ev = await result_context(self, worker_id=wid, data=data)
                            _t = time.monotonic()
                            with scoped_context(self, ev):
                                saved = await self._save_result_inner_unlocked(data, batch_id)
                            record_stage("save_result", (time.monotonic() - _t) * 1000)
                            if saved:
                                accepted += 1
                        # B2（非成功 / 无 data）：什么都没发生，也就没有终态尝试，不发事件。
                        continue

                    if is_success:
                        # 校验 lease
                        _t = time.monotonic()
                        cursor = await self._db.execute(
                            "UPDATE tasks SET status='done', updated_at=? "
                            "WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                            (now, tid, wid, epoch)
                        )
                        record_stage("update_tasks_lease", (time.monotonic() - _t) * 1000)
                        if cursor.rowcount == 0:
                            stale += 1
                            # B3：这里只是 `continue`，批事务照常在末尾提交，
                            # 所以事件直接进本事务，不需要 S1 那个独立事务。
                            await emit_stale_event(
                                self, task_id=tid, worker_id=wid,
                                lease_epoch=epoch, data=data, batch_id=batch_id)
                            continue

                        ev = await result_context(self, task_id=tid, worker_id=wid,
                                                  lease_epoch=epoch, data=data,
                                                  batch_id=batch_id)

                        # 解析失败检测
                        if _is_parse_failure(data):
                            await self._db.execute(
                                "UPDATE tasks SET status='failed', error_type='server_reject', "
                                "error_detail='parse_failure_on_server' WHERE id=?",
                                (tid,)
                            )
                            failed += 1
                            # B4
                            _asin = data.get("asin", "")
                            await emit(
                                self, outcome="parse_failed",
                                asin=(_asin.strip() if isinstance(_asin, str) else _asin),
                                data=data, batch_id=batch_id,
                                error_type="server_reject",
                                error_detail="parse_failure_on_server",
                                **(ev.as_kwargs() if ev is not None else {}))
                            continue

                        # 写入 asin_data
                        _t = time.monotonic()
                        with scoped_context(self, ev):      # B5 的事件在 inner 里发
                            saved = await self._save_result_inner_unlocked(data, batch_id)
                        record_stage("save_result", (time.monotonic() - _t) * 1000)
                        if saved:
                            accepted += 1
                        else:
                            failed += 1
                            # B6：空 asin，钩子没发过，任务却已经是 done
                            await emit(
                                self, outcome="parse_failed", asin="",
                                data=data, batch_id=batch_id,
                                error_detail="empty_asin",
                                **(ev.as_kwargs() if ev is not None else {}))
                    else:
                        # 失败结果：校验 lease 后标记失败/重试
                        error_type = data.get("error_type", "")
                        error_detail = data.get("error_detail", "")
                        # FOR UPDATE：SQLite 靠全局写锁免费拿到的"读到写"原子性，
                        # PG 下要显式锁住这一行，否则并发 reclaim 会在 SELECT 与
                        # UPDATE 之间把任务重新入队。
                        # ⚠ 下面两条 UPDATE 只有 WHERE id=?（既无 lease 谓词也无
                        #   rowcount 检查），比 fail_task 更弱——**是已知 bug，照抄**。
                        #   见 .agent/catalog_sync_audit.md:167，修复排在后续阶段。
                        # ⚠ 多选出来的 4 列**只**给事件流用：``row[0]`` 仍然是
                        #   retry_count，下面一个字都没改。这是加列不是加 RETURNING
                        #   ——后者会让 rowcount 变成 -1 而毁掉租约门（见文件头 §五）。
                        async with self._db.execute(
                            "SELECT retry_count, asin, zip_code, batch_id AS task_batch_id, "
                            "COALESCE(auto_retry_count, 0) AS auto_retry_count "
                            "FROM tasks WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing' FOR UPDATE",
                            (tid, wid, epoch)
                        ) as c:
                            row = await c.fetchone()
                        if not row:
                            stale += 1
                            # B7：失败项也是一次终态尝试被丢弃，同样进流。
                            await emit_stale_event(
                                self, task_id=tid, worker_id=wid,
                                lease_epoch=epoch, data=data, batch_id=batch_id,
                                error_type=error_type, error_detail=error_detail)
                            continue
                        retry_count = row[0] + 1
                        # 按 error_type 决定该任务的失败上限
                        # （variant_offset 不重试，其他用 MAX_RETRIES）
                        cap = _fail_cap(error_type)
                        if retry_count >= cap:
                            await self._db.execute(
                                "UPDATE tasks SET status='failed', retry_count=?, error_type=?, error_detail=?, updated_at=? "
                                "WHERE id=?",
                                (retry_count, self.text_affinity(error_type),
                                 self.text_affinity(error_detail), now, tid)
                            )
                            failed += 1
                            # B8：终态失败。attempt 用**已经自增过**的值
                            # （= 这次是第几次尝试），asin/zip 来自上面那条
                            # SELECT ——失败 payload 里没有 asin
                            # （worker/engine.py:1647 只发 error_type/error_detail）。
                            # outcome 用 text_affinity 之后的值：error_type 来自
                            # 未经校验的 JSON，`(True or "").strip()` 会
                            # AttributeError。上一条 UPDATE 已经调用过同一个
                            # 函数，所以这里不会引入新的失败面。
                            await emit(
                                self,
                                outcome=outcome_for_error_type(
                                    self.text_affinity(error_type)),
                                asin=row["asin"], data=data, task_id=tid,
                                batch_id=(batch_id if batch_id is not None
                                          else row["task_batch_id"]),
                                worker_id=wid,
                                lease_epoch=epoch, attempt=retry_count,
                                auto_retry_count=row["auto_retry_count"],
                                zip_requested=(row["zip_code"] if row["zip_code"] is not None
                                               else data.get("zip_code")),
                                zip_requested_source=("task" if row["zip_code"] is not None
                                                      else "payload"),
                                error_type=error_type, error_detail=error_detail)
                        else:
                            await self._db.execute(
                                "UPDATE tasks SET status='pending', retry_count=?, error_type=?, error_detail=?, "
                                "worker_id=NULL, lease_epoch=lease_epoch+1, updated_at=? WHERE id=?",
                                (retry_count, self.text_affinity(error_type),
                                 self.text_affinity(error_detail), now, tid)
                            )
                            # 重新入队不计入 accepted；若后续需要单独统计可在此累加 requeued
                            # B9：重新入队**不是**终态尝试，不发事件。
                            # 这条任务将来还会再产生一次终态尝试（成功或达上限），
                            # 那一次才有事件。规格 §2.5 的"requeue 静默"用例守着它。

                _t = time.monotonic()
                await self._db.execute("COMMIT")
                record_stage("commit", (time.monotonic() - _t) * 1000)
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            record_stage("total_in_lock", (time.monotonic() - _t_total) * 1000)

        return {"accepted": accepted, "stale": stale, "failed": failed}

    async def accept_failed_result(self, task_id: int, worker_id: str, lease_epoch: int,
                                   error_type: str = "", error_detail: str = "") -> dict:
        """原子受理失败结果（校验 lease）"""
        return await self.fail_task(task_id, worker_id, lease_epoch, error_type, error_detail)

    async def _save_result_inner_unlocked(self, data: dict, batch_id: int = None) -> bool:
        """保存采集结果到 asin_data（调用方必须持有 _write_lock 并在事务内）

        变动检测基准：baseline 字段（上次定时采集的数据）
        baseline 更新：仅定时采集（is_auto=True）时更新
        """
        asin = data.get("asin", "").strip()
        if not asin:
            return False

        # F-012：站点是唯一键 (asin, marketplace) 的一部分 —— 它**决定写哪一行**，
        # 不只是行里的一个值。所以这里**故意不兜底**：站点不在注册表里就让
        # ValueError 往上抛，整条提交失败。
        #
        # 反面做法（静默回退成 amazon.com）会把一条加拿大站的数据写进美国站
        # 那一行、覆盖掉真的美国数据，而且不报错、值还长得像真的 ——
        # 那正是 F-012 要消灭的那类故障，不能在修它的过程中重新引入一个。
        #
        # 缺键 / None 走 `get()` 的默认分支落到 amazon.com，那是**兼容老 worker**：
        # 灰度期还在线的老 worker 根本不提交这个字段，而它们采的确实是美国站。
        marketplace = _marketplace.get(data.get("marketplace")).id
        data["marketplace"] = marketplace

        # 控制流仍用原始 batch_id（``if batch_id:`` 的真值语义与 SQLite 一致），
        # 只有绑到 bigint 列时才强转。
        bid = self.as_int(batch_id)

        now = now_ts()

        # P4-3：这一次提交是不是「商品不存在」。判据与事件流**完全同源**
        # （relay.payload_says_not_found：`_outcome == 'not_found'` 或哨兵标题），
        # 所以不可能出现「行按 404 处理、事件却标着 ok」这种两侧打架的记录。
        # 见文件头 NOT_FOUND_PRESERVED_FIELDS 上方的三条实测理由。
        not_found = payload_says_not_found(data)

        if not_found:
            # 两个哈希**不算也不写**。它们是慢变字段的派生量，而这次提交里
            # 一个慢变字段都没有：算出来的是「空记录的哈希」，写进去就等于把
            # 目录层的指纹抹掉，并在下一次成功采集时产出一条假变动。
            # 不写 key（而不是写 None）：下面两个循环的判据是 `is not None`，
            # 缺键与 None 在那里等价；缺键还能让事件流的 payload 如实反映
            # 「这次没有可哈希的内容」，与 relay 对 outcome != 'ok' 一律写
            # NULL 哈希的口径一致。
            data.pop("content_hash", None)
            data.pop("title_bullets_hash", None)
        else:
            data["content_hash"] = _compute_content_hash(data)
            data["title_bullets_hash"] = _compute_title_bullets_hash(data)

        # ================= Phase 2 事件流写钩子（规格 §1.1 的 H3）=================
        # 位置是承重的，三条理由各自独立：
        #   1. 它是本函数"只计算、还没碰过库"的最后一个点。**之后每一条语句都可能
        #      失败**，钩子必须与写入处在同一个失败域里（同一事务，一起提交或
        #      一起回滚）。
        #   2. 它在两处 hash 赋值**之后**，所以 body 带的 content_hash /
        #      title_bullets_hash 与这次落进 asin_data 的行完全一致。
        #   3. 它在 `if not asin: return False` 早退**之后**，所以没有 asin 的提交
        #      不从这里产生事件（由调用方按 S4 / B6 记 parse_failed）。
        # body 用**提交上来的 data**，绝不回读 asin_data：下面那两个
        # `if val is not None` 的循环不改 data，但它们会让**行**跨时间携带旧值
        # （实测只剩 rating / review_count / seller_id / seller_name 四个字段真的
        # 会结转，因为 content_hash / title_bullets_hash 在上面已经无条件赋过值），
        # 而事件必须是"一次完整采集"，不是一条跨时间合并出来的记录。
        # ⚠ 结论要写进 Phase 3 契约：lxml 回退路径与所有早退路径上，
        #   rating / review_count / seller_id / seller_name 在 payload 里是**缺席**
        #   （不是 null、更不是旧值）。缺席 ≠ null ≠ 旧值。
        await emit_result_event(self, data, batch_id)
        # =========================================================================

        # 查询批次是否为定时采集
        is_auto = False
        if batch_id:
            async with self._db.execute(
                "SELECT is_auto FROM batches WHERE id = ?", (bid,)
            ) as c:
                row = await c.fetchone()
                if row:
                    is_auto = bool(row[0])

        resolved_screenshot_path = await self._get_done_screenshot_path(asin, batch_id)

        # 只查变动检测和更新路径实际用到的列，减少 I/O 与反序列化开销（原先 SELECT *，40+ 列）
        async with self._db.execute(
            "SELECT screenshot_path, title, "
            "baseline_price, baseline_buybox_price, baseline_stock_count, "
            "baseline_stock_status, baseline_title_bullets_hash "
            "FROM asin_data WHERE asin = ? AND marketplace = ?", (asin, marketplace)
        ) as c:
            existing = await c.fetchone()

        if existing:
            existing_dict = dict(existing)
            existing_screenshot_path = _normalize_screenshot_path(
                existing_dict.get("screenshot_path")
            )
            changes = []

            # 变动检测：对比 baseline（上次定时采集），而非当前值
            bl_price = existing_dict.get("baseline_price")
            bl_buybox = existing_dict.get("baseline_buybox_price")
            bl_stock = existing_dict.get("baseline_stock_count")
            bl_status = existing_dict.get("baseline_stock_status")
            bl_tb_hash = existing_dict.get("baseline_title_bullets_hash")
            has_baseline = bl_price is not None  # baseline 存在才做变动检测

            # P4-3：404 上**不做**变动检测。一次 404 不是一次「价格/标题变了」的
            # 观测，是「这一页不存在了」——拿占位符去和 baseline 比，产出的是
            #     price_stock   stock_qty:down   'price=29.99…' -> 'price=N/A…'
            #     title_bullets title_or_bullets_changed  'title=Anker…' -> 'title='
            # 两条假变动（实测，见文件头第 2/3 条）。title_bullets 那条现在已经
            # 被"不算哈希"消掉了（空 hash 走 `bl_tb_hash and new_tb_hash` 短路），
            # 价格那条只能在这里拦。
            # ⚠ 读 `config.CHANGE_DETECTION_ENABLED` 用的是**属性访问**（不是
            #   `from ... import CHANGE_DETECTION_ENABLED`）—— 后者会在导入时把值
            #   定死，测试就没法 monkeypatch 它把检测打开。默认关闭之后，这个
            #   代码路径在生产上不再执行，唯一还在覆盖它的就是那几条把开关拨到
            #   True 的用例；写成 import 常量的话它们会静默失效，逻辑随后腐烂。
            if config.CHANGE_DETECTION_ENABLED and has_baseline and not not_found:
                # 1. 价格/库存变动（对比 baseline）
                price_change = _compare_price(bl_price, data.get("current_price"))
                buybox_change = _compare_price(bl_buybox, data.get("buybox_price"))
                stock_qty_change = _compare_stock_qty(bl_stock, data.get("stock_count"))
                stock_status_change = _compare_stock_status(bl_status, data.get("stock_status"))

                if any([price_change, buybox_change, stock_qty_change, stock_status_change]):
                    detail_parts = []
                    if price_change:
                        detail_parts.append(f"price:{price_change}")
                    if buybox_change:
                        detail_parts.append(f"buybox:{buybox_change}")
                    if stock_qty_change:
                        detail_parts.append(f"stock_qty:{stock_qty_change}")
                    if stock_status_change:
                        detail_parts.append(f"stock_status:{stock_status_change}")

                    prev_vals = f"price={bl_price}, buybox={bl_buybox}, stock={bl_stock}, status={bl_status}"
                    new_vals = f"price={data.get('current_price')}, buybox={data.get('buybox_price')}, stock={data.get('stock_count')}, status={data.get('stock_status')}"
                    changes.append(("price_stock", ", ".join(detail_parts), prev_vals, new_vals))

                # 2. 标题/五点描述变动（对比 baseline）
                new_tb_hash = data.get("title_bullets_hash", "")
                if bl_tb_hash and new_tb_hash and bl_tb_hash != new_tb_hash:
                    prev_title = (existing_dict.get("title") or "")[:100]
                    new_title = (data.get("title") or "")[:100]
                    changes.append(("title_bullets", "title_or_bullets_changed",
                                    f"title={prev_title}", f"title={new_title}"))

            # 写入变动记录
            for change_type, detail, prev_val, new_val in changes:
                await self._db.execute(
                    "INSERT INTO asin_changes (asin, batch_id, change_type, change_detail, prev_value, new_value) VALUES (?, ?, ?, ?, ?, ?)",
                    (asin, bid, change_type, detail, prev_val, new_val)
                )

            # 更新主表当前值
            update_fields = []
            update_values = []
            for f in ASIN_DATA_FIELDS:
                # marketplace 与 asin 一样是**定位键**，不是被更新的值：
                # 它在下面的 WHERE 里，写进 SET 是自我赋值（无害但误导），
                # 而真要改它等于把这一行搬到另一个站点去 —— 那不是 UPDATE
                # 该做的事，是一次数据迁移。
                if f in ("asin", "screenshot_path", "marketplace"):
                    continue
                # P4-3：404 一个目录层字段都不许写。新 worker 本来就不提交它们
                # （缺键 ⇒ 下面 `is not None` 跳过），这一道拦的是**老 worker**
                # 交上来的 30/40 个 "N/A"——灰度期它们还在线。
                if not_found and f in NOT_FOUND_PRESERVED_FIELDS:
                    continue
                val = data.get(f)
                if val is not None:
                    update_fields.append(f"{f} = ?")
                    # asin_data 的 50 个业务列全是 text；SQLite 的 TEXT affinity
                    # 会把 int/float/bool 静默转成字符串，asyncpg 不会。
                    update_values.append(text_affinity(val))

            if resolved_screenshot_path and resolved_screenshot_path != existing_screenshot_path:
                update_fields.append("screenshot_path = ?")
                update_values.append(text_affinity(resolved_screenshot_path))

            # 定时采集：同时更新 baseline
            # P4-3：404 除外。baseline 是变动检测的**基准**，让一次 404 把它写成
            # 占位符，等于给下一次成功采集预约一条假变动（占位符 -> 真值）。
            # 实测：baseline_price 29.99 -> N/A、baseline_title_bullets_hash
            # 变成空记录的哈希。基准必须来自一次真正看见了商品页的采集。
            if is_auto and not not_found:
                update_fields.append("baseline_price = ?")
                update_values.append(text_affinity(data.get("current_price")))
                update_fields.append("baseline_buybox_price = ?")
                update_values.append(text_affinity(data.get("buybox_price")))
                update_fields.append("baseline_stock_count = ?")
                update_values.append(text_affinity(data.get("stock_count")))
                update_fields.append("baseline_stock_status = ?")
                update_values.append(text_affinity(data.get("stock_status")))
                update_fields.append("baseline_title_bullets_hash = ?")
                update_values.append(text_affinity(data.get("title_bullets_hash")))
                update_fields.append("baseline_updated_at = ?")
                update_values.append(now)

            update_fields.append("updated_at = ?")
            update_values.append(now)
            update_values.append(asin)
            update_values.append(marketplace)

            await self._db.execute(
                f"UPDATE asin_data SET {', '.join(update_fields)} "
                f"WHERE asin = ? AND marketplace = ?",
                update_values
            )
        else:
            # 新 ASIN，插入
            insert_fields = ["asin"]
            insert_values = [asin]
            for f in ASIN_DATA_FIELDS:
                # 这里**不**跳过 marketplace（与 UPDATE 分支不同）：新行必须把
                # 站点写进去。它在上面被归一化过、必然非 None，所以下面那个
                # `if val is not None` 一定放行 —— 列是 NOT NULL，漏了会当场
                # NotNullViolation，不会静默落到默认值上。
                if f in ("asin", "screenshot_path"):
                    continue
                # P4-3：同 UPDATE 分支。这里没有旧值可保，但结果同样重要——
                # 目录列留 NULL（"没观测到"）而不是占位符（"观测到了是空"）。
                # 这两者在导出与哈希里是完全不同的两件事。
                if not_found and f in NOT_FOUND_PRESERVED_FIELDS:
                    continue
                val = data.get(f)
                if val is not None:
                    insert_fields.append(f)
                    insert_values.append(text_affinity(val))

            if resolved_screenshot_path:
                insert_fields.append("screenshot_path")
                insert_values.append(text_affinity(resolved_screenshot_path))

            # 首次入库：baseline = 当前值（无论手动还是定时）
            # P4-3：除非这一次就是 404。用占位符播种 baseline 会让**第一次成功
            # 采集**变成一条假变动（'N/A' -> '29.99'）；留 NULL 则 has_baseline
            # 为假，变动检测安静地等到第一次真正看见商品页的定时采集。
            if not not_found:
                insert_fields.extend([
                    "baseline_price", "baseline_buybox_price",
                    "baseline_stock_count", "baseline_stock_status",
                    "baseline_title_bullets_hash", "baseline_updated_at",
                ])
                insert_values.extend([
                    text_affinity(data.get("current_price")),
                    text_affinity(data.get("buybox_price")),
                    text_affinity(data.get("stock_count")),
                    text_affinity(data.get("stock_status")),
                    text_affinity(data.get("title_bullets_hash")), now,
                ])

            insert_fields.extend(["created_at", "updated_at"])
            insert_values.extend([now, now])

            placeholders = ",".join("?" * len(insert_values))
            await self._db.execute(
                f"INSERT INTO asin_data ({', '.join(insert_fields)}) VALUES ({placeholders})",
                insert_values
            )

            # 记录新增变动
            if batch_id:
                await self._db.execute(
                    "INSERT INTO asin_changes (asin, batch_id, change_type, change_detail) VALUES (?, ?, 'new', 'first_seen')",
                    (asin, bid)
                )

        return True

    async def save_result(self, data: dict, batch_id: int = None) -> bool:
        """兼容入口：直接保存结果（不走 lease 校验，用于测试/直接写入）"""
        asin = data.get("asin", "").strip()
        if not asin:
            return False
        if _is_parse_failure(data):
            # ⚠ 这条早退**不发事件**：没有任务、也没往库里写任何东西。
            #   注意与 B1 的不对称——accept_results_batch 的无 task_id 分支
            #   **不做** _is_parse_failure 检测，于是同样一份 payload 走 B1 会有
            #   事件、走这里没有。这是从 SQLite 版忠实继承的现状，写下来，别顺手修。
            logger.warning(f"解析失败数据跳过: {asin}")
            return False
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # 无 task_id 直写：zip_requested 只能取 payload 里的 zip_code，
                # body 里会带 zip_requested_source="payload" 标出来。
                ev = await result_context(self, worker_id=data.get("worker_id"),
                                          data=data)
                with scoped_context(self, ev):
                    result = await self._save_result_inner_unlocked(data, batch_id)
                await self._db.execute("COMMIT")
                return result
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def delete_asins(self, asins) -> List[str]:
        """按 ASIN 删除结果及其关联行，返回**待删除的截图物理文件路径**。

        ``DELETE /api/results`` 与 ``POST /api/results/delete-by-file``
        的库侧动作（Phase 3.8 批 (3) 把两处收成一处）。

        **文件不在这里删**：返回路径清单，由 handler 调
        ``_remove_screenshot_files``（同 ``delete_batches``）。

        入参顺序原样保留、**不排序**：两个后端必须发出同样的分块序列。
        分块大小 ``ASIN_DELETE_CHUNK`` 两侧共用 —— SQLite 的
        SQLITE_MAX_VARIABLE_NUMBER 默认 999，PG 没这个限制，但边界不同
        就没法拿一边的日志去查另一边。

        整个删除在**一个事务**里（所有分块），与收口前逐字一致：
        中途失败就整体回滚，不会留下删了一半的 ASIN。
        """
        asin_list = list(asins)
        if not asin_list:
            return []

        def _chunks():
            for i in range(0, len(asin_list), ASIN_DELETE_CHUNK):
                chunk = asin_list[i:i + ASIN_DELETE_CHUNK]
                yield chunk, ",".join("?" * len(chunk))

        # 先收集截图物理文件路径（只读连接，不占写锁）
        screenshot_files: List[str] = []
        for chunk, placeholders in _chunks():
            async with self.read() as rc, rc.execute(
                f"SELECT file_path FROM screenshots WHERE asin IN ({placeholders})"
                f" AND file_path IS NOT NULL", chunk
            ) as c:
                screenshot_files.extend(row["file_path"] for row in await c.fetchall())

        # 分批删除
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                for chunk, placeholders in _chunks():
                    for tbl in ASIN_DELETE_TABLES:
                        await self._db.execute(
                            f"DELETE FROM {tbl} WHERE asin IN ({placeholders})", chunk)
                await self._db.execute("COMMIT")
            except BaseException:
                try:
                    await self._db.execute("ROLLBACK")
                except BaseException:
                    pass
                raise
        return screenshot_files

    async def save_results_batch(self, results: List[dict], batch_id: int = None) -> int:
        """批量保存结果"""
        saved = 0
        for data in results:
            if await self.save_result(data, batch_id):
                saved += 1
        return saved
