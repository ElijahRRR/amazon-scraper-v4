# Amazon Scraper v4

高性能分布式 Amazon 商品数据采集系统。Server/Worker 分离架构，支持百万级 ASIN 采集、变动检测、定时任务、截图存证、webhook 回调通知、第三方卖家店铺采集；存储后端是 **PostgreSQL**，并提供面向下游 catalog_sync 的事件流与增量导出契约。

## 架构

```
Server (FastAPI)                          Worker (可部署多台)
  - Web 管理控制台（仪表盘/任务/结果/           - curl_cffi TLS 指纹模拟
    Worker 监控/设置，5 个页面）               - AIMD 自适应并发控制
  - 任务分发 & 结果收集                        - 每采集协程独立 Session（冷轮换，
  - 存储：PostgreSQL（asyncpg，                   无全局热备）
      pg_trgm GIN 索引）                       - 双解析引擎：selectolax 优先，
                                                lxml 为进程级回退
  - 定时任务调度                               - Playwright 截图（可选）
  - 全局并发配额协调                           - lease_epoch 防重复提交
  - Webhook 完成回调（SSRF 防御）              - variant_offset 检测
  - 卖家店铺采集（发现 ASIN → 详情采集）        - 多属性变体提取（twister）
                                              - 邮编校验 + 配送降级页重试

下游数据出口（PostgreSQL 后端提供）：
  - catalog_sync 事件流：写入 outbox → 单例 relay → 按 seq 分区的事件表
  - 三条对外契约：/api/export/*（UI 导出）、/api/export/incremental（下游增量导出）、
    /api/v1/sync/*（下游运维/对账：游标拉取、状态、计数、ack、保留期）
```

### 关于存储后端

**PostgreSQL 是正式后端**，`DB_BACKEND` 不配时就是它。迁移已完成。

仓库里还留着一条 SQLite 实现（`common/database.py`），**仅作为切换后的回滚兜底**，
不是新部署的选项。两套存储层由同一份公开方法签名驱动（`common/pgdb/` 对
`common/database.py` 做逐方法契约比对），等价性由字节级 HTTP 行为基线
（`tests/golden/`，同一份基线两个后端都要过）与 800 条 pytest 交叉验证。
等生产稳定后这条路径会整条删除，届时那批以 SQLite 为参照的等价性用例一并退役。

## 快速开始

### 1. 环境要求

- Python 3.10+（本地开发建议锁定 Python 3.12——3.13/3.14 上 `curl_cffi`/`lxml`/`selectolax`/`asyncpg` 常没有预编译 wheel，会掉进源码编译）
- PostgreSQL **14+**（硬性下限，`tools/preflight.py` 会拦；需要声明式分区与 `FOR UPDATE SKIP LOCKED`）。
  **实测通过：16.x 与 17.x** —— 17 上黄金基线 86 步逐字节一致、pytest 与 16 同为 867 passed。
  没有理由的话建新库直接上 17。
- **建库时必须带 `LC_COLLATE=C LC_CTYPE=C`**（见下）。PG 17 新增了 builtin locale provider，
  **不要**用 `--locale-provider=builtin`，保持默认的 libc（`datlocprovider=c`）
- TPS 代理（帐密认证，每次请求自动换 IP）
- 服务器最低 1C / 2GB / 20GB SSD，外加一个 PostgreSQL 实例（可同机）

### 2. Server 部署

```bash
git clone https://github.com/ElijahRRR/amazon-scraper-v4.git
cd amazon-scraper-v4
# 虚拟环境目录名用 .venv（带点）—— deploy/setup.sh 建的也是它，
# .gitignore 忽略的也是它。叫 venv 的话满目录文件会变成 untracked。
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 建库：LC_COLLATE=C 不是可选项 —— PG 的默认排序规则会让分页/搜索/导出
# 的顺序与预期不一致，而且是建库时定死的，事后只能重建库
createdb -T template0 --lc-collate=C --lc-ctype=C -E UTF8 scraper

# 配置
cp .env.example .env
# 至少填 PG_DSN 与 PROXY_URL；DB_BACKEND 留空即走 PostgreSQL

python3 run_server.py
```

首次启动前建议先跑一遍环境体检，它会实测 PG 连通性、建表、分区、排序规则、
磁盘与依赖，比直接起服务看日志报错快得多：

```bash
python tools/preflight.py
```

Server 默认监听 `0.0.0.0:8899`，浏览器访问 `http://<IP>:8899`。首次启动自动建 `public` 与 `scraper` 两个 schema 的表、`pg_trgm` GIN 索引、事件流分区，并完成幂等的结构迁移（重复启动无副作用）。

### 3. Worker 启动

Worker 可以在本机或任意远程机器上运行，通过 HTTP 连接 Server。

```bash
# 基础启动（含截图）
python3 run_worker.py --server http://<SERVER_IP>:8899

# 禁用截图 + 自定义 worker_id
python3 run_worker.py --server http://<SERVER_IP>:8899 --worker-id my-worker --no-screenshot

# 定时自动重启（避免长跑内存泄漏）
python3 run_worker.py --server http://<SERVER_IP>:8899 --auto-restart-hours 6
```

| 参数 | 说明 |
|---|---|
| `--server` | Server 地址（必填） |
| `--worker-id` | Worker 标识（默认自动生成） |
| `--concurrency` | 初始并发数（默认从 Server 同步） |
| `--zip-code` | 配送邮编（默认从 Server 同步） |
| `--no-screenshot` | 禁用截图功能（只拉取非截图任务） |
| `--api-key` | ERP Server Worker API Key（也可用环境变量 `WORKER_API_KEY`） |
| `--auto-restart-hours` | 定时自动重启小时数（0 或省略 = 关闭；也可用环境变量 `WORKER_AUTO_RESTART_HOURS`） |

### 4. systemd 常驻服务（Linux，Server 端）

仓库自带一键部署脚本，在目标机器上以有 sudo 权限的账号执行：

```bash
bash deploy/setup.sh
sudo systemctl start amazon-scraper
sudo systemctl status amazon-scraper
journalctl -u amazon-scraper -f
```

`deploy/setup.sh` 做的事：把项目 rsync 到 `/opt/amazon-scraper-v4`、建 `.venv`、`pip install -r requirements.txt`、创建 `data/`、`.env.example` 复制成 `.env`（已存在则跳过）、安装并 `enable`（不会自动 `start`）systemd 单元 `amazon-scraper.service`（对应 `deploy/server.service`）。

**目前的局限**（如实说明，按需自行调整）：
- 这个脚本只打包 Server（`rsync` 时显式排除了 `worker/`），Worker 需要自己在目标机器上单独 clone + 建 venv + 跑 `run_worker.py`，仓库里没有配套的 worker 部署脚本。
- 脚本自己不建库、不装 PostgreSQL：`PG_DSN` 指向的库要提前建好（记得 `LC_COLLATE=C`）。

### 5. 本地开发环境

在本机（含 macOS）搭开发环境的完整步骤见 [`docs/local_macos_setup.md`](docs/local_macos_setup.md)：装 PostgreSQL 17（16 亦可）、**建库时必须带 `LC_COLLATE=C LC_CTYPE=C`**（PG 的默认排序规则会导致分页/搜索/导出顺序与预期不一致，且建库时定死、事后只能重建库）、Python 3.12 虚拟环境、`pip install -r requirements-dev.txt`、设好 `PG_DSN` / `SCRAPER_INSTANCE_ID`、跑一遍 `tools/preflight.py` 体检，然后 golden 基线与 pytest 各跑一遍。

> 该文档开头有一步"先 `git checkout` 到某迁移分支"已经过时——相关代码目前都已经在 `main` 分支上，不需要切分支。

`requirements.txt` 是运行所需（含 `asyncpg`；`aiosqlite` 只服务 SQLite 回滚兜底那条路径）；`requirements-dev.txt` 在此基础上加了 `pytest`/`pytest-asyncio`/`httpx`（测试），以及显式重复声明的 `selectolax`/`lxml`/`dateparser`（这三个其实已经在 `requirements.txt` 里，重复声明是因为真的出过"venv 没装全导致解析器回归测试大批量静默 skip、变异测试全部放行"的事故，见该文件内注释）。

## 功能说明

### 任务上传

访问 **任务管理** 页面，上传包含 ASIN 的文件：

- 支持格式：`.xlsx` / `.csv` / `.txt`
- 自动提取 `B[0-9A-Z]{9}` 格式的 ASIN 并去重
- 可选：指定批次名、邮编、是否截图、**是否自动采集多属性变体**
- **🔗 自动采集多属性变体**（开关，默认关）：开启后，采完上传的 ASIN 会把它们的同款全部变体（不同颜色/尺寸等）也采进【同一批次】，详见「多属性变体提取 + 自动展开」
- **per-ASIN 邮编**：仅 `.xlsx` / `.csv` 的 B 列填 5 位邮编可为单个 ASIN 指定邮编（同一 batch 内不同邮编自动切换 session）；**`.txt` 上传不支持这一列**，会被忽略
- 上传大小上限 50MB
- **批次名撞名返回 `409 Conflict`**（不再静默合并）：响应体带 `detail.batch_id` / `detail.status_url`，可直接轮询该批次状态。旧行为是把新 ASIN 悄悄合并进同名批次、并悄悄丢弃这次请求带的 `external_id`/`callback_url`——如果你的调用方依赖旧的"重名即合并"语义，需要改成先处理 409、按需换个批次名重传
- **API 调用**：可直接 `POST /api/upload`（multipart），支持以下额外字段：
  - `external_id`：调用方自己的批次 ID，原样回传，便于追踪
  - `callback_url`：采集完成时 POST 到此地址通知（详见下方 Webhook）
  - `expand_variants`：`true`/`false`，是否自动展开变体（同上方开关）

### 推送采集任务（JSON API）

程序化调用方不必先拼一个 xlsx 再 multipart 上传，`POST /api/batches` 收 JSON：

```bash
curl -X POST http://<server>:8899/api/batches \
  -H 'Content-Type: application/json' -d '{
    "asins": ["B0XXXXXXX1", "B0XXXXXXX2"],
    "zip_code": "10001",
    "needs_screenshot": true,
    "batch_name": "job_20260809_10001"
  }'
```

字段（除 `asins`/`items` 外全部可选）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `asins` | — | ASIN 数组。与 `items` 二选一，也可同时给（合并） |
| `items` | — | `[{"asin": "...", "zip_code": "..."}]`，需要逐个 ASIN 指定邮编时用 |
| `zip_code` | 服务端默认邮编 | **整批**邮编。不传 / `null` = 跟随服务端默认 |
| `needs_screenshot` | `false` | **批次级**截图开关 |
| `batch_name` | 自动生成 | 撞名 → `409`（同 `/api/upload`） |
| `callback_url` | `null` | 完成回调，同 `/api/upload` |
| `external_id` | `null` | 原样回传 |
| `expand_variants` | `false` | 自动展开变体 |

响应体、撞名 `409` 语义、回调注册与 `POST /api/upload` **完全一致**（同一份实现）。
ASIN 去重保序；非法 ASIN 丢弃，一个都不剩 → `400`。整批邮编非法 → `400`；
逐 ASIN 邮编非法只计入响应的 `invalid_zip_rows` 并退回批次邮编。

**邮编优先级**：`items[].zip_code` > 顶层 `zip_code` > 服务端默认。

#### 同一个 ASIN 要采多个邮编

**一个邮编推一个批次**，批次名带上邮编。这不是风格建议，是库结构决定的：
`tasks` 上有 `UNIQUE(batch_id, asin)`，一个批次里一个 ASIN 只能有一个邮编。
所以在**同一次推送**里给同一个 ASIN 两个不同邮编会被拒绝：

```
400 {"error": "conflicting_zip_for_asin", "asin": "...", "zip_codes": ["10001", "90001"]}
```

拆成两个批次之后：

- **截图**天然分开 —— 落盘路径是 `<批次名>/<asin>.png`，**批次名就是隔离键**；
- **数据**要走 `GET /api/export/incremental`，按 `scrape_params.zipcode` 分辨。

⚠️ **不要用快照类端点取多邮编数据。** `asin_data.asin` 是 `UNIQUE`，全库每个
ASIN 只有一行，后采的覆盖先采的；而 `/api/results?batch_id=` 的 `batch_id`
只用来**挑 ASIN**，数据仍取那一行全局快照。结果是同一个 ASIN 的两个邮编批次，
`/api/results` 和 `GET /api/export/{batch_name}` 会返回**完全相同的行**，
且不会有任何报错。逐邮编准确的只有增量导出这一条路。
（这四条性质由 `tests/test_multi_zip_same_asin.py` 端到端钉住。）

### 截图查询与取图

- `GET /api/screenshots?batch_name=<批次>` —— 列出该批次每个 ASIN 的截图状态，
  每条带 `url`（仅 `status == "done"` 时非 `null`）。可按 `asin` / `status`
  过滤，按 ASIN 升序 `cursor` 分页，`limit` 上限 1000。响应里的 `progress`
  是**整批**计数，不受过滤影响。也可用 `batch_id=` 代替 `batch_name=`。
- `GET /api/screenshots/{batch_name}/{asin}` —— 取那张 PNG（`.png` 后缀可带可不带）。

取图的状态码是有意分开的，调用方据此决定要不要重试：

| 码 | 含义 | 该怎么办 |
|---|---|---|
| `200` | 图在这儿 | — |
| `404` | 没有这条截图记录（或批次不存在、文件已被清理） | 别重试 |
| `409` | 有记录但还没截好 | **稍后再来**，带 `Retry-After: 10` |
| `410` | 截图失败，不会再有；响应体带 `error_detail` | 别重试 |

整批打包下载仍是 `GET /api/export/{batch_name}/screenshots`（ZIP）。

### 卖家店铺采集

同样在 **任务管理** 页面（没有单独页面），上传卖家 ID 或店铺链接文件：

- 支持裸卖家 ID（`A` 开头）或带 `?me=` / `?seller=` 的店铺链接
- `discover_mode`：
  - `discover_only`：只枚举该店铺在售的 ASIN，写入 `seller_discoveries` 表
  - `with_detail`：枚举后自动为这些 ASIN 派生完整详情采集任务，追加进主任务队列
- API：`POST /api/upload-sellers`、`GET /api/seller-batches/{id}/progress`（发现+详情两阶段进度）、`GET /api/seller-batches/{id}/discoveries`（按 `seller_id` 过滤查看已发现的 ASIN）

### 采集结果

访问 **采集结果** 页面：

- **批次筛选**：下拉选择特定批次
- **变动筛选**：全部 / 价格库存变动 / 标题描述变动 / 新增 ASIN
- **搜索**：支持 ASIN、标题、品牌模糊搜索，多个关键词用换行或逗号分隔
  - 走 `pg_trgm` GIN 表达式索引，百万级数据下 5-50ms 量级（比 LIKE 全表扫快 ~1000 倍）
  - 短查询（<3 字符）自动 fallback 到 LIKE 路径保证正确性
- **分页**：keyset cursor 分页，单页上限 1000（近期从 200 上调）
- **两个减负开关**（默认都关，不传就是原行为）：`fields=a,b,c` 只返回指定列、
  `with_total=false` 不算全表 COUNT。本端点没有 `response_model`，**82% 的耗时在
  Python 序列化上**，所以这两个开关冲的是响应体而不是 SQL。
  实测 100 万行、单页 50 行：默认 `60.9ms / 274.2KB` → 窄投影 `52.1ms / 20.0KB`
  → 再关掉 total `2.7ms / 20.0KB`。采集结果页已经默认用上（首屏要 total，翻页不要）。
  ⚠️ 服务端会强制补 `id`/`asin`/`screenshot_path`/`updated_at`；非法列名 **422 拒绝**。
- **带 `batch_id` 时多四个字段**：`batch_task_status` / `batch_task_updated_at` /
  `batch_asin_data_updated_at` / `batch_has_asin_data`。
  这个端点底层是 `SELECT d.* FROM asin_data d JOIN batch_asins ...`，
  `asin_data` 是**每个 ASIN 一行的最新态**，`batch_id` 只是成员过滤器、
  **不参与取哪一行** —— 所以这批采失败的 ASIN（只要以前采过）会返回**上一次的旧行**。
  `batch_task_status != "done"` 就是"这行不是本批采的"。不带 `batch_id` 时这四个键
  **不出现**。⚠️ 从没采过的 ASIN 在这个端点**整行不返回**（INNER JOIN），
  要查覆盖率用 `GET /api/export/batch/{name}/records` 的 `coverage`。
- **选中删除**：勾选行 checkbox，点击"删除选中"（同时删除关联截图文件）
- **清空数据**：根据当前筛选条件智能删除
  - 选了批次 → 只删该批次数据
  - 输了搜索词 → 只删匹配数据
  - 无筛选 → 清空全部数据和截图（`DELETE /api/database`，不可撤销）

### 导出

导出不是一条路径，而是服务三类不同消费者的三套机制：

1. **UI 导出**（`server/api/export.py`，`GET /api/export/*`）：网页点"导出"按钮走的路径，弹窗选择格式（Excel/CSV）、字段（全选/仅价格库存/自定义勾选）、范围（当前批次 + 变动筛选）；支持流式导出，百万级数据不 OOM。导出列含**变体属性**（`color_name=X; size_name=Y`）/ 父体 ASIN / 变体 ASIN 列表；原 **EAN 列已下线**（amazon.com 实测 100% 为空，`ean_list` 已经不在可导出字段集合里），槽位由「变体属性」顶上。
2. **增量导出契约**（`server/api/export_incremental.py`，`GET /api/export/incremental`，**仅 PostgreSQL 后端**）：面向下游 catalog_sync（沃尔玛侧）的固定契约 v1，cursor+limit 分页，可选 `X-Export-Token` 请求头鉴权（`EXPORT_TOKEN`/`EXPORT_REQUIRE_TOKEN` 控制是否强制）。完整字段定义与不变量见 [`docs/incremental_export_contract.md`](docs/incremental_export_contract.md)。

   **拉得慢就用 `fields=`**：实测消费侧跨机房拉一页 500 条要 8~11 秒，其中服务端
   只占 ~1.1 秒 —— 剩下全在传 **3.4 MB** 响应体。单条 record 里 `slow` 占 69%，
   而 `slow.description` 一项就占 47%。只要价格库存的话
   `fields=fast` 直接降到 **6%**（3429 KB → 217 KB）。
   块名 `scrape_params`/`slow`/`fast`/`raw`，块内可用一层点号（`slow.title`）。
   ⚠️ `source_id`/`cursor`/`asin`/`marketplace`/`scraped_at`/`outcome` 恒返回、裁不掉；
   非法字段名 **422 拒绝**不静默丢弃。不传 `fields=` 时响应逐字段不变。

   **副标题在这里**：`slow.subtitle`。2026-08 Amazon 把商品标题拆成两个元素
   （`span#productTitle` + `div.dp-title-differentiators`），采集侧用 **Amazon 自己的
   分隔符 `" | "`** 把两段拼回 `slow.title`，**同时**把后半段单独给一份。
   内容重复是有意的：省得消费侧自己按 `" | "` 切标题 —— 标题正文里本来就可能出现 `|`。
   页面没有这一块时是 `null`（键恒在）。它**不进 `slow_hash`**（内容已在 title 里，
   再算一遍等于同一段文本数两次）。两个解析引擎共用同一份提取实现，
   否则同一商品会因为走了哪条引擎而给出不同答案。
   副标题**也落库**（`asin_data.subtitle`），因此 `/api/results` 和 CSV/xlsx 导出
   （列名「副标题」，在最右侧）也都有它。

   **运费在这里**：`fast.shipping`（float 或 null）+ `fast.shipping_raw`（原始串）。
   采集侧存的是字符串，三种形态映射到三个互不相同的结果 ——
   `"FREE"` → `0.0`（**确认免运费**）、`"$5.99"` → `5.99`、`"N/A"` → `null`
   （**这次没采到**，落地价算不出来）。⚠️ `null` ≠ `0`，**别写 `shipping or 0`**：
   把没采到当 0 的话落地价照样算得出来、看着也正常，只是偏小，两侧都不报错。
3. **按批次取记录**（`server/api/export_incremental.py`，`GET /api/export/batch/{batch_name}/records`，**仅 PostgreSQL 后端**）：回答"我刚推的这一批，到底采到了什么"。读**同一份事件流**、用**同一个** `_to_record`，所以 `records[]` 与上一条逐字段相同；差别只在 `WHERE batch_id = $1 AND seq > $2` 与游标的定义域。

   **它补的是一个会静默给出陈旧数据的洞。** `GET /api/results?batch_id=` 底层是
   `SELECT d.* FROM asin_data d JOIN batch_asins ba ...` —— `asin_data` 是每个 ASIN
   一行的**最新态**，`batch_id` 只是成员过滤器，**不参与取哪一行**。这批采失败的
   ASIN，只要它以前采过，照样命中 JOIN 返回**上一次的旧行**，而 JSON 响应里
   没有任何字段能看出年龄（CSV/xlsx 导出有 `data_source` 列，JSON 没有）。
   本端点读事件流，每一行都是**一次真实发生过的采集**，没采成就没有行。

   响应还带 `coverage: {asin_total, asin_with_event}`，一眼看出这批有没有 ASIN
   一次事件都没有。⚠️ 两者相等**不等于**都成功了 —— `not_found` 也算有事件，
   成功与否看每条记录的 `outcome`。
   ⚠️ **游标不可与上一条互换**：数值同源于 `seq`，喂错了不会报错，会**静默跳过**
   中间所有别的批次的事件。

   现成消费脚本 `tools/consume_batch.py`（只用标准库，拷走即用）：

   ```bash
   # 拿这一批的数据 + 截图
   python3 tools/consume_batch.py batch job_20260819 \
       --server http://host:8899 --token "$EXPORT_TOKEN" --out ./out --screenshots

   # 持续增量同步（游标存盘，可随时中断续跑）
   python3 tools/consume_batch.py sync \
       --server http://host:8899 --token "$EXPORT_TOKEN" --out ./out
   ```

   它按 `outcome` / `zip_verify` 过滤、按 **`(asin, zipcode)`** 去重取最新
   （同一 ASIN 的两个邮编是两条事实，只按 asin 归并会互相覆盖），
   写出 `.jsonl` + `.csv`，并把该批次已截好的图下到 `out/screenshots/<batch>/`。

4. **同步运维 API**（`server/api/sync.py`，`/api/v1/sync/*`，**仅 PostgreSQL 后端**）：读的是同一份事件流，但给的是内部原始事件形状 + 运维可观测性（relay 延迟、outbox 深度、保留期水位、`ack` 游标、强制裁剪通知）。完整契约见 [`docs/sync_contract.md`](docs/sync_contract.md)；面向 erpAPI 侧的业务端点（上传/状态/结果/失败明细）契约见 [`docs/erpapi_contract.md`](docs/erpapi_contract.md)。

后三者依赖事件流，只有 PostgreSQL 后端提供；万一跑在 SQLite 回滚路径上，它们统一返回结构化 `503`（不是 404——404 容易被消费方误读成"暂无数据"，导致游标停滞）。

### 响应压缩

服务端对 >1 KB 的响应做 gzip（level 6）。实测 500 条真实体量的增量导出：
**2319.6 KB → 389.6 KB（16%）**，压缩耗时 71 ms。

对客户端透明——`requests`/`httpx`/浏览器默认发 `Accept-Encoding: gzip` 并自动解压。
已压缩的类型（PNG/JPEG/zip/字体/视频）走 Starlette 的排除名单，不会被白压一遍。

选 level 6 而不是默认的 9 是实测的：9 比 6 多花 35 ms 只多省 4 KB。

⚠ 上面是沙箱夹具的数字，真实数据更难压（线上全字段量到 24%）。
更要紧的是：**放在反代后面时这一节可能整节静默失效** —— 见下一节。

### 反向代理（nginx）：别让 `Accept-Encoding` 被抹掉

压缩在 **FastAPI 侧**做（见上一节）。放在应用里而不是 nginx 里是有意的：换掉前面
任何一层反代它都还在，而且有测试盖着。但它有一个**静默失效**的前提 ——
上游必须真的收到 `Accept-Encoding: gzip`。

反代配置里这一行会让整节改动完全作废：

```nginx
proxy_set_header Accept-Encoding "";
```

这行在 nginx 配方里极常见（`sub_filter` 需要它，「让 nginx 自己压」的教程也教它）。
一旦有，`GZipMiddleware` 收到的请求就没有 `Accept-Encoding`，直接放行不压 ——
**没有任何报错、没有任何日志**，只是响应体大了四倍。2026-08 线上就踩过：
FastAPI 直连 710,184 字节，同一请求走公网 2,938,176 字节且响应头里没有
`Content-Encoding`。

三个不必配的（这几点容易被误传）：

* **不需要 `gzip_proxied any;`**。它只在**你想让 nginx 来压上游响应**时才有意义。
  上游已经压好了，nginx 不用参与。
* **不需要为 `Content-Encoding` 配透传**。`proxy_pass` 不改上游响应头，
  `proxy_hide_header` 的默认名单只有 `Date / Server / X-Pad / X-Accel-*`，不含它。
* **也不会双压**。`ngx_http_gzip_module` 遇到已带 `Content-Encoding` 的响应直接跳过，
  所以上游压过之后 nginx 那边 `gzip on` 只是空转，客户端永远只需解一层。

若通用 location 因为 `sub_filter` 改写 UI 路径而必须保留那一行，就给 API 单开一个
更具体的 location（`^~` 保证它优先于通用前缀匹配）：

```nginx
location ^~ /amazon-v4/api/ {
    proxy_pass http://127.0.0.1:8899/api/;

    # 关键：不要在这里 set Accept-Encoding —— 让客户端的原样透传到 FastAPI
    proxy_http_version 1.1;          # 默认 1.0，会让到上游的 keepalive 失效
    proxy_set_header Connection "";  # 配合上一行启用上游连接复用

    proxy_buffering on;              # 默认即 on，确认没被别处关掉
    proxy_read_timeout 300s;         # 大 limit 的导出服务端就要 1s+，跨机房再加传输
}
```

把 API 整族（而不只是 `/api/export/`）放进来是有理由的：`/api/results` 的
`fields=` / `with_total=` 同样靠压缩才能吃满，而 `sub_filter` 对 JSON 本来就是空转
（`sub_filter_types` 不设时只作用于 `text/html`），导出里唯一像路径的字段
`screenshot_path` 是库里存的相对路径、由客户端自己拼，不经反代改写。所以让 API
绕开 `sub_filter` 是**无损**的。

截图取图那类二进制端点不用特殊处理：`image/png` / `image/jpeg` 在 Starlette 的
默认排除名单里（见上一节），不会被白压一遍。

**改完必须实测**，因为这个失败模式不报错：

```bash
# 1. 响应头里应出现 content-encoding: gzip
curl -sI -H 'Accept-Encoding: gzip' -H "X-Export-Token: $EXPORT_TOKEN" \
  'http://<host>/amazon-v4/api/export/incremental?limit=500'

# 2. 压缩前后字节数对比；两行一样大就是压根没压上
for ae in gzip identity; do
  echo -n "$ae: "
  curl -s -o /dev/null -w '%{size_download} bytes  %{time_total}s\n' \
    -H "Accept-Encoding: $ae" -H "X-Export-Token: $EXPORT_TOKEN" \
    'http://<host>/amazon-v4/api/export/incremental?limit=500'
done
```

2026-08 线上实测（500 条/页，跨机房）：

| 请求 | 修复前 | 修复后 |
|---|---|---|
| 全字段 | 2,938,176 | 710,184（24%） |
| `fields=fast` | 211,356 | 34,284（16%） |

两刀叠起来 2,938 KB → 34 KB。注意真实数据比合成夹具**更难压**（上一节沙箱量到
16%，线上全字段是 24%）—— 报数时别把夹具数字当线上预期。

### 升级到带 `subtitle` 列的版本（2026-08）

`asin_data` 新增一列 `subtitle`。**不需要手工执行任何 SQL**，两个后端都在启动时
自动补列，幂等、可反复跑：

| 后端 | 补列方式 |
|---|---|
| PostgreSQL | `common/pgdb/schema.py:DDL_ALTERS` 的 `ALTER TABLE asin_data ADD COLUMN IF NOT EXISTS subtitle`，由 `init_tables()` 执行 |
| SQLite | `common/database.py` 的 ALTER 阶梯里新增一条 |

`CREATE TABLE IF NOT EXISTS` 对**已存在**的表是 no-op，所以老库只能靠上面这条
ALTER 拿到新列 —— 这一条由 `tests/pgdb/test_schema_migration.py` 专门守着
（它会把列 DROP 掉再跑 `init_tables()`，验证列回来了、列序对得上、老数据没被动）。

**为什么新列必须排在最末尾**（在 `created_at` / `updated_at` 之后）：`ALTER TABLE
ADD COLUMN` 只能追加到末尾，若在 DDL 里把它插在中间，**新建库**与**升级库**的
物理列序就会分叉；而 `SELECT d.*` 没有 `response_model`，列序会整个泄进 API 响应
—— 同一份代码两种响应，且 `verify_schema()` 只能对上其中一种。

实测（PG 17 与 SQLite 各跑一遍）：先用旧代码建库、写入一行，再用新代码启动，
新建库与升级库的列序**逐列相同**，老行原样保留、新列为 `NULL`。

升级后既有商品的 `subtitle` 都是 `NULL`，下一轮采集才会填上。

### 首次部署 PostgreSQL 后端的验证清单

新机器上把 PG 后端跑起来之后，按顺序验这几步，每一步不过就别往下走：

```bash
# 1. 环境体检（实测，不是读配置：PG 版本/扩展/编码、asyncpg 能否连、
#    DDL 能否建、分区能否创、磁盘、依赖）
python tools/preflight.py

# 2. 起服务
DB_BACKEND=postgres PG_DSN=postgresql://... python run_server.py

# 3. 事件流活着：relay_state 应为 running，outbox_depth 不应单调增长
curl -s localhost:8899/api/_debug/event-stream

# 4. 对账：counts 与直查一致
curl -s "localhost:8899/api/v1/sync/counts?from_seq=0&to_seq=100000000"
psql -c "SELECT count(*) FROM scraper.scrape_events"

# 5. 契约端到端：分页拉到底
curl -s -H "X-Export-Token: $EXPORT_TOKEN" \
     "localhost:8899/api/export/incremental?cursor=0&limit=500"
```

第 5 步要核对的不变量（完整定义见 [`docs/incremental_export_contract.md`](docs/incremental_export_contract.md)）：

- `source_id` 全局唯一（拉完全量后 `sort | uniq -d` 应为空）
- `cursor` 严格升序，`next_cursor` 等于最后一条的 cursor
- 空页返回 **200** 且 `next_cursor` **不推进**（不是 404）
- `scraped_at` 形如 `2026-08-05T10:00:00Z`（精确到秒、带 Z）
- `outcome != 'ok'` 的记录 `slow`/`fast` 基本为空——这类只进 snapshots

### 鉴权

**默认没有鉴权**——这套系统的默认假设是跑在内网。两个可选的令牌开关，都是 opt-in（不配就是原来的行为）：

| 环境变量 | 保护范围 | 传令牌的方式 |
|---|---|---|
| `EXPORT_TOKEN` | `GET /api/export/incremental` | `X-Export-Token` 请求头。配 `EXPORT_REQUIRE_TOKEN=1` 可让"没配令牌"直接关闭该端点，而不是放行 |
| `ADMIN_TOKEN` | 破坏性操作：`DELETE /api/database`（清空全库 + 全部截图）、删批次、`POST /api/batches/delete-bulk`、删结果、`POST /api/settings/reset`、worker 的删除/重启 | `X-Admin-Token` 请求头，**或**同源 cookie `admin_token`。控制台「设置」页有个输入框，填一次即写 cookie，之后网页上的操作自动带上 |

两者都只在**配置了**对应变量时才校验；没配则放行，并在日志里持续告警（`ADMIN_TOKEN` 是进程内首次命中受保护端点时警告一次）。`ADMIN_TOKEN` 刻意只锁破坏性操作，不锁日常读写（上传批次、改设置、建定时任务）——全锁的结果通常是运维图省事把整个开关关掉。

实现见 [`server/authz.py`](server/authz.py)。它是**纯 ASGI 中间件**而不是 FastAPI 依赖，因为 `/openapi.json` 是黄金基线里逐字节钉死的一步，`Depends(...)` 会把安全方案渲染进 schema。

> ⚠ 如果这台服务器能从不受信网络访问，`ADMIN_TOKEN` 和 `EXPORT_TOKEN` 都应该配上。上面那张表里的"破坏性操作"在未配置时是**任何人发一个 HTTP 请求就能触发**的。

### 定时自动采集

在 **系统设置** 页面的"定时自动采集"区域：

1. 点击 **新建任务**
2. 填写：
   - **任务名称**：如"每日核心商品监控"
   - **执行时间**：时:分
   - **执行间隔**：天数（输入数字，1=每天，2=每两天，7=每周...）
   - **ASIN 文件**：上传 xlsx/csv/txt（留空则使用主库全部 ASIN，主库增加时自动覆盖）
   - **需要截图**：是否截图存证
3. 创建后自动启用，到达时间点自动创建批次并开始采集
4. 支持手动 **立即执行**（播放按钮）
5. 支持 **启用/禁用** 切换和 **删除**

### Webhook 完成回调

上传任务时指定 `callback_url`，批次跑完后 server 自动 POST：

```json
{
  "event": "batch.completed",
  "batch_id": 123,
  "batch_name": "...",
  "external_id": "<调用方传入>",
  "status": "completed",
  "stats": {
    "total": 100,
    "done": 98,
    "failed": 2,
    "success_rate": 0.98,
    "duration_seconds": 542
  },
  "screenshots": {"total": 100, "done": 98, "failed": 2},
  "completed_at": "2026-05-20T07:42:11Z",
  "data_url": null,
  "export_url": null
}
```

- `event_id`（用于去重/幂等）**不在 JSON body 里，走 `X-Scraper-Event-Id` 请求头**，同时还有 `X-Scraper-Delivery-Attempt` 头标明第几次投递
- `data_url`/`export_url` 只有配置了 `server_public_base` 时才会有值，否则是 `null`

特性：
- **SSRF 防御**：只允许 `http`/`https`；拒绝 `localhost` 字面量；拒绝私有/回环/链路本地/保留/组播 IP；域名会解析全部 A/AAAA 记录逐一校验（防止"域名指向内网 IP"这种 SSRF），上传时校验一次、真正发送前再校验一次（TOCTOU 下是尽力校验，不是强保证）
- **失败重试**：30s → 5min → 30min → 2h → 终态（5 次后放弃）
- **手动重试**：`POST /api/batches/{name}/callback/retry`
- **状态查询**：`GET /api/batches/{name}/status`

### Worker 监控

访问 **Worker 监控** 页面（`仪表盘` 首页也有一份整体概览：任务统计、采集速度、总体进度、在线 Worker 数）：

- 全局并发/QPS 预算分配（`/api/coordinator`，按 Worker 健康度加权分配）
- 每个 Worker 的实时指标：
  - 成功率、封锁率、延迟 p50
  - 在飞请求、本地排队、待提交
  - 采集速度、已接受、已过期（stale）
- 软重启：重建 Session（新指纹+新 Cookie），采集不中断
- 清理离线 Worker

留意三个不同含义的"离线"判定，别混为一谈：Worker 列表上的 `online` 标记按 60s 无心跳判定；心跳感知任务回收（把 processing 任务收回 pending）按 120s 无心跳判定；Worker 注册表条目本身被后台循环清理按 300s 无心跳判定。

### 错误详情查看

任务管理页失败批次 ❗ 按钮，弹出错误明细（大批次走 `GET /api/batches/{id}/failures`，取代了仅返回前 200 条的旧版 `GET /api/batches/{name}/errors`）：

- 错误类型按数量倒序展示，含占比和颜色标识
- 11 种 `error_type` 全部中文化（被封锁 / 验证码拦截 / 请求超时 / 网络异常 / 解析失败 / Variant 偏移 / 邮编切换失败 / 服务端拒绝 / Session 未就绪 / 卖家发现失败 / 未知）
- 长 `error_detail` 完整展示（不再截断）
- 表格附带 Worker / 时间列，便于定位
- 点击 ASIN 直达亚马逊页面（含 `?th=1&psc=1` 强制变体）

### 系统设置

所有设置保存后 Worker 在 30 秒内自动同步，无需重启。

| 分类 | 主要参数 |
|---|---|
| 基础 | 邮编、重试次数、请求超时、Session 轮换频率 |
| 代理 | TPS 代理地址 |
| 速率 | 全局总并发/QPS 上限、单 Worker QPS、初始/最大/最小并发 |
| AIMD | 评估间隔、目标延迟、延迟上限、封锁冷却、成功率阈值 |
| 重试 | 自动重试开关、最大轮数、失败延迟 |

## 目录结构

```
amazon-scraper-v4/
  common/
    config.py          # 共享配置（含 DB_BACKEND 开关、各类阈值）
    dbfactory.py        # 存储后端开关：默认 postgres，按 DB_BACKEND 惰性 import
    database.py          # SQLite 实现（WAL + FTS5），**仅回滚兜底**，待生产稳定后整条删除
    models.py            # 数据类定义（AsinData / Batch / 可导出字段集合）
    slowhash.py          # 变更检测哈希（review_hash/slow_hash），纯 stdlib、零 common.* 依赖
    core/                # 真源工具层：SQLite 与 PostgreSQL 两个后端共用的纯逻辑
      idents.py             # ASIN 正则
      zipcode.py            # 邮编归一化公共子规则
      dbtables.py           # 清库/删除/搜索用的表名与 chunk 常量
      completeness.py       # 采集结果完整性位图
      retry.py              # error_type → 重试上限策略
      asindata.py           # 解析失败判定 + 内容哈希 + 变动对比
      timeutil.py           # 时间戳格式（DB 用/HTTP 用分开）
      lockmeter.py          # 锁等待/持有耗时统计（供 /api/_debug/lock-stats）
      coerce.py             # HTTP 边界整数强转
    pgdb/                # PostgreSQL 存储实现（可选后端）
      schema.py             # DDL：public 库表 + scraper 库事件流表
      pool.py               # 连接池 / 写连接
      tasks.py / batches.py / results_read.py / results_write.py / media.py / admin.py
      outbox.py / relay.py  # catalog_sync 事件流：写路径 outbox + 单例 relay
      retention.py          # 事件流保留期裁剪 + ack 机制
      OWNERSHIP.md          # 迁移期的文件归属/决策台账
  server/
    app.py              # FastAPI 入口：生命周期、中间件、4 条后台协程、
                        # callback 基础设施、以及被多方共用的私有助手
                        #（_normalize_asin/_batch_name/_is_safe_callback_url…）
                        # 路由本身已全部搬进 api/
    authz.py            # 破坏性端点的可选 ADMIN_TOKEN 鉴权（纯 ASGI 中间件）
    api/                # 从 app.py 拆出的路由模块
      pages.py              # 5 个页面路由（仪表盘/任务/结果/Worker/设置）
      batches.py            # 上传建批次 + 批次生命周期（状态/重试/删除/失败明细）
      schedules.py          # 定时采集任务（增删改查 + 立即执行）
      settings.py           # 运行时设置读写与恢复默认
      worker_queue.py       # Worker 拉任务 / 提交结果 / 提交截图
      worker_package.py     # Worker 安装包下载（完整包 / 代码更新包）
      fleet.py              # Worker 注册、心跳、软重启、并发配额
      sellers.py            # 卖家店铺采集（发现 + 详情派生）
      results.py            # 结果查询/搜索/删除
      export.py             # UI 导出（xlsx/csv/截图打包）
      export_incremental.py # 增量导出契约（PG-only，面向下游 catalog_sync）
      sync.py               # /api/v1/sync/*（PG-only，运维/对账契约）
      debug.py              # 诊断端点：lock-stats、event-stream、清空数据库
    templates/          # Jinja2 页面模板
    static/             # 静态资源 + 截图存储
  worker/
    engine.py           # 采集引擎（流水线 + 每协程独立 SessionSlot + 3 并行 submitter）
    session.py           # curl_cffi Session 封装 + zip 验证 + variant 检测支持
    parser.py            # Amazon 页面解析器（selectolax/lxml 双引擎，page_asin/twister/卖家/制造商提取）
    proxy.py              # TPS 代理管理
    adaptive.py           # AIMD 自适应并发控制器
    metrics.py            # 性能指标收集（滑窗 + 双 EWMA）
    screenshot.py         # Playwright 截图子进程
    ziputil.py            # 邮编（不是压缩包）校验工具：判断配送控件里是否命中目标邮编
  tools/
    smoke_local.py        # 端到端冒烟测试（上传→拉取→提交→查询→契约校验），面向操作者
    preflight.py          # 上机环境体检：PG 连通性/建表/排序规则/磁盘/依赖，
                          # 外加路由顺序一项（判定逻辑来自 server/routing.py，
                          # 不是第二份实现）
    consume_batch.py      # 消费端脚本（只用标准库，拷到任何机器即用）：
                          #   batch <name>  拉某批次真正采到的数据 + 截图
                          #   sync          全局增量同步，游标存盘
    desc_glue_check.py    # 单个解析 bug（<br> 不产生分隔符）的修复验证工具
  docs/
    erpapi_contract.md            # erpAPI 侧业务端点契约
    incremental_export_contract.md # 增量导出契约 v1
    sync_contract.md               # /api/v1/sync/* 运维契约
    local_macos_setup.md           # 本机 macOS 开发环境搭建（含 PostgreSQL 后端）
  tests/
    test_*.py            # 单元/集成测试（解析质量、批次/结果 API、错误码、golden 相关守卫等）
    pgdb/                 # PostgreSQL 存储层测试
    golden/                # 字节级 HTTP 行为回归基线（同一份基线两个后端都要过）
  data/
    scraper.db            # SQLite 数据库文件（仅 DB_BACKEND=sqlite 回滚路径会用到）
    scraper.db-wal/-shm    # WAL 日志 + 共享内存
    exports/                # 导出文件 + 临时文件（自动清理）
    schedules/              # 定时任务 ASIN 文件
  deploy/
    setup.sh              # Server 部署脚本（不含 worker，也不建库）
    server.service         # systemd 服务配置
  run_server.py          # Server 启动入口
  run_worker.py          # Worker 启动入口
  .env.example           # 环境变量样例（存储后端/代理/邮编/导出鉴权等）
```

## 数据库

正式后端是 **PostgreSQL**（`DB_BACKEND` 未设置时即走它，见 `common/dbfactory.py`）。仓库里还保留一条 SQLite 实现作为回滚兜底；两个后端对外暴露的方法签名一一对应，行为等价性由 `tests/golden/` 的字节级基线（同一份基线两个后端都要过）+ 800 条 pytest 用例持续验证。

### `public` schema —— 业务表

| 表 | 说明 |
|---|---|
| `batches` | 批次元数据 + callback 状态 + external_id |
| `batch_asins` | 批次-ASIN 多对多映射 |
| `asin_data` | ASIN 数据（UNIQUE，覆盖更新） |
| `asin_changes` | 变动检测历史（价格/库存/标题/新增） |
| `tasks` | 采集任务队列（含 `lease_epoch` + `auto_retry_count` + 卖家发现任务的 `task_type`/`task_meta`） |
| `screenshots` | 截图追踪 |
| `seller_discoveries` | 卖家店铺发现结果（`(batch_id, seller_id, asin)` 主键） |

`asin_data` 包含字段：ASIN / 标题 / 品牌 / 价格 / 库存 / 评分 / 评论数 / 卖家店铺 ID / 卖家名 / 父 ASIN / 变体属性 / 类目 / 尺寸 / 重量 / 制造商 / 排名 / ...

**搜索索引**：`pg_trgm` GIN 表达式索引，三条 —— `idx_asin_data_asin_trgm` / `idx_asin_data_title_trgm` / `idx_asin_data_brand_trgm`。

> ⚠ **建库必须 `LC_COLLATE=C LC_CTYPE=C`**。PG 的默认排序规则与代码里假定的字节序不同，会让分页游标、搜索结果、导出顺序都对不上；而排序规则是**建库时定死**的，事后只能重建库。

**写连接 / 只读连接池物理隔离**：写路径是**一条专用连接**（事务粘在连接上，见 `common/pgdb/pool.py` 的 D-2），只服务 worker 热路径（`pull_tasks`/`accept_results_batch`）；仪表盘/导出/聚合查询走独立的只读连接池（`PG_POOL_MIN`/`PG_POOL_MAX`，默认 2-10），避免管理后台的重读把 worker 的写堵住。

### `scraper` schema —— catalog_sync 事件流

| 表 | 说明 |
|---|---|
| `scraper.scrape_outbox` | 写路径暂存表：业务写和事件写在同一个事务里提交 |
| `scraper.scrape_outbox_dead` | 死信表：relay 无法处理的行原样保留，供人工排查/重放，绝不静默丢弃 |
| `scraper.scrape_events` | 正式的、按 `seq` **分区**的追加事件日志，下游消费者按游标顺序拉取 |
| `scraper.sync_meta` | 部署标识（`gen`/`instance_id`）、`ack_seq`、强制裁剪记录等运维状态 |

**事件流机制**：每一次终态写入（结果入库、任务终态失败、租约失效）都会在**同一个数据库事务**里往 `scrape_outbox` insert 一行；一个通过 `pg_try_advisory_lock` 选举出的**单例** relay 协程每秒左右把 outbox 排干，为每一行分配单调递增的 `seq` 并写入分区好的 `scrape_events`，drop-outbox 和 insert-events 在同一事务里提交。这样设计是因为直接对并发写入的表做 `seq > X` 轮询，行有可能乱序提交导致游标永久跳过某些行；outbox 把"行已存在"和"行已被安全排序"拆成了两个独立步骤。

**保留期（retention）**：只整分区 `DROP`，从最老分区往后推，遇到第一个不能删的分区就停手（不会在分区中间留洞）。可删除下界 = `max(硬下限, min(时间下限, ack 水位 − slack))`——`ack_seq` 是下游通过 `POST /api/v1/sync/ack` 确认的、已经落盘的最高 `seq`。磁盘紧张时硬下限可以越过 `ack` 强制裁剪未确认数据，这种情况会记进 `sync_meta` 的 `forced_prune_log`（持久闩锁），下游需要显式 `POST /ack-prune` 才能清掉这条记录——这条设计是为了不让"磁盘紧张时的数据丢失"被悄悄吞掉。

**当前状态**：迁移已完成，PostgreSQL 提供生产服务。SQLite 那条路径仅作为回滚兜底保留，待生产稳定后整条删除。

## 核心机制

### 任务分发防重复（lease_epoch）

多 Worker 并发采集的核心难题是任务重复分发。两个后端都通过 `lease_epoch` 机制解决：

- 每个任务有 `lease_epoch` 计数器（初始 0）
- 任务被回收重新入队时 `lease_epoch += 1`（所有回队路径：回收/失败重试/归还）
- Worker 提交结果时携带 `lease_epoch`，Server 原子校验：`WHERE task_id=? AND worker_id=? AND lease_epoch=? AND status='processing'`
- 校验通过才写入 `asin_data`，不通过返回 `stale=true`（迟到结果被丢弃）
- 结果写入和任务完成在同一事务内，不会出现半写状态

### 分层重试架构

第 1 层不是"一律本地重试 + 轮换"的单一循环，而是**按错误类型分流**：

```
┌────────────────────────────────────────────────────────────────┐
│ 层 1：Worker 本地处理（按错误类型分流，预算 MAX_RETRIES=3）      │
│   网络超时/无响应        → 本地重试，不轮换 session              │
│   命中 block/验证码/API 封锁 → 轮换 session，但只出手一次，       │
│                              立刻扔给层 2（不占本地重试预算）    │
│   variant_offset / 邮编切换失败 → 完全不重试不轮换，直接终态失败  │
│   解析失败/空标题/非美元价格/核心字段全空 → 轮换 + 本地重试        │
│   配送信息缺失（疑似降级页） → 独立重试预算，不占用上面的循环     │
├────────────────────────────────────────────────────────────────┤
│ 层 2：Server fail_task / accept_results_batch                  │
│   收 success=False → retry_count++                              │
│   if retry_count >= cap[error_type]: status='failed'           │
│   else: status='pending'（重新入队）                            │
├────────────────────────────────────────────────────────────────┤
│ 层 3：auto_retry_failed_tasks（每 30s 周期任务）                │
│   扫描 status='failed' AND auto_retry_count < 2 AND 失败>1min  │
│   重置为 pending，再走 2 轮（每轮 = 层 1+2 完整循环）          │
├────────────────────────────────────────────────────────────────┤
│ 层 4：reclaim_dead_worker_tasks（每 30s 周期任务）              │
│   扫描 status='processing' AND（心跳超 2min 或 任务超 10min）  │
│   回收为 pending、lease_epoch++（迟到结果失效）                 │
└────────────────────────────────────────────────────────────────┘
```

正常错误（network / timeout / blocked / captcha / ...）最大尝试 = `MAX_RETRIES × MAX_RETRIES × (1 + auto_retry_cycles) = 3 × 3 × 3 = 27 次`；`variant_offset` 这类首次失败即终态的错误只会尝试 1 次。

层 3 的两个参数是**运行期设置**，可随时改，不用重启：

| 设置项 | 默认 | 含义 |
|---|---|---|
| `auto_retry_failed_enabled` | `true` | 关掉就完全不自动重试 |
| `auto_retry_cycles` | `2` | 最多捞回来几轮 |
| `auto_retry_delay_minutes` | `1` | 终态失败后至少等这么久才有资格被捞 |

```bash
curl -X PUT http://<server>:8899/api/settings \
  -H 'Content-Type: application/json' -d '{"auto_retry_delay_minutes": 1}'
```

⚠️ 代码里的默认值只对**全新部署**和 `POST /api/settings/reset` 之后生效 ——
`_load_settings` 是「默认值 `.update(` 磁盘上的 `runtime_settings.json)`」，
已有部署以磁盘那份为准。升级后想让新默认值生效，要么走上面的 `PUT`，
要么删掉 `runtime_settings.json` 里那个键。

按 `error_type` 分级的不重试策略（`common/core/retry.py`，SQLite/PostgreSQL 共用同一份）：

| 错误类型 | layer 2 cap | layer 3 自动重试 | layer 4 手动重试 |
|---|---|---|---|
| `network` / `timeout` / `parse_error` / `blocked` / `captcha` 等 | 3 | ✓ | ✓ |
| `variant_offset` | **1**（不重试，首次失败即终态） | ✗ 跳过 | ✗ 跳过 |

要加新类型只需改 `LIMITED_RETRY_ERROR_TYPES` 和 `NO_AUTO_RETRY_ERROR_TYPES`，全链路自动跟上。

### Variant 偏移检测（防止数据中毒）

多属性产品（如同 parent 下的 2-100 个变体）请求 `/dp/B0XXX?th=1&psc=1` 时，Amazon 偶发返回另一个 variant 的页面（A/B test / 库存 / 缓存）。若 parser 不校验，会把错 variant 的 title 写到 B0XXX 这一行，造成数据中毒。

**parser 层防御**（`worker/parser.py` `_extract_page_asin`）：按优先级提取 `<input id="ASIN" value="...">` 隐藏字段 → `<link rel="canonical" href=".../dp/ASIN">` → JS 中的 `"currentAsin":"..."`，任一信号 ≠ 请求 ASIN → 标记 `error_type='variant_offset'`，**绝不写入主表**。

**worker 处理策略**：不本地重试，不 rotate session（避免打爆隧道 5 QPS），直接上报失败，server 首次收到后即标记终态 failed。

### Session 管理（每协程独立，无全局热备）

**没有全局共享 session。** 每个采集协程各自持有一个独立的 `SessionSlot`（各自的 `AmazonSession`/指纹/Cookie），一次轮换只影响它自己所在的协程，其它协程照常采集——不再需要"后台预热一个全局热备 session、轮换瞬间切换"这套机制。

- **主动轮换**：单个 slot 累计 `SESSION_ROTATE_EVERY`（默认 1000）次成功请求后触发
- **被动轮换**：被封 / 验证码 / API 封锁 / 非美元价格 / 核心字段全空时触发；`rotate()` 内部是**先关旧 session 再开新 session**（不是延迟关闭），同一个 slot 两次轮换之间有 5s 防抖
- **variant_offset 不轮换**：避免打爆隧道 QPS，见上一节

### TPS 代理模式

每次 HTTP 请求通过代理自动获取不同出口 IP，代理管理器本身不做通道/频道管理，只是把同一个代理 URL 原样交给每次请求；换 IP 完全是代理服务商那一端的行为。代理地址格式：`http://user:pwd@host:port`

### AIMD 自适应并发

单个 `AdaptiveController` 全局共享一套并发信号量，按优先级评估：冷却中 → 保持；封锁率超阈值 → ×0.7 + 冷却；成功率过低 → ×0.7 + 半冷却；延迟 p50 超上限 → ×0.7 + 半冷却；RTT 梯度上升（Gradient2）→ 预防性 -1；带宽饱和 → 保持；健康且梯度良好 → +2（带抖动）。

### 邮编校验模式

`ZIP_VERIFY_MODE` 控制切换邮编后如何确认生效：

- **`on_fetch`（默认）**：不额外发送独立验证 GET；切邮编只 POST，采完商品后用商品页 HTML 自带的配送控件（glow 挂件）判断邮编是否生效。未生效时先在同一 session 上原地重发一次邮编 POST（最多 `ZIP_REPOST_MAX_TRIES` 次，默认 2），仍不生效才降级为冷轮换换 IP。
- **`standalone`**：旧行为，切邮编后单独发一次首页验证 GET。

`on_fetch` 相比 `standalone` 每个 ASIN 省一次验证请求，真机 A/B 验证过数据准确性和邮编失败率后设为默认；需要回退旧行为时设 `ZIP_VERIFY_MODE=standalone`。

### 配送降级页检测

部分请求会被 Amazon 判定为可疑（数据中心 IP 经 TPS 轮换代理），返回"能买但没有配送区"的降级页：标题/价格/图片正常，但配送 ETA 和变体报价被拿掉。商品明明可售（FBA/In Stock）却拿不到配送区时，判定为降级页 → 换 IP 重采，最多 `DELIVERY_RETRY_MAX`（默认 2）次仍未恢复才接受 N/A。这个重试预算和上面"分层重试架构"里的常规重试预算是分开计的，互不占用。`DUMP_DEGRADED_HTML=1` 可以把重试耗尽后仍判定为降级的原始 HTML 存盘（`worker/degraded_dump/`），供离线分析、收紧检测条件；`DEGRADED_DUMP_MAX`（默认 300）限制最多存多少张，防止塞满磁盘。

### 双解析引擎

`selectolax` 是生产首选解析引擎，`lxml` 是**进程启动时**的整体回退（没装 `selectolax` 才会启用整个 worker 进程走 lxml，不是逐页 try/fallback——某一页用 selectolax 解析失败不会退回 lxml 重试那一页）。两条路径产出的字段完全对齐（含评分/评论数/卖家信息，这几个字段曾经只在 selectolax 路径上实现，lxml 回退时会结转旧值，现已修复统一）。每条采集结果记录 `parse_engine` 字段标明实际用的是哪个引擎，采集前即可通过它判断当前 worker 是否真的装了生产引擎。

### 数据质量修复：制造商匹配 / 卖家信息提取

- **制造商精确匹配**：改成精确 key 匹配（`manufacturer` / `manufacturer name` / `manufactured by`），修掉了旧版本"子串匹配全文档"可能把商品详情页里"Manufacturer recommended age"之类的年龄段文案当成制造商写入的问题。
- **卖家店铺 ID/名称提取**：四级回退——buybox 卖家链接 → 页面里其它带 `seller=` 的链接 → 亚马逊自营文案识别（"Sold by Amazon.com" 类，命中则直接给 `seller_id=AMAZON`）→ 页面 JS 里的 `merchantID` 兜底。

### 完整性位图

每条采集结果记录一个 4 位完整性位图（`common/core/completeness.py`）：面包屑区块 / 详情表区块 / 图片区块 / 是否已测量。判定依据是**对应的 HTML 区块是否存在**，而不是"解析出的值是否为空"——这样才能把"亚马逊这次请求本来就没返回这块内容（降级/A-B test）"和"返回了但解析成了空值"区分开，为后续排查降级页/解析回归提供信号。

### 多属性变体提取 + 自动展开

**变体属性提取**（`worker/parser.py` `_parse_twister`）：单次请求的商品页内嵌 Amazon twister
变体矩阵，无需逐个子体请求即可拿到全家族。
- `dimensionValuesDisplayData`（子 ASIN → 各维度值）+ `dimensions`（有序维度名，如
  `["color_name","size_name"]`）→ 本 ASIN 写入 `variant_attributes`，格式
  `color_name=Red; size_name=L`（沿用全库「分隔字符串」风格，无 JSON；维度任意：
  color/size/style/flavor… 一列吃下，不加稀疏列）
- `variation_asins` 由 twister 的真实家族键生成（精确同族）；取不到时回退旧的全页正则
- worker 只负责产出 `variant_attributes` / `variation_asins` 这两个字段；下面的"自动展开"逻辑完全在服务端/存储层实现（SQLite: `common/database.py`；PostgreSQL: `common/pgdb/media.py`），worker 侧不参与

**自动展开**（batch 级 opt-in，`expand_variants`）：把同款全部变体补齐进【同一批次】。
- **触发时机**：批次本轮**全部终态后**（completion watcher），不是每条结果实时 →
  上传 1 文件 = 1 批次 = 1 导出，**不裂变成多个批次/文件**，只是行数变多（补齐家族）
- **流程**：轮1 采上传 ASIN → 读其 `variation_asins` → 入队同族进同批 → 轮2 采变体 →
  无新增即完成。**正常 2 轮收敛**（同族每个成员都列同一家族，轮2 发现的都已在批内）
- **去重防循环**：`tasks` 表 `UNIQUE(batch_id, asin)` + `INSERT OR IGNORE`，每个
  `(batch, asin)` 至多一行 → 数学上保证收敛、绝不死循环（即便家族列表不一致也只多 1 轮）
- **安全阀**（`config.VARIANT_EXPAND_FAMILY_CAP`，默认 10）：单个产品候选同族 > 上限
  视为巨型/定制类家族（如定制尺寸围栏网，单族可达 2500），**跳过该产品展开 + 打 WARNING**，
  防止一个种子炸成几千任务；正常小家族（≤上限）照常展开
- **存储仍为 UTC / 全库口径不变**；非变体产品 `variant_attributes` 为空，不造假数据

> 注意：变体属性依赖 worker 的 twister 解析，**所有 worker 机器都需更新代码**才能产出
> 干净的同族数据（否则旧正则会混入广告/相关推荐 ASIN）。

### SQLite 性能优化 + 读写解耦

**连接模型：写连接（worker 热路径）与只读连接池（仪表盘 / 导出）物理隔离。**
全服曾共用单个 aiosqlite 连接，读写挤在同一后台线程：管理后台一开导出（扫 1GB+ asin_data）
或聚合查询（百万行 tasks），写操作 `pull_tasks` / `accept_results_batch` 就在 `BEGIN IMMEDIATE`
处排队，持锁飙到几十秒~数分钟，触发 worker 的 10s 拉取 / 8s 提交超时。
WAL 模式天然支持「多读 + 单写」并发，故把重读全部移到独立只读连接池，写连接只服务 worker，根治此类 stall。

**只读连接池**（默认 3 条）：每条 `PRAGMA query_only=ON`，独立后台线程，私有 cache 16MB；`read()` 上下文管理器借还连接，池空排队（读侧背压，绝不触碰写连接）。路由的读：`get_batches` / `get_progress` / `get_results` / `iter_results`(导出) / `get_change_stats` / `get_batch_completion_status` / 批次错误详情。

**覆盖索引** `idx_tasks(batch_id, status)`：`get_batches` 的「按 batch 统计各 status」走 index-only，不再为百万行逐行回表读 status。

**trigram 全文搜索**：`asin`/`title`/`brand` 三条 `pg_trgm` GIN 表达式索引；短查询（<3 字符）trigram 无法命中，fallback 到 LIKE 路径保证正确性。量级：全表扫 46 秒 → 5-50 毫秒（~1000× 加速；该数字来自 SQLite 侧 FTS5 时代的实测，PG 侧同量级但未逐条复测）。

### 心跳感知任务回收

- **主机制**：后台 30s 循环检查，只回收死 Worker（无心跳 2 分钟+）的 processing 任务
- **硬超时兜底**：10 分钟（liveness safety net），防止任务永久占位
- 回收不在 `pull_tasks()` 中执行，避免每次拉取都触发误回收
- 有了 lease_epoch，即使硬超时误触发也不会写脏数据，只浪费少量代理资源
- 与「Worker 监控」页面的 60s / 300s 两个离线判定是三件独立的事，见该节说明

### 双口径统计

Worker 维护两组指标：

| 指标 | 计数时机 | 含义 |
|---|---|---|
| `success` / `failed` | 采集完成时 | 本地采集结果（代理+Amazon 层面） |
| `accepted` / `stale` | Server 响应后 | 服务端实际录入（`success - accepted = 重复采集量`） |

### Worker 写路径优化

```
playwright 抓取 → _result_queue (maxsize=500)
                       ↓
                 3 个并行 _batch_submitter（共用 queue）
                       ↓
                 POST /api/tasks/result/batch
                       ↓
                 server accept_results_batch
```

- **3 个并行 submitter**：单个 submitter 在 HTTP retry 时不阻塞其他
- **HTTP timeout**：8s，retry backoff：0.5/1/2 秒（原 1/2/4 秒，最坏耗时 52s → 27.5s）
- **fallback 改并发**：单条 fallback 用 `asyncio.gather` 并发提交（10×10s → ~10s）
- **反压**：`_result_queue` 500 上限，提交慢时自动减速采集

### PostgreSQL 事件流细节

见上方「数据库 → PostgreSQL」一节的 outbox/relay/retention 说明；对外契约见 [`docs/sync_contract.md`](docs/sync_contract.md) 与 [`docs/incremental_export_contract.md`](docs/incremental_export_contract.md)。

## 调试与诊断

### 锁竞争快速诊断

```bash
# 重置统计
curl -X POST http://<SERVER>:8899/api/_debug/lock-stats/reset

# 跑一段采集后查看
curl http://<SERVER>:8899/api/_debug/lock-stats | python3 -m json.tool

# 关注：
# - pull_tasks.waits.p99 高 → 锁竞争严重
# - accept_results_batch.holds.max 大 → commit 抖动
# - stage_timings.commit 大 → SSD/fsync 问题
```

### PostgreSQL 事件流诊断（仅 PostgreSQL 后端）

```bash
curl http://<SERVER>:8899/api/_debug/event-stream | python3 -m json.tool
```

返回 relay 状态、outbox 深度、每分钟事件数、死信数等；SQLite 后端下固定返回 `{"enabled": false}`。此端点不出现在 `/openapi.json` 里。

### 手动重试失败任务

UI：任务管理页点击"重试"按钮，跳过 `NO_AUTO_RETRY_ERROR_TYPES`（如 `variant_offset`）。

API：
```bash
curl -X POST http://<SERVER>:8899/api/batches/<batch_name>/retry
```

返回：
```json
{
  "ok": true,
  "retried": 1230,
  "skipped_no_retry": 45,
  "no_retry_types": ["variant_offset"],
  "forced": false
}
```

### 手动复采单个任务

```sql
-- 重置 task 为 pending（用 ASIN 或 task_id 定位）；SQLite/PostgreSQL 字段一致，
-- sqlite3 打开 data/scraper.db 或 psql 连 PG_DSN 均可执行同一条语句
UPDATE tasks SET status='pending', error_type=NULL, error_detail=NULL,
                 retry_count=0, auto_retry_count=0
WHERE asin = 'B0XXXXXXXX';
```

### Webhook 回调状态查询

```bash
curl http://<SERVER>:8899/api/batches/<batch_name>/status | python3 -m json.tool
```

返回完整状态：任务进度 / 截图进度 / callback 状态 / external_id / 重试次数 / 下次重试时间。

## 测试

标准测试命令是 pytest（`pytest.ini` 已配置 `testpaths = tests`）：

```bash
pytest tests/ -q                              # 默认 SQLite 后端
DB_BACKEND=postgres pytest tests/ -q          # PostgreSQL 后端（含 tests/pgdb/ 全量）
```

- **`tests/golden/`**：字节级 HTTP 行为回归基线（`tests/golden/samples/baseline.json`，64 步覆盖上传/拉任务/提交/分页/搜索/导出/删除等完整生命周期），用于保证存储层重写（SQLite ↔ PostgreSQL）不改变对外可观察行为。用法见 [`tests/golden/README.md`](tests/golden/README.md)：`python -m tests.golden.run {selfcheck|record|verify}`。
- **`tests/pgdb/`**：PostgreSQL 存储层专项测试（批次/任务/结果读写、并发、保留期、relay/事件流接线等）。
- 其余测试覆盖解析质量、批次名冲突语义、搜索转义、游标分页活性、错误码闭集、卖家 API 等具体行为契约。
- `tools/smoke_local.py` 是一个不依赖真实代理/Amazon 的端到端冒烟脚本（上传 → 拉任务 → 提交结果 → 查询 → 事件流契约校验），适合部署后快速验证：`python3 tools/smoke_local.py`。

## 性能基线

### 为什么要**多开 worker**，而不是把单个 worker 的并发调大

**结论：单个 worker 进程的吞吐上限由 CPU 解析速度决定，与并发数无关。**

`parse_product` 是**同步调用**，直接跑在 worker 的事件循环里
（`worker/engine.py` 里那处 `self.parser.parse_product(...)`），`worker/` 全树
没有任何 `to_thread` / `run_in_executor` / 线程池。实测（475 KB 的商品页，
selectolax 引擎）：

```
parse_product 平均耗时           126.5 ms   ← 纯 CPU
单核纯解析吞吐上限               ≈ 474 个/分钟
```

于是在**一个进程内**提高并发度完全不起作用 —— 实测同一个事件循环里
`gather` 16 路解析：

```
顺序 16 次        2.02s
gather 16 路      1.93s     加速比 1.05x   ← 等于没有并行
4 进程并行 16 次   0.67s     加速比 3.01x   ← 多进程才真的并行
```

原因是 asyncio 的并发只对**等待**有效（HTTP 往返、代理延迟）。解析是计算，
一个协程解析的那 126 ms 里，同进程内**其余协程全部冻结** —— 包括那些响应
已经回来、只等着被解析的。所以：

| 配置 | 结果 |
|---|---|
| 1 worker，并发 32 | ≈ 500/min —— 已经顶到单核解析上限，此时瓶颈是 CPU 不是网络 |
| 1 worker，并发 256 | **不会更快**。多出来的并发只增加内存、session 数与调度开销；单次解析把循环占住的时间还会拉大延迟抖动，反而更容易触发 AIMD 降速与超时 |
| 8 worker，各并发 32 | 8 个进程 = 8 个事件循环 = 真正吃满多核，吞吐随核数线性放大且稳定 |

**怎么定参数**：worker 进程数 ≈ CPU 核数（留 1 核给 server/PG）；单个 worker
的并发只要够把网络等待填满即可，`32` 已经绰绰有余，再往上只有坏处。
想验证自己机器的上限，把上面那段解析耗时在目标机器上测一遍：
`60 / 单次解析秒数` 就是**单个 worker 进程**的理论天花板。

---


> 以下为 SQLite 后端在一次实测（DMIT VPS 1C/2GB + 10 worker，2026-05）中的快照，架构此后有过多处调整（如 Session 管理改为每协程独立），未重新测过，仅供数量级参考，不代表当前版本的实测结果。

| 指标 | 数值 |
|---|---|
| 单 worker 采集速率 | 5-8 ASIN/s |
| 全局采集峰值 | 3000-5000 ASIN/min（60-83 ASIN/s）|
| Server `accept_results_batch` 持锁 p50 | 7.5 ms |
| Server `accept_results_batch` 持锁 p99 | 71-94 ms |
| Server `pull_tasks` 持锁 p50 | 0.28 ms |
| 30k ASIN 跑完总时长 | ~9 分钟 |
| `/api/results` 搜索（10 万行）| 5-50 ms |
| `/api/batches` 仪表盘加载 | 35-100 ms |
| 数据库主表 | ~1.8 GB / 29 万 ASIN（VACUUM 后）|
| 搜索索引开销 | ~90 MB / 29 万 ASIN（SQLite FTS5 时代实测）|

**读写解耦前后**（导出风暴下：3 路全量 CSV 导出 + 仪表盘轮询并发打）：

| 指标 | 解耦前 | 解耦后 |
|---|---:|---:|
| `pull_tasks` 持锁 max | **288,000 ms** | **272 ms** |
| `pull_tasks` 等锁 max | **259,000 ms** | **963 ms** |
| 8899 并发连接堆积 | 42-43 | 4 |
| 冷启动耗时（2.4GB 库）| ~150 s | ~10 s |

## License

Private use.
