"""HTML 页面路由（5 个）—— 从 `server/app.py` 拆出来的第一块，Phase 3.1 的试点。

它的作用不止是"少 48 行"：它是**「光秃 `APIRouter()` + `include_router` 不改
openapi」这个假设的实测**。假设错了，代价只有这 48 行（`git revert`）。

------------------------------------------------------------------------
三条承重约束（改之前先读完，每一条都对应一次会静默飘红的事故）
------------------------------------------------------------------------

1. **router 必须光秃**：`APIRouter()`，不带 `tags=`、不带 `prefix=`、
   不设 `include_in_schema`。当前整份 `/openapi.json` 里**没有任何 `tags` 键**，
   照抄 `server/api/sync.py` 的 `APIRouter(tags=[...])` 会给本 router 下每个
   operation 加一个 tags 数组 —— 而 openapi 是黄金基线的一步、逐字节钉死。

2. **函数名 / docstring / 路径一个字都不能改**：它们被编码进 `operationId`、
   `summary`、`description`。改名 = 基线当场飘红。

3. **模块级可变全局一个都不搬**：`db` / `_worker_registry` / `_runtime_settings`
   全部留在 `server/app.py`，这里一律走 `_srv().xxx` 属性访问。
   **禁止 `from server.app import db`** —— 一来是循环导入（`app.py` 要 import
   本模块来挂路由），二来黄金夹具 `tests/golden/harness.py` 按名字给
   `server.app` 打补丁（no-op 后台协程、重置 5 个进程级注册表），
   三个 PG 夹具还 `monkeypatch.setattr(srv, "db", pgdb)` ——
   from-import 会把补丁前的旧对象快照下来，补丁就打空了。

`templates` 与 `| cst` 过滤器搬到这里是因为**只有这 5 个 handler 用它**
（全仓已复核：`app.py` 之外零引用）。`app.mount("/static", ...)` 留在 `app.py`，
那是 app 级挂载不是路由。
"""

import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from common import config
from common.core import marketplace as _mkt


def _srv():
    from server import app as _s
    return _s


router = APIRouter()

templates = Jinja2Templates(directory=config.TEMPLATE_DIR)


def _cst_filter(value):
    """Jinja 过滤器 `| cst`：把 UTC 时间字符串转中国时间（Asia/Shanghai, UTC+8）显示。
    服务器存 UTC（系统时区 Etc/UTC），前端统一显示中国时间。"""
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return value


templates.env.filters["cst"] = _cst_filter


def _marketplace_options():
    """站点下拉框的选项：``[(id, label, default_postal), ...]``（F-012 blocker 3）。

    直接从站点注册表渲染，模板里**不许**再写第二份站点表 —— 两份表迟早分叉，
    分叉的后果是下拉里有一个服务端会 400 的站点，或者少一个能采的站点。

    顺序：美国站固定第一个，其余按注册表的排序。``all_ids()`` 是按字母排的，
    ``amazon.ca`` 排在 ``amazon.com`` 前面 —— 模板里哪个分支要是「默认选第一个」，
    默认站点就悄悄变成了加拿大。默认值由模板按 ``DEFAULT_MARKETPLACE`` 显式选中，
    这里把美国站放第一个只是让下拉的观感与「默认 = 美国站」一致。
    """
    ids = sorted(_mkt.all_ids(), key=lambda i: i != _mkt.DEFAULT_MARKETPLACE)
    return [(i, _mkt.get(i).label, _mkt.get(i).default_postal) for i in ids]


# ==================== HTML 页面 ====================

@router.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    progress = await _srv().db.get_progress()
    total = progress["done"] + progress["failed"]
    progress["completion_rate"] = round(progress["done"] / progress["total"] * 100, 1) if progress["total"] else 0
    progress["success_rate"] = round(progress["done"] / total * 100, 1) if total else 0
    batches = await _srv().db.get_batches()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "request": request,
        "progress": progress,
        "batches": batches,
        "active_workers": len([w for w in _srv()._worker_registry.values() if time.time() - w["last_seen"] < 60]),
    })


@router.get("/tasks", response_class=HTMLResponse)
async def page_tasks(request: Request):
    batches = await _srv().db.get_batches()
    return templates.TemplateResponse(request=request, name="tasks.html", context={
        "request": request,
        "batches": batches,
        "default_zip_code": _srv()._runtime_settings.get("zip_code", config.DEFAULT_ZIP_CODE),
        "marketplaces": _marketplace_options(),
        "default_marketplace": _mkt.DEFAULT_MARKETPLACE,
    })


@router.get("/results", response_class=HTMLResponse)
async def page_results(request: Request):
    return templates.TemplateResponse(request=request, name="results.html", context={"request": request})


@router.get("/workers", response_class=HTMLResponse)
async def page_workers(request: Request):
    return templates.TemplateResponse(request=request, name="workers.html", context={"request": request})


@router.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "request": request,
        "settings": _srv()._runtime_settings,
        "marketplaces": _marketplace_options(),
        "default_marketplace": _mkt.DEFAULT_MARKETPLACE,
        "config": {
            "port": config.SERVER_PORT,
            "timeout": config.REQUEST_TIMEOUT,
            "db_path": config.DB_PATH,
        },
    })
