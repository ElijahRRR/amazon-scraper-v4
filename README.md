# Amazon Scraper v4

高性能分布式 Amazon 商品数据采集系统。Server/Worker 分离架构，支持百万级 ASIN 采集、变动检测、定时任务、截图存证、webhook 回调通知、第三方卖家店铺采集；存储后端可在 SQLite（默认）与 PostgreSQL 之间切换，PostgreSQL 模式下额外提供面向下游 catalog_sync 的事件流与增量导出契约。

## 架构

```
Server (FastAPI)                          Worker (可部署多台)
  - Web 管理控制台（仪表盘/任务/结果/           - curl_cffi TLS 指纹模拟
    Worker 监控/设置，5 个页面）               - AIMD 自适应并发控制
  - 任务分发 & 结果收集                        - 每采集协程独立 Session（冷轮换，
  - 存储后端可选（DB_BACKEND）：                  无全局热备）
      sqlite（默认，WAL + FTS5 trigram）       - 双解析引擎：selectolax 优先，
      postgres（asyncpg，opt-in）               lxml 为进程级回退
  - 定时任务调度                               - Playwright 截图（可选）
  - 全局并发配额协调                           - lease_epoch 防重复提交
  - Webhook 完成回调（SSRF 防御）              - variant_offset 检测
  - 卖家店铺采集（发现 ASIN → 详情采集）        - 多属性变体提取（twister）
                                              - 邮编校验 + 配送降级页重试

仅 PostgreSQL 后端可用：
  - catalog_sync 事件流：写入 outbox → 单例 relay → 按 seq 分区的事件表
  - 三条对外契约：/api/export/*（UI 导出）、/api/export/incremental（下游增量导出）、
    /api/v1/sync/*（下游运维/对账：游标拉取、状态、计数、ack、保留期）
```

SQLite 与 PostgreSQL 两套存储层由同一份公开方法签名驱动（`common/pgdb/` 对 `common/database.py` 做了逐方法契约比对），已用字节级 HTTP 行为基线（`tests/golden/`）与近 700 条 pytest 交叉验证过等价性，但**生产环境目前仍运行在 SQLite 上**——PostgreSQL 是已就绪、可选启用的后端，尚未完成生产切换。除非你需要 catalog_sync 事件流，否则不需要碰任何 PostgreSQL 相关配置。

## 快速开始

### 1. 环境要求

- Python 3.10+（若要启用 PostgreSQL 后端，本地开发建议锁定 Python 3.12——3.13/3.14 上 `curl_cffi`/`lxml`/`selectolax`/`asyncpg` 常没有预编译 wheel，会掉进源码编译）
- TPS 代理（帐密认证，每次请求自动换 IP）
- 服务器最低 1C / 2GB / 20GB SSD（SQLite 模式；PostgreSQL 模式需要单独部署一个 PG 实例）

### 2. Server 部署（SQLite，默认，多数场景选这个）

```bash
git clone https://github.com/ElijahRRR/amazon-scraper-v4.git
cd amazon-scraper-v4
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 配置代理等
cp .env.example .env
# 至少填一下 PROXY_URL；DB_BACKEND 留空/sqlite 即走默认后端，其余变量都可不填

python3 run_server.py
```

Server 默认监听 `0.0.0.0:8899`，浏览器访问 `http://<IP>:8899`。首次启动自动建表 + FTS5 全文索引 + 完成数据库迁移（`ALTER TABLE` 幂等，重复启动无副作用）。

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
- 只装 `requirements.txt`，不装 `requirements-dev.txt`，所以这条部署路径目前只覆盖 SQLite 后端；要用 PostgreSQL 后端得自己补 `pip install asyncpg` 并在 `.env` 里配好 `DB_BACKEND=postgres` / `PG_DSN`。

### 5. 本地开发 / 可选启用 PostgreSQL 后端

不需要 PostgreSQL 的话跳过这一节——`python3 run_server.py` 默认就是完整可用的 SQLite 部署。

要在本机（含 macOS）搭一套带 PostgreSQL 后端 + catalog_sync 事件流的开发环境，完整步骤见 [`docs/local_macos_setup.md`](docs/local_macos_setup.md)：装 PostgreSQL 16、**建库时必须带 `LC_COLLATE=C LC_CTYPE=C`**（PG 默认排序规则和 SQLite 的 `BINARY` 不同，会导致分页/搜索/导出顺序对不上）、Python 3.12 虚拟环境、`pip install -r requirements-dev.txt`、设好 `DB_BACKEND=postgres` / `PG_DSN` / `SCRAPER_INSTANCE_ID` 等环境变量、跑一遍 `tools/phase5_preflight.py` 体检、两个后端各跑一遍 golden 基线和 pytest。

> 该文档开头有一步"先 `git checkout` 到某迁移分支"已经过时——相关代码目前都已经在 `main` 分支上，不需要切分支。

`requirements.txt` 只含 SQLite 运行所需依赖（`aiosqlite`，不含任何 Postgres 驱动）；`requirements-dev.txt` 在此基础上加了 `asyncpg`（PostgreSQL 驱动）、`pytest`/`pytest-asyncio`/`httpx`（测试），以及显式重复声明的 `selectolax`/`lxml`/`dateparser`（这三个其实已经在 `requirements.txt` 里，重复声明是因为真的出过"venv 没装全导致解析器回归测试大批量静默 skip、变异测试全部放行"的事故，见该文件内注释）。

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
  - SQLite 后端走 FTS5 trigram 索引，PostgreSQL 后端走 `pg_trgm` GIN 索引，百万级数据下都是 5-50ms 量级（比 LIKE 全表扫快 ~1000 倍）
  - 短查询（<3 字符）自动 fallback 到 LIKE 路径保证正确性
- **分页**：keyset cursor 分页，单页上限 1000（近期从 200 上调）
- **选中删除**：勾选行 checkbox，点击"删除选中"（同时删除关联截图文件）
- **清空数据**：根据当前筛选条件智能删除
  - 选了批次 → 只删该批次数据
  - 输了搜索词 → 只删匹配数据
  - 无筛选 → 清空全部数据和截图（`DELETE /api/database`，不可撤销）

### 导出

导出不是一条路径，而是服务三类不同消费者的三套机制：

1. **UI 导出**（`server/api/export.py`，`GET /api/export/*`）：网页点"导出"按钮走的路径，弹窗选择格式（Excel/CSV）、字段（全选/仅价格库存/自定义勾选）、范围（当前批次 + 变动筛选）；支持流式导出，百万级数据不 OOM。导出列含**变体属性**（`color_name=X; size_name=Y`）/ 父体 ASIN / 变体 ASIN 列表；原 **EAN 列已下线**（amazon.com 实测 100% 为空，`ean_list` 已经不在可导出字段集合里），槽位由「变体属性」顶上。
2. **增量导出契约**（`server/api/export_incremental.py`，`GET /api/export/incremental`，**仅 PostgreSQL 后端**）：面向下游 catalog_sync（沃尔玛侧）的固定契约 v1，cursor+limit 分页，可选 `X-Export-Token` 请求头鉴权（`EXPORT_TOKEN`/`EXPORT_REQUIRE_TOKEN` 控制是否强制）。完整字段定义与不变量见 [`docs/incremental_export_contract.md`](docs/incremental_export_contract.md)。
3. **同步运维 API**（`server/api/sync.py`，`/api/v1/sync/*`，**仅 PostgreSQL 后端**）：读的是同一份事件流，但给的是内部原始事件形状 + 运维可观测性（relay 延迟、outbox 深度、保留期水位、`ack` 游标、强制裁剪通知）。完整契约见 [`docs/sync_contract.md`](docs/sync_contract.md)；面向 erpAPI 侧的业务端点（上传/状态/结果/失败明细）契约见 [`docs/erpapi_contract.md`](docs/erpapi_contract.md)。

后两者是 PostgreSQL-only：SQLite 部署下这些端点统一返回结构化 `503`（不是 404——404 容易被消费方误读成"暂无数据"，导致游标停滞）。

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
    dbfactory.py        # 存储后端开关：按 DB_BACKEND 惰性 import sqlite/postgres 实现
    database.py          # SQLite 实现（WAL + FTS5 + lease_epoch + 重试机制），PG 迁移未改动它一个字节
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
    phase5_preflight.py   # 环境体检：PG 连通性/建表/排序规则 + 路由顺序守卫
                          # （名字带 phase5 是历史遗留，工具本身仍在用：
                          #   check_route_order 是「增量导出端点必须排在
                          #   /api/export/{batch_name} 之前」这条不变量的
                          #   第二道守卫，见 server/api/export_incremental.py）
    phase5_compare.py     # 两套系统同一批 ASIN 的采集内容比对
                          # （tests/test_phase5_compare.py 等在用它的 classify/compare）
    desc_glue_check.py    # 单个解析 bug（<br> 不产生分隔符）的修复验证工具
  docs/
    erpapi_contract.md            # erpAPI 侧业务端点契约
    incremental_export_contract.md # 增量导出契约 v1
    sync_contract.md               # /api/v1/sync/* 运维契约
    local_macos_setup.md           # 本机 macOS 开发环境搭建（含 PostgreSQL 后端）
    phase5_runbook.md              # PostgreSQL 生产切换运行手册
  tests/
    test_*.py            # 单元/集成测试（解析质量、批次/结果 API、错误码、golden 相关守卫等）
    pgdb/                 # PostgreSQL 存储层测试
    golden/                # 字节级 HTTP 行为回归基线（SQLite 迁移到 PostgreSQL 时用于防回归）
  data/
    scraper.db            # SQLite 数据库文件（默认后端）
    scraper.db-wal/-shm    # WAL 日志 + 共享内存
    exports/                # 导出文件 + 临时文件（自动清理）
    schedules/              # 定时任务 ASIN 文件
  deploy/
    setup.sh              # Server 部署脚本（SQLite 后端，不含 worker）
    server.service         # systemd 服务配置
  run_server.py          # Server 启动入口
  run_worker.py          # Worker 启动入口
  .env.example           # 环境变量样例（存储后端/代理/邮编/导出鉴权等）
```

## 数据库

存储后端由 `DB_BACKEND` 环境变量选择（`common/dbfactory.py`），未设置时为 `sqlite`；两个后端对外暴露的方法签名一一对应，行为等价性由 `tests/golden/` 的 64 步字节级基线 + 两个后端各约 700 条 pytest 用例持续验证。

### SQLite（默认后端）

| 表 | 说明 |
|---|---|
| `batches` | 批次元数据 + callback 状态 + external_id |
| `batch_asins` | 批次-ASIN 多对多映射 |
| `asin_data` | ASIN 数据（UNIQUE，覆盖更新） |
| `asin_data_fts` | FTS5 trigram 全文索引（external content） |
| `asin_changes` | 变动检测历史（价格/库存/标题/新增） |
| `tasks` | 采集任务队列（含 `lease_epoch` + `auto_retry_count` + 卖家发现任务的 `task_type`/`task_meta`） |
| `screenshots` | 截图追踪 |
| `seller_discoveries` | 卖家店铺发现结果（`(batch_id, seller_id, asin)` 主键） |

`asin_data` 包含字段：ASIN / 标题 / 品牌 / 价格 / 库存 / 评分 / 评论数 / 卖家店铺 ID / 卖家名 / 父 ASIN / 变体属性 / 类目 / 尺寸 / 重量 / 制造商 / 排名 / ...

**写连接 / 只读连接池物理隔离**：写连接只服务 worker 热路径（`pull_tasks`/`accept_results_batch`），仪表盘/导出/聚合查询走独立的只读连接池（默认 3 条，各自 `PRAGMA query_only=ON` + 私有 16MB cache），避免管理后台的重读把 worker 的写操作堵在锁上。

**关键 PRAGMA**：`journal_mode=WAL`、`synchronous=NORMAL`、`cache_size=-65536`（64MB）、`mmap_size=268435456`（256MB）、`temp_store=MEMORY`、`journal_size_limit=67108864`（64MB）。`PRAGMA optimize` 不在启动时同步跑（大库上会阻塞启动），改为监听端口后异步执行；后台每 120s 做一次 `wal_checkpoint(TRUNCATE)`。

### PostgreSQL（可选后端，`DB_BACKEND=postgres`）

`public` schema 里是与 SQLite 对应的同名表（`batches`/`batch_asins`/`asin_data`/`asin_changes`/`tasks`/`screenshots`/`seller_discoveries`），搜索用 `pg_trgm` GIN 表达式索引替代 FTS5。

`scraper` schema 额外承载 catalog_sync 事件流：

| 表 | 说明 |
|---|---|
| `scraper.scrape_outbox` | 写路径暂存表：业务写和事件写在同一个事务里提交 |
| `scraper.scrape_outbox_dead` | 死信表：relay 无法处理的行原样保留，供人工排查/重放，绝不静默丢弃 |
| `scraper.scrape_events` | 正式的、按 `seq` **分区**的追加事件日志，下游消费者按游标顺序拉取 |
| `scraper.sync_meta` | 部署标识（`gen`/`instance_id`）、`ack_seq`、强制裁剪记录等运维状态 |

**事件流机制**：每一次终态写入（结果入库、任务终态失败、租约失效）都会在**同一个数据库事务**里往 `scrape_outbox` insert 一行；一个通过 `pg_try_advisory_lock` 选举出的**单例** relay 协程每秒左右把 outbox 排干，为每一行分配单调递增的 `seq` 并写入分区好的 `scrape_events`，drop-outbox 和 insert-events 在同一事务里提交。这样设计是因为直接对并发写入的表做 `seq > X` 轮询，行有可能乱序提交导致游标永久跳过某些行；outbox 把"行已存在"和"行已被安全排序"拆成了两个独立步骤。

**保留期（retention）**：只整分区 `DROP`，从最老分区往后推，遇到第一个不能删的分区就停手（不会在分区中间留洞）。可删除下界 = `max(硬下限, min(时间下限, ack 水位 − slack))`——`ack_seq` 是下游通过 `POST /api/v1/sync/ack` 确认的、已经落盘的最高 `seq`。磁盘紧张时硬下限可以越过 `ack` 强制裁剪未确认数据，这种情况会记进 `sync_meta` 的 `forced_prune_log`（持久闩锁），下游需要显式 `POST /ack-prune` 才能清掉这条记录——这条设计是为了不让"磁盘紧张时的数据丢失"被悄悄吞掉。

**当前状态**：存储层与事件流已经过真机验证（含真实代理/真实 Amazon 页面采集、新旧系统同批 ASIN 内容比对），但生产切换尚未执行——目前仍是 SQLite 提供生产服务。

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
│   扫描 status='failed' AND auto_retry_count < 2 AND 失败>5min  │
│   重置为 pending，再走 2 轮（每轮 = 层 1+2 完整循环）          │
├────────────────────────────────────────────────────────────────┤
│ 层 4：reclaim_dead_worker_tasks（每 30s 周期任务）              │
│   扫描 status='processing' AND（心跳超 2min 或 任务超 10min）  │
│   回收为 pending、lease_epoch++（迟到结果失效）                 │
└────────────────────────────────────────────────────────────────┘
```

正常错误（network / timeout / blocked / captcha / ...）最大尝试 = `MAX_RETRIES × MAX_RETRIES × (1 + auto_retry_cycles) = 3 × 3 × 3 = 27 次`；`variant_offset` 这类首次失败即终态的错误只会尝试 1 次。

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

**FTS5 全文搜索**：`asin_data_fts` 虚拟表（trigram tokenizer + external content + detail=none），配套 3 个触发器（AI/AD/AU）自动同步主表变化；搜索查询走 `UNION` 形态让每个 LIKE 都命中 trigram L1 索引；短查询（<3 字符）fallback 到主表 LIKE。实测：原 LIKE 全表扫描 46 秒 → FTS UNION 5-50 毫秒（~1000× 加速）。

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

- **`tests/golden/`**：字节级 HTTP 行为回归基线（`tests/golden/samples/sqlite_baseline.json`，64 步覆盖上传/拉任务/提交/分页/搜索/导出/删除等完整生命周期），用于保证存储层重写（SQLite ↔ PostgreSQL）不改变对外可观察行为。用法见 [`tests/golden/README.md`](tests/golden/README.md)：`python -m tests.golden.run {selfcheck|record|verify}`。
- **`tests/pgdb/`**：PostgreSQL 存储层专项测试（批次/任务/结果读写、并发、保留期、relay/事件流接线等）。
- 其余测试覆盖解析质量、批次名冲突语义、搜索转义、游标分页活性、错误码闭集、卖家 API 等具体行为契约。
- `tools/smoke_local.py` 是一个不依赖真实代理/Amazon 的端到端冒烟脚本（上传 → 拉任务 → 提交结果 → 查询 → 事件流契约校验），适合部署后快速验证：`python3 tools/smoke_local.py`。

## 性能基线

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
| FTS5 索引开销 | ~90 MB / 29 万 ASIN |

**读写解耦前后**（导出风暴下：3 路全量 CSV 导出 + 仪表盘轮询并发打）：

| 指标 | 解耦前 | 解耦后 |
|---|---:|---:|
| `pull_tasks` 持锁 max | **288,000 ms** | **272 ms** |
| `pull_tasks` 等锁 max | **259,000 ms** | **963 ms** |
| 8899 并发连接堆积 | 42-43 | 4 |
| 冷启动耗时（2.4GB 库）| ~150 s | ~10 s |

## License

Private use.
