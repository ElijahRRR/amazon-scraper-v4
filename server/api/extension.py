"""浏览器插件对接（3 个端点）—— F-011。

    GET  /api/extension/ping
    POST /api/extension/collect
    POST /api/extension/resolve-sellers

------------------------------------------------------------------------
为什么插件要单独一组端点，而不是直接用 /api/batches
------------------------------------------------------------------------
差别只有一条，但那一条是整个翻页采集能不能成立的前提：**同名批次要能追加**。

插件在列表页翻页采集时，是**一页一推**的（一页读完就推，不攒到最后 ——
攒着的话中途关标签页就全丢了）。这些页必须落进**同一个批次**，否则一次
"翻 10 页" 会在控制台里变成 10 个批次，导出、进度、回调全部没法看。

而 `POST /api/batches` 与 `POST /api/upload` 的同名语义是 **409 Conflict**，
那是有意为之的（防"两个调用方撞名后数据悄悄混在一起"，见 README）。
不能为了插件把那条语义改掉 —— 已经有下游依赖它。所以插件走自己这条：
同名 = 追加，且这个意图由插件**显式**给出批次名来表达，不存在误撞。

------------------------------------------------------------------------
实现上刻意**不新增任何 DB 方法**
------------------------------------------------------------------------
三个端点全部由既有的公开方法拼出来：

    create_batch_if_absent + create_tasks   -> 商品批次（追加语义天然成立）
    create_seller_batch                     -> 卖家批次（它自己就是 ON CONFLICT 取回已存在 id）

好处不只是少写代码：`common/pgdb` 对 `common/database.py` 有逐方法的契约
比对（PUBLIC_API 双向断言 + 签名比对），每加一个方法就要两个后端同步加、
再补一轮双跑测试。这里没有任何一件事需要新语义，那就一件都别加。

------------------------------------------------------------------------
商品批次与卖家批次为什么是**两个**批次
------------------------------------------------------------------------
详情队列里勾了"采集该卖家全部商品"时，会同时产生两种任务。塞进一个批次
是可行的（F-009 的 `accept_seller_discovery_result` 就往同一批次里插详情
任务），但对插件这个场景不合适：

  * 那两批东西的**规模差三个数量级** —— 用户勾的 5 个商品 vs 5 家店的全部
    在售商品。混在一起之后批次进度条永远卡在 1%，看不出自己勾的那 5 个采完没有；
  * 卖家批次要 `batch_type='seller_discovery'` 才能用专属进度端点，
    而商品批次是 `'asin'`。一个批次只有一个 batch_type。

所以拆成 `<name>` 与 `<name>_sellers` 两个，各自有各自的进度端点。

------------------------------------------------------------------------
站点（F-012 blocker 3）：只认**显式**的 `marketplace` 字段
------------------------------------------------------------------------
`collect` / `resolve-sellers` 的请求体都可以带 `marketplace`（`amazon.ca` /
`www.amazon.ca` / `https://www.amazon.ca/` 都收），缺省 = 美国站。

* **不从 `page_url` 推导**。v1.1.0 及更早的插件不发这个字段，它们推过来的
  一律按美国站处理 —— 与改造前逐字节一致。从 `page_url` 推导看着更聪明，
  但老插件的队列提交根本不带 `page_url`（只有列表/翻页才带），于是同一个
  老插件的两种推送会落到两个站点上，比"一律美国站"更难解释。
* 不在注册表里的站点（`amazon.de` 等）一律 **400**，绝不静默回落到美国站：
  插件以为在采德国站、实际采的是美国站，而任何一侧都不会响。插件把
  服务端的 detail 原样弹给用户，所以站点白名单只在服务端这一份。
* 邮编按站点校验与取默认（`_normalize_zip(zip, spec.id)` /
  `_site_default_zip(spec)`），美国站的报错文案与默认值逐字节不变。
* **一个批次只属于一个站点**。`tasks` 的唯一键是 `(batch_id, asin)`，
  下游（进度、导出、截图目录、回调）都以批次为单位、按任务行的站点取数据；
  同名追加如果把两个站点混进一个批次，这四处都会变成说不清的东西。
  所以追加时若同名批次里已有**另一个站点**的任务 -> **409**，什么都不写。
  商品批次 `<name>` 与卖家批次 `<name>_sellers` 都查，而且**先查完再写**：
  不能出现"商品批次写进去了、卖家批次被拒"的半截推送。

------------------------------------------------------------------------
承重约束（与 sellers.py / searches.py 同款）
------------------------------------------------------------------------
1. 模块级可变全局一个都不搬，一律 `_srv().xxx`。
2. router 光秃（`APIRouter()`，不带 tags/prefix）—— `/openapi.json` 逐字节钉死。
3. 路径全在 `/api/extension/` 这个新前缀下，与既有路由零交集。
4. **鉴权姿态与 `/api/upload` 一致**（都不在 `server/authz.py` 的 `_PROTECTED`
   名单里）：这些端点只**创建**采集任务，不删数据、不改设置。
   把它们塞进保护名单会让插件在配了 ADMIN_TOKEN 的部署上默认不可用，
   而真正要命的那几扇门（清库/删批次/删结果）已经锁着了。
   插件仍然会带上用户填的 `X-Admin-Token`，所以将来要收紧也不必改插件。

------------------------------------------------------------------------
⚠ 这里**故意没有加 CORS**，别"顺手补上"
------------------------------------------------------------------------
第一反应会觉得插件跨源调用需要服务端放 CORS 头。不需要，而且加了有害：

* **不需要**：插件的所有请求都从 service worker 发出（见 `background.js` 头注），
  走的是扩展自己的源。MV3 下只要用户在选项页授权过 `host_permissions`，
  扩展的 fetch **不受 CORS 约束** —— 那正是 `optional_host_permissions`
  这套机制存在的意义。content script 一次都不直接调服务端。

* **有害**：本服务的采集端点默认是无鉴权的（`/api/upload` 一直如此）。
  今天没有 CORS 头，浏览器对带 `Content-Type: application/json` 的跨源
  POST 会先发 preflight，拿不到放行头就**根本不发出真正的请求** ——
  于是用户随便逛的任意网站都无法往他的采集服务器里塞任务。
  加一条 `allow_origins=["*"]` 会把这道天然屏障拆掉，换来的是零收益。

要让浏览器里的第三方页面直接调用，那是另一件事（需要先给这些端点配鉴权），
不在本模块范围内。
"""

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from common import config
from common.core import marketplace as _marketplace


def _srv():
    from server import app as _s
    return _s


def _site_of(body: Dict[str, Any]) -> "_marketplace.MarketplaceSpec":
    """请求体里的 `marketplace` -> 注册表 spec。缺省 / 空串 -> 美国站。

    解析与报错委托给 `server/app.py:_resolve_marketplace`（ASIN 清单入口的
    唯一站点解析点，不在注册表里 -> 400）。这里只多挡一种形状：非字符串。
    不挡的话 `["amazon.ca"]` 会被 `str()` 成 `"['amazon.ca']"` 再报"不支持的
    站点"，报错说的是一个调用方从没发过的值。
    """
    raw = body.get("marketplace")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(400, f"marketplace 必须是字符串，收到 {type(raw).__name__}")
    return _srv()._resolve_marketplace(raw)


async def _reject_cross_site_append(name: str, spec) -> None:
    """同名批次已属于另一个站点 -> 409（理由见模块头注「站点」一节）。

    判定本身在 ``server/app.py::_reject_cross_site_batch``：关键词批次与卖家
    文件上传也是「同名 = 合并」，三个入口共用同一条规则。
    """
    await _srv()._reject_cross_site_batch(name, spec)


router = APIRouter()

#: 单次推送的 ASIN 上限。翻页采集是一页一推（一页最多 ~60 个），
#: 详情队列靠人手点，都远远够用；给上限是为了让一个手滑的脚本
#: 打不垮写连接（create_tasks 是一次事务写）。
MAX_ASINS_PER_PUSH = 2000

#: 单次推送的卖家上限。整店发现任务一个能炸出几万个 ASIN，比 ASIN 危险得多。
MAX_SELLERS_PER_PUSH = 50

#: resolve-sellers 单次查询的 ASIN 上限（拼 `?,?,?` 的 IN 列表，别无限长）。
MAX_RESOLVE_ASINS = 500


@router.get("/api/extension/ping")
async def api_extension_ping():
    """插件选项页的「测试连接」打这里。

    只回**无害**的运行状态：版本、默认邮编、在线 worker 数。
    刻意不回批次列表 / ASIN 数 / 任何业务数据 —— 这个端点存在的唯一目的是
    回答"地址对不对、服务活没活着"，多回一个字段就多一份信息泄露面
    （它是插件唯一一个在配置完成**之前**就会被调用的端点）。
    """
    _s = _srv()
    # 在线口径**照抄** server/api/fleet.py:api_workers 的那一行
    # （`last_seen` 是 time.time() 浮点，60 秒无心跳算离线）。
    # 自己另定一套阈值的话，插件说"3 个 worker 在线"而控制台说 1 个，
    # 两边都没错、都没法解释。
    now = time.time()
    online = sum(1 for w in _s._worker_registry.values()
                 if (now - w.get("last_seen", 0)) < 60)
    return {
        "ok": True,
        "service": "amazon-scraper-v4",
        "version": "4.0.0",
        "default_zip_code": _s._runtime_settings.get("zip_code", config.DEFAULT_ZIP_CODE),
        "workers_online": online,
        "server_time": _s._cn_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post("/api/extension/collect")
async def api_extension_collect(request: Request):
    """插件推送：一批 ASIN + 一批卖家 ID。

    Body（全部可选，但 `asins` 与 `seller_ids` 至少要有一个非空）:

    | 字段 | 默认 | 说明 |
    |---|---|---|
    | `asins` | `[]` | 要采详情的 ASIN |
    | `seller_ids` | `[]` | 要整店发现的卖家 ID |
    | `batch_name` | 自动生成 `ext_<ts>` | **同名 = 追加**（见模块头注） |
    | `marketplace` | `amazon.com` | 站点（`amazon.ca` 等）。不在注册表里 -> 400；不从 `page_url` 推导 |
    | `zip_code` | 服务端按站点取默认 | 配送邮编，按站点校验 |
    | `needs_screenshot` | `false` | 详情阶段是否截图 |
    | `seller_discover_mode` | `with_detail` | 卖家批次的采集深度 |
    | `source` / `page_url` | — | 仅记录用途，不影响行为 |

    响应里 `asin_batch` / `seller_batch` 两块各自独立，某一块没提交就是 `null`。
    非美国站时两块各多一个 `marketplace` 键。同名批次已属于另一个站点 -> 409。
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")

    _s = _srv()

    # ---- ASIN：复用 server 侧唯一的归一化实现 ----
    raw_asins = body.get("asins") or []
    if not isinstance(raw_asins, list):
        raise HTTPException(400, "asins 必须是数组")
    if len(raw_asins) > MAX_ASINS_PER_PUSH:
        raise HTTPException(400, f"单次最多推送 {MAX_ASINS_PER_PUSH} 个 ASIN，收到 {len(raw_asins)}")
    asins: List[str] = []
    seen = set()
    invalid_asins: List[str] = []
    for a in raw_asins:
        norm = _s._normalize_asin(a)
        if not norm:
            invalid_asins.append(str(a)[:32])
        elif norm not in seen:
            seen.add(norm)
            asins.append(norm)

    # ---- 卖家 ID：复用 sellers.py 的抽取器（裸 ID / URL 两种形态都收）----
    raw_sellers = body.get("seller_ids") or []
    if not isinstance(raw_sellers, list):
        raise HTTPException(400, "seller_ids 必须是数组")
    if len(raw_sellers) > MAX_SELLERS_PER_PUSH:
        raise HTTPException(400, f"单次最多推送 {MAX_SELLERS_PER_PUSH} 个卖家，收到 {len(raw_sellers)}")
    from server.api.sellers import _extract_sellers_from_text
    seller_ids = _extract_sellers_from_text("\n".join(str(s) for s in raw_sellers))

    if not asins and not seller_ids:
        detail = "asins 与 seller_ids 不能都为空"
        if invalid_asins:
            detail += f"（收到 {len(invalid_asins)} 个非法 ASIN，如 {invalid_asins[:3]}）"
        raise HTTPException(400, detail)

    # ---- 站点（F-012）：放在上面那几条 400 之后，老调用方的报错顺序不变；
    #      放在任何写库之前，站点非法时不会留下一个空批次 ----
    spec = _site_of(body)
    is_us = spec.id == _marketplace.DEFAULT_MARKETPLACE

    # ---- 邮编 / 截图 / 批次名 ----
    zip_raw = body.get("zip_code")
    if zip_raw:
        zc = _s._normalize_zip(zip_raw, spec.id)
        if not zc:
            if is_us:
                # 美国站文案逐字节不变（tests/test_extension_api 钉着 "非法邮编"）。
                raise HTTPException(400, f"非法邮编: {zip_raw!r}")
            raise HTTPException(
                400, f"非法邮编: {zip_raw!r}（{spec.label}需要 "
                     f"{spec.default_postal!r} 这种形状）")
    else:
        # 美国站：全局设置原样透传，不校验（与改造前逐字节一致）；
        # 其它站点：全局值对该站合法才用，否则注册表默认 —— 全局设置里存的
        # 是美国邮编，拿它去加拿大站不会报错，而是静默落到默认地区。
        zc = _s._site_default_zip(spec)

    needs_screenshot = bool(body.get("needs_screenshot", False))

    discover_mode = body.get("seller_discover_mode") or "with_detail"
    if discover_mode not in ("discover_only", "with_detail"):
        raise HTTPException(400, f"非法 seller_discover_mode: {discover_mode}")

    batch_name = (body.get("batch_name") or "").strip()[:120]
    if not batch_name:
        batch_name = _s._batch_name("ext")
    # 批次名会进截图落盘路径（`server/static/screenshots/<批次名>/<asin>.png`），
    # 所以必须过一遍路径组件校验，否则插件端一个带 `/` 或 `..` 的名字就能
    # 把截图写到别处去。⚠ `_safe_fs_component` 是**校验器不是净化器**：
    # 不合法时返回 None，而不是删掉坏字符 —— 拿返回值直接切片会 TypeError。
    if _s._safe_fs_component(batch_name) is None:
        raise HTTPException(
            400, f"batch_name 含有不能用作路径的字符（/ \\ .. 或控制字符）: {batch_name!r}")

    # ---- 一个批次一个站点：两个批次都先查完，再动手写（见模块头注）----
    if asins:
        await _reject_cross_site_append(batch_name, spec)
    if seller_ids:
        await _reject_cross_site_append(f"{batch_name}_sellers", spec)

    out: Dict[str, Any] = {
        "asin_batch": None,
        "seller_batch": None,
        "invalid_asins": len(invalid_asins),
        "source": str(body.get("source") or "")[:32],
    }

    # ---- 商品批次：create_batch_if_absent + create_tasks（追加语义天然成立）----
    if asins:
        batch_id, created = await _s.db.create_batch_if_absent(
            name=batch_name, needs_screenshot=needs_screenshot)
        if not batch_id:
            raise HTTPException(500, f"创建批次失败: {batch_name}")
        inserted = await _s.db.create_tasks(batch_id, asins, zc, needs_screenshot,
                                            marketplace=spec.id)
        out["asin_batch"] = {
            "batch_id": batch_id,
            "batch_name": batch_name,
            "created": created,
            "submitted_asins": len(asins),
            "inserted_tasks": inserted,
            "zip_code": zc,
            "status_url": f"/api/batches/{batch_name}/status",
        }
        # 只在非美国站时多回一个键：老插件（不带 marketplace）拿到的响应
        # 与改造前逐字节一致，与 /api/upload 的口径相同。
        if not is_us:
            out["asin_batch"]["marketplace"] = spec.id

    # ---- 卖家批次：独立批次，见模块头注 ----
    if seller_ids:
        seller_batch_name = f"{batch_name}_sellers"
        sb_id, sb_inserted = await _s.db.create_seller_batch(
            name=seller_batch_name,
            seller_ids=seller_ids,
            discover_mode=discover_mode,
            zip_code=zc,
            needs_screenshot=needs_screenshot,
            # 整店发现任务带上站点：worker 按任务行的站点去翻**这个站点**的
            # 店铺，派生出来的详情任务再继承它（common/pgdb/media.py）。
            # 不带的话加拿大店铺页上点"整店采集"，翻的是同一个卖家的美国店。
            marketplace=spec.id,
        )
        if not sb_id:
            raise HTTPException(500, f"创建卖家批次失败: {seller_batch_name}")
        out["seller_batch"] = {
            "batch_id": sb_id,
            "batch_name": seller_batch_name,
            "discover_mode": discover_mode,
            "submitted_sellers": len(seller_ids),
            "inserted_tasks": sb_inserted,
            "status_url": f"/api/seller-batches/{sb_id}/progress",
        }
        if not is_us:
            out["seller_batch"]["marketplace"] = spec.id

    return out


@router.post("/api/extension/resolve-sellers")
async def api_extension_resolve_sellers(request: Request):
    """按 ASIN 反查**已采到的**卖家（`asin_data.seller_id`）。

    用途：商品详情页上读不到三方卖家时（Amazon 改版、自营页、异步渲染没跑完），
    插件拿这个补全，用户就还能勾"采集该卖家全部商品"。

    **只回库里已经有的**，查不到就不回 —— 这是补全，不是推测。
    返回一个猜的卖家会让用户采回一整店无关商品，而且完全看不出是错的。

    Body：`asins`（数组）+ 可选 `marketplace`（缺省 `amazon.com`，不在注册表里 -> 400）。
    **只查该站点的行**：同一个 ASIN 在美国站和加拿大站可以是不同的卖家。
    """
    body = await request.json()
    raw = (body or {}).get("asins") or []
    if not isinstance(raw, list):
        raise HTTPException(400, "asins 必须是数组")
    if len(raw) > MAX_RESOLVE_ASINS:
        raise HTTPException(400, f"单次最多查询 {MAX_RESOLVE_ASINS} 个 ASIN，收到 {len(raw)}")

    _s = _srv()
    # F-012：按站点查。`asin_data` 的唯一键是 `(asin, marketplace)`，不过滤
    # 的话同一个 ASIN 两个站点的行都会回来、后一行覆盖前一行 —— 加拿大队列
    # 条目拿到美国卖家，再拿它去建加拿大站的整店任务。缺省按美国站过滤：
    # F-012 之前库里只有美国行，对老数据结果不变。
    spec = _site_of(body or {})
    asins: List[str] = []
    seen = set()
    for a in raw:
        norm = _s._normalize_asin(a)
        if norm and norm not in seen:
            seen.add(norm)
            asins.append(norm)
    if not asins:
        return {"sellers": {}, "queried": 0}

    # 变长 IN 列表：`?` 占位符两个后端通用（PG 侧由 common/pgdb 翻译成 $n）。
    # 长度已由 MAX_RESOLVE_ASINS 封顶，不会撞上 PG 的 32767 参数上限。
    placeholders = ",".join("?" for _ in asins)
    sql = (
        "SELECT asin, seller_id, seller_name FROM asin_data "
        f"WHERE asin IN ({placeholders}) AND marketplace = ? "
        "AND seller_id IS NOT NULL AND seller_id != ''"
    )
    sellers: Dict[str, Dict[str, Optional[str]]] = {}
    async with _srv().db.read() as rc, rc.execute(sql, asins + [spec.id]) as c:
        async for r in c:
            row = dict(r)
            sid = (row.get("seller_id") or "").strip()
            # 解析不出卖家时写进库的是字面量 "N/A"（见 worker/parser.py 的
            # _default_result）。它是"没采到"，不是一个卖家 ID —— 回给插件
            # 会变成一个采不出任何东西的整店任务。
            if not sid or sid.upper() in ("N/A", "NA", "NONE"):
                continue
            sellers[row["asin"]] = {
                "seller_id": sid,
                "seller_name": row.get("seller_name") or None,
            }
    return {"sellers": sellers, "queried": len(asins)}
