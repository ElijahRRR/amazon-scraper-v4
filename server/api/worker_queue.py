"""Worker 任务队列：拉取 / 释放 / 结果提交 / 截图（6 个端点）—— Phase 3.5 从
`server/app.py` 拆出。

    GET  /api/tasks/pull
    POST /api/tasks/release
    POST /api/tasks/result
    POST /api/tasks/result/batch
    POST /api/tasks/screenshot
    POST /api/tasks/screenshot/fail

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **模块级可变全局一个都不搬。** `db` / `_worker_registry` /
   `_completion_check_set` 全部留在 `server/app.py`，这里一律 `_srv().xxx`
   属性访问，**禁止 from-import**。
   `tests/golden/harness.py` 按名字给 `server.app` 打补丁（每个样本前
   `srv._worker_registry.clear()` 等），三个 PG 夹具还
   `monkeypatch.setattr(srv, "db", pgdb)` —— 名字搬走 = 补丁打空 = 样本漂移。

2. **`_completion_check_set` 的 HTTP 层写点全部收口在
   `_mark_for_completion_check()` 一个函数里**，其余读写都在 `app.py` 的
   `_completion_watcher` 后台协程里。
   它必须写成 `_s._completion_check_set.add(...)`：from-import 拿到的是
   同一个 set 对象没错，但夹具会**整体替换**这个属性，快照下来的旧 set
   之后没人读，完成通知静默失效。

   曾经只有 `api_submit_batch` 一处入队，于是「任务采完但截图还没完」的
   批次要靠 `_timeout_loop` 那个 30 秒 / `LIMIT 30` 的兜底扫描才收尾 ——
   而 `LIMIT 30` 是硬截断，同时 running 的批次超过 30 个时后面的会被饿死。
   现在**每一个可能让批次完成的写点**都入队：两个结果端点 + 截图 done/fail。
   收口成一个函数是为了让"又加了一个写点却忘了入队"这件事有个显眼的去处。

3. **`_register_worker` / `_rollback_quietly` / `_normalize_asin` /
   `_safe_fs_component` 留在 `app.py`**（前两个直接读写全局，后两个另有
   调用点），这里走 `_srv().xxx(...)`。

4. `api_release_tasks` 的旧格式 `task_ids` 分支（裸 `db._write_lock` +
   `db._db.execute`）**本步原文照搬，一个字不改**。它是不是该删是 X.1 的
   独立决策，不在搬运这一步里顺手做 —— 搬运步只允许「位置变了、行为没变」
   这一种 diff。

5. **router 光秃**：`APIRouter()`，不带 `tags=` / `prefix=` /
   `include_in_schema`。`/openapi.json` 是黄金基线的一步、逐字节钉死，
   而整份 schema 里没有 `tags` 键。

6. **函数名 / docstring / 路径一个字不改** —— 它们被编码进 `operationId` /
   `summary` / `description` / `Body_*` schema 名。

7. **路由匹配不受影响**：本节六条全是静态路径，`/api/tasks/` 下没有任何
   `/api/tasks/{x}` catch-all（`/api/tasks/seller-result` 也是静态的，
   它在 Phase 3.4 按域搬去了 `sellers.py`）。
"""

import os
import time
from common.core.timeutil import now_ts
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile

from common import config
from common.core import error_types


def _srv():
    from server import app as _s
    return _s


router = APIRouter()


# ==================== API: Worker 任务拉取和提交 ====================

@router.get("/api/tasks/pull")
async def api_pull_tasks(request: Request,
                         worker_id: str = Query(...),
                         count: int = Query(10),
                         needs_screenshot: Optional[bool] = Query(None),
                         enable_screenshot: Optional[bool] = Query(None),
                         prefer_zip: Optional[str] = Query(None)):
    _s = _srv()
    ip = request.client.host if request.client else None
    # enable_screenshot 来自 worker 的 query param，更新 registry
    _s._register_worker(worker_id, enable_screenshot=enable_screenshot, ip=ip)

    # ns=None: 不限制; ns=False: 只拉不需要截图的任务
    # enable_screenshot 表示 worker 是否有截图能力：
    #   True  → 拉所有任务 (ns=None)
    #   False → 只拉不需要截图的 (ns=False)
    ns = None
    if needs_screenshot is not None:
        ns = needs_screenshot
    elif enable_screenshot is not None:
        if not enable_screenshot:
            ns = False
        # enable_screenshot=True → ns=None (拉所有任务)
    elif worker_id in _s._worker_registry:
        if not _s._worker_registry[worker_id].get("enable_screenshot", True):
            ns = False

    # prefer_zip 校验（防注入）+ 归一化。
    #
    # P4.6：原先这里是**内联的第二份**规则（一条只认整 5 位数字的正则），
    # 与 `server/app.py:_normalize_zip` 分叉，后果是一次**静默降级**：
    # worker 传 ``prefer_zip="1001"``（整数往返丢掉前导零，relay 侧的
    # ``normalize_zip`` 正是为这个形状而写）时，`_normalize_zip` 会 zfill 成
    # ``"01001"`` 接受，而这里直接判 None 丢弃 —— 任务分配悄悄退回
    # 「不挑邮编」，worker 每个任务都要切一次 session，没有任何一侧会报错。
    #
    # 顺带堵掉「正则用 ``$`` 收尾」允许**尾随换行**这个小口子
    # （Python 的 ``$`` 在末尾一个 ``\n`` 处也匹配）：`_normalize_zip` 先
    # ``.strip()`` 再用 ``\Z`` 收尾，防注入强度不降反升。
    #
    # ⚠ **受理面还拓宽了三种形状，一并声明**（Phase 4.5-4.8 审计发现原注释漏了）：
    #   "90001.0"     HEAD 丢弃 -> 现在 '90001'  （Excel 把邮编读成浮点的老毛病）
    #   "10001-1234"  HEAD 丢弃 -> 现在 '10001'  （ZIP+4，取前段）
    #   "1"           HEAD 丢弃 -> 现在 '00001'  （单个数字也补成合法邮编）
    # 这三种是「换成 _normalize_zip」的必然副产物，不是额外加的逻辑。
    # 方向上说得通：它们最终都要过 `_US_ZIP_RE` 校验，实测放行值恒为
    # **恰好 5 位数字或 None**，所以防注入强度不降反升。
    # 但它们是行为改动，所以钉在 tests/test_prefer_zip_normalization.py 里，
    # 不靠这段注释自证。
    #
    # 走 ``_s._normalize_zip``（属性访问）而不是 from-import：本模块承重约束 1/3，
    # 名字必须留在 `server.app` 上，夹具按名字打补丁。
    pz = _s._normalize_zip(prefer_zip)

    tasks = await _s.db.pull_tasks(worker_id, count, ns, prefer_zip=pz)
    if worker_id in _s._worker_registry:
        _s._worker_registry[worker_id]["tasks_pulled"] += len(tasks)
    return {"tasks": tasks}


def _mark_for_completion_check(_s, batch_id, where: str) -> None:
    """把 batch_id 标记为「需要检查是否完成」。

    ------------------------------------------------------------------
    为什么每个"可能让批次完成"的写点都要调它
    ------------------------------------------------------------------
    一个批次算完成，要**任务采完 + 截图也完**（`get_batch_completion_status`）。
    在这之前只有 `api_submit_batch` 一处入队，于是：

      * 最后一张截图上传完成 -> 批次其实已经完成了，但没有人入队；
      * 只能等 `_timeout_loop` 那个 **30 秒一轮、`LIMIT 30`** 的兜底扫描。

    兜底扫描有两个问题，第二个是真的会丢：
      1. 完成通知/回调最坏晚 30 秒；
      2. `LIMIT 30` 是**硬截断** —— 同时 running 的批次超过 30 个时，
         排在后面的批次这一轮根本不会被检查。它按
         `updated_at DESC, id DESC` 取前 30，一个长期 running 的老批次
         会被新批次一直挤在窗口外，**饿死**。

    所以这里给每个写点补上入队。兜底扫描保留不动 —— 它守的是另一种场景
    （服务重启、内存 set 丢失）。

    ------------------------------------------------------------------
    ⚠ 必须 `_s._completion_check_set`，不能 from-import
    ------------------------------------------------------------------
    夹具会**整体替换** `server.app` 上这个属性（见本文件承重约束 1/2）。
    from-import 拿到的是快照下来的旧 set，之后没人读，完成通知静默失效。

    任何异常都吞掉只 log：通知机制绝不允许污染采集主路径 —— worker 提交
    结果/截图失败会触发重试，而重试解决不了"入队失败"这种问题。
    """
    if not batch_id:
        return
    try:
        _s._completion_check_set.add(batch_id)
    except Exception as e:                                     # noqa: BLE001
        _s.logger.warning(f"完成检测入队异常（{where}，不影响采集）: {e}")


@router.post("/api/tasks/release")
async def api_release_tasks(request: Request):
    _s = _srv()
    db = _s.db
    body = await request.json()
    worker_id = body.get("worker_id", "")
    tasks = body.get("tasks", [])
    # 兼容旧格式 {"task_ids": [1,2,3]}（无 lease 校验，直接释放）
    if not tasks and "task_ids" in body:
        task_ids = body["task_ids"]
        if task_ids:
            now = now_ts()
            placeholders = ",".join("?" * len(task_ids))
            async with db._write_lock:
                await db._db.execute("BEGIN")
                try:
                    cursor = await db._db.execute(
                        f"UPDATE tasks SET status='pending', worker_id=NULL, "
                        f"lease_epoch=lease_epoch+1, updated_at=? "
                        f"WHERE id IN ({placeholders}) AND status='processing'",
                        [now] + task_ids
                    )
                    await db._db.execute("COMMIT")
                except BaseException:
                    await _s._rollback_quietly(db._db)
                    raise
            return {"ok": True, "released": cursor.rowcount}
        return {"ok": True, "released": 0}
    released = await db.release_tasks(worker_id, tasks)
    return {"ok": True, "released": released}


@router.post("/api/tasks/result")
async def api_submit_result(request: Request):
    """提交单个结果（lease 校验 → 原子写入）"""
    _s = _srv()
    data = await request.json()
    task_id = data.pop("task_id", None)
    batch_id = data.pop("batch_id", None)
    worker_id = data.get("worker_id", "")
    lease_epoch = data.pop("lease_epoch", 0)
    error_types.normalize_fields(data)

    if task_id:
        if data.get("success", True):
            result = await _s.db.accept_success_result(task_id, worker_id, lease_epoch, data, batch_id)
        else:
            result = await _s.db.accept_failed_result(
                task_id, worker_id, lease_epoch,
                data.get("error_type", ""), data.get("error_detail", ""))
        if worker_id in _s._worker_registry and result.get("accepted"):
            _s._worker_registry[worker_id]["results_submitted"] += 1
        # 与 /api/tasks/result/batch 同样入队。这条是 worker 的**回退路径**
        # （批量接口连续失败时走 `_submit_batch_fallback`，worker/engine.py:2021），
        # 恰恰是批量接口不好使的时候才走到 —— 那时更不该让完成检测只剩兜底扫描。
        _mark_for_completion_check(_s, batch_id, "result")
        return {"ok": result.get("accepted", False), "stale": result.get("stale", False)}
    else:
        # 无 task_id 的直接写入（兼容）
        saved = await _s.db.save_result(data, batch_id)
        _mark_for_completion_check(_s, batch_id, "result(no-task_id)")
        return {"ok": saved, "stale": False}


@router.post("/api/tasks/result/batch")
async def api_submit_batch(request: Request):
    """批量提交结果（单事务，减少锁争用）"""
    _s = _srv()
    body = await request.json()
    results = body.get("results", [])

    # 构建 batch items
    batch_items = []
    worker_id_set = set()
    for item in results:
        task_id = item.pop("task_id", None)
        batch_id = item.pop("batch_id", None)
        worker_id = item.get("worker_id", "")
        lease_epoch = item.pop("lease_epoch", 0)
        is_success = item.pop("success", True)
        error_types.normalize_fields(item)
        worker_id_set.add(worker_id)
        batch_items.append({
            "task_id": task_id,
            "worker_id": worker_id,
            "lease_epoch": lease_epoch,
            "batch_id": batch_id,
            "data": item,
            "success": is_success,
        })

    result = await _s.db.accept_results_batch(batch_items)

    # results_submitted 只计 accepted
    for wid in worker_id_set:
        if wid in _s._worker_registry and result["accepted"] > 0:
            _s._worker_registry[wid]["results_submitted"] += result["accepted"]

    # 防御性入队：本次写入涉及的 batch_id 标记为"需要检查是否完成"。
    # set.add 是 O(1) 内存操作，不会影响 worker 提交响应。
    try:
        touched = {item["batch_id"] for item in batch_items if item.get("batch_id")}
    except Exception as e:                                     # noqa: BLE001
        _s.logger.warning(f"完成检测入队异常（result/batch 取 batch_id，不影响采集）: {e}")
        touched = set()
    for bid in touched:
        _mark_for_completion_check(_s, bid, "result/batch")

    return {**result, "total": len(results)}


# POST /api/tasks/seller-result 已搬到 server/api/sellers.py（Phase 3.4）——
# 它按域属于 F-009，不属于本节；路径是静态的，本节六条也全是静态路径，
# 没有 /api/tasks/{x} catch-all，换模块不改路由匹配。


@router.post("/api/tasks/screenshot")
async def api_upload_screenshot(request: Request,
                                asin: str = Form(...),
                                batch_name: str = Form(...),
                                worker_id: str = Form(""),
                                file: UploadFile = File(...)):
    """接收截图上传（检查 worker 存活状态）"""
    _s = _srv()
    # 拒绝已死 worker 的截图上传
    if worker_id and worker_id not in _s._worker_registry:
        raise HTTPException(409, f"Worker {worker_id} 已离线，截图被丢弃")
    if worker_id and worker_id in _s._worker_registry:
        if time.time() - _s._worker_registry[worker_id]["last_seen"] > 120:
            raise HTTPException(409, f"Worker {worker_id} 心跳超时，截图被丢弃")

    # 路径安全：asin / batch_name 会直接拼成磁盘路径，必须校验防穿越
    asin = _s._normalize_asin(asin)
    if not asin:
        raise HTTPException(400, "非法 ASIN")
    if _s._safe_fs_component(batch_name) is None:
        raise HTTPException(400, "非法批次名")

    batch = await _s.db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(400, f"批次不存在: {batch_name}")

    batch_id = batch["id"]
    save_dir = os.path.join(config.SCREENSHOT_DIR, batch_name)
    os.makedirs(save_dir, exist_ok=True)

    filename = f"{asin}.png"
    filepath = os.path.join(save_dir, filename)
    content = await file.read()
    # 单张截图上限 10MB，防止恶意/损坏文件耗尽磁盘
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, f"截图过大：{len(content)//1024//1024}MB，上限 10MB")
    with open(filepath, "wb") as f:
        f.write(content)

    rel_path = f"/static/screenshots/{batch_name}/{filename}"
    updated = await _s.db.update_screenshot_status(asin, batch_id, "done", file_path=rel_path)
    if not updated:
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise HTTPException(409, f"截图状态不存在: {asin}@{batch_name}")
    # 截图是批次完成的**第二个**条件（任务采完 + 截图也完）。最后一张截图
    # 落地往往就是批次真正完成的那一刻，不入队就只能等 30 秒兜底扫描。
    _mark_for_completion_check(_s, batch_id, "screenshot")
    return {"ok": True, "path": rel_path}


@router.post("/api/tasks/screenshot/fail")
async def api_screenshot_fail(request: Request):
    """截图渲染失败上报（触发重试或标记永久失败）"""
    _s = _srv()
    body = await request.json()
    asin = body.get("asin", "")
    batch_name = body.get("batch_name", "")
    error = body.get("error", "unknown")
    batch = await _s.db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(400, f"批次不存在: {batch_name}")
    updated = await _s.db.update_screenshot_status(asin, batch["id"], "failed", error=error)
    if not updated:
        raise HTTPException(409, f"截图状态不存在: {asin}@{batch_name}")
    # failed 与 done 一样是**终态**：它同样能让"截图还没完"变成"截图完了"。
    # 漏掉它，一个最后一张截图失败的批次要靠兜底扫描才收尾。
    _mark_for_completion_check(_s, batch["id"], "screenshot/fail")
    return {"ok": True}
