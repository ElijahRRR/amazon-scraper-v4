"""
Amazon ASIN 采集系统 v3 - 数据库模块
重新设计的 Schema，针对百万级 ASIN 优化：
- keyset 分页替代 OFFSET
- batch_asins 直接 JOIN 替代 EXISTS 子查询
- asin_changes 预计算表替代运行时 OR 多列筛选
- 合理的复合索引
"""
import os
import json
import time
import aiosqlite
import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, Tuple
from datetime import timedelta

from common import config
from common.core.timeutil import now_ts, ts_from, utc_now
from common.core.error_types import SERVER_REJECT

# ============================================================
# 与 PG 后端共享的纯 Python 符号 —— 定义在 common/core/（Phase 4.1）。
#
# 这里**逐名再导出**，让 `from common.database import LOCK_STATS` 等既有写法
# 一字不用改，且与 common/pgdb/_shared.py 指向**同一批对象**
# （tests/pgdb/test_skeleton.py:85 的 `is` 断言就是钉这个的）。
#
# 不要写成 `from common.core import *`。理由见 common/core/__init__.py 的
# 模块 docstring —— 注意那里订正过一次：因为 common.core 定义了 __all__，
# 星号导入今天其实能用，「下划线名被跳过」只在没有 __all__ 时才发生。
# 逐名写法要的是「导出面是显式的、评审 diff 能看见」，不是「会当场炸」。
# ============================================================
from common.core import (  # noqa: F401  —— 全部是有意的再导出
    # ---- 重试策略 ----
    LIMITED_RETRY_ERROR_TYPES,
    NO_AUTO_RETRY_ERROR_TYPES,
    NO_RETRY_ERROR_TYPES,
    _fail_cap,
    # ---- 锁仪表（必须与 PG 实现共用同一个全局容器）----
    LOCK_STATS,
    TimedLock,
    _NamedLockCtx,
    _record_wait,
    _record_hold,
    record_stage,
    # ---- 解析失败 / 截图路径归一 ----
    _NA_VALUES,
    _normalize_screenshot_path,
    _is_parse_failure,
    # ---- 变动比较器 ----
    _parse_price_float,
    _compare_price,
    _compare_stock_qty,
    _compare_stock_status,
    # ---- hash ----
    _HASH_FIELDS,
    _TITLE_BULLETS_FIELDS,
    _compute_content_hash,
    _compute_title_bullets_hash,
    # ---- asin_data 列清单 ----
    ASIN_DATA_FIELDS,
    _ASIN_DATA_COLUMN_SET,
    # ---- 清库的表清单（DELETE /api/database）----
    CLEAR_TABLES,
    # ---- 按 ASIN 删除 / 模糊搜索（DELETE /api/results）----
    ASIN_DELETE_CHUNK,
    ASIN_DELETE_TABLES,
    search_like_pattern,
)

logger = logging.getLogger(__name__)


# 一个 search term 的三列 OR 谓词（SQLite 侧原文；PG 侧见
# common/pgdb/results_read.py 的 _TERM_OR —— 那边必须写成
# ascii_lower(col) LIKE ascii_lower(?) ESCAPE ''，见 OWNERSHIP.md D-5 / D-16）。
# 带 `?` 占位符 = 方言相关，所以它**不进** common/core。
SEARCH_TERM_OR = "(d.asin LIKE ? OR d.title LIKE ? OR d.brand LIKE ?)"


class Database:
    """异步 SQLite 数据库管理器 v3"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self._db: Optional[aiosqlite.Connection] = None
        # 用 TimedLock 替代裸 asyncio.Lock，自动按 caller 记录 wait/hold 时长
        # 兼容旧用法：所有现有 `async with self._write_lock:` 计入 caller='other'
        # 热点路径用 `async with self._write_lock("xxx"):` 单独追踪
        self._write_lock = TimedLock()
        # ============ 读写解耦（2026-05 落地）============
        # 独立只读连接池：仪表盘/导出等读查询走这里，与 worker 写热路径（self._db）
        # 物理隔离。WAL 模式下「多读 + 单写」可并发，重读（扫 asin_data / 聚合 113 万
        # tasks）不再占用写连接的单线程，从根本上消除「读阻塞写」导致的拉取/上传超时。
        self._read_pool: Optional[asyncio.Queue] = None
        self._read_conns: List[aiosqlite.Connection] = []
        self._read_pool_size = 3  # 2GB VPS 上的折中：3 条读连接，私有 cache 各 16MB
        self._maintenance_task: Optional[asyncio.Task] = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path, isolation_level=None)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        # WAL 自动 checkpoint（每 1000 页 ≈ 4MB 触发一次），防止 WAL 文件无限膨胀
        await self._db.execute("PRAGMA wal_autocheckpoint=1000")
        # ============ Tier 1 性能优化（2026-05 落地）============
        # 1) synchronous=NORMAL：WAL 模式下安全，fsync 只在 checkpoint 时做，写延迟 -30%~50%
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # 2) cache_size=-65536：page cache 提升到 64MB（原 16MB），减少冷查询磁盘 IO
        await self._db.execute("PRAGMA cache_size=-65536")
        # 3) mmap_size=256MB：内存映射读路径（原 32MB），2GB VPS 上限保留余量
        await self._db.execute("PRAGMA mmap_size=268435456")
        # 4) temp_store=MEMORY：临时表/排序在内存做，避免临时文件 IO
        await self._db.execute("PRAGMA temp_store=MEMORY")
        # 5) journal_size_limit=64MB：checkpoint 后自动 truncate WAL，回收磁盘
        await self._db.execute("PRAGMA journal_size_limit=67108864")
        # 6) optimize：原先在此同步执行（PRAGMA optimize=0x10002），在 2.4GB 库上会
        #    阻塞启动 2~3 分钟（ANALYZE 扫全表）。改为端口监听后由 run_startup_optimize()
        #    异步执行，让服务先就绪、worker 先能拉任务，ANALYZE 在后台慢慢做。
        # =====================================================
        self._db.row_factory = aiosqlite.Row
        await self.init_tables()
        await self._open_read_pool()

    async def _open_read_pool(self):
        """打开独立只读连接池（query_only=ON）。每条连接独立后台线程，
        WAL 下与写连接并发读，互不阻塞。"""
        self._read_pool = asyncio.Queue()
        self._read_conns = []
        for _ in range(self._read_pool_size):
            rconn = await aiosqlite.connect(self.db_path, isolation_level=None)
            await rconn.execute("PRAGMA query_only=ON")       # 物理只读，杜绝误写
            await rconn.execute("PRAGMA busy_timeout=5000")
            await rconn.execute("PRAGMA cache_size=-16384")   # 私有 cache 16MB/条
            await rconn.execute("PRAGMA mmap_size=268435456") # 共享文件映射，复用页缓存
            await rconn.execute("PRAGMA temp_store=MEMORY")
            rconn.row_factory = aiosqlite.Row
            self._read_conns.append(rconn)
            self._read_pool.put_nowait(rconn)
        logger.info(f"只读连接池就绪：{self._read_pool_size} 条连接")

    @asynccontextmanager
    async def read(self):
        """从只读连接池借一条连接（池空时排队等待，形成读侧背压，绝不触碰写连接）。
        回退：池未初始化时退化到写连接，保证调用方始终可用。"""
        if self._read_pool is None:
            yield self._db
            return
        conn = await self._read_pool.get()
        try:
            yield conn
        finally:
            self._read_pool.put_nowait(conn)

    async def run_startup_optimize(self):
        """启动后异步执行一次 PRAGMA optimize（刷新查询优化器统计）。
        optimize 本质是写（更新 sqlite_stat1），必须走写连接；用 analysis_limit=400
        限制每个索引的采样行数，在 2.4GB 库上也是毫秒级，套 _write_lock 短暂持有，
        不会重演同步执行时 2~3 分钟的冷启动阻塞。"""
        try:
            async with self._write_lock("optimize"):
                await self._db.execute("PRAGMA analysis_limit=400")  # 采样上限，避免全表 ANALYZE
                await self._db.execute("PRAGMA optimize=0x10002")
            logger.info("✅ 启动期 optimize 完成（异步，采样上限 400）")
        except Exception as e:
            logger.warning(f"启动期 optimize 异常: {e}")

    async def maintenance_loop(self, checkpoint_interval: int = 120):
        """后台维护协程：定期 WAL checkpoint(TRUNCATE)，防止 WAL 顶到 64MB 上限
        后 checkpoint 饥饿，同时回收磁盘、缩短下次重启的 WAL recovery 时间。
        必须在 _write_lock 下执行：否则 checkpoint 语句可能插进别的写事务（BEGIN..
        COMMIT）中间，因连接持有写事务而无法 checkpoint，永远 busy。"""
        logger.info(f"🔧 WAL 维护协程启动（每 {checkpoint_interval}s 一次 TRUNCATE checkpoint）")
        while True:
            await asyncio.sleep(checkpoint_interval)
            try:
                async with self._write_lock("checkpoint"):
                    res = await self.wal_checkpoint("TRUNCATE")
                if res and res[0] == 1:
                    # busy=1：仍有读连接持 WAL 读标记，TRUNCATE 未完全完成，下轮再试
                    logger.debug(f"WAL checkpoint busy，稍后重试: {res}")
            except Exception as e:
                logger.warning(f"WAL 维护协程异常: {e}")

    def start_maintenance(self, checkpoint_interval: int = 120):
        """由 lifespan 调用：拉起后台维护协程（幂等）。"""
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(
                self.maintenance_loop(checkpoint_interval))

    async def close(self):
        if self._maintenance_task and not self._maintenance_task.done():
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except (asyncio.CancelledError, Exception):
                pass
        for rconn in self._read_conns:
            try:
                await rconn.close()
            except Exception:
                pass
        self._read_conns = []
        self._read_pool = None
        if self._db:
            await self._db.close()
            self._db = None

    async def wal_checkpoint(self, mode: str = "PASSIVE") -> Optional[tuple]:
        """显式触发 WAL checkpoint。mode: PASSIVE / FULL / RESTART / TRUNCATE。
        返回 (busy, log, checkpointed) 三元组；出错返回 None。"""
        if not self._db:
            return None
        try:
            async with self._db.execute(f"PRAGMA wal_checkpoint({mode})") as c:
                row = await c.fetchone()
            return tuple(row) if row else None
        except Exception as e:
            logger.warning(f"wal_checkpoint 异常: {e}")
            return None

    async def init_tables(self):
        await self._db.executescript("""
            -- 批次表（注：callback 相关索引在 ALTER TABLE 加列之后再单独创建，
            -- 避免老表升级时 CREATE INDEX 引用不存在的列）
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                needs_screenshot BOOLEAN DEFAULT 0,
                is_auto BOOLEAN DEFAULT 0,
                expand_variants BOOLEAN DEFAULT 0,    -- 变体自动展开开关
                -- 状态机：running / completed / failed
                status TEXT DEFAULT 'running',
                completed_at TIMESTAMP,
                -- 调用方原样回传字段
                external_id TEXT,
                -- 完成时回调通知
                callback_url TEXT,
                callback_status TEXT,                 -- pending / sent / failed / disabled
                callback_attempts INTEGER DEFAULT 0,
                callback_next_retry_at TIMESTAMP,
                callback_last_error TEXT,
                callback_sent_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 批次-ASIN 关联表（核心优化：直接 JOIN 替代 EXISTS 子查询）
            CREATE TABLE IF NOT EXISTS batch_asins (
                batch_id INTEGER NOT NULL,
                asin TEXT NOT NULL,
                is_new INTEGER DEFAULT 0,
                PRIMARY KEY (batch_id, asin)
            );
            CREATE INDEX IF NOT EXISTS idx_batch_asins_asin ON batch_asins(asin);

            -- ASIN 数据主表（每 ASIN 一行，存储最新状态）
            CREATE TABLE IF NOT EXISTS asin_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT NOT NULL UNIQUE,
                title TEXT,
                brand TEXT,
                product_type TEXT,
                manufacturer TEXT,
                model_number TEXT,
                part_number TEXT,
                country_of_origin TEXT,
                is_customized TEXT,
                best_sellers_rank TEXT,
                original_price TEXT,
                current_price TEXT,
                buybox_price TEXT,
                buybox_shipping TEXT,
                is_fba TEXT,
                stock_count TEXT,
                stock_status TEXT,
                delivery_date TEXT,
                delivery_time TEXT,
                image_urls TEXT,
                bullet_points TEXT,
                long_description TEXT,
                upc_list TEXT,
                ean_list TEXT,
                parent_asin TEXT,
                variation_asins TEXT,
                variant_attributes TEXT,
                root_category_id TEXT,
                category_ids TEXT,
                category_tree TEXT,
                first_available_date TEXT,
                package_dimensions TEXT,
                package_weight TEXT,
                item_dimensions TEXT,
                item_weight TEXT,
                product_url TEXT,
                site TEXT DEFAULT 'amazon.com',
                zip_code TEXT DEFAULT '10001',
                crawl_time TEXT,
                screenshot_path TEXT,
                content_hash TEXT,
                title_bullets_hash TEXT,
                baseline_price TEXT,
                baseline_buybox_price TEXT,
                baseline_stock_count TEXT,
                baseline_stock_status TEXT,
                baseline_title_bullets_hash TEXT,
                baseline_updated_at TEXT,
                rating TEXT,
                review_count TEXT,
                seller_id TEXT,
                seller_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 变动记录表（预计算，按类型索引，支持高效筛选）
            CREATE TABLE IF NOT EXISTS asin_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT NOT NULL,
                batch_id INTEGER,
                change_type TEXT NOT NULL,
                change_detail TEXT,
                prev_value TEXT,
                new_value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_changes_asin ON asin_changes(asin);
            CREATE INDEX IF NOT EXISTS idx_changes_type ON asin_changes(change_type);
            CREATE INDEX IF NOT EXISTS idx_changes_batch_type ON asin_changes(batch_id, change_type);
            -- change_filter 的 EXISTS 半连接（change_type + asin 双等值）的支撑索引。
            -- 与 PG 侧 common/pgdb/schema.py 的 idx_changes_type_asin 一一对应（C4）。
            CREATE INDEX IF NOT EXISTS idx_changes_type_asin ON asin_changes(change_type, asin);

            -- 采集任务表
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                asin TEXT NOT NULL,
                zip_code TEXT DEFAULT '10001',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                needs_screenshot BOOLEAN DEFAULT 0,
                worker_id TEXT,
                lease_epoch INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                auto_retry_count INTEGER DEFAULT 0,
                error_type TEXT,
                error_detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(batch_id, asin)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_worker ON tasks(status, worker_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at);
            -- 覆盖索引：让 get_batches 的「按 batch 分组 + 统计各 status」走 index-only，
            -- 不再为 113 万行逐行回表读 status（仪表盘 /api/batches 每几秒轮询一次）。
            CREATE INDEX IF NOT EXISTS idx_tasks_batch_status ON tasks(batch_id, status);
            -- pull_tasks 拆分后的两条候选查询各自的有序输出都由它提供。
            -- 与 PG 侧 common/pgdb/schema.py 的 idx_tasks_pull 一一对应（C4），
            -- 但**不写 NULLS FIRST 修饰符**：SQLite 的 ASC 默认就是 NULLS FIRST，
            -- 而 PG 的 ASC 默认是 NULLS LAST 所以那边必须显式写。
            CREATE INDEX IF NOT EXISTS idx_tasks_pull ON tasks(status, priority, zip_code, id);

            -- 截图任务表（独立追踪，可靠重试）
            CREATE TABLE IF NOT EXISTS screenshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT NOT NULL,
                batch_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                error_detail TEXT,
                file_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(batch_id, asin)
            );
            CREATE INDEX IF NOT EXISTS idx_screenshots_status ON screenshots(status);
            CREATE INDEX IF NOT EXISTS idx_screenshots_batch ON screenshots(batch_id);
            -- _get_done_screenshot_paths 的第二次 load() 不带 batch 过滤，
            -- 而这张表上原本没有以 asin 打头的索引。SQLite 侧本来能靠
            -- UNIQUE(batch_id,asin) 的跳跃扫描凑合，但跳跃扫描依赖
            -- sqlite_stat1（见本文件 PRAGMA analysis_limit + 启动期 ANALYZE）；
            -- 加了这条就不再依赖统计信息。与 PG 侧
            -- common/pgdb/schema.py 的 idx_screenshots_asin_done 一一对应（C4）。
            CREATE INDEX IF NOT EXISTS idx_screenshots_asin_done ON screenshots(asin) WHERE status = 'done';

            -- 卖家店铺发现结果表（F-009：seller storefront 模式）
            -- 每行 = 某 batch 在某 seller 店内发现的一个 ASIN
            CREATE TABLE IF NOT EXISTS seller_discoveries (
                batch_id   INTEGER NOT NULL,
                seller_id  TEXT NOT NULL,
                asin       TEXT NOT NULL,
                list_title TEXT,
                list_price TEXT,
                list_image TEXT,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (batch_id, seller_id, asin)
            );
            CREATE INDEX IF NOT EXISTS idx_seller_disc_seller ON seller_discoveries(seller_id);
            CREATE INDEX IF NOT EXISTS idx_seller_disc_asin ON seller_discoveries(asin);
            CREATE INDEX IF NOT EXISTS idx_seller_disc_batch ON seller_discoveries(batch_id);
        """)

        # 迁移：为已有 tasks 表添加 lease_epoch 列（CREATE TABLE IF NOT EXISTS 不修改已有表）
        try:
            await self._db.execute("ALTER TABLE tasks ADD COLUMN lease_epoch INTEGER DEFAULT 0")
            logger.info("数据库迁移: tasks 表新增 lease_epoch 列")
        except Exception:
            pass  # 列已存在
        # 迁移：自动重试轮数（失败任务由后台自动重入队的次数）
        try:
            await self._db.execute("ALTER TABLE tasks ADD COLUMN auto_retry_count INTEGER DEFAULT 0")
            logger.info("数据库迁移: tasks 表新增 auto_retry_count 列")
        except Exception:
            pass  # 列已存在
        try:
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_worker ON tasks(status, worker_id, updated_at)")
        except Exception:
            pass
        try:
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at)")
        except Exception:
            pass

        # 迁移：batches 表添加 is_auto / expand_variants（变体自动展开开关）
        for col, default in [("is_auto", "0"), ("expand_variants", "0")]:
            try:
                await self._db.execute(f"ALTER TABLE batches ADD COLUMN {col} BOOLEAN DEFAULT {default}")
                logger.info(f"数据库迁移: batches 表新增 {col} 列")
            except Exception:
                pass

        # 迁移：F-009 seller-storefront 采集
        # batches.batch_type: 'asin' (现有) | 'seller_discovery'
        # batches.discover_mode: NULL | 'discover_only' | 'with_detail'
        for col_def in [
            ("batch_type", "TEXT NOT NULL DEFAULT 'asin'"),
            ("discover_mode", "TEXT"),
        ]:
            col, ddl = col_def
            try:
                await self._db.execute(f"ALTER TABLE batches ADD COLUMN {col} {ddl}")
                logger.info(f"数据库迁移: batches 表新增 {col} 列")
            except Exception:
                pass

        # tasks.task_type: 'asin' (现有) | 'discover_seller'
        # tasks.task_meta: JSON, 仅 discover 任务用
        for col_def in [
            ("task_type", "TEXT NOT NULL DEFAULT 'asin'"),
            ("task_meta", "TEXT"),
        ]:
            col, ddl = col_def
            try:
                await self._db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
                logger.info(f"数据库迁移: tasks 表新增 {col} 列")
            except Exception:
                pass

        # 迁移：asin_data 表添加 baseline 字段
        for col in ["baseline_price", "baseline_buybox_price", "baseline_stock_count",
                     "baseline_stock_status", "baseline_title_bullets_hash", "baseline_updated_at"]:
            try:
                await self._db.execute(f"ALTER TABLE asin_data ADD COLUMN {col} TEXT")
                logger.info(f"数据库迁移: asin_data 表新增 {col} 列")
            except Exception:
                pass

        # 迁移：asin_data 表添加变体属性字段（多属性产品：本 ASIN 自己的属性值，
        # 形如 "color_name=Red; size_name=L"；非变体产品为空）
        for col in ["variant_attributes"]:
            try:
                await self._db.execute(f"ALTER TABLE asin_data ADD COLUMN {col} TEXT")
                logger.info(f"数据库迁移: asin_data 表新增 {col} 列")
            except Exception:
                pass

        # 迁移：asin_data 表添加评分 + 卖家字段
        for col in ["rating", "review_count", "seller_id", "seller_name"]:
            try:
                await self._db.execute(f"ALTER TABLE asin_data ADD COLUMN {col} TEXT")
                logger.info(f"数据库迁移: asin_data 表新增 {col} 列")
            except Exception:
                pass

        # 迁移：batches 表添加 callback / 状态字段
        for col_ddl in [
            ("status", "TEXT DEFAULT 'running'"),
            ("completed_at", "TIMESTAMP"),
            ("external_id", "TEXT"),
            ("callback_url", "TEXT"),
            ("callback_status", "TEXT"),
            ("callback_attempts", "INTEGER DEFAULT 0"),
            ("callback_next_retry_at", "TIMESTAMP"),
            ("callback_last_error", "TEXT"),
            ("callback_sent_at", "TIMESTAMP"),
        ]:
            col, ddl = col_ddl
            try:
                await self._db.execute(f"ALTER TABLE batches ADD COLUMN {col} {ddl}")
                logger.info(f"数据库迁移: batches 表新增 {col} 列")
            except Exception:
                pass
        # callback 相关索引（IF NOT EXISTS 幂等）
        try:
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_batches_callback_pending "
                "ON batches(callback_status, callback_next_retry_at)")
        except Exception:
            pass

        # 一次性 backfill：老批次（迁移前没有 status 字段，迁移后默认 running）
        # 如果它们的 task + screenshot 都已经终态，直接标记 completed + 禁用回调
        # 这样新启动的 _completion_watcher 不会把这些老批次拉起来检测
        try:
            cursor = await self._db.execute(
                """UPDATE batches
                   SET status='completed',
                       completed_at=COALESCE(updated_at, created_at),
                       callback_status='disabled'
                   WHERE COALESCE(status,'running')='running'
                     AND callback_status IS NULL
                     AND id NOT IN (
                       SELECT DISTINCT batch_id FROM tasks
                       WHERE status NOT IN ('done','failed')
                     )
                     AND id NOT IN (
                       SELECT DISTINCT batch_id FROM screenshots
                       WHERE status NOT IN ('done','failed')
                     )
                """
            )
            if cursor.rowcount > 0:
                logger.info(f"数据库迁移: 回填 {cursor.rowcount} 个老批次为 completed")
        except Exception as e:
            logger.warning(f"老批次 backfill 异常: {e}")

        # ============================================================
        # FTS5 全文搜索索引（trigram + detail=none + external content）
        # 用于替代 asin_data 上的 LIKE '%xxx%' 全表扫描（46s → ~50ms）
        # 设计：
        #   - external content：不复制 title/brand，磁盘增量极小（~20 MB）
        #   - trigram tokenizer：原生支持 LIKE '%xxx%' 子串匹配
        #   - detail=none：不支持 phrase/NEAR 查询（我们也用不到），索引更小
        #   - 3 个触发器（AI/AD/AU）保持与主表 asin_data 一致
        # 查询模式：使用 LIKE on FTS 表 + UNION 走索引，详见 get_results()
        # ============================================================
        try:
            await self._db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS asin_data_fts USING fts5(
                    asin, title, brand,
                    content='asin_data',
                    content_rowid='id',
                    tokenize='trigram',
                    detail='none'
                );

                CREATE TRIGGER IF NOT EXISTS asin_data_ai AFTER INSERT ON asin_data BEGIN
                  INSERT INTO asin_data_fts(rowid, asin, title, brand)
                  VALUES (new.id, new.asin, new.title, new.brand);
                END;

                CREATE TRIGGER IF NOT EXISTS asin_data_ad AFTER DELETE ON asin_data BEGIN
                  INSERT INTO asin_data_fts(asin_data_fts, rowid, asin, title, brand)
                  VALUES ('delete', old.id, old.asin, old.title, old.brand);
                END;

                CREATE TRIGGER IF NOT EXISTS asin_data_au AFTER UPDATE ON asin_data BEGIN
                  INSERT INTO asin_data_fts(asin_data_fts, rowid, asin, title, brand)
                  VALUES ('delete', old.id, old.asin, old.title, old.brand);
                  INSERT INTO asin_data_fts(rowid, asin, title, brand)
                  VALUES (new.id, new.asin, new.title, new.brand);
                END;
            """)
            # 首次部署：如果 FTS 索引为空但 asin_data 已有数据，自动 rebuild。
            # 不要对 FTS 虚表做 COUNT(*)：生产库上会扫完整 trigram 索引，阻塞启动。
            async with self._db.execute("SELECT 1 FROM asin_data_fts_idx LIMIT 1") as c:
                fts_count = 1 if await c.fetchone() else 0
            async with self._db.execute("SELECT COUNT(*) FROM asin_data") as c:
                main_count = (await c.fetchone())[0]
            if main_count > 0 and fts_count == 0:
                logger.info(f"FTS5 索引为空且主表有 {main_count} 行，启动 rebuild...")
                await self._db.execute(
                    "INSERT INTO asin_data_fts(asin_data_fts) VALUES('rebuild')"
                )
                logger.info("FTS5 rebuild 完成")
        except Exception as e:
            logger.warning(f"FTS5 索引初始化异常（不影响核心功能）: {e}")

    # ==================== 批次操作 ====================

    async def create_batch(self, name: str, needs_screenshot: bool = False,
                           is_auto: bool = False,
                           external_id: Optional[str] = None,
                           callback_url: Optional[str] = None,
                           expand_variants: bool = False) -> int:
        """创建批次，返回 batch_id。

        external_id: 调用方自己的批次 ID（原样回传，便于追踪）。
        callback_url: 采集完成时 POST 通知到此 URL。空表示不通知。
        expand_variants: 开启后，本批第一轮采完会把已采 ASIN 的同族变体（variation_asins）
            自动入队进【同一批次】继续采，直到无新增（正常 2 轮收敛）。默认关。

        ⚠ **撞名时返回的是既有批次的 id，本方法分辨不出「新建」还是「命中已有」。**
        需要分辨的调用方（``POST /api/upload`` 要据此回 409）用
        ``create_batch_if_absent``——那才是真源，本方法只是丢掉第二个返回值的薄壳。
        签名与返回类型一字未动：自动调度器（``_auto_scrape_scheduler`` /
        ``/api/schedules/{name}/run``）那两条路径不该被这次改动波及。
        """
        batch_id, _created = await self.create_batch_if_absent(
            name, needs_screenshot, is_auto,
            external_id=external_id, callback_url=callback_url,
            expand_variants=expand_variants,
        )
        return batch_id

    async def create_batch_if_absent(self, name: str, needs_screenshot: bool = False,
                                     is_auto: bool = False,
                                     external_id: Optional[str] = None,
                                     callback_url: Optional[str] = None,
                                     expand_variants: bool = False) -> Tuple[int, bool]:
        """建批次，返回 ``(batch_id, created)``。``created=False`` = 这个名字已经有批次了。

        ------------------------------------------------------------------
        为什么要有这个方法（旧坑 b + c 的共同根因）
        ------------------------------------------------------------------
        ``create_batch`` 是 ``INSERT OR IGNORE`` + 事后 ``SELECT id``，撞名时
        返回既有批次的 id，**和新建时的返回形状完全一样**。Phase 4.7 已经写明
        「撞名不是 no-op」，但当时只把结论写进注释，调用方仍然分辨不出来。
        实测的后果（同名批次第二次上传）：

          * 两次 ``create_batch`` 返回同一个 id；
          * ``create_tasks`` 靠 ``UNIQUE(batch_id, asin)`` 吃掉重复，于是
            ``inserted`` 是 0 还是非 0 取决于「这次的清单里有几个新 ASIN」——
            部分重叠时返回非零，看起来像是成功新建了一个批次（旧坑 c）；
          * **第二次的 external_id / callback_url 被静默丢弃**（``INSERT OR
            IGNORE`` 整行不插，既有行一个字段都不更新），而 HTTP 响应回显的是
            **请求**里的值 —— 调用方以为回调注册好了，回调永远不会触发。

        ``created`` 这一位就是让 ``POST /api/upload`` 能回 409 的那一位。
        409 之后：200 恒等于「新建了一个批次」，``inserted`` 的歧义消失（旧坑 c），
        调用方也不再需要毫秒精度的批次名去躲开合并（旧坑 b）。

        ------------------------------------------------------------------
        为什么是新方法，而不是给 create_batch 加参数
        ------------------------------------------------------------------
        加参数（比如 ``return_created=True``）会让**返回类型随参数变**，
        而 ``create_batch`` 的返回值今天被两处生产调用方（``app.py:831`` 的
        自动调度器、``app.py:1820`` 的 ``/api/schedules/{name}/run``）直接当
        int 用，测试里更多；更要紧的是
        ``tests/pgdb/test_skeleton.py::test_signatures_match_sqlite`` 只比签名、
        比不到「返回形状」，一个随参数变形的返回值在两个后端漂了也不会有人红。
        新方法则是：SQL 只有一份（``create_batch`` 转调它），返回类型恒定，
        两侧签名各自静态可比。代价是 ``PUBLIC_API`` 多一个名字——值。

        ------------------------------------------------------------------
        两条必须保持的既有性质
        ------------------------------------------------------------------
        1. **撞名照样烧号**：INSERT 照发（只是被 IGNORE 掉），自增序列照样 +1。
           这是两个后端实测一致的现状，黄金基线的 ``golden_batch_b`` 是 id 3
           不是 2 就是这么来的。**绝不允许**改成「先 SELECT 再决定插不插」。
        2. ``name=None``（NOT NULL 冲突）→ ``(0, False)``：插不进去、SELECT 也
           查不到。``created`` 为 False 是对的——确实没建出来。
        """
        callback_status = "pending" if callback_url else None
        async with self._write_lock:
            await self._db.execute("BEGIN")
            # rowcount：INSERT OR IGNORE 真插进去是 1，被 IGNORE 掉是 0。
            # 这一位必须在 COMMIT **之前**读——之后 cursor 上的计数不再对应这条语句。
            cur = await self._db.execute(
                "INSERT OR IGNORE INTO batches "
                "(name, needs_screenshot, is_auto, status, external_id, "
                " callback_url, callback_status, expand_variants) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
                (name, 1 if needs_screenshot else 0, 1 if is_auto else 0,
                 external_id, callback_url, callback_status,
                 1 if expand_variants else 0)
            )
            created = cur.rowcount > 0
            await self._db.execute("COMMIT")
            async with self._db.execute("SELECT id FROM batches WHERE name = ?", (name,)) as c:
                row = await c.fetchone()
                batch_id = row[0] if row else 0
        # 没拿到 id 就谈不上"新建"（name=None 那条路）。
        return batch_id, (created and batch_id > 0)

    async def get_batches(self) -> List[Dict]:
        """获取所有批次及其统计"""
        sql = """
            SELECT b.id, b.name, b.needs_screenshot, b.created_at,
                   COUNT(t.id) as total_tasks,
                   SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) as failed,
                   SUM(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) as pending,
                   SUM(CASE WHEN t.status = 'processing' THEN 1 ELSE 0 END) as processing
            FROM batches b
            LEFT JOIN tasks t ON t.batch_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
        """
        async with self.read() as rc, rc.execute(sql) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

    async def get_batch_by_name(self, name: str) -> Optional[Dict]:
        async with self.read() as rc, rc.execute("SELECT * FROM batches WHERE name = ?", (name,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

    async def expand_batch_variants(self, batch_id: int) -> int:
        """变体自动展开：把本批【已采 ASIN 的同族变体】（variation_asins）入队进【同一批次】。

        仅对开启 expand_variants 的批次生效；其余返回 0。
        去重：只入队不在本批 batch_asins 里的 ASIN；底层 create_tasks 用
        INSERT OR IGNORE(UNIQUE(batch_id,asin)) 再兜一层 → 数学上保证收敛、绝不死循环。
        返回本次实际新增的任务数（0 = 没有新变体，批次可真正完成）。
        正常调用时机：批次本轮全部终态后（completion watcher）。
        """
        async with self.read() as rc:
            async with rc.execute(
                "SELECT needs_screenshot, expand_variants FROM batches WHERE id=?",
                (batch_id,)
            ) as c:
                brow = await c.fetchone()
            if not brow or not brow["expand_variants"]:
                return 0
            ss = bool(brow["needs_screenshot"])
            # 继承本批已有任务的 zip（同族应同邮编）
            async with rc.execute(
                "SELECT zip_code FROM tasks WHERE batch_id=? LIMIT 1", (batch_id,)
            ) as c:
                zrow = await c.fetchone()
            zip_code = (zrow["zip_code"] if zrow else None) or config.DEFAULT_ZIP_CODE
            # 本批已采 ASIN 的同族列表（twister 洗干净的真家族）
            async with rc.execute(
                "SELECT d.variation_asins FROM asin_data d "
                "JOIN batch_asins ba ON ba.asin = d.asin "
                "WHERE ba.batch_id=? AND d.variation_asins IS NOT NULL AND d.variation_asins != ''",
                (batch_id,)
            ) as c:
                fam_rows = await c.fetchall()
            # 本批已有 ASIN（去重基准）
            async with rc.execute(
                "SELECT asin FROM batch_asins WHERE batch_id=?", (batch_id,)
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
        # 复用 create_tasks：INSERT OR IGNORE tasks + 维护 batch_asins + 截图任务，返回实际新增数
        return await self.create_tasks(batch_id, sorted(candidates), zip_code, ss)

    # ==================== 批次完成检测 + 回调 ====================

    async def get_batch_completion_status(self, batch_id: int) -> Dict[str, Any]:
        """返回批次完成度统计 + 是否已全部终态。

        完成判定：所有 task ∈ {done, failed} AND 所有 screenshot ∈ {done, failed}
        """
        async with self.read() as rc:
            async with rc.execute(
                """SELECT
                       SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status NOT IN ('done','failed') THEN 1 ELSE 0 END) AS open_,
                       COUNT(*) AS total
                   FROM tasks WHERE batch_id=?""",
                (batch_id,)
            ) as c:
                t = await c.fetchone()
            async with rc.execute(
                """SELECT
                       SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status NOT IN ('done','failed') THEN 1 ELSE 0 END) AS open_,
                       COUNT(*) AS total
                   FROM screenshots WHERE batch_id=?""",
                (batch_id,)
            ) as c:
                s = await c.fetchone()

        t_done = (t["done"] or 0) if t else 0
        t_failed = (t["failed"] or 0) if t else 0
        t_open = (t["open_"] or 0) if t else 0
        t_total = (t["total"] or 0) if t else 0
        s_done = (s["done"] or 0) if s else 0
        s_failed = (s["failed"] or 0) if s else 0
        s_open = (s["open_"] or 0) if s else 0
        s_total = (s["total"] or 0) if s else 0

        all_done = (t_total > 0 and t_open == 0 and s_open == 0)
        return {
            "tasks": {"total": t_total, "done": t_done, "failed": t_failed, "open": t_open},
            "screenshots": {"total": s_total, "done": s_done, "failed": s_failed, "open": s_open},
            "all_terminal": all_done,
        }

    async def mark_batch_completed(self, batch_id: int) -> bool:
        """标记批次为 completed（仅 running → completed 转移有效，幂等）。
        返回 True 表示本次确实做了状态转移，调用方可以触发回调入队。
        False 表示批次已经是 completed/failed/其他状态，不重复处理。
        """
        now = now_ts()
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    """UPDATE batches
                       SET status='completed',
                           completed_at=?,
                           updated_at=?
                       WHERE id=? AND status='running'""",
                    (now, now, batch_id)
                )
                changed = cursor.rowcount > 0
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return changed

    async def list_callback_due(self, now_str: str, limit: int = 50) -> List[Dict]:
        """查询所有需要立刻发送/重试的 callback。
        条件：
        - callback_status='pending'（待发送）
        - callback_url 非空
        - status='completed'（必须真正完成，避免在 _completion_watcher 跑完之前抢跑）
        - next_retry_at <= 当前时间（NULL 视为立即）
        """
        async with self._db.execute(
            """SELECT id, name, external_id, callback_url, callback_attempts,
                      completed_at, status
               FROM batches
               WHERE callback_status='pending'
                 AND callback_url IS NOT NULL
                 AND status='completed'
                 AND (callback_next_retry_at IS NULL OR callback_next_retry_at <= ?)
               ORDER BY id ASC LIMIT ?""",
            (now_str, limit)
        ) as c:
            rows = await c.fetchall()
        return [dict(r) for r in rows]

    async def mark_callback_attempt(self, batch_id: int, success: bool,
                                     error: Optional[str] = None,
                                     next_retry_at: Optional[str] = None,
                                     max_attempts: int = 5) -> Dict[str, Any]:
        """记录一次回调尝试。
        success=True:  callback_status='sent', sent_at=now
        success=False: attempts+1；若达到 max_attempts 则 status='failed'，
                       否则更新 next_retry_at 等下次扫描
        """
        now = now_ts()
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                if success:
                    await self._db.execute(
                        """UPDATE batches
                           SET callback_status='sent',
                               callback_sent_at=?,
                               callback_last_error=NULL,
                               updated_at=?
                           WHERE id=?""",
                        (now, now, batch_id)
                    )
                    await self._db.execute("COMMIT")
                    return {"final_status": "sent"}
                # 失败：判断是否达到上限
                async with self._db.execute(
                    "SELECT callback_attempts FROM batches WHERE id=?", (batch_id,)
                ) as c:
                    row = await c.fetchone()
                attempts = (row["callback_attempts"] or 0) + 1 if row else 1
                if attempts >= max_attempts:
                    await self._db.execute(
                        """UPDATE batches
                           SET callback_attempts=?,
                               callback_status='failed',
                               callback_last_error=?,
                               updated_at=?
                           WHERE id=?""",
                        (attempts, (error or "")[:500], now, batch_id)
                    )
                    final = "failed"
                else:
                    await self._db.execute(
                        """UPDATE batches
                           SET callback_attempts=?,
                               callback_next_retry_at=?,
                               callback_last_error=?,
                               updated_at=?
                           WHERE id=?""",
                        (attempts, next_retry_at, (error or "")[:500], now, batch_id)
                    )
                    final = "pending"
                await self._db.execute("COMMIT")
                return {"final_status": final, "attempts": attempts}
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def reset_callback_for_retry(self, batch_id: int) -> bool:
        """运维手动触发：把已经 failed 或 sent 的回调重置回 pending 立即重试。"""
        now = now_ts()
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    """UPDATE batches
                       SET callback_status='pending',
                           callback_attempts=0,
                           callback_next_retry_at=NULL,
                           callback_last_error=NULL,
                           updated_at=?
                       WHERE id=? AND callback_url IS NOT NULL""",
                    (now, batch_id)
                )
                changed = cursor.rowcount > 0
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return changed

    # ==================== 任务操作 ====================

    async def create_tasks(self, batch_id: int, asins: List[str], zip_code: str = "10001",
                           needs_screenshot: bool = False,
                           per_asin_zip: Dict[str, str] = None) -> int:
        """批量创建采集任务，同时维护 batch_asins 关联。

        per_asin_zip: 可选 {asin: zip} 映射；某个 asin 在其中则用该 zip，否则回落到 zip_code。
        """
        clean_asins = []
        seen = set()
        ss_val = 1 if needs_screenshot else 0
        for asin in asins:
            asin = asin.strip()
            if asin and asin not in seen:
                clean_asins.append(asin)
                seen.add(asin)

        if not clean_asins:
            return 0

        per_asin_zip = per_asin_zip or {}

        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # 插入任务（每个 asin 用各自指定的 zip，未指定则用批次默认）
                before_tasks = self._db.total_changes
                await self._db.executemany(
                    "INSERT OR IGNORE INTO tasks (batch_id, asin, zip_code, needs_screenshot) VALUES (?, ?, ?, ?)",
                    [(batch_id, asin, per_asin_zip.get(asin) or zip_code, ss_val)
                     for asin in clean_asins]
                )
                task_inserted = self._db.total_changes - before_tasks

                # 维护 batch_asins（判断是否新 ASIN）
                for asin in clean_asins:
                    async with self._db.execute(
                        "SELECT 1 FROM asin_data WHERE asin = ?", (asin,)
                    ) as c:
                        exists = await c.fetchone()
                    is_new = 0 if exists else 1
                    await self._db.execute(
                        "INSERT OR IGNORE INTO batch_asins (batch_id, asin, is_new) VALUES (?, ?, ?)",
                        (batch_id, asin, is_new)
                    )

                # 如果需要截图，创建截图任务
                if needs_screenshot:
                    await self._db.executemany(
                        "INSERT OR IGNORE INTO screenshots (asin, batch_id) VALUES (?, ?)",
                        [(asin, batch_id) for asin in clean_asins]
                    )

                inserted = task_inserted
                await self._db.execute("COMMIT")
            except Exception:
                await self._db.execute("ROLLBACK")
                raise
        return inserted

    async def pull_tasks(self, worker_id: str, count: int = 10,
                         needs_screenshot=None,
                         prefer_zip: Optional[str] = None) -> List[Dict]:
        """Worker 拉取待处理任务（原子操作，不再内联超时回收）。

        prefer_zip: worker 当前 session 的邮编。若指定，server 优先返回相同 zip 的任务，
        从而最大化复用同一 session（避免每个任务都切换邮编）。多 worker 不同 prefer_zip
        时各自被分到对应邮编池，自然分流。
        """
        now = now_ts()
        tasks = []

        async with self._write_lock("pull_tasks"):
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                ss_filter = ""
                ss_params = []
                if needs_screenshot is not None:
                    ss_filter = " AND t.needs_screenshot = ?"
                    ss_params = [1 if needs_screenshot else 0]

                async with self._db.execute(
                    f"SELECT MAX(priority) FROM tasks t WHERE t.status = 'pending'{ss_filter}",
                    ss_params
                ) as cur:
                    row = await cur.fetchone()
                    top_priority = row[0] if row and row[0] is not None else 0

                # 排序策略：
                #   1. prefer_zip 匹配优先（同 zip 任务先派发，节省 session 切换）
                #   2. 同 zip 内按 id 升序（FIFO，先入先出）
                #
                # prefer_zip 非空时拆成两条查询，等价性论证见
                # common/pgdb/tasks.py 的同名注释（组 0 内 zip 恒定 → 只按 id；
                # zip IS NULL 落 ELSE 分支 → 归组 1）。
                # ⚠ 两处 C4 不对称，与 PG 侧**有意**不同：
                #   (a) `IS DISTINCT FROM` 要 SQLite ≥3.39，这里写等价的
                #       `t.zip_code IS NOT ?`（IS/IS NOT 在 SQLite 里就是 null-safe 比较）；
                #   (b) SQLite 没有 FOR UPDATE / SKIP LOCKED，PG 侧那两句这里不存在。
                # 另外 SQLite 的 ASC 默认就是 NULLS FIRST，不写修饰符（写了要 ≥3.30）。
                base_sql = (
                    f"""SELECT t.id, t.batch_id, t.asin, t.zip_code, t.retry_count,
                               t.priority, t.needs_screenshot, t.lease_epoch,
                               t.task_type, t.task_meta,
                               b.name as batch_name, b.discover_mode
                        FROM tasks t
                        JOIN batches b ON b.id = t.batch_id
                        WHERE t.status = 'pending' AND t.priority = ?{ss_filter}"""
                )

                async def _candidates(zip_pred, zip_params, order_clause, limit):
                    # limit <= 0 -> 空表，两个作用：
                    # (1) Q2 的 limit 是 count - len(Q1)，Q1 填满配额时它就是 0；
                    # (2) **顺带改掉了一处既有行为**，本轮显式声明：
                    #     端点 server/app.py 的 `count: int = Query(10)` 没有下界，
                    #     GET /api/tasks/pull?count=-1 可达。改动前两个后端在这里
                    #     不一致（违反 C4）：SQLite 的 LIMIT -1 等于不限，会把整个
                    #     pending 队列派给一个 worker 并全部置 processing；PG 抛
                    #     InvalidRowCountInLimitClauseError（HTTP 500）。
                    #     两种都不该留，现在统一返回 0 行。
                    if limit <= 0:
                        return []
                    async with self._db.execute(
                        f"{base_sql}{zip_pred}\n                        {order_clause}\n"
                        f"                        LIMIT ?",
                        (top_priority, *ss_params, *zip_params, limit)
                    ) as cursor:
                        return list(await cursor.fetchall())

                want = int(count)
                if prefer_zip:
                    rows = await _candidates(" AND t.zip_code = ?", [prefer_zip],
                                             "ORDER BY t.id ASC", want)
                    if len(rows) < want:
                        rows += await _candidates(
                            " AND t.zip_code IS NOT ?", [prefer_zip],
                            "ORDER BY t.zip_code, t.id ASC", want - len(rows))
                else:
                    # 无偏好时按 zip_code 分组，同 zip 仍尽量连续派发
                    rows = await _candidates("", [], "ORDER BY t.zip_code, t.id ASC", want)

                if not rows:
                    await self._db.execute("COMMIT")
                    return tasks

                ids = []
                for row in rows:
                    task = {
                        "id": row["id"],
                        "batch_id": row["batch_id"],
                        "batch_name": row["batch_name"],
                        "asin": row["asin"],
                        "zip_code": row["zip_code"],
                        "retry_count": row["retry_count"],
                        "priority": row["priority"],
                        "needs_screenshot": bool(row["needs_screenshot"]),
                        "lease_epoch": row["lease_epoch"],
                        "task_type": row["task_type"] or "asin",
                        "task_meta": row["task_meta"],
                        "discover_mode": row["discover_mode"],
                    }
                    tasks.append(task)
                    ids.append(row["id"])

                placeholders = ",".join("?" * len(ids))
                await self._db.execute(
                    f"UPDATE tasks SET status='processing', worker_id=?, updated_at=? WHERE id IN ({placeholders})",
                    [worker_id, now] + ids
                )
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        return tasks

    async def reclaim_dead_worker_tasks(self, dead_worker_ids: List[str]):
        """回收死 Worker 的任务 + 硬超时兜底（liveness safety net）

        所有回收路径都 bump lease_epoch，让旧 Worker 的迟到结果失效。
        合成一条 SQL 防止双重 bump。
        """
        if not dead_worker_ids:
            dead_worker_ids = []
        now = now_ts()
        hard_cutoff = ts_from(utc_now() - timedelta(minutes=config.TASK_TIMEOUT_MINUTES))

        async with self._write_lock:
            await self._db.execute("BEGIN")
            if dead_worker_ids:
                placeholders = ",".join("?" * len(dead_worker_ids))
                cursor = await self._db.execute(
                    f"""UPDATE tasks SET status='pending', worker_id=NULL,
                           lease_epoch=lease_epoch+1, updated_at=?
                       WHERE status='processing' AND (
                           worker_id IN ({placeholders}) OR updated_at < ?
                       )""",
                    [now] + dead_worker_ids + [hard_cutoff]
                )
            else:
                # 没有死 Worker，只做硬超时兜底
                cursor = await self._db.execute(
                    "UPDATE tasks SET status='pending', worker_id=NULL, "
                    "lease_epoch=lease_epoch+1, updated_at=? "
                    "WHERE status='processing' AND updated_at < ?",
                    (now, hard_cutoff)
                )
            reclaimed = cursor.rowcount
            await self._db.execute("COMMIT")
        if reclaimed > 0:
            logger.info(f"回收 {reclaimed} 个任务 (dead_workers={len(dead_worker_ids)}, hard_cutoff={hard_cutoff})")
        return reclaimed

    async def auto_retry_failed_tasks(self, max_auto_cycles: int = 2,
                                       delay_minutes: int = 5) -> int:
        """自动重试终态失败的任务：重置为 pending、重置 retry_count、bump epoch、bump auto_retry_count。

        一个任务达到 MAX_RETRIES (默认 3) 后变为 failed 终态。本方法把这些
        失败任务重新入队，让它们再走一遍最多 3 次重试。最多允许 max_auto_cycles 轮。
        总体尝试次数上限 ≈ MAX_RETRIES * (1 + max_auto_cycles)。
        每次自动重试前，任务的 updated_at 至少要早于 delay_minutes 分钟（避免刚失败就立刻再跑）。

        排除：NO_AUTO_RETRY_ERROR_TYPES 中的失败（如 variant_offset）。
        这些类型是稳定的产品/页面层事实，循环重试会浪费配额。
        """
        if max_auto_cycles <= 0:
            return 0
        now_str = now_ts()
        cutoff = ts_from(utc_now() - timedelta(minutes=delay_minutes))

        # 动态构造 NOT IN (...) 子句
        no_retry_list = sorted(NO_AUTO_RETRY_ERROR_TYPES)
        no_retry_placeholders = ",".join("?" * len(no_retry_list))
        excl_clause = f"AND COALESCE(error_type, '') NOT IN ({no_retry_placeholders}) " if no_retry_list else ""

        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "UPDATE tasks SET status='pending', worker_id=NULL, retry_count=0, "
                    "lease_epoch=lease_epoch+1, "
                    "auto_retry_count=COALESCE(auto_retry_count,0)+1, "
                    "updated_at=? "
                    "WHERE status='failed' "
                    "AND COALESCE(auto_retry_count,0) < ? "
                    "AND updated_at < ? "
                    f"{excl_clause}",
                    (now_str, max_auto_cycles, cutoff, *no_retry_list)
                )
                retried = cursor.rowcount
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        if retried > 0:
            logger.info(f"自动重试 {retried} 个失败任务 (delay={delay_minutes}min, max_cycles={max_auto_cycles})")
        return retried

    async def retry_failed_tasks(self, batch_id: int,
                                 exclude_error_types=()) -> dict:
        """把一个批次里的 failed 任务重新入队（``POST /api/batches/{name}/retry`` 的库侧动作）。

        exclude_error_types: 不重试的 error_type 清单（调用方给，通常是
            ``sorted(NO_AUTO_RETRY_ERROR_TYPES)``）。空清单 = 全部重试。
            **调用方自己传**而不是在这里读全局常量：这条策略是 handler 的语义
            （"始终跳过 variant_offset"），响应体里还要原样回显它。

        Returns: ``{"retried": n, "skipped": m}``
            retried —— 真被改成 pending 的行数；
            skipped —— 因命中 exclude 清单而**没**被重试的 failed 行数（供前端展示）。

        与 ``auto_retry_failed_tasks`` 的区别：这是**运维手动**触发，不看
        ``auto_retry_count`` / ``updated_at`` 冷却，也不 bump ``lease_epoch``。
        两者刻意不合并 —— 合并要么给自动重试加上手动的旁路，要么给手动加上
        自动的节流，哪个方向都是行为变更。

        统计与更新必须在**同一个事务**里，否则 skipped 与 retried 可能来自
        两个不同的快照，加起来对不上那一刻的 failed 总数。
        """
        excl = list(exclude_error_types or ())
        placeholders = ",".join("?" * len(excl))

        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # 先统计：本次将跳过的"不可重试"任务数（供前端展示）
                skipped = 0
                if excl:
                    async with self._db.execute(
                        f"SELECT COUNT(*) FROM tasks WHERE batch_id=? AND status='failed' "
                        f"AND COALESCE(error_type, '') IN ({placeholders})",
                        [batch_id] + excl
                    ) as c:
                        row = await c.fetchone()
                        skipped = row[0] if row else 0

                # 实际重试 SQL：始终排除 excl 清单。
                if not excl:
                    cursor = await self._db.execute(
                        "UPDATE tasks SET status='pending', retry_count=0, worker_id=NULL "
                        "WHERE batch_id=? AND status='failed'",
                        (batch_id,)
                    )
                else:
                    cursor = await self._db.execute(
                        f"UPDATE tasks SET status='pending', retry_count=0, worker_id=NULL "
                        f"WHERE batch_id=? AND status='failed' "
                        f"AND COALESCE(error_type, '') NOT IN ({placeholders})",
                        [batch_id] + excl
                    )
                retried = cursor.rowcount
                await self._db.execute("COMMIT")
            except BaseException:
                try:
                    await self._db.execute("ROLLBACK")
                except BaseException:
                    pass
                raise
        return {"retried": retried, "skipped": skipped}

    async def fail_task(self, task_id: int, worker_id: str, lease_epoch: int,
                        error_type: str = "", error_detail: str = "") -> dict:
        """标记任务失败（校验 worker_id + lease_epoch）

        Returns: {"accepted": True/False, "stale": True/False}
        """
        now = now_ts()
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                # 在同一事务内 SELECT + UPDATE，避免 TOCTOU（与 reclaim 循环并发时旧代码会静默失配）
                async with self._db.execute(
                    "SELECT retry_count FROM tasks WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                    (task_id, worker_id, lease_epoch)
                ) as c:
                    row = await c.fetchone()
                if not row:
                    await self._db.execute("ROLLBACK")
                    return {"accepted": False, "stale": True}
                retry_count = row[0] + 1

                # 按 error_type 决定该任务的失败上限：
                # - variant_offset: cap=1（不重试）
                # - 其他: cap=MAX_RETRIES（=3）
                cap = _fail_cap(error_type)
                if retry_count >= cap:
                    cursor = await self._db.execute(
                        "UPDATE tasks SET status='failed', retry_count=?, error_type=?, error_detail=?, updated_at=? "
                        "WHERE id=? AND worker_id=? AND lease_epoch=?",
                        (retry_count, error_type, error_detail, now, task_id, worker_id, lease_epoch)
                    )
                else:
                    # 回 pending + bump epoch
                    cursor = await self._db.execute(
                        "UPDATE tasks SET status='pending', retry_count=?, error_type=?, error_detail=?, "
                        "worker_id=NULL, lease_epoch=lease_epoch+1, updated_at=? "
                        "WHERE id=? AND worker_id=? AND lease_epoch=?",
                        (retry_count, error_type, error_detail, now, task_id, worker_id, lease_epoch)
                    )
                if cursor.rowcount == 0:
                    # 极罕见：事务内 SELECT 成功但 UPDATE 失配（例如被 TRIGGER 拦截），按 stale 处理
                    await self._db.execute("ROLLBACK")
                    return {"accepted": False, "stale": True}
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return {"accepted": True, "stale": False}

    async def release_tasks(self, worker_id: str, tasks: List[Dict]):
        """释放任务回 pending（校验 worker_id + lease_epoch，bump epoch）

        tasks: [{"task_id": int, "lease_epoch": int}, ...]
        """
        if not tasks:
            return 0
        now = now_ts()
        released = 0

        # 按 epoch 分组批量更新
        from collections import defaultdict
        groups = defaultdict(list)
        for t in tasks:
            groups[t["lease_epoch"]].append(t["task_id"])

        async with self._write_lock:
            await self._db.execute("BEGIN")
            for epoch, ids in groups.items():
                placeholders = ",".join("?" * len(ids))
                cursor = await self._db.execute(
                    f"UPDATE tasks SET status='pending', worker_id=NULL, "
                    f"lease_epoch=lease_epoch+1, updated_at=? "
                    f"WHERE id IN ({placeholders}) AND worker_id=? AND lease_epoch=? AND status='processing'",
                    [now] + ids + [worker_id, epoch]
                )
                released += cursor.rowcount
            await self._db.execute("COMMIT")
        return released

    async def prioritize_batch(self, batch_id: int, priority: int = 10):
        async with self._write_lock:
            await self._db.execute("BEGIN")
            await self._db.execute(
                "UPDATE tasks SET priority=? WHERE batch_id=? AND status='pending'",
                (priority, batch_id)
            )
            await self._db.execute("COMMIT")

    async def delete_batches(self, batch_ids) -> List[str]:
        """删除若干批次及其全部关联数据，返回**待删除的截图物理文件路径**。

        ``DELETE /api/batches/{name}`` 与 ``POST /api/batches/delete-bulk``
        的库侧动作（Phase 3.8 批 (2) 把两处收成一处）。

        **文件不在这里删**：返回路径清单，由 handler 调 ``_remove_screenshot_files``。
        库方法删文件意味着一个失败的 unlink 能污染一次数据库操作，而且
        测试再也不能在不碰磁盘的前提下验它。

        入参去重 / 上限（bulk 端点的 500 上限）属于**请求校验**，留在 handler。
        这里只做 ``int()`` 归一 —— asyncpg 按 Python 类型推参数，路由给的
        str id 会 DataError。

        路径清单在写锁**外**、事务**前**取：截图行马上就要被删掉，取晚了就空了。
        收口前单条删除走的是 ``db._db``（写连接），bulk 走的是 ``db.read()``
        （读池）；这里统一成读池 —— 两者读的都是已提交数据，但读池不占写连接，
        也就不会跟正在跑的写事务抢那一条连接。

        删除顺序：先四张子表、最后 batches。PG 侧有真外键，顺序即正确性。
        """
        ids = [int(b) for b in batch_ids]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))

        # 先收集截图物理文件路径（只读连接，不占写锁）
        async with self.read() as rc, rc.execute(
            f"SELECT file_path FROM screenshots WHERE batch_id IN ({ph})"
            f" AND file_path IS NOT NULL", ids
        ) as c:
            screenshot_files = [row["file_path"] for row in await c.fetchall()]

        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                for tbl in ("tasks", "batch_asins", "screenshots", "asin_changes"):
                    await self._db.execute(
                        f"DELETE FROM {tbl} WHERE batch_id IN ({ph})", ids)
                await self._db.execute(
                    f"DELETE FROM batches WHERE id IN ({ph})", ids)
                await self._db.execute("COMMIT")
            except BaseException:
                try:
                    await self._db.execute("ROLLBACK")
                except BaseException:
                    pass
                raise
        return screenshot_files

    async def get_progress(self, batch_id: int = None) -> Dict:
        """获取任务进度"""
        if batch_id:
            sql = "SELECT status, COUNT(*) as cnt FROM tasks WHERE batch_id = ? GROUP BY status"
            params = (batch_id,)
        else:
            sql = "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
            params = ()

        stats = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0}
        async with self.read() as rc, rc.execute(sql, params) as c:
            async for row in c:
                stats[row["status"]] = row["cnt"]
        stats["total"] = sum(stats[k] for k in ["pending", "processing", "done", "failed"])
        finished = stats["done"] + stats["failed"]
        stats["completion_rate"] = round(stats["done"] / stats["total"] * 100, 1) if stats["total"] else 0
        stats["success_rate"] = round(stats["done"] / finished * 100, 1) if finished else 0
        return stats

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
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                await self._db.execute(
                    "INSERT OR IGNORE INTO batches (name, needs_screenshot, is_auto, batch_type, discover_mode) "
                    "VALUES (?, ?, 0, 'seller_discovery', ?)",
                    (name, ss_val, discover_mode)
                )
                async with self._db.execute("SELECT id FROM batches WHERE name = ?", (name,)) as c:
                    row = await c.fetchone()
                    batch_id = row[0] if row else 0
                if not batch_id:
                    await self._db.execute("ROLLBACK")
                    return (0, 0)

                before = self._db.total_changes
                await self._db.executemany(
                    "INSERT OR IGNORE INTO tasks "
                    "(batch_id, asin, zip_code, needs_screenshot, task_type) "
                    "VALUES (?, ?, ?, 0, 'discover_seller')",
                    [(batch_id, sid, zip_code) for sid in clean_ids]
                )
                inserted = self._db.total_changes - before
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
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # Step 1: 校验 lease 并标 done
                meta_json = json.dumps(meta) if meta else None
                cursor = await self._db.execute(
                    "UPDATE tasks SET status='done', updated_at=?, task_meta=? "
                    "WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                    (now, meta_json, task_id, worker_id, lease_epoch)
                )
                if cursor.rowcount == 0:
                    await self._db.execute("ROLLBACK")
                    return {"accepted": False, "stale": True,
                            "discovered": 0, "new_asins": 0, "detail_tasks_created": 0}

                # Step 2: 读 batch 元信息
                async with self._db.execute(
                    "SELECT discover_mode, needs_screenshot, name FROM batches WHERE id=?",
                    (batch_id,)
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
                        batch_id, seller_id, asin,
                        (it.get("title") or "")[:1000],
                        (it.get("price") or "")[:64],
                        (it.get("image") or "")[:1000],
                    ))
                if disc_rows:
                    await self._db.executemany(
                        "INSERT OR IGNORE INTO seller_discoveries "
                        "(batch_id, seller_id, asin, list_title, list_price, list_image) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        disc_rows
                    )

                # Step 4: 若 with_detail，为每个 ASIN 插入详情任务（同 batch_id）
                detail_inserted = 0
                if discover_mode == "with_detail" and seen_asins:
                    # 取该 task 的 zip_code 复用
                    async with self._db.execute(
                        "SELECT zip_code FROM tasks WHERE id=?", (task_id,)
                    ) as c:
                        zrow = await c.fetchone()
                    zip_code = zrow["zip_code"] if zrow else "10001"

                    before = self._db.total_changes
                    await self._db.executemany(
                        "INSERT OR IGNORE INTO tasks "
                        "(batch_id, asin, zip_code, needs_screenshot, task_type) "
                        "VALUES (?, ?, ?, ?, 'asin')",
                        [(batch_id, a, zip_code, ss_val) for a in seen_asins]
                    )
                    detail_inserted = self._db.total_changes - before

                    # 维护 batch_asins 关联（is_new 判定参考 create_tasks）
                    for a in seen_asins:
                        async with self._db.execute(
                            "SELECT 1 FROM asin_data WHERE asin=?", (a,)
                        ) as c:
                            exists = await c.fetchone()
                        await self._db.execute(
                            "INSERT OR IGNORE INTO batch_asins (batch_id, asin, is_new) VALUES (?, ?, ?)",
                            (batch_id, a, 0 if exists else 1)
                        )

                    if needs_screenshot:
                        await self._db.executemany(
                            "INSERT OR IGNORE INTO screenshots (asin, batch_id) VALUES (?, ?)",
                            [(a, batch_id) for a in seen_asins]
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
            "new_asins": len(seen_asins),  # 旧 batch 的 PK 冲突会被 INSERT OR IGNORE 吞掉，这里返回本次提交的去重总数
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
        async with self.read() as rc:
            async with rc.execute(
                "SELECT discover_mode FROM batches WHERE id=?", (batch_id,)
            ) as c:
                row = await c.fetchone()
                if row:
                    out["discover_mode"] = row["discover_mode"]

            async with rc.execute(
                "SELECT task_type, status, COUNT(*) as cnt FROM tasks "
                "WHERE batch_id=? GROUP BY task_type, status",
                (batch_id,)
            ) as c:
                async for row in c:
                    tt = row["task_type"] or "asin"
                    bucket = out["discover"] if tt == "discover_seller" else out["detail"]
                    bucket[row["status"]] = row["cnt"]

            for bucket in (out["discover"], out["detail"]):
                bucket["total"] = sum(bucket[k] for k in ("pending", "processing", "done", "failed"))

            async with rc.execute(
                "SELECT COUNT(*) FROM seller_discoveries WHERE batch_id=?", (batch_id,)
            ) as c:
                row = await c.fetchone()
                out["discovered_asins"] = row[0] if row else 0
        return out

    # ==================== 结果操作（含变动检测）====================

    async def accept_success_result(self, task_id: int, worker_id: str, lease_epoch: int,
                                    data: dict, batch_id: int = None) -> dict:
        """原子事务：校验 lease → 写数据 → 标 done

        Returns: {"accepted": True, "saved": True} 正常
                 {"accepted": True, "saved": False, "server_reject": True} 解析失败
                 {"accepted": False, "stale": True} lease 不匹配
        """
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # Step 1: 校验 lease（原子 gate）
                cursor = await self._db.execute(
                    "UPDATE tasks SET status='done', updated_at=? "
                    "WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                    (now_ts(),
                     task_id, worker_id, lease_epoch)
                )
                if cursor.rowcount == 0:
                    await self._db.execute("ROLLBACK")
                    return {"accepted": False, "stale": True}

                # Step 2: 解析失败检测
                if _is_parse_failure(data):
                    asin = data.get("asin", "")
                    logger.warning(f"server_reject: {asin} 解析失败数据")
                    # 降级为 failed（不回 pending 重试，Worker 已重试过）
                    await self._db.execute(
                        "UPDATE tasks SET status='failed', error_type=?, "
                        "error_detail=? WHERE id=?",
                        (SERVER_REJECT, "parse_failure_on_server", task_id)
                    )
                    await self._db.execute("COMMIT")
                    return {"accepted": True, "saved": False, "server_reject": True}

                # Step 3: 写入 asin_data（事务内，不拿锁不开新事务）
                saved = await self._save_result_inner_unlocked(data, batch_id)

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
            # 用 BEGIN IMMEDIATE 显式拿写锁，与 pull_tasks 一致
            _t_total = time.monotonic()
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    task_id = item.get("task_id")
                    worker_id = item.get("worker_id", "")
                    lease_epoch = item.get("lease_epoch", 0)
                    batch_id = item.get("batch_id")
                    data = item.get("data", {})
                    is_success = item.get("success", True)

                    if not task_id:
                        # 无 task_id 的直接写入
                        if is_success and data:
                            _t = time.monotonic()
                            saved = await self._save_result_inner_unlocked(data, batch_id)
                            record_stage("save_result", (time.monotonic() - _t) * 1000)
                            if saved:
                                accepted += 1
                        continue

                    if is_success:
                        # 校验 lease
                        _t = time.monotonic()
                        cursor = await self._db.execute(
                            "UPDATE tasks SET status='done', updated_at=? "
                            "WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                            (now, task_id, worker_id, lease_epoch)
                        )
                        record_stage("update_tasks_lease", (time.monotonic() - _t) * 1000)
                        if cursor.rowcount == 0:
                            stale += 1
                            continue

                        # 解析失败检测
                        if _is_parse_failure(data):
                            await self._db.execute(
                                "UPDATE tasks SET status='failed', error_type='server_reject', "
                                "error_detail='parse_failure_on_server' WHERE id=?",
                                (task_id,)
                            )
                            failed += 1
                            continue

                        # 写入 asin_data
                        _t = time.monotonic()
                        saved = await self._save_result_inner_unlocked(data, batch_id)
                        record_stage("save_result", (time.monotonic() - _t) * 1000)
                        if saved:
                            accepted += 1
                        else:
                            failed += 1
                    else:
                        # 失败结果：校验 lease 后标记失败/重试
                        error_type = data.get("error_type", "")
                        error_detail = data.get("error_detail", "")
                        async with self._db.execute(
                            "SELECT retry_count FROM tasks WHERE id=? AND worker_id=? AND lease_epoch=? AND status='processing'",
                            (task_id, worker_id, lease_epoch)
                        ) as c:
                            row = await c.fetchone()
                        if not row:
                            stale += 1
                            continue
                        retry_count = row[0] + 1
                        # 按 error_type 决定该任务的失败上限
                        # （variant_offset 不重试，其他用 MAX_RETRIES）
                        cap = _fail_cap(error_type)
                        if retry_count >= cap:
                            await self._db.execute(
                                "UPDATE tasks SET status='failed', retry_count=?, error_type=?, error_detail=?, updated_at=? "
                                "WHERE id=?",
                                (retry_count, error_type, error_detail, now, task_id)
                            )
                            failed += 1
                        else:
                            await self._db.execute(
                                "UPDATE tasks SET status='pending', retry_count=?, error_type=?, error_detail=?, "
                                "worker_id=NULL, lease_epoch=lease_epoch+1, updated_at=? WHERE id=?",
                                (retry_count, error_type, error_detail, now, task_id)
                            )
                            # 重新入队不计入 accepted；若后续需要单独统计可在此累加 requeued

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

        now = now_ts()
        data["content_hash"] = _compute_content_hash(data)
        data["title_bullets_hash"] = _compute_title_bullets_hash(data)

        # 查询批次是否为定时采集
        is_auto = False
        if batch_id:
            async with self._db.execute(
                "SELECT is_auto FROM batches WHERE id = ?", (batch_id,)
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
            "FROM asin_data WHERE asin = ?", (asin,)
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

            if has_baseline:
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
                    (asin, batch_id, change_type, detail, prev_val, new_val)
                )

            # 更新主表当前值
            update_fields = []
            update_values = []
            for f in ASIN_DATA_FIELDS:
                if f in ("asin", "screenshot_path"):
                    continue
                val = data.get(f)
                if val is not None:
                    update_fields.append(f"{f} = ?")
                    update_values.append(val)

            if resolved_screenshot_path and resolved_screenshot_path != existing_screenshot_path:
                update_fields.append("screenshot_path = ?")
                update_values.append(resolved_screenshot_path)

            # 定时采集：同时更新 baseline
            if is_auto:
                update_fields.append("baseline_price = ?")
                update_values.append(data.get("current_price"))
                update_fields.append("baseline_buybox_price = ?")
                update_values.append(data.get("buybox_price"))
                update_fields.append("baseline_stock_count = ?")
                update_values.append(data.get("stock_count"))
                update_fields.append("baseline_stock_status = ?")
                update_values.append(data.get("stock_status"))
                update_fields.append("baseline_title_bullets_hash = ?")
                update_values.append(data.get("title_bullets_hash"))
                update_fields.append("baseline_updated_at = ?")
                update_values.append(now)

            update_fields.append("updated_at = ?")
            update_values.append(now)
            update_values.append(asin)

            await self._db.execute(
                f"UPDATE asin_data SET {', '.join(update_fields)} WHERE asin = ?",
                update_values
            )
        else:
            # 新 ASIN，插入
            insert_fields = ["asin"]
            insert_values = [asin]
            for f in ASIN_DATA_FIELDS:
                if f in ("asin", "screenshot_path"):
                    continue
                val = data.get(f)
                if val is not None:
                    insert_fields.append(f)
                    insert_values.append(val)

            if resolved_screenshot_path:
                insert_fields.append("screenshot_path")
                insert_values.append(resolved_screenshot_path)

            # 首次入库：baseline = 当前值（无论手动还是定时）
            insert_fields.extend([
                "baseline_price", "baseline_buybox_price",
                "baseline_stock_count", "baseline_stock_status",
                "baseline_title_bullets_hash", "baseline_updated_at",
            ])
            insert_values.extend([
                data.get("current_price"), data.get("buybox_price"),
                data.get("stock_count"), data.get("stock_status"),
                data.get("title_bullets_hash"), now,
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
                    (asin, batch_id)
                )

        return True

    async def save_result(self, data: dict, batch_id: int = None) -> bool:
        """兼容入口：直接保存结果（不走 lease 校验，用于测试/直接写入）"""
        asin = data.get("asin", "").strip()
        if not asin:
            return False
        if _is_parse_failure(data):
            logger.warning(f"解析失败数据跳过: {asin}")
            return False
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                result = await self._save_result_inner_unlocked(data, batch_id)
                await self._db.execute("COMMIT")
                return result
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def _get_done_screenshot_path(self, asin: str, batch_id: int = None) -> Optional[str]:
        queries = []
        if batch_id is not None:
            queries.append((
                """SELECT file_path FROM screenshots
                   WHERE asin = ? AND batch_id = ? AND status = 'done'
                   ORDER BY updated_at DESC, id DESC LIMIT 1""",
                (asin, batch_id),
            ))
        queries.append((
            """SELECT file_path FROM screenshots
               WHERE asin = ? AND status = 'done'
               ORDER BY updated_at DESC, id DESC LIMIT 1""",
            (asin,),
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
        if not asins:
            return {}

        remaining = list(dict.fromkeys(asins))
        mapping: Dict[str, str] = {}

        async def load(batch_filter: int = None):
            nonlocal remaining
            if not remaining:
                return

            placeholders = ",".join("?" * len(remaining))
            sql = (
                f"SELECT asin, file_path FROM screenshots "
                f"WHERE status = 'done' AND asin IN ({placeholders})"
            )
            params: List[Any] = list(remaining)
            if batch_filter is not None:
                sql += " AND batch_id = ?"
                params.append(batch_filter)
            sql += " ORDER BY updated_at DESC, id DESC"

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

    async def save_results_batch(self, results: List[dict], batch_id: int = None) -> int:
        """批量保存结果"""
        saved = 0
        for data in results:
            if await self.save_result(data, batch_id):
                saved += 1
        return saved

    async def get_batch_failures(
        self,
        batch_id: int,
        error_types: Optional[List[str]] = None,
        limit: int = 100000,
    ) -> List[Dict]:
        """返回指定批次失败任务明细，用于调用方按失败原因处理本批最新采集状态。"""
        where_parts = ["batch_id = ?", "status = 'failed'"]
        params: list = [batch_id]
        clean_types = [str(t).strip() for t in (error_types or []) if str(t).strip()]
        if clean_types:
            placeholders = ",".join("?" for _ in clean_types)
            where_parts.append(f"COALESCE(error_type, '') IN ({placeholders})")
            params.extend(clean_types)
        params.append(max(1, min(int(limit or 100000), 100000)))

        sql = f"""
            SELECT asin, status, error_type, error_detail, retry_count, worker_id, updated_at
            FROM tasks
            WHERE {' AND '.join(where_parts)}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
        """
        async with self.read() as rc, rc.execute(sql, params) as c:
            return [dict(r) for r in await c.fetchall()]

    # ==================== 查询操作（keyset 分页）====================

    async def get_results(self, batch_id: int = None, cursor_id: int = None,
                          limit: int = 50, search: str = None,
                          change_filter: str = "all",
                          direction: str = "next") -> Dict:
        """
        获取结果列表（keyset 分页）
        change_filter: all / price_stock / title_bullets / new
        direction: next (向后翻页) / prev (向前翻页)
        返回: {"items": [...], "has_more": bool, "next_cursor": int, "prev_cursor": int, "total": int}
        """
        join_parts = []
        count_join_parts = []
        join_params: list = []
        where_parts = []
        where_params: list = []

        # 批次筛选 - 通过 batch_asins JOIN
        if batch_id:
            join_parts.append("JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?")
            count_join_parts.append("JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?")
            join_params.append(batch_id)

        # 变动筛选 —— 由 `JOIN (SELECT DISTINCT asin FROM asin_changes ...)`
        # 改写成 EXISTS 半连接。等价性与两条注意事项见 common/pgdb/results_read.py
        # 的同名注释（DISTINCT 本就不放大行数；参数随谓词从 join_params 挪进
        # where_params；谓词文本不含 "d.id" 所以不会被 D-8 的 count 过滤误剔）。
        if change_filter in ("price_stock", "title_bullets"):
            # change_filter 的取值被上面这个成员判断限死在两个字面量上，不是外部拼接
            pred = ("EXISTS (SELECT 1 FROM asin_changes ac "
                    f"WHERE ac.asin = d.asin AND ac.change_type = '{change_filter}'")
            if batch_id:
                pred += " AND ac.batch_id = ?"
                where_params.append(batch_id)
            where_parts.append(pred + ")")
        elif change_filter == "new":
            if batch_id:
                sub = "JOIN batch_asins ba2 ON ba2.asin = d.asin AND ba2.batch_id = ? AND ba2.is_new = 1"
                join_params.append(batch_id)
                join_parts.append(sub)
                count_join_parts.append(sub)
            else:
                where_parts.append(
                    "EXISTS (SELECT 1 FROM asin_changes ac "
                    "WHERE ac.asin = d.asin AND ac.change_type = 'new')")

        # 搜索（支持逗号分隔的批量搜索）—— 限长防 DoS
        # FTS5 trigram 优化路径：对每个 term × 每列做 UNION 子查询，rowid 命中再 JOIN 回主表
        # 实测：原 LIKE 全表扫 23-46s -> 新 FTS UNION ~5-50ms (~1000x 加速)
        # 旧 LIKE fallback：term 长度 < 3 时（trigram 索引最小单位 3 字符）
        if search:
            # 单个请求最多 500 字符、最多 10 个关键词，每个关键词截断到 100 字符
            search = str(search)[:500]
            terms = [t.strip()[:100] for t in search.split(",") if t.strip()][:10]
            if terms:
                if any(len(t) < 3 for t in terms):
                    # 慢路径：含 < 3 字符的 term，trigram 无法加速，沿用旧 LIKE
                    or_clauses = []
                    for t in terms:
                        # ⚠ 谓词文本与模式函数都必须与 find_asins_by_search（删除路径）
                        #   共用真源。这两条路径给出不同的行集，就是 D-16 那起事故
                        #   （同一个 search，GET 读出来的和 DELETE 删掉的不是同一批）。
                        #   钉它的网：tests/test_search_like_escape_parity.py
                        or_clauses.append(SEARCH_TERM_OR)
                        pat = search_like_pattern(t)
                        where_params.extend([pat, pat, pat])
                    where_parts.append(f"({' OR '.join(or_clauses)})")
                else:
                    # 快路径：FTS5 trigram + UNION（每个 (term, column) 走独立 L1 索引）
                    # SQL 形态：
                    #   d.id IN (
                    #     SELECT rowid FROM asin_data_fts WHERE asin LIKE ?
                    #     UNION SELECT rowid FROM asin_data_fts WHERE title LIKE ?
                    #     UNION SELECT rowid FROM asin_data_fts WHERE brand LIKE ?
                    #     UNION ... (每多一个 term 再 +3 个 UNION)
                    #   )
                    fts_subs = []
                    for t in terms:
                        like_pattern = search_like_pattern(t)
                        fts_subs.append("SELECT rowid FROM asin_data_fts WHERE asin LIKE ?")
                        where_params.append(like_pattern)
                        fts_subs.append("SELECT rowid FROM asin_data_fts WHERE title LIKE ?")
                        where_params.append(like_pattern)
                        fts_subs.append("SELECT rowid FROM asin_data_fts WHERE brand LIKE ?")
                        where_params.append(like_pattern)
                    where_parts.append(f"d.id IN ({' UNION '.join(fts_subs)})")

        # 构建 count 查询参数（join_params + where_params，不含 cursor）
        count_params = join_params + where_params

        # keyset 分页
        if cursor_id is not None:
            if direction == "next":
                where_parts.append("d.id < ?")
            else:
                where_parts.append("d.id > ?")
            where_params.append(cursor_id)

        params = join_params + where_params

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        join_clause = " ".join(join_parts) if join_parts else ""
        count_join_clause = " ".join(count_join_parts) if count_join_parts else ""

        order = "DESC" if direction == "next" else "ASC"

        # 查询数据
        sql = f"""
            SELECT d.* FROM asin_data d
            {join_clause}
            WHERE {where_clause}
            ORDER BY d.id {order}
            LIMIT ?
        """
        params.append(limit + 1)  # 多取一条判断 has_more

        async with self.read() as rc, rc.execute(sql, params) as c:
            rows = await c.fetchall()

        items = [dict(r) for r in rows]
        has_more = len(items) > limit
        if has_more:
            items = items[:limit]

        if direction == "prev":
            items.reverse()

        await self._hydrate_screenshot_paths(items, batch_id)

        # 查询总数
        count_where = " AND ".join(
            [p for p in (where_parts[:-1] if cursor_id is not None else where_parts)]
        ) if where_parts else "1=1"
        # 移除 cursor 条件
        if cursor_id is not None and where_parts:
            count_where_parts = [p for p in where_parts if "d.id" not in p]
            count_where = " AND ".join(count_where_parts) if count_where_parts else "1=1"

        count_sql = f"SELECT COUNT(*) FROM asin_data d {count_join_clause} WHERE {count_where}"
        async with self.read() as rc, rc.execute(count_sql, count_params) as c:
            total = (await c.fetchone())[0]

        next_cursor = items[-1]["id"] if items else None
        prev_cursor = items[0]["id"] if items else None

        return {
            "items": items,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
            "total": total,
        }

    async def get_batch_asin_set(self, batch_id) -> set:
        """一个批次里的 ASIN 集合（``DELETE /api/results`` 的 batch_id 分支）。

        ``batch_asins`` 记的是"这一批采过哪些 ASIN"，与 ``tasks`` 不同：
        任务可以失败、可以重试，这张表只管入过队的 ASIN。删除端点用它来
        「删掉这一批的全部结果」以及与 asins/search 取交集。

        与 ``get_all_asins`` 的区别是后者读 ``asin_data``（全库已采 ASIN），
        两者不可互换。

        ⚠ **一处有意的行为对齐，Phase 3.8 显式声明**（原来只写成 asyncpg 传参细节，
        读起来像纯移植，不对）：``batch_id`` 走 ``int()`` 强制转换。
        这让 ``DELETE /api/results {"batch_id": "1"}``（JSON body 里字符串形状的 id）
        在 **PG 上从 500（asyncpg DataError）变成 200**，与 SQLite 拉齐 ——
        SQLite 靠类型亲和本来就吃字符串。
        方向上是 C4 要的（同类残余差异 e21e2c6 的提交信息里以「已知残余」记过
        ``/api/tasks/release`` 那条），但它是**行为变更**不是移植细节，
        所以钉在 ``tests/test_results_delete_api.py::test_batch_id_accepts_string_form``。
        """
        async with self.read() as rc, rc.execute(
            "SELECT asin FROM batch_asins WHERE batch_id = ?", (batch_id,)
        ) as c:
            return {row["asin"] for row in await c.fetchall()}

    async def find_asins_by_search(self, terms) -> set:
        """按模糊搜索词选中 ASIN 集合（``DELETE /api/results`` 的 search 分支）。

        terms: 已经切好、去空、限过长的关键词列表（切词与限长是 handler 的
            请求校验，不在这里做）。词之间是 **OR**。

        谓词与 ``get_results`` 的搜索分支**慢路径**使用同一份文本
        （SEARCH_TERM_OR），这正是 Phase 3.8 批 (3) 的收益：
        在这之前删除路径是 handler 里自己拼的 f-string，PG 侧靠
        ``pool._LIKE_QMARK_RE`` **按字面文本**把它改写成带 ``ESCAPE ''`` 的形式
        才跟读路径对齐 —— 把 ``LIKE ?`` 换个写法、三段 OR 拆开、甚至多一个空格，
        正则就不再命中，PG 当场换语义而两边都不报错（D-16）。现在两条路径引用
        的是同一个常量，改不歪。
        
        ⚠ SQLite 的 ``get_results`` 在 term ≥ 3 字符时走的是 **FTS5 trigram**
        子查询（快路径），文本形态与这里不同、行集相同（
        ``tests/test_search_like_escape_parity.py`` 逐条比对读/删两条路径的选中
        行集，正是钉这一条）。删除路径不套 FTS5：它一次全表选完就走，
        没有分页，加索引路径只会多一层「索引与主表不同步」的失败模式。

        走只读连接：这里只是选行，真正的删除在 ``delete_asins`` 里另开事务。
        """
        terms = [t for t in terms if t]
        if not terms:
            return set()
        or_clauses = []
        params = []
        for term in terms:
            or_clauses.append(SEARCH_TERM_OR)
            pat = search_like_pattern(term)
            params.extend([pat, pat, pat])
        where = " OR ".join(or_clauses)
        sql = f"SELECT d.asin FROM asin_data d WHERE {where}"
        async with self.read() as rc, rc.execute(sql, params) as c:
            return {row["asin"] for row in await c.fetchall()}

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

    async def get_result_by_asin(self, asin: str) -> Optional[Dict]:
        # 注意：先释放读连接再 _hydrate（其内部还要借读连接），避免池内嵌套借用导致死锁
        async with self.read() as rc, rc.execute("SELECT * FROM asin_data WHERE asin = ?", (asin,)) as c:
            row = await c.fetchone()
        if not row:
            return None
        item = dict(row)
        await self._hydrate_screenshot_paths([item])
        return item

    async def get_asin_changes(self, asin: str) -> List[Dict]:
        """获取 ASIN 的变动历史"""
        async with self.read() as rc, rc.execute(
            "SELECT * FROM asin_changes WHERE asin = ? ORDER BY id DESC", (asin,)
        ) as c:
            return [dict(r) for r in await c.fetchall()]

    # ==================== 截图操作 ====================

    async def get_pending_screenshots(self, batch_id: int, limit: int = 50) -> List[Dict]:
        async with self.read() as rc, rc.execute(
            "SELECT * FROM screenshots WHERE batch_id = ? AND status = 'pending' LIMIT ?",
            (batch_id, limit)
        ) as c:
            return [dict(r) for r in await c.fetchall()]

    async def update_screenshot_status(self, asin: str, batch_id: int, status: str,
                                       file_path: str = None, error: str = None) -> bool:
        now = now_ts()
        updated = False
        async with self._write_lock:
            await self._db.execute("BEGIN")
            if status == "done" and file_path:
                cursor = await self._db.execute(
                    "UPDATE screenshots SET status=?, file_path=?, updated_at=? WHERE asin=? AND batch_id=?",
                    (status, file_path, now, asin, batch_id)
                )
                updated = cursor.rowcount > 0
                if updated:
                    await self._db.execute(
                        "UPDATE asin_data SET screenshot_path=? WHERE asin=?",
                        (file_path, asin)
                    )
            elif status == "failed":
                cursor = await self._db.execute(
                    """UPDATE screenshots
                       SET status='failed', retry_count=retry_count+1,
                           error_detail=?, updated_at=?
                       WHERE asin=? AND batch_id=?""",
                    (error, now, asin, batch_id)
                )
                updated = cursor.rowcount > 0
            else:
                cursor = await self._db.execute(
                    "UPDATE screenshots SET status=?, updated_at=? WHERE asin=? AND batch_id=?",
                    (status, now, asin, batch_id)
                )
                updated = cursor.rowcount > 0
            await self._db.execute("COMMIT")
        return updated

    async def get_screenshot_progress(self, batch_id: int) -> Dict:
        sql = "SELECT status, COUNT(*) as cnt FROM screenshots WHERE batch_id = ? GROUP BY status"
        stats = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        async with self.read() as rc, rc.execute(sql, (batch_id,)) as c:
            async for row in c:
                stats[row["status"]] = row["cnt"]
        stats["total"] = sum(stats.values())
        return stats

    # ==================== 导出操作 ====================

    async def iter_results(self, batch_id: int = None, change_filter: str = "all",
                           batch_size: int = 500, columns: Optional[List[str]] = None):
        """流式迭代结果（keyset 分页，支持 batch_id + change_filter）。
        整个导出（可能数千次 LIMIT 查询、持续数分钟）借用同一条只读连接，
        全程不触碰写连接，导出再大也不会拖慢 worker 拉取/上传。

        columns: 仅投影这些 asin_data 列（导出实际需要的字段）；None 时回退 d.*。
            收窄投影可避免为每行读出 bullet_points / long_description / image_urls /
            category_tree 等大文本，显著降低宽表 + 大批次导出的磁盘 I/O。

        批次路径游标改用 ba.asin（走 PK 索引 (batch_id, asin)）：原先按 ba.rowid 排序时，
        batch_asins 无 (batch_id, rowid) 索引，SQLite 每翻一页都要 "USE TEMP B-TREE FOR
        ORDER BY" 把整批重排，导致大批次导出退化为 O(N²)。改用 asin 后过滤+游标+排序
        由 PK 索引一把命中，整批降到 O(N)（30k 行实测 ~14x，60k 行 ~27x）。
        """
        # 收窄投影：把导出需要的 asin_data 列拼成 "d.col" 列表；
        # 防注入起见与已知列集取交集（列名来自 EXPORTABLE_FIELDS 白名单，这里再兜一层）。
        proj_cols = None
        if columns:
            proj_cols = [c for c in dict.fromkeys(columns) if c in _ASIN_DATA_COLUMN_SET] or None

        # 游标：批次路径按 asin（TEXT），非批次路径按 asin_data.id（INTEGER）
        cursor = "" if batch_id else 0
        async with self.read() as rc:
            while True:
                if batch_id:
                    d_select = ", ".join(f"d.{c}" for c in proj_cols) if proj_cols else "d.*"
                    joins = [
                        "LEFT JOIN asin_data d ON d.asin = ba.asin",
                        "LEFT JOIN tasks t ON t.batch_id = ba.batch_id AND t.asin = ba.asin",
                    ]
                    join_params: list = []
                    where = ["ba.batch_id = ?", "ba.asin > ?"]
                    where_params: list = [batch_id, cursor]

                    # 同 get_results：改 EXISTS 半连接。
                    # ⚠ 这一处的驱动表是 **batch_asins (ba)**，不是 asin_data —— 原
                    #   join 条件写的就是 `ac.asin = ba.asin`，EXISTS 里必须照抄 ba.asin。
                    # ⚠ 参数从 join_params 挪到 where_params：`params = join_params +
                    #   where_params + [batch_size]`，这一条排在 ba.batch_id / ba.asin
                    #   之后追加，与 SQL 文本顺序天然对齐。
                    if change_filter in ("price_stock", "title_bullets"):
                        where.append(
                            "EXISTS (SELECT 1 FROM asin_changes ac WHERE ac.asin = ba.asin "
                            f"AND ac.change_type='{change_filter}' AND ac.batch_id=?)")
                        where_params.append(batch_id)
                    elif change_filter == "new":
                        where.append("ba.is_new = 1")

                    params = join_params + where_params + [batch_size]
                    sql = f"""
                        SELECT {d_select},
                               ba.asin AS batch_requested_asin,
                               ba.is_new AS batch_is_new,
                               t.status AS batch_task_status,
                               t.error_type AS batch_error_type,
                               t.error_detail AS batch_error_detail,
                               t.retry_count AS batch_retry_count,
                               t.auto_retry_count AS batch_auto_retry_count,
                               t.worker_id AS batch_worker_id,
                               t.updated_at AS batch_task_updated_at,
                               d.updated_at AS batch_asin_data_updated_at,
                               CASE WHEN d.asin IS NULL THEN 0 ELSE 1 END AS batch_has_asin_data
                        FROM batch_asins ba
                        {' '.join(joins)}
                        WHERE {' AND '.join(where)}
                        ORDER BY ba.asin ASC
                        LIMIT ?
                    """

                    async with rc.execute(sql, params) as c:
                        rows = await c.fetchall()
                    if not rows:
                        break
                    for row in rows:
                        d = dict(row)
                        cursor = d["batch_requested_asin"]
                        d["asin"] = d.get("batch_requested_asin") or d.get("asin")
                        yield d
                    continue

                # 非批次路径（导出全部）：主键 id 游标，本就最优。
                # 收窄投影时须显式带上 d.id 作游标。
                if proj_cols:
                    d_select = "d.id, " + ", ".join(f"d.{c}" for c in proj_cols)
                else:
                    d_select = "d.*"
                joins = []
                join_params = []
                where = ["d.id > ?"]
                where_params = [cursor]

                # 同上，改 EXISTS 半连接。这三条都不带参数，绑定顺序不受影响。
                if change_filter in ("price_stock", "title_bullets", "new"):
                    where.append(
                        "EXISTS (SELECT 1 FROM asin_changes ac WHERE ac.asin = d.asin "
                        f"AND ac.change_type='{change_filter}')")

                join_clause = " ".join(joins)
                where_clause = " AND ".join(where)
                params = join_params + where_params + [batch_size]

                sql = f"SELECT {d_select} FROM asin_data d {join_clause} WHERE {where_clause} ORDER BY d.id ASC LIMIT ?"

                async with rc.execute(sql, params) as c:
                    rows = await c.fetchall()
                if not rows:
                    break
                for row in rows:
                    d = dict(row)
                    cursor = d["id"]
                    yield d

    # ==================== 统计 ====================

    async def get_total_asins(self) -> int:
        async with self.read() as rc, rc.execute("SELECT COUNT(*) FROM asin_data") as c:
            return (await c.fetchone())[0]

    async def get_all_asins(self) -> List[str]:
        """获取所有已知 ASIN（用于自动采集）"""
        result = []
        async with self.read() as rc, rc.execute("SELECT asin FROM asin_data ORDER BY id") as c:
            async for row in c:
                result.append(row["asin"])
        return result

    async def get_change_stats(self, batch_id: int = None) -> Dict:
        """获取变动统计"""
        batch_filter = "WHERE batch_id = ?" if batch_id else ""
        params = (batch_id,) if batch_id else ()

        stats = {}
        sql = f"SELECT change_type, COUNT(DISTINCT asin) as cnt FROM asin_changes {batch_filter} GROUP BY change_type"
        async with self.read() as rc, rc.execute(sql, params) as c:
            async for row in c:
                stats[row["change_type"]] = row["cnt"]
        return stats

    # ==================== 管理（清库）====================

    async def clear_all_data(self) -> None:
        """清空全部业务数据，并把自增 id 归位（``DELETE /api/database`` 的库侧动作）。

        **只管库，不碰截图文件** —— 文件清理留在 handler 里
        （``server/api/debug.py`` 的 ``api_clear_database``），
        因为它跟 ``common.config.SCREENSHOT_DIR`` 绑定，不是数据库的事。

        SQLite 侧「把自增归位」= 删掉 ``sqlite_sequence`` 里的行；PG 侧是
        ``ALTER TABLE ... ALTER COLUMN id RESTART WITH 1``（见
        ``common/pgdb/admin.py`` 的同名方法）。Phase 3.8 批 (1) 之前这条差异
        由 ``common/pgdb/pool.py`` 的 ``_STATEMENT_OVERRIDES`` 做整句文本替换，
        代价是 handler 里那句 SQL 改一个字符就静默失配。现在差异落在各自的
        实现里，改不坏。

        失败必须回滚：这段是裸 ``BEGIN`` 块，PG 下事务粘在那条唯一的写连接上，
        不回滚就会让之后每一次 BEGIN 都撞上「嵌套 BEGIN」守卫
        （与 ``server/app.py`` 的 ``_rollback_quietly`` 同一个理由）。
        捕 ``BaseException`` 是因为 handler 被客户端断开时会吃到 CancelledError。
        """
        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                for table in CLEAR_TABLES:
                    await self._db.execute(f"DELETE FROM {table}")
                await self._db.execute("DELETE FROM sqlite_sequence")
                await self._db.execute("COMMIT")
            except BaseException:
                try:
                    await self._db.execute("ROLLBACK")
                except BaseException:
                    pass
                raise
