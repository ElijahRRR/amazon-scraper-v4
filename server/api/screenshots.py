"""截图查询与取图（2 个端点）。

    GET /api/screenshots                       —— 列状态 + URL（JSON，游标分页）
    GET /api/screenshots/{batch_name}/{asin}   —— 取那张 PNG

------------------------------------------------------------------------
为什么需要这两条
------------------------------------------------------------------------
在这之前，程序化调用方想拿截图只有两个办法：

1. `GET /api/export/{batch}/screenshots` —— 整批打 ZIP。要拿一张也得拉全批，
   而且批次没截完就是 404，中途取不到任何东西。
2. `/static/screenshots/<batch>/<asin>.png` —— 这条**是**对外契约
   （`docs/erpapi_contract.md` §4.7），本模块不取代它、也不改它。但它有两个
   使用上的坎：
   * 路径不该自己拼，得先 `GET /api/results` 读 `items[].screenshot_path`
     —— 而那个端点返回的是整行商品数据，为了一个路径拉一整行；
   * 它的 404 有歧义：没这个 ASIN、还没截、截失败了，全是
     `StaticFiles` 的 404，调用方无法据此决定「再等等」还是「别等了」。

本模块补的正是这两点：先按批次列状态拿 URL（不必拉商品数据），再按 URL 取图；
取图时 pending 与 failed 是**两个不同的状态码**，可重试与不可重试因此能被区分。

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------
* `batch_name` / `asin` 都会拼进磁盘路径，两个都必须过 `_safe_fs_component`
  / `_normalize_asin`。这不是重复校验——写入侧（`POST /api/tasks/screenshot`）
  校验的是**当时**那个值，读取侧收到的是**另一个**请求里的值。
* 路由不冲突：`/api/screenshots` 是 2 段静态路径，
  `/api/screenshots/{batch_name}/{asin}` 是 4 段，段数不同，谁先注册都一样。
* 模块级可变全局一个不搬，一律 `_srv().xxx`（理由同 `server/api/batches.py`
  的承重约束 1：夹具按名字给 `server.app` 打补丁）。
"""

import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from common import config


def _srv():
    from server import app as _s
    return _s


router = APIRouter()

#: 单页上限。截图行很窄（6 列，无大文本），1000 行的响应体也就几百 KB，
#: 与 /api/results 的 MAX_PAGE_LIMIT 取同一个数，调用方不用记两套。
MAX_PAGE_LIMIT = 1000


@router.get("/api/screenshots")
async def api_screenshots(request: Request,
                          batch_name: str = Query(None),
                          batch_id: int = Query(None),
                          asin: str = Query(None),
                          status: str = Query(None),
                          cursor: str = Query(None),
                          limit: int = Query(200, le=MAX_PAGE_LIMIT)):
    """列出某批次的截图状态，每条带可直接 GET 的 `url`。

    定位批次：`batch_name` 或 `batch_id` **给一个**（都不给 -> 400）。

    过滤：
    - `asin` —— 只看这一个
    - `status` —— `pending` / `processing` / `done` / `failed`

    分页：按 ASIN 升序，`cursor` 传上一页最后一个 `asin`，返回体的
    `next_cursor` 已经算好（没有下一页时是 `null`）。

    返回：

        {
          "batch_id": 12, "batch_name": "batch_20260809_101500",
          "progress": {"pending": 3, "processing": 0, "done": 7,
                       "failed": 0, "total": 10},
          "items": [
            {"asin": "B0XXXXXXX1", "status": "done", "retry_count": 0,
             "error_detail": null, "updated_at": "2026-08-09 10:20:31",
             "url": "http://host:8899/api/screenshots/batch_.../B0XXXXXXX1"}
          ],
          "next_cursor": "B0XXXXXXX1"
        }

    `url` 只在 `status == "done"` 时是非 null —— 别的状态下那张图不存在，
    给个 URL 只会让调用方去撞 404。`progress` 是**整批**的计数，不受
    `asin`/`status`/`cursor` 过滤影响，方便一次请求同时回答「好了几张」和
    「这一页是哪几张」。
    """
    _s = _srv()
    db = _s.db

    if batch_name:
        if _s._safe_fs_component(batch_name) is None:
            raise HTTPException(400, "非法批次名")
        batch = await db.get_batch_by_name(batch_name)
        if not batch:
            raise HTTPException(404, f"批次不存在: {batch_name}")
        bid = batch["id"]
        bname = batch_name
    elif batch_id is not None:
        bid = batch_id
        # 反查名字：url 要拼它，而且能顺带确认批次存在。
        rows = await db.get_batches()
        match = next((b for b in rows if b.get("id") == batch_id), None)
        if not match:
            raise HTTPException(404, f"批次不存在: {batch_id}")
        bname = match.get("name")
    else:
        raise HTTPException(400, "需要 batch_name 或 batch_id")

    if status and status not in ("pending", "processing", "done", "failed"):
        raise HTTPException(400, f"非法 status: {status}")

    norm_asin = None
    if asin:
        norm_asin = _s._normalize_asin(asin)
        if not norm_asin:
            raise HTTPException(400, "非法 ASIN")

    rows = await db.list_screenshots(bid, asin=norm_asin, status=status,
                                     cursor_asin=cursor, limit=limit)
    progress = await db.get_screenshot_progress(bid)

    base = str(request.base_url).rstrip("/")
    items = []
    for r in rows:
        done = r.get("status") == "done"
        items.append({
            "asin": r["asin"],
            "status": r.get("status"),
            "retry_count": r.get("retry_count"),
            "error_detail": r.get("error_detail"),
            "updated_at": r.get("updated_at"),
            "url": f"{base}/api/screenshots/{bname}/{r['asin']}" if done else None,
        })

    # 只有「这一页装满了」才可能还有下一页。装不满就必然到底了，
    # 给 next_cursor 会让调用方多打一次空请求。
    next_cursor = items[-1]["asin"] if len(items) >= limit and items else None

    return {
        "batch_id": bid,
        "batch_name": bname,
        "progress": progress,
        "items": items,
        "next_cursor": next_cursor,
    }


@router.get("/api/screenshots/{batch_name}/{asin}")
async def api_screenshot_file(batch_name: str, asin: str):
    """取一张截图（`image/png`）。

    状态码是有意分开的，调用方可以据此决定要不要重试：

    - **200** —— 图在这儿
    - **404** —— 这个批次/ASIN 根本没有截图记录（或批次不存在）。别重试。
    - **409** —— 有记录但还没截好（`pending` / `processing`）。**稍后再来**。
      响应体带 `status`，`Retry-After: 10`。
    - **410** —— 截图**失败**了，不会再有。响应体带 `error_detail`。别重试。

    `.png` 后缀可带可不带，`B0XXXXXXX1` 与 `B0XXXXXXX1.png` 等价。
    """
    _s = _srv()

    if _s._safe_fs_component(batch_name) is None:
        raise HTTPException(400, "非法批次名")
    # 允许调用方把 url 里的文件名整个抄过来
    if asin.lower().endswith(".png"):
        asin = asin[:-4]
    norm_asin = _s._normalize_asin(asin)
    if not norm_asin:
        raise HTTPException(400, "非法 ASIN")

    batch = await _s.db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(404, f"批次不存在: {batch_name}")

    rows = await _s.db.list_screenshots(batch["id"], asin=norm_asin, limit=1)
    if not rows:
        raise HTTPException(404, f"无截图记录: {norm_asin}@{batch_name}")
    row = rows[0]
    st = row.get("status")

    if st == "failed":
        raise HTTPException(410, {
            "error": "screenshot_failed",
            "message": f"截图失败，不会再产出: {norm_asin}@{batch_name}",
            "error_detail": row.get("error_detail"),
            "retry_count": row.get("retry_count"),
        })
    if st != "done":
        raise HTTPException(409, {
            "error": "screenshot_pending",
            "message": f"截图尚未完成: {norm_asin}@{batch_name}",
            "status": st,
        }, headers={"Retry-After": "10"})

    # 落盘路径由 batch_name/asin 重新拼出，**不**用库里的 file_path 直接开文件：
    # 那一列是给前端拼 URL 用的相对路径，历史数据里出现过绝对路径与占位值
    # （所以才有 _normalize_screenshot_path）。这里两个组件都已校验过，
    # 重拼一次比信任一个可能被改写过的字段安全。
    path = os.path.join(config.SCREENSHOT_DIR, batch_name, f"{norm_asin}.png")
    if not os.path.isfile(path):
        # 库说 done、盘上没有 —— 文件被清过（比如删批次时的
        # _remove_screenshot_files 半途失败），对调用方就是没有了。
        raise HTTPException(404, f"截图文件缺失: {norm_asin}@{batch_name}")

    return FileResponse(path, media_type="image/png",
                        filename=f"{norm_asin}.png")
