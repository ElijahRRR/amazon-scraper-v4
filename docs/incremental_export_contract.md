# 增量导出契约 v1 —— 采集侧存档副本

> **权威原本在沃尔玛侧 `docs/scraper_migration_brief.md` 第五节。**
> 本文是采集侧按约定各存一份的副本 + 实现说明。
> **改动需两侧同步升 `contract_version`。**
>
> 实现：`server/api/export_incremental.py`
> 用例：`tests/test_incremental_export.py`（每个用例对应契约里的一句话）

- `contract_version`: **1**
- 端点：`GET /api/export/incremental?cursor=<int>&limit=<int, ≤1000, 默认 500>`
- 鉴权：请求头 `X-Export-Token`（**可选**，见下）
- 返回：`{"records": [...], "next_cursor": <int>, "has_more": <bool>}`，按 `cursor` 升序

---

## 对账结果（2026-08-06 拿到正式契约后逐条核过）

已按正式契约改掉的 6 处：`limit` 默认 200→**500**；`scrape_params` 的键
`zip`→**`zipcode`**；`scraped_at` 截到**秒**；`slow_hash` 由 `"v1:<64位>"`
→**16 位十六进制**；补齐 `slow` 的可选字段（bullet_points / description /
weight / dimensions / variant）与 `fast` 的可选字段（buybox_* / coupon / deal）；
新增可选的 `raw`。鉴权由 fail-closed 改成**契约语义的可选**（见下）。

**2026-08-09 追加**：`fast` 新增 `stock_count` 与 `delivery_days`（均 int 或
null）。这是**纯追加**，按 `docs/erpapi_contract.md` §3.2「往成功响应里加字段
可以单方面做」，因此 **`contract_version` 仍是 1**。

两个值本来就在事件体里（`raw.stock_count` / `raw.delivery_time` 一直看得见），
只是没提升到 `fast`，所以**存量事件也会拿到这两个字段，不需要回填、不需要重采**。
对外命名用 `delivery_days` 而不是源字段名 `delivery_time`：采集侧存的是天数，
而 "time" 读起来像时刻；`delivery_date`（"Tomorrow" / "August 12" 那种人读日期）
是另一个字段，仍只在 `raw` 里。

### 5 处待确认项：沃尔玛侧已确认（2026-08-06），全部按采集侧实现

| # | 结论 | 要点 |
|---|---|---|
| 1 | **`slow_hash` 当不透明值用** | 算法是采集侧那一套（NFKC + 空白折叠 + 哨兵值全等归一 + 列表排序 + 图片 URL 归约到 image ID + 排序键 JSON + SHA-256，取前 16 位十六进制），**与契约文字描述的「字段排序后 sha256」不是同一个算法**。⚠ **不要按收到的 `slow` 对象自己重算再比对——两边必然不等。** 它保证的是「同页面跨进程跨引擎稳定、慢变字段真变了才变」 |
| 2 | `fast.currency` 恒 `"USD"` | 采集侧**不采币种**，这是适配器补的常量。要真实币种需先在采集侧加抓取 |
| 3 | `fast.stock_state` 值域 = `in_stock` / `out_of_stock` / `unknown` | 三值封闭集 |
| 3b | `fast.stock_count` / `fast.delivery_days` 的 `null` 与 `0` **不是一回事** | `null` = 这次没采到；`0` = 采到了且确实是 0（`stock_count=0` 即缺货）。适配器绝不用 0 表示「没取到」，与 `price` 同一条原则。消费侧请分开处理，别用 `or 0` 兜底 |
| 4 | `slow.weight` / `slow.dimensions` = `{package, item}` 对象 | 采集侧两个值分别是包装与本体，不合并 |
| 5 | 游标掉出保留窗口 → **409 `cursor_below_retention`** | 消费侧须实现「告警 + 全量对账 + 停」 |

> ### ⚠ 两份副本目前**内容不一致**，沃尔玛侧的 §5 需要补三处
>
> 「双方各存一份」这条规矩防的就是两份内容漂移。上面第 3、5 两条（以及第 1 条的
> 算法说明）是本次确认新增的**行为约定**，只写在采集侧这一份里，沃尔玛侧
> `docs/scraper_migration_brief.md` §5 尚未包含。需要补：
>
> 1. **`409 cursor_below_retention`**——这是 v1 原文里没有的状态码。
>    照 v1 原文实现的消费者收到它会不知所措，而它恰恰出现在「你要的数据已经
>    被裁掉了」这个必须硬停的时刻。**这条最要紧，catalog_sync 上线前必须写进去。**
> 2. `stock_state` 的三值封闭集。
> 3. `slow_hash` 是**不透明值**、不可自行重算比对。
>
> **是否升版本号：建议仍是 `v1`。** 上述都是「填补原文未定义的空白」，
> 没有任何一条改变了原文已经写死的行为，消费侧按 v1 写的代码不会因此失效。
> 但两份文本必须先对齐——最终以你侧判断为准。

### 采集侧的扩展字段（契约未要求，收着无害，不收也不影响）

`outcome`（依你们此前「失败/降级采集要进流」的决定；**`!= "ok"` 只进 snapshots，
绝不 upsert products**）、`completeness_ok`、`review_hash`、`recorded_at`、
`scrape_params` 里的 `zip_observed` / `zip_verify` / `source_marketplace` /
`parse_engine`。

其中 `zip_observed` / `zip_verify` 值得一提：契约要的是「影响结果的**全部**采集参数」，
而「请求的邮编有没有真的生效」直接决定这条价格属不属于该邮编分组——
`zip_verify == "mismatch"` 的记录不建议写进该邮编的价格序列。

### 鉴权：我按契约改了，但留了开关

契约写的是**可选**（「建议加上，服务器是公网 IP」），所以：
配了 `EXPORT_TOKEN` → 强制校验（不匹配/缺失 401）；**没配 → 放行**，但每次打
WARNING 日志。

我最初实现的是 fail-closed（没配就 503）。**契约是权威，这里服从契约**——
否则你按契约部署、不配 token，会撞上一个没预期的 503。
想要 fail-closed 回来：设 `EXPORT_REQUIRE_TOKEN=1`。

### 两条语义提醒（不是待定项）

- **`marketplace` 是「上架目的地」，不是「采集来源站点」。** 契约里恒 `"US"`，
  与你们 `(marketplace, asin)` 复合主键对齐。采集侧内部另有一个同名概念指
  **从哪个亚马逊站点采的**（当前恒 `amazon.com`）。今天一一对应；等你们开
  Walmart CA，很可能**仍从 amazon.com 采、却上架到 CA**，那时两者分叉。
  所以我没改内部值名，而是显式映射，并把来源站点原样放进
  `scrape_params.source_marketplace`——**两个概念从第一天就是两个字段。**
- **验收项「cursor 相同的多条记录不丢」在本实现下是平凡成立的。**
  我们的 `cursor` 是 `bigserial` 主键，**结构上不可能重复**。说这句是为了让你
  知道这条验收**没有真正测到什么**，别把它当证据。已写成用例
  （`test_cursor_values_are_unique_so_the_same_cursor_case_is_vacuous`），
  哪天有人把 cursor 换成时间戳之类可重复的东西，它会立刻红。

---

## 1. 请求

```
GET /api/export/incremental?cursor=0&limit=1000
X-Export-Token: <token>
```

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `cursor` | int ≥ 0 | 0 | **独占**下界，返回 `cursor` **大于**它的记录。从头拉传 `0` |
| `limit` | int 1..1000 | **500** | 超过 1000 返回 422 |

**鉴权**：`X-Export-Token` 与服务端 `EXPORT_TOKEN` 环境变量比对（`hmac.compare_digest`）。

- 服务端配了 `EXPORT_TOKEN`，请求头不匹配或缺失 → **401**
- 服务端**没配** `EXPORT_TOKEN` → **放行**（契约说鉴权可选），但服务端每次打 WARNING
- 想要「没配就关闭该端点」：服务端设 `EXPORT_REQUIRE_TOKEN=1` → **503**

## 2. 响应

```jsonc
{
  "contract_version": 1,
  "records": [ { … } ],
  "next_cursor": 41208500,
  "has_more": true
}
```

### record

下面是**真实响应**（跑一条采集抓下来的，不是手写示例）：

```jsonc
{
  "source_id": "93f6b81b1f58:1c8ee3da-e70a-49d8-b4e9-5002d9187221",  // 幂等键
  "cursor": 1,                                   // 单调不回跳
  "marketplace": "US",                           // 上架目的地，当前恒 US
  "asin": "B0INCR0000",
  "scraped_at": "2026-08-05T10:00:00Z",          // UTC ISO8601，精确到秒

  "scrape_params": {
    "zipcode": "10001",                          // 请求的邮编（分组键的一部分）
    "zip_observed": null,                        // 页面实际反映的邮编，可为 null
    "zip_verify": "unverified",                  // confirmed|assumed|mismatch|unverified
    "source_marketplace": "amazon.com",          // 采集来源站点，见上文语义提醒
    "parse_engine": null                         // selectolax|lxml|null
  },

  "slow": {
    "title": "Incr B0INCR0000",
    "brand": "IncrBrand",
    "category_path": ["Home", "Tools", "Wrenches"],
    "images": ["https://m.media-amazon.com/images/I/71ABC._AC_SL1500_.jpg"],
    "bullet_points": [],
    "description": null,
    "weight":     { "package": null, "item": null },
    "dimensions": { "package": null, "item": null },
    "variant": null                              // 或 {parent_asin, theme}
  },

  "fast": {
    "price": 19.99,
    "currency": "USD",
    "stock_state": "in_stock",                   // in_stock|out_of_stock|unknown
    "stock_count": 37,                           // int 或 null；**0 是合法值**
    "delivery_days": 8,                          // int 或 null；预计送达天数
    "buybox_price": null,
    "buybox_seller": null,
    "buybox_seller_id": null,
    "coupon": null,                              // 采集侧不采，恒 null
    "deal": null                                 // 采集侧不采，恒 null
  },

  "slow_hash": "3471dc8c36e2d028",               // 16 位十六进制，**当不透明值用**
  "raw": { /* 裁剪后的原始载荷，去掉内部字段与已在 slow/fast 给过的大文本 */ },

  // 以下是采集侧扩展，契约未要求
  "outcome": "ok",                               // ok|not_found|blocked|parse_failed|stale
  "completeness_ok": false,
  "review_hash": "40385603e042518d",
  "recorded_at": "2026-08-05T16:04:27Z"
}
```

**空值语义（重要）**：`null` 与 `[]` 一律表示「**本次采集没取到**」，
**不表示「该商品没有这个属性」**。软降级页会把面包屑、详情表整块剥掉，
那时 `category_path` 就是 `[]`。**别拿空值去覆盖你侧已有的值**——
用 `completeness_ok` 与 `outcome` 判断这条记录够不够格进 products。

## 3. 边界语义（验收会测的项）

| 项 | 行为 |
|---|---|
| **`cursor=0` 从头拉** | 返回流里现存的最小 cursor 起的记录 |
| **重复返回无害** | 同一个 `cursor` 拉两次，结果完全一致。消费侧靠 `source_id` 幂等去重 |
| **`cursor` 相同多条不丢** | 平凡成立：`cursor` 唯一。见上文语义提醒 |
| **空结果** | **200** + `records: []` + `has_more: false` + `next_cursor` **原样返回**（不推进）。**永不用 404 表达「没有数据」** |
| **游标推进** | 只用响应里的 `next_cursor`。不要自己算 `max(cursor)`，也不要用 `cursor + count`。空页不推进是**唯一不丢数据的方向** |
| **cursor 有空洞** | 正常。底层序列非事务性，回滚的批次会烧号。**不要用「cursor 不连续」判断丢数据** |

### 状态码

| 码 | `error` | 你要做什么 |
|---|---|---|
| 200 | — | 处理并推进游标（含空结果） |
| 401 | `invalid_export_token` | 修 token，不要重试 |
| **409** | `cursor_below_retention` | **告警 + 全量对账 + 停**。你要的下一条已被保留期裁掉（**待写进 v1.1**，见上文第 5 条） |
| 422 | `invalid_parameter` | 修请求，不要重试 |
| 503 | `event_stream_unavailable` / `export_token_not_configured` | 退避重试并告警。后者只在服务端设了 `EXPORT_REQUIRE_TOKEN=1` 却没配 token 时出现 |

## 4. 一条实现上的坑，写下来免得两侧都踩

`/api/export/incremental` 落在采集侧既有的 `GET /api/export/{batch_name}` 这条
**catch-all** 路由的前缀里。实测（新端点未挂载时）：

```
GET /api/export/incremental  ->  404 {"detail":"批次不存在: incremental"}
```

**404 正是消费方最容易读成「暂无数据」的码**——游标永不推进，同步静默停摆，两侧都不报错。

采集侧靠「注册顺序在 catch-all 之前」解决，并有回归守卫
（`tests/test_incremental_export.py::test_route_order_is_load_bearing`）钉死。

**对你侧的意义**：如果哪天你收到 `404` 且响应体里带「批次不存在」字样，
那不是没有数据，是**请求打歪了或采集侧路由退化了**——按 5xx 处理并告警，不要推进游标。

## 5. 与 `/api/v1/sync/*` 的关系

采集侧另有一组 `/api/v1/sync/{records,status,counts,ack,ack-prune}`，是**运维面**：
保留期水位、对账、位点确认。**你侧不实现它们也能正常消费**，本端点自足。

但有一条值得知道：采集侧的保留期下界是
`max(磁盘应急下界, min(时间下界, ack 下界))`。**你侧不调 `/ack` 时，`ack` 那一项不参与**，
保留期退化成「按时间 + 磁盘尽力而为」。这不会丢已经拉走的数据（中心库是持久副本），
但意味着「保留期绝不裁掉你还没拉的数据」这条**从可证降级为尽力而为 + 可检测**
（靠 409）。若将来想要那条强保证，接 `POST /api/v1/sync/ack` 即可，一个字段。
