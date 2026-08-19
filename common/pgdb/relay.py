"""common/pgdb/relay.py —— Phase 2 事件流：outbox 写钩子 + 单例 relay。

OWNS（都不在 ``PUBLIC_API`` 里 —— 那个元组是 SQLite 对等面的契约，不准动）:

    写侧（在**调用方的事务里**执行，results_write.py / tasks.py 调用）
        _emit_outbox(...)              -> Optional[int]   插一行 scrape_outbox
        classify_success_outcome(data) -> 'ok' | 'not_found' | 'parse_failed'
        outcome_for_error_type(et)     -> 'blocked' | 'parse_failed'

    引导
        init_event_stream()            建表 / 建分区 / 定 gen / 定 instance_id
        reset_event_stream()           测试用：清空四张表并把 seq 重置

    relay
        start_event_relay()  -> bool   拿单例锁 + 拉起后台任务；拿不到返回 False
        run_event_relay()              app.py lifespan 用：**带重试**地保证本进程
                                       一直在排队当 relay，直到被取消 / stop
        stop_event_relay()             取消 + 解锁 + 关连接
        ensure_event_partitions()      提前建未来分区（永远 >= 2 个）+ 修 B1 遗留

    指标
        event_relay_metrics()  -> dict  纯内存计数器（同步，不碰库）
        event_stream_stats()   -> dict  加上 outbox 深度 / 滞后 / max_seq（查库）

==========================================================================
一、为什么是 outbox + relay，而不是 ``seq > X`` 直接查
==========================================================================
PG 下 ``seq > X`` 轮询会**永久跳过已提交的行**，这不是边角情况而是并发写入的
默认行为：序列值在 INSERT 时分配、行在 COMMIT 时才可见，而并发事务的提交顺序
与分配顺序无关。游标「跨过去」就再也回不来；队列「认领」，未提交的行对认领者
根本不可见，下一轮自然被捞走。实测（scratchpad/relay_probe.py）：

    A got seq=1 (uncommitted), B got seq=2 (committed first)
    consumer poll #1 sees: [(2,'B')] -> cursor = 2
    consumer poll #2 (seq > 2) sees: []
    row A is now committed but PERMANENTLY SKIPPED: True

同一段交错走 outbox：

    outbox: A id=8 (uncommitted), B id=9 (committed first)
    relay tick 1 -> [(8,'g:B')]
    relay tick 2 -> [(9,'g:A')]        A recovered on a LATER seq than B: True

三环证明：(1) ``scrape_events`` 只有一个写入者 = relay，由
``pg_try_advisory_lock`` 保证；(2) relay 单任务单事务逐批串行提交；
(3) read committed 下消费者看不到未提交行。⇒ seq 顺序 == 提交顺序 == 可见顺序。

替代方案（xid8 水位 / 时间窗 / 串行化写入 / advisory lock 天花板 / 逻辑复制）
在计划书 §1.3 已逐条否决，**不要重开**。

==========================================================================
二、连接与事务纪律（D-15 / D-2，读 OWNERSHIP.md）
==========================================================================
* **写钩子** ``_emit_outbox`` 走 ``self._db``，因此**天然落在调用方的事务里**：
  它是 INSERT，不是 plain read，D-15 的改道逻辑不会碰它
  （``_is_plain_read('INSERT INTO scraper.scrape_outbox ...') = False``）。
  实测三张表的 xmin 相同 = 同一个事务的证书。
* **relay 自己开一条专用连接**，既不是 ``self._db``（那是 ``_write_lock``
  守着的唯一写连接，D-2），也不是 ``self.read()``（池连接 ``allow_tx=False``，
  pool.py:943，开不了事务）。两个理由：
    - ``pg_try_advisory_lock`` 是 **session 级**的，必须在 relay 的整个生命周期里
      按着同一条连接；借来还回去的池连接做不到。
    - relay 每秒开一个事务，占着 ``_write_lock`` 一秒钟就等于把整条写路径按住。
  连接预算：读池 max 10 + 写连接 1 + relay 1 = 12，远低于 max_connections。
  这是对 OWNERSHIP.md §4.3「不准自己 import asyncpg 建连接」的**有意豁免**，
  由设计规格 §4.2 明文要求；除 relay 外任何人都不准照抄。
* relay **绝不**碰 ``_write_lock``，绝不碰 asin_data / tasks / batches，
  也绝不新增 ``record_stage`` key 或 ``_write_lock(caller)`` 名字
  —— 黄金 step 56 把那两组 key 钉死了，多一个立刻 64/64 失败。
* 每个 ``BEGIN`` 块都 ``except BaseException: rollback; raise``（D-17）。
  取消也必须回滚：relay 是长期后台任务，被 cancel 是**正常**关停路径，
  留下一个 idle-in-transaction 会钉住 xmin horizon。

==========================================================================
三、gen：**不是每次启动新铸**
==========================================================================
计划书 §2.1 的说明里写着「gen 每次启动新铸」，而 §5.5 又把 gen 变化定义成消费侧
**硬停 + 全量对账**（``if st.gen != stored_gen: ALARM; full_reconcile(); STOP``），
T11(a) 则把「铸新 gen」描述成**检出回退之后的响应**。三者不能同时成立：
每次启动新铸 = 每次普通发版都触发一次全量对账。取 T11 的读法：

    on init:
        gen = sync_meta['gen']
        actual = COALESCE(max(seq) FROM scrape_events, 0)
        if gen is NULL:                          mint()          # 全新库
        elif actual < int(sync_meta['max_seq_ever'] or 0):       # 流倒退了
                                                 mint(); log.error(...)
                                                 setval(seq, max(actual, ever)+1)
    relay 每批在**同一个事务**里推进 sync_meta['max_seq_ever']

``gen`` **逐行落库**（``scrape_events.gen``）这一条无论如何都成立：只存 meta 表的话，
一次从备份恢复会把全部历史重贴上恢复后的标签。

``instance_id`` 由运维配置（env ``SCRAPER_INSTANCE_ID``），持久化进 sync_meta，
**永不自动铸造**——它是人用来区分两个克隆部署的（T12）。

整机快照回滚（T11b）服务端**检不出来**，这是设计如此：消费侧的 ``max_seq``
单调性检查是唯一防线，Phase 3 必须把 ``max_seq`` 暴露出去并写进契约。

==========================================================================
四、毒丸与停摆：两种失败，两种处置
==========================================================================
relay 事务失败 → 回滚 → DELETE 一起回滚 → 认领的行**全部留在 outbox**。
零丢失。但如果失败是**确定性**的（队头某一行的 body 畸形），下一 tick 会再
认领它、再失败，**整条事件流永久停摆**。所以分两类处理：

  * **不是行的错**（分区溢出 23514 "no partition"、连接断、超时、
    InFailedSQLTransaction）：只回滚 + 大声记日志 + 退避。这正是规格 §3.2 要的
    「吵闹的停摆」——分区溢出必须停下来等人建分区，绝不能把行扔掉。
  * **是行的错**（数据类错误，且批量缩到 1 行后仍连续失败 N 次）：把那一行
    原封不动搬进 ``scraper.scrape_outbox_dead`` 并继续。有审计、可重放、不静默。

批量在失败后**折半**（500 → 250 → … → 1），成功后立刻恢复，用来把毒丸从
一整批健康行里隔离出来。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncpg

from common import config
from common.core.error_types import BLOCKED, CAPTCHA
from common.core.zipcode import _zfill_short_numeric
from common.pgdb.pool import translate_sql
from common.pgdb.schema import (
    EVENT_COMPLETENESS_MAX,
    EVENT_COMPLETENESS_UNMEASURED,
    EVENT_CONTRACT_VERSION,
    EVENT_DEFAULT_MARKETPLACE,
    EVENT_EXPECTED_COLUMNS,
    EVENT_FOREIGN_RANGE_CHECKS_SQL,
    EVENT_FUTURE_PARTITIONS,
    EVENT_LIST_PARTITIONS_SQL,
    EVENT_MARKETPLACES,
    EVENT_META_COMPLETENESS,
    EVENT_META_OUTCOME,
    EVENT_META_PARSE_ENGINE,
    EVENT_META_ZIP_OBSERVED,
    EVENT_META_ZIP_REQUESTED,
    EVENT_META_ZIP_VERIFY,
    EVENT_OUTCOMES,
    EVENT_OWN_RANGE_CHECK_SQL,
    EVENT_BATCH_INDEX_SQL,
    EVENT_PARENT_INDEX_SQL,
    EVENT_PARSE_ENGINES,
    EVENT_PARTITION_PREFIX,
    EVENT_PARTITION_SPAN,
    EVENT_STREAM_DDL,
    EVENT_WORKER_OUTCOMES,
    EVENT_ZIP_VERIFY_DEFAULT,
    EVENT_ZIP_VERIFY_VALUES,
    event_create_first_partition_sql,
    event_next_partition_sql,
    event_partition_name,
)

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

#: relay 单例锁。advisory lock 是**按库**隔离的，所以一个常量就够：
#: 同一个库上的第二个 relay 拿不到锁并拒绝启动（T5），不同的临时测试库互不干扰。
#: 0x5343524150455631 = b'SCRAPEV1'，最高位是 0x53 < 0x80，落在 int8 正数区间。
RELAY_LOCK_KEY = int(os.environ.get("PG_RELAY_LOCK_KEY", "0x5343524150455631"), 0)

#: 轮询间隔（秒）。计划 §1.2：约 1s。
RELAY_TICK_SECONDS = float(os.environ.get("PG_RELAY_TICK_SECONDS", "1.0"))

#: 每批认领多少行。计划 §1.2：500。
RELAY_BATCH = int(os.environ.get("PG_RELAY_BATCH", "500"))

#: 事务出错后的退避（秒）。
RELAY_ERROR_BACKOFF = float(os.environ.get("PG_RELAY_ERROR_BACKOFF", "2.0"))

#: ``run_event_relay`` 两次尝试之间的间隔（秒）。见那个方法的注释（B2）：
#: 拿不到单例锁 / 启动期连库失败都必须**继续重试**，否则滚动重启之后可能一个
#: relay 都不剩。5s：滚动部署的接管延迟可以接受，也不会把日志刷爆。
RELAY_RETRY_SECONDS = float(os.environ.get("PG_RELAY_RETRY_SECONDS", "5.0"))

#: 连续多少个 tick 全部失败，就判定「这条连接大概率已经死了」，去重开一条并
#: 重抢单例锁（B3）。3 次 × RELAY_ERROR_BACKOFF ≈ 6s，短到不会让流白停，
#: 长到一次偶发的死锁/超时不会触发一轮重连。
RELAY_RECOVER_AFTER = int(os.environ.get("PG_RELAY_RECOVER_AFTER", "3"))

#: 停机时每一步的上限（秒）。见 stop_event_relay 的注释：它跑在 lifespan 的
#: db.close() 之前，无界等待 = 进程停不下来。
RELAY_STOP_TIMEOUT = float(os.environ.get("PG_RELAY_STOP_TIMEOUT", "10.0"))

#: 每隔多少 tick 检查一次分区余量（默认 60 tick ≈ 1 分钟）。
#: 20000000 的跨度下 10 万/天要 200 天才用掉一个分区，检查再密也没意义。
RELAY_PARTITION_EVERY = int(os.environ.get("PG_RELAY_PARTITION_EVERY", "60"))

#: 批量缩到 1 行后连续失败多少次，就把队头那行隔离进死信表。
RELAY_QUARANTINE_AFTER = int(os.environ.get("PG_RELAY_QUARANTINE_AFTER", "5"))

#: events/minute 的滑动窗口（秒）。
RELAY_RATE_WINDOW = 60.0

#: body 的结构版本。改形状时 +1，relay 侧按版本分支。
EVENT_BODY_VERSION = 1

#: worker 侧的哨兵标题。**全等匹配**，绝不能 startswith("[")
#: —— `[2-Pack] Storage Bins` 是真标题（计划 §4.2 / 审计 §2.11）。
NOT_FOUND_TITLE = "[商品不存在]"          # worker/engine.py:1172
_PARSE_FAIL_TITLES = frozenset({
    "[页面为空]", "[HTML解析失败]", "[验证码拦截]", "[API封锁]",
})

#: error_type -> outcome（设计规格 §2.2）。传输类失败一律折进 parse_failed：
#: outcome 是封闭集，原始 error_type 在自己的列里无损保留；加第六个值会打断
#: 每一个消费者的 ``outcomes=`` 过滤，那是契约版本升级，不是 Phase 2 的决定。
_OUTCOME_BY_ERROR_TYPE: Dict[str, str] = {
    BLOCKED: "blocked",
    CAPTCHA: "blocked",
}
_OUTCOME_DEFAULT = "parse_failed"

# --------------------------------------------------------------------------
# crawl_time：**两种线格式并存**（P4-7 的混合机队窗口）
# --------------------------------------------------------------------------
# 旧（Phase 4 之前的 worker）：``datetime.now(UTC+8).strftime('%Y-%m-%d %H:%M:%S')``
#     -> ``'2026-08-05 10:00:00'``，东八区墙上时间，``%z`` 被**刻意丢掉**。
# 新（P4-7 之后的 worker）    ：``datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')``
#     -> ``'2026-08-05T02:00:00Z'``，RFC3339 UTC。
#
# **两种必须同时收**，而且这不是防御性编程：worker 与 server 是分开部署的，
# 一次 worker 灰度发布期间，同一秒钟进 outbox 的两行就可能一新一旧。
# 只认新格式 ⇒ 老 worker 的每条记录都退化成 fallback（recorded_at）；
# 只认旧格式 ⇒ 新 worker 的 ``'…T02:00:00Z'`` 用 ``s[:19]`` + 旧 fmt 解析会
# **抛 ValueError**（'T' 对不上空格）从而也退到 fallback。两个方向都不会静默
# 错 8 小时——这正是 P4-7 选 ``T``+``Z`` 而不是 ``'+00:00'`` 的原因：
# ``'2026-08-05 02:00:00+00:00'[:19]`` 会**解析成功**然后被重新贴上 +08:00。
#
# 判形规则（``parse_crawl_time``）：
#   * 带偏移 / 带 Z            -> 照单全收，不做任何假设（tz_aware）
#   * 裸的、空格分隔           -> 老 worker，补 +08:00（legacy_cst）
#   * 裸的、``T`` 分隔          -> 新一代形状但丢了标记，按 UTC（naive_utc）
#   * 其它                     -> 兜底 recorded_at + 计数（unparsable / missing）
# 三种形态各有计数器，混合机队因此是**可观测**的，而不是靠猜。
_WORKER_LEGACY_TZ = timezone(timedelta(hours=8))
_CRAWL_TIME_LEGACY_FMT = "%Y-%m-%d %H:%M:%S"
_CRAWL_TIME_ISO_FMT = "%Y-%m-%dT%H:%M:%S"

#: ``parse_crawl_time`` 的判形结果。
CRAWL_TIME_TZ_AWARE = "tz_aware"
CRAWL_TIME_LEGACY_CST = "legacy_cst"
CRAWL_TIME_NAIVE_UTC = "naive_utc"
CRAWL_TIME_MISSING = "missing"
CRAWL_TIME_UNPARSABLE = "unparsable"

#: 判形 -> 计数器名。``tz_aware`` 是常态，不计数（计数器是给异常状况用的）。
_CRAWL_TIME_COUNTER = {
    CRAWL_TIME_LEGACY_CST: "collected_at_legacy_cst",
    CRAWL_TIME_NAIVE_UTC: "collected_at_naive_utc",
}

#: 严格模式：事件流没引导起来时 _emit_outbox 是抛异常还是记账跳过。
#: 默认**跳过**——事件流的 bug 不该把 worker 真实采集到的结果一起毁掉；
#: 计数器 + ERROR 日志保证它不会静默。
_EVENTSTREAM_STRICT = os.environ.get("PG_EVENTSTREAM_STRICT", "0") == "1"


# ============================================================
# 纯函数：可单测，不碰库
# ============================================================

def outcome_for_error_type(error_type: Optional[str]) -> str:
    """终态失败（B8 / F2）的 error_type -> outcome。设计规格 §2.2。"""
    return _OUTCOME_BY_ERROR_TYPE.get((error_type or "").strip(), _OUTCOME_DEFAULT)


def classify_success_outcome(data: Optional[dict]) -> str:
    """success=True 路径的 outcome（设计规格 §2.4）。

    **两个信号，缺一不可**（P4-3 之后）：

    1. ``_outcome``（``worker/engine.py`` 的 ``META_OUTCOME``）。新 worker 的
       404 提交体里**没有 title**——title 是慢变字段，P4-3 之后 404 分支一个
       慢变字段都不提交（提交就是覆盖）。于是哨兵标题这条唯一的旧信号消失了，
       只看标题会把每一个 404 判成 ``ok``，而 ``ok`` 是会算哈希、会被消费侧
       upsert 进 ``catalog.products`` 的那一档：实测好页 → 404 → 好页
       = 两次 review_hash 翻转 = 两次误复审，正是契约 §6.5 要防的模式。
    2. 哨兵标题。**老 worker 照旧只有它**，而且 worker 与 server 分开部署，
       灰度期两种提交体必然同时在线。掉队的那一半不能静默降级。

    顺序是「先标题、后 ``_outcome``」，因为标题哨兵是页面级证据（那个字符串
    只可能由 worker 在真的拿到 404/空页时写进去），而 ``_outcome`` 是一个
    自述字段。两者冲突时以更强的证据为准；``_outcome`` 只在标题不表态时说话。
    worker 侧的值域是 ``{'ok','not_found'}``（``EVENT_WORKER_OUTCOMES``）——
    一个 worker 声称自己 ``stale`` 是没有意义的（它不知道自己的租约还在不在），
    所以集外值一律忽略、回到 ``ok``，再由服务端自己的判定接管。

    另外四个哨兵在今天的 worker 里到不了 success 路径（它们各自设置
    last_error_type 然后 continue 重试循环，最终走 success=False 提交），
    这里照样认出来当 parse_failed —— 一次 dict 查找的代价，换将来 worker
    改动时的失败安全。
    """
    d = data if isinstance(data, dict) else {}
    title = d.get("title")
    if title == NOT_FOUND_TITLE:
        return "not_found"
    if title in _PARSE_FAIL_TITLES:
        return "parse_failed"
    declared = d.get(EVENT_META_OUTCOME)
    if isinstance(declared, str) and declared.strip() in EVENT_WORKER_OUTCOMES:
        return declared.strip()
    return "ok"


def payload_says_not_found(result: Optional[dict]) -> bool:
    """payload 自身是否声明「商品不存在」。``_build_event_row`` 用它兜底。

    为什么 relay 侧还要再判一次（``_emit_outbox`` 明明已经用
    ``classify_success_outcome`` 定过 body 的 outcome）：**outbox 行会跨越
    服务端升级**。它是一张库表，不是内存队列——升级前入队、升级后才被 relay
    认领的行，带的是**老代码写的 body**（新 worker 的 404 payload 被判成
    ``'ok'``）。那个窗口的宽度是「重启时 outbox 的深度」，不是零。
    """
    d = result if isinstance(result, dict) else {}
    if d.get("title") == NOT_FOUND_TITLE:
        return True
    declared = d.get(EVENT_META_OUTCOME)
    return isinstance(declared, str) and declared.strip() == "not_found"


def reconcile_outcome(outcome: str, result: Optional[dict]) -> Tuple[str, bool]:
    """(最终 outcome, 是否被 payload 纠正)。**只允许 ok -> not_found 一个方向。**

    单向是刻意的：``blocked`` / ``parse_failed`` / ``stale`` 都是**服务端事实**
    （封锁判定、``_is_parse_failure``、租约门），payload 无权推翻它们——尤其是
    ``stale``，它说的是"这次提交到得太晚了"，与页面上有什么毫无关系。
    而 ``ok -> not_found`` 是**保守**方向：not_found 的记录不算哈希、
    不进 ``catalog.products``（契约 §6.3），认错了顶多少一次 upsert，
    漏认则是两次误复审 + 一条被占位符覆盖的目录记录。
    """
    if outcome == "ok" and payload_says_not_found(result):
        return "not_found", True
    return outcome, False


def normalize_outcome(outcome: Optional[str]) -> Tuple[str, bool]:
    """(归一化后的 outcome, 是否被改写)。越界值 -> parse_failed。

    故意在 Python 里做而不是靠 CHECK 约束：一个越界值撞上 CHECK 会让 relay
    事务永久回滚，整条流停摆；而它本来只是一条记录的分类错了。
    """
    o = (outcome or "").strip()
    if o in EVENT_OUTCOMES:
        return o, False
    return _OUTCOME_DEFAULT, True


def normalize_marketplace(value: Optional[str]) -> Tuple[str, bool]:
    """(封闭集内的 marketplace, 是否被改写)。

    计划 §2.3：**绝不**透传 parser 的 ``site``（那永远是硬编码的 ``"US"``）。
    集外的值在这里就地纠正 + 计数，而不是留给列上的 CHECK —— 同上，
    一条脏行不该让整条流停摆。
    """
    v = (value or "").strip().lower()
    if v in EVENT_MARKETPLACES:
        return v, False
    return EVENT_DEFAULT_MARKETPLACE, True


def normalize_zip(value: Any) -> Tuple[str, bool]:
    """(zip_requested, 是否补过零)。

    ``zip_requested`` 是消费侧分组键 ``(asin, marketplace, zip_requested)``
    的一部分（契约 §5.5 硬规则 2），所以它的形状必须稳定。补零只针对
    「纯数字且短于 5 位」——那是整数往返丢掉前导零的唯一还原方式
    （'1001' -> '01001'）。其余一律原样透传：不认识的形状宁可原样留着，
    也不要自作主张把一个商品的价格序列劈成两组。
    """
    if value is None:
        return "", False
    s = str(value).strip()
    z = _zfill_short_numeric(s)          # P4.6：补零规则的唯一真源
    if z is not None:
        return z, True
    return s, False


# --------------------------------------------------------------------------
# Phase 4 的四个采集质量信号（worker payload 的 `_` 键 -> scrape_events 的列）
# --------------------------------------------------------------------------
# 共同口径：**归一化，不停摆**。集外值就地纠正 + 计数 + 一条 WARNING，绝不
# 依赖列上的 CHECK —— 一条脏信号不该让整条事件流回滚（schema.py 的 Phase 4
# 常量段写了同一条理由）。
#
# 第二条共同口径：**只许削弱，不许加强**。这四个字段是消费侧用来决定
# 「这条记录算不算数」的，服务端把一个说不清的值补成 confirmed / 15 /
# 'selectolax' 就是在伪造观测。所以拿不准一律落到最弱的那个值
# （unverified / 0 / NULL），而不是最方便的那个。
#
# 由此推出的一条禁令：**绝不拿 ``data['zip_code']`` 去合成 zip_observed。**
# 那一列是**请求**邮编（P4-1 之后语义已收紧成「恒为请求值」），拿它当观测值
# 会让 zip_verify 变成一个恒为 confirmed 的常量 —— 一个错的 confirmed 比一个
# 诚实的 unverified 更糟。观测值只有一个来源：worker 从 glow-ingress-line2
# 抽出来放进 payload 的 ``_zip_observed``（worker/parser.py 的
# ``_parse_zip_observed``，它同样拒绝「取不到就回退成请求值」的兜底）。

def normalize_zip_observed(value: Any) -> Tuple[Optional[str], bool]:
    """(zip_observed 或 None, 是否被改写)。

    ``None`` 是**合法且常见**的：页面没挂 glow 挂件时 worker 如实交 null
    （worker/parser.py 的 ``_parse_zip_observed`` 明确不做「取不到就回退成请求值」
    的兜底）。所以「没有观测值」不算改写、不计数。

    补零规则与 ``normalize_zip`` **必须一致**：这两个值会被
    ``reconcile_zip_verify`` 直接比较，一边补零一边不补，就会凭空造出
    ``mismatch``（'01001' vs '1001'）。

    P4.6：这条「必须一致」以前**只写在这段注释里**，两个函数各抄了一遍
    ``s.isdigit() and 0 < len(s) < 5``，谁改一边都不会有任何东西响。
    现在两边调同一个 ``_zfill_short_numeric``，注释变成可执行的。
    **外层语义仍然是两份**（本函数的 ``None`` 合法、还要拦 bool/list/dict），
    那部分刻意不合并，理由见 common/core/zipcode.py 的 docstring。
    """
    if value is None:
        return None, False
    if isinstance(value, bool) or isinstance(value, (list, dict, tuple, set)):
        # jsonb 里什么都可能出现；这几种绑到 text 列就是 DataError，
        # 而 relay 对 DataError 的处置是把整行搬进死信表。
        return None, True
    s = str(value).strip()
    if not s:
        return None, False
    z = _zfill_short_numeric(s)          # P4.6：与 normalize_zip 同一条规则
    if z is not None:
        return z, True
    return s, False


def normalize_zip_verify(value: Any) -> Tuple[str, bool]:
    """(封闭集内的 zip_verify, 是否被改写)。集外一律 ``unverified``。

    列是 ``NOT NULL``，所以这里永远要给出一个值。老 worker 的 payload 里根本
    没有 ``_zip_verify`` 这个键 —— 那不是错误，是「没测过」，正好就是
    ``unverified`` 的字面含义，因此**不计入** coerced（否则灰度期计数器会被
    老 worker 刷满，真正的脏值反而看不见）。
    """
    if value is None:
        return EVENT_ZIP_VERIFY_DEFAULT, False
    v = value.strip() if isinstance(value, str) else ""
    if v in EVENT_ZIP_VERIFY_VALUES:
        return v, False
    return EVENT_ZIP_VERIFY_DEFAULT, True


def reconcile_zip_verify(verify: str, zip_requested: Any,
                         zip_observed: Optional[str]) -> Tuple[str, bool]:
    """服务端的一致性复核：``confirmed`` 必须真的自洽。返回 (值, 是否被降级)。

    唯一一条规则：``verify == 'confirmed'`` 却带着一个**与请求值不同**的
    ``zip_observed`` ⇒ 降级成 ``mismatch``。这不是 worker 现在会犯的错
    （``derive_zip_verify`` 结构上不会这么写），而是「这两列将来任何一处漂了，
    消费侧不会拿到一条自相矛盾的记录」的守卫 —— 而它守的恰好是本阶段最贵的
    那个故障：一条声称 zip A、价格其实是 zip B 的记录会把一个商品的价格序列
    悄悄劈成两组，且从数据上完全看不出异常（契约 §6.1 硬规则）。

    **只降不升**：观测值与请求值相同却写着 ``mismatch`` 的记录原样留着。
    把它改成 ``confirmed`` 是在替 worker 断言「这一页确实按目标邮编渲染」，
    而 relay 手上并没有那个证据。
    """
    if verify != "confirmed" or not zip_observed:
        return verify, False
    req = str(zip_requested or "").strip()
    if req and zip_observed != req:
        return "mismatch", True
    return verify, False


def normalize_completeness(value: Any) -> Tuple[int, bool]:
    """(0..15 的位图, 是否被改写)。拿不准一律 0 = **未测量**（契约 §6.4）。

    ``bool`` 显式拒收：``True`` 是 ``int`` 的子类，直接放行会变成 bit0=1
    ——「面包屑区块存在」，而那是一句没人测过的断言。
    ``None``（老 worker 根本没有这个键）不计入 coerced，理由同 zip_verify。
    """
    if value is None:
        return EVENT_COMPLETENESS_UNMEASURED, False
    if isinstance(value, bool):
        return EVENT_COMPLETENESS_UNMEASURED, True
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return EVENT_COMPLETENESS_UNMEASURED, True
    if 0 <= iv <= EVENT_COMPLETENESS_MAX:
        return iv, False
    return EVENT_COMPLETENESS_UNMEASURED, True


def normalize_parse_engine(value: Any) -> Tuple[Optional[str], bool]:
    """(``'selectolax'`` / ``'lxml'`` / None, 是否被改写)。

    列可空，而契约 §6.3 已经写明「Phase 4 之前可能为 null」——所以 NULL 是
    一个消费者已经准备好的合法值，比猜一个引擎名好得多：parse_engine 存在的
    唯一理由是让「引擎导致的分歧」可归因，一条猜出来的溯源信息比没有更坏。
    """
    if value is None:
        return None, False
    v = value.strip() if isinstance(value, str) else ""
    if v in EVENT_PARSE_ENGINES:
        return v, False
    return None, True


def scrub_nul(obj: Any) -> Tuple[Any, int]:
    """递归剔除字符串里的 NUL，返回 (清洗后的对象, 剔除个数)。

    **无条件执行，与 D-12 的 PG_STRIP_NUL 开关无关。** 实测：

        json.dumps 转义过的 \\u0000  -> UntranslatableCharacterError (22P05)
                                        "\\u0000 cannot be converted to text"
        原始 NUL 字节                -> CharacterNotInRepertoireError (22021)

    ``jsonb`` **连正确转义过的 \\u0000 都拒收**，而且 SQLSTATE 与 text 列的那个
    还不一样。默认 ``PG_STRIP_NUL=0`` 下这不改变任何事（事务三条语句之后照样
    会死在 asin_data 上）；但 ``PG_STRIP_NUL=1`` 是运维的逃生口，此时一个没洗过
    的 body 会杀掉那个开关本来要救的事务。jsonb 表示不了 NUL，没有忠实选项：
    剔除、计数、记日志。
    """
    if isinstance(obj, str):
        if "\x00" in obj:
            return obj.replace("\x00", ""), obj.count("\x00")
        return obj, 0
    if isinstance(obj, dict):
        out: Dict[Any, Any] = {}
        n = 0
        for k, v in obj.items():
            k2, nk = scrub_nul(k)
            v2, nv = scrub_nul(v)
            out[k2] = v2
            n += nk + nv
        return out, n
    if isinstance(obj, (list, tuple)):
        out_l = []
        n = 0
        for v in obj:
            v2, nv = scrub_nul(v)
            out_l.append(v2)
            n += nv
        return out_l, n
    return obj, 0


def dumps_body(obj: Any) -> str:
    """body / event row 的唯一序列化入口。

    ``default=str`` 只是兜底：body 里出现 datetime / Decimal 这类东西是上游的
    bug，但让它变成字符串比让整条写事务炸掉好。
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def parse_crawl_time(crawl_time: Any) -> Tuple[Optional[datetime], str]:
    """``result.crawl_time`` -> (UTC datetime 或 None, 判形)。见上方 P4-7 注释块。

    判形是**返回值的一部分**而不是内部细节：混合机队期间「这条是老 worker 还是
    新 worker 发的」是运维要看的数字，调用方据此记计数器。

    ``datetime`` 实例（``save_result()`` 这类进程内直调路径能喂进来）：带 tzinfo
    的照收；**裸 datetime 沿用旧口径当 +08:00**。那是本函数改动之前的行为，
    而 JSON 路径根本产生不了 datetime 对象，所以这条分支只影响 Python 直调方
    ——对它们保持等价比换一个同样是猜的口径更有价值。
    """
    if isinstance(crawl_time, datetime):
        if crawl_time.tzinfo is not None:
            return crawl_time.astimezone(timezone.utc), CRAWL_TIME_TZ_AWARE
        return (crawl_time.replace(tzinfo=_WORKER_LEGACY_TZ).astimezone(timezone.utc),
                CRAWL_TIME_LEGACY_CST)

    s = crawl_time.strip() if isinstance(crawl_time, str) else ""
    if not s:
        return None, CRAWL_TIME_MISSING

    # 1) 完整 ISO-8601 / RFC3339。Python 3.11 的 fromisoformat 认 'Z'，
    #    这里仍然显式换成 '+00:00'，免得哪天跑在更老的解释器上静默退化。
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc), CRAWL_TIME_TZ_AWARE
        # 裸值：靠分隔符判代际。老 worker 是 strftime('%Y-%m-%d %H:%M:%S')，
        # 空格；'T' 只可能来自 ISO 系的写法，那一代的时钟是 UTC。
        if "T" in s or "t" in s:
            return dt.replace(tzinfo=timezone.utc), CRAWL_TIME_NAIVE_UTC
        return (dt.replace(tzinfo=_WORKER_LEGACY_TZ).astimezone(timezone.utc),
                CRAWL_TIME_LEGACY_CST)

    # 2) fromisoformat 咬不动的形状（尾巴上挂了别的东西）。旧实现是
    #    ``strptime(s[:19], fmt)``，那个前缀切法救得回 '2026-08-05 10:00:00.123'
    #    这类值，照样保留 —— 两种分隔符各试一次。
    head = s[:19]
    for fmt, form in ((_CRAWL_TIME_LEGACY_FMT, CRAWL_TIME_LEGACY_CST),
                      (_CRAWL_TIME_ISO_FMT, CRAWL_TIME_NAIVE_UTC)):
        try:
            naive = datetime.strptime(head, fmt)
        except (ValueError, TypeError):
            continue
        tz = _WORKER_LEGACY_TZ if form == CRAWL_TIME_LEGACY_CST else timezone.utc
        return naive.replace(tzinfo=tz).astimezone(timezone.utc), form

    return None, CRAWL_TIME_UNPARSABLE


def parse_collected_at(crawl_time: Any, fallback: datetime) -> Tuple[datetime, bool]:
    """``result.crawl_time`` -> UTC datetime。返回 (值, 是否用了兜底)。

    ``parse_crawl_time`` 的窄口径包装，保持 Phase 2 起就有的两元组签名。
    兜底值是 ``recorded_at``（服务端时钟）：它不是采集时刻，但与之相差一次
    提交的时间；而 collected_at 是 NOT NULL 列，且契约 §6.2 已经把它标成
    「仅供参考，排序权威是 seq」。
    """
    dt, _form = parse_crawl_time(crawl_time)
    if dt is None:
        return fallback, True
    return dt, False


_BOUND_RE = re.compile(r"FOR\s+VALUES\s+FROM\s+\((.+?)\)\s+TO\s+\((.+?)\)",
                       re.IGNORECASE | re.DOTALL)


def parse_partition_bound(expr: str) -> Tuple[Optional[int], Optional[int]]:
    """``FOR VALUES FROM (MINVALUE) TO ('20000000')`` -> (None, 20000000)。

    PG 没有给 range 边界提供结构化的目录 API，``pg_get_expr`` 的文本是唯一来源；
    bigint 边界会被引号包起来。MINVALUE/MAXVALUE 返回 None。
    """
    m = _BOUND_RE.search(expr or "")
    if not m:
        return None, None

    def one(tok: str) -> Optional[int]:
        t = tok.strip().strip("'\"").strip()
        if t.upper() in ("MINVALUE", "MAXVALUE"):
            return None
        try:
            return int(t)
        except ValueError:
            return None

    return one(m.group(1)), one(m.group(2))


def _partition_index(name: str) -> Optional[int]:
    if not name.startswith(EVENT_PARTITION_PREFIX):
        return None
    try:
        return int(name[len(EVENT_PARTITION_PREFIX):])
    except ValueError:
        return None


# ============================================================
# common.slowhash 适配层
# ============================================================
#
# 那个模块由另一个 builder 在写，此刻可能还不存在。规矩（任务书原话）：
# 「惰性 import，缺模块就当哈希为 NULL，而不是崩溃」。
# 名字上做多路兼容：谁都不想因为一个函数叫 review_hash 还是 compute_review_hash
# 就让整条流的哈希列全空。

_SLOWHASH_SENTINEL = object()
_REVIEW_NAMES = ("review_hash", "compute_review_hash", "review")
_SLOW_NAMES = ("slow_hash", "compute_slow_hash", "slow")
_COMBINED_NAMES = ("compute_hashes", "hashes", "compute")


def parse_hash_ver(raw: Any) -> int:
    """``HASH_VER`` -> ``scrape_events.hash_ver`` 的 int。

    ⚠ 两边的类型**本来就不一样**，这不是 bug：
      * ``common.slowhash.HASH_VER == 'v1'`` —— 它同时是哈希字符串的前缀
        （``'v1:<sha256>'``，计划 §4.2 第 6 条）。
      * ``scrape_events.hash_ver`` 是 ``int NOT NULL DEFAULT 1``（计划 §2.1 的
        DDL，逐字照抄，不改）。
    所以这里做一次显式转换，别让任何人以为可以把 'v1' 直接塞进 int 列。
    """
    if isinstance(raw, bool):
        return 1
    if isinstance(raw, int):
        return raw
    s = str(raw or "").strip().lstrip("vV")
    try:
        return int(s)
    except ValueError:
        logger.warning("认不出的 HASH_VER=%r，hash_ver 记 1", raw)
        return 1


class _SlowHashAdapter:
    """``common.slowhash`` 的适配层。

    模块由另一个 builder 拥有，这里**只读不改**。今天它导出的是
    ``compute_hashes(data, outcome='ok') -> (review, slow)`` +
    ``review_hash(data)`` / ``slow_hash(data)`` + ``HASH_VER='v1'``；
    名字上留了多路兼容，且模块整个缺席时降级成「哈希写 NULL」而不是崩溃
    （任务书明写的降级口径）。
    """

    __slots__ = ("_mod", "_review", "_slow", "_combined", "_combined_takes_outcome",
                 "_ver", "_warned")

    def __init__(self):
        self._mod: Any = _SLOWHASH_SENTINEL
        self._review = None
        self._slow = None
        self._combined = None
        self._combined_takes_outcome = False
        self._ver = 1
        self._warned = False

    def _resolve(self):
        if self._mod is not _SLOWHASH_SENTINEL:
            return
        try:
            import common.slowhash as mod  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            self._mod = None
            logger.warning(
                "common.slowhash 不可用（%s: %s）—— review_hash / slow_hash 一律写 NULL。"
                "这是 Phase 2 约定的降级，不是错误。", type(e).__name__, e)
            return
        self._mod = mod
        self._combined = _first_callable(mod, _COMBINED_NAMES)
        self._review = _first_callable(mod, _REVIEW_NAMES)
        self._slow = _first_callable(mod, _SLOW_NAMES)
        self._combined_takes_outcome = _accepts_two_args(self._combined)
        self._ver = parse_hash_ver(getattr(mod, "HASH_VER",
                                           getattr(mod, "hash_ver", 1)))
        if self._combined is None and self._review is None and self._slow is None:
            logger.error(
                "common.slowhash 存在但没有认得出来的入口（找过 %s / %s / %s）——"
                "哈希列会全是 NULL。", _COMBINED_NAMES, _REVIEW_NAMES, _SLOW_NAMES)

    @property
    def hash_ver(self) -> int:
        self._resolve()
        return self._ver

    @property
    def available(self) -> bool:
        self._resolve()
        return self._mod is not None and (
            self._combined is not None or self._review is not None
            or self._slow is not None)

    def compute(self, payload: dict, outcome: str = "ok"
                ) -> Tuple[Optional[str], Optional[str]]:
        """(review_hash, slow_hash)。任何异常都降级成 (None, None)。

        哈希算错不该让一整批事件停在 outbox 里：哈希是消费侧的复审**判据**，
        它为空只是让那一条不触发复审；而一个抛异常的 relay 会让整条流停摆。
        """
        self._resolve()
        if not self.available:
            return None, None
        try:
            if self._combined is not None:
                got = (self._combined(payload, outcome)
                       if self._combined_takes_outcome else self._combined(payload))
                if isinstance(got, dict):
                    return got.get("review_hash"), got.get("slow_hash")
                if isinstance(got, (tuple, list)) and len(got) >= 2:
                    return got[0], got[1]
            rh = self._review(payload) if self._review else None
            sh = self._slow(payload) if self._slow else None
            return rh, sh
        except Exception as e:  # noqa: BLE001
            if not self._warned:
                self._warned = True
                logger.exception("common.slowhash 计算失败，本进程后续哈希写 NULL: %s", e)
            return None, None


def _first_callable(mod: Any, names: Sequence[str]):
    for n in names:
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    return None


def _accepts_two_args(fn) -> bool:
    if fn is None:
        return False
    try:
        import inspect  # noqa: PLC0415
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        return len(params) >= 2
    except (TypeError, ValueError):
        return False


# ============================================================
# SQL —— 全部 schema 限定（search_path 被钉死在 public）
# ============================================================
# D-6：pgdb 内部用 ``?`` 占位符。relay 走的是**裸 asyncpg 连接**（没有 ConnProxy
# 的翻译层），所以这里在模块加载时就把 ``?`` 翻好，运行期零开销、方言仍然只有一种。

SQL_EMIT_OUTBOX = "INSERT INTO scraper.scrape_outbox (body) VALUES (?::jsonb) RETURNING id"

SQL_CLAIM = translate_sql("""
DELETE FROM scraper.scrape_outbox
 WHERE id IN (SELECT id FROM scraper.scrape_outbox
               ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED)
RETURNING id, enqueued_at, body
""")

# ORDER BY ord：让一批之内的 seq 跟着 outbox 的 id 顺序走。
# 光标保证并不依赖这一点（依赖的是「relay 是唯一写入者且串行提交」），
# 它只是让同一批的顺序可读、可复现。
# 无目标的 ON CONFLICT DO NOTHING：唯一索引在**分区**上，
# ``ON CONFLICT (source_id)`` 推断不出来（InvalidColumnReferenceError）。
# 另一条唯一索引是 seq 主键，bigserial 撞不上，所以吞掉的只会是 source_id 重复。
SQL_INSERT_EVENTS = translate_sql("""
INSERT INTO scraper.scrape_events (
    source_id, gen, asin, marketplace, zip_requested, zip_observed, zip_verify,
    collected_at, recorded_at, outcome, completeness, error_type, error_detail,
    batch_id, task_id, worker_id, attempt, parse_engine,
    review_hash, slow_hash, hash_ver, payload)
SELECT r->>'source_id', r->>'gen', r->>'asin', r->>'marketplace',
       r->>'zip_requested', r->>'zip_observed', r->>'zip_verify',
       (r->>'collected_at')::timestamptz, (r->>'recorded_at')::timestamptz,
       r->>'outcome', COALESCE((r->>'completeness')::int, 0),
       r->>'error_type', r->>'error_detail',
       (r->>'batch_id')::bigint, (r->>'task_id')::bigint, r->>'worker_id',
       COALESCE((r->>'attempt')::int, 0), r->>'parse_engine',
       r->>'review_hash', r->>'slow_hash', COALESCE((r->>'hash_ver')::int, 1),
       COALESCE(r->'payload', '{}'::jsonb)
  FROM unnest(?::jsonb[]) WITH ORDINALITY AS t(r, ord)
 ORDER BY ord
ON CONFLICT DO NOTHING
RETURNING seq, source_id
""")

SQL_BUMP_MAX_SEQ = translate_sql("""
INSERT INTO scraper.sync_meta (k, v) VALUES ('max_seq_ever', ?)
ON CONFLICT (k) DO UPDATE
   SET v = GREATEST(scraper.sync_meta.v::bigint, EXCLUDED.v::bigint)::text
""")

SQL_QUARANTINE_HEAD = translate_sql("""
WITH claimed AS (
    DELETE FROM scraper.scrape_outbox
     WHERE id IN (SELECT id FROM scraper.scrape_outbox
                   ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
 RETURNING id, enqueued_at, body
)
INSERT INTO scraper.scrape_outbox_dead (id, enqueued_at, failure, body)
SELECT id, enqueued_at, ?, body FROM claimed
ON CONFLICT (id) DO NOTHING
RETURNING id
""")

SQL_META_GET = translate_sql("SELECT v FROM scraper.sync_meta WHERE k = ?")
SQL_META_SET = translate_sql(
    "INSERT INTO scraper.sync_meta (k, v) VALUES (?, ?) "
    "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v")

# bigserial 的底层序列名。两个陷阱，都实测过：
#   * ``FROM pg_get_serial_sequence(...)::regclass`` 不是合法的 FROM 项
#     （PostgresSyntaxError: syntax error at or near "::"）。
#   * ``pg_sequences.last_value`` 在 ``is_called = false`` 时是 **NULL**
#     —— 也就是刚 ``setval(..., false)`` 完（回退恢复路径正是这么做的）就读不到值，
#     分区维护会以为水位还在 0。所以必须直接读序列关系本身。
SQL_SEQ_NAME = "SELECT pg_get_serial_sequence('scraper.scrape_events', 'seq')"

SQL_OUTBOX_DEPTH = """
SELECT count(*) AS depth,
       COALESCE(EXTRACT(EPOCH FROM (now() - min(enqueued_at))), 0)::float8 AS lag_s
  FROM scraper.scrape_outbox
"""


# ============================================================
# EventStreamMixin
# ============================================================

class _RelayHandedOver(Exception):
    """重连时发现单例锁已被别人拿走 —— 交班，不是故障。

    单独一个类型是为了让 ``_relay_main`` 能把它与真故障分开：真故障要
    ``relay_state='failed'`` + ``logger.exception``（该报警），交班只要
    ``'refused'`` + 一条 WARNING（该安静地排队等下一次机会）。
    """


class EventStreamMixin:
    """事件流：写钩子 + 引导 + relay + 指标。

    只依赖 PoolMixin 提供的 ``self._db`` / ``self._write_conn`` / ``self.dsn``。
    **不定义 __init__**（MRO 里 PoolMixin 的 __init__ 必须赢），所有状态都靠
    ``_ev()`` 惰性建出来。
    """

    # ---------------- 惰性状态 ----------------
    def _ev(self) -> Dict[str, Any]:
        st = getattr(self, "_event_state", None)
        if st is None:
            st = {
                "ready": False,
                "gen": None,
                "instance_id": None,
                "hash_adapter": _SlowHashAdapter(),
                "relay_task": None,
                "relay_conn": None,
                # stopped   —— 没启动过，或已干净停下
                # starting  —— start_event_relay 在途（连库 / 抢锁 / 建表）
                # running   —— 主循环在跑
                # refused   —— 单例锁被别人持有，本进程有意不启动
                # failed    —— 主循环异常退出，事件流已停摆（要报警的那个）
                "relay_state": "stopped",
                # 起停竞争的两个把手，见 start_event_relay / stop_event_relay。
                # stop 只看得见**已经装好**的 relay_task / relay_conn，而
                # start 在装好它们之前有三个 await 点；这两个字段就是让
                # 一个在途的 start 能发现「我已经被叫停了」。
                "stopping": False,
                "start_epoch": 0,
                "batch": RELAY_BATCH,
                "consec_fail": 0,
                "tick": 0,
                "rate": deque(),               # [(monotonic, n_relayed)]
                "counters": {
                    "emitted": 0,
                    "emit_dropped": 0,
                    "emit_nul_stripped": 0,
                    "relayed": 0,
                    "conflicts": 0,
                    "ticks": 0,
                    "tick_errors": 0,
                    "quarantined": 0,
                    "outcome_coerced": 0,
                    "marketplace_coerced": 0,
                    "zip_padded": 0,
                    "collected_at_fallback": 0,
                    # ---- Phase 4：采集质量信号 ----
                    # 各自的归一化次数。这些不是错误计数，是**漂移探针**：
                    # 常态应当恒为 0，任何一个开始增长都意味着 worker 与
                    # server 对某个字段的口径已经对不上了。
                    "zip_verify_coerced": 0,
                    "zip_verify_downgraded": 0,
                    "zip_observed_coerced": 0,
                    "completeness_coerced": 0,
                    "parse_engine_coerced": 0,
                    # payload 把一条 body 里写着 'ok' 的记录纠正成 not_found
                    # 的次数。>0 = outbox 里还压着升级前入队的行（正常，会归零），
                    # 持续增长 = _emit_outbox 那侧的分类漏了。
                    "outcome_recovered_not_found": 0,
                    # 混合机队观测：老 worker（裸 UTC+8）与丢了标记的裸 ISO 各多少条。
                    "collected_at_legacy_cst": 0,
                    "collected_at_naive_utc": 0,
                    # zip_requested 的来源仲裁（见 _emit_outbox）。
                    "zip_requested_from_payload_meta": 0,
                    "zip_requested_mismatch": 0,
                    "partitions_created": 0,
                    "range_checks_repaired": 0,
                    "gen_minted": 0,
                    "rewinds_detected": 0,
                    # B3：tick 连续失败到阈值之后重开连接 + 重抢单例锁的次数。
                    "relay_reconnects": 0,
                    "relay_restarts": 0,
                },
                # 连续失败的 tick 数（成功一次即清零）。到 RELAY_RECOVER_AFTER
                # 就去重开连接——见 _relay_main / _relay_recover（B3）。
                "consec_tick_fail": 0,
                "last_tick_ms": 0.0,
                "outbox_depth": 0,
                "relay_lag_s": 0.0,
            }
            self._event_state = st
        return st

    def _bump(self, key: str, n: int = 1) -> None:
        self._ev()["counters"][key] = self._ev()["counters"].get(key, 0) + n

    @property
    def event_gen(self) -> Optional[str]:
        return self._ev()["gen"]

    @property
    def event_instance_id(self) -> Optional[str]:
        return self._ev()["instance_id"]

    # ======================================================
    # 1) 引导
    # ======================================================
    async def init_event_stream(self, conn=None) -> None:
        """建表 / 建分区 / 定 gen / 定 instance_id。**幂等**。

        由 ``SchemaMixin.init_tables()`` 在 connect() 期调用（那里有一段长注释
        解释为什么落在这里而不是 relay 启动时），也可以自己调。

        ⚠ ``conn`` 参数不是可有可无的方便：``start_event_relay()`` **必须**传
        自己那条专用连接。默认的 ``self._write_conn`` 是**裸** asyncpg 连接
        （不是 ConnProxy），在上面发语句既拿不到 ``_op_lock`` 也拿不到
        ``_write_lock``——而 relay 是在 lifespan 里 ``create_task`` 起来的，它第一次
        真正执行的时刻可能已经在服务 HTTP 请求了。那时候在写连接上发 DDL 有两种
        死法：``InterfaceError: another operation is in progress``，或者更坏——
        DDL 悄悄执行在**别人开着的事务里**，跟着别人一起回滚。
        connect() 期没有并发，所以那条路径上用 ``_write_conn`` 是安全的。

        每条 DDL 独立自动提交，与 init_tables() 同一口径：裹成一个事务的话，
        任何一条失败都会让后面全部 InFailedSQLTransactionError。
        """
        conn = conn if conn is not None else self._write_conn
        if conn is None:
            raise RuntimeError("init_event_stream 需要一条可用连接")

        for stmt in EVENT_STREAM_DDL:
            await conn.execute(stmt)
        # p0 必须先于父表索引建：CREATE INDEX 在父表上会自动传播到**已存在**的分区，
        # 顺序反了 p0 就少一条 recorded_at 索引（也就不能当后续分区的 LIKE 模板）。
        for stmt in event_create_first_partition_sql():
            await conn.execute(stmt)
        await conn.execute(EVENT_PARENT_INDEX_SQL)
        await conn.execute(EVENT_BATCH_INDEX_SQL)

        await self._assert_event_columns(conn)
        await self._bootstrap_identity(conn)
        await self.ensure_event_partitions(conn)

        self._ev()["ready"] = True
        logger.info("事件流就绪：gen=%s instance_id=%s contract=%s",
                    self._ev()["gen"], self._ev()["instance_id"],
                    EVENT_CONTRACT_VERSION)

    async def _assert_event_columns(self, conn) -> None:
        """列集自检。少一列必须现在炸，而不是等第一次 INSERT。"""
        problems: List[str] = []
        for table, expected in EVENT_EXPECTED_COLUMNS.items():
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'scraper' AND table_name = $1 "
                "ORDER BY ordinal_position", table)
            actual = [r["column_name"] for r in rows]
            if actual != expected:
                problems.append(f"scraper.{table}: 期望={expected} 实际={actual}")
        if problems:
            raise RuntimeError("事件流 schema 不符:\n  " + "\n  ".join(problems))

    async def _meta_get(self, conn, key: str) -> Optional[str]:
        return await conn.fetchval(SQL_META_GET, key)

    async def _meta_set(self, conn, key: str, value: str) -> None:
        await conn.execute(SQL_META_SET, key, value)

    async def _bootstrap_identity(self, conn) -> None:
        """gen / instance_id / contract_version。规则见模块头注释第三节。"""
        st = self._ev()

        if await self._meta_get(conn, "contract_version") is None:
            await self._meta_set(conn, "contract_version", EVENT_CONTRACT_VERSION)

        # ---- instance_id：运维配置，永不自动铸造（T12）----
        env_iid = (os.environ.get("SCRAPER_INSTANCE_ID") or "").strip()
        stored_iid = await self._meta_get(conn, "instance_id")
        if env_iid:
            if stored_iid and stored_iid != env_iid:
                logger.warning("instance_id 由 %r 改成 %r（SCRAPER_INSTANCE_ID）",
                               stored_iid, env_iid)
            await self._meta_set(conn, "instance_id", env_iid)
            st["instance_id"] = env_iid
        elif stored_iid:
            st["instance_id"] = stored_iid
        else:
            st["instance_id"] = "unconfigured"
            await self._meta_set(conn, "instance_id", "unconfigured")
            logger.warning(
                "SCRAPER_INSTANCE_ID 未配置，instance_id 记作 'unconfigured'。"
                "两个克隆部署会因此无法区分（计划 T12）——请在部署里配上。")

        # ---- gen：复用；只有全新库 / 检出回退才新铸 ----
        #
        # ⚠ 判据是**序列水位**，不是 ``max(seq)``（B4）。原来写的是
        # ``actual < ever``，那有一条 Phase 6 一定会踩的假阳性：保留期一旦把
        # ``scrape_events`` 清空（DROP 掉全部分区、或 TRUNCATE），``max(seq)``
        # 归零，而 ``max_seq_ever`` 还留在 meta 表里 —— 于是一次**普通重启**就铸出
        # 新 gen，消费侧按契约 §5.5 硬停 + 全量对账。实测每次 TRUNCATE 必现：
        #     事件流倒退：max(seq)=0 < max_seq_ever=1001 ... 铸新 gen
        #
        # 真正要防的是「seq 会被**重新发**一遍」：同一个 seq 第二次出现、内容却不同，
        # 消费者的游标就永久错位。而 seq 由 bigserial 发号，**序列不随分区 DROP
        # 回退**（也不随不带 RESTART IDENTITY 的 TRUNCATE 回退）。所以：
        #     水位 = max(max(seq), 序列已发出的最大值)
        #     水位 < max_seq_ever  ⇔  序列真的退回去了  ⇔  seq 会被重发 ⇔ 真回退
        # 保留期清空 ⇒ 水位仍等于历史高点 ⇒ 不铸新 gen（正确）。
        # 备份恢复把序列一起退回去 ⇒ 水位 < ever ⇒ 铸新 gen（正确，且照旧 setval）。
        # 整机快照回滚（T11b）连 ever 一起退回，服务端依旧检不出来 —— 那是设计如此，
        # 消费侧的 max_seq 单调性检查是唯一防线。
        gen = await self._meta_get(conn, "gen")
        actual = await conn.fetchval(
            "SELECT COALESCE(max(seq), 0) FROM scraper.scrape_events")
        issued = await self._seq_high_water(conn)
        watermark = max(int(actual or 0), issued)
        ever_raw = await self._meta_get(conn, "max_seq_ever")
        try:
            ever = int(ever_raw or 0)
        except (TypeError, ValueError):
            ever = 0

        if not gen:
            gen = self._mint_gen()
            await self._meta_set(conn, "gen", gen)
            logger.info("全新事件流，铸 gen=%s", gen)
        elif watermark < ever:
            old = gen
            gen = self._mint_gen()
            await self._meta_set(conn, "gen", gen)
            self._bump("rewinds_detected")
            logger.error(
                "事件流倒退：seq 水位=%s（max(seq)=%s，序列已发到 %s）< "
                "max_seq_ever=%s。多半是从备份恢复了 DB。"
                "铸新 gen %s -> %s（消费侧会看到 gen 变化并触发全量对账），"
                "并把序列推到 %s 之后以保持实例内单调。",
                watermark, actual, issued, ever, old, gen, ever)
            target = ever + 1
            await self.ensure_event_partitions(conn, floor_seq=target)
            await conn.execute(
                "SELECT setval(pg_get_serial_sequence('scraper.scrape_events','seq'),"
                " $1, false)", target)
        elif actual < ever:
            # 表空了/被裁剪了，但序列没退——保留期干的活，不是回退。
            # 记一条 INFO 而不是静默：Phase 6 上线后第一次看到它的人应该能对上账。
            logger.info(
                "scrape_events 里最高 seq=%s 低于 max_seq_ever=%s，但序列已发到 %s "
                "—— 是保留期裁剪，不是回退，gen 保持 %s 不变。", actual, ever, issued, gen)
        st["gen"] = gen

        # max_seq_ever 至少不低于当前实际值（老库首次引导时它可能还不存在）
        if actual > ever:
            await self._meta_set(conn, "max_seq_ever", str(actual))

    def _mint_gen(self) -> str:
        self._bump("gen_minted")
        return uuid.uuid4().hex[:12]

    async def reset_event_stream(self) -> None:
        """清空事件流四张表并把 seq 重置回 1（**只给测试用**）。

        故意不并进 SchemaMixin.reset_all()：那个方法的语义对齐
        ``DELETE /api/database``，而那个端点不该碰事件流。
        """
        conn = self._write_conn
        await conn.execute(
            "TRUNCATE scraper.scrape_events, scraper.scrape_outbox, "
            "scraper.scrape_outbox_dead RESTART IDENTITY")
        await conn.execute("DELETE FROM scraper.sync_meta")
        self._ev()["ready"] = False
        await self._bootstrap_identity(conn)
        self._ev()["ready"] = True

    # ======================================================
    # 2) 写钩子（跑在**调用方的事务里**）
    # ======================================================
    async def _emit_outbox(
        self,
        *,
        outcome: str,
        data: Optional[dict] = None,
        asin: Optional[str] = None,
        task_id: Any = None,
        batch_id: Any = None,
        batch_name: Any = None,
        worker_id: Any = None,
        lease_epoch: Any = None,
        attempt: Any = 0,
        auto_retry_count: Any = 0,
        error_type: Any = None,
        error_detail: Any = None,
        zip_requested: Any = None,
        zip_requested_source: Optional[str] = None,
    ) -> Optional[int]:
        """往 ``scraper.scrape_outbox`` 插一行，**在调用方已经开着的事务里**。

        调用方（results_write.py / tasks.py）必须持有 ``_write_lock`` 并且已经
        ``BEGIN`` 过 —— 与结果写入、tasks 租约 UPDATE 同一个事务。实测三张表
        xmin 相同即同事务证书；注入一次失败后 outbox 行随之消失、任务回到
        processing。

        返回新行的 id；事件流没引导起来时返回 None（见 PG_EVENTSTREAM_STRICT）。

        ⚠ ``data`` 必须已经过 ``common.pgdb.outbox.safe_payload``（写钩子侧的
        ``outbox.emit()`` 会做）。``json.dumps`` 默认会把 ``NaN`` / ``Infinity``
        原样吐出来，而 PG 的 json 解析器不收（invalid input syntax for type json），
        那会**在调用方的事务里**炸掉一次今天本来能成功的提交。这里不重复那层
        定形：它属于写钩子（那边知道 D-20 的口径），收在一个地方才守得住。

        ⚠ 不要给它加 ``RETURNING`` 之外的花样，也不要把它塞进 lease 门那条
        UPDATE 里：给 ``UPDATE ... WHERE lease`` 加 RETURNING 会让 ConnProxy 走
        ``returns_rows`` 分支、rowcount 变成 -1，于是 ``rowcount == 0`` 的 lease
        门**永远不成立**，所有过期结果被静默接收（设计规格 §1.3 TRAP 1）。
        需要 retry_count / zip_code 就在同一事务里另发一条普通 SELECT。
        """
        st = self._ev()
        if not st["ready"]:
            self._bump("emit_dropped")
            msg = ("事件流未引导（init_event_stream 没跑过？），"
                   "本条结果不会进流：outcome=%s task_id=%s asin=%s")
            if _EVENTSTREAM_STRICT:
                raise RuntimeError(msg % (outcome, task_id, asin))
            logger.error(msg, outcome, task_id, asin)
            return None

        data = data if isinstance(data, dict) else {}
        if asin is None:
            asin = (data.get("asin") or "").strip()

        norm_outcome, coerced = normalize_outcome(outcome)
        if coerced:
            self._bump("outcome_coerced")
            logger.warning("未知 outcome %r -> %s（task_id=%s）",
                           outcome, norm_outcome, task_id)

        # ---------------- zip_requested 的三级仲裁（P4-1）----------------
        # 1) 调用方传进来的 tasks.zip_code —— 服务端事实，非空时永远赢；
        # 2) payload 的 `_zip_requested` —— worker **真正**发出请求时用的邮编；
        # 3) payload 的 zip_code。
        #
        # 为什么需要第 2 级：worker 的 target_zip 是
        # ``(task.zip_code or "").strip() or worker.zip_code``，也就是说
        # **tasks.zip_code 为空串时，真正请求的是 worker 的默认邮编**，
        # 而 tasks 行上那个空串既非空（`is not None`）又不是真相。照收它等于
        # 把整批记录的分组键 (asin, marketplace, zip_requested) 写成 ''，
        # 消费侧那一组价格序列的邮编从此不可知（契约 §6.1 硬规则）。
        #
        # 第 3 级的注释在 P4-1 之前是「data['zip_code'] 是**观测**邮编」——
        # 那句话现在**过期了**：`_slx_parse_zip_code` / `_parse_zip_code`
        # （读 glow-ingress-line1 的那两个）已随 P4-1 删除，`zip_code` 的语义
        # 收紧成「恒为请求邮编」。观测值另有其列（zip_observed）。
        meta_zip = data.get(EVENT_META_ZIP_REQUESTED)
        meta_blank = meta_zip is None or not str(meta_zip).strip()
        if zip_requested is None or not str(zip_requested).strip():
            if not meta_blank:
                zip_requested = meta_zip
                zip_requested_source = "payload_meta"
                self._bump("zip_requested_from_payload_meta")
            elif zip_requested is None:
                zip_requested = data.get("zip_code")
                if zip_requested_source is None:
                    zip_requested_source = "payload"
        elif not meta_blank and (normalize_zip(meta_zip)[0]
                                 != normalize_zip(zip_requested)[0]):
            # 服务端事实与 worker 自述不一致：**服务端赢**（消费侧的分组键必须
            # 与 tasks 行对得上，否则同一个批次的记录会散进两组），但这必须响。
            # 正常情况下这个计数器恒为 0：worker 的 target_zip 就是从这一行读的。
            self._bump("zip_requested_mismatch")
            logger.warning(
                "zip_requested 不一致：tasks 行=%r，worker 自述=%r（task_id=%s "
                "asin=%s）。按 tasks 行落库；worker 可能没读到 per-ASIN 邮编。",
                zip_requested, meta_zip, task_id, asin)
        zip_norm, padded = normalize_zip(zip_requested)
        if padded:
            self._bump("zip_padded")

        body = {
            "v": EVENT_BODY_VERSION,
            "source_id": f"{st['gen']}:{uuid.uuid4()}",
            "gen": st["gen"],
            "instance_id": st["instance_id"],
            "outcome": norm_outcome,
            "asin": asin,
            "marketplace": EVENT_DEFAULT_MARKETPLACE,
            "zip_requested": zip_norm,
            "zip_requested_source": zip_requested_source or "task",
            "task_id": _as_int_or_none(task_id),
            "batch_id": _as_int_or_none(batch_id),
            "batch_name": batch_name if batch_name is not None else data.get("batch_name"),
            "worker_id": worker_id,
            "lease_epoch": _as_int_or_none(lease_epoch),
            "attempt": _as_int_or_none(attempt) or 0,
            "auto_retry_count": _as_int_or_none(auto_retry_count) or 0,
            "error_type": error_type or None,
            "error_detail": error_detail or None,
            "result": data,
        }

        clean, n_nul = scrub_nul(body)
        if n_nul:
            self._bump("emit_nul_stripped", n_nul)
            logger.warning("body 里剔除了 %d 个 NUL 字节（asin=%s task_id=%s）"
                           "—— jsonb 连转义过的 \\u0000 都不收", n_nul, asin, task_id)

        async with self._db.execute(SQL_EMIT_OUTBOX, (dumps_body(clean),)) as cur:
            row = await cur.fetchone()
        self._bump("emitted")
        return int(row[0]) if row else None

    # ======================================================
    # 3) relay
    # ======================================================
    async def _relay_open_conn(self) -> asyncpg.Connection:
        """relay 专用连接。见模块头注释第二节（对 OWNERSHIP §4.3 的有意豁免）。"""
        return await asyncpg.connect(
            dsn=self.dsn,
            statement_cache_size=0,
            command_timeout=config.PG_COMMAND_TIMEOUT,
            server_settings={"search_path": "public"},
        )

    async def start_event_relay(self) -> bool:
        """拿单例锁并把 relay 循环拉成后台任务。

        返回 True = 本进程就是那个唯一的 relay；False = 别人已经在跑，本进程
        **不启动**（计划 T5）。拿不到锁不是错误，是滚动部署时的正常状态。

        顺序是有讲究的：**先开自己的连接、先拿锁，再跑 init_event_stream(conn)**。
          * 引导 DDL 因此跑在 relay 自己的连接上，碰不到 ``_write_conn``
            （见 init_event_stream 的注释：那条路上会撞进别人的事务）；
          * 也因此被单例锁保护着，两个实例同时启动不会去抢建同一个分区。

        ⚠ 三个 ``await`` 点上都要重新确认「我还该不该起来」（``_start_aborted``）。
        这不是防御性编程，是实测过的泄漏：``lifespan`` 里是
        ``create_task(relay)`` → ``yield`` → ``await stop_event_relay()``，
        进程刚起来就关（配置错误、健康检查失败、测试里起停一次服务器）时，
        stop 跑到的时候 relay 还卡在 ``asyncpg.connect()`` 里，而
        ``relay_task`` / ``relay_conn`` 要到本函数最后才被赋值 —— stop 因此
        **什么都看不见、空转返回**，随后 ``db.close()`` 关掉池子，relay 才慢悠悠
        起来拿到连接和单例锁。实测后果：

            关服之后：relay_state='running' relay_conn=<Connection ...>
            库上残留会话=1  残留 advisory lock=1
            第二个实例 start_event_relay() -> False  state='refused'

        一条泄漏的连接 + 一把没人放的单例锁，而且同进程内再起的服务器会被
        永久拒之门外 —— 事件流静默不工作，outbox 无限堆积。
        """
        st = self._ev()
        task = st["relay_task"]
        if task is not None and not task.done():
            return True

        st["stopping"] = False
        st["start_epoch"] += 1
        epoch = st["start_epoch"]
        # 'starting' 让「刚 create_task、还没连上库」与「跑完停了」在指标上分得开。
        st["relay_state"] = "starting"

        try:
            conn = await self._relay_open_conn()
        except BaseException:
            st["relay_state"] = "failed"
            raise
        try:
            if self._start_aborted(epoch):
                await conn.close()
                return False
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", RELAY_LOCK_KEY)
            if not got:
                await conn.close()
                st["relay_state"] = "refused"
                logger.warning(
                    "relay 单例锁 %s 已被本库上的另一个会话持有，本进程不启动 relay。"
                    "（这是设计如此：scrape_events 必须只有一个写入者，否则 seq 顺序"
                    "不再等于提交顺序，游标保证当场失效。）", RELAY_LOCK_KEY)
                return False
            # 锁已经在手：此后任何一条提前返回都必须显式解锁再关连接，否则
            # 就是上面注释里那个泄漏。
            if self._start_aborted(epoch):
                await self._release_and_close(conn)
                return False
            await self.init_event_stream(conn)
            if self._start_aborted(epoch):
                await self._release_and_close(conn)
                return False
        except asyncio.CancelledError:
            await self._release_and_close(conn)
            st["relay_state"] = "stopped"
            raise
        except BaseException:
            # 建表 / 抢锁失败：锁可能已经在手，必须连锁一起还回去，否则这个库上
            # 再也起不来 relay（下一个实例只会看到 refused）。
            await self._release_and_close(conn)
            st["relay_state"] = "failed"
            raise

        # 下面三句之间没有 await：装好之后 stop 一定看得见它们。
        st["relay_conn"] = conn
        st["relay_state"] = "running"
        st["relay_task"] = asyncio.create_task(
            self._relay_main(conn), name="scrape-event-relay")
        logger.info("relay 启动：tick=%.1fs batch=%d gen=%s",
                    RELAY_TICK_SECONDS, RELAY_BATCH, st["gen"])
        return True

    def _start_aborted(self, epoch: int) -> bool:
        """在途的 start 该不该放弃：被 stop 叫停了，或者被后来的 start 顶掉了。"""
        st = self._ev()
        if st["stopping"]:
            logger.info("relay 启动途中收到停止请求，放弃启动（连接与单例锁一并释放）")
            st["relay_state"] = "stopped"
            return True
        if st["start_epoch"] != epoch:
            logger.info("relay 启动被更晚的一次 start 顶掉，放弃本次")
            return True
        return False

    async def _release_and_close(self, conn) -> None:
        """放掉单例锁再关连接。两步都不许因为异常而漏掉 close。"""
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", RELAY_LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            conn.terminate()

    async def run_event_relay(self) -> None:
        """app.py lifespan 用的入口：**一直**保证本进程有机会成为那个 relay。

        ⚠ 这是一个重试循环，不是「起一次就完」（B2）。原来的写法是
        ``if not await start(): return``，它有两条实测过的路径能让**整个进程余生
        都没有事件流**，而 HTTP 一路绿灯、outbox 只涨不落：

          * **滚动重启。** 新实例启动时老实例还在跑，start 拿不到单例锁返回
            False，任务当场结束；老实例随后停机，锁没人接 ——
                t1: NEW.state=refused task.done=True
                t2: OLD 停机。库上持锁数=0
                t3: 4s 之后 NEW.state=refused task.done=True   持锁数=0
                >>> 滚动重启之后还有 relay 在跑吗？ 否
            "拿不到锁是滚动部署时的正常状态"这句话只对了一半：**当时**正常，
            但必须继续等着接班。
          * **启动瞬间连库失败。** 一次 `PostgresConnectionError: connection
            refused` 让 start 抛出去，lifespan 记一条日志就完了 ——
                _relay_open_conn 被调用 1 次；task.done=True  relay_state='failed'
                6 秒后（库早就好了）: {'outbox': 20, 'events': 0}  持锁数=0

        循环也顺带兜住 B3：``_relay_main`` 因为连接反复出错而退出（state='failed'
        或 'refused'）之后，这里会重新拉起一次完整的 start（新连接 + 重抢锁）。

        退出条件只有两个：被取消（正常关停路径），或者 ``stop_event_relay()``
        置了 ``stopping``。**每一轮重试前都要重查 ``stopping``**，因为
        ``start_event_relay`` 的第一句就把它清掉了 —— 那个清除是给"停完再起"用的
        （测试、热重载），但 lifespan 里 relay 是 ``create_task`` 起来的，
        「stop 整个跑完了 start 协程才第一次被调度」这种交错真实存在，
        循环顶上的这一查同时堵住了那个泄漏。
        """
        st = self._ev()
        refusals = 0
        try:
            while True:
                if st["stopping"]:
                    return
                started = False
                try:
                    started = await self.start_event_relay()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        "relay 启动失败（%s: %s）—— %.1fs 后重试。在此期间"
                        "scraper.scrape_outbox 会堆积，但一行都不会丢。",
                        type(e).__name__, e, RELAY_RETRY_SECONDS)

                if started:
                    refusals = 0
                    task = st["relay_task"]
                    if task is not None:
                        try:
                            await task
                        except asyncio.CancelledError:
                            # stop_event_relay() 取消的是 relay_task，不是本协程。
                            # 那是干净关停，不该再往上抛一个 CancelledError。
                            if st["stopping"]:
                                return
                            raise
                        except Exception:  # noqa: BLE001
                            pass          # _relay_main 已经记过日志并置了 failed
                    if st["stopping"]:
                        return
                    self._bump("relay_restarts")
                    logger.error(
                        "relay 主循环已退出（state=%s）—— %.1fs 后重新拉起。",
                        st["relay_state"], RELAY_RETRY_SECONDS)
                elif st["relay_state"] == "refused":
                    refusals += 1
                    self._log_relay_refusal(refusals)
                # start 还有两种返回 False 的方式，都**不是**"锁被别人占着"：
                # 被 stop 叫停（state='stopped'）和被更晚的一次 start 顶掉
                # （state 还是 'starting'）。给它们记一条"单例锁被占用"的
                # WARNING 是误导 —— 关服日志里出现"另一个实例没停干净"会让人
                # 去查一个根本不存在的进程。循环顶上的 stopping 检查负责收场。

                await self._sleep_until_stop(RELAY_RETRY_SECONDS)
        except asyncio.CancelledError:
            await self.stop_event_relay()
            raise

    def _log_relay_refusal(self, n: int) -> None:
        """拿不到单例锁的日志节流。

        每 5s 一条 WARNING 会在滚动部署期间刷屏，而这件事本身是**正常**的；
        但它也可能是"另一个实例忘了停"，所以不能一句不说。第一次、以及此后
        每 ~1 分钟说一次。
        """
        every = max(1, int(60.0 / max(RELAY_RETRY_SECONDS, 0.001)))
        if n == 1 or n % every == 0:
            logger.warning(
                "relay 单例锁被本库上的另一个会话持有，本进程继续等待接管"
                "（第 %d 次尝试，每 %.1fs 重试一次）。滚动部署时这是正常的；"
                "如果它一直不消失，说明有另一个实例没停干净。", n, RELAY_RETRY_SECONDS)

    async def _sleep_until_stop(self, seconds: float) -> None:
        """可被 stop 提前打断的睡眠。

        lifespan 是 ``create_task(_scrape_event_relay())`` —— 任务句柄被丢掉了，
        关服时只有 ``stop_event_relay()``，没人 cancel 本协程。所以重试间隔不能是
        一觉睡到底：那样进程会拖着一个 pending task 走完关闭流程。
        """
        st = self._ev()
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or st["stopping"]:
                return
            await asyncio.sleep(min(0.2, left))

    async def stop_event_relay(self) -> None:
        """取消 relay、释放单例锁、关掉专用连接。幂等，且**有界**。

        每一步都套了超时。理由不是洁癖：这个方法挂在 ``lifespan`` 的 yield 之后，
        在 ``db.close()`` **之前**——它一卡住，整个进程就停不下来，运维只能 kill -9，
        而 kill -9 会把 relay 那条连接留成服务端的 idle-in-transaction。
        取消发生在事务中途时，``_relay_drain_once`` 的 ``except BaseException``
        还要 ``await tx.rollback()``，asyncpg 那条路径要先跟服务端把取消协商完；
        正常是毫秒级，但它依赖网络，不能无界地等。
        超时了就放弃优雅路径直接关连接——连接一断，服务端会回滚未提交事务并释放
        advisory lock，语义上没有任何损失（认领的行留在 outbox，下一个 relay 接着捞）。

        第一句 ``stopping = True`` 是给**还没装好的那个 start** 看的：lifespan 里
        relay 是 ``create_task`` 起来的，停得足够快时它还卡在 ``asyncpg.connect()``
        里，``relay_task`` / ``relay_conn`` 都还是 None，本方法就什么也看不见。
        没有这个标志，那次 start 会在 ``db.close()`` **之后**才起来，留下一条连接
        和一把没人放的单例锁（见 start_event_relay 的注释与实测输出）。
        """
        st = self._ev()
        st["stopping"] = True
        task = st["relay_task"]
        st["relay_task"] = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), RELAY_STOP_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("relay 任务在 %.1fs 内没有响应取消，直接关连接收场",
                             RELAY_STOP_TIMEOUT)
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        conn = st["relay_conn"]
        st["relay_conn"] = None
        st["relay_state"] = "stopped"
        if conn is not None:
            try:
                # 显式解锁：连接一关锁自然就没了，但显式解一次能让「同一个进程里
                # stop 完立刻 start」这种用法（测试、热重载）不依赖连接关闭时机。
                await asyncio.wait_for(
                    conn.execute("SELECT pg_advisory_unlock($1)", RELAY_LOCK_KEY),
                    RELAY_STOP_TIMEOUT)
            except (asyncio.TimeoutError, Exception):  # noqa: B014
                pass
            try:
                await asyncio.wait_for(conn.close(), RELAY_STOP_TIMEOUT)
            except (asyncio.TimeoutError, Exception):  # noqa: B014
                conn.terminate()

    async def _relay_main(self, conn: asyncpg.Connection) -> None:
        st = self._ev()
        try:
            while True:
                t0 = time.monotonic()
                try:
                    n = await self._relay_tick(conn)
                except asyncio.CancelledError:
                    raise
                except _RelayHandedOver:
                    raise
                except Exception as e:  # noqa: BLE001
                    n = 0
                    self._bump("tick_errors")
                    st["consec_fail"] += 1
                    st["consec_tick_fail"] += 1
                    self._on_relay_failure(e)
                    if st["consec_tick_fail"] >= RELAY_RECOVER_AFTER:
                        conn = await self._relay_recover(conn)
                    await asyncio.sleep(RELAY_ERROR_BACKOFF)
                else:
                    st["consec_fail"] = 0
                    st["consec_tick_fail"] = 0
                    st["batch"] = RELAY_BATCH
                st["last_tick_ms"] = (time.monotonic() - t0) * 1000.0
                st["counters"]["ticks"] += 1
                st["tick"] += 1
                if n and n >= st["batch"]:
                    # 还有积压：不睡，但让出一次调度，别把事件循环饿死。
                    await asyncio.sleep(0)
                    continue
                await asyncio.sleep(RELAY_TICK_SECONDS)
        except asyncio.CancelledError:
            st["relay_state"] = "stopped"
            raise
        except _RelayHandedOver as e:
            # 重连时发现单例锁已经被别人拿走了：这不是故障，是交班。
            # 状态已经在 _relay_recover 里置成 'refused'，run_event_relay 会
            # 继续按 RELAY_RETRY_SECONDS 排队等下一次接管机会。
            logger.warning("relay 交班：%s", e)
            return
        except BaseException:
            # 循环体外面炸了（tick 自己的异常在里面就被吃掉并退避了）。
            # 这里**必须**与"正常停下来"区分开：两者都写 'stopped' 的话，
            # /api/_debug/event-stream 上一个已经死掉的 relay 与一个还没启动的
            # relay 长得一模一样，而这个端点存在的唯一理由就是让停摆变响。
            st["relay_state"] = "failed"
            logger.exception("relay 主循环异常退出——事件流已停止，outbox 将持续堆积")
            raise

    async def _relay_recover(self, dead: asyncpg.Connection) -> asyncpg.Connection:
        """连续 RELAY_RECOVER_AFTER 个 tick 全挂之后：换一条连接、重抢单例锁。

        修的是 B3。实测（``pg_terminate_backend`` 打掉 relay 自己那条会话）：

            after kill : events=50 outbox_backlog=70 state='running' tick_errors=14
            advisory locks still held on the singleton key : 0
            instance #1 still reports relay_state='running'

        数据是安全的（认领的行全在 outbox，零丢失，别的实例接管就能抽干），
        但这一条 relay 会**永远**每 tick 抛一次 ``InterfaceError: connection is
        closed``，而 ``relay_state`` 一直报 'running'。D-26 加的 'failed' 只覆盖
        「主循环整个抛出去了」，覆盖不到「每个 tick 都抛、循环还在转」。
        本仓库是单实例部署，所以那等于事件流永久停摆 + 唯一该报警的字段说一切正常。

        三种结局，每一种都必须是**响的**：
          * 重连成功且抢回锁 -> 继续跑，计一次 relay_reconnects；
          * 锁已经被别人拿走 -> 抛 _RelayHandedOver，state='refused'，本实例退位；
          * 连都连不上       -> 原样上抛，_relay_main 置 state='failed'。
        """
        st = self._ev()
        logger.error(
            "relay 连续 %d 个 tick 全部失败，判定连接已不可用：关掉旧连接、"
            "重开一条并重抢单例锁。", st["consec_tick_fail"])
        try:
            await asyncio.wait_for(dead.close(), RELAY_STOP_TIMEOUT)
        except (asyncio.TimeoutError, Exception):  # noqa: B014
            try:
                dead.terminate()
            except Exception:  # noqa: BLE001
                pass
        if st["relay_conn"] is dead:
            st["relay_conn"] = None

        try:
            fresh = await self._relay_open_conn()
        except asyncio.CancelledError:
            st["relay_state"] = "stopped"
            raise
        except BaseException:
            st["relay_state"] = "failed"
            logger.exception(
                "relay 重连失败——事件流已停摆，scrape_outbox 会持续堆积"
                "（行不会丢，恢复后会被重新认领）。")
            raise

        try:
            got = await fresh.fetchval("SELECT pg_try_advisory_lock($1)",
                                       RELAY_LOCK_KEY)
        except asyncio.CancelledError:
            # 与 start_event_relay 同一口径：取消途中也必须把锁和连接还回去，
            # 否则这个库上再也起不来 relay（下一个实例只会看到 refused）。
            await self._release_and_close(fresh)
            st["relay_state"] = "stopped"
            raise
        except BaseException:
            await self._release_and_close(fresh)
            st["relay_state"] = "failed"
            raise
        if not got:
            await self._release_and_close(fresh)
            st["relay_state"] = "refused"
            raise _RelayHandedOver(
                f"重连之后单例锁 {RELAY_LOCK_KEY} 已被本库上的另一个会话持有 —— "
                f"对方已经接管，本实例退出 relay 循环（绝不能两个写入者同时写 "
                f"scrape_events，那样游标保证当场失效）。")

        st["relay_conn"] = fresh
        st["relay_state"] = "running"
        st["consec_tick_fail"] = 0
        st["consec_fail"] = 0
        self._bump("relay_reconnects")
        logger.warning("relay 已重连并重新持有单例锁，事件流恢复（第 %d 次重连）。",
                       st["counters"]["relay_reconnects"])
        return fresh

    def _on_relay_failure(self, exc: BaseException) -> None:
        """失败分类：是「行的错」就缩批（准备隔离），不是就只吵。"""
        st = self._ev()
        if _is_row_fault(exc):
            st["batch"] = max(1, st["batch"] // 2)
            logger.error("relay 批次失败（疑似队头毒丸），批量缩到 %d：%s: %s",
                         st["batch"], type(exc).__name__, exc)
        else:
            logger.error(
                "relay 批次失败（**不是**某一行的错，行全部留在 outbox，零丢失）："
                "%s: %s", type(exc).__name__, exc)

    async def _relay_tick(self, conn: asyncpg.Connection) -> int:
        """一个 tick：（按需）维护分区 -> 抽干一批 -> 刷新深度指标。"""
        st = self._ev()
        if st["tick"] % RELAY_PARTITION_EVERY == 0:
            await self.ensure_event_partitions(conn)

        if (st["batch"] == 1
                and st["consec_fail"] >= RELAY_QUARANTINE_AFTER):
            await self._quarantine_head(conn)
            st["consec_fail"] = 0

        n = await self._relay_drain_once(conn, st["batch"])

        row = await conn.fetchrow(SQL_OUTBOX_DEPTH)
        st["outbox_depth"] = int(row["depth"])
        st["relay_lag_s"] = float(row["lag_s"])
        return n

    async def _relay_drain_once(self, conn: asyncpg.Connection, limit: int) -> int:
        """认领 + 落库 + 推进 max_seq_ever —— **一个事务**。

        崩在任何地方都只会回滚：DELETE 跟着回滚，认领的行原样留在 outbox，
        下一 tick（或下一个 relay 进程）重新认领。seq 会被烧掉几个号 —— 序列是
        非事务性的，**seq 有空洞是设计的一部分**，没有任何东西依赖它连续。
        （Phase 3 的 ``409 cursor_below_retention`` 判据
        ``after_seq + 1 < min_available_seq`` 会在保留期边界上被空洞误伤，
        那是 Phase 3 要处理的事，这里先记下。）
        """
        st = self._ev()
        tx = conn.transaction()
        await tx.start()
        try:
            claimed = await conn.fetch(SQL_CLAIM, limit)
            if not claimed:
                await tx.commit()
                return 0
            claimed = sorted(claimed, key=lambda r: r["id"])
            rows = [self._build_event_row(r) for r in claimed]
            inserted = await self._relay_write_events(conn, rows)
            if inserted:
                await conn.execute(
                    SQL_BUMP_MAX_SEQ, str(max(r["seq"] for r in inserted)))
            await tx.commit()
        except BaseException:
            # BaseException 而非 Exception：CancelledError 也必须回滚，
            # 否则那条 relay 连接会留下一个 idle-in-transaction，钉住 xmin
            # horizon 并且永远拿不回单例锁的语义（D-17 / _tx() 同一口径）。
            try:
                await tx.rollback()
            except Exception as re:  # noqa: BLE001
                # 回滚本身失败（多半是连接断了）。**不能**让它顶掉原始异常：
                # 上层的 _is_row_fault() 是按异常类型决定"要不要隔离队头那一行"的，
                # 拿到一个 InterfaceError 会把毒丸误判成连接问题（或者反过来）。
                # 事务没提交，行照样留在 outbox，连接断开时服务端也会自己回滚。
                logger.error("relay 事务回滚失败（原始异常照常上抛）：%s: %s",
                             type(re).__name__, re)
            raise

        n_in, n_claimed = len(inserted), len(claimed)
        self._bump("relayed", n_in)
        if n_in != n_claimed:
            self._bump("conflicts", n_claimed - n_in)
            logger.warning(
                "relay 认领 %d 行只落库 %d 行——差额被 ON CONFLICT DO NOTHING 吞了，"
                "意味着 source_id 重复（同一条记录被写了两遍）。这是坏数据的响铃。",
                n_claimed, n_in)
        st["rate"].append((time.monotonic(), n_in))
        _trim_rate(st["rate"])
        return n_claimed

    async def _relay_write_events(self, conn, rows: List[Dict[str, Any]]):
        """一条语句把整批写进 scrape_events。**必须**在 _relay_drain_once 的事务里。

        单独拆出来是为了让「relay 死在事务中途」可以被测试真实地驱动
        （tests/pgdb/test_relay.py 把它换成挂起 / 抛异常），而不是靠往生产代码里
        埋 test-only 钩子。
        """
        return await conn.fetch(SQL_INSERT_EVENTS, [dumps_body(r) for r in rows])

    def _build_event_row(self, claimed) -> Dict[str, Any]:
        """一行 outbox -> 一行 scrape_events 的字段字典。

        规矩：body 来自**提交上来的 data**，绝不回查 asin_data —— 那张表是
        跨时间合并出来的（rating / review_count / seller_id / seller_name 四个
        字段会从上一次采集结转），回查得到的是一条 franken-record，
        「一条记录 = 一次完整采集结果」当场不成立。
        """
        st = self._ev()
        raw = claimed["body"]
        body = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
        if not isinstance(body, dict):
            body = {}
        recorded_at: datetime = claimed["enqueued_at"]
        result = body.get("result")
        if not isinstance(result, dict):
            result = {}

        outcome, coerced = normalize_outcome(body.get("outcome"))
        if coerced:
            self._bump("outcome_coerced")
        marketplace, mk_coerced = normalize_marketplace(body.get("marketplace"))
        if mk_coerced:
            self._bump("marketplace_coerced")
            logger.warning("marketplace %r 不在封闭集里，纠正成 %s（source_id=%s）",
                           body.get("marketplace"), marketplace, body.get("source_id"))
        zip_requested, padded = normalize_zip(body.get("zip_requested"))
        if padded:
            self._bump("zip_padded")

        # ---- outcome：payload 的兜底纠正（升级窗口里入队的老 body）----
        outcome, recovered = reconcile_outcome(outcome, result)
        if recovered:
            self._bump("outcome_recovered_not_found")
            logger.warning(
                "body 记的 outcome 是 ok，但 payload 自称 not_found（source_id=%s "
                "asin=%s）——按 not_found 落库。多半是升级前入队的 outbox 行。",
                body.get("source_id"), body.get("asin"))

        # ---- Phase 4：四个采集质量信号 ----
        zip_observed, zo_coerced = normalize_zip_observed(
            result.get(EVENT_META_ZIP_OBSERVED))
        if zo_coerced:
            self._bump("zip_observed_coerced")
        zip_verify, zv_coerced = normalize_zip_verify(
            result.get(EVENT_META_ZIP_VERIFY))
        if zv_coerced:
            self._bump("zip_verify_coerced")
        zip_verify, downgraded = reconcile_zip_verify(
            zip_verify, zip_requested, zip_observed)
        if downgraded:
            self._bump("zip_verify_downgraded")
            logger.warning(
                "zip_verify=confirmed 但 zip_observed=%r != zip_requested=%r"
                "（source_id=%s）——降级成 mismatch。一条声称 A 而价格属于 B 的"
                "记录会把这个商品的价格序列悄悄劈成两组。",
                zip_observed, zip_requested, body.get("source_id"))
        completeness, c_coerced = normalize_completeness(
            result.get(EVENT_META_COMPLETENESS))
        if c_coerced:
            self._bump("completeness_coerced")
        parse_engine, pe_coerced = normalize_parse_engine(
            result.get(EVENT_META_PARSE_ENGINE))
        if pe_coerced:
            self._bump("parse_engine_coerced")

        collected_at, form = parse_crawl_time(result.get("crawl_time"))
        if collected_at is None:
            collected_at = recorded_at
            self._bump("collected_at_fallback")
        counter = _CRAWL_TIME_COUNTER.get(form)
        if counter:
            self._bump(counter)

        # outcome != 'ok' 时两个哈希都写 NULL —— 这比计划 §2.1（只说了
        # review_hash）更严一点，是有意的：not_found 的 payload 有 30/40 个
        # 占位符，对它算 slow_hash 得到的是一个「好页→404→好页」每次都翻转的值，
        # 正是 §4.3 那道合取门要防的误复审模式。
        review_hash = slow_hash = None
        if outcome == "ok":
            review_hash, slow_hash = st["hash_adapter"].compute(result, outcome)

        return {
            "source_id": body.get("source_id") or f"{st['gen']}:{uuid.uuid4()}",
            "gen": body.get("gen") or st["gen"],
            "asin": body.get("asin") or "",
            "marketplace": marketplace,
            "zip_requested": zip_requested,
            "zip_observed": zip_observed,
            "zip_verify": zip_verify,
            "collected_at": collected_at.isoformat(),
            "recorded_at": recorded_at.isoformat(),
            "outcome": outcome,
            "completeness": completeness,
            "error_type": body.get("error_type"),
            "error_detail": body.get("error_detail"),
            "batch_id": body.get("batch_id"),
            "task_id": body.get("task_id"),
            "worker_id": body.get("worker_id"),
            "attempt": body.get("attempt") or 0,
            "parse_engine": parse_engine,
            "review_hash": review_hash,
            "slow_hash": slow_hash,
            "hash_ver": st["hash_adapter"].hash_ver,
            "payload": result,
        }

    async def _quarantine_head(self, conn: asyncpg.Connection) -> Optional[int]:
        """把队头那一行搬进死信表。见模块头注释第四节。"""
        reason = f"relay 连续 {RELAY_QUARANTINE_AFTER} 次以单行批量失败"
        try:
            row = await conn.fetchrow(SQL_QUARANTINE_HEAD, reason)
        except Exception as e:  # noqa: BLE001
            logger.error("隔离队头失败（事件流仍然停摆）：%s: %s", type(e).__name__, e)
            return None
        if row is None:
            return None
        self._bump("quarantined")
        logger.error(
            "outbox 队头 id=%s 已隔离进 scraper.scrape_outbox_dead（原因：%s）。"
            "行没有丢，人工排查后可以重放。事件流继续。", row["id"], reason)
        return int(row["id"])

    # ======================================================
    # 4) 分区维护
    # ======================================================
    async def ensure_event_partitions(self, conn=None, *,   # noqa: C901
                                      floor_seq: Optional[int] = None) -> int:
        """保证「下界 > 当前 last_value」的未来分区不少于 EVENT_FUTURE_PARTITIONS。

        跑在 relay 自己的连接上（也就是持着单例锁的那条），所以两个实例不可能
        同时建同一个分区；引导期没有 relay，靠 ``IF NOT EXISTS`` + 重复建表的
        异常吞掉兜底。

        配方是 create-then-ATTACH，模板是**父表**（外加显式补一条 source_id
        唯一索引）—— 理由见 schema.event_next_partition_sql() 的长注释：抄上一个
        分区会把它自己的 range CHECK 一起抄过来，从 p2 起每个分区都永久不可写。
        """
        conn = conn or self._write_conn
        await self._repair_foreign_range_checks(conn)
        parts = await self._list_partitions(conn)
        if not parts:
            raise RuntimeError("scraper.scrape_events 一个分区都没有")

        last_value = await self._seq_last_value(conn)
        if floor_seq is not None:
            last_value = max(last_value, floor_seq)

        created = 0
        for _ in range(16):                     # 防跑飞
            future = [p for p in parts if p[1] is not None and p[1] > last_value]
            if len(future) >= EVENT_FUTURE_PARTITIONS:
                break
            top = max(p[2] for p in parts if p[2] is not None)
            nxt_index = max(p[3] for p in parts) + 1
            lo, hi = top, top + EVENT_PARTITION_SPAN
            if await self._create_partition(conn, nxt_index, lo, hi):
                created += 1
            parts = await self._list_partitions(conn)
        else:
            logger.error("分区维护循环达到上限，last_value=%s 分区=%s",
                         last_value, [p[0] for p in parts])

        if created:
            logger.info("事件流新建 %d 个分区（last_value=%s，未来分区目标 %d）",
                        created, last_value, EVENT_FUTURE_PARTITIONS)
        return created

    async def _create_partition(self, conn, index: int, lo: int, hi: int) -> bool:
        """建表 -> 补唯一索引 -> 预挂 CHECK -> ATTACH。四步各自幂等，中途崩了下次能接着做。

        为什么要逐步容错：上一次跑到「表建好了、还没 ATTACH」就崩，
        下一次 ``CREATE TABLE IF NOT EXISTS`` 会安静地什么都不做，而
        ``ADD CONSTRAINT`` 会抛 DuplicateObject。若整段共用一个 except 直接
        return，这个分区就**永远**挂不上去，分区维护从此原地打转——一个可自愈的
        中断被固化成永久故障。
        """
        name = event_partition_name(index)
        create_sql, index_sql, check_sql, attach_sql = event_next_partition_sql(
            index, lo, hi)
        await conn.execute(create_sql)                    # IF NOT EXISTS，天然幂等
        await conn.execute(index_sql)                     # IF NOT EXISTS，天然幂等
        try:
            await conn.execute(check_sql)
        except asyncpg.DuplicateObjectError:
            logger.info("分区 %s 的 CHECK 已存在（上次中断在 ATTACH 之前），继续", name)
        try:
            await conn.execute(attach_sql)
        except asyncpg.DuplicateTableError:
            logger.info("分区 %s 已经挂在父表上（多进程同时引导），跳过", name)
            return False

        await self._assert_partition_is_sane(conn, name, lo, hi)
        self._bump("partitions_created")
        logger.info("事件流新分区 scraper.%s [%s, %s)", name, lo, hi)
        return True

    async def _assert_partition_is_sane(self, conn, name: str,
                                        lo: int, hi: int) -> None:
        """新分区的硬闸门。两条，都是实测踩出来的，都**不报错地**发生：

        1. **source_id 唯一索引在不在。** 那条索引长在分区上（父表建不了 —— 声明式分区
           要求唯一约束包含分区键 seq），LIKE 抄错模板就会得到一个没有重复保护的
           分区，而且一声不吭。
        2. **有且只有一条属于自己的 range CHECK。** 这是 B1：
           ``LIKE <上一个分区> INCLUDING ALL`` 会把模板自己的 range CHECK 抄进来，
           两条互斥 CHECK 让这个分区**永远收不下任何行**，而 DDL 全部成功。
           上一版闸门只看索引，所以整条流在 seq=40,000,000 处停摆这件事一路绿灯
           走到了验证环节。宁可现在炸。
        """
        defs = [r["indexdef"] for r in await conn.fetch(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'scraper' AND tablename = $1", name)]
        if not any("UNIQUE" in d and "source_id" in d for d in defs):
            raise RuntimeError(
                f"新分区 scraper.{name} 没有 source_id 唯一索引，拒绝继续。"
                f"实际索引：{defs}")

        checks = await conn.fetch(EVENT_OWN_RANGE_CHECK_SQL, name)
        got = [(r["conname"], r["condef"]) for r in checks]
        if len(got) != 1 or got[0][0] != f"{name}_range":
            raise RuntimeError(
                f"新分区 scraper.{name} 的 range CHECK 不是恰好一条自己的："
                f"{got}。两条互斥的 CHECK = 这个分区永远收不下任何行"
                f"（Phase 2 BLOCKER B1）。期望边界 [{lo}, {hi})。")

    async def _repair_foreign_range_checks(self, conn) -> int:
        """把「别的分区的 range CHECK」从分区身上摘掉。返回摘掉的条数。

        为什么代码修好了还需要这个：B1 修的是**将来**怎么建分区，而 p1/p2 是
        ``connect()`` 期就建出来的 —— **每一个** Phase 2 建过的库里，p2 身上都已经
        长着一条 ``scrape_events_p1_range``，它自己永远收不下行。只改 DDL 的话，
        已有的库（包括 scraper_dev、以及任何跑过 Phase 2 的部署）修不好。

        安全性：只认 schema 自己生成的名字（``scrape_events_p<N>_range``），
        且只摘「名字不等于自己那条」的。别人的 CHECK、父表的 marketplace CHECK
        都不在模式内。DROP CONSTRAINT 要 ACCESS EXCLUSIVE，所以先查后改 ——
        没有可摘的（修好之后的常态）时一条 DDL 都不发。
        """
        try:
            rows = await conn.fetch(EVENT_FOREIGN_RANGE_CHECKS_SQL)
        except Exception as e:  # noqa: BLE001
            logger.warning("分区 CHECK 体检失败（跳过本次修复）：%s: %s",
                           type(e).__name__, e)
            return 0
        n = 0
        for r in rows:
            part, con = r["partition"], r["conname"]
            try:
                await conn.execute(
                    f'ALTER TABLE scraper."{part}" DROP CONSTRAINT "{con}"')
            except Exception as e:  # noqa: BLE001
                logger.error("摘除 scraper.%s 上的越界 CHECK %s 失败：%s: %s",
                             part, con, type(e).__name__, e)
                continue
            n += 1
            logger.error(
                "修复 B1：从分区 scraper.%s 上摘掉了属于别的分区的 CHECK %s（%s）。"
                "在此之前这个分区永远收不下任何行，事件流会在它的区间上永久停摆。",
                part, con, r["condef"])
        if n:
            self._bump("range_checks_repaired", n)
        return n

    async def _seq_last_value(self, conn) -> int:
        """seq 序列已经发到哪儿了。见 SQL_SEQ_NAME 的两个陷阱。

        名字来自 ``pg_get_serial_sequence``（目录输出，已按需加引号），
        不是用户输入，直接内插是安全的 —— 序列名不能当参数绑定。
        """
        name = await conn.fetchval(SQL_SEQ_NAME)
        if not name:
            return 0
        return int(await conn.fetchval(f"SELECT last_value FROM {name}") or 0)

    async def _seq_high_water(self, conn) -> int:
        """seq 序列**真正发出去过**的最大值（一次都没发过就是 0）。B4 的判据。

        与 ``_seq_last_value`` 的区别只在一处，但那一处是要害：新建/刚 setval 过的
        序列 ``last_value = 1`` 而 ``is_called = false``，意思是「下一个发 1」，
        也就是**一个号都还没发**。分区维护要的是「下一个号落在哪」，用 last_value
        正好；回退检测要的是「发出去过的最高号」，多算一个就会把一个全新的库
        误判成「有过 seq=1」。
        （``pg_sequences.last_value`` 在 is_called=false 时是 NULL，所以这里跟
        ``_seq_last_value`` 一样直接读序列关系本身，见 SQL_SEQ_NAME 的注释。）
        """
        name = await conn.fetchval(SQL_SEQ_NAME)
        if not name:
            return 0
        row = await conn.fetchrow(f"SELECT last_value, is_called FROM {name}")
        if row is None:
            return 0
        last = int(row["last_value"] or 0)
        return last if row["is_called"] else max(last - 1, 0)

    async def _list_partitions(self, conn) -> List[Tuple[str, Optional[int],
                                                         Optional[int], int]]:
        """[(name, lo, hi, index)]，按 index 排序。lo 为 None 表示 MINVALUE。"""
        rows = await conn.fetch(EVENT_LIST_PARTITIONS_SQL)
        out = []
        for r in rows:
            idx = _partition_index(r["name"])
            if idx is None:
                continue
            lo, hi = parse_partition_bound(r["bound"])
            out.append((r["name"], lo, hi, idx))
        out.sort(key=lambda p: p[3])
        return out

    # ======================================================
    # 5) 指标
    # ======================================================
    def event_relay_metrics(self) -> Dict[str, Any]:
        """纯内存计数器。同步、不碰库，可以在任何地方调。"""
        st = self._ev()
        return {
            "gen": st["gen"],
            "instance_id": st["instance_id"],
            "ready": st["ready"],
            "relay_state": st["relay_state"],
            "relay_batch": st["batch"],
            # 连续失败的 tick 数。relay_state 只在到达 RELAY_RECOVER_AFTER 之后
            # 才会变（重连 / failed / refused），在那之前这个数字是唯一的先兆。
            "consec_tick_fail": st["consec_tick_fail"],
            "last_tick_ms": round(st["last_tick_ms"], 3),
            "outbox_depth": st["outbox_depth"],
            "relay_lag_s": round(st["relay_lag_s"], 3),
            "events_per_minute": self._events_per_minute(),
            "hash_backend": "common.slowhash" if st["hash_adapter"].available else None,
            "counters": dict(st["counters"]),
        }

    def _events_per_minute(self) -> float:
        st = self._ev()
        _trim_rate(st["rate"])
        if not st["rate"]:
            return 0.0
        total = sum(n for _, n in st["rate"])
        span = max(time.monotonic() - st["rate"][0][0], 1e-9)
        return round(total * 60.0 / min(max(span, 1.0), RELAY_RATE_WINDOW), 2)

    async def event_stream_stats(self) -> Dict[str, Any]:
        """内存指标 + 一次查库（outbox 深度 / 滞后 / seq 窗口 / 分区）。

        Phase 3 的 ``GET /api/v1/sync/status`` 直接用这个，别再另写一份。
        读用池连接，不碰写连接、不碰 _write_lock。
        """
        out = self.event_relay_metrics()
        async with self.read() as rc:
            async with rc.execute(SQL_OUTBOX_DEPTH) as cur:
                row = await cur.fetchone()
            out["outbox_depth"] = int(row["depth"])
            out["relay_lag_s"] = round(float(row["lag_s"]), 3)

            async with rc.execute(
                "SELECT COALESCE(max(seq),0) AS max_seq, "
                "COALESCE(min(seq),0) AS min_seq, count(*) AS n "
                "FROM scraper.scrape_events") as cur:
                row = await cur.fetchone()
            out["max_seq"] = int(row["max_seq"])
            out["min_available_seq"] = int(row["min_seq"])
            out["events_total"] = int(row["n"])

            async with rc.execute(
                "SELECT count(*) AS n FROM scraper.scrape_outbox_dead") as cur:
                row = await cur.fetchone()
            out["dead_letters"] = int(row["n"])

            async with rc.execute(
                "SELECT k, v FROM scraper.sync_meta ORDER BY k") as cur:
                rows = await cur.fetchall()
            out["sync_meta"] = {r["k"]: r["v"] for r in rows}

            async with rc.execute(EVENT_LIST_PARTITIONS_SQL) as cur:
                rows = await cur.fetchall()
        parts = []
        for r in rows:
            lo, hi = parse_partition_bound(r["bound"])
            parts.append({"name": r["name"], "lo": lo, "hi": hi})
        parts.sort(key=lambda p: (p["lo"] is not None, p["lo"] or 0))
        out["partitions"] = parts
        out["future_partitions"] = sum(
            1 for p in parts if p["lo"] is not None and p["lo"] > out["max_seq"])
        out["contract_version"] = out["sync_meta"].get("contract_version")
        return out


# ============================================================
# 小工具
# ============================================================

def _as_int_or_none(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _trim_rate(rate: deque) -> None:
    cutoff = time.monotonic() - RELAY_RATE_WINDOW
    while rate and rate[0][0] < cutoff:
        rate.popleft()


#: 「不是某一行的错」的错误：分区溢出、连接问题、事务已 abort。
#: 撞上这些**绝不**隔离任何行——分区溢出必须停下来等人建分区，
#: 把行扔进死信表只会把一次可修复的停摆变成一次真的数据搬家。
_NOT_ROW_FAULT = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.InFailedSQLTransactionError,
    asyncio.TimeoutError,
    OSError,
)


def _is_row_fault(exc: BaseException) -> bool:
    if isinstance(exc, _NOT_ROW_FAULT):
        return False
    if isinstance(exc, asyncpg.CheckViolationError):
        # "no partition of relation ... found for row" 也是 23514，
        # 但它是分区余量的问题，不是这一行的问题。
        if "no partition of relation" in str(exc).lower():
            return False
        return True
    return isinstance(exc, (asyncpg.DataError, asyncpg.PostgresError, ValueError,
                            TypeError, json.JSONDecodeError))


__all__ = [
    "EventStreamMixin",
    "NOT_FOUND_TITLE",
    "RELAY_LOCK_KEY",
    "RELAY_BATCH",
    "RELAY_TICK_SECONDS",
    "classify_success_outcome",
    "outcome_for_error_type",
    "payload_says_not_found",
    "reconcile_outcome",
    "normalize_outcome",
    "normalize_marketplace",
    "normalize_zip",
    "normalize_zip_observed",
    "normalize_zip_verify",
    "reconcile_zip_verify",
    "normalize_completeness",
    "normalize_parse_engine",
    "parse_crawl_time",
    "parse_collected_at",
    "parse_partition_bound",
    "parse_hash_ver",
    "scrub_nul",
    "dumps_body",
]
