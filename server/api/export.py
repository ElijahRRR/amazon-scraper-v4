"""导出（4 个端点 + 两条流式实现）—— Phase 3.7 从 `server/app.py` 拆出。

    GET /api/export/fields
    GET /api/export/all
    GET /api/export/{batch_name}              ← catch-all，对不认识的名字回 404
    GET /api/export/{batch_name}/screenshots

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **本文件顶部那两行 `include_router` 是承重的，不是风格 —— 顺序局部化。**

   `/api/export/incremental`（`server/api/export_incremental.py`）落在下面
   `@router.get("/api/export/{batch_name}")` 这条 catch-all 的前缀里，
   而 Starlette 按**注册顺序**匹配。挪到 catch-all 之后，它会静默退化成
   404 `{"detail":"批次不存在: incremental"}` —— 而 404 正是 catalog_sync
   最容易读成「暂无数据」的码：游标永不推进，同步静默停摆，两侧都不报错。

   拆分前这条依赖跨文件：`app.py` 顶部 include 增量 router、几百行之后才
   定义 catch-all，靠「别动那一行的位置」维持。**现在把它局部化了**：
   增量 router 由本文件在**第一个 `@router.get` 之前**包进来，`app.py` 只
   include 本文件这一个 router。顺序从此由单文件自上而下阅读保证，
   `app.py` 的 include 列表怎么重排都打不破这条不变量。

   实测（fastapi 0.141.1）：`router.include_router(_incr.router)` 写在自己的
   `@router.get` 之前、再 `app.include_router(router)`，GET
   `/api/export/incremental` 命中增量 handler 而非 catch-all；
   `_incr.router` 上的 router 级 `include_in_schema=False` 在被父 router
   包含时保留，openapi paths 里只有 `/api/export/fields`、`/api/export/all`、
   `/api/export/{batch_name}`、`/api/export/{batch_name}/screenshots`。

   守卫三层，缺一层都不够（见 `tests/test_incremental_export.py`）：
     * 结构断言（递归展开 `_IncludedRouter` 后比索引）；
     * **行为断言**（真打一次，响应体不含「批次不存在」）—— 结构断言会被
       下一次 FastAPI 版本变化绕过，行为断言不会；
     * 源码断言（本文件里 `include_router(_incr.router)` 必须出现在第一个
       `@router.get` 之前）。
   第二份副本在 `tools/preflight.py:check_route_order`。

2. **模块级可变全局一个都不搬。** `db` / `logger` 留在 `server/app.py`，
   这里一律 `_srv().xxx`，**禁止 from-import**（PG 夹具
   `monkeypatch.setattr(srv, "db", pgdb)` 整体换掉这个属性）。
   `_cn_now` / `_safe_fs_component` 同理留在 `app.py`（另有调用点）。
   本文件自己带的 `BATCH_STATUS_EXPORT_HEADERS` / `_VARIANT_PAGE_ASIN_RE` /
   `_EXPORT_ROWS_PER_CHUNK` 是**常量**，不是被夹具打补丁的状态，可以搬。

3. **router 光秃**：`APIRouter()`，不带 `tags=` / `prefix=` /
   `include_in_schema`。`/openapi.json` 是黄金基线的一步、逐字节钉死，
   整份 schema 里没有 `tags` 键。（`_incr.router` 自带的
   `tags` + `include_in_schema=False` 是它自己的事，schema 里不出现。）

4. **函数名 / docstring / 路径一个字不改** —— 它们被编码进 `operationId` /
   `summary` / `description`。
"""

import asyncio
import csv
import io
import os
import re
import zipfile

import openpyxl
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from common import config
from common.models import EXPORTABLE_FIELDS
from common.core import _parse_price_float


def _srv():
    from server import app as _s
    return _s


router = APIRouter()

# ⚠ 下面这两行必须留在第一个 @router.get 之前 —— 承重约束 1。
# 挪到任何一条 @router.get 之后，/api/export/incremental 就被
# /api/export/{batch_name} 吞成 404「批次不存在: incremental」，
# 而消费方会把 404 读成「暂无数据」并永远不推进游标，两侧都不报错。
from server.api import export_incremental as _incr  # noqa: E402

router.include_router(_incr.router)


# ==================== API: 导出 ====================

BATCH_STATUS_EXPORT_HEADERS = [
    "本批采集结果",
    "数据来源",
    "失败类型",
    "失败详情",
    "实际页面ASIN",
    "重试次数",
    "本批任务更新时间",
    "产品库数据更新时间",
]

# ⚠ **这条正则是另一条规则，故意不与 common/core/idents.py:ASIN_RE 统一。**
# 别顺手改成 `from common.core.idents import ASIN_RE`——三处都不一样：
#   1. **搜索**语义（`\b...\b` 词边界），ASIN_RE 是**整串校验**（`^...$`）；
#   2. **不要求 B 前缀**（`[A-Z0-9]{10}`），ASIN_RE 是 `B` + 9 位；
#   3. 带 **re.IGNORECASE**，ASIN_RE 没有。
# 它的输入是 `variant_offset` 的 `error_detail` 自由文本（我们自己拼的诊断串），
# 不是用户提交的 ASIN。换成 ASIN_RE 会让所有非 B 开头的 variant 诊断**静默**
# 捞不到值，导出列「实际页面ASIN」变空——而黄金基线里没有这一步，不会响。
_VARIANT_PAGE_ASIN_RE = re.compile(r"\bpage=([A-Z0-9]{10})\b", re.IGNORECASE)


def _parse_selected_fields(fields_param: str = None):
    """解析并校验字段选择，返回 None 表示全选"""
    if not fields_param:
        return None
    selected = [f for f in fields_param.split(",") if f in EXPORTABLE_FIELDS]
    return selected if selected else None


# 导出时每攒够这么多行就交给工作线程处理一次（append/格式化）。
# 取值权衡：太小 → executor 往返频繁；太大 → 单次线程调用持锁时间变长。
_EXPORT_ROWS_PER_CHUNK = 2000


def _export_needed_columns(field_keys, include_total: bool):
    """导出实际需要从 asin_data 读取的列（供 iter_results 收窄投影用）。

    = 用户勾选的输出列 field_keys；若含虚拟列「总价」，额外需要 buybox_price /
    buybox_shipping 两列参与计算。批次状态列来自 tasks/别名，不在此列出。
    """
    needed = list(field_keys)
    if include_total:
        for c in ("buybox_price", "buybox_shipping"):
            if c not in needed:
                needed.append(c)
    return needed


def _get_export_headers(selected_fields=None, include_batch_status: bool = False):
    """构建导出表头和字段键"""
    if selected_fields is None:
        selected_fields = list(EXPORTABLE_FIELDS)

    include_total = "total_price" in selected_fields
    field_keys = [f for f in selected_fields if f != "total_price"]
    headers = [config.HEADER_MAP.get(f, f) for f in field_keys]

    if include_total:
        shipping_h = config.HEADER_MAP.get("buybox_shipping", "buybox_shipping")
        idx = headers.index(shipping_h) + 1 if shipping_h in headers else len(headers)
        headers.insert(idx, config.HEADER_MAP.get("total_price", "总价"))

    if include_batch_status:
        headers.extend(BATCH_STATUS_EXPORT_HEADERS)

    return headers, field_keys, include_total


def _batch_status_export_values(item: dict) -> list:
    status = str(item.get("batch_task_status") or "")
    has_asin_data = bool(item.get("batch_has_asin_data"))
    error_type = str(item.get("batch_error_type") or "") if status == "failed" else ""
    error_detail = str(item.get("batch_error_detail") or "") if status == "failed" else ""

    result_map = {
        "done": "成功",
        "failed": "失败",
        "processing": "处理中",
        "pending": "待采集",
    }
    batch_result = result_map.get(status, status)

    if status == "done" and has_asin_data:
        data_source = "本次采集更新"
    elif has_asin_data:
        data_source = "历史产品库数据，本次未更新"
    elif status == "failed":
        data_source = "无产品库数据，本次失败"
    elif status in ("pending", "processing"):
        data_source = "无产品库数据，本次未完成"
    else:
        data_source = "无产品库数据"

    actual_page_asin = ""
    if error_type == "variant_offset":
        m = _VARIANT_PAGE_ASIN_RE.search(error_detail)
        if m:
            actual_page_asin = m.group(1).upper()

    return [
        batch_result,
        data_source,
        error_type,
        error_detail,
        actual_page_asin,
        str(item.get("batch_retry_count") or ""),
        str(item.get("batch_task_updated_at") or ""),
        str(item.get("batch_asin_data_updated_at") or ""),
    ]


def _prepare_row(item: dict, field_keys: list, headers: list, include_total: bool,
                 include_batch_status: bool = False):
    """构建单行导出数据"""
    row = [str(item.get(f, "") or "") for f in field_keys]
    if include_total:
        bp = _parse_price_float(item.get("buybox_price", ""))
        bs_str = str(item.get("buybox_shipping", ""))
        if bp is not None:
            bs = 0.0 if bs_str.upper() == "FREE" else (_parse_price_float(bs_str) or 0.0)
            total = f"${bp + bs:.2f}"
        else:
            total = ""
        shipping_h = config.HEADER_MAP.get("buybox_shipping", "buybox_shipping")
        idx = headers.index(shipping_h) + 1 if shipping_h in headers else len(row)
        row.insert(idx, total)
    if include_batch_status:
        row.extend(_batch_status_export_values(item))
    return row


@router.get("/api/export/fields")
async def api_export_fields():
    """返回可导出字段列表"""
    return {
        "fields": EXPORTABLE_FIELDS,
        "headers": {f: config.HEADER_MAP.get(f, f) for f in EXPORTABLE_FIELDS},
    }


@router.get("/api/export/all")
async def api_export_all(format: str = "xlsx", change_filter: str = "all", fields: str = None):
    selected = _parse_selected_fields(fields)
    # P4.7：批次名的唯一构造点在 server/app.py:_batch_name（精度不变，本来就是秒）。
    # 这里的 name 只喂 Content-Disposition 的 filename，不落库、不进响应体
    # —— 黄金的 Recorder 不记 header，所以这一处对基线不可见。
    name = _srv()._batch_name("all")
    if format == "csv":
        return await _export_csv_streaming(name, batch_id=None, change_filter=change_filter, selected_fields=selected)
    else:
        return await _export_xlsx_streaming(name, batch_id=None, change_filter=change_filter, selected_fields=selected)


@router.get("/api/export/{batch_name}")
async def api_export_batch(batch_name: str, format: str = "xlsx", change_filter: str = "all", fields: str = None):
    batch = await _srv().db.get_batch_by_name(batch_name)
    if not batch:
        raise HTTPException(404, f"批次不存在: {batch_name}")
    selected = _parse_selected_fields(fields)
    if format == "csv":
        return await _export_csv_streaming(batch_name, batch_id=batch["id"], change_filter=change_filter, selected_fields=selected)
    else:
        return await _export_xlsx_streaming(batch_name, batch_id=batch["id"], change_filter=change_filter, selected_fields=selected)


async def _export_xlsx_streaming(filename: str, batch_id: int = None,
                                  change_filter: str = "all", selected_fields=None):
    """write_only 模式 + 临时文件 + 流式响应（百万级不 OOM）。

    P2：openpyxl 的行 append 与 wb.save() 是纯 CPU/序列化重活，若在事件循环里同步做，
    会在导出期间卡住仪表盘轮询与 worker 拉取/上传。这里把所有 openpyxl 操作放到一条
    专属工作线程（max_workers=1，保证 lxml/zip 有状态对象始终同线程），DB 迭代仍在事件
    循环里 async 进行；run_in_executor 让出事件循环，save 的 lxml 序列化 + zlib 压缩会
    释放 GIL，从而与仪表盘/worker 的请求处理真正并行。
    """
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    include_batch_status = batch_id is not None
    headers, field_keys, include_total = _get_export_headers(
        selected_fields, include_batch_status=include_batch_status)
    needed_cols = _export_needed_columns(field_keys, include_total)

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xlsx-export")
    st = {"wb": None, "ws": None, "count": 0}

    def _init():
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet(title="采集结果")
        ws.append(headers)
        st["wb"], st["ws"] = wb, ws

    def _append(items):
        ws = st["ws"]
        for item in items:
            ws.append(_prepare_row(
                item, field_keys, headers, include_total,
                include_batch_status=include_batch_status))
        st["count"] += len(items)

    def _close():
        try:
            st["wb"].close()
        except Exception:
            _srv().logger.debug("openpyxl workbook close 失败（忽略）", exc_info=True)

    tmp_path = None
    try:
        await loop.run_in_executor(executor, _init)

        buf = []
        async for item in _srv().db.iter_results(batch_id, change_filter=change_filter, columns=needed_cols):
            buf.append(item)
            if len(buf) >= _EXPORT_ROWS_PER_CHUNK:
                await loop.run_in_executor(executor, _append, buf)
                buf = []
        if buf:
            await loop.run_in_executor(executor, _append, buf)

        if st["count"] == 0:
            await loop.run_in_executor(executor, _close)
            raise HTTPException(404, "无数据")

        os.makedirs(config.EXPORT_DIR, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".xlsx", prefix="export_",
            dir=config.EXPORT_DIR)
        tmp_path = tmp.name
        tmp.close()
        try:
            await loop.run_in_executor(executor, st["wb"].save, tmp_path)
        except Exception:
            await loop.run_in_executor(executor, _close)
            os.unlink(tmp_path)
            raise
        await loop.run_in_executor(executor, _close)
    finally:
        executor.shutdown(wait=False)

    async def stream_and_cleanup():
        try:
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            os.unlink(tmp_path)

    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', filename)
    return StreamingResponse(
        stream_and_cleanup(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={safe}.xlsx"},
    )


async def _export_csv_streaming(filename: str, batch_id: int = None,
                                 change_filter: str = "all", selected_fields=None):
    """分块流式 CSV（百万级不 OOM）。

    P2：把每 _EXPORT_ROWS_PER_CHUNK 行的 _prepare_row + csv 格式化放到工作线程
    （run_in_executor），避免行格式化的 CPU 工作占住事件循环；同时按块 yield 而非逐行，
    减少 chunk 数量。DB 迭代仍在事件循环里 async 进行。
    """
    include_batch_status = batch_id is not None
    headers, field_keys, include_total = _get_export_headers(
        selected_fields, include_batch_status=include_batch_status)
    needed_cols = _export_needed_columns(field_keys, include_total)

    def _format_chunk(items):
        out = io.StringIO()
        w = csv.writer(out)
        for item in items:
            w.writerow(_prepare_row(
                item, field_keys, headers, include_total,
                include_batch_status=include_batch_status))
        return out.getvalue().encode("utf-8")

    async def generate():
        loop = asyncio.get_running_loop()
        out = io.StringIO()
        csv.writer(out).writerow(headers)
        yield out.getvalue().encode("utf-8-sig")

        buf = []
        async for item in _srv().db.iter_results(batch_id, change_filter=change_filter, columns=needed_cols):
            buf.append(item)
            if len(buf) >= _EXPORT_ROWS_PER_CHUNK:
                yield await loop.run_in_executor(None, _format_chunk, buf)
                buf = []
        if buf:
            yield await loop.run_in_executor(None, _format_chunk, buf)

    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', filename)
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={safe}.csv"},
    )


@router.get("/api/export/{batch_name}/screenshots")
async def api_export_screenshots(batch_name: str):
    # 路径安全：batch_name 直接拼成磁盘目录，校验防穿越
    if _srv()._safe_fs_component(batch_name) is None:
        raise HTTPException(400, "非法批次名")
    ss_dir = os.path.join(config.SCREENSHOT_DIR, batch_name)
    if not os.path.isdir(ss_dir):
        raise HTTPException(404, "无截图文件")

    png_files = [
        fname for fname in os.listdir(ss_dir)
        if fname.lower().endswith(".png")
        and os.path.isfile(os.path.join(ss_dir, fname))
    ]
    if not png_files:
        raise HTTPException(404, "无截图文件")

    # BytesIO 会让完整 ZIP 常驻进程内存；并发下载时按 ZIP 大小线性叠加。
    # 落盘临时文件后按块发送，让常驻内存与截图批次大小脱钩。
    import tempfile
    os.makedirs(config.EXPORT_DIR, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".zip", prefix="screenshots_",
        dir=config.EXPORT_DIR,
    )
    tmp_path = tmp.name
    tmp.close()

    def build_zip():
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for fname in png_files:
                zf.write(os.path.join(ss_dir, fname), fname)

    try:
        await asyncio.to_thread(build_zip)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    async def stream_and_cleanup():
        try:
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    filename = f"screenshots_{batch_name}.zip"
    return StreamingResponse(
        stream_and_cleanup(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
