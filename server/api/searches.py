"""F-010 关键词搜索采集（4 个端点）—— 与 `sellers.py` 逐条同构。

    POST /api/search-batches
    GET  /api/search-batches/{batch_id}/progress
    GET  /api/search-batches/{batch_id}/discoveries
    POST /api/tasks/search-result          （worker 提交发现结果）

------------------------------------------------------------------------
为什么是"发现 + 派发"两阶段，而不是一次采完
------------------------------------------------------------------------
搜索结果页上能拿到的只有 ASIN / 标题 / 价格 / 缩略图 —— 库存、配送、
BuyBox、卖家、类目、变体一个都没有，而且价格还常常是"起价"。所以关键词
采集必须是两段：**发现**阶段只负责把 ASIN 翻全（这是唯一需要"按关键词"
做的事），**详情**阶段完全复用既有的 ASIN 采集队列（`task_type='asin'`），
一条代码路径都不必重写。

这也正是 F-009 卖家采集的形状，两者共用同一套 `tasks` 表、同一批 worker、
同一条重试/租约/回收通道。差异只有"种子是什么"和"翻页 URL 怎么拼"。

------------------------------------------------------------------------
承重约束（与 sellers.py 同款，逐条对齐）
------------------------------------------------------------------------

1. **模块级可变全局一个都不搬**：`db` / `_runtime_settings` 留在
   `server/app.py`，这里一律 `_srv().xxx`。from-import 会把值快照下来，
   而黄金夹具按名字给 `server.app` 打补丁、PG 夹具还
   `monkeypatch.setattr(srv, "db", pgdb)`。

2. **router 光秃**（`APIRouter()`，不带 `tags=` / `prefix=`）：
   `/openapi.json` 是黄金基线的一步，整份 schema 里没有 `tags` 键。

3. **筛选参数的校验只有一处** —— `common.core.searchurl.normalize_search_params`。
   这里不重复实现任何一条规则（价格区间是否颠倒、配送方式该站点支不支持、
   翻页上限……），只负责把 `ValueError` 翻成 400。worker 拼 URL 用的是同一个
   模块，所以"server 收下了但 worker 没拼进去"这种静默失效不可能发生。

4. `/api/tasks/search-result` 是**静态路径**，`/api/tasks/` 下没有
   `/api/tasks/{x}` catch-all，所以放在本模块不影响路由匹配
   （`/api/tasks/seller-result` 同理，见 `sellers.py` 承重约束 1）。

5. `api_search_discoveries` 的 f-string 拼 WHERE 与 `db.read()` 照抄
   `sellers.py` 的同名端点：走只读池而不是裸 `db._db`，`?` 占位符由
   `common/pgdb` 侧翻译。
   ⚠ 唯一的额外之处：`rank` 是 PG 保留字，SELECT 列表里必须写成 `"rank"`。
     不加引号在 PG 16 上是 syntax error，而 SQLite 两种写法都吃。
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from common import config
from common.core import searchurl


def _srv():
    from server import app as _s
    return _s


router = APIRouter()


# ==================== F-010: 关键词搜索采集 ====================


@router.post("/api/search-batches")
async def api_create_search_batch(request: Request):
    """按关键词创建一个 keyword_discovery 批次（JSON）。

    Body（除 `keywords` 外全部可选）:

    | 字段 | 默认 | 说明 |
    |---|---|---|
    | `keywords` | — | 关键词数组，或用换行/逗号分隔的一个字符串 |
    | `min_price` / `max_price` | `null` | 价格区间（站点货币，支持只给一端） |
    | `delivery` | `null` | 配送/履约筛选，取值见 `GET /api/search-options` |
    | `sort` | `null` | 排序：relevance / price_asc / price_desc / newest / review_rank / featured |
    | `rh_extra` | `null` | 逃生口：原样拼进 `rh=` 的 refinement 串 |
    | `max_pages` | 7 | 每个关键词翻几页（上限 20） |
    | `include_sponsored` | `false` | 是否保留广告位结果 |
    | `domain` | `www.amazon.com` | 站点 |
    | `discover_mode` | `with_detail` | `discover_only` 只翻 ASIN；`with_detail` 顺带派发详情采集 |
    | `zip_code` | 服务端默认 | 详情阶段的配送邮编 |
    | `needs_screenshot` | `false` | 详情阶段是否截图（发现任务本身不截图） |
    | `batch_name` | 自动生成 `keywords_<ts>` | 同名会**并入**已存在的批次（见下） |

    **同名批次的语义与 `/api/upload` 不同，是有意的**：这里同名 = 往已有批次
    追加关键词（`ON CONFLICT DO NOTHING` + 取回已存在的 id），不是 409。
    理由是关键词采集天然是增量的 —— 先跑 5 个词看看效果、再补 20 个词进同一批
    是常规用法；而 `/api/upload` 的 409 是为了防"两个调用方撞名后数据悄悄混在
    一起"，那条场景在这里由调用方显式给出 batch_name 来表达意图。
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")

    # ---- 关键词：数组或分隔字符串都收 ----
    raw_kws = body.get("keywords")
    if isinstance(raw_kws, str):
        raw_list: List[Any] = [k for k in raw_kws.replace(",", "\n").split("\n")]
    elif isinstance(raw_kws, list):
        raw_list = raw_kws
    else:
        raise HTTPException(400, "keywords 必填，且必须是数组或换行/逗号分隔的字符串")

    keywords: List[str] = []
    seen = set()
    for k in raw_list:
        kw = searchurl.normalize_keyword(k)
        # 大小写不敏感去重：与 db.create_search_batch 里那条是同一条规则。
        # 这里也做一遍是为了让响应里的 total_keywords 说的是实话
        # （不做的话响应会把 "mouse"/"Mouse" 报成 2 个，落库只有 1 个）。
        if kw and kw.lower() not in seen:
            keywords.append(kw)
            seen.add(kw.lower())
    if not keywords:
        raise HTTPException(400, "未识别到任何有效关键词")

    # ---- 筛选参数：唯一校验点，见承重约束 3 ----
    try:
        search_params = searchurl.normalize_search_params(body)
    except ValueError as e:
        raise HTTPException(400, str(e))

    discover_mode = body.get("discover_mode") or "with_detail"
    if discover_mode not in ("discover_only", "with_detail"):
        raise HTTPException(400, f"非法 discover_mode: {discover_mode}")

    _s = _srv()
    zip_raw = body.get("zip_code")
    if zip_raw:
        zc = _s._normalize_zip(zip_raw)
        if not zc:
            raise HTTPException(400, f"非法邮编: {zip_raw!r}")
    else:
        zc = _s._runtime_settings.get("zip_code", config.DEFAULT_ZIP_CODE)

    batch_name = body.get("batch_name")
    if not batch_name:
        # 批次名的唯一构造点（P4.7），与 sellers.py 同约定：走 _srv()
        batch_name = _s._batch_name("keywords")

    needs_screenshot = bool(body.get("needs_screenshot", False))

    batch_id, inserted = await _s.db.create_search_batch(
        name=batch_name,
        keywords=keywords,
        search_params=search_params,
        discover_mode=discover_mode,
        zip_code=zc,
        needs_screenshot=needs_screenshot,
    )
    if not batch_id:
        raise HTTPException(500, "创建关键词批次失败")

    return {
        "batch_id": batch_id,
        "batch_name": batch_name,
        "discover_mode": discover_mode,
        "zip_code": zc,
        "total_keywords": len(keywords),
        "inserted_tasks": inserted,
        "search_params": search_params,
        "status_url": f"/api/search-batches/{batch_id}/progress",
    }


@router.get("/api/search-options")
async def api_search_options(domain: str = searchurl.DEFAULT_DOMAIN):
    """某站点当前可用的筛选取值。UI 的下拉框与调用方的自检都读它。

    存在的理由：`delivery` 背后是 Amazon 的 refinement 节点 ID，**按站点不同、
    且会随 Amazon 改版变化**（见 `common/core/searchurl` 头注）。把可用取值做成
    端点而不是写死在前端，改配置（`SEARCH_DELIVERY_FILTERS`）就能立刻生效，
    不必发版；也让"这个站点为什么没有 prime 选项"这个问题有个可查的答案。
    """
    domain = (domain or searchurl.DEFAULT_DOMAIN).strip().lower()
    if domain not in searchurl.SUPPORTED_DOMAINS:
        raise HTTPException(
            400, f"不支持的站点: {domain}；可选: {', '.join(searchurl.SUPPORTED_DOMAINS)}")
    return {
        "domain": domain,
        "domains": list(searchurl.SUPPORTED_DOMAINS),
        "delivery": searchurl.delivery_choices(domain),
        "sort": sorted(searchurl.SORT_CHOICES),
        "max_pages_cap": searchurl.MAX_PAGES_CAP,
        "default_max_pages": searchurl.DEFAULT_MAX_PAGES,
        "max_keyword_len": searchurl.MAX_KEYWORD_LEN,
    }


@router.get("/api/search-batches/{batch_id}/progress")
async def api_search_batch_progress(batch_id: int):
    """keyword_discovery 批次专属进度端点：discover + detail + 已发现 ASIN 数。"""
    return await _srv().db.get_search_batch_progress(batch_id)


@router.get("/api/search-batches/{batch_id}/discoveries")
async def api_search_discoveries(batch_id: int,
                                 keyword: Optional[str] = None,
                                 include_sponsored: bool = True,
                                 limit: int = 200,
                                 offset: int = 0):
    """列出某批次发现的 ASIN（可按关键词过滤）。

    默认按 `(page_no, rank)` 升序 —— 也就是**搜索结果里的原始顺序**。
    这一点与 `sellers.py` 的同名端点（按 `discovered_at DESC`）有意不同：
    关键词采集的核心产出之一就是名次，按时间排会把它打乱，而 `discovered_at`
    在同一次提交里几乎全部相同（同一个事务），根本排不出稳定顺序。
    """
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    where = ["batch_id = ?"]
    params: List[Any] = [batch_id]
    if keyword:
        kw = searchurl.normalize_keyword(keyword)
        if not kw:
            raise HTTPException(400, "keyword 过滤值为空")
        where.append("keyword = ?")
        params.append(kw)
    if not include_sponsored:
        where.append("is_sponsored = 0")
    # "rank" 必须带引号：PG 保留字（承重约束 5）
    sql = (
        'SELECT keyword, asin, list_title, list_price, list_image, '
        '       page_no, "rank", is_sponsored, discovered_at '
        f"FROM search_discoveries WHERE {' AND '.join(where)} "
        'ORDER BY keyword ASC, page_no ASC, "rank" ASC, asin ASC LIMIT ? OFFSET ?'
    )
    params.extend([limit, offset])
    rows = []
    async with _srv().db.read() as rc, rc.execute(sql, params) as c:
        async for r in c:
            rows.append(dict(r))
    return {"items": rows, "limit": limit, "offset": offset}


@router.post("/api/tasks/search-result")
async def api_submit_search_result(request: Request):
    """接收 worker 的 discover_search 任务结果（F-010）。

    Payload: {task_id, batch_id, worker_id, lease_epoch, keyword, items, meta}
    """
    body = await request.json()
    task_id = body.get("task_id")
    batch_id = body.get("batch_id")
    worker_id = body.get("worker_id", "")
    lease_epoch = body.get("lease_epoch", 0)
    keyword = body.get("keyword") or ""
    items = body.get("items") or []
    meta = body.get("meta") or {}

    if not task_id or not str(keyword).strip():
        raise HTTPException(400, "task_id 和 keyword 必填")

    result = await _srv().db.accept_search_discovery_result(
        task_id=task_id,
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        batch_id=batch_id,
        keyword=keyword,
        items=items,
        meta=meta,
    )
    if worker_id in _srv()._worker_registry and result.get("accepted"):
        _srv()._worker_registry[worker_id]["results_submitted"] += 1
    return result
