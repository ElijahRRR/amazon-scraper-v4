"""common/pgdb/media.py —— 截图 + 卖家发现（F-009）+ 关键词发现（F-010）+ 变体展开。

OWNS（13 个方法）:
    create_search_batch            database.py（F-010，无 SQLite 行号：与本文件同批新增）
    accept_search_discovery_result database.py（同上）
    get_search_batch_progress      database.py（同上）
    get_pending_screenshots        database.py:2291
    update_screenshot_status       database.py:2298
    get_screenshot_progress        database.py:2333
    _get_done_screenshot_path      database.py:2008   ← 走 self._db（写连接）
    _get_done_screenshot_paths     database.py:2033   ← 走 self.read()
    _hydrate_screenshot_paths      database.py:2071   ← **原地修改** items
    create_seller_batch            database.py:1443
    accept_seller_discovery_result database.py:1500
    get_seller_batch_progress      database.py:1616
    expand_batch_variants          database.py:851    ← 调 self.create_tasks

对外欠账（别人依赖本文件）:
    results_write.py -> _get_done_screenshot_path(asin, batch_id)
    results_read.py  -> _hydrate_screenshot_paths(items, batch_id)
依赖别人:
    tasks.py -> create_tasks(...)

--------------------------------------------------------------------------
⚠ 本文件的方法**黄金基线一条都没覆盖**
--------------------------------------------------------------------------
64 步场景里没有任何一步碰截图落地、seller 发现或变体展开。
所以：每个方法都要自己写 pytest，逐键断言返回形状（见 tests/pgdb/helpers.py
提供的 scratch 库夹具）。没有网可以兜着。
→ tests/pgdb/test_media.py 对每个方法做 SQLite/PG 双跑逐字段比对。

--------------------------------------------------------------------------
移植要点
--------------------------------------------------------------------------
* _get_done_screenshot_path 用 ``self._db``、_get_done_screenshot_paths 用
  ``self.read()``。两者不同**不是**笔误，保持原样。
  （前者的唯一调用点在 _save_result_inner_unlocked 里，那时已经持有写锁并且
   正处在写事务中间——必须走同一条写连接才能看见未提交的自身写入。）
* 两者都靠 ``ORDER BY updated_at DESC, id DESC LIMIT 1`` 决定"哪张截图胜出"。
  PG 的 DESC 默认 NULLS FIRST——一行 updated_at 为 NULL 就会窜到最前面，
  改掉每一条结果/导出里的 screenshot_path。**必须**写成
  ``ORDER BY updated_at DESC NULLS LAST, id DESC``（SQLite 的 DESC 是
  NULLS LAST）。id 是 PRIMARY KEY 非空，不用补。
* _hydrate_screenshot_paths 是**原地改** items 的（没有返回值），
  并且会把无效占位值统一成 None（_shared 的 _normalize_screenshot_path）。
* update_screenshot_status 里两条 UPDATE 的加锁顺序照抄，返回 bool。
  rowcount 来自 ConnProxy，已经是 int。
* create_seller_batch:
  - discover_mode 非法值 raise ValueError；空 seller_ids 返回 (0, 0)。
  - 返回 **2-tuple** ``(batch_id, inserted)``，app.py:1067 直接解包。
  - ``inserted`` 同样来自 total_changes 差值 → 换成单条 set-based INSERT +
    命令标签（做法见 tasks.py 的 create_tasks 说明）。
  - SELECT id 在**事务内**发（与 create_batch 不同，那边是 COMMIT 之后）——
    照抄，不要对齐。
* accept_seller_discovery_result:
  - 返回 dict **原样**就是 HTTP 响应体（app.py:1626 ``return result``），键是
    {"accepted", "stale", "discovered", "new_asins", "detail_tasks_created"}。
  - ``detail_tasks_created`` 又是一个 total_changes 差值。
  - lease 门与 results_write 那条同构：rowcount==0 即 stale，必须是 int。
  - seller_discoveries 是复合主键，``ON CONFLICT DO NOTHING`` 有合法仲裁者。
* get_seller_batch_progress 返回
  {batch_id, discover: {...}, detail: {...}, discovered_asins, discover_mode}。
* expand_batch_variants 返回新增任务数，内部调 ``self.create_tasks``。
  app.py:380 的 _completion_watcher 会循环调用它直到收敛，返回值是收敛判据。
* seller_discoveries.discovered_at 是唯一一个**依赖列默认值**并直接出现在
  响应里的时间戳（app.py:1106），同时还是 ORDER BY 键（app.py:1108）。
  schema.py 已经把默认值写成 to_char(clock_timestamp() AT TIME ZONE 'UTC', ...)。

--------------------------------------------------------------------------
两处**有意的、非行为性**偏离（其余全部逐字移植，含 bug）
--------------------------------------------------------------------------
1. total_changes 差值 → 单条 ``INSERT ... SELECT unnest(...) ON CONFLICT
   DO NOTHING`` 读命令标签。源行数 = 尝试插入次数 → identity 烧号与
   SQLite 的 executemany(INSERT OR IGNORE) 完全一致；标签里的计数 =
   真正落库行数 = total_changes 差值。**绝不预过滤已存在的行**。
2. update_screenshot_status 原版没有 try/except（database.py:2302-2331）。
   SQLite 下一条语句报错只是把连接留在事务里，下一次 BEGIN 会报错但至少
   语句还能跑；PG 下事务一旦 abort，这条**专用写连接**上的后续语句会全部
   InFailedSQLTransactionError，整个进程的写路径就此瘫痪。所以这里补了
   ``except: ROLLBACK; raise``。返回值、异常类型、成功路径**都没有变**，
   变的只是"报错之后连接还能不能用"。
"""
from __future__ import annotations

import json
import logging
from common.core.timeutil import now_ts
from typing import Any, Dict, List, Optional, Tuple

from common import config
from common.core.searchurl import normalize_keyword
from common.pgdb._shared import _normalize_screenshot_path

logger = logging.getLogger(__name__)


class MediaMixin:
    """只定义方法，绝不定义 __init__。"""

    # ==================== 变体展开 ====================

    async def expand_batch_variants(self, batch_id: int) -> int:
        """变体自动展开：把本批【已采 ASIN 的同族变体】（variation_asins）入队进【同一批次】。

        仅对开启 expand_variants 的批次生效；其余返回 0。
        去重：只入队不在本批 batch_asins 里的 ASIN；底层 create_tasks 用
        ON CONFLICT DO NOTHING(UNIQUE(batch_id,asin)) 再兜一层 → 数学上保证收敛、绝不死循环。
        返回本次实际新增的任务数（0 = 没有新变体，批次可真正完成）。
        正常调用时机：批次本轮全部终态后（completion watcher）。
        """
        bid = self.as_int(batch_id)
        async with self.read() as rc:
            async with rc.execute(
                "SELECT needs_screenshot, expand_variants FROM batches WHERE id=?",
                (bid,)
            ) as c:
                brow = await c.fetchone()
            if not brow or not brow["expand_variants"]:
                return 0
            ss = bool(brow["needs_screenshot"])
            # 继承本批已有任务的 zip（同族应同邮编）
            async with rc.execute(
                "SELECT zip_code FROM tasks WHERE batch_id=? LIMIT 1", (bid,)
            ) as c:
                zrow = await c.fetchone()
            zip_code = (zrow["zip_code"] if zrow else None) or config.DEFAULT_ZIP_CODE
            # 本批已采 ASIN 的同族列表（twister 洗干净的真家族）
            async with rc.execute(
                "SELECT d.variation_asins FROM asin_data d "
                "JOIN batch_asins ba ON ba.asin = d.asin "
                "WHERE ba.batch_id=? AND d.variation_asins IS NOT NULL AND d.variation_asins != ''",
                (bid,)
            ) as c:
                fam_rows = await c.fetchall()
            # 本批已有 ASIN（去重基准）
            async with rc.execute(
                "SELECT asin FROM batch_asins WHERE batch_id=?", (bid,)
            ) as c:
                existing = {r["asin"].upper() for r in await c.fetchall()}

        # 安全阀：按【单个产品家族】计数。某产品的候选同族变体数超过上限，
        # 视为巨型/定制类家族（如定制尺寸产品，单族可达数千），跳过该产品的展开，
        # 防止一个种子炸成几千任务。正常小家族（≤上限）照常展开。
        cap = config.VARIANT_EXPAND_FAMILY_CAP
        candidates = set()
        skipped_big = 0
        for r in fam_rows:
            sibs = [a.strip() for a in (r["variation_asins"] or "").split(",")
                    if a.strip() and a.strip().upper() not in existing]
            if len(sibs) > cap:
                skipped_big += 1
                continue  # 超大家族：跳过，不入队
            candidates.update(sibs)
        if skipped_big:
            logger.warning(
                f"批次 {batch_id}: 跳过 {skipped_big} 个超大变体家族（单族新增 >{cap}，"
                f"疑似定制/巨型 listing），不自动展开")
        if not candidates:
            return 0
        # 复用 create_tasks：ON CONFLICT DO NOTHING tasks + 维护 batch_asins + 截图任务，返回实际新增数
        return await self.create_tasks(batch_id, sorted(candidates), zip_code, ss)

    # ==================== 卖家店铺采集（F-009）====================

    async def create_seller_batch(self, name: str, seller_ids: List[str],
                                  discover_mode: str = "with_detail",
                                  zip_code: str = "10001",
                                  needs_screenshot: bool = False) -> Tuple[int, int]:
        """创建一个 seller_discovery 类型的批次，并为每个 seller_id 插入一个 discover_seller 任务。

        - 衍生的 ASIN 详情任务在 discover 任务完成时由 accept_seller_discovery_result 动态插入。
        - 截图开关 needs_screenshot 应用于衍生的详情任务，discover 任务本身不截图。

        Returns: (batch_id, inserted_seller_task_count)
        """
        if discover_mode not in ("discover_only", "with_detail"):
            raise ValueError(f"非法 discover_mode: {discover_mode}")

        clean_ids = []
        seen = set()
        for sid in seller_ids:
            sid = (sid or "").strip().upper()
            if sid and sid not in seen:
                clean_ids.append(sid)
                seen.add(sid)
        if not clean_ids:
            return (0, 0)

        ss_val = 1 if needs_screenshot else 0
        name_p = self.text_affinity(name)
        mode_p = self.text_affinity(discover_mode)
        zip_p = self.text_affinity(zip_code)
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                await self._db.execute(
                    "INSERT INTO batches (name, needs_screenshot, is_auto, batch_type, discover_mode) "
                    "VALUES (?, ?, 0, 'seller_discovery', ?) "
                    "ON CONFLICT DO NOTHING",
                    (name_p, ss_val, mode_p)
                )
                # 同名重传：上面被 DO NOTHING 吞掉，这里取回**已存在**的 id
                # （与 create_batch 同构）。冲突时 identity 照样烧号。
                async with self._db.execute("SELECT id FROM batches WHERE name = ?", (name_p,)) as c:
                    row = await c.fetchone()
                    batch_id = row[0] if row else 0
                if not batch_id:
                    await self._db.execute("ROLLBACK")
                    return (0, 0)

                # total_changes 差值 → 单条 set-based INSERT 读命令标签。
                # unnest 的源行数 = 尝试插入次数，烧号与逐行 INSERT OR IGNORE 一致。
                cursor = await self._db.execute(
                    "INSERT INTO tasks "
                    "(batch_id, asin, zip_code, needs_screenshot, task_type) "
                    "SELECT ?::bigint, u.asin, ?::text, 0, 'discover_seller' "
                    "  FROM unnest(?::text[]) AS u(asin) "
                    "ON CONFLICT DO NOTHING",
                    (batch_id, zip_p, clean_ids)
                )
                inserted = cursor.rowcount
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return (batch_id, inserted)

    async def accept_seller_discovery_result(self, task_id: int, worker_id: str,
                                             lease_epoch: int, batch_id: int,
                                             seller_id: str,
                                             items: List[Dict],
                                             meta: Optional[Dict] = None) -> Dict:
        """接受 discover_seller 任务结果。

        items: [{"asin","title","price","image"}, ...]
        meta:  {"pages_scanned": N, "truncated": bool}

        副作用：
          1. 写入 seller_discoveries
          2. 若 batch.discover_mode='with_detail'，插入对应的 ASIN 详情任务（task_type='asin'）
          3. 标记 discover 任务为 done（校验 worker_id + lease_epoch）

        Returns: {"accepted":bool,"stale":bool,"discovered":int,"new_asins":int,"detail_tasks_created":int}
        """
        now = now_ts()
        tid = self.as_int(task_id)
        wid = self.text_affinity(worker_id)
        epoch = self.as_int(lease_epoch)
        bid = self.as_int(batch_id)
        sid = self.text_affinity(seller_id)
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # Step 1: 校验 lease 并标 done
                meta_json = json.dumps(meta) if meta else None
                cursor = await self._db.execute(
                    "UPDATE tasks SET status='done', updated_at=?, task_meta=? "
                    "WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                    (now, meta_json, tid, wid, epoch)
                )
                if cursor.rowcount == 0:
                    await self._db.execute("ROLLBACK")
                    return {"accepted": False, "stale": True,
                            "discovered": 0, "new_asins": 0, "detail_tasks_created": 0}

                # Step 2: 读 batch 元信息
                async with self._db.execute(
                    "SELECT discover_mode, needs_screenshot, name FROM batches WHERE id=?",
                    (bid,)
                ) as c:
                    brow = await c.fetchone()
                discover_mode = brow["discover_mode"] if brow else None
                needs_screenshot = bool(brow["needs_screenshot"]) if brow else False
                ss_val = 1 if needs_screenshot else 0

                # Step 3: 写 seller_discoveries（去重靠 PK）
                disc_rows = []
                seen_asins = set()
                for it in items or []:
                    asin = (it.get("asin") or "").strip().upper()
                    if not asin or asin in seen_asins:
                        continue
                    seen_asins.add(asin)
                    disc_rows.append((
                        bid, sid, asin,
                        self.text_affinity((it.get("title") or "")[:1000]),
                        self.text_affinity((it.get("price") or "")[:64]),
                        self.text_affinity((it.get("image") or "")[:1000]),
                    ))
                if disc_rows:
                    # 复合主键 (batch_id, seller_id, asin) 是合法仲裁者。
                    # 这里不需要"插了几行"，所以保留 executemany 的形状。
                    await self._db.executemany(
                        "INSERT INTO seller_discoveries "
                        "(batch_id, seller_id, asin, list_title, list_price, list_image) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT DO NOTHING",
                        disc_rows
                    )

                # Step 4: 若 with_detail，为每个 ASIN 插入详情任务（同 batch_id）
                detail_inserted = 0
                if discover_mode == "with_detail" and seen_asins:
                    # 取该 task 的 zip_code 复用
                    async with self._db.execute(
                        "SELECT zip_code FROM tasks WHERE id=?", (tid,)
                    ) as c:
                        zrow = await c.fetchone()
                    zip_code = zrow["zip_code"] if zrow else "10001"

                    # total_changes 差值 → set-based INSERT + 命令标签。
                    # seen_asins 是 set，原版 executemany 也按 set 迭代序插入，
                    # 这里同样用 list(seen_asins)：尝试次数一致 → 烧号一致。
                    detail_asins = list(seen_asins)
                    cursor = await self._db.execute(
                        "INSERT INTO tasks "
                        "(batch_id, asin, zip_code, needs_screenshot, task_type) "
                        "SELECT ?::bigint, u.asin, ?::text, ?::int, 'asin' "
                        "  FROM unnest(?::text[]) AS u(asin) "
                        "ON CONFLICT DO NOTHING",
                        (bid, self.text_affinity(zip_code), ss_val, detail_asins)
                    )
                    detail_inserted = cursor.rowcount

                    # 维护 batch_asins 关联（is_new 判定参考 create_tasks）
                    for a in seen_asins:
                        async with self._db.execute(
                            "SELECT 1 FROM asin_data WHERE asin=?", (a,)
                        ) as c:
                            exists = await c.fetchone()
                        await self._db.execute(
                            "INSERT INTO batch_asins (batch_id, asin, is_new) VALUES (?, ?, ?) "
                            "ON CONFLICT DO NOTHING",
                            (bid, a, 0 if exists else 1)
                        )

                    if needs_screenshot:
                        await self._db.executemany(
                            "INSERT INTO screenshots (asin, batch_id) VALUES (?, ?) "
                            "ON CONFLICT DO NOTHING",
                            [(a, bid) for a in seen_asins]
                        )

                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        return {
            "accepted": True, "stale": False,
            "discovered": len(seen_asins),
            "new_asins": len(seen_asins),  # 旧 batch 的 PK 冲突会被 ON CONFLICT DO NOTHING 吞掉，这里返回本次提交的去重总数
            "detail_tasks_created": detail_inserted,
        }

    async def get_seller_batch_progress(self, batch_id: int) -> Dict:
        """seller_discovery 批次的混合进度：discover 任务 + 衍生的 detail 任务 + 发现总数。"""
        out = {
            "batch_id": batch_id,
            "discover": {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0},
            "detail":   {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0},
            "discovered_asins": 0,
            "discover_mode": None,
        }
        bid = self.as_int(batch_id)
        async with self.read() as rc:
            async with rc.execute(
                "SELECT discover_mode FROM batches WHERE id=?", (bid,)
            ) as c:
                row = await c.fetchone()
                if row:
                    out["discover_mode"] = row["discover_mode"]

            async with rc.execute(
                "SELECT task_type, status, COUNT(*) as cnt FROM tasks "
                "WHERE batch_id=? GROUP BY task_type, status",
                (bid,)
            ) as c:
                async for row in c:
                    tt = row["task_type"] or "asin"
                    bucket = out["discover"] if tt == "discover_seller" else out["detail"]
                    bucket[row["status"]] = row["cnt"]

            for bucket in (out["discover"], out["detail"]):
                bucket["total"] = sum(bucket[k] for k in ("pending", "processing", "done", "failed"))

            async with rc.execute(
                "SELECT COUNT(*) FROM seller_discoveries WHERE batch_id=?", (bid,)
            ) as c:
                row = await c.fetchone()
                out["discovered_asins"] = row[0] if row else 0
        return out

    # ==================== 关键词搜索采集（F-010）====================
    #
    # 与上面 F-009 三件套逐条同构。移植要点（与 SQLite 侧的差异）与那边一致：
    #   * total_changes 差值 -> 单条 set-based INSERT + 命令标签；
    #   * `?` 占位符由 common/pgdb/pool 翻译，text_affinity / as_int 照打；
    #   * INSERT OR IGNORE -> ON CONFLICT DO NOTHING（两张发现表都有复合主键
    #     做合法仲裁者，tasks 有 UNIQUE(batch_id, asin)）。
    #
    # ⚠ 一处 F-009 没有的东西：`rank` 是 PG 的**保留字**（窗口函数 RANK()）。
    #   建表时不带引号能过（PG 允许保留字做列名，只要不在需要消歧的位置），
    #   但 INSERT 的列清单里必须写成 "rank" —— 不加引号在 PG 16 上直接
    #   syntax error。SQLite 侧没有这个限制，所以两边的 SQL 文本在这一处
    #   **有意不同**，不是笔误。

    async def create_search_batch(self, name: str, keywords: List[str],
                                  search_params: Optional[Dict] = None,
                                  discover_mode: str = "with_detail",
                                  zip_code: str = "10001",
                                  needs_screenshot: bool = False) -> Tuple[int, int]:
        """创建一个 keyword_discovery 批次，每个关键词一个 discover_search 任务。

        语义与 SQLite 侧同名方法逐字一致，见那边的 docstring。

        Returns: (batch_id, inserted_search_task_count)
        """
        if discover_mode not in ("discover_only", "with_detail"):
            raise ValueError(f"非法 discover_mode: {discover_mode}")

        clean_kws = []
        seen = set()
        for kw in keywords or []:
            kw = normalize_keyword(kw)
            if kw and kw.lower() not in seen:
                clean_kws.append(kw)
                seen.add(kw.lower())
        if not clean_kws:
            return (0, 0)

        meta_json = json.dumps({"search": search_params or {}}, ensure_ascii=False)
        ss_val = 1 if needs_screenshot else 0
        name_p = self.text_affinity(name)
        mode_p = self.text_affinity(discover_mode)
        zip_p = self.text_affinity(zip_code)
        meta_p = self.text_affinity(meta_json)
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                await self._db.execute(
                    "INSERT INTO batches (name, needs_screenshot, is_auto, batch_type, discover_mode) "
                    "VALUES (?, ?, 0, 'keyword_discovery', ?) "
                    "ON CONFLICT DO NOTHING",
                    (name_p, ss_val, mode_p)
                )
                # 同名重传：上面被 DO NOTHING 吞掉，这里取回**已存在**的 id
                # （与 create_seller_batch 同构）。
                async with self._db.execute("SELECT id FROM batches WHERE name = ?", (name_p,)) as c:
                    row = await c.fetchone()
                    batch_id = row[0] if row else 0
                if not batch_id:
                    await self._db.execute("ROLLBACK")
                    return (0, 0)

                cursor = await self._db.execute(
                    "INSERT INTO tasks "
                    "(batch_id, asin, zip_code, needs_screenshot, task_type, task_meta) "
                    "SELECT ?::bigint, u.kw, ?::text, 0, 'discover_search', ?::text "
                    "  FROM unnest(?::text[]) AS u(kw) "
                    "ON CONFLICT DO NOTHING",
                    (batch_id, zip_p, meta_p, clean_kws)
                )
                inserted = cursor.rowcount
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return (batch_id, inserted)

    async def accept_search_discovery_result(self, task_id: int, worker_id: str,
                                             lease_epoch: int, batch_id: int,
                                             keyword: str,
                                             items: List[Dict],
                                             meta: Optional[Dict] = None) -> Dict:
        """接受 discover_search 任务结果。语义见 SQLite 侧同名方法。

        Returns: {"accepted","stale","discovered","new_asins","detail_tasks_created"}
        """
        now = now_ts()
        tid = self.as_int(task_id)
        wid = self.text_affinity(worker_id)
        epoch = self.as_int(lease_epoch)
        bid = self.as_int(batch_id)
        kw = normalize_keyword(keyword) or ""
        kw_p = self.text_affinity(kw)
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # Step 1: 读回既有 task_meta，把建批次时写下的筛选参数保住。
                # 直接 SET task_meta=? 会把它抹掉（SQLite 侧同注）。
                async with self._db.execute(
                    "SELECT task_meta FROM tasks WHERE id=?", (tid,)
                ) as c:
                    mrow = await c.fetchone()
                merged: Dict[str, Any] = {}
                if mrow and mrow["task_meta"]:
                    try:
                        prev = json.loads(mrow["task_meta"])
                        if isinstance(prev, dict):
                            merged.update(prev)
                    except Exception:
                        pass
                merged.update(meta or {})
                meta_json = json.dumps(merged, ensure_ascii=False) if merged else None

                cursor = await self._db.execute(
                    "UPDATE tasks SET status='done', updated_at=?, task_meta=? "
                    "WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                    (now, meta_json, tid, wid, epoch)
                )
                if cursor.rowcount == 0:
                    await self._db.execute("ROLLBACK")
                    return {"accepted": False, "stale": True,
                            "discovered": 0, "new_asins": 0, "detail_tasks_created": 0}

                # Step 2: 读 batch 元信息
                async with self._db.execute(
                    "SELECT discover_mode, needs_screenshot, name FROM batches WHERE id=?",
                    (bid,)
                ) as c:
                    brow = await c.fetchone()
                discover_mode = brow["discover_mode"] if brow else None
                needs_screenshot = bool(brow["needs_screenshot"]) if brow else False
                ss_val = 1 if needs_screenshot else 0

                # Step 3: 写 search_discoveries（去重靠 PK）
                disc_rows = []
                seen_asins = set()
                for it in items or []:
                    asin = (it.get("asin") or "").strip().upper()
                    if not asin or asin in seen_asins:
                        continue
                    seen_asins.add(asin)
                    disc_rows.append((
                        bid, kw_p, asin,
                        self.text_affinity((it.get("title") or "")[:1000]),
                        self.text_affinity((it.get("price") or "")[:64]),
                        self.text_affinity((it.get("image") or "")[:1000]),
                        self.as_int(it.get("page") or 0),
                        self.as_int(it.get("rank") or 0),
                        1 if it.get("sponsored") else 0,
                    ))
                if disc_rows:
                    # 复合主键 (batch_id, keyword, asin) 是合法仲裁者。
                    # "rank" 必须带引号：PG 保留字（见本节头注）。
                    await self._db.executemany(
                        "INSERT INTO search_discoveries "
                        "(batch_id, keyword, asin, list_title, list_price, list_image, "
                        ' page_no, "rank", is_sponsored) '
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT DO NOTHING",
                        disc_rows
                    )

                # Step 4: 若 with_detail，为每个 ASIN 插入详情任务（同 batch_id）
                detail_inserted = 0
                if discover_mode == "with_detail" and seen_asins:
                    async with self._db.execute(
                        "SELECT zip_code FROM tasks WHERE id=?", (tid,)
                    ) as c:
                        zrow = await c.fetchone()
                    zip_code = zrow["zip_code"] if zrow else "10001"

                    detail_asins = list(seen_asins)
                    cursor = await self._db.execute(
                        "INSERT INTO tasks "
                        "(batch_id, asin, zip_code, needs_screenshot, task_type) "
                        "SELECT ?::bigint, u.asin, ?::text, ?::int, 'asin' "
                        "  FROM unnest(?::text[]) AS u(asin) "
                        "ON CONFLICT DO NOTHING",
                        (bid, self.text_affinity(zip_code), ss_val, detail_asins)
                    )
                    detail_inserted = cursor.rowcount

                    for a in seen_asins:
                        async with self._db.execute(
                            "SELECT 1 FROM asin_data WHERE asin=?", (a,)
                        ) as c:
                            exists = await c.fetchone()
                        await self._db.execute(
                            "INSERT INTO batch_asins (batch_id, asin, is_new) VALUES (?, ?, ?) "
                            "ON CONFLICT DO NOTHING",
                            (bid, a, 0 if exists else 1)
                        )

                    if needs_screenshot:
                        await self._db.executemany(
                            "INSERT INTO screenshots (asin, batch_id) VALUES (?, ?) "
                            "ON CONFLICT DO NOTHING",
                            [(a, bid) for a in seen_asins]
                        )

                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        return {
            "accepted": True, "stale": False,
            "discovered": len(seen_asins),
            "new_asins": len(seen_asins),
            "detail_tasks_created": detail_inserted,
        }

    async def get_search_batch_progress(self, batch_id: int) -> Dict:
        """keyword_discovery 批次的混合进度。语义见 SQLite 侧同名方法。"""
        out = {
            "batch_id": batch_id,
            "discover": {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0},
            "detail":   {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0},
            "discovered_asins": 0,
            "keywords": 0,
            "discover_mode": None,
        }
        bid = self.as_int(batch_id)
        async with self.read() as rc:
            async with rc.execute(
                "SELECT discover_mode FROM batches WHERE id=?", (bid,)
            ) as c:
                row = await c.fetchone()
                if row:
                    out["discover_mode"] = row["discover_mode"]

            async with rc.execute(
                "SELECT task_type, status, COUNT(*) as cnt FROM tasks "
                "WHERE batch_id=? GROUP BY task_type, status",
                (bid,)
            ) as c:
                async for row in c:
                    tt = row["task_type"] or "asin"
                    bucket = out["discover"] if tt == "discover_search" else out["detail"]
                    bucket[row["status"]] = row["cnt"]

            for bucket in (out["discover"], out["detail"]):
                bucket["total"] = sum(bucket[k] for k in ("pending", "processing", "done", "failed"))
            out["keywords"] = out["discover"]["total"]

            async with rc.execute(
                "SELECT COUNT(DISTINCT asin) FROM search_discoveries WHERE batch_id=?",
                (bid,)
            ) as c:
                row = await c.fetchone()
                out["discovered_asins"] = row[0] if row else 0
        return out

    # ==================== 截图路径回填 ====================

    async def _get_done_screenshot_path(self, asin: str,
                                        batch_id: int = None) -> Optional[str]:
        """走 self._db（写连接）。

        唯一调用点是 _save_result_inner_unlocked，那时已经持有写锁且身处写事务，
        必须走同一条连接才看得见事务内的未提交写入 —— 不要"顺手"改成 read()。
        """
        queries = []
        if batch_id is not None:
            queries.append((
                """SELECT file_path FROM screenshots
                   WHERE asin = ? AND batch_id = ? AND status = 'done'
                   ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1""",
                (self.text_affinity(asin), self.as_int(batch_id)),
            ))
        queries.append((
            """SELECT file_path FROM screenshots
               WHERE asin = ? AND status = 'done'
               ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1""",
            (self.text_affinity(asin),),
        ))

        for sql, params in queries:
            async with self._db.execute(sql, params) as c:
                row = await c.fetchone()
            if row:
                file_path = _normalize_screenshot_path(row["file_path"])
                if file_path:
                    return file_path
        return None

    async def _get_done_screenshot_paths(self, asins: List[str],
                                         batch_id: int = None) -> Dict[str, str]:
        """走 self.read()。"""
        if not asins:
            return {}

        remaining = list(dict.fromkeys(asins))
        mapping: Dict[str, str] = {}

        async def load(batch_filter: int = None):
            nonlocal remaining
            if not remaining:
                return

            # SQLite 版是 ",".join("?" * len(remaining)) 的变长 IN 列表。
            # 换成 = ANY(?::text[])：一个参数，空数组也合法，顺带绕开 asyncpg
            # 的 32767 个参数上限。行为完全一致（IN 与 = ANY 同义）。
            sql = (
                "SELECT asin, file_path FROM screenshots "
                "WHERE status = 'done' AND asin = ANY(?::text[])"
            )
            params: List[Any] = [[self.text_affinity(a) for a in remaining]]
            if batch_filter is not None:
                sql += " AND batch_id = ?"
                params.append(self.as_int(batch_filter))
            # PG 的 DESC 默认 NULLS FIRST，SQLite 是 NULLS LAST —— 必须显式写，
            # 否则一行 updated_at 为 NULL 就会抢到"哪张截图胜出"的第一名。
            sql += " ORDER BY updated_at DESC NULLS LAST, id DESC"

            async with self.read() as rc, rc.execute(sql, params) as c:
                rows = await c.fetchall()

            for row in rows:
                file_path = _normalize_screenshot_path(row["file_path"])
                if file_path and row["asin"] not in mapping:
                    mapping[row["asin"]] = file_path
            remaining = [asin for asin in remaining if asin not in mapping]

        if batch_id is not None:
            await load(batch_id)
        await load()
        return mapping

    async def _hydrate_screenshot_paths(self, items: List[Dict], batch_id: int = None):
        """**原地修改** items，无返回值。"""
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

    # ==================== 截图操作 ====================

    async def get_pending_screenshots(self, batch_id: int,
                                      limit: int = 50) -> List[Dict]:
        # 原版（database.py:2292）是 LIMIT 没有 ORDER BY —— 不是全序，取哪 5 行
        # 由存储层决定。SQLite 全表扫按 rowid 走，而 screenshots.id 就是 rowid，
        # 所以它实际产出的一直是 id 升序；PG 按堆序，一旦有行被 UPDATE 过（截图
        # done→pending 的重试就会）新版本挪到堆尾，取到的就是另一批行。实测
        # 20 行里 churn 掉 8 行、limit=5：sqlite [S001..S005] / pg [S009..S013]。
        # 补 ORDER BY id 把 PG 钉到 SQLite 今天的产出上，SQLite 侧不经过这里。
        async with self.read() as rc, rc.execute(
            "SELECT * FROM screenshots WHERE batch_id = ? AND status = 'pending' "
            "ORDER BY id LIMIT ?",
            (self.as_int(batch_id), self.as_int(limit))
        ) as c:
            return [dict(r) for r in await c.fetchall()]

    async def update_screenshot_status(self, asin: str, batch_id: int, status: str,
                                       file_path: str = None,
                                       error: str = None) -> bool:
        now = now_ts()
        updated = False
        asin_p = self.text_affinity(asin)
        bid = self.as_int(batch_id)
        status_p = self.text_affinity(status)
        path_p = self.text_affinity(file_path)
        err_p = self.text_affinity(error)
        async with self._write_lock:
            await self._db.execute("BEGIN")
            # 原版没有 try/except（database.py:2302-2331）。SQLite 下报错只是把
            # 连接留在事务里；PG 下事务一旦 abort，这条**专用写连接**上的后续
            # 语句会全部 InFailedSQLTransactionError，写路径就此瘫痪。
            # 补 ROLLBACK 不改返回值、不改异常类型、不改成功路径。
            try:
                if status == "done" and file_path:
                    cursor = await self._db.execute(
                        "UPDATE screenshots SET status=?, file_path=?, updated_at=? WHERE asin=? AND batch_id=?",
                        (status_p, path_p, now, asin_p, bid)
                    )
                    updated = cursor.rowcount > 0
                    if updated:
                        await self._db.execute(
                            "UPDATE asin_data SET screenshot_path=? WHERE asin=?",
                            (path_p, asin_p)
                        )
                elif status == "failed":
                    cursor = await self._db.execute(
                        """UPDATE screenshots
                           SET status='failed', retry_count=retry_count+1,
                               error_detail=?, updated_at=?
                           WHERE asin=? AND batch_id=?""",
                        (err_p, now, asin_p, bid)
                    )
                    updated = cursor.rowcount > 0
                else:
                    cursor = await self._db.execute(
                        "UPDATE screenshots SET status=?, updated_at=? WHERE asin=? AND batch_id=?",
                        (status_p, now, asin_p, bid)
                    )
                    updated = cursor.rowcount > 0
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return updated

    async def get_screenshot_progress(self, batch_id: int) -> Dict:
        """{pending, processing, done, failed, total}；total 是四者之和
        （在 total 自身被塞进 dict **之前**用 sum(stats.values()) 算的）。"""
        sql = "SELECT status, COUNT(*) as cnt FROM screenshots WHERE batch_id = ? GROUP BY status"
        stats = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        async with self.read() as rc, rc.execute(sql, (self.as_int(batch_id),)) as c:
            async for row in c:
                stats[row["status"]] = row["cnt"]
        stats["total"] = sum(stats.values())
        return stats

    async def list_screenshots(self, batch_id: int, asin: str = None,
                               status: str = None, cursor_asin: str = None,
                               limit: int = 200) -> List[Dict]:
        """逐条列出一个批次的截图状态。SQLite 侧同名方法的对等实现。

        文本参数一律过 ``text_affinity``：``screenshots.asin`` / ``status`` 是
        ``text COLLATE "C"``，而 asyncpg 对 Python str 的推断在参数位置上不总能
        落到同一类型，绕过索引就得全表扫。排序由 ``COLLATE "C"`` 保证与 SQLite
        逐字节一致 —— 这也是建库必须 ``LC_COLLATE=C`` 的老原因，游标分页靠它。
        """
        sql = ["SELECT asin, status, retry_count, error_detail, file_path, updated_at "
               "FROM screenshots WHERE batch_id = ?"]
        params: List[Any] = [self.as_int(batch_id)]
        if asin:
            sql.append("AND asin = ?")
            params.append(self.text_affinity(asin))
        if status:
            sql.append("AND status = ?")
            params.append(self.text_affinity(status))
        if cursor_asin:
            sql.append("AND asin > ?")
            params.append(self.text_affinity(cursor_asin))
        sql.append("ORDER BY asin LIMIT ?")
        params.append(self.as_int(max(1, min(int(limit), 1000))))
        async with self.read() as rc, rc.execute(" ".join(sql), tuple(params)) as c:
            return [dict(r) for r in await c.fetchall()]
