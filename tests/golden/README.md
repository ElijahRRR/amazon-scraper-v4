# 黄金样本夹具（PG 迁移 Phase 0）

## 这是什么

把当前 SQLite 实现的**对外 HTTP 行为**逐步录成基线，用于在把存储层移植到
PostgreSQL 的过程中反复校验「行为没变」。

存在的理由：仓库原有测试只覆盖 3 个纯函数（`test_ziputil` / `test_delivery_parse` /
`test_session_slot`），**没有任何 HTTP 层测试**。要重写 5000 行存储层而不改变对外行为，
没有对照物就是闭眼改。erpAPI 的端点清单尚未提供，所以覆盖面刻意做宽——宁可多录。

## 用法

```bash
# 一次性：装 server 端依赖（不含 worker/截图那套重依赖）
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 场景自检：两次独立运行必须完全一致。失败 = 夹具还有没擦干净的不可重复源，
# 此时录出来的基线是废的，先修这个
.venv/bin/python -m tests.golden.run selfcheck

# 录基线（只在 SQLite 版上跑一次，移植期间不要再跑，否则等于把 bug 录进基线）
.venv/bin/python -m tests.golden.run record

# 校验（移植期间反复跑）
.venv/bin/python -m tests.golden.run verify

# 或走 pytest
.venv/bin/python -m pytest tests/golden/ -q
```

## 覆盖了什么

64 步，一条完整生命周期：

- 空库状态下的全部只读端点 + `/openapi.json`
- 上传 xlsx（含 per-ASIN 邮编列）/ txt；**同名批次重传**（现状是静默 no-op，已钉住）
- worker 心跳 → 拉任务 → 批量提交 / 单条提交 / 失败提交
- **lease 门的双向断言**（见下）
- 结果查询：翻页（next / prev）、搜索（FTS 快路径 + <3 字符 LIKE 慢路径）、变动筛选、详情、404
- 第二轮采集同一 ASIN（**覆盖更新语义**——这正是迁移后要新增事件流的原因，
  但移植阶段必须保持不变，这一步就是那条不变式的锚点）
- 导出：CSV / xlsx / 全量 / 选定字段 / 不存在的批次
- 设置读写、批次重试、诊断端点、5 个 HTML 页面、删除批次

## 规范化原则：擦得越少越好

每擦掉一个字段，就少一份发现移植 bug 的机会。所以只擦真正不可重复的东西：

| 对象 | 处理 |
|---|---|
| 时间戳字符串 | → `<TS>` |
| 时钟/运行时长字段（`first_seen`、`uptime`、`duration_seconds`…） | → `<VOLATILE>`，但**字段的存在与类型仍被比较** |
| 临时目录路径 | → `<TMP>` |
| `/api/diagnostic`、`/api/_debug/lock-stats` | 数字 → `<NUM>`，**结构仍逐字段比较** |
| xlsx | 解析成 `{sheet, header, rows}`（zip 字节含时间戳，不可逐字节比） |
| 截图 zip | 成员名列表 |
| HTML 页面 | 只留「是否有 traceback / jinja 错误 / 大小量级」 |

其余全部逐字段比较，包括 55 列的完整 `asin_data` 行。

## 变异测试（夹具本身的验收）

一个抓不到回归的夹具等于没有。已实测两种变异：

| 变异 | 结果 |
|---|---|
| 给 `asin_data` 加一列 | ✅ 捕获 21 处差异。证实审计的判断：`SELECT d.*` 无 `response_model`，**任何新加的列都会泄进 erpAPI 的响应** |
| 静默删掉 `accept_success_result` 的 lease 校验 | ✅ 捕获 45 处差异，头三行直指根因 |

**第二个变异第一次没被抓到**，这暴露了场景本身的缺陷并已修掉：原来的 stale 测试拿一个
**已 done** 的任务去试，而 lease 校验的 WHERE 同时含 `lease_epoch=?` 和
`status='processing'`——status 条件本身就让 rowcount=0，lease 校验被完全遮蔽。
现在用一个**仍处于 processing** 的专用探针任务，并做双向断言：

1. 过期 lease → 必须 stale，且该 ASIN 此刻必须查不到（404）
2. 正确 lease 的同一任务 → 必须被受理（否则「一律拒绝」也能骗过第 1 条）

## strict 与非 strict

- `record` / `selfcheck` 用 **strict**：`expect` 不符立即抛异常。此时 `expect` 守护的是
  场景脚本自身的正确性——录进一个错误的基线比没有基线更糟。
- `verify` 用**非 strict**：跑完全程，一次给出全部差异。撞到第一处就停会把后面的问题
  藏起来，逼人一轮只修一个。

## 移植期间怎么用

1. 移植前：`record` 一次，把 `samples/baseline.json` 提交进版本库。
2. 移植中：每改一块跑 `verify`。**任何差异都要先解释清楚**，再决定是 bug 还是有意变更。
3. 有意变更（例如已确认的 `crawl_time` 改带时区 UTC）：改完重录基线，
   并在提交信息里写明为什么这次基线变了。
