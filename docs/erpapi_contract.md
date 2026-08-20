# erpAPI 对接契约 —— 采集侧（amazon-scraper-v4）

> **读者**：erpAPI 的实现者。照这份文档改代码即可，不需要读采集侧源码。
>
> **权威来源**：本文每一条论断后面都跟着 `file:line` 或实测数字。
> 代码与本文不一致时**以代码为准**，并请开 issue —— 本文的每一条行为都有
> 守卫用例钉住（见 §8），代码改了而用例没红，说明是本文漏写。
>
> **版本**：2026-08-09。本轮**只新增**三个端点（`POST /api/batches` JSON 推送、
> `GET /api/screenshots` 列截图、`GET /api/screenshots/{batch_name}/{asin}` 取图），
> 既有端点一个字节都没改，现有接入无需改动。新增部分见 §4.9 / §4.10。
>
> 上一版（2026-08-07）改了 `POST /api/upload` 的撞名语义与
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
| 8 | `POST /api/batches` | 提交采集批次（**JSON**） | **本轮新增**，与 #2 等价（§4.9） |
| 9 | `GET /api/screenshots` | 列批次截图状态 + URL | **本轮新增**（§4.10） |
| 10 | `GET /api/screenshots/{batch_name}/{asin}` | 取单张截图，状态码可区分 | **本轮新增**（§4.10） |
| 11 | `GET /api/export/batch/{batch_name}/records` | 按批次拿**这一批真正采到的**数据 | **本轮新增**（§4.11） |

> 曾经的第 6 个端点 `GET /api/batches/{batch_name}/errors`（失败明细旧接口）
> 已经**删除**，不是废弃——`失败明细`统一走 `GET /api/batches/{batch_id}/failures`
> （§4.4）。历史细节见 §5.4。

十一个端点在本仓库**全部存在**，路由声明位置：

| 端点 | 声明处 |
|---|---|
| `POST /api/upload` | `server/app.py:1053` |
| `GET /api/batches/{batch_name}/status` | `server/app.py:1266` |
| `POST /api/batches/{batch_id}/prioritize` | `server/app.py:1354` |
| `GET /api/batches/{batch_id}/failures` | `server/app.py:1433` |
| `GET /api/results` | `server/api/results.py:151` |
| `GET /api/export/incremental` | `server/api/export_incremental.py:375` |
| `/static/**`（截图） | `server/app.py:253`（`StaticFiles` 挂载） |
| `POST /api/batches` | `server/api/batches.py:api_create_batch` |
| `GET /api/screenshots` | `server/api/screenshots.py:api_screenshots` |
| `GET /api/screenshots/{batch_name}/{asin}` | `server/api/screenshots.py:api_screenshot_file` |
| `GET /api/export/batch/{batch_name}/records` | `server/api/export_incremental.py:export_batch_records` |

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

所有机器读的 `error` 码登记在 `server/api/sync.py` 的 `ERROR_CODES`：

```
ack_ahead_of_stream, batch_name_conflict, batch_not_found,
conflicting_zip_for_asin, cursor_ahead_of_stream, cursor_below_retention,
event_stream_unavailable, export_token_not_configured, gen_mismatch,
internal_error, invalid_export_token, invalid_parameter, range_too_wide,
screenshot_failed, screenshot_pending
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
           或 GET /api/screenshots?batch_name=             # 列状态 + url（§4.10）
           或 GET /api/screenshots/{batch_name}/{asin}     # 取图，状态码可区分（§4.10）
           或 GET /api/export/{batch_name}/screenshots     # 整批打包 zip
```

`POST /api/upload` 也可以换成 `POST /api/batches`（JSON，§4.9）—— 两者等价，
上面这条流程的其余每一步都不变。

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

### 3.2 本文档覆盖的 erpAPI 端点（#2 ~ #11）

一经本文发布即为对外契约。具体地：

- **路径、HTTP 方法、成功响应的字段名与类型**：不许改。
- **参数的边界值**（`limit` 的 `le=` / `ge=`）：它们会渲染进 `/openapi.json`
  的 `maximum` / `minimum`，而 `/openapi.json` 是黄金基线**逐字节钉死**的一步
  （`tests/golden/samples/baseline.json` 的 `openapi_schema`）。
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
| `fields` | str | `null`（= 全部列） | 逗号分隔的列名。**非法列名 422 拒绝，不静默丢弃** |
| `with_total` | bool | `true` | `false` → 不算 `total`（响应里是 `null`） |

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

#### 两个减负开关：`fields` 与 `with_total`

**默认都关着，不传就是今天的行为。** 它们冲的是响应体，不是 SQL ——
本端点没有 `response_model`，走 FastAPI 的通用 `jsonable_encoder` →
`json.dumps`，**82% 的耗时在 Python 序列化上**（`server/api/results.py`
头部有完整实测）。

实测（100 万行、单页 50 行、`long_description` 等宽列有真实内容）：

| | 耗时 | 响应体 |
|---|---|---|
| 默认（56 列 + count） | 60.9 ms | 274.2 KB |
| `fields=`（15 列）+ count | 52.1 ms | 20.0 KB |
| `fields=` + `with_total=false` | **2.7 ms** | **20.0 KB** |

`fields=asin,title,current_price` —— 只返回这些列。

⚠ 服务端会**强制补上** `id` / `asin` / `screenshot_path` / `updated_at`，
即使你没点名（翻页游标与截图路径归一化要用）。所以**返回的键可能比你要的多**，
别按"键集恰好等于我要的"写解析。

⚠ 非法列名 → **422**，不静默丢弃。拼错的列名被悄悄丢掉的话，
你会把「这个字段没返回」读成「这个字段是空的」。与 `limit` 超限同一个纪律。

`with_total=false` —— `total` 返回 `null`。它是**全表 COUNT**，随行数线性增长，
而翻页途中值恒定不变：**首屏要一次就够**，之后一路 `false`。
翻页的终止条件始终是 `has_more`，**不是** `total`（§5.6 已经说过这一点）。

#### 带 `batch_id` 时**多四个字段**：这一行是不是本批采的

⚠ **先理解这个端点在做什么**，否则下面四个字段没法用对：

```sql
SELECT d.* FROM asin_data d
JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?
```

`asin_data` 是**每个 ASIN 一行的最新态**，`batch_id` 只回答「属不属于这批」，
**不参与取哪一行**。所以这批采失败的 ASIN —— 只要它以前采过 —— 照样命中
JOIN，返回的是**上一次的旧行**。带上 `batch_id` 时追加下面四个字段，
就是为了让你**看得出**这件事：

| 字段 | 含义 |
|---|---|
| `batch_task_status` | 这个 ASIN 在**本批次**里的任务状态：`done` / `failed` / `processing` / `pending`；批次里没有这个任务时是 `null` |
| `batch_task_updated_at` | 该任务最后一次变更的时间 |
| `batch_asin_data_updated_at` | **这一行数据**的时间（= `updated_at`）。与上一列一比就知道数据比任务旧多少 |
| `batch_has_asin_data` | 见下，本端点**恒为 1** |

判据（与 CSV/xlsx 导出的 `data_source` 列同一套逻辑）：

```python
if row["batch_task_status"] == "done":   # 本次采集更新
elif row["batch_task_status"]:           # 历史产品库数据，本次未更新 ← 就是陈旧行
```

**不带 `batch_id` 时这四个字段不出现**（不是 `null`，是没有这个键）。
没有批次就没有「本次任务」可言，给 `null` 只会让人以为「这批没跑过」。

⚠ **`batch_has_asin_data` 在本端点恒为 1**，这不是占位符：驱动表是
`asin_data`、走 INNER JOIN，能返回的行必然有 `asin_data`。给出它是为了让你
用**同一套代码**从 JSON 复算 CSV 的 `data_source`。

⚠⚠ **真正的差别在另一头**：CSV/xlsx 批次导出以 `batch_asins` 为驱动表
LEFT JOIN，所以这批里**一次都没采过**的 ASIN 会出现在 CSV 里
（`batch_has_asin_data = 0`）；而本端点**整行都不会返回** —— 连"缺了一个"
都看不出来。要知道这批有哪些 ASIN 一次都没采过，用
`GET /api/export/batch/{name}/records` 的 `coverage`（§4.11），
或比对 `/api/batches/{name}/status` 的任务数。

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

**本轮 `fast` 追加了 `shipping` / `shipping_raw`**（运费）。纯追加，按 §3.2
可以单方面做，`contract_version` 仍是 1；值本来就在 `raw.buybox_shipping` 里，
**存量事件也会拿到，不需要回填**。三种形态映射到三个互不相同的结果：

| 采集侧 | `fast.shipping` | `fast.shipping_raw` | 含义 |
|---|---|---|---|
| `"FREE"` | `0.0` | `"FREE"` | **确认免运费**，落地价 = `price + 0` |
| `"$5.99"` | `5.99` | `"$5.99"` | 确认运费 5.99 |
| `"N/A"` / 空 | `null` | `null` | **这次没采到**，落地价**算不出来** |

⚠ `null` ≠ `0`（`docs/incremental_export_contract.md` 不变量 3b，与
`stock_count` 同一条）。**别写 `shipping or 0`** —— 把没采到当 0 的话落地价
照样算得出来、看着也正常，只是**偏小**，没有任何一侧会报错。

> 顺带说明一处**已知的不一致**，以免被当成本端点的行为：UI 导出（Excel/CSV）
> 的虚拟列「总价」把 `N/A` 也当 0 加进总价（`server/api/export.py:_prepare_row`），
> 所以那一列上「没采到运费」和「免运费」是同一个结果。本端点**刻意不复制**
> 那个行为。两者哪个对齐哪个，是个待定项，不影响本契约。

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

### 4.9 `POST /api/batches` —— 提交采集批次（**JSON**，本轮新增）

与 §4.1 的 `POST /api/upload` **是同一件事**，只是不用把 ASIN 列表拼成
xlsx/csv 再 multipart 上传。两者在采集侧共用同一个函数
（`server/api/batches.py:_create_batch_with_tasks`），所以 §4.1 里关于
**撞名 409、回调注册、回显读回值**的每一条在这里逐字成立，包括 §5.1 / §5.2
说的「批次名不需要毫秒精度」「POST 可以安全重试」。

**请求**：`Content-Type: application/json`

```json
{
  "asins": ["B0XXXXXXX1", "B0XXXXXXX2"],
  "items": [{"asin": "B0XXXXXXX1", "zip_code": "10001"}],
  "zip_code": "90001",
  "needs_screenshot": false,
  "batch_name": null,
  "callback_url": null,
  "external_id": null,
  "expand_variants": false
}
```

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `asins` | 与 `items` 二选一 | — | ASIN 数组。两个都给 = 合并 |
| `items` | 与 `asins` 二选一 | — | `[{asin, zip_code}]`，逐 ASIN 指定邮编。元素也可以直接是 ASIN 字符串 |
| `zip_code` | 否 | 服务端默认邮编 | 整批邮编。不传或 `null` = 跟随默认 |
| `needs_screenshot` | 否 | `false` | **批次级**开关，没有逐 ASIN 粒度 |
| `batch_name` | 否 | 自动生成 | 撞名 → 409，形状同 §4.1 |
| `callback_url` | 否 | `null` | 同 §4.1，走同一套 SSRF 校验 |
| `external_id` | 否 | `null` | 原样回传，上限 120 字符 |
| `expand_variants` | 否 | `false` | 自动展开变体 |

**邮编优先级**：`items[].zip_code` > 顶层 `zip_code` > 服务端默认。

**200 响应**：键集合与 §4.1 完全一致（`batch_id` / `batch_name` /
`external_id` / `total_asins` / `inserted` / `per_asin_zip_count` /
`invalid_zip_rows` / `callback_url` / `status_url`）。

**错误**：

| 码 | `detail` | 触发 |
|---|---|---|
| 400 | `"请求体不是合法 JSON"` / `"请求体必须是 JSON 对象"` | 体坏了 / 顶层不是对象 |
| 400 | `"未找到有效 ASIN"` | 过滤后一个 ASIN 都不剩 |
| 400 | `"非法邮编: xxx"` | **整批**邮编非法（逐 ASIN 的非法邮编不致命，见下） |
| 400 | `"非法批次名"` | 批次名含 `/` `\\` `..` 等，会穿出截图目录 |
| 400 | 对象，`error = "conflicting_zip_for_asin"` | 同一次推送里给同一 ASIN 两个**不同**邮编，见下 |
| 409 | 对象，`error = "batch_name_conflict"` | 撞名，形状同 §4.1 |

**逐 ASIN 邮编非法不拦请求**：计入响应的 `invalid_zip_rows`，那些 ASIN 退回
用批次邮编。这与「整批邮编非法就 400」的不对称是有意的，也与 §4.1 处理
xlsx B 列的方式一致：一列几万行里有一格脏数据就整批失败，比退回默认更难用。

#### 4.9.1 同一个 ASIN 要采多个邮编 —— **一个邮编一个批次**

这是库结构决定的，不是接口偏好：`tasks` 上有 `UNIQUE(batch_id, asin)`，
**一个批次里一个 ASIN 只能有一个邮编**。所以同一次推送里给同一 ASIN 两个不同
邮编会被拒绝，而不是静默取第一个：

```json
400 {"detail": {"error": "conflicting_zip_for_asin",
                "asin": "B0XXXXXXX1",
                "zip_codes": ["10001", "90001"],
                "message": "...一个邮编推一个批次..."}}
```

拆成两个批次（批次名带上邮编）之后：

* **截图**天然分开 —— 落盘是 `<批次名>/<asin>.png`，**批次名就是隔离键**；
* **数据**必须走 `GET /api/export/incremental`（§4.8），按
  `scrape_params.zipcode` 分辨。

> ⚠ **不要用快照类端点取多邮编数据。** `asin_data.asin` 是 `UNIQUE`，全库每个
> ASIN 只有一行，后采的覆盖先采的；而 `GET /api/results?batch_id=` 的
> `batch_id` 只用来**挑 ASIN**，数据仍取那一行全局快照
> （`common/pgdb/results_read.py:156`，`JOIN batch_asins ba ON ba.asin = d.asin`）。
> 结果：同一 ASIN 的两个邮编批次，`GET /api/results` 与
> `GET /api/export/{batch_name}` 返回**完全相同的行**，且不报任何错。
> 逐邮编准确的只有 §4.8 这一条路。
> 四条性质由 `tests/test_multi_zip_same_asin.py` 端到端钉住。

---

### 4.10 `GET /api/screenshots*` —— 查截图状态与取图（本轮新增）

§4.7 的 `/static/screenshots/...` **保留不变**，本节是它的补充而非替代。
用本节这两条的理由：不必为了拿一个路径去 `GET /api/results` 拉整行商品数据，
以及取图失败时能分清「再等等」和「别等了」。

**`GET /api/screenshots`** —— 列一个批次的截图状态

查询参数：`batch_name` 或 `batch_id`（**给一个**，都不给 → 400）；可选
`asin`、`status`（`pending`/`processing`/`done`/`failed`）、`cursor`、
`limit`（默认 200，上限 1000）。

```json
{
  "batch_id": 12,
  "batch_name": "job_20260809_10001",
  "progress": {"pending": 3, "processing": 0, "done": 7, "failed": 0, "total": 10},
  "items": [
    {"asin": "B0XXXXXXX1", "status": "done", "retry_count": 0,
     "error_detail": null, "updated_at": "2026-08-09 10:20:31",
     "url": "http://host:8899/api/screenshots/job_20260809_10001/B0XXXXXXX1"}
  ],
  "next_cursor": "B0XXXXXXX1"
}
```

* `url` **仅在 `status == "done"` 时非 `null`** —— 别的状态那张图不存在，
  给 URL 只会让你去撞 404。
* `progress` 是**整批**计数，不受 `asin`/`status`/`cursor` 过滤影响。
* 分页按 ASIN 升序；`next_cursor` 为 `null` 表示到底了（本页没装满就必然到底，
  不会再给 cursor）。
* 批次不存在 → 404。非法 `status` → 400。

**`GET /api/screenshots/{batch_name}/{asin}`** —— 取那张 PNG

`.png` 后缀可带可不带。**四种结局是四个不同的状态码**，据此决定要不要重试：

| 码 | 含义 | body | 该怎么办 |
|---|---|---|---|
| 200 | 图在这儿 | `image/png` 二进制 | — |
| 404 | 没有这条截图记录 / 批次不存在 / 文件已被清理 | `{"detail": "..."}` | **别重试** |
| 409 | 有记录但还没截好 | `detail.error = "screenshot_pending"`，带 `detail.status`；响应头 `Retry-After: 10` | **稍后再来** |
| 410 | 截图失败，不会再有 | `detail.error = "screenshot_failed"`，带 `detail.error_detail` / `detail.retry_count` | **别重试** |

对比 §4.7：那条路上后三种全是同一个 404，分不出来。

由 `tests/test_screenshot_api.py` 钉住，其中
`test_all_four_outcomes_are_distinct` 专门守「这四个码互不相同」。

---

### 4.11 `GET /api/export/batch/{batch_name}/records` —— 按批次拿**这一批真正采到的**数据（本轮新增）

**先说它补的是什么洞。** §4.3 的 `GET /api/results?batch_id=` 底层是：

```sql
SELECT d.* FROM asin_data d
JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?
```

`asin_data` 是**每个 ASIN 一行的最新态**，`batch_id` 只回答「属不属于这批」，
**不参与取哪一行**。于是这批采失败的 ASIN —— 只要它以前采过 —— 照样命中
JOIN，返回的是**上一次的旧行**，而响应里**没有任何字段**能让你看出它的年龄
（`SELECT d.*` 之外只补了截图路径）。摄进你自己的库、盖上一个新鲜的接收时间，
陈旧数据就此看起来很新鲜，**两侧都不会报错**。

CSV/xlsx 批次导出**有**防护（`data_source` 列会写「历史产品库数据，本次未更新」，
见 §4.3 与 `server/api/export.py`），但那是文件出口，脚本消费不了。

本端点从**事件流**读，语义上没有这个洞：`scraper.scrape_events` 的每一行都是
**一次真实发生过的采集**，没采成就没有行。

**请求**

```
GET /api/export/batch/{batch_name}/records?cursor=0&limit=500
X-Export-Token: <token>          # 与 §4.8 同一个 token，规则一致
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `cursor` | `0` | **独占**下界，返回 cursor 大于它的记录 |
| `limit` | `500` | 上限 1000（超出 → 422） |

**响应**

```json
{
  "contract_version": 1,
  "batch": {"id": 12, "name": "job_20260819", "status": "completed",
            "created_at": "2026-08-19 10:00:00"},
  "coverage": {"asin_total": 500, "asin_with_event": 498},
  "records": [ /* 与 §4.8 逐字段相同的 record */ ],
  "next_cursor": 34567,
  "has_more": false,
  "retention_min_cursor": 676,
  "max_cursor": 34567
}
```

* `records[]` 与 §4.8 **完全同构** —— 两个端点共用同一个 `_to_record`，
  由 `tests/test_batch_records_export.py::test_records_are_byte_identical_to_the_global_stream`
  钉住。
* `coverage` 让你不必再发一次请求就能判断这批齐没齐：
  `asin_total` 是这批入过队的 ASIN 数（含没采成的），`asin_with_event` 是
  **整批**出现过事件的去重 ASIN 数（与分页无关）。
  ⚠ 两者相等**不等于**「这批都成功了」——`outcome='not_found'` 也算有事件。
  成功与否看每条记录的 `outcome`。

**状态码**

| 码 | 含义 | 该怎么办 |
|---|---|---|
| 200 | 正常（**包括批次存在但一条事件都没有** → `records: []`） | — |
| 401 | `X-Export-Token` 缺失或不匹配 | 修配置 |
| 404 | `batch_not_found`，**只有这一个含义：批次名不存在** | 别重试 |
| 422 | `limit` 越界 / `cursor` 超 bigint | 修参数 |
| 503 | `event_stream_unavailable`（事件流是 PostgreSQL 专属） | 稍后再来 |

404 在这一族端点里只有一个含义是**刻意**的：批次存在但还没采完若也回 404，
消费方会把它读成「暂无数据」并静默停摆（同 §4.8 的理由）。

**⚠ 两个游标不可互换**

本端点的 `next_cursor` 与 §4.8 的数值同源于事件流的 `seq`，但定义域不同：
本端点是「这个批次内部」，§4.8 是「全局」。把本端点的 `next_cursor` 喂进
`/api/export/incremental` **不会报错**，会**静默跳过**中间所有别的批次的事件。

**保留期**

事件流有保留期，老批次的事件会被裁掉。本端点**不**回 409（那是全量流的语义），
而是照实回 200 + 一个可能不完整的集合，并给出 `retention_min_cursor`。
`coverage` 对不上且批次早于保留窗口 ⇒ 被裁过。

**现成的消费脚本**：`tools/consume_batch.py`（只用标准库，拷走即用）

```bash
python3 tools/consume_batch.py batch job_20260819 \
    --server http://host:8899 --token "$EXPORT_TOKEN" --out ./out --screenshots
```

它会拉全量分页、按 `outcome`/`zip_verify` 过滤、按 `(asin, zipcode)` 去重取最新，
写出 `.jsonl` + `.csv`，并顺带把该批次已截好的图下到 `out/screenshots/<batch>/`。

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
| `fast.shipping` 的 FREE→0.0 / 没采到→null / 具体金额 **三者互不相同** | `…::test_free_and_unknown_do_not_collapse_into_the_same_value` |
| 没采到的运费不得当成 0（落地价会静默偏小） | `…::test_unknown_shipping_is_null_not_zero` |
| `POST /api/batches` 与 `/api/upload` 走**同一份实现**（撞名语义不会分叉） | `tests/test_json_submit_endpoint.py::ParityWithUploadTests` |
| JSON 推送的邮编三档（逐 ASIN / 整批 / 服务端默认） | `…::ZipCodeChoiceTests` |
| JSON 推送的截图开关，不传即不截 | `…::ScreenshotChoiceTests` |
| 取图的四种结局是**四个不同的状态码** | `tests/test_screenshot_api.py::…::test_all_four_outcomes_are_distinct` |
| 截图列表给的 `url` 真的能打，且只在 `done` 时非 null | `tests/test_screenshot_api.py::ScreenshotListTests` |
| 同 ASIN 多邮编：截图按批次隔离、增量导出保留两份、快照只有一行 | `tests/test_multi_zip_same_asin.py` |
| 一次推送里同 ASIN 给两个邮编 → 400（不静默丢一个） | `…::test_two_zips_for_one_asin_in_one_push_is_rejected` |
| `/api/results?batch_id=` **确实**会把旧行当本批结果返回（本端点的存在理由） | `tests/test_batch_records_export.py::…::test_failed_asin_does_not_come_back_as_a_stale_row` |
| 按批次取记录**不会**返回这批没采成的 ASIN | `…::test_failed_asin_does_not_come_back_as_a_stale_row` |
| 别的批次的事件一条都不混进来（含 `coverage`） | `…::test_records_are_scoped_to_the_batch` |
| 按批次与全量流对同一行给出**同一个** record | `…::test_records_are_byte_identical_to_the_global_stream` |
| 404 只表示批次名不存在；空批次是 200 + `[]` | `…::test_unknown_batch_is_404_but_empty_batch_is_200` |
| 终态失败**也算**有事件（`coverage` 的语义） | `…::test_terminal_failure_still_counts_as_covered` |
| 消费脚本只落 `outcome=ok` 且邮编可信的记录 | `…::ConsumerScriptTests` |
| `zip_verify=mismatch` 丢弃、`unverified` 保留 | `…::test_mismatch_zip_is_dropped_but_unverified_is_kept` |
| 标题被拆两段后要拼回去（2026-08 Title Differentiators） | `tests/test_title_differentiators.py::TitleDifferentiatorTests` |
| 分隔符 `" \| "` 与 Amazon 自己拼的串逐字节相同 | `…::test_separator_is_amazons_own_not_ours` |
| 没有副标题时不留下孤零零的分隔符 | `…::test_missing_differentiator_leaves_no_dangling_separator` |
| `title` 与 `subtitle` 共用一份提取、永不自相矛盾 | `…::SubtitleFieldTests::test_subtitle_and_title_never_contradict` |
| `slow.subtitle` 进契约，没有就是 null（键恒在） | `tests/test_incremental_export.py::…::test_subtitle_*` |
| `asin_data` 新列在老库上会被自动补上（`CREATE TABLE IF NOT EXISTS` 不补列） | `tests/pgdb/test_schema_migration.py::test_existing_table_gets_the_new_columns_back` |
| 补列后列序仍与 `EXPECTED_COLUMNS` 一致、老数据不动 | `…::test_migration_preserves_existing_rows` |
| 往后新增的列必须落在两份 DDL 的末尾（否则新建库/升级库列序分叉） | `tests/test_asin_data_field_table_guard.py::test_every_alter_added_column_sits_at_the_tail_of_both_ddls` |
| `/api/results?batch_id=` 带上本次任务状态，陈旧行能被识别 | `tests/test_results_batch_status.py::test_stale_row_is_now_distinguishable` |
| 不带 `batch_id` 时**不加**这四个字段 | `…::test_fields_absent_without_batch_id` |
| 本端点 INNER JOIN，从没采过的 ASIN 整行不出现（故 `batch_has_asin_data` 恒 1） | `…::test_never_scraped_asin_is_absent_entirely` |
| 每个可能让批次完成的写点都给完成检测入队（截图 done/fail、两个结果端点） | `tests/test_completion_check_enqueue.py` |
| 入队失败绝不影响采集主路径 | `…::test_enqueue_failure_never_breaks_the_write_path` |
| 非终态失败不发事件；终态失败发 `parse_failed`；stale 提交发 `stale` | `tests/test_batch_records_export.py::RetryTimelineTests` |
| 终态失败 → 重试 → 成功：流里两条，后一条 cursor 更大 | `…::test_failed_then_success_leaves_both_records` |
| `fields=` 窄投影：行集不变、强制列必在、翻页照常 | `tests/test_results_projection.py::ProjectionTests` |
| 非法列名 422 拒绝而不是静默丢弃（含注入形状） | `…::test_unknown_field_is_rejected_not_silently_dropped` / `…::test_injection_attempt_is_rejected` |
| `with_total=false` 时 `total` 为 null 且 `has_more` 照常 | `…::WithTotalTests` |
| **不传这两个参数时响应与改动前逐字段相同**（契约 §3.2 不许删字段） | `…::DefaultUnchangedTests::test_default_response_is_unchanged` |
| 两个后端行为逐字节一致 | `python -m tests.golden.run verify` / `DB_BACKEND=postgres … verify` |

**门禁**（改了本文覆盖的任何行为都要全绿）：

```
.venv/bin/python -m tests.golden.run selfcheck / verify / DB_BACKEND=postgres verify
.venv/bin/python -m pytest tests/ --ignore=tests/pgdb -q
DB_BACKEND=postgres .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m unittest discover -s tests
DB_BACKEND=postgres .venv/bin/python -m unittest discover -s tests
```
