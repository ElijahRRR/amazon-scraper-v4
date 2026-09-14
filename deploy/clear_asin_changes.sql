-- deploy/clear_asin_changes.sql —— 清空变动记录表（2026-09）
--
-- 什么时候用：变动检测已默认关闭（config.CHANGE_DETECTION_ENABLED=0），
-- `asin_changes` 不再增长，但存量行还占着空间。线上曾约 140 万行。
--
-- ⚠ 不可逆。执行前确认你不再需要历史变动记录。
-- ⚠ 只清这一张表。**不要**用 `DELETE /api/database` —— 那会连 asin_data
--   和全部截图文件一起清掉。
--
-- 为什么 TRUNCATE 而不是 DELETE：
--   * DELETE 要逐行写 WAL 并留下 140 万个死行，之后还得 VACUUM 才能把空间
--     还给操作系统，而 VACUUM 期间照样压着这台 2 核机器；
--   * TRUNCATE 直接换文件、立即释放空间，且不产生死行。
--   代价是 TRUNCATE 要 ACCESS EXCLUSIVE 锁。这张表已经没有写入方，读它的只有
--   `change_filter=` 与 `/api/changes/stats`，所以锁窗口只有毫秒级。
--
-- RESTART IDENTITY：把自增 id 归零。这张表的 id 不是对外契约的一部分
--   （没有任何接口回显它），归零只是让今后万一重新打开检测时 id 从 1 开始。

BEGIN;

-- 先看一眼将要删掉多少（执行日志里留个底）
SELECT count(*) AS rows_to_delete FROM asin_changes;

TRUNCATE TABLE asin_changes RESTART IDENTITY;

-- 确认已空
SELECT count(*) AS rows_remaining FROM asin_changes;

COMMIT;

-- TRUNCATE 已经把空间还给了操作系统，不需要 VACUUM FULL。
-- 但顺手更新一下统计值：产品列表页的「共 N 条」走 pg_class.reltuples 估算，
-- ANALYZE 能让它立刻反映真实情况（不做也会由 autovacuum 追上）。
ANALYZE asin_changes;
