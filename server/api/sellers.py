"""F-009 卖家店铺采集（4 个端点）—— Phase 3.4 从 `server/app.py` 拆出。

    POST /api/upload-sellers
    GET  /api/seller-batches/{batch_id}/progress
    GET  /api/seller-batches/{batch_id}/discoveries
    POST /api/tasks/seller-result          （原本在 app.py 的 worker 节下）

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **归域是逐端点判定的，不能信节头。** `app.py` 里「F-009 卖家店铺采集」
   节头之后只有前 ~130 行真属于卖家采集；`@app.get("/api/batches")` 往下
   300+ 行是被挤下来的批次 / worker / 设置端点，**不归本模块**。
   反过来，真正的卖家结果提交端点 `api_submit_seller_result` 却待在
   worker 节下 —— 它跟着域走，一起搬过来。
   跨节搬运不改路由匹配：`/api/tasks/seller-result` 是静态路径，
   worker 队列域里没有 `/api/tasks/{x}` catch-all（那边六条全是静态路径）。

2. **本步没有黄金网。** 4 个端点在黄金 78 步里一步都没有，而且补不进去：
   `/api/upload-sellers` 的响应含 `sellers_{%Y%m%d_%H%M%S}` 批次名，
   不被 `harness.py` 的 `_TS_RE` 覆盖，逐次不同。
   替代网是 `tests/test_seller_api.py`（普通 pytest，非黄金）：显式传
   `batch_name` 消掉那个唯一的非确定源，把 upload → pull → seller-result →
   progress → discoveries 的串联钉死，两个后端都跑。

3. **模块级可变全局一个都不搬**：`db` / `_runtime_settings` / `_worker_registry`
   留在 `server/app.py`，这里一律 `_srv().xxx`（`MAX_UPLOAD_BYTES` 与 `_cn_now`
   同样走 `_srv()`，保持单一来源）。
   from-import 会把值快照下来，而黄金夹具按名字给 `server.app` 打补丁、
   PG 夹具还 `monkeypatch.setattr(srv, "db", pgdb)`。

4. **router 光秃**（`APIRouter()`，不带 `tags=` / `prefix=`），
   **函数名 / docstring / 路径一个字不改** —— `/openapi.json` 逐字节钉死。

5. `api_seller_discoveries` 的 f-string 拼 WHERE 与 `db.read()` **原文照搬**：
   它走的是只读池而不是裸 `db._db`，`?` 占位符由 `common/pgdb` 侧翻译。
"""

import io
import re
from typing import Any, List, Optional

import openpyxl
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from common import config


def _srv():
    from server import app as _s
    return _s


router = APIRouter()


# ==================== F-009: 卖家店铺采集 ====================

# Amazon 三方卖家 ID 通常是 13-14 位 A 开头的字母数字串（如 A2L77EE7U53NWQ）。
# 同时支持从 URL 中提取 me=... / seller=... 参数。
_SELLER_URL_RE = re.compile(r'(?:[?&](?:me|seller)=)([A-Z0-9]{10,16})', re.IGNORECASE)
_BARE_SELLER_RE = re.compile(r'^A[A-Z0-9]{12,14}$')


def _extract_sellers_from_text(text: str) -> List[str]:
    """从一段文本中提取所有 seller_id（去重，保持出现顺序）。

    支持 3 种形式：
      1. 完整 URL: https://www.amazon.com/s?me=A2L77EE7U53NWQ
      2. URL 片段: ?me=A2L77... 或 ?seller=A2L77...
      3. 裸 ID: A2L77EE7U53NWQ
    """
    seen = set()
    out = []
    for line in text.splitlines():
        candidates = []
        for m in _SELLER_URL_RE.finditer(line):
            candidates.append(m.group(1).upper())
        for tok in re.split(r'[\s,;\t]+', line):
            tok = tok.strip().upper()
            if _BARE_SELLER_RE.match(tok):
                candidates.append(tok)
        for sid in candidates:
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


@router.post("/api/upload-sellers")
async def api_upload_sellers(request: Request,
                              file: UploadFile = File(...),
                              batch_name: str = Form(None),
                              discover_mode: str = Form("with_detail"),
                              zip_code: str = Form(None),
                              needs_screenshot: bool = Form(False)):
    """上传卖家 ID/URL 文件，创建一个 seller_discovery 批次。

    discover_mode:
      - 'discover_only': 仅翻页发现 ASIN，写入 seller_discoveries 表
      - 'with_detail':   发现后自动衍生 ASIN 详情任务进入主采集队列
    """
    if discover_mode not in ("discover_only", "with_detail"):
        raise HTTPException(400, f"非法 discover_mode: {discover_mode}")

    max_upload_bytes = _srv().MAX_UPLOAD_BYTES
    content = await file.read()
    if len(content) > max_upload_bytes:
        raise HTTPException(413, f"文件过大：{len(content)//1024//1024}MB，上限 {max_upload_bytes//1024//1024}MB")
    filename = (file.filename or "").lower()

    seller_ids: List[str] = []
    if filename.endswith(".xlsx"):
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        try:
            ws = wb.active
            buf = []
            for row in ws.iter_rows(min_row=1, values_only=True):
                for cell in row:
                    if cell:
                        buf.append(str(cell))
            seller_ids = _extract_sellers_from_text("\n".join(buf))
        finally:
            wb.close()
    elif filename.endswith(".csv"):
        text = content.decode("utf-8", errors="ignore")
        seller_ids = _extract_sellers_from_text(text)
    else:
        text = content.decode("utf-8", errors="ignore")
        seller_ids = _extract_sellers_from_text(text)

    if not seller_ids:
        raise HTTPException(400, "未识别到任何 seller ID（支持裸 ID、含 me=/seller= 的 URL）")

    if not batch_name:
        # P4.7：批次名的唯一构造点在 server/app.py:_batch_name（精度不变，本来就是秒）。
        # 走 _srv() 属性访问，与 _cn_now / MAX_UPLOAD_BYTES 同约定（本模块承重约束）。
        batch_name = _srv()._batch_name("sellers")

    zc = zip_code or _srv()._runtime_settings.get("zip_code", config.DEFAULT_ZIP_CODE)
    # 本入口只建美国站卖家批次；「同名 = 合并」不能把美国任务并进一个别的站点的
    # 批次（F-012：一个批次只属于一个站点）。美国批次之间的合并语义不变。
    await _srv()._reject_cross_site_batch(batch_name, _srv()._resolve_marketplace(None))
    batch_id, inserted = await _srv().db.create_seller_batch(
        name=batch_name,
        seller_ids=seller_ids,
        discover_mode=discover_mode,
        zip_code=zc,
        needs_screenshot=needs_screenshot,
    )
    if not batch_id:
        raise HTTPException(500, "创建卖家批次失败")

    return {
        "batch_id": batch_id,
        "batch_name": batch_name,
        "discover_mode": discover_mode,
        "total_sellers": len(seller_ids),
        "inserted_tasks": inserted,
    }


@router.get("/api/seller-batches/{batch_id}/progress")
async def api_seller_batch_progress(batch_id: int):
    """seller_discovery 批次专属进度端点：discover + detail + 已发现 ASIN 数。"""
    return await _srv().db.get_seller_batch_progress(batch_id)


@router.get("/api/seller-batches/{batch_id}/discoveries")
async def api_seller_discoveries(batch_id: int,
                                  seller_id: Optional[str] = None,
                                  limit: int = 200,
                                  offset: int = 0):
    """列出某批次发现的 ASIN（可按 seller_id 过滤）。"""
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    where = ["batch_id = ?"]
    params: List[Any] = [batch_id]
    if seller_id:
        where.append("seller_id = ?")
        params.append(seller_id.strip().upper())
    sql = (
        "SELECT seller_id, asin, list_title, list_price, list_image, discovered_at "
        f"FROM seller_discoveries WHERE {' AND '.join(where)} "
        "ORDER BY discovered_at DESC, asin ASC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    rows = []
    async with _srv().db.read() as rc, rc.execute(sql, params) as c:
        async for r in c:
            rows.append(dict(r))
    return {"items": rows, "limit": limit, "offset": offset}


# 下面这一个原本待在 app.py 的「Worker 任务拉取和提交」节下 ——
# 节头骗人，它按域属于 F-009。路径是静态的，换模块不改匹配。

@router.post("/api/tasks/seller-result")
async def api_submit_seller_result(request: Request):
    """接收 worker 的 discover_seller 任务结果（F-009）。

    Payload: {task_id, batch_id, worker_id, lease_epoch, seller_id, items, meta}
    """
    body = await request.json()
    task_id = body.get("task_id")
    batch_id = body.get("batch_id")
    worker_id = body.get("worker_id", "")
    lease_epoch = body.get("lease_epoch", 0)
    seller_id = (body.get("seller_id") or "").strip().upper()
    items = body.get("items") or []
    meta = body.get("meta") or {}

    if not task_id or not seller_id:
        raise HTTPException(400, "task_id 和 seller_id 必填")

    result = await _srv().db.accept_seller_discovery_result(
        task_id=task_id,
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        batch_id=batch_id,
        seller_id=seller_id,
        items=items,
        meta=meta,
    )
    if worker_id in _srv()._worker_registry and result.get("accepted"):
        _srv()._worker_registry[worker_id]["results_submitted"] += 1
    return result
