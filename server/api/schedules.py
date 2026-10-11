"""定时采集任务（5 个端点）—— 从 `server/app.py` 拆出。

    GET    /api/schedules
    POST   /api/schedules
    PUT    /api/schedules/{sched_id}
    DELETE /api/schedules/{sched_id}
    POST   /api/schedules/{sched_id}/run

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **模块级可变全局一个都不搬。** `db` / `_runtime_settings` 留在
   `server/app.py`，这里一律 `_srv().xxx` 属性访问，**禁止 from-import**：
   夹具按名字给 `server.app` 打补丁，而 `_runtime_settings` 还会被
   `api_reset_settings` **整体重新赋值**，快照下来的旧 dict 之后没人读。

2. **`_SCHEDULES_DIR` 与 `_extract_asins_from_file` 留在 `app.py`**，
   本模块走 `_srv()`。它们不只服务本模块：
     * `_SCHEDULES_DIR` 在 lifespan 里 `makedirs`；
     * `_extract_asins_from_file` 还被后台协程 `_auto_scrape_scheduler` 调。
   搬过来就等于让 app.py 反向 import 本模块，绕成环。

3. **`_get_schedules` / `_save_schedules` 搬过来了** —— 与上一条相反，
   这两个只有本模块的 5 条路由在用（`_auto_scrape_scheduler` 直接读
   `_runtime_settings["auto_scrape_schedules"]` 并调 `_save_settings()`，
   不经过它们）。

4. **函数名 / docstring / 路径一个字不改** —— 编码进 `operationId` /
   `summary` / `description`，而 `/openapi.json` 是黄金基线的一步。

5. **router 光秃**：`APIRouter()`，不带 `tags=` / `prefix=`。

6. **路由匹配**：`/api/schedules` 是静态的，`/api/schedules/{sched_id}` 与
   `/api/schedules/{sched_id}/run` 段数不同，三者互不遮蔽。
"""

import os
import uuid
from datetime import timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from typing import Optional

# 起别名：`api_create_schedule` 有个同名 Form 参数 `marketplace`，会遮蔽
# 模块级同名 import（同 server/api/batches.py）。
from common.core import marketplace as _mkt
from common.core.timeutil import now_ts


def _srv():
    from server import app as _s
    return _s


router = APIRouter()


def _get_schedules() -> list:
    return _srv()._runtime_settings.get("auto_scrape_schedules", [])


def _save_schedules(schedules: list):
    _s = _srv()
    _s._runtime_settings["auto_scrape_schedules"] = schedules
    _s._save_settings()


async def _resolve_asins(source_file: str, spec: "_mkt.MarketplaceSpec") -> list:
    """定时任务要采的 ASIN 清单。

    **空 `source_file` = 全库**，这是 `_auto_scrape_scheduler` 一直以来的语义
    （它见到空串就走 `db.get_all_asins()`）。这里把同一套判定收进一个函数，
    让「自动触发」和「手动立即执行」走同一条路。

    以前 `api_run_schedule_now` 是直接 `_extract_asins_from_file(source_file)`，
    对全库任务（`source_file=""`）恒返回空列表 -> 400「ASIN 文件为空或不存在」，
    也就是**全库定时任务点「立即执行」永远失败**，而它自动触发时明明是好的。

    F-012 blocker 3：「全库」= **本定时任务那个站点**的全库。不按站点取的话，
    美国定时任务会把只在加拿大站采过的 ASIN 当成美国任务去采。
    """
    _s = _srv()
    if source_file and os.path.isfile(source_file):
        return _s._extract_asins_from_file(source_file)
    if source_file:
        # 指定了文件却找不到 —— 这是真的坏了，不能悄悄降级成全库跑一遍
        return []
    return await _s.db.get_all_asins(marketplace=spec.id)


# ---------------------------------------------------------------------------
# F-012 blocker 3：定时任务的站点与邮编
#
# 存储形状：sched dict 上两个**可选**键 `marketplace`（规范站点键）与
# `zip_code`（已按该站点归一）。老定时任务（以及不传这两个字段的老客户端
# 新建的定时任务）没有这两个键 -> 美国站 + 全局邮编原样，与改造前逐字节一致。
# 不回填老数据：回填会让下一次 `_save_settings` 把老 JSON 改写掉。
#
# 下面三个函数由「立即执行」与 `server/app.py:_auto_scrape_scheduler` 共用，
# 两条路径各写一份必然会漂 —— 漂了之后两边都不报错，只是一条路采错站点。
# ---------------------------------------------------------------------------

def _schedule_spec(sched: dict) -> "_mkt.MarketplaceSpec":
    """定时任务存的站点 -> spec。缺键 / None / 空串 -> 美国站。

    存储值**每次都重新校验**，不信任创建时的校验：`PUT /api/settings` 能把
    整个 `auto_scrape_schedules` 列表原样覆盖，里面可以是任何东西。
    不合法 -> HTTPException(400)（与 HTTP 入口同一个解析函数，口径不会漂）；
    自动调度那边接住它、记日志、跳过**这一条**。
    """
    try:
        return _srv()._resolve_marketplace(sched.get("marketplace"))
    except HTTPException as e:
        raise HTTPException(
            400, f"定时任务 {sched.get('id')} 存的站点不可用：{e.detail}") from None


def _schedule_zip(sched: dict, spec: "_mkt.MarketplaceSpec") -> str:
    """定时任务这一次用哪个邮编。

    自己存了邮编、且对该站点合法 -> 用它；否则 -> 该站点的默认邮编
    （美国站 = 全局设置原样，与改造前一致；其它站点见 `_site_default_zip`）。
    存的值不合法时不中断（同上，存储值可能被 PUT /api/settings 改坏），
    但记一条 warning —— 静默换邮编不能连日志都没有。
    """
    _s = _srv()
    own = sched.get("zip_code")
    if own:
        nz = _s._normalize_zip(own, spec.id)
        if nz:
            return nz
        _s.logger.warning(
            f"定时任务 {sched.get('id')} 存的邮编 {own!r} 不是{spec.label}的形状，"
            f"本次改用该站点默认邮编")
    return _s._site_default_zip(spec)


def _auto_batch_prefix(sched: dict, spec: "_mkt.MarketplaceSpec") -> str:
    """定时任务批次名的前缀（后面由 `_batch_name` 接秒级时间戳）。

    美国站与改造前一字不差：`auto_{name}`。其它站点追加站点键
    （`auto_{name}_amazon.ca`）：两个**同名同时刻**的定时任务会在同一秒拼出同一个
    批次名，而 `create_batch` 撞名返回的是既有批次 id —— 美国与加拿大的任务就会
    并进同一个批次，一个批次混两个站点（下游按批次取数全都假设一批一站点），
    两站都有的 ASIN 还会被 tasks 的 UNIQUE(batch_id, asin) 静默吃掉一边。
    """
    p = f"auto_{sched.get('name', 'task')}"
    return p if spec.id == _mkt.DEFAULT_MARKETPLACE else f"{p}_{spec.id}"


@router.get("/api/schedules")
async def api_list_schedules():
    return {"schedules": _get_schedules()}


@router.post("/api/schedules")
async def api_create_schedule(request: Request,
                              file: Optional[UploadFile] = File(None),
                              name: str = Form(""),
                              time_str: str = Form(..., alias="time"),
                              interval_days: int = Form(1),
                              needs_screenshot: bool = Form(False),
                              marketplace: str = Form(""),
                              zip_code: str = Form("")):
    """创建定时采集任务。

    `file` 可选：
      * **带文件** —— 定时采集文件里的那批 ASIN；
      * **不带文件** —— 定时采集**全库** ASIN（`source_file` 存空串，
        调度时现取该站点的 `db.get_all_asins(marketplace=...)`）。

    `marketplace` 可选：不传 / 空 = 美国站 `amazon.com`（与加这个字段之前一致）；
    `amazon.ca`（也收 `www.amazon.ca` 等写法）= 加拿大站；不在站点注册表里 -> 400。
    `zip_code` 可选：本定时任务固定用的邮编，必须是该站点的形状，否则 400；
    不传则每次执行时取该站点的默认邮编。两个字段都只在传了时才存进定时任务。

    不带文件这条路以前是另一个端点 `POST /api/auto-scrape/schedules`
    （`api_legacy_add_schedule`）。那个端点与本端点写的是同一份
    `_runtime_settings["auto_scrape_schedules"]`，只是不收 `name` /
    `interval_days` / `needs_screenshot`，于是前端被迫先 POST 旧端点、
    再立刻 PUT 本端点把这几个字段补回去。现在合成一个，旧端点已删除。
    """
    _s = _srv()
    # 验证时间格式
    try:
        h, m = map(int, time_str.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "时间格式错误，应为 HH:MM")

    if interval_days < 1:
        raise HTTPException(400, "间隔天数至少为 1")

    # F-012 blocker 3：站点与邮编在**写文件之前**校验 —— 400 时 _SCHEDULES_DIR
    # 里不能留下一个没有定时任务指向的孤儿文件。
    spec = _s._resolve_marketplace(marketplace)
    sched_zip = None
    if zip_code and zip_code.strip():
        # 这是个新字段，没有需要兼容的旧行为，所以美国站也照样校验
        # （与 /api/upload 的批次邮编不同，那边美国站要保持原样透传）。
        sched_zip = _s._normalize_zip(zip_code, spec.id)
        if not sched_zip:
            raise _s._site_zip_error(zip_code, spec)

    sched_id = f"sched_{uuid.uuid4().hex[:8]}"

    # 浏览器在「选了文件的 input 又清空」时仍会发一个 filename 为空的 part，
    # 所以判定"有没有文件"要看 filename，不能只看 file is None。
    has_file = file is not None and bool((file.filename or "").strip())

    if has_file:
        os.makedirs(_s._SCHEDULES_DIR, exist_ok=True)
        ext = os.path.splitext(file.filename or "")[1] or ".txt"
        source_file = os.path.join(_s._SCHEDULES_DIR, f"{sched_id}{ext}")
        content = await file.read()
        if len(content) > _s.MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"文件过大：{len(content)//1024//1024}MB，上限 {_s.MAX_UPLOAD_BYTES//1024//1024}MB")
        with open(source_file, "wb") as f:
            f.write(content)

        # 验证文件中有 ASIN
        asin_list = _s._extract_asins_from_file(source_file)
        if not asin_list:
            os.remove(source_file)
            raise HTTPException(400, "文件中未找到有效 ASIN")
        asin_count = len(asin_list)
    else:
        # 空 source_file = 全库模式，调度时现取 db.get_all_asins()。
        # asin_count 记 0：这里没法预知执行那一刻库里有多少个 ASIN，
        # 记一个当前值只会让 UI 显示一个很快就过期的数字。
        source_file = ""
        asin_count = 0

    # 首次创建：last_run_date 设为昨天，确保首次检查时立即触发
    yesterday = (_s._cn_now() - timedelta(days=interval_days)).strftime("%Y-%m-%d")

    default_name = f"定时任务-{time_str}" if has_file else f"全库采集-{time_str}"
    if spec.id != _mkt.DEFAULT_MARKETPLACE:
        # 非美国站的默认名带站点名：同一时刻的美国与加拿大定时任务不给名字时
        # 默认名会一模一样，列表里分不出来，批次名也只差 _auto_batch_prefix 那个后缀。
        default_name = f"{spec.label}-{default_name}"
    sched = {
        "id": sched_id,
        "name": name or default_name,
        "time": time_str,
        "interval_days": interval_days,
        "source_file": source_file,
        "asin_count": asin_count,
        "needs_screenshot": needs_screenshot,
        "enabled": True,
        "last_run_date": yesterday,
        "created_at": now_ts(),
    }
    # 只在调用方给了时才存：老客户端建出来的定时任务（以及它在响应 / GET
    # /api/schedules / runtime_settings.json 里的样子）与改造前逐字节一致。
    # 读的一侧把缺键当美国站 + 默认邮编，见 _schedule_spec / _schedule_zip。
    if marketplace and marketplace.strip():
        sched["marketplace"] = spec.id
    if sched_zip:
        sched["zip_code"] = sched_zip

    schedules = _get_schedules()
    schedules.append(sched)
    _save_schedules(schedules)

    return {"ok": True, "schedule": sched, "schedules": schedules}


@router.put("/api/schedules/{sched_id}")
async def api_update_schedule(sched_id: str, request: Request):
    """修改定时任务（enabled/time/interval_days/name）"""
    body = await request.json()
    schedules = _get_schedules()
    target = None
    for s in schedules:
        if s.get("id") == sched_id:
            target = s
            break
    if target is None:
        raise HTTPException(404, "定时任务不存在")

    if "enabled" in body:
        target["enabled"] = bool(body["enabled"])
    if "name" in body:
        target["name"] = body["name"]
    if "time" in body:
        try:
            h, m = map(int, body["time"].split(":"))
            if 0 <= h <= 23 and 0 <= m <= 59:
                target["time"] = body["time"]
        except (ValueError, AttributeError):
            pass
    if "interval_days" in body:
        val = int(body["interval_days"])
        if val >= 1:
            target["interval_days"] = val

    _save_schedules(schedules)
    return {"ok": True, "schedules": schedules}


@router.delete("/api/schedules/{sched_id}")
async def api_delete_schedule(sched_id: str):
    """删除定时任务 + ASIN 文件"""
    schedules = _get_schedules()
    target = None
    new_schedules = []
    for s in schedules:
        if s.get("id") == sched_id:
            target = s
        else:
            new_schedules.append(s)
    if target is None:
        raise HTTPException(404, "定时任务不存在")

    # 删除 ASIN 文件
    source_file = target.get("source_file", "")
    if source_file and os.path.isfile(source_file):
        os.remove(source_file)

    _save_schedules(new_schedules)
    return {"ok": True, "schedules": new_schedules}


@router.post("/api/schedules/{sched_id}/run")
async def api_run_schedule_now(sched_id: str):
    """手动立即执行一次定时任务"""
    _s = _srv()
    schedules = _get_schedules()
    target = None
    for s in schedules:
        if s.get("id") == sched_id:
            target = s
            break
    if target is None:
        raise HTTPException(404, "定时任务不存在")

    # F-012 blocker 3：站点先于一切 —— 「全库」取哪个站点、批次名、邮编都看它；
    # 存的值不合法 -> 400，且必须在 create_batch 之前（否则留下一个空批次）。
    spec = _schedule_spec(target)

    source_file = target.get("source_file", "")
    # 空 source_file = 全库，与 _auto_scrape_scheduler 同一套判定（见 _resolve_asins）
    asin_list = await _resolve_asins(source_file, spec)
    if not asin_list:
        if source_file:
            msg = "ASIN 文件为空或不存在"
        elif spec.id == _mkt.DEFAULT_MARKETPLACE:
            msg = "全库没有任何 ASIN 可采"
        else:
            msg = f"{spec.label}全库没有任何 ASIN 可采"
        raise HTTPException(400, msg)

    now = _s._cn_now()  # last_run 用中国时间
    # P4.7：**分钟 -> 秒**（有意的行为改动，见 _batch_name 的 docstring）。
    # 这一处和 _auto_scrape_scheduler 那一处正是会互相撞名的两个。
    batch_name = _s._batch_name(_auto_batch_prefix(target, spec))
    zc = _schedule_zip(target, spec)
    ns = target.get("needs_screenshot", False)
    batch_id = await _s.db.create_batch(batch_name, ns, is_auto=True)
    await _s.db.create_tasks(batch_id, asin_list, zc, ns, marketplace=spec.id)

    target["last_run_date"] = now.strftime("%Y-%m-%d")
    _save_schedules(schedules)
    _s.logger.info(f"手动执行定时任务: {batch_name}, {len(asin_list)} ASINs")

    resp = {"ok": True, "batch_id": batch_id, "batch_name": batch_name, "asin_count": len(asin_list)}
    # 同 /api/upload：站点只在非美国站时回显，美国站响应逐字节不变
    if spec.id != _mkt.DEFAULT_MARKETPLACE:
        resp["marketplace"] = spec.id
    return resp
