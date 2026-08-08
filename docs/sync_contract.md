# catalog_sync 拉取契约 v1

> 采集侧（amazon-scraper-v4）→ 沃尔玛侧（catalog_sync）的**唯一**数据出口。
> 实现：`server/api/sync.py` + `common/pgdb/retention.py`。
> 规格来源：`.agent/pg_migration_plan.md` §4 / §5 / §Phase 6。
> 契约测试：`tests/pgdb/test_sync_api.py` 与 `tests/pgdb/test_retention.py`
> （每个用例对应本文的一句话）。
>
> **本文中标「硬性规则」的条目，违反即数据错误。** 它们不是建议，也不是
> 「按需实现」——每一条都对应一种在数据上看不出异常的静默错误。

- `contract_version`: **1**（每个响应都带；变了就是不兼容变更，会提前通知）
- 传输：HTTP/1.1，`Content-Type: application/json; charset=utf-8`
- 鉴权：**暂无**。服务端已留 router 级依赖口子，加的时候会提前通知并给过渡期。
- 建议节奏：**每 5 分钟一轮**，每页 `limit=1000`

---

## 0. 三十秒版

```
每 5 分钟：
  GET /api/v1/sync/status          → 检查 gen / max_seq / forced_prune_log
                                     并确认 OVERLAP <= max_safe_overlap
  GET /api/v1/sync/records?after_seq=<游标-OVERLAP>&limit=1000
      → 200：写 snapshots；outcome=='ok' 且 completeness_ok 才 upsert products
      → 409：告警 + 全量对账 + 停
  POST /api/v1/sync/ack {gen, ack_seq}
  has_more 为 true 就继续下一页（轮内翻页用 next_after_seq，不再减 OVERLAP）

forced_prune_log 非空时（罕见，意味着真的丢了数据）：
  记下区间 → 人工处理 → POST /api/v1/sync/ack-prune {gen, prune_ids}
```

排序权威**只有 `seq`**。分组键**只有 `(asin, marketplace, zip_requested)`。
「没有新记录」**永远不等于**商品下架。

---

## 0.1 本次发布的变更清单（Phase 4 + Phase 6）

> 一页看完你侧要做什么。每一行都指向本文里写详细的那一节。
> **`contract_version` 仍然是 1**：新增字段开始有真值，顶层字段语义不变。
> 需要你**动手**的是**第 4 行和第 11 行**——第 11 行是 `payload` 内部的一处
> 形状反转，旧文档教过的判别式失效了，按旧规则实现的消费方会静默改变行为。

| # | 变了什么 | 你侧要做的事 | 详见 |
|---|---|---|---|
| 1 | `zip_observed` / `zip_verify` 从恒定占位值变成**真实判定** | 建议：`zip_verify == 'mismatch'` 的记录不要写进该邮编分组的价格序列 | §6.1 |
| 2 | `completeness` 开始出现非零值，`completeness_ok` **第一次可能为 `true`** | 无需改动（§6.5 的合取门本来就该这么写） | §6.4 |
| 3 | `parse_engine` 开始有值（`selectolax` / `lxml`），`null` 仍合法 | 无需改动。可用它观察采集侧的灰度进度 | §6.3 |
| 4 | **↑ 上面第 2 条的前提**：如果你侧开过「把 `completeness_ok` 视为 `true`」的临时旁路 | **必须撤掉。** 不撤则软降级页重新变成 products 的合法输入，合取门当场失效 | §6.4 |
| 5 | `not_found` 的 `payload` **不再携带**商品慢变字段（此前是 30/40 个 `"N/A"`） | 无需改动（这类记录本来就不进 products）。但别再读 `payload.title` | §10.4 |
| 6 | `payload.crawl_time` 的**线格式**改成 RFC3339 UTC；灰度期两种格式并存 | 无需改动。**顶层 `collected_at` 一个字没变**——请一直读它 | §6.2 |
| 7 | 保留期不再只看天数：改成磁盘/容量/ack 三者取下界，且**整分区 DROP** | 无需改动 | §7「余量由谁留」 |
| 8 | 新增 `status.max_safe_overlap`；保留期保证下界比 `ack_seq` 低至少这么多 | **读它，别把 `OVERLAP=200` 硬编码。** 断言 `OVERLAP <= max_safe_overlap` | §7 硬性规则 9 |
| 9 | 新增第五个端点 `POST /api/v1/sync/ack-prune` + `status.forced_prune_log` | 实现「非空 ⇒ 硬停 + 记录区间 + 人工确认」这条支路 | §5.1 |
| 10 | `ack_seq: 0` 明确定义成合法空操作（采集侧从库层面拒绝存 0） | 无需改动 | §5 |
| 11 | **`rating`/`review_count`/`seller_id`/`seller_name` 从「早退路径上缺席」改成「恒存在，取不到即 `"N/A"`」** | **必须改。**`key in payload` 不再是判别式，改判值是否为 `"N/A"`；`"N/A"` 意思是「本次没取到」而非「与上次相同」 | §6.6 |

**没有变的东西**（免得你去找）：`seq` 的语义与排序权威性、`source_id` 的幂等
锚点形状、分组键、`gen` 的硬停规则、`outcome` 的封闭集、`hash_ver`（仍是 `1`）、
`review_hash` / `slow_hash` 的字段集与算法。

---

## 1. 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/sync/records` | 拉数据。拉取循环的主体 |
| GET | `/api/v1/sync/status` | 每轮开头的健康/一致性检查 |
| GET | `/api/v1/sync/counts` | 对账（抽样比对无漏采） |
| POST | `/api/v1/sync/ack` | 确认位点，解锁采集侧的保留期裁剪 |
| POST | `/api/v1/sync/ack-prune` | 逐条确认「强制裁剪」事件（§5.1）。**非常规调用** |

> **前缀 `/api/v1` 是承重的，不要自己改写路径。**
> 采集侧存在 `GET /api/results/{asin}` 与 `GET /api/export/{batch_name}` 两条
> catch-all 路由，它们对不认识的名字回 **404**。把同步端点挂到那两个前缀下
> （或者请求时写错前缀）会得到一个 404，而 404 很容易被读成「暂无数据」，
> 于是游标永不推进、同步静默停摆。**本契约的五个端点永不用 404 表达「没有数据」。**

---

## 2. `GET /api/v1/sync/records`

### 参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `after_seq` | int ≥ 0 | **必填** | **独占**下界。返回 `seq > after_seq` 的记录。从头拉传 `0` |
| `limit` | int 1..1000 | 200 | 建议用 1000 |
| `outcomes` | csv | 全部 | 例 `ok,not_found`。**拉取循环里不要用**，见 §2.5 |

### 200 响应

```jsonc
{
  "contract_version": 1,
  "gen": "a3f19c2b7e04",
  "instance_id": "prod-hk-1",
  "min_available_seq": 39120041,
  "max_seq": 41872330,
  "relay_lag_seconds": 0.8,
  "outbox_depth": 12,
  "relay_state": "running",
  "server_time_utc": "2026-08-04T09:12:33.418922Z",

  "after_seq": 41208000,
  "next_after_seq": 41208500,
  "has_more": true,
  "count": 500,
  "limit": 500,
  "outcomes": null,
  "retention_forced": false,

  "records": [{
    "seq": 41208001,
    "source_id": "a3f19c2b7e04:evt-41208001",
    "gen": "a3f19c2b7e04",
    "asin": "B0CXXXXXXX",
    "marketplace": "amazon.com",
    "zip_requested": "10001",
    "zip_observed": "10001",
    "zip_verify": "confirmed",
    "collected_at": "2026-08-04T09:11:02.114000Z",
    "recorded_at":  "2026-08-04T09:11:07.902311Z",
    "outcome": "ok",
    "completeness": 15,
    "completeness_ok": true,
    "error_type": null,
    "error_detail": null,
    "batch_id": 8123,
    "task_id": 5512907,
    "worker_id": "w-hk-02",
    "attempt": 0,
    "parse_engine": "selectolax",
    "review_hash": "v1:77ab12…",
    "slow_hash": "v1:1f0c9a…",
    "hash_ver": 1,
    "payload": { /* 一次采集的完整结果，见 §6 */ }
  }]
}
```

### 2.1 顶层字段

| 字段 | 含义 |
|---|---|
| `gen` | 采集实例的**代号**。变化 = 硬停（§5 规则 3） |
| `instance_id` | 运维配置的部署标识。未配置时是 `"unconfigured"` |
| `min_available_seq` | **现在还能拉到的最小 seq**。空流时 = `max_seq + 1` |
| `max_seq` | 流头。**单调不减**，保留期裁剪不会让它回退 |
| `relay_lag_seconds` | outbox 里最老一条已经等了多久。持续 > 60 秒说明 relay 有问题 |
| `outbox_depth` | 还没进流的条数。单调增长 = relay 停摆 |
| `relay_state` | `running` / `stopped` / `starting` / `refused` / `failed` |
| `next_after_seq` | **下一轮的 `after_seq`**。见 §2.3 |
| `has_more` | 按**当前过滤条件**判定，精确（不是「seq < max_seq」那种估计） |
| `count` | `records` 的长度。恒等于 `len(records)` |
| `retention_forced` | 曾经发生过应急裁剪。为 true 时去 `/status` 读 `forced_prune_log` |

时间戳一律 **RFC 3339 UTC，带 `Z` 结尾**，可能带小数秒。

### 2.2 排序与分页

- 页内**严格按 `seq` 升序**，且所有 `seq > after_seq`。
- 页与页之间无重叠、无空洞（在没有 409 的前提下）。
- **`seq` 允许有空洞**，这是设计的一部分：底层序列非事务性，回滚的批次会烧号。
  连续性不是契约的一部分，**不要**用「seq 不连续」判断丢数据；
  要判断用 `/counts`（§4）。

### 2.3 游标推进规则（硬性规则）

```
X = r.next_after_seq
```

**不要**自己算 `max(rec.seq)`，也**不要**用 `after_seq + count`。
服务端保证：

- `records` 非空时，`next_after_seq == records[-1].seq`；
- `records` 为空时，`next_after_seq == after_seq`（**游标不推进**）。

游标只推进到**真正投递过的那一条**。这是唯一不会丢数据的方向。

### 2.4 空结果（硬性规则）

**本端点永不对空结果返回 404。** 空就是：

```
200  {"records": [], "has_more": false, "count": 0, "next_after_seq": <原样>}
```

这与采集侧其它导出端点（`/api/export/{batch_name}` 找不到批次回 404）**故意不同**。
若你在这四个端点上收到 404，那说明**路径写错了**或者请求被别的路由接走了，
**绝不是**「没有数据」——按 5xx 处理并告警。

### 2.5 `outcomes` 过滤（读前先读这一段）

`outcomes` 是**给对账和排查用的**，不要放进拉取循环。原因：

过滤是在 SQL 里做的，所以「空页不推进游标」这条规则在过滤下会让循环原地打转
（游标停在 `after_seq`，下一轮从同一个位置重扫）。
反过来，如果服务端替你把游标推到流头，你就会**跳过**那些被过滤掉、
但以后你可能需要的行。两害相权，服务端选了「不推进」。

**拉取循环一律不带 `outcomes`，全量收下，在你自己这一侧分流。**

### 2.6 状态码

| 码 | `error` | 条件 | 你要做什么 |
|---|---|---|---|
| 200 | — | 正常（含空结果） | 处理并推进游标 |
| **409** | `cursor_below_retention` | `after_seq + 1 < min_available_seq` | **告警 + 全量对账 + 停**。你要的下一条已经被裁掉了 |
| **409** | `cursor_ahead_of_stream` | `after_seq > max_seq` | **告警 + 全量对账 + 停**。采集侧疑似从备份恢复/回滚 |
| 422 | `invalid_parameter` / `range_too_wide` | 参数不合法 | 修请求，不要重试 |
| 503 | `event_stream_unavailable` | 采集侧跑在 SQLite 后端 / 库未就绪 / 事件流表未建 | 退避重试并告警 |
| 5xx | `internal_error` | 服务端故障（未捕获异常） | 退避重试并告警 |

> `429` 在计划里为背压预留，**当前不会发出**。你侧仍然应该实现它
> （收到就按 `Retry-After` 退避），这样将来加上时不需要改消费端。

5xx 的响应体与契约层错误同形状：`{"error": "internal_error", "detail": "服务器内部错误",
"request_id": "…"}`。`detail` 是固定文案、**不含任何异常细节**；`request_id` 是这一次请求的
关联键，服务端日志里有同一个值，报障时带上它。

422 有两种响应体：契约层面的错误带 `{"error": ..., "detail": "…"}`；
框架层面的类型错误（例如 `after_seq=abc`）只带 FastAPI 标准的
`{"detail": [ … ]}`（`detail` 是数组）。两者都不要重试。

两个 409 都是**服务端强制**的，没有任何参数能关掉。409 响应体形如：

```jsonc
{
  "error": "cursor_below_retention",
  "detail": "…人读的说明…",
  "after_seq": 41208000,
  "min_available_seq": 41300000,
  "max_seq": 41872330,
  "gen": "a3f19c2b7e04",
  "instance_id": "prod-hk-1",
  "server_time_utc": "…"
}
```

**409 永远不夹带半页数据** —— 响应体里没有 `records` 键。

> ⚠ **`cursor_below_retention` 有一类已知的假阳性，是有意保留的。**
> `seq` 允许有空洞。如果保留期边界正好落在一段被烧掉的号上，一个其实没掉窗的
> 游标也会拿到 409。代价是一次多余的全量对账；反方向（放宽判据）的代价是
> **静默丢数据**。
> **收到 409 就照章办事，不要试图自己判断它是不是假阳性。**
>
> 判据本身**不会**因保留期而放宽（那等于把守卫关掉）。改的是另一头：
> 保留期在裁之前会先算「裁完之后 `min_available_seq` 会变成多少」，
> 只有那个值仍然低于安全下界时才动手（§7「余量由谁留」）。
> 于是常规裁剪**不会**把你的游标推到窗口外，你也就不会因为它拿到 409。

### 2.7 一致性保证

`min_available_seq`、`max_seq` 与页查询在**同一个 `REPEATABLE READ` 只读事务**
里完成，页查询之后还会用同一快照复核一次下界并取较大者。
因此不存在「保留期在 MIN 与页查询之间跑完 → 守卫用旧下界放行 → 你拿到一段
有洞的 200」这条竞态。这条保证由 `test_bounds_and_page_share_one_repeatable_read_snapshot`
守着（把隔离级别降成 READ COMMITTED 该用例立刻转红）。

---

## 3. `GET /api/v1/sync/status`

无参数。每轮拉取开始前调一次。

```jsonc
{
  "contract_version": 1,
  "gen": "a3f19c2b7e04",
  "instance_id": "prod-hk-1",
  "min_available_seq": 39120041,
  "max_seq": 41872330,
  "ack_seq": 41808200,          // 从没 ack 过就是 null，不是 0
  "ack_at": "2026-08-04T09:07:11.000123Z",
  "lag_records": 64130,          // max_seq - ack_seq；ack_seq 为 null 时也是 null
  "relay_lag_seconds": 0.8,
  "relay_state": "running",
  "outbox_depth": 12,
  "dead_letters": 0,             // 被隔离的毒丸行数。> 0 需要人工看
  "events_per_minute": 71.4,
  "oldest_recorded_at": "2026-06-19T02:00:11.881000Z",
  "newest_recorded_at": "2026-08-04T09:12:30.104000Z",
  "retention_forced": false,
  "forced_prune_log": [],
  "db_size_bytes": 18273615872,
  "free_disk_bytes": 41203105792,
  "free_disk_path": "/opt/amazon-scraper-v4",
  "observed_daily_insert_rate": 103882,
  "partitions": [{"name": "scrape_events_p1", "lo": 20000000, "hi": 40000000}],
  "future_partitions": 2,

  "max_safe_overlap": 1000,      // ← 你的 OVERLAP 必须 ≤ 它。见 §7
  "retention": {                 // 保留期的现算观测，排障用
    "enabled": true,
    "age_days": 30,
    "age_horizon_utc": "2026-07-06T09:12:33Z",
    "age_floor_seq": 39120040,   // 比这更老的行才允许被裁
    "ack_seq": 41808200,         // 与顶层同源；从未 ack 是 null
    "ack_slack_seq": 1000,       // = max_safe_overlap
    "ack_floor_seq": 41807200,   // ack_seq - ack_slack_seq
    "hard_floor_seq": 0,         // 磁盘/容量应急下界，0 = 没有压力
    "hard_floor_reasons": [],    // 例 ["disk"] / ["partitions>6"] / ["rows>60000000"]
    "effective_floor_seq": 39120040,
    "min_seq": 39120041,
    "max_seq": 41872330,
    "next_seq": 41872331,
    "free_disk_bytes": 41203105792,
    "disk_floor_bytes": 8589934592,
    "disk_pressure": false,
    "max_retained_partitions": 6,
    "retained_partitions": 3,
    "max_event_rows": 60000000,
    "estimated_rows": 41000000,
    "droppable_now": [],
    "forced_prune_pending": 0,
    "partitions": [ /* 每个分区的 lo/hi/min_seq/max_seq/est_rows/size_bytes */ ],
    "last_pass": { /* 上一轮裁剪的摘要，进程重启后为 null */ }
  },
  "server_time_utc": "2026-08-04T09:12:33.418922Z"
}
```

要点：

- **`ack_seq` 初值是 `null`，不是 `0`。** 你侧的解析必须区分这两者。
- `observed_daily_insert_rate` 是**上界估计**（`max_seq` 减去最近 24 小时内最早
  一行的 `seq`），因为 seq 会被回滚烧号。用来看数量级，别拿来对账。
- `free_disk_bytes` 量的是 `free_disk_path` 那块盘。PG 与采集服务同机部署。
- `forced_prune_log` 是**持久列表**，不是瞬时布尔。采集侧被迫应急裁剪时往里追加
  一条，**一直返回**直到你逐条确认（`POST /ack-prune`，§5.1）。
  它存在采集侧的库里，采集侧进程重启、你侧宕机多久都不会丢 ——
  **消费者宕机正是触发它的前提**，所以一个只出现在某次响应里的布尔等于没有。
  看到非空 = 有数据被强制丢弃且**永远拿不回来了**，需要人处理，不是自动重试。
- `dead_letters > 0` = 有行畸形到进不了事件流，被隔离了。那些行**不会**出现在
  `/records` 里，需要采集侧人工处理。
- **`max_safe_overlap`：读它，不要把 200 硬编码进你侧。** 见 §7「余量由谁留」。
- `retention` 整块是**每次调用现算**的观测值，不是缓存。`min_available_seq`
  同理 —— 采集侧不允许把它写进任何元数据表（缓存值会落后于一次裁剪，
  于是掉窗的游标拿到 200 而不是 409，两侧都察觉不到）。

`min_available_seq` / `max_seq` 与 `/records` **逐字同源**，不会出现
「status 说还有、records 回 409」这种状态。

---

## 4. `GET /api/v1/sync/counts`

对账用。**闭区间** `[from_seq, to_seq]`。

| 参数 | 类型 | 说明 |
|---|---|---|
| `from_seq` | int ≥ 0 | 必填，含 |
| `to_seq` | int ≥ from_seq | 必填，含 |
| `bucket` | `hour` | 可选。带上就多返回按 UTC 整点分的桶 |

区间宽度 `to_seq - from_seq + 1` 上限 **8 000 000**，超出返回
`422 {"error": "range_too_wide"}`。

```jsonc
{
  "contract_version": 1,
  "gen": "a3f19c2b7e04",
  "instance_id": "prod-hk-1",
  "from_seq": 41000000, "to_seq": 41999999, "span": 1000000,
  "count": 812394,
  "min_seq": 41000003, "max_seq": 41999998,
  "min_recorded_at": "2026-07-28T11:04:00.100Z",
  "max_recorded_at": "2026-08-04T09:12:30.104Z",
  "by_outcome": {"ok": 790112, "not_found": 12033, "blocked": 8112, "parse_failed": 2137},
  "bucket": "hour",
  "buckets": [
    {"bucket_start": "2026-07-28T11:00:00Z", "count": 4021,
     "min_seq": 41000003, "max_seq": 41004024}
  ],
  "stream_min_available_seq": 39120041,
  "stream_max_seq": 41872330,
  "range_fully_retained": true,
  "server_time_utc": "…"
}
```

- `sum(by_outcome.values()) == count`，`sum(b.count for b in buckets) == count`。
- 桶边界是 **UTC 整点**，与服务端会话时区无关。
- 空区间是 `count: 0` + `min_seq: null`，**不是错误**。
- **先看 `range_fully_retained`。** 为 `false` 时 `count` 偏小是因为那段被裁剪了，
  不是漏采 —— 拿它去判「漏采」会得出错误结论。
  它**只看下界**（`max(from_seq, 1) >= stream_min_available_seq`）：保留期只从
  底部裁，所以「这段有没有被裁过」等价于「区间下界还在窗口里」。
  `to_seq` 超过 `stream_max_seq` 只是「还没采到那儿」，不是「没保留」——
  要判断这个自己比 `stream_max_seq`。
  （`seq` 由 bigserial 发号、永远 ≥ 1，所以 `from_seq=0` 与 `from_seq=1`
  是同一个区间。）

---

## 5. `POST /api/v1/sync/ack`

```jsonc
// 请求
{"gen": "a3f19c2b7e04", "ack_seq": 41808200}
```

`ack_seq` = 「这个位置及之前的数据，持久副本已经拿到了」。
采集侧不做本地备份，你侧的中心库就是持久副本 —— 所以 `ack_seq`
是保留期敢往下裁的**唯一**依据。**请老实 ack，也不要提前 ack。**

```jsonc
// 200
{
  "contract_version": 1,
  "gen": "a3f19c2b7e04",
  "instance_id": "prod-hk-1",
  "ack_seq": 41808200,        // 生效值（单调 max 之后的）
  "sent_ack_seq": 41808200,   // 你发来的
  "advanced": true,
  "ack_at": "2026-08-04T09:12:33.418922Z",
  "min_available_seq": 39120041,
  "max_seq": 41872330,
  "lag_records": 64130,
  "server_time_utc": "…"
}
```

规则：

- **单调取 max，永不后退。** 发一个比已存值小的 `ack_seq` 会返回 200，
  但 `ack_seq` 字段回的是**已存的那个更大值**，`advanced` 为 `false`。
- **并发安全。** 多个并发 ack 不会互相覆盖。
- `gen` 不符 → `409 {"error": "gen_mismatch"}`，**不写任何东西**。
- `ack_seq > max_seq` → `409 {"error": "ack_ahead_of_stream"}`，**不写任何东西**。
  确认一段本实例从未发出过的 seq，等于授权保留期裁掉你其实没拿到的数据。
- 形状不对（缺字段 / `ack_seq` 是字符串或布尔 / 负数 / `gen` 空串）→ `422`。
- **`ack_seq: 0` 是合法的空操作**：返回 200，`advanced: false`，
  但采集侧**不落库**，`ack_seq` 仍然回 `null`。
  0 的含义（「持久副本里一条都没有」）与「从未 ack」完全一致，
  而采集侧的保留期下界是 `min(时间下界, ack_seq)` ——
  存进一个 0 会让这个式子恒等于 0，保留期从此一行不裁直到磁盘写满。
  所以采集侧从库层面就拒绝存 0。你**不需要**为此改什么，
  但要知道：**冷启动时反复 ack 0 不会给你任何保护，也不会造成任何伤害。**

---

## 5.1 `POST /api/v1/sync/ack-prune`

**只在 `/status.forced_prune_log` 非空时才用得上，不要放进拉取循环。**

```jsonc
// 请求：确认指定的几条
{"gen": "a3f19c2b7e04", "prune_ids": [7, 8]}
// 或者：把当前所有未确认的一次清掉（运维手动执行）
{"gen": "a3f19c2b7e04", "all": true}
```

```jsonc
// 200
{
  "contract_version": 1,
  "gen": "a3f19c2b7e04",
  "instance_id": "prod-hk-1",
  "acknowledged": [7, 8],       // 本次真正被确认的 id
  "unknown_ids": [],            // 你发来但闩锁里没有的 id（重发不算错误）
  "forced_prune_log": [],       // 确认之后**还剩**哪些未确认
  "retention_forced": false,
  "server_time_utc": "…"
}
```

`forced_prune_log` 的每一条形如：

```jsonc
{
  "id": 7,
  "from_seq": 39120041, "to_seq": 41000000,   // 被销毁的 seq 区间（闭区间）
  "dropped_through_seq": 41000000,            // = to_seq，兼容字段
  "ack_seq_at_time": 40000000,                // 事发时你的 ack 位点；从未 ack 是 null
  "ts": "2026-08-04T09:00:00Z",
  "partition": "scrape_events_p1",
  "reason": "disk_floor",                     // disk_floor / disk_target / partition_cap / row_cap
  "est_rows": 20000000, "size_bytes": 41203105792,
  "soft_floor_seq": 40000000,                 // 本该停在这里，被应急闸门越过了
  "free_disk_bytes": 1073741824
}
```

规则：

- **这是一条数据丢失通知，不是一次重试信号。**
  `ack_seq_at_time < to_seq` 意味着 `(ack_seq_at_time, to_seq]` 这一段
  你**永远拿不到了** —— 采集侧不做本地备份（§8）。确认之前请先把区间记下来。
- 确认是**逐条**的。确认过的条目不再出现在 `/status.forced_prune_log` 里，
  但采集侧不会删除它（还留着 `acknowledged_at`，事后可查）。
- `gen` 不符 → `409 gen_mismatch`。gen 变了你本来就该硬停 + 全量对账，
  这时清掉旧实例的闩锁没有意义。
- 既没给非空 `prune_ids`、也没显式 `all: true` → `422`。
  一次什么都不确认的调用多半是消费端写错了，采集侧不替你猜。

---

## 6. 记录字段

### 6.1 采集参数

| 字段 | 说明 |
|---|---|
| `asin` | Amazon ASIN |
| `marketplace` | **封闭集**，当前值域是单元素集 `{"amazon.com"}`。域名形式，由采集侧 CHECK 约束强制。加站点时会提前通知 |
| `zip_requested` | worker 实际请求的邮编，5 位补零 |
| `zip_observed` | 从页面 glow 配送挂件抽出的邮编（**Phase 4 起有值**）。页面没挂那个挂件时是 `null` —— 这是常态，不是错误 |
| `zip_verify` | `confirmed` / `assumed` / `mismatch` / `unverified`。**Phase 4 起是真实判定**（此前恒为 `unverified`） |

> **`zip_verify` 的读法**（Phase 4 起）：
>
> | 值 | 含义 | 建议处置 |
> |---|---|---|
> | `confirmed` | 页面上显示的邮编 == 请求的邮编 | 正常入库 |
> | `assumed` | 商品页解析成功，但页面没挂 glow 挂件 —— 只能假设切邮编生效了 | 正常入库（这是多数正常记录的取值） |
> | `mismatch` | 页面显示的是**别的**邮编 ⇒ 这条价格/配送**不属于** `zip_requested` | **不要**写进该分组的价格序列 |
> | `unverified` | 根本没测（空页 / 拦截 / 404） | 按 `outcome` 处理 |
>
> 采集侧保证 `confirmed` 是自洽的：relay 会把「`confirmed` 但
> `zip_observed != zip_requested`」的记录**降级**成 `mismatch`。
> 反方向**不会**发生（服务端只削弱、不加强这个判定）。

> **硬性规则：分组键是 `(asin, marketplace, zip_requested)`。**
> 只按 `asin` 分组会退化成「最近哪个邮编采的」，价格序列会在邮编之间振荡，
> 而且从数据上完全看不出异常。

### 6.2 时间

| 字段 | 权威性 |
|---|---|
| `collected_at` | **worker 时钟，仅供参考。** 不同 worker 之间可能不同步 |
| `recorded_at` | 服务端时钟，展示用 |
| `seq` | **唯一的排序权威** |

> **`collected_at` 一直是、且始终是 RFC3339 UTC（带 `Z`）**，两个字段都由
> 服务端归一化输出，你侧无需关心 worker 内部用什么格式。
>
> 补充说明（只影响读 `payload.crawl_time` 原始值的人）：Phase 4 起 worker 写的
> `crawl_time` 从裸 `'2026-08-05 10:00:00'`（无标记的 UTC+8）改成
> `'2026-08-05T02:00:00Z'`（RFC3339 UTC）。**顶层 `collected_at` 的语义与格式
> 一个字没变**——采集侧的 relay 同时认得两种线格式，并把它们折算到同一个 UTC
> 时刻（worker 与服务端独立部署，灰度期两种格式必然同时在线）。
> 如果你侧有代码直接读 `payload.crawl_time`，请改读 `collected_at`。
>
> **灰度窗口（这一段是给运维看的，你侧无需动作）**
>
> worker 是分批发版的，所以存在一段时间，同一段 `seq` 区间里两种线格式并存：
>
> | `payload.crawl_time` 形态 | 来自 | relay 的折算 |
> |---|---|---|
> | `'2026-08-05T02:00:00Z'`（带 `Z` 或带偏移） | 新 worker | 照收 |
> | `'2026-08-05 10:00:00'`（裸 + **空格**分隔） | 老 worker | 当作 UTC+8，减 8 小时 |
> | `'2026-08-05T10:00:00'`（裸 + `T` 分隔） | 无 | 当作 UTC |
> | 缺失 / 解析不了 | 异常 | 退回 `recorded_at`，并计数 |
>
> 两种主形态实测落在**同一微秒**上，所以 `collected_at` 在整个窗口里都是可比的。
> 窗口是否结束可以直接观察：`/api/_debug/event-stream` 的计数器
> `collected_at_legacy_cst` 停止增长 ⇒ 老 worker 已全部下线。
> `collected_at_fallback` **应当恒为 0**；它一旦增长就说明有 worker 交上来的
> `crawl_time` 两种格式都不是，那是采集侧的缺陷，不是你侧的。
>
> 选 `T`+`Z` 而不是 `' +00:00'` 是刻意的：一个还没改的消费者若对新格式做
> `s[:19]` 再 `strptime`，`' +00:00'` 会被**静默**解析成一个差 8 小时的时刻，
> 而 `T`+`Z` 会直接抛 `ValueError`，把它推进自己的兜底分支。**宁可响，不可静默偏移。**

> **硬性规则：「同组最新值」一律按 `seq` 排序，不得用 `recorded_at`，
> 更不得用 `collected_at`。** NTP 前跳/后跳会让时间戳与 seq 非单调。
> products 的单调守卫写成 `WHERE excluded.seq > products.last_seq`。

### 6.3 质量 / 溯源

| 字段 | 说明 |
|---|---|
| `outcome` | `ok` / `not_found` / `blocked` / `parse_failed` / `stale`。封闭集 |
| `completeness` | 位图，见 §6.4 |
| `completeness_ok` | 服务端算好的合取结果，见 §6.4 |
| `error_type` / `error_detail` | `outcome != 'ok'` 时的原因 |
| `batch_id` / `task_id` / `worker_id` / `attempt` | 溯源 |
| `parse_engine` | `selectolax` / `lxml`。**Phase 4 起有值**；仍可能是 `null`（老 worker、或根本没解析 HTML 的 404 分支），`null` 是合法值 |

> **硬性规则：`outcome != 'ok'` 的记录只入 `snapshots`，
> 不触发 `products` upsert，其哈希不参与复审判定。**
> 理由：`not_found` 的 payload 里有 30/40 个占位符，对它算哈希得到的是
> 「好页 → 404 → 好页」每次都翻转的值 —— 正是复审门要防的误复审模式。
> （采集侧已在两层各拦一次：`outcome != 'ok'` 时 `review_hash` 与 `slow_hash`
> **都**写 `null`。你侧这条规则是第三道。）

`stale` 的含义：那次采集是**完整真实**的，但提交时租约已经被回收了
（worker 只是慢，不是死了）。它进流是为了不丢失观测，但它**不代表当前状态**。

### 6.4 `completeness` 与 `completeness_ok`（**现在必读**）

位图定义：

| 位 | 值 | 含义 |
|---|---|---|
| 0 | 1 | 面包屑区块存在 |
| 1 | 2 | 详情表存在 |
| 2 | 4 | 主图集存在 |
| 3 | 8 | **MEASURED**：这次采集真的测量过上面三项 |

```
completeness_ok  ⟺  (completeness & 8) != 0  AND  (completeness & 7) == 7
```

服务端已经算好放在 `completeness_ok` 字段里，直接用，不要自己拼。

> ✅ **Phase 4 已落地：`completeness` 开始出现非零值，`completeness_ok` 可以为
> `true`。** 判据是 **HTML 区块是否存在**（面包屑 / 详情表 / 主图集），不是
> 「解析出来的值是否非空」—— 二者不等价：「区块在但里面是空的」和「区块被整块
> 删掉」解析值完全一样，而后者正是软降级页的特征，也正是 §6.5 那道合取门要拦的。
>
> ⚠ **`0` 的含义仍然是「未测量」，不是「三项都缺」。** 它出现在：老 worker 的
> 记录、`not_found`、被拦截/空页的记录。**不要**把「`completeness == 0` 当成
> ok」写死进代码。
>
> ⚠ **如果你侧在 Phase 4 之前开了那个「把 `completeness_ok` 视为 true」的
> 临时旁路，现在必须撤掉。** 不撤的话，软降级页会重新变成 products 的合法输入，
> §6.5 的合取门当场失效 —— 那道门的两个前提（本条 + 上一条 `completeness_ok`）
> 正是靠这个字段。撤掉之后 products 的入库量会下降一截，那是**正确的**：
> 少掉的那部分本来就是没测量过的记录。

### 6.5 哈希与复审门

| 字段 | 说明 |
|---|---|
| `review_hash` | 复审门用。字段集见 `.agent/pg_migration_plan.md` §4.1 |
| `slow_hash` | 身份层变化检测，**不当门** |
| `hash_ver` | 整数，当前恒为 `1`。哈希串本身带 `"v1:"` 前缀 |

> **硬性规则：复审门是合取式。**
>
> ```
> 需要复审 ⟺ 本条 outcome == 'ok'
>          AND 本条 completeness_ok
>          AND products 中上一条 completeness_ok
>          AND review_hash 与存储值不同
>          AND hash_ver 与存储值相同
> ```
>
> **为什么必须是合取**：类目只有面包屑一个数据源，而面包屑正是软降级页会剥掉的
> 区块。好页 → 降级页 → 好页 = 两次哈希翻转 = 两次误复审。归一化解决不了，
> 因为 NULL 仍然 ≠ 真值，两个方向都翻。
> **占位符进/出永不触发复审。**

`hash_ver` 升级时采集侧会走「过渡期双输出」：同时输出 v1 和 v2，
你侧继续用 v1 把门、后台回填 v2，回填完再切。不会有一次性全语料复审风暴。

> ⚠ **一次性事件：首次上线后的第一轮重采会让近乎全语料的 `slow_hash` 同时翻转。**
> `hash_ver` 不变，所以上面那条「双输出」的保护**不覆盖这次**。
>
> 成因有两个（都是既有解析缺陷的修复，不是新增行为）：
> **(a)** `manufacturer` 过去会被 "Manufacturer recommended age" 这类键名子串命中而写成
> 年龄段（谁在文档里靠前谁赢），现在改成精确匹配；
> **(b)** `long_description` 过去吸进了容器之外的文本。下面详述 (b)。
>
> 其中 (b) 是：采集侧修掉了一个既有的解析缺陷（`worker/parser.py`
> `_slx_parse_long_description` 用了 selectolax 的 `Node.traverse()`，而它不受
> 子树约束）。`long_description` 因此长期吸进了容器之外的文本——价格、库存、
> 评分、BSR、CDN 图片 URL 都在里面：
>
> ```
> 修复前 long_description: '…for any desk. [image: …71xrpjis8ll…jpg] $549.99 in stock 2431 ratings…'
> ```
>
> 也就是说这个字段过去**每次采集都在变**（价格一动它就动），`long_description`
> 又在 `slow_hash` 的字段集里，所以 `slow_hash` 在生产引擎下一直是纯噪声：
> 实测 30 天序列里翻转 29/29 次，而 `review_hash` 只翻 7 次（正确值）。
>
> 修复后两个解析引擎对同一页面逐字节一致，`slow_hash` 恢复成真正的慢变信号。
> 代价是**修复前后的 `slow_hash` 不可比**：部署后第一轮重采，凡是页面在描述
> 容器之后还有任何内容的商品（实测语料里只有「容器就是最后一个元素」那种不变），
> `slow_hash` 都会变一次。
>
> **对你侧的要求**：`slow_hash` 不是复审门（门是 `review_hash`，它不含
> `long_description`，因此**不受这次影响**），所以这次翻转不该触发复审风暴。
> 但如果你把 `slow_hash` 变化用作任何别的信号（例如「慢变属性变了」的告警），
> 请为首轮重采准备一次性的抑制，或者干脆在部署后重新基线化一次。
>
> `review_hash` 在修复前后**不变**（实测两个引擎下均为同一值），因为
> `long_description` 只属于 `slow_hash`。

### 6.6 `payload`

一次采集的完整结果，JSON 对象（**不是**被引号包起来的字符串）。
消费侧自行解析，采集侧不保证它的键集跨版本稳定 —— 稳定的是本文列出的**顶层字段**。

已知的形状约束：

- ⚠ **本条在本次发布中反转了，如果你侧按旧版实现过，必须改。**

  **旧行为（≤ 上一版）**：`rating` / `review_count` / `seller_id` / `seller_name`
  这 4 个字段在 lxml 回退路径与全部早退路径（验证码 / 空 HTML / 404 …）上
  **在 `payload` 里缺席**，旧文档因此教你用 `key in payload` 判断。

  **新行为（本版起）**：这 4 个字段**恒定存在**。取不到时是字符串 `"N/A"`，
  不是缺席、不是 `null`。

  ```
  验证码页    旧: key in payload -> False       新: key in payload -> True, 值 "N/A"
  空 HTML     旧: key in payload -> False       新: key in payload -> True, 值 "N/A"
  ```

  **为什么改**：旧的「缺席」在采集侧是个 bug，不是设计。服务端写库时按
  `if val is not None` 跳过缺席字段，于是那一行**保留上一次采集的旧值**，
  却带着一个全新的 `crawl_time` —— 结转的陈旧数据被打扮成了新鲜观测。
  让 4 个字段恒存在，是为了让「这次没取到」变成一个可以说出口的事实。

  **你侧要做的**：`key in payload` 现在恒真，不再是判别式。改判
  `payload.get(k)` 是否为 `"N/A"`：
  **`"N/A"` 的含义是「本次采集没取到」，不是「和上次一样」。**
  按旧规则实现的消费方会从「未测量，保留我存的值」静默翻转成
  「已测量，写入 N/A」——这正是本条标⚠ 的原因。

  注意这 4 个字段**不在** `review_hash` 与 `slow_hash` 的字段集里，
  所以本条不影响复审门，也不引起哈希翻转。
- **`screenshot_path` 是易失引用（硬性规则）。** 该键**可能缺席**（不是所有采集
  都出截图）。
  截图文件会被常规清理，采集侧**不做**独立持久化，且明确接受随机器丢失。
  所以这个路径**随时可能指向已删除的文件**。
  **消费侧不得依赖该路径可解引用，也不得据此判断截图是否曾经存在。**
  需要截图时走采集侧现有的导出接口现取，取不到就是没有了。

---

## 7. 消费侧拉取算法（契约的一部分，不是建议）

```python
OVERLAP = 200          # 重叠回拉的条数，宁可重复

st = GET /api/v1/sync/status

# --- 三道硬停检查，缺一不可 ---
if st.gen != stored_gen:
    ALARM("generation changed"); full_reconcile(); STOP
if st.max_seq < stored_max_seq_ever:
    ALARM("stream rewound"); full_reconcile(); STOP
if st.forced_prune_log:
    ALARM(st.forced_prune_log)        # 逐条处理，然后 POST /ack-prune（§5.1）

assert OVERLAP <= st.max_safe_overlap # ← 硬性规则 9。不满足就是配置错误，别跑
stored_max_seq_ever = max(stored_max_seq_ever, st.max_seq)

X = max(0, stored_cursor - OVERLAP)   # 重叠回拉，**每轮只做一次**
while True:
    r = GET /api/v1/sync/records?after_seq=X&limit=1000

    if r.status == 409:
        ALARM(r.error); full_reconcile(); STOP
    if r.status == 404:
        ALARM("路径写错了或被别的路由接走了"); STOP     # 绝不是"没有数据"
    if r.status >= 500:
        backoff(); continue

    for rec in r.records:
        INSERT INTO catalog.snapshots (...)
          VALUES (...) ON CONFLICT (source_id) DO NOTHING     # 幂等锚点

        if rec.outcome == 'ok' and rec.completeness_ok:
            UPSERT catalog.products ...
              WHERE excluded.seq > products.last_seq          # 按 seq，不按时间

    X = r.next_after_seq                  # 只用服务端给的值
    stored_cursor = X
    POST /api/v1/sync/ack {"gen": st.gen, "ack_seq": X}

    if not r.has_more:
        break
```

### 硬性规则汇总（违反即数据错误）

1. **「同组最新值」一律按 `seq` 排序**，不得用 `recorded_at`，
   更不得用 `collected_at`。时钟前跳/后跳会让时间戳与 seq 非单调。
2. **分组键 = `(asin, marketplace, zip_requested)`。** 只按 asin 分组会退化成
   「最近哪个邮编采的」，价格序列在邮编间振荡且无法察觉。
3. **`gen` 变化是硬停**，不是「正常、无需动作」。它意味着采集侧是一套全新的
   实例（重装 / 从备份恢复 / 克隆部署），历史 `seq` 与新 `seq` 不可比。
4. **绝不把「没有新记录」读成下架/撤回**，也不要发 tombstone。
   采集侧有多条无鉴权删除端点，其中 `DELETE /api/results` 用
   `asin LIKE ? OR title LIKE ? OR brand LIKE ?` 模糊选目标 ——
   一次手滑会被复制成中心库里的大规模墓碑。
   商品下架必须由**独立的、显式的**信号驱动，不由本流的沉默驱动。
5. **`outcome != 'ok'` 的记录只入 snapshots，不触发 products upsert，
   其哈希不参与复审判定。**
6. **复审门是 §6.5 的合取式。占位符进/出永不触发复审。**
7. **`source_id` 是幂等锚点。** 形如 `{gen}:{uuid}`，在采集侧写入时铸造，
   重放不变。`ON CONFLICT (source_id) DO NOTHING` 是你侧唯一需要的去重。
8. **`screenshot_path` 不可解引用**（§6.6）。
9. **`OVERLAP <= status.max_safe_overlap`，且 `limit > OVERLAP`。** 见下一节。

### 重叠回拉为什么是必须的

`ack` 与写库不在同一个事务里，你侧崩溃可能落在两者之间。
`OVERLAP` 让你重复读回若干条已处理的记录，靠 `source_id` 的
`ON CONFLICT DO NOTHING` 吸收。**重复是安全的，空洞不是。**

`OVERLAP` 取多少：≥ 一次崩溃可能丢失的最大条数。建议 200，代价可以忽略。
把 `OVERLAP` 设成 0 等于赌「ack 之后一定写成功了」。

**`limit` 必须严格大于 `OVERLAP`。** 否则每轮开头的 `X = cursor - OVERLAP`
会把游标退回到不超过一页之前，下一页又只能推进 `limit` 条 —— 游标原地打转，
同步永远前进不了。`limit=1000` / `OVERLAP=200` 满足这一条。
另外注意伪码里的位置：**重叠回拉每轮只做一次**，轮内翻页一律用
`next_after_seq`，不要每页都减一次 `OVERLAP`。

### 余量由谁留（这一条两侧都要看）

重叠回拉与采集侧的保留期天然冲突：你要读 `cursor - OVERLAP`，
而保留期的下界之一是 `ack_seq`（你自己给的位点）。如果采集侧真的裁到 `ack_seq`，
那么你**每一轮**的重叠回拉都会落在保留窗口之外，拿到一个
`409 cursor_below_retention` —— 一次完全守规矩的拉取被判成掉窗，
然后按 §2.6 触发全量对账。每 5 分钟一次。

分工是这样定的：

| 谁 | 负责什么 |
|---|---|
| **采集侧** | 保留期的下界永远比 `ack_seq` 低至少 `max_safe_overlap` 条。**并且**在真正 DROP 之前先算「裁完之后 `min_available_seq` 会变成多少」，只有那个值仍然落在安全下界之内才动手 —— 这一步是必需的，因为 `seq` 有空洞，单看下界不等于单看窗口 |
| **你侧** | `OVERLAP <= status.max_safe_overlap`。**读那个字段，不要硬编码 200** —— 采集侧调大之后你才能跟着调大 |

当前 `max_safe_overlap` 出厂值是 **1000**（≥ 契约建议的 `OVERLAP=200`）。
采集侧永远不会把它配置成小于 200。

**唯一的例外是应急裁剪**：磁盘或容量闸门触发时，采集侧会越过 `ack_seq` 强裁，
这时你**会**拿到一个真的 `409`，并且 `/status.forced_prune_log` 里同时出现一条
记录（§5.1）。那不是假阳性，是真的丢了数据。

---

## 8. 不在本流里的东西（免得被读成数据丢失）

| 项 | 说明 |
|---|---|
| **卖家发现任务** | `accept_seller_discovery_result` 属于不同的域（`seller_discoveries` 表，无商品 payload），**不进本流**。需要的话是**第二条流**，不是混进这一条 |
| **截图文件** | 从不进中心库，也不可重建。见 §6.6 |
| `tasks` / `batches` / `asin_changes` | 采集侧内部状态，不进流。采集侧重装后这些归零，属于已接受的损失 |
| **被隔离的毒丸行** | 进 `scrape_outbox_dead`，不进流。`/status` 的 `dead_letters` 会显示条数 |

---

## 9. 故障排查速查

| 现象 | 多半是 |
|---|---|
| 四个端点全 404 | 路径写错，或者请求打到了别的服务。**不是没有数据** |
| 503 `event_stream_unavailable` | 采集侧跑在 SQLite 后端，或者还在启动。退避重试 |
| 409 `cursor_below_retention` | 你落后太多掉出保留窗口了（或者踩到了 §2.6 的已知假阳性）。全量对账 |
| **每一轮**都 409 `cursor_below_retention`，但游标其实在动 | `OVERLAP > status.max_safe_overlap`（§7 硬性规则 9）。调小 `OVERLAP`，或者让采集侧调大 `SYNC_ACK_SLACK_SEQ` |
| 409 + `/status.forced_prune_log` 非空 | **不是**假阳性：采集侧磁盘/容量告急，越过你的 ack 强裁了。那段数据永久丢失，按 §5.1 处理 |
| 游标原地打转，`records` 每轮返回同一批 | `limit <= OVERLAP`（§7 硬性规则 9），或者你在**每一页**都减了一次 `OVERLAP` |
| 409 `cursor_ahead_of_stream` | 采集侧从备份恢复/回滚了。全量对账 |
| 200 但 `records` 一直是空的，`max_seq` 不涨 | 看 `/status` 的 `relay_state` 与 `outbox_depth`：`outbox_depth` 单调增长 = relay 停摆，找采集侧 |
| `products` 一行都没进 | 看 §6.4。Phase 4 **之前** `completeness_ok` 恒为 false（预期）；Phase 4 之后若仍全 false，看 `/records` 里 `parse_engine` 是不是也全 `null` —— 那说明采集侧还跑着老 worker |
| Phase 4 之后 `products` 入库量下降 | 多半是没撤掉那个「`completeness_ok` 视为 true」的临时旁路，现在真值生效了。见 §6.4 |
| `count` 比预期少 | 先看 `/counts` 的 `range_fully_retained`。为 false 说明是被裁剪，不是漏采 |
| 同一商品价格在两个值之间振荡 | 分组键漏了 `zip_requested`（硬性规则 2） |

---

## 10. 采集侧已知边界（诚实清单）

1. **整机快照回滚采集侧检测不到。** 只回滚数据库能被检出（会铸新 `gen`），
   但连同 meta 一起回滚的整机快照检不出来 —— 这是设计上的边界。
   **你侧的 `st.max_seq < stored_max_seq_ever` 单调性检查是唯一防线**，
   必须实现，且必须是告警而不是静默继续。
2. `cursor_below_retention` 的假阳性（§2.6）**依然存在**，判据没有放宽 ——
   放宽它就等于把守卫关掉。Phase 6 改的是另一头：常规裁剪现在会先算
   「裁完之后 `min_available_seq` 落在哪」，不会把守规矩的消费者推出窗口（§7）。
   所以你在实践中不该再因为常规裁剪拿到 409；真拿到了，八成是
   `OVERLAP` 配置超标或者发生了应急裁剪，两者都能在 `/status` 上分辨。
3. ~~Phase 4 之前：`zip_observed` 恒 `null`、`zip_verify` 恒 `unverified`、
   `completeness` 恒 `0`、`parse_engine` 可能为 `null`、`crawl_time` 时区待统一。~~
   **Phase 4 已落地**，四个字段都开始输出真值（见 §6.1 / §6.3 / §6.4）。
   遗留的诚实边界只剩一条：**worker 是分批发版的**，所以在灰度窗口里，
   同一段 `seq` 区间内会同时存在
   「有 `zip_observed` / `completeness != 0` / `parse_engine` 有值」的新记录与
   「`null` / `0` / `null`」的老记录。二者**不可**按字段有无来判优劣 ——
   老记录的 `0` 是「未测量」，按 §6.4 照常不进 products，无需你侧特判。
   灰度进度可以直接观察：`parse_engine` 非 `null` 的记录占比。
4. **`not_found` 的记录不再携带商品字段。** Phase 4 起，404 的 payload 里
   **没有** `title` / `brand` / 类目 / UPC / 图片这些慢变字段（此前是 30/40 个
   `"N/A"` 占位符）—— 它们是「上一次采集到的值仍然有效」，而不是「变成了空」。
   `outcome != 'ok'` 的记录本来就只入 snapshots（§6.3 硬性规则），所以对你侧
   零影响；但如果你有代码直接读 `payload.title`，注意它现在可能**不存在**
   （缺席 ≠ `null` ≠ 空串）。
5. 采集侧的写路径今天是串行的（单写连接 + 真锁），所以「乱序提交」在今天的 API
   上还不可能发生。事件流的 outbox + 单 relay 是**提前建好的保险**，
   Phase 1.5 放开写并发时它就地生效，届时对你侧零改动。
