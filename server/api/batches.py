"""批次上传与生命周期（12 个端点）—— 从 `server/app.py` 拆出。

    POST   /api/upload
    POST   /api/batches
    GET    /api/batches
    GET    /api/progress
    GET    /api/batches/{batch_name}/screenshots/progress
    GET    /api/batches/{batch_name}/status
    POST   /api/batches/{batch_name}/callback/retry
    POST   /api/batches/{batch_id}/prioritize
    POST   /api/batches/{batch_name}/retry
    DELETE /api/batches/{batch_name}
    POST   /api/batches/delete-bulk
    GET    /api/batches/{batch_id}/failures

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **模块级可变全局一个都不搬。** `db` / `_callback_send_queue` /
   `_runtime_settings` / `_completion_check_set` 全部留在 `server/app.py`，
   这里一律 `_srv().xxx` 属性访问，**禁止 from-import**：
     * 黄金夹具与 PG 夹具按名字给 `server.app` 打补丁
       （`monkeypatch.setattr(srv, "db", pgdb)` 等），from-import 拿到的是
       快照，补丁打空；
     * `db` 与 `_callback_send_queue` 在 lifespan 里才被赋值
       （`global db, _callback_send_queue`），导入期 from-import 只会拿到 None。

2. **私有助手留在 `app.py`**：`_normalize_asin` / `_normalize_zip` /
   `_batch_name` / `_is_safe_callback_url` / `_remove_screenshot_files` /
   `_BATCH_NAME_CONFLICT_CODE` / `MAX_UPLOAD_BYTES`。它们另有调用点
   （后台协程、其它模块、以及 `tests/test_batch_name_precision.py` 与
   `tests/test_error_codes.py` 直接 from-import 了后两个），这里走 `_srv()`。

3. **函数名 / docstring / 路径一个字不改** —— 编码进 `operationId` /
   `summary` / `description` / `Body_*` schema 名，而 `/openapi.json` 是黄金
   基线的一步、逐字节钉死。

4. **router 光秃**：`APIRouter()`，不带 `tags=` / `prefix=`。

5. **路由注册次序原样保留**（本文件自上而下 = 原 `app.py` 里的先后）。
   `/api/batches` 这一族里有两种路径参数，靠**末段**区分，互不遮蔽：
     `{batch_name}/status`、`{batch_name}/retry`、`{batch_name}/callback/retry`、
     `{batch_name}/screenshots/progress`、`{batch_id}/prioritize`、
     `{batch_id}/failures`。
   `POST /api/batches/delete-bulk` 与 `DELETE /api/batches/{batch_name}` 段数
   相同但**方法不同**，也不冲突。
   `POST /api/batches`（JSON 推送）比它们都短一段，同样不冲突。

6. **`POST /api/upload` 与 `POST /api/batches` 共用 `_create_batch_with_tasks`。**
   两者只在「怎么把 ASIN 递进来」上不同（multipart 文件 vs JSON 数组），
   递进来之后的语义——撞名 409 的响应体、回显读回值——必须逐字一致，所以那段
   只有一份实现。改 409 的形状请改那个函数，别在端点里各改各的。
"""

import csv
import io
from datetime import datetime
from typing import Dict, List, Optional

import openpyxl
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile

from common import config
from common.core.timeutil import now_ts


def _srv():
    from server import app as _s
    return _s


router = APIRouter()


async def _create_batch_with_tasks(
    request: Request,
    unique_asins: List[str],
    per_asin_zip: Dict[str, str],
    invalid_zip_count: int,
    batch_name: Optional[str],
    zip_code: Optional[str],
    needs_screenshot: bool,
    callback_url: Optional[str],
    external_id: Optional[str],
    expand_variants: bool,
) -> Dict:
    """「建批次 + 建任务 + 撞名 409 + 回显存下来的值」——**唯一**一份实现。

    `POST /api/upload`（传文件）和 `POST /api/batches`（传 JSON）只是两种把
    ASIN 递进来的方式，递进来之后的语义必须一模一样。所以差异到 `unique_asins`
    / `per_asin_zip` 这两个入参就结束了，往下共用本函数。

    这里是**共享层**而不是复制粘贴，理由很具体：撞名 409 那段（见下）是靠一堆
    互相咬合的性质成立的——响应体里带既有 batch_id 才能让调用方接着轮询、回显
    读回值而不是请求值才不会对 callback 撒谎。这些性质抄第二遍必然会漂，而漂了
    之后**两个端点都不报错**，只是行为不同。

    入参 `unique_asins` 必须已经去重且保序（谁调谁负责），本函数不再去一次。
    """
    _s = _srv()

    if not batch_name:
        batch_name = _s._batch_name("batch")     # P4.7：精度不变（本来就是秒）

    # callback_url 校验（防 SSRF + 格式）
    cb_url = (callback_url or "").strip() or None
    if cb_url:
        ok, reason = await _s._is_safe_callback_url(cb_url)
        if not ok:
            raise HTTPException(400, f"非法 callback_url（{reason}）。仅接受 http(s)://公网域名/IP")

    ext_id = (external_id or "").strip()[:120] or None  # 上限 120 字符防滥用

    zc = zip_code or _s._runtime_settings.get("zip_code", config.DEFAULT_ZIP_CODE)

    # 构造 status URL（调用方可以轮询）。409 也要带上它，所以在建批次之前先算好。
    base = str(request.base_url).rstrip("/")
    status_url = f"{base}/api/batches/{batch_name}/status"

    batch_id, created = await _s.db.create_batch_if_absent(
        batch_name, needs_screenshot,
        external_id=ext_id, callback_url=cb_url,
        expand_variants=expand_variants,
    )
    if not created:
        # 撞名 -> 409，**绝不静默合并**。
        #
        # 以前这里是 200 + 既有 batch_id，实测后果（Phase 4.7 已证明撞名不是
        # no-op，本轮修的就是它）：
        #   * 本次的新 ASIN 被悄悄塞进上一个批次，「一次采集」的语义破掉；
        #   * `inserted` 在部分重叠时是非零，看起来像成功新建；
        #   * **本次的 external_id / callback_url 被静默丢弃**（INSERT OR IGNORE
        #     整行不插，既有行一个字段都不更新），而响应回显的是**请求**里的值 ——
        #     调用方以为回调注册好了，回调永远不会触发。
        #
        # 为什么是 409，而不是「自动加后缀」或「200 加个 merged 标志」：只有让
        # 撞名变成一个**可识别的失败**，上传/推送才是可安全重试的 —— 网络超时后
        # 重发，上一次若其实成功了，这里回的是 409 + 那个 batch_id。
        # 自动加后缀会让重试造出第二个批次，200+标志则要求每个调用方都记得读那个
        # 标志，漏读的代价与今天一模一样。
        #
        # 注意 batch_id 是**既有批次**的 id（create_batch_if_absent 撞名时照样把它
        # SELECT 回来），调用方可以直接拿 status_url 接着轮询，不必再查一次。
        raise HTTPException(409, {
            "error": _s._BATCH_NAME_CONFLICT_CODE,
            "message": f"批次名已存在: {batch_name}（未合并，也未改动既有批次）",
            "batch_id": batch_id,
            "batch_name": batch_name,
            "status_url": status_url,
        })

    inserted = await _s.db.create_tasks(
        batch_id, unique_asins, zc, needs_screenshot,
        per_asin_zip=per_asin_zip,
    )

    # 回显**存下来的**值，不是请求里的值。
    # 撞名已经在上面 409 掉了，所以走到这里两者必然相等——但「回显请求值」这个
    # 写法本身就是上面那个回调撒谎 bug 的载体，留着它等于把地雷埋回去。
    # 读回一行的代价（一次主键级 SELECT）远小于「回调注册成功了吗」这种问题。
    stored = await _s.db.get_batch_by_name(batch_name)
    return {
        "batch_id": batch_id,
        "batch_name": batch_name,
        "external_id": stored.get("external_id") if stored else ext_id,
        "total_asins": len(unique_asins),
        "inserted": inserted,
        "per_asin_zip_count": len(per_asin_zip),
        "invalid_zip_rows": invalid_zip_count,
        "callback_url": stored.get("callback_url") if stored else cb_url,
        "status_url": status_url,
    }


@router.post("/api/upload")
async def api_upload(request: Request,
                     file: UploadFile = File(...),
                     batch_name: str = Form(None),
                     zip_code: str = Form(None),
                     needs_screenshot: bool = Form(False),
                     callback_url: str = Form(None),
                     external_id: str = Form(None),
                     expand_variants: bool = Form(False)):
    """上传 ASIN 文件创建批次。

    支持 xlsx / csv / txt 三种格式：
    - **xlsx / csv**：A 列 = ASIN，B 列（可选）= 该 ASIN 单独的采集邮编（5 位数字）
      B 列为空 → 用本次上传指定的 batch zip（或服务端默认）
    - **txt**：每行一个 ASIN（无邮编列）

    可选 callback：
    - `callback_url`：批次完成时（含截图）POST 到此 URL 通知调用方
    - `external_id`：调用方自己的批次 ID，原样回传，便于追踪

    批次名冲突 → **409 Conflict**，绝不静默合并进已有批次。409 的响应体里带
    `detail.batch_id` / `detail.batch_name` / `detail.status_url`，调用方可以
    直接拿去接着轮询。

    由此得到的三条性质（调用方可以依赖）：
    - **本端点可以安全重试**：网络超时后重发，若上一次其实成功了，拿到的是
      409 + 那个既有 batch_id，而不是又建一个批次、也不会悄悄并进去。
    - 批次名不需要毫秒精度去躲开合并。
    - **200 恒等于「新建了一个批次」**，`inserted` 因此不再有歧义。

    200 响应里的 `external_id` / `callback_url` 回显的是**批次实际存下来的**值
    （读回后再回显），不是请求里的值。
    """
    _s = _srv()
    content = await file.read()
    if len(content) > _s.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大：{len(content)//1024//1024}MB，上限 {_s.MAX_UPLOAD_BYTES//1024//1024}MB")
    filename = file.filename or ""

    asins: List[str] = []                # 顺序、可重复（去重在后面）
    per_asin_zip: Dict[str, str] = {}    # 仅记录 B 列指定的邮编
    invalid_zip_count = 0

    def add_pair(asin_val, zip_val):
        nonlocal invalid_zip_count
        asin = _s._normalize_asin(asin_val)
        if not asin:
            return
        asins.append(asin)
        if zip_val is not None and str(zip_val).strip():
            zip_norm = _s._normalize_zip(zip_val)
            if zip_norm:
                # 同一 ASIN 在多行重复时，以第一次出现的非空 zip 为准
                per_asin_zip.setdefault(asin, zip_norm)
            else:
                invalid_zip_count += 1

    if filename.endswith(".xlsx"):
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        try:
            ws = wb.active
            for row in ws.iter_rows(min_row=1, values_only=True):
                if not row:
                    continue
                a = row[0] if len(row) > 0 else None
                b = row[1] if len(row) > 1 else None
                # 兼容旧上传：当 A 列不是 ASIN 时，扫描该行所有列找 ASIN（不带 zip）
                if _s._normalize_asin(a):
                    add_pair(a, b)
                else:
                    for cell in row:
                        if _s._normalize_asin(cell):
                            add_pair(cell, None)
        finally:
            wb.close()
    elif filename.endswith(".csv"):
        text = content.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                continue
            a = row[0] if len(row) > 0 else None
            b = row[1] if len(row) > 1 else None
            if _s._normalize_asin(a):
                add_pair(a, b)
            else:
                for cell in row:
                    if _s._normalize_asin(cell):
                        add_pair(cell, None)
    else:
        text = content.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            add_pair(line, None)

    if not asins:
        raise HTTPException(400, "未找到有效 ASIN")

    # 去重（保持顺序）
    seen = set()
    unique_asins = []
    for a in asins:
        if a not in seen:
            unique_asins.append(a)
            seen.add(a)

    return await _create_batch_with_tasks(
        request, unique_asins, per_asin_zip, invalid_zip_count,
        batch_name=batch_name, zip_code=zip_code,
        needs_screenshot=needs_screenshot, callback_url=callback_url,
        external_id=external_id, expand_variants=expand_variants,
    )


@router.post("/api/batches")
async def api_create_batch(request: Request):
    """**推送采集任务（JSON）** —— 和 `POST /api/upload` 等价，只是不用传文件。

    给程序化调用方用：上游系统手里本来就是一个 ASIN 列表，为了调这个接口先拼
    一个 xlsx 再 multipart 上传是纯粹的仪式。响应体、409 撞名语义、回调注册，
    与 `/api/upload` **完全一致**（同一份实现）。

    请求体：

        {
          "asins": ["B0XXXXXXX1", "B0XXXXXXX2"],   // 与 items 二选一
          "items": [                                // 需要逐个 ASIN 指定邮编时用
            {"asin": "B0XXXXXXX1", "zip_code": "10001"},
            {"asin": "B0XXXXXXX2"}                  // 不写 -> 用下面的 zip_code
          ],
          "zip_code":         "90001",   // 可选。不传 = 用服务端默认邮编
          "needs_screenshot": false,     // 可选，默认 false
          "batch_name":       null,      // 可选，不传则自动生成
          "callback_url":     null,      // 可选，批次完成时 POST 通知
          "external_id":      null,      // 可选，原样回传
          "expand_variants":  false      // 可选
        }

    两个「自由选择」按调用方要的语义来：

    * **邮编**：三档独立。`items[].zip_code`（这一个 ASIN）> 顶层 `zip_code`
      （这一批）> 服务端默认。想跟随服务端默认就整个不传这个键——传 `null` 也
      当没传，但**不要**传 `""` 去表达「用默认」，空串这里按缺省处理、别的地方
      未必，少一层歧义是一层。
    * **截图**：`needs_screenshot` 是**批次级**开关（底层 `batches` 表就一列，
      截图任务是按批派生的）。不传即 false，也就是**默认不截图**。想要一半截图
      一半不截，推两个批次——不要指望 items 里逐个开关，那个粒度库里不存在。

    `asins` 与 `items` 可以同时给（合并，`items` 的邮编优先）。ASIN 去重保序，
    非法 ASIN 直接丢弃；一个都不剩 -> 400。非法邮编不拦请求，计入响应的
    `invalid_zip_rows`，那些 ASIN 退回用批次邮编。
    """
    _s = _srv()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")

    asins: List[str] = []
    per_asin_zip: Dict[str, str] = {}
    invalid_zip_count = 0

    def add_pair(asin_val, zip_val):
        nonlocal invalid_zip_count
        asin = _s._normalize_asin(asin_val)
        if not asin:
            return
        asins.append(asin)
        if zip_val is not None and str(zip_val).strip():
            zip_norm = _s._normalize_zip(zip_val)
            if zip_norm:
                # 同一 ASIN 给了**两个不同**邮编 -> 400，不静默取第一个。
                #
                # 库里 `tasks` 是 UNIQUE(batch_id, asin)，一个批次里同一个 ASIN
                # 只能有一条任务、也就只能有一个邮编 —— 这个诉求在**一个批次内**
                # 根本表达不了。/api/upload 那边靠 `setdefault` 悄悄取第一个，
                # 调用方拿到 200、少采了一个邮编，而且响应里看不出来。
                # 这里明确失败并告诉他们怎么做：一个邮编一个批次。
                prev = per_asin_zip.get(asin)
                if prev is not None and prev != zip_norm:
                    raise HTTPException(400, {
                        "error": "conflicting_zip_for_asin",
                        "message": (
                            f"同一批次里 {asin} 被指定了两个邮编"
                            f"（{prev} 与 {zip_norm}）。一个批次里一个 ASIN 只能有"
                            f"一个邮编——同一 ASIN 要采多个邮编，请**一个邮编推一个"
                            f"批次**（批次名带上邮编），这样数据与截图都按批次分开。"),
                        "asin": asin,
                        "zip_codes": [prev, zip_norm],
                    })
                per_asin_zip.setdefault(asin, zip_norm)
            else:
                invalid_zip_count += 1

    raw_asins = body.get("asins") or []
    if not isinstance(raw_asins, list):
        raise HTTPException(400, "asins 必须是数组")
    for a in raw_asins:
        add_pair(a, None)

    raw_items = body.get("items") or []
    if not isinstance(raw_items, list):
        raise HTTPException(400, "items 必须是数组")
    for it in raw_items:
        if isinstance(it, dict):
            add_pair(it.get("asin"), it.get("zip_code"))
        else:
            # 允许 items 里直接写字符串 ASIN——调用方混着写不该是个错误
            add_pair(it, None)

    if not asins:
        raise HTTPException(400, "未找到有效 ASIN")

    # 去重（保持顺序）
    seen = set()
    unique_asins = []
    for a in asins:
        if a not in seen:
            unique_asins.append(a)
            seen.add(a)

    zip_code = body.get("zip_code")
    zip_code = str(zip_code).strip() if zip_code is not None else None
    if zip_code:
        zip_norm = _s._normalize_zip(zip_code)
        if not zip_norm:
            raise HTTPException(400, f"非法邮编: {zip_code}")
        zip_code = zip_norm
    else:
        # "" 与 null 同义：跟随服务端默认
        zip_code = None

    batch_name = body.get("batch_name")
    batch_name = str(batch_name).strip() if batch_name else None
    if batch_name and _s._safe_fs_component(batch_name) is None:
        # 批次名会拼成截图目录名，非法值必须在建批次**之前**挡掉，
        # 否则批次建好了、截图却永远落不了盘。
        raise HTTPException(400, "非法批次名")

    return await _create_batch_with_tasks(
        request, unique_asins, per_asin_zip, invalid_zip_count,
        batch_name=batch_name,
        zip_code=zip_code,
        needs_screenshot=bool(body.get("needs_screenshot", False)),
        callback_url=body.get("callback_url"),
        external_id=body.get("external_id"),
        expand_variants=bool(body.get("expand_variants", False)),
    )


@router.get("/api/batches")
async def api_batches():
    batches = await _srv().db.get_batches()
    return {"batches": batches}


@router.get("/api/progress")
async def api_progress(batch_id: int = None):
    return await _srv().db.get_progress(batch_id)


@router.get("/api/batches/{batch_name}/screenshots/progress")
async def api_screenshot_progress(batch_name: str):
    db = _srv().db
    batch = await db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(404, f"批次不存在: {batch_name}")
    return await db.get_screenshot_progress(batch["id"])


@router.get("/api/batches/{batch_name}/status")
async def api_batch_status(batch_name: str):
    """轮询批次完成状态（兼容旧调用方 + 给纯轮询模式用）。

    返回结构：
        {
          "batch_name": ...,
          "batch_id": ...,
          "external_id": ...,
          "status": "running"|"completed"|"failed",
          "stats": {total, done, failed, success_rate, duration_seconds},
          "screenshots": {total, done, failed},
          "completed_at": null|"2026-...",
          "callback": {url, status, attempts, last_error, sent_at}
        }
    """
    _s = _srv()
    batch = await _s.db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(404, f"批次不存在: {batch_name}")
    snap = await _s.db.get_batch_completion_status(batch["id"])
    t = snap["tasks"]
    s = snap["screenshots"]
    total = t["total"] or 0
    success_rate = (t["done"] / total) if total > 0 else 0.0

    # 持续时长（创建到完成；未完成则到当前时间）
    duration = None
    try:
        created_at = batch.get("created_at")
        end_at = batch.get("completed_at") or now_ts()
        if created_at:
            start = datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
            stop = datetime.strptime(end_at[:19], '%Y-%m-%d %H:%M:%S')
            duration = int((stop - start).total_seconds())
    except Exception:
        _s.logger.debug("计算批次耗时失败（duration 置空）", exc_info=True)

    return {
        "batch_name": batch_name,
        "batch_id": batch["id"],
        "external_id": batch.get("external_id"),
        "status": batch.get("status") or "running",
        "stats": {
            "total": total,
            "done": t["done"] or 0,
            "failed": t["failed"] or 0,
            "open": t["open"] or 0,
            "success_rate": round(success_rate, 4),
            "duration_seconds": duration,
        },
        "screenshots": {
            "total": s["total"] or 0,
            "done": s["done"] or 0,
            "failed": s["failed"] or 0,
            "open": s["open"] or 0,
        },
        "completed_at": batch.get("completed_at"),
        "callback": {
            "url": batch.get("callback_url"),
            "status": batch.get("callback_status"),
            "attempts": batch.get("callback_attempts") or 0,
            "last_error": batch.get("callback_last_error"),
            "sent_at": batch.get("callback_sent_at"),
            "next_retry_at": batch.get("callback_next_retry_at"),
        },
    }


@router.post("/api/batches/{batch_name}/callback/retry")
async def api_batch_callback_retry(batch_name: str):
    """运维手动触发：重置该批次的 callback 状态，立即重新发送。
    可用于：webhook 失败 5 次后；或调用方端点恢复后想重新接收一次通知。
    """
    _s = _srv()
    batch = await _s.db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(404, f"批次不存在: {batch_name}")
    if not batch.get("callback_url"):
        raise HTTPException(400, "该批次没有配置 callback_url")
    changed = await _s.db.reset_callback_for_retry(batch["id"])
    # 入队让 dispatcher 立刻处理。
    # 走 _s._callback_send_queue（属性访问）而不是 from-import：这个名字在
    # lifespan 里才被赋值（`global db, _callback_send_queue`），导入期取到的是 None。
    if _s._callback_send_queue is not None:
        try:
            _s._callback_send_queue.put_nowait(batch["id"])
        except Exception:
            _s.logger.warning(f"callback 重发入队失败（忽略）: batch_id={batch['id']}", exc_info=True)
    return {"ok": changed, "batch_id": batch["id"]}


@router.post("/api/batches/{batch_id}/prioritize")
async def api_prioritize(batch_id: int):
    await _srv().db.prioritize_batch(batch_id)
    return {"ok": True}


@router.post("/api/batches/{batch_name}/retry")
async def api_retry_batch(batch_name: str, force: bool = False):
    """重试失败任务

    始终跳过 NO_AUTO_RETRY_ERROR_TYPES（如 variant_offset），因为这些类型
    是 Amazon 侧返回兄弟变体页的稳定问题，不再重试。
    返回 retried/skipped_no_retry 数量，前端可展示。
    """
    from common.core import NO_AUTO_RETRY_ERROR_TYPES
    db = _srv().db
    batch = await db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(404, f"批次不存在: {batch_name}")
    batch_id = batch["id"]

    # 排除清单在**这里**算好再传下去：它是本端点的策略（"始终跳过
    # variant_offset"），而且要原样回显在响应体里。db 层只负责按清单执行。
    no_retry_list = sorted(NO_AUTO_RETRY_ERROR_TYPES)

    # force 参数保留兼容旧调用，但不覆盖 NO_AUTO_RETRY_ERROR_TYPES。
    res = await db.retry_failed_tasks(batch_id, no_retry_list)
    return {
        "ok": True,
        "retried": res["retried"],
        "skipped_no_retry": res["skipped"],
        "no_retry_types": no_retry_list,
        "forced": force,
    }


@router.delete("/api/batches/{batch_name}")
async def api_delete_batch(batch_name: str):
    """删除批次及其任务"""
    _s = _srv()
    batch = await _s.db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(404, f"批次不存在: {batch_name}")
    batch_id = batch["id"]
    screenshot_files = await _s.db.delete_batches([batch_id])

    # 删除物理截图文件
    _s._remove_screenshot_files(screenshot_files)
    return {"ok": True}


@router.post("/api/batches/delete-bulk")
async def api_delete_batches_bulk(request: Request):
    """批量删除多个批次（按 batch_id）及其全部关联数据 + 截图文件。
    入参 JSON：{"batch_ids": [1,2,3]}。一次事务删除，原子性。"""
    _s = _srv()
    body = await request.json()
    raw = body.get("batch_ids", [])
    if not isinstance(raw, list):
        raise HTTPException(400, "batch_ids 必须是数组")
    # 仅接受整数 id，去重，上限保护（防超大 IN 子句）
    seen = set()
    batch_ids = []
    for x in raw:
        try:
            i = int(x)
        except (ValueError, TypeError):
            continue
        if i not in seen:
            seen.add(i)
            batch_ids.append(i)
    batch_ids = batch_ids[:500]
    if not batch_ids:
        raise HTTPException(400, "batch_ids 为空或无效")

    screenshot_files = await _s.db.delete_batches(batch_ids)

    _s._remove_screenshot_files(screenshot_files)
    _s.logger.info(f"批量删除批次: {len(batch_ids)} 个 (ids={batch_ids[:20]}{'...' if len(batch_ids) > 20 else ''})")
    return {"ok": True, "deleted": len(batch_ids)}


@router.get("/api/batches/{batch_id}/failures")
async def api_batch_failures(
    batch_id: int,
    error_type: Optional[str] = Query(None, description="逗号分隔的 error_type 过滤"),
    limit: int = Query(100000, ge=1, le=100000),
):
    """按 batch_id 获取失败任务明细；不依赖批次名，且不截断到 200 条。"""
    error_types = None
    if error_type:
        error_types = [t.strip() for t in error_type.split(",") if t.strip()]
    failed_tasks = await _srv().db.get_batch_failures(
        batch_id, error_types=error_types, limit=limit)
    return {"batch_id": batch_id, "failed_tasks": failed_tasks, "count": len(failed_tasks)}
