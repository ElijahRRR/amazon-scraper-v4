# erpAPI 对接契约 —— 采集侧（amazon-scraper-v4）

> **读者**：erpAPI 的实现者。照这份文档改代码即可，不需要读采集侧源码。
>
> **权威来源**：本文每一条论断后面都跟着 `file:line` 或实测数字。
> 代码与本文不一致时**以代码为准**，并请开 issue —— 本文的每一条行为都有
> 守卫用例钉住（见 §8），代码改了而用例没红，说明是本文漏写。
>
> **版本**：2026-08-07。本轮改了 `POST /api/upload` 的撞名语义与
> `GET /api/results` 的单页上限，两处都是**有意的、破坏性的**变更，
> 详见 §5「与旧系统的差异」。

---

## 0. 端点总表

| # | 端点 | 用途 | 本轮状态 |
|---|---|---|---|
| 1 | `GET /api/export/incremental` | catalog_sync 拉增量 | **契约 v1，不许单方面改**（§4.8） |
| 2 | `POST /api/upload` | 提交采集批次 | **行为已改**：撞名 → 409（§4.1） |
| 3 | `GET /api/batches/{batch_name}/status` | 轮询批次状态 | 保留，未改（§4.2） |
| 4 | `GET /api/results` | 拉结果，游标翻页 | **单页上限 200 → 1000**（§4.3） |
| 5 | `GET /api/batches/{batch_id}/failures` | 完整失败明细 | 保留，旧坑在我们这边本来就不存在（§4.4） |
| 6 | `POST /api/batches/{batch_id}/prioritize` | 插队 | 保留，但 `ok:true` 不代表批次存在（§4.6） |
| 7 | `GET /static/screenshots/...` | 取截图 | 保留，未改（§4.7） |

> 曾经的第 6 个端点 `GET /api/batches/{batch_name}/errors`（失败明细旧接口）
> 已经**删除**，不是废弃——`失败明细`统一走 `GET /api/batches/{batch_id}/failures`
> （§4.4）。历史细节见 §5.4。

七个端点在本仓库**全部存在**，路由声明位置：

| 端点 | 声明处 |
|---|---|
| `POST /api/upload` | `server/app.py:1053` |
| `GET /api/batches/{batch_name}/status` | `server/app.py:1266` |
| `POST /api/batches/{batch_id}/prioritize` | `server/app.py:1354` |
| `GET /api/batches/{batch_id}/failures` | `server/app.py:1433` |
| `GET /api/results` | `server/api/results.py:151` |
| `GET /api/export/incremental` | `server/api/export_incremental.py:375` |
| `/static/**`（截图） | `server/app.py:253`（`StaticFiles` 挂载） |

---

## 1. 通用约定

### 1.1 两个存储后端的行为一致

采集侧同时支持 SQLite 与 PostgreSQL（`DB_BACKEND` 环境变量）。
**本文描述的所有行为在两个后端上逐字节一致**，这是仓库的硬约束（C4），
由黄金基线的双后端 verify 看守。§5 与 §4 里每一个「实测」数字都是两个后端各跑
一遍得到的；下文凡是两边不同的地方会显式标注（目前只有一处：
`/api/export/incremental` 在 SQLite 上恒回 503，见 §4.8）。

### 1.2 三种错误响应形状 —— **它们不是一个形状，客户端要分别处理**

这是接入时最容易出错的地方。采集侧对外一共有三种错误体：

**(a) FastAPI 参数校验失败（422）** —— 框架产生，`detail` 是**数组**：

```json
{"detail": [{"type": "less_than_equal", "loc": ["query", "limit"],
             "msg": "Input should be less than or equal to 1000",
             "input": "1001", "ctx": {"le": 1000}}]}
```

**(b) 业务错误（400 / 404 / 409 / 413）** —— `HTTPException`，`detail` 是
**字符串或对象**：

```json
{"detail": "批次不存在: nope"}
```
```json
{"detail": {"error": "batch_name_conflict", "message": "...",
            "batch_id": 1, "batch_name": "...", "status_url": "..."}}
```

> ⚠ **`detail` 的类型不固定**。目前只有 `POST /api/upload` 的 409 是对象，
> 其余 4xx 都是字符串。别直接把 `detail` 塞进字符串拼接 —— 采集侧自己的前端
> 就踩过这个，修法见 `server/templates/tasks.html:231-235`（判 `typeof`）。

**(c) catalog_sync 契约错误（`/api/export/incremental` 专用）** —— 统一形状，
由 `server/api/sync.py:217` 的 `_err` 产生：

```json
{"error": "cursor_below_retention", "detail": "人读的说明",
 "server_time_utc": "2026-08-07T11:33:20.195593Z", "cursor": 0,
 "min_available_cursor": 676, "max_cursor": 700}
```

`error` 是**机器读**的稳定枚举，`detail` 是人读的。

**(d) 未捕获异常（500）** —— 全局处理器 `server/app.py:302`：

```json
{"error": "internal_error", "detail": "服务器内部错误", "request_id": "<32 位 hex>"}
```

`request_id` 与服务端日志里的 `request_id=` 一一对应，报障时请带上它。
body 不泄漏任何异常细节，这是有意的。

### 1.3 错误码封闭集

所有机器读的 `error` 码登记在 `server/api/sync.py:132-149` 的 `ERROR_CODES`：

```
ack_ahead_of_stream, batch_name_conflict, cursor_ahead_of_stream,
cursor_below_retention, event_stream_unavailable, export_token_not_configured,
gen_mismatch, internal_error, invalid_export_token, invalid_parameter,
range_too_wide
```

漂移由 `tests/test_error_codes.py` 看守（调用点 ⊆ 本集合、文档里出现的码 ⊆ 本集合）。
**一个不在这张表里的 `error` 值，采集侧不会发出来。**

### 1.4 时间戳格式

| 字段 | 格式 | 出处 |
|---|---|---|
| `created_at` / `updated_at` / `completed_at` 等 DB 列 | `YYYY-MM-DD HH:MM:SS`（**UTC，秒精度，空格分隔，不带时区后缀**） | `common/core/timeutil.py` 的 `TS_FMT` |
| `crawl_time`（采集时刻） | `YYYY-MM-DDTHH:MM:SSZ`（RFC3339） | `worker/parser.py` 的 `_CRAWL_TIME_FMT` |
| `server_time_utc`（仅错误体 (c)） | RFC3339 带微秒 | `server/api/sync.py` 的 `_now_iso` |

⚠ **前两者不是一个格式，别对齐**。DB 列的 `' '` 分隔格式是**契约**不是风格：
所有时间比较都是 `text` 列上的字符串比较（字典序 == 时间序），
换成 RFC3339 会让超时回收静默漏行。理由完整写在 `common/core/timeutil.py` 文件头。

### 1.5 鉴权

除 `GET /api/export/incremental` 外，**本文覆盖的端点目前全部无鉴权**。
`/api/export/incremental` 用 `X-Export-Token` 请求头（可选，见 §4.8）。

---

## 2. 推荐接入流程

```
POST /api/upload                        -> 200 {batch_id, status_url, ...}
                                        -> 409 {detail:{batch_id, status_url}}  # 换个名字，或直接接着轮询
       |
       |  首选：注册 callback_url，被动等 batch.completed（§6.1）
       |  兜底：GET {status_url} 轮询，间隔见 §6.2
       v
status.status == "completed"
       |
       +-> GET /api/results?batch_id=&cursor=&limit=1000   # 翻页拉成功结果（§4.3）
       +-> GET /api/batches/{batch_id}/failures            # 拉完整失败明细（§4.4）
       +-> GET /static/screenshots/{batch_name}/{asin}.png # 逐张取截图（§4.7）
           或 GET /api/export/{batch_name}/screenshots     # 整批打包 zip
```

---

## 3. 不可单方面改的对外契约

以下两组是**对外契约**，采集侧与消费侧任何一方都不许单方面修改；
改动需两侧同步、并按各自的版本规则升版本号。

### 3.1 `GET /api/export/incremental` —— 契约 v1

- 权威原本在沃尔玛侧 `docs/scraper_migration_brief.md` 第五节；
  采集侧存档副本在 `docs/incremental_export_contract.md`。
- **沃尔玛侧已按 v1 实现并上线**，采集侧不许单方面改。
- 响应体里带 `contract_version: 1`（`server/api/export_incremental.py:444`）。
- 改动流程：两侧同步升 `contract_version`。
  （`docs/incremental_export_contract.md` 里已登记 3 处待补进沃尔玛侧 §5 的空白，
  它们是「填补原文未定义的空白」，不改变已写死的行为，因此**仍是 v1**。）

### 3.2 本文档覆盖的 erpAPI 端点（#2 ~ #8）

一经本文发布即为对外契约。具体地：

- **路径、HTTP 方法、成功响应的字段名与类型**：不许改。
- **参数的边界值**（`limit` 的 `le=` / `ge=`）：它们会渲染进 `/openapi.json`
  的 `maximum` / `minimum`，而 `/openapi.json` 是黄金基线**逐字节钉死**的一步
  （`tests/golden/samples/sqlite_baseline.json` 的 `openapi_schema`）。
  所以**改一个 `le=` 就是改契约**，不是改实现细节 ——
  这一条明确写在 `server/api/results.py:42-48`。
- **错误码**：`error` 字段的字面量值不许改（§1.3 的封闭集）。
- 变更流程：先在本文档写明改了什么、为什么，再改代码，再重录黄金基线，
  两个后端 verify。

> **例外（可以单方面加）**：往成功响应里**加**字段。
> 但注意 `GET /api/results` 的 `items[]` 是 `SELECT d.*` 且**没有
> `response_model`**（`server/api/results.py:151-166`），
> 所以**任何加到 `asin_data` 表的列都会自动泄进响应**（当前 55 个键）。
> 严格反序列化的客户端会因此崩 —— 请按「忽略未知字段」实现。

---

## 4. 逐端点规格

### 4.1 `POST /api/upload` —— 提交采集批次

**声明**：`server/app.py:1053`

`multipart/form-data`。

#### 参数

| 参数 | 位置 | 类型 | 必填 | 默认 | 边界 |
|---|---|---|---|---|---|
| `file` | form file | `.xlsx` / `.csv` / `.txt` | ✅ | — | **≤ 50 MB**，超出 413（`MAX_UPLOAD_BYTES`，`server/app.py:891`） |
| `batch_name` | form | str | ❌ | `batch_YYYYMMDD_HHMMSS`（**东八区**，`server/app.py:363`） | 不许含 `/`、`\`、`\x00`、`..`（路径穿越校验，`server/app.py:1040-1050`）；批次名会直接拼成截图目录名 |
| `zip_code` | form | str | ❌ | 服务端运行期设置 / `config.DEFAULT_ZIP_CODE` | 5 位数字 |
| `needs_screenshot` | form | bool | ❌ | `false` | — |
| `callback_url` | form | str | ❌ | `null` | 必须 `http(s)://` + **公网**域名/IP，否则 400（SSRF 校验，`_is_safe_callback_url`）。IP 字面量不做 DNS 解析 |
| `external_id` | form | str | ❌ | `null` | **截断到 120 字符**（`server/app.py:1167`），超出部分静默丢弃 |
| `expand_variants` | form | bool | ❌ | `false` | 开启后批次完成判定会多轮展开变体，见 §6.3 |

**文件格式**：
- `xlsx` / `csv`：A 列 = ASIN，B 列（可选）= 该 ASIN 单独的邮编（5 位数字）。
  A 列不是 ASIN 时，退化为扫描整行找 ASIN（不带邮编）。
- `txt`：每行一个 ASIN，无邮编列。

**ASIN 归一**：正则 `^B[0-9A-Z]{9}$`（`common/core/idents.py` 的 `ASIN_RE`）。
调用侧做 `.strip().upper()`，所以 `b00aaa0003` 与 `" B00AAA0004 "` 都收
（实测：`["b00aaa0003", " B00AAA0004 ", "not-an-asin"]` → `total_asins=2`）。
文件内重复的 ASIN 按**首次出现顺序**去重。

#### 成功响应 `200` —— **200 恒等于「新建了一个批次」**

```json
{
  "batch_id": 1,
  "batch_name": "probe_a",
  "external_id": null,
  "total_asins": 2,
  "inserted": 2,
  "per_asin_zip_count": 0,
  "invalid_zip_rows": 0,
  "callback_url": null,
  "status_url": "http://testserver/api/batches/probe_a/status"
}
```

| 字段 | 确切含义 |
|---|---|
| `batch_id` | 新建批次的自增 id |
| `batch_name` | 实际使用的批次名（没传就是服务端生成的那个） |
| `external_id` | **批次实际存下来的**值（已截断到 120 字符），不是请求里的值 |
| `total_asins` | 文件里识别出的**去重后**有效 ASIN 数 |
| `inserted` | 实际入队的任务数。**200 时恒等于 `total_asins`** —— 批次是全新的，`tasks` 表里不可能有它的行。实测 4 组（含文件内重复、跨批次重复 ASIN、500 行）全部相等 |
| `per_asin_zip_count` | B 列指定了合法邮编的 ASIN 数 |
| `invalid_zip_rows` | B 列非空但不是合法 5 位邮编的行数（这些行的 ASIN **照样入队**，用批次邮编） |
| `callback_url` | **批次实际存下来的**值，不是请求里的值 |
| `status_url` | 直接可打的轮询地址 |

> `external_id` / `callback_url` 之所以「回显存下来的值」而不是请求值，
> 是因为「回显请求值」正是旧坑 c 那个回调撒谎 bug 的载体
> （`server/app.py:1212-1215` 有完整说明）。代价是一次主键级 SELECT。

#### 错误响应

| 状态码 | `detail` | 触发条件 |
|---|---|---|
| **409** | **对象**，见下 | **批次名已存在** |
| 400 | `"未找到有效 ASIN"` | 文件里一个合法 ASIN 都没有 |
| 400 | `"非法 callback_url（<原因>）。仅接受 http(s)://公网域名/IP"` | SSRF 校验不过 |
| 413 | `"文件过大：<N>MB，上限 50MB"` | 文件 > 50 MB |

**409 Conflict**（`server/app.py:1199-1205`）：

```json
{"detail": {
  "error": "batch_name_conflict",
  "message": "批次名已存在: golden_batch_a（未合并，也未改动既有批次）",
  "batch_id": 1,
  "batch_name": "golden_batch_a",
  "status_url": "http://testserver/api/batches/golden_batch_a/status"
}}
```

- `batch_id` 是**既有批次**的 id，可以直接拿 `status_url` 接着轮询，不必再查一次。
- **409 之后既有批次一个字节都没动**：不加 ASIN、不改 `external_id`、
  不改 `callback_url`（`tests/test_upload_batch_name_conflict.py::test_conflicting_upload_does_not_touch_the_existing_batch`）。
- ⚠ 撞名那一发**照样烧掉一个批次自增号**（`INSERT` 照发、只是被 `IGNORE`）。
  所以 batch id 序列会有洞，别把它当计数器用。
  （`common/database.py:667-669` 明写这条性质不许改。）

#### 怎么用 409 做安全重试 —— 见 §5.1、§5.2

---

### 4.2 `GET /api/batches/{batch_name}/status` —— 轮询批次状态

**声明**：`server/app.py:1266`。注意路径参数是**批次名**不是 id。

#### 成功响应 `200`

```json
{
  "batch_name": "shape_a",
  "batch_id": 1,
  "external_id": null,
  "status": "running",
  "stats": {"total": 6, "done": 4, "failed": 2, "open": 0,
            "success_rate": 0.6667, "duration_seconds": 0},
  "screenshots": {"total": 0, "done": 0, "failed": 0, "open": 0},
  "completed_at": null,
  "callback": {"url": null, "status": null, "attempts": 0,
               "last_error": null, "sent_at": null, "next_retry_at": null}
}
```

| 字段 | 含义 |
|---|---|
| `status` | `running` / `completed` / `failed`。**这是权威的完成信号**，见下面的 ⚠ |
| `stats.open` | 非终态任务数（既不是 `done` 也不是 `failed`） |
| `stats.success_rate` | `done / total`，4 位小数；`total == 0` 时为 `0.0` |
| `stats.duration_seconds` | 创建到完成（未完成则到当前）的秒数；时间戳解析失败时为 `null` |
| `completed_at` | 完成时刻；未完成为 `null` |
| `callback.status` | 回调投递状态；`attempts` 是已尝试次数 |

> ⚠ **不要用 `stats.open == 0` 判定批次完成。**
> 实测：4 成功 + 2 失败之后 `stats.open == 0` 但 `status` 仍是 `"running"`。
> 原因是 `status` / `completed_at` 由后台的 `_completion_watcher`
> （`server/app.py:562`）异步落库，它每 `_COMPLETION_WATCHER_INTERVAL = 2.0` 秒
> 醒一次（`server/app.py:924`）。所以 `open == 0` 会**领先** `status=="completed"`
> 约 0~2 秒。
> 对 `expand_variants=true` 的批次，`open == 0` 更是**主动错的**：
> watcher 在全部终态后还会调 `expand_batch_variants` 往**同一批次**里追加变体任务，
> 追加 > 0 则本批未真正完成（`server/app.py:588-592`）。
> **权威判据只有 `status == "completed"`（等价地 `completed_at != null`）。**

#### 错误响应

| 状态码 | body |
|---|---|
| 404 | `{"detail": "批次不存在: <name>"}` |

---

### 4.3 `GET /api/results` —— 拉结果（游标翻页）

**声明**：`server/api/results.py:151`

#### 参数

| 参数 | 类型 | 默认 | 边界 |
|---|---|---|---|
| `batch_id` | int | `null`（= 全库） | 无校验 |
| `cursor` | int | `null`（= 第一页） | **无 `ge=0`**，负数照收（实测回 200 空页） |
| `limit` | int | `50` | **`le=1000`**（`MAX_PAGE_LIMIT`，`server/api/results.py:148`）。超限 **422 拒绝，不截断** |
| `search` | str | `null` | 服务端截到 500 字符，逗号分隔最多 10 个词，每词截到 100 字符；多词是 OR；匹配 asin/title/brand |
| `change_filter` | str | `"all"` | `all` / `price_stock` / `title_bullets` / `new` |
| `direction` | str | `"next"` | ⚠ **只有 `next` 和 `prev` 两个值有定义**，见下 |

> ⚠ **`direction` 的非法值不会报错**。实现是
> `order = "DESC" if direction == "next" else "ASC"`（`common/pgdb/results_read.py:242`），
> 而 `items.reverse()` 只在 `direction == "prev"` 时执行。
> 实测 `direction="sideways"`、`cursor=2` 返回 `[3, 4]`，
> 而 `direction="prev"` 同游标返回 `[4, 3]` —— 行集合相同、**顺序相反**，
> 且两者都是 200。**请只发 `next` / `prev` 这两个字面量。**

#### 成功响应 `200`

```json
{"items": [...], "has_more": true, "next_cursor": 3, "prev_cursor": 4, "total": 4}
```

| 字段 | 含义 |
|---|---|
| `items` | 结果行数组。**当前 55 个键**（`asin`/`title`/`brand`/`current_price`/`screenshot_path`/…），且**会随 `asin_data` 加列而增长** —— 见 §3.2 的例外 |
| `has_more` | 还有下一页。**翻页的唯一终止条件** |
| `next_cursor` | 下一页的游标（`direction=next` 用）= 本页最后一行的 `id` |
| `prev_cursor` | 上一页的游标（`direction=prev` 用）= 本页第一行的 `id` |
| `total` | **不含游标谓词**的全集计数。翻页途中恒定不变，可以直接拿去算进度条 |

`items[].screenshot_path` 是形如 `/static/screenshots/<batch_name>/<asin>.png`
的相对路径，或 `null`（无截图）。占位串 `"none"` / `"null"` / 空串会被统一成
`null`（`common/core/asindata.py:22-29`），所以**只需判 `null` 一种缺失形态**。

#### 游标是严格单调的 —— 翻页不会卡死

keyset 谓词用的是**严格**不等号：

- `direction=next`：`d.id < ?` + `ORDER BY d.id DESC`
  （`common/pgdb/results_read.py:231`、`common/database.py:2218`）
- `direction=prev`：`d.id > ?` + `ORDER BY d.id ASC`
  （`common/pgdb/results_read.py:233`、`common/database.py:2220`）

`next_cursor = items[-1].id` / `prev_cursor = items[0].id`
（`common/pgdb/results_read.py:281-282`）。
`d.id` 是主键，非空且无并列，所以游标严格前进、翻页一定终止。

**守卫用例**：`tests/test_results_cursor_liveness.py`。
它翻满整个结果集并断言四条：`next_cursor` 严格递减、`prev_cursor` 严格递增、
硬轮次上限（超了就判定卡死）、逐页 id 两两不相交且并集等于全集。

#### 正确的翻页写法

```python
cursor, out = None, []
while True:
    p = {"limit": 1000, "direction": "next", "batch_id": bid}
    if cursor is not None:
        p["cursor"] = cursor
    r = GET("/api/results", params=p).json()
    out += r["items"]
    if not r["has_more"]:
        break
    cursor = r["next_cursor"]      # 严格递减，一定收敛
```

**终止条件只认 `has_more`**，不要靠「本页行数 < limit」——
那在实现里是靠「多取一条」判定的，等价但更脆。

#### 错误响应

| 状态码 | 触发 |
|---|---|
| 422 | `limit > 1000` 或 `limit` 不是整数。body 是形状 (a) |

---

### 4.4 `GET /api/batches/{batch_id}/failures` —— 完整失败明细（**推荐**）

**声明**：`server/app.py:1433`。路径参数是 **batch_id（int）**，不依赖批次名。

#### 参数

| 参数 | 类型 | 默认 | 边界 |
|---|---|---|---|
| `error_type` | str | `null` | **逗号分隔**的多值过滤，空白自动 strip |
| `limit` | int | `100000` | `ge=1, le=100000`。超出 422 |

#### 成功响应 `200`

```json
{
  "batch_id": 1,
  "count": 2,
  "failed_tasks": [
    {"asin": "B00SHAPE05", "status": "failed", "error_type": "variant_offset",
     "error_detail": "probe non-retryable", "retry_count": 1,
     "worker_id": "w1", "updated_at": "2026-08-07 11:34:13"}
  ]
}
```

排序：`ORDER BY updated_at DESC NULLS LAST, id DESC`
（`common/pgdb/tasks.py:720`、`common/database.py:2118`）——
带 `id` tiebreaker，所以是**全序**，两个后端行集合与顺序都一致。

> ⚠ **`batch_id` 不存在时回 200 空数组，不是 404**（两个后端实测一致：
> `GET /api/batches/99999/failures` → `{"batch_id":99999,"failed_tasks":[],"count":0}`）。
> 所以「拿到空数组」有两种含义：批次没有失败任务，或者批次根本不存在。
> 需要区分请先打 `GET /api/batches/{batch_name}/status`（它会 404）。

#### `error_type` 取值表

**唯一真源**：`common/core/error_types.py`。以下清单与该文件的 `DESCRIPTIONS`
字典逐字一致，改了那边这里也要跟着改。

| `error_type` | 含义 |
|---|---|
| `network` | 网络请求失败（连接错误、DNS 解析失败、连接被重置等） |
| `timeout` | 请求超时 |
| `blocked` | 被 Amazon 判定为异常流量并拦截（403/503，非验证码） |
| `captcha` | 遇到验证码页面 |
| `parse_error` | 页面拿到了，但解析不出预期字段（HTML 结构变化、空页面等） |
| `zip_switch_failed` | 切换配送邮编失败 |
| `variant_offset` | Amazon 把请求重定向到了兄弟 variant 页面（不是目标 ASIN 本身）。不自动重试（见 §2.x 重试策略：`LIMITED_RETRY_ERROR_TYPES` cap=1） |
| `zip_not_effective` | 邮编设置多次重发仍未生效（页面仍显示默认配送地区） |
| `session_not_ready` | worker 本地 session 迟迟未就绪（冷启动/轮换中超时） |
| `discover_failed` | 卖家店铺发现阶段失败（找不到任何在售 ASIN） |
| `server_reject` | server 端二次校验判定这条结果本身不合法，直接判失败——**不是** worker 上报的原始类型，是 server 改写的 |
| `unknown` | worker 上报的 `error_type` 不在此表中，server 入口已改写成 `unknown`；原始值会被保留在 `error_detail` 开头（形如 `[unrecognized_type:xxx] 原始 detail`），不丢信息 |

> `error_type` 只是短代码；`error_detail` 才是给人看的具体信息（HTTP 状态码、
> 异常消息等，截断到 500 字符）。两者总是成对出现，别指望单独一个字段
> 就能读懂失败原因。

#### 错误响应

| 状态码 | 触发 |
|---|---|
| 422 | `limit` 越界（`> 100000` 或 `< 1`），或 `batch_id` 不是整数 |

---

### 4.6 `POST /api/batches/{batch_id}/prioritize` —— 插队

**声明**：`server/app.py:1354`。无请求体，无查询参数。

**作用**：把该批次**仍处于 `pending`** 的任务的 `priority` 从 0 改成 10
（`common/pgdb/tasks.py:660-675`、`common/database.py:1369-1376`）。
已经被 worker 领走（`running`）或已终态的任务不受影响。

#### 响应

```json
{"ok": true}
```

> ⚠ **`ok:true` 不代表批次存在，也不代表改动了任何一行。**
> 实现是一条无条件 `UPDATE`，不查批次是否存在、不看 rowcount。
> 实测两个后端一致：`POST /api/batches/99999/prioritize` → `200 {"ok": true}`。
> 这个端点**没有任何可观测的失败模式**（除了参数类型错），
> 与 erpAPI 侧「best-effort 不抛」的用法是吻合的 —— 继续这么用即可，
> 但**不要**把 `ok:true` 当作「插队生效了」的证据。
> 要确认效果请看 `/status` 的 `stats` 变化速度。

| 状态码 | 触发 |
|---|---|
| 422 | `batch_id` 不是整数（如 `/api/batches/abc/prioritize`） |

---

### 4.7 `GET /static/screenshots/{batch_name}/{asin}.png` —— 截图

**挂载**：`server/app.py:253`，`StaticFiles(directory=config.STATIC_DIR)`，
磁盘布局 `server/static/screenshots/<batch_name>/<asin>.png`
（`common/config.py:43`；写入方 `server/api/worker_queue.py:277`）。

**路径来源**：不要自己拼。用 `GET /api/results` 返回的
`items[].screenshot_path`（已经是 `/static/screenshots/...` 相对路径），
或 `null` 表示没有截图。

| 状态码 | body |
|---|---|
| 200 | `image/png` 二进制 |
| 404 | `{"detail": "Not Found"}`（**JSON**，Starlette 的 `StaticFiles` 产生） |

拉不到就是 404，采集侧不会抛别的东西。erpAPI 侧「拉不到返 None 不抛」的处理
继续保持即可。

**整批打包**：`GET /api/export/{batch_name}/screenshots`
（`server/api/export.py:395`）返回 zip 流；批次目录不存在或没有 png 时 404
`{"detail": "无截图文件"}`。批次名非法（路径穿越）时 400。

---

### 4.8 `GET /api/export/incremental` —— catalog_sync 拉增量（**契约 v1**）

**声明**：`server/api/export_incremental.py:375`。
**这是对外契约，沃尔玛侧已按它实现，不许单方面改**（§3.1）。
完整规格见 `docs/incremental_export_contract.md`，这里只列 erpAPI 需要知道的。

#### 参数

| 参数 | 类型 | 默认 | 边界 |
|---|---|---|---|
| `cursor` | int | `0` | `ge=0`。**独占下界**（返回 `seq > cursor`）。从头拉传 `0`。超 bigint 上限 → 422 `invalid_parameter` |
| `limit` | int | `500`（`DEFAULT_LIMIT`） | `ge=1, le=1000`（`MAX_LIMIT`，`server/api/export_incremental.py:80`） |
| `X-Export-Token` | header | — | 可选。不配 `EXPORT_TOKEN` 就放行（每次 WARNING）；配了就强制校验 |

#### 成功响应 `200`

```json
{"contract_version": 1, "records": [...], "next_cursor": 0, "has_more": false}
```

`next_cursor` 只推进到**真正投递过的那一条**；**空页不推进游标**
（`server/api/export_incremental.py:441`）—— 这是唯一不丢数据的方向。

#### 错误响应（形状 (c)，见 §1.2）

| 状态码 | `error` | 含义 |
|---|---|---|
| 409 | `cursor_below_retention` | **你要的下一条已被保留期裁掉。消费侧必须告警 + 全量对账 + 停。** 带 `cursor` / `min_available_cursor` / `max_cursor` |
| 422 | `invalid_parameter` | `cursor` 超 bigint 上限 |
| 503 | `event_stream_unavailable` | 连接池未就绪 / 事件流表未建 / **当前后端是 SQLite** |

> ⚠ **这是全文唯一一处两个后端行为不同的地方**，且是有意的：
> 事件流是 PostgreSQL 专属。SQLite 上该端点**如实回 503**而不是 404
> （实测：`{"error":"event_stream_unavailable","detail":"事件流是 PostgreSQL 专属；当前后端不提供同步流。","backend":"sqlite"}`）。
> 理由写在 `server/app.py:326`：不挂载 = 404，而消费者会把 404 读成
> 「暂无数据」并静默停摆。

---

## 5. 与旧系统的差异 —— 哪些规避动作**现在可以拆掉了**

> 这一节逐条对应 erpAPI 给来的旧坑清单。**这些约束是旧数据库的限制，
> 新系统按我们自己的限制建，不继承。**

### 5.1 旧坑 b：批次名要毫秒精度 —— **不需要了**

**旧状况**：同秒批次会被服务端合并，所以批次名必须带毫秒去躲。

**现在**：撞名 → **409 Conflict**，绝不静默合并（`server/app.py:1199-1205`）。
批次名精度爱是多少是多少，秒精度完全够用；甚至可以用完全业务化的名字
（`erp-po-20260807-001`），因为**撞名是一个可识别的失败**，不是静默的数据污染。

**旧行为为什么必须改**（Phase 4.7 实测复现，同名批次第二次上传）：

- 两次 `create_batch` 返回**同一个 id**（`batches.name` 有 UNIQUE +
  `INSERT OR IGNORE` 后 `SELECT`）；
- 本次的新 ASIN 被**悄悄塞进上一个批次** —— 批次 ASIN 从 2 个变成 3 个，
  含一个从没在第一批里出现过的；「一次采集」的语义就此破掉，两侧都不报错。

**怎么用 409 做安全重试**：

```python
r = POST("/api/upload", ...)
if r.status_code == 200:
    batch_id = r.json()["batch_id"]            # 确定是这次新建的
elif r.status_code == 409:
    d = r.json()["detail"]
    assert d["error"] == "batch_name_conflict"
    batch_id  = d["batch_id"]                  # 既有批次的 id
    status_url = d["status_url"]               # 直接接着轮询即可
    # 语义：这个名字已经有批次了。若这是一次重试，说明上一次其实成功了。
    # 若这是一次全新提交且你不希望复用，请换一个 batch_name 重发。
else:
    raise
```

⚠ **409 的语义是「这个名字已经有批次了」，不是「你上次成功了」。**
两者只有在「同名 = 同一次业务提交」时才等价。
建议 erpAPI 把 `batch_name` 做成**业务幂等键**（比如订单号 + 日期），
这样 409 就精确地等于「这次提交已受理」。

### 5.2 旧坑 a：POST 永不自动重试 —— **现在可以安全重试**

**旧状况**：`POST /api/upload` 非幂等，重试 = 重复建批，所以禁止自动重试。

**现在**：可以重试。重试语义如下：

| 上一次实际发生了什么 | 重发得到 | 后果 |
|---|---|---|
| 请求没到服务端 | `200` | 正常新建，符合预期 |
| 服务端建成了，但响应在网络上丢了 | `409` + **那个** `batch_id` | 拿到既有批次 id，接着轮询即可。**不会建第二个批次，也不会并进去** |
| 服务端 400/413 拒绝了 | 同样的 4xx | 幂等地失败 |

实测（`tests/test_upload_batch_name_conflict.py::test_post_upload_is_safe_to_retry`）：
同一个请求连发三次 → 一个 200 两个 409，全程只有**一个**批次，
每次都拿回同一个 `batch_id`。

**重试的前提**：必须**显式传 `batch_name`**。
不传的话服务端每次生成 `batch_<东八区秒精度时间戳>`（`server/app.py:363`），
跨秒重试就会建出第二个批次 —— 那时 409 保护不了你。

**为什么是 409，不是「自动加后缀」或「200 + merged 标志」**
（`server/app.py:1191-1197` 的完整论证）：
自动加后缀会让重试造出第二个批次；200 + 标志则要求每个调用方都记得读那个标志，
漏读的代价与今天一模一样。**只有让撞名变成一个可识别的失败，
POST 才是可安全重试的。**

⚠ **重试不要无限快**。409 是廉价的（不建任务、不写库），但撞名那一发
**照样烧一个批次自增号**（§4.1）。建议指数退避，上限 3~5 次。

### 5.3 旧坑 c：`inserted=0` 语义不明 —— **歧义消失了**

**旧状况**：已存在 ASIN 返 0 会被误判提交失败；而实测更糟 ——
**部分重叠时 `inserted` 是 1 不是 0**，看起来像成功。

**现在**：

- **`200` 恒等于「新建了一个批次」**，`409` 恒等于「这个名字已经有批次了」。
  判定「建成了吗」**只看状态码**，不必再解读 `inserted`。
- 承上，`200` 时 `inserted` **恒等于 `total_asins`**（批次是全新的，
  `tasks` 表里不可能有它的行）。实测 4 组全部相等，见 §4.1。
  于是 `inserted` 成了一个冗余字段 —— 保留它只是为了不破坏在跑的调用方。
- 各字段的确切含义见 §4.1 的表。

**旧行为的第三个后果（最隐蔽的那个）也一并修了**：
旧版 `batches` 表仍存第一次的 `external_id` / `callback_url`，第二次的被静默丢弃
（`INSERT OR IGNORE` 整行不插，既有行一个字段都不更新），
而 HTTP 响应回显的是**请求**里的值 —— 调用方以为回调注册成功，**回调永远不触发**。
现在：撞名 409（根本不会走到回显），且 200 的回显读的是**存下来的**值
（`server/app.py:1216-1227`）。
守卫：`tests/test_upload_batch_name_conflict.py::test_200_echoes_what_was_actually_stored`
（用 200 字符的 `external_id` 验回显的是截断后的 120 字符）。

### 5.4 旧坑：`/errors` 只返回最近 200 条 —— **端点本身已删除**

`GET /api/batches/{batch_name}/errors` 曾经是失败明细的旧接口，有两条
设计如此、调参数也没用的限制：`failed_tasks` 硬截断到 200 条
（SQL 里写死 `LIMIT 200`）；排序是 `updated_at DESC, id DESC`，而
`updated_at` 是秒精度、`accept_results_batch` 又会把整次提交盖上同一个
时间戳——所以「最近 200 条」在同一批内部不是一个有业务含义的切片。

`GET /api/batches/{batch_id}/failures`（§4.4）本来就没有这两个问题：
`limit` 上限 **100000**（`server/app.py:1437`：
`Query(100000, ge=1, le=100000)`）、docstring 明写「不依赖批次名，且不
截断到 200 条」（`server/app.py:1439`）、支持 `error_type` 逗号分隔过滤、
排序带 `id` tiebreaker 是全序，两个后端一致。`/errors` 从一开始就是多余
的重复实现，所以**直接删除**，不再保留兼容层：

- 端点：从 `server/app.py` 移除。
- Web 控制台的"查看错误详情"改成调 `/failures` 并在前端本地把
  `failed_tasks` 按 `error_type` 聚合出摘要（`server/templates/tasks.html`
  的 `showErrors()`）——旧接口的 `error_summary` 聚合字段没有对应替身，
  这是唯一的响应形状差异，其余字段 `/failures` 全都有（还多一个 `status`）。
- **仍在调用 `/errors` 的外部方**（如果有）需要迁移到 `/failures`：
  路径参数从批次名换成 `batch_id`——`POST /api/upload` 的 200 和 409
  响应都给了 `batch_id`，`/status` 也给。

### 5.5 旧坑：单页上限 200 是硬约束调大无效 —— **重估后是 1000**

**新数字：`limit ≤ 1000`**，出处 `server/api/results.py:148` 的 `MAX_PAGE_LIMIT`。

上一版确实是 `le=200`，它就是从旧 erpAPI 抄来的、**一行注释都没有**。
2026-08-07 用 15 万行 `asin_data` / 15 万 `batch_asins` / 30 万 `asin_changes` /
15 万 `screenshots` 的 bench 库重测（列宽照真实采集填满，整行 JSON 化 5675 B/行），
逐档量了翻页 SQL、count SQL、响应体字节。结论三条：

1. **count 与 limit 无关**，是每请求的固定成本（无筛选 ~15 ms、
   `change_filter` 路径 ~70-105 ms）。它是**支持**调大 limit 的论据：
   拉 1 万行时 `limit=200` 要付 50 次 count，`limit=1000` 只付 10 次。
   实测端到端拉满 1 万行：`change_filter` 路径
   `limit=200` 6315 ms → 500 3774 ms → **1000 3129 ms** → 2000 2516 ms。
2. **翻页 SQL 根本不是瓶颈**：5000 行也只要 2.7 ms（无筛选）/ 18.5 ms（有筛选），
   比同一请求里那条 count 还便宜一个数量级。**任何档位都没有 SQL 悬崖。**
3. **真正的约束是响应体与序列化**，且是线性的：
   `limit=2000` 那档 82% 的时间花在 Python 序列化上（每行约 230-250 µs，每档一样）。

1000 这个数是按「响应体 / 内存」口径定的：
响应体 5.3 MB/页（2000 就是 10.6 MB，反代默认体积限制开始成为不可见的失败源）；
单请求 Python 峰值内存 37.5 MB，`PG_POOL_MAX` 默认 10 → 满并发约 375 MB
（2000 档就是 750 MB，那才是会把进程打死的线）；
且与 `/api/export/incremental`、`/api/v1/sync/records` 的 `MAX_LIMIT` 对齐，
调用方少记一个数。完整数据表在 `server/api/results.py:86-147`（`MAX_PAGE_LIMIT` 上方那段注释）。

> ⚠ **与旧系统的语义差别，必须知道**：
> 旧系统是「调大无效」（听起来像**静默截断**）；
> 我们这边是 `le=`，FastAPI 对超限**直接 422 拒绝，不截断**。
> 静默截断更危险 —— 消费者会把「只回了 N 条」读成「只有 N 条」——
> 但它要求调用方**自己分页**，而不是「传个大数看它给多少」。
> 守卫：`tests/test_results_cursor_liveness.py::test_page_size_ceiling_rejects_not_truncates`。
>
> ⚠ 这个上限**不是**实现细节：`le=` 会渲染成 `/openapi.json` 里的 `maximum`，
> 而那份 schema 是黄金基线逐字节钉死的一步。**改它就是改契约**（§3.2）。

### 5.6 旧坑：`next_cursor` 不前进导致翻页卡死 —— **结构上不成立**

游标谓词是**严格**不等号（`d.id < ?` / `d.id > ?`，`d.id` 是主键、非空无并列），
`next_cursor = items[-1].id`。所以每翻一页游标必然严格前进，循环必然终止。
出处见 §4.3。

**但在这一轮之前没有任何用例钉住它** —— 黄金基线也钉不住，
它只翻了一页（`results_page2` / `results_page_prev` 各一次），
而一个「游标不前进」的实现在只翻一页时表现完全正常。

**所以补了一条守卫用例：`tests/test_results_cursor_liveness.py`。**
它翻满整个结果集，断言：游标严格单调、硬轮次上限（超了就判定卡死并失败，
而不是挂在那里等超时）、逐页 id 两两不相交、并集恰好等于全集。
把不等号从 `<` 放宽成 `<=` 会让这条用例当场红。

**erpAPI 侧可以拆掉的规避代码**：任何「检测 `next_cursor` 是否等于上一次」
的死循环保护。留着无害，但它保护的缺陷在我们这边不存在。
（真要留一条兜底轮次上限也合理 —— 那是防网络层重放的，不是防这个 bug 的。）

---

## 6. 轮询建议

### 6.1 首选：**别轮询，注册 callback**

`POST /api/upload` 传 `callback_url`，批次完成（含截图）时采集侧会 POST 通知：

```json
{"event": "batch.completed", "batch_id": 1, "batch_name": "...",
 "external_id": "...", "status": "completed",
 "stats": {"total": 6, "done": 4, "failed": 2, "success_rate": 0.6667,
           "duration_seconds": 12},
 "screenshots": {"total": 0, "done": 0, "failed": 0},
 "completed_at": "2026-08-07 11:34:13",
 "data_url": "<base>/api/results?batch_id=1",
 "export_url": "<base>/api/export/<batch_name>"}
```

（`server/app.py:718-738`。`data_url` / `export_url` 在服务端未配置
`server_public_base` 时为 `null`。）

请求头：`X-Scraper-Event-Id`（**幂等键**）、`X-Scraper-Delivery-Attempt`（第几次投递）。
事件 id 的构造是 `evt_{batch_id}_{completed_at 去掉 - : 且空格换成 T}`
（`server/app.py:713-717`），例如 `evt_1_20260807T113413`；
`completed_at` 为空时退化成 `evt_{batch_id}`。

投递策略（`server/app.py:921-922`）：最多 **5 次**，
失败后依次等 **30s / 300s / 1800s / 7200s**。
所以回调**可能重复投递** —— 请按 `X-Scraper-Event-Id` 去重。
运维可手动重发：`POST /api/batches/{batch_name}/callback/retry`。

回调状态可以在 `/status` 的 `callback` 子对象里看到（`status` / `attempts` /
`last_error` / `next_retry_at`），所以「回调有没有送到」是可自查的。

### 6.2 兜底轮询：**建议 5 秒；旧系统的 20-30s 可以大幅调快**

**实测 `GET /api/batches/{name}/status` 的 HTTP 全栈墙钟**
（30 次采样，中位数 / p95，两个后端各跑一遍）：

| 批次任务数 | SQLite 中位数 | SQLite p95 | PG 中位数 | PG p95 | 响应体 |
|---|---|---|---|---|---|
| 100 | 2.87 ms | 4.27 ms | 6.48 ms | 7.41 ms | 350 B |
| 1 000 | 3.19 ms | 3.66 ms | 6.54 ms | 7.46 ms | 353 B |
| 5 000 | 5.13 ms | 7.00 ms | 6.92 ms | 8.08 ms | 353 B |
| 20 000 | 9.36 ms | 12.78 ms | 11.46 ms | 13.14 ms | 356 B |

**读数**：

- **成本几乎与批次大小无关**：20 000 个任务也只有 ~11 ms（PG），
  且 `tasks(batch_id)` / `screenshots(batch_id)` 都有索引
  （`common/pgdb/schema.py:272`、`:287`）。
- **响应体恒定 ~350 B**，与批次大小无关（它只返回聚合数，不返回明细）。
- 端点做 3 次查询：一次 `get_batch_by_name` + 两条按 `batch_id` 的聚合
  （`common/pgdb/batches.py:220-246`）。没有全表扫。

**建议值**：

| 场景 | 建议间隔 | 理由 |
|---|---|---|
| 已注册 callback，轮询只是兜底 | **30~60 s** | 回调是主路径，轮询只防回调丢失。这里保持旧系统的节奏甚至更慢都可以 |
| 纯轮询模式（没配 callback） | **5 s** | 单次 ~11 ms、350 B。10 个批次并发轮询 = 每 5 秒 ~110 ms 服务端时间，可以忽略 |
| 想更快 | **不建议 < 2 s** | `_completion_watcher` 每 **2.0 秒**才醒一次（`server/app.py:924`），比它快的轮询拿不到更新的信息，纯属空转 |

**为什么可以从 20-30 s 调到 5 s**：旧系统那个间隔是为一个未知成本的端点定的
保守值。这里的成本是量过的，且**与批次大小解耦**。把间隔从 30 s 缩到 5 s，
服务端成本涨 6 倍（每批次每分钟从 2 次涨到 12 次 × 11 ms = 0.13 秒），
换来的是完成检测延迟从最坏 30 s 降到最坏 5 s。

**退避**：批次刚提交的头几秒不可能完成，可以先 `sleep(预估采集时长 × 0.8)`
再开始轮询。采集速率参考 `README.md` 的实测峰值 60-83 ASIN/s。

### 6.3 判定完成的正确写法

```python
r = GET(status_url).json()
if r["status"] == "completed":      # ✅ 权威判据
    ...
# ❌ 不要用 r["stats"]["open"] == 0
```

理由见 §4.2 的 ⚠：`open == 0` 会领先 `status=="completed"` 约 0~2 秒；
对 `expand_variants=true` 的批次更是主动错的（watcher 还会往同一批次追加变体任务）。

`status` 的三个值：`running` / `completed` / `failed`。
**`failed` 不表示「全部失败」** —— 批次级的失败态；逐任务的成败看
`stats.done` / `stats.failed`，明细看 `/failures`。

---

## 7. 迁移清单（erpAPI 侧照这个改）

| # | 动作 | 参考 |
|---|---|---|
| 1 | `POST /api/upload` **必须显式传 `batch_name`**，建议做成业务幂等键 | §5.2 |
| 2 | 处理 **409**：读 `detail.batch_id` / `detail.status_url`，别当成通用错误 | §4.1、§5.1 |
| 3 | 打开 `POST /api/upload` 的自动重试（指数退避，3~5 次） | §5.2 |
| 4 | 批次名**去掉毫秒精度**的规避逻辑 | §5.1 |
| 5 | 判定提交成功**只看状态码**，删掉所有解读 `inserted` 的分支 | §5.3 |
| 6 | `/errors` → **`/api/batches/{batch_id}/failures`**（注意多一个 `status` 键） | §4.4、§5.4 |
| 7 | `/api/results` 的 `limit` 从 200 提到 **1000**；确认自己**分页**而不是传大数（超限是 422 不是截断） | §4.3、§5.5 |
| 8 | 可删掉 `next_cursor` 不前进的死循环保护（留着无害） | §5.6 |
| 9 | 翻页终止只认 `has_more`；`direction` 只发 `next` / `prev` | §4.3 |
| 10 | 注册 `callback_url`，按 `X-Scraper-Event-Id` 去重；轮询降为兜底 | §6.1 |
| 11 | 轮询间隔 30s → **5s**（纯轮询）或保持 30~60s（有 callback 兜底） | §6.2 |
| 12 | 完成判据改成 `status == "completed"`，不要用 `stats.open == 0` | §6.3 |
| 13 | 结果行按「忽略未知字段」反序列化（`items[]` 会随 `asin_data` 加列而增长） | §3.2 |
| 14 | `prioritize` 的 `ok:true` 不要当作「插队生效」的证据 | §4.6 |

---

## 8. 守卫用例索引

本文每一条行为论断都有用例钉住。代码改了而这些用例没红 = 本文漏写，请开 issue。

| 论断 | 守卫 |
|---|---|
| 撞名 → 409，既有批次一个字节没动 | `tests/test_upload_batch_name_conflict.py::UploadBatchNameConflictTests` |
| 409 里的 `status_url` 真的能打 | `…::test_the_status_url_in_the_409_actually_works` |
| POST /api/upload 可安全重试（连发三次 = 一个批次） | `…::test_post_upload_is_safe_to_retry` |
| 200 恒等于「新建了一个批次」 | `…::test_two_hundred_now_means_a_new_batch_was_created` |
| 200 回显的是**存下来的**值 | `…::test_200_echoes_what_was_actually_stored` |
| 撞名照样烧一个自增号 | `…::CreateBatchCompatibilityTests::test_conflict_still_reuses_the_existing_id_and_still_burns_one` |
| `create_batch` 签名未变（自动调度器不受 409 波及） | `…::test_create_batch_signature_is_untouched` |
| 游标严格单调 + 翻页必然终止 | `tests/test_results_cursor_liveness.py` |
| 单页上限是**拒绝**（422）不是截断 | `…::test_page_size_ceiling_rejects_not_truncates` |
| 单页上限值 = 1000，且与 `/openapi.json` 的 `maximum` 一致 | `…::test_page_size_ceiling_is_1000` |
| `/failures` 的 `limit` 上限（100000）与 `error_type` 过滤不能被削弱 | `tests/test_batch_failures_endpoint.py` |
| 错误码封闭集不漂移（含 `batch_name_conflict`） | `tests/test_error_codes.py` |
| 增量导出契约 v1 逐句 | `tests/test_incremental_export.py` |
| 两个后端行为逐字节一致 | `python -m tests.golden.run verify` / `DB_BACKEND=postgres … verify` |

**门禁**（改了本文覆盖的任何行为都要全绿）：

```
.venv/bin/python -m tests.golden.run selfcheck / verify / DB_BACKEND=postgres verify
.venv/bin/python -m pytest tests/ --ignore=tests/pgdb -q
DB_BACKEND=postgres .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m unittest discover -s tests
DB_BACKEND=postgres .venv/bin/python -m unittest discover -s tests
```
