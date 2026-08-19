"""
Amazon ASIN 采集系统 v4 - 配置
server 和 worker 共用的配置项
"""
import os

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.isfile(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _key, _, _val = _line.partition("=")
                    os.environ.setdefault(_key.strip(), _val.strip())

# ============================================================
# 基础路径
# ============================================================
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "scraper.db")

# ============================================================
# 存储后端
# ============================================================
# "postgres"（默认，未设置时也是它）-> common.pgdb.Database
# "sqlite"                          -> common.database.Database
#
# 迁移已完成，PostgreSQL 是**正式后端**，所以未设置时走它。
# SQLite 那条路径暂时保留作为回滚兜底，但不再是默认，也不再是新部署的选项。
# 选择逻辑在 common/dbfactory.py。
DB_BACKEND = os.environ.get("DB_BACKEND", "postgres").strip().lower()

# PostgreSQL 连接串。仅在 DB_BACKEND=postgres 时使用。
PG_DSN = os.environ.get("PG_DSN", "postgresql://scraper:scraper@127.0.0.1/scraper_dev")
# 读侧连接池大小（写侧是**一条专用连接**，见 common/pgdb/pool.py 的决策 D-2）。
PG_POOL_MIN = int(os.environ.get("PG_POOL_MIN", "2"))
PG_POOL_MAX = int(os.environ.get("PG_POOL_MAX", "10"))
# 单条语句超时（秒）。导出会长时间占用连接，别设得太小。
PG_COMMAND_TIMEOUT = float(os.environ.get("PG_COMMAND_TIMEOUT", "60"))
EXPORT_DIR = os.path.join(PROJECT_DIR, "data", "exports")
SCREENSHOT_DIR = os.path.join(PROJECT_DIR, "server", "static", "screenshots")
TEMPLATE_DIR = os.path.join(PROJECT_DIR, "server", "templates")
STATIC_DIR = os.path.join(PROJECT_DIR, "server", "static")

# ============================================================
# 服务器配置
# ============================================================
SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8899"))

# ============================================================
# 采集配置
# ============================================================
DEFAULT_ZIP_CODE = os.environ.get("DEFAULT_ZIP_CODE", "10001")
MAX_CLIENTS = 32
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
TASK_TIMEOUT_MINUTES = 10  # 硬超时兜底（liveness safety net），主回收靠心跳感知
SESSION_ROTATE_EVERY = 1000
# 会话池化后并发初始化闸门：冷启动/大规模轮换时最多同时初始化多少个 session，
# 平滑代理 ~5 QPS CONNECT 突发。稳态下 session 复用、初始化很少，几乎无副作用。
SESSION_INIT_CONCURRENCY = int(os.environ.get("SESSION_INIT_CONCURRENCY", "3"))
# Tier 2a：邮编验证模式
#   "on_fetch"（默认）—— 不发独立验证 GET；切邮编只 POST，采完商品后用商品页
#                        HTML 自身的 glow 挂件判定邮编是否生效
#   "standalone"       —— 旧行为：切邮编后单发一次首页 GET 验证是否生效
# per-ASIN 邮编批次下，on_fetch 每个 ASIN 省一次代理请求（验证 GET）。真机 A/B
# 已验证 on_fetch 数据准确、未生效能被 glow 捕获并重试、zip 失败率更低，故设为默认；
# 需回退旧行为时设 ZIP_VERIFY_MODE=standalone。
ZIP_VERIFY_MODE = os.environ.get("ZIP_VERIFY_MODE", "on_fetch")
# on_fetch 下「商品页邮编未生效」的处理：先在同一 session 上重发邮编 POST（代价远
# 小于冷轮换），最多重发 N 次仍未生效才回退到冷轮换换 IP。冷轮换会关 session +
# report_blocked，喂给代理 429/被封计数；重发 POST 则复用 IP 预算，更省。
ZIP_REPOST_MAX_TRIES = int(os.environ.get("ZIP_REPOST_MAX_TRIES", "2"))
# 软降级页兜底：亚马逊对可疑请求（经 TPS 轮换代理的数据中心 IP）有时只返回标题/
# 价格/图片，删掉个性化区块（配送 ETA + 变体报价）。商品明明可售（FBA/In Stock）
# 却拿不到配送区时，判为降级页 → 轮换换 IP 重采，最多重采 N 次仍无配送才接受 N/A。
# 每次命中多花代理请求；能否恢复取决于换 IP 后是否仍降级（真机验证）。设 0 关闭。
DELIVERY_RETRY_MAX = int(os.environ.get("DELIVERY_RETRY_MAX", "2"))
# 降级页样本采集（排查用，默认关）：DUMP_DEGRADED_HTML=1 时，把重采用尽后仍判定为
# 降级页（可售但无配送区）的原始 HTML 另存到 worker/degraded_dump（截图流程不碰它），
# 供离线分析、收紧检测条件。DEGRADED_DUMP_MAX 限制最多存多少张，防塞满磁盘。
DUMP_DEGRADED_HTML = os.environ.get("DUMP_DEGRADED_HTML", "0") == "1"
DEGRADED_DUMP_MAX = int(os.environ.get("DEGRADED_DUMP_MAX", "300"))

# 变体自动展开安全阀：单个产品的候选同族变体数超过此值，视为巨型/定制类家族
# （如定制尺寸围栏网，单族可达数千），跳过该产品的自动展开，防止一个种子炸成几千任务。
VARIANT_EXPAND_FAMILY_CAP = 10

# 令牌桶限流
TOKEN_BUCKET_RATE = 64.0

# ============================================================
# 自适应并发控制
# ============================================================
INITIAL_CONCURRENCY = 16
MIN_CONCURRENCY = 16
MAX_CONCURRENCY = 32

PROXY_BANDWIDTH_MBPS = 0
ADJUST_INTERVAL_S = 3
TARGET_LATENCY_S = 5.0
MAX_LATENCY_S = 8.0
TARGET_SUCCESS_RATE = 0.95
MIN_SUCCESS_RATE = 0.85
BLOCK_RATE_THRESHOLD = 0.05
BANDWIDTH_SOFT_CAP = 0.80
GLOBAL_MAX_CONCURRENCY = 1000
GLOBAL_MAX_QPS = 1000.0

TASK_QUEUE_SIZE = 64
TASK_PREFETCH_THRESHOLD = 0.5

# ============================================================
# 代理配置
# ============================================================
# TPS 模式（每次请求自动换 IP）
PROXY_URL = os.environ.get("PROXY_URL", "")

# ============================================================
# 浏览器指纹
# ============================================================
BROWSER_PROFILES = [
    {
        "impersonate": "chrome120",
        "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ],
    },
    {
        "impersonate": "chrome123",
        "sec_ch_ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        ],
    },
    {
        "impersonate": "chrome124",
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ],
    },
    {
        "impersonate": "chrome131",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ],
    },
    {
        "impersonate": "chrome136",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        ],
    },
]

USER_AGENTS = []
for _p in BROWSER_PROFILES:
    USER_AGENTS.extend(_p["user_agents"])

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# ============================================================
# 导出配置
# ============================================================
HEADER_MAP = {
    "crawl_time": "商品采集时间",
    "zip_code": "配送邮编",
    "product_url": "产品链接",
    "asin": "ASIN (商品ID)",
    "title": "商品标题",
    "original_price": "商品原价",
    "current_price": "当前价格",
    "buybox_price": "BuyBox 价格",
    "buybox_shipping": "BuyBox 运费",
    "is_fba": "是否 FBA 发货",
    "stock_count": "库存数量",
    "stock_status": "库存状态",
    "delivery_date": "配送到达时间",
    "delivery_time": "配送时长",
    "brand": "品牌",
    "model_number": "产品型号",
    "country_of_origin": "原产国",
    "is_customized": "是否为定制产品",
    "best_sellers_rank": "畅销排名",
    "upc_list": "UPC 列表",
    "ean_list": "EAN 列表",
    "package_dimensions": "包装尺寸",
    "package_weight": "包装重量",
    "item_dimensions": "商品本体尺寸",
    "item_weight": "商品本体重量",
    "variant_attributes": "变体属性",
    "parent_asin": "父体 ASIN",
    "variation_asins": "变体 ASIN 列表",
    "root_category_id": "根类目 ID",
    "category_ids": "类目 ID 链",
    "category_tree": "类目路径树",
    "bullet_points": "五点描述",
    "image_urls": "商品图片链接",
    "site": "站点",
    "manufacturer": "制造商",
    "part_number": "部件编号",
    "first_available_date": "上架时间",
    "long_description": "长描述",
    "product_type": "商品类型",
    "total_price": "总价",
    "rating": "商品评分",
    "review_count": "评论数",
    "seller_id": "卖家店铺ID",
    "seller_name": "卖家店铺名",
    # 2026-08：Amazon 把商品标题拆成两段，后半段（Title Differentiators）
    # 既拼进「商品标题」也单独成列。少了这一行，导出表头会直接写
    # 英文键名 `subtitle`（_get_export_headers 的 `HEADER_MAP.get(f, f)` 兜底）。
    "subtitle": "副标题",
}

EXPORT_COLUMN_ORDER = [
    "ASIN (商品ID)", "产品链接", "商品标题", "品牌",
    "商品评分", "评论数",
    "商品原价", "当前价格", "BuyBox 价格", "BuyBox 运费", "总价", "是否 FBA 发货",
    "卖家店铺名", "卖家店铺ID",
    "库存数量", "库存状态", "配送到达时间", "配送时长",
    "商品图片链接", "五点描述", "长描述", "是否为定制产品",
    "UPC 列表", "类目路径树",
    "包装尺寸", "包装重量", "商品本体尺寸", "商品本体重量",
    "商品类型", "制造商", "产品型号", "部件编号", "原产国",
    "EAN 列表", "父体 ASIN", "变体 ASIN 列表", "畅销排名",
    "根类目 ID", "类目 ID 链", "上架时间",
    "站点", "配送邮编", "商品采集时间",
]

# ============================================================
# 启动校验
# ============================================================
assert MIN_CONCURRENCY <= INITIAL_CONCURRENCY <= MAX_CONCURRENCY
assert TARGET_LATENCY_S < MAX_LATENCY_S
