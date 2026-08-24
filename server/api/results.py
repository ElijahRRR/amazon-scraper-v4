"""结果查询与删除（5 个端点）—— Phase 3.6 从 `server/app.py` 拆出。

    GET    /api/results
    GET    /api/results/{asin}
    GET    /api/changes/stats
    POST   /api/results/delete-by-file
    DELETE /api/results

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **模块级可变全局一个都不搬。** `db` 留在 `server/app.py`，这里一律
   `_srv().db`，**禁止 from-import** —— 三个 PG 夹具
   （`tests/pgdb/test_sync_api.py:38`、`test_retention.py`、
   `test_export_retention_window.py`）用 `monkeypatch.setattr(srv, "db", pgdb)`
   整体换掉这个属性，快照下来就补丁打空。
   `MAX_UPLOAD_BYTES` / `_remove_screenshot_files` 同理留在 `app.py`，
   这里走 `_srv().xxx`。

2. **本模块一条 SQL 都没有了**（Phase 3.8 批 (3)）。三段裸 SQL 收进了
   `db.find_asins_by_search(terms)` / `db.get_batch_asin_set(batch_id)` /
   `db.delete_asins(asins)`，两个后端各一份实现，名字都在
   `common/pgdb/__init__.py` 的 `PUBLIC_API` 里（导入期守卫）。

   收口之前这里是这么脆的：`api_delete_results` 里那段
   `"(d.asin LIKE ? OR d.title LIKE ? OR d.brand LIKE ?)"` + `f"%{term}%"`
   在 PG 侧的语义**不是** psycopg 原生的 —— 它靠 `common/pgdb/pool.py` 的
   `_LIKE_QMARK_RE` **按字面文本**把 `LIKE ?` 改写成带 `ESCAPE ''` 的形式
   （`LIKE_NO_ESCAPE`），才让反斜杠在两个后端表现一致（D-16）。
   把 `LIKE ?` 换个写法、把三段 OR 拆开、甚至只是多一个空格，正则就不再命中，
   PG 侧当场换语义而两边都不报错。现在删除路径与读路径引用**同一个常量**
   （SQLite 侧 `common/database.py:SEARCH_TERM_OR`、PG 侧
   `common/pgdb/results_read.py:_TERM_OR`，模式函数共用
   `_shared.search_like_pattern`），那条正则不再是删除路径的承重件。

   ⚠ **这条不一致今天是死的，别把它弄活**：
   `tests/test_search_like_escape_parity.py` 逐条比对「同一个 `search` 在
   GET /api/results 读出来的行集」与「DELETE /api/results 删掉的行集」，
   跟着 `DB_BACKEND` 走两列。实测把 `LIKE_NO_ESCAPE` 置空会让其中 3 条当场红。

3. **router 光秃**：`APIRouter()`，不带 `tags=` / `prefix=` /
   `include_in_schema`。`/openapi.json` 是黄金基线的一步、逐字节钉死。
   ⚠ 因此 `Query(...)` 的**每一个约束参数都是对外契约的一部分**：
   `le=` 会渲染成 schema 里的 `maximum`。本轮把 `le=200` 改成
   `le=MAX_PAGE_LIMIT`（1000），黄金基线的 `openapi_schema` 那一步
   因此有一处**有意的** diff（`maximum: 200 -> 1000`），已重录。
   改这个参数 = 改契约，不是改实现细节。

4. **函数名 / docstring / 路径一个字不改** —— 它们被编码进 `operationId` /
   `summary` / `description` / `Body_api_delete_by_file_*` schema 名。

5. **路由匹配不受影响**：`GET /api/results/{asin}` 与
   `POST /api/results/delete-by-file` 方法不同（Starlette 对方法不匹配的
   路径记 PARTIAL 后继续找 FULL），`GET /api/results` 与
   `DELETE /api/results` 同理；两者在本文件里的先后顺序仍与拆分前一致。

6. **本模块的两个删除端点今天没有黄金网**（78 步一步没有，2.4 的错误路径扩容
   也没覆盖它们 —— 它们不是错误路径）。替代网是两份 `unittest.TestCase`
   （写成 TestCase 而非裸 `def test_*`，否则门禁里 `unittest discover`
   那两列会静默跳过），两个后端都跑：
     * `tests/test_results_delete_api.py` —— 端点行为（三个上传分派分支、
       三条筛选路径、两条 4xx、`deleted` 计的是请求里的 ASIN 数）。
     * `tests/test_search_like_escape_parity.py` —— 读路径与删除路径的
       LIKE 语义必须选中同一批行（批 (3) 的前置条件）。
"""

import csv
import io
import re

import openpyxl
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

# `fields=` 的列名白名单。与 iter_results 的投影白名单**同一个对象** ——
# 两处对"什么是合法列名"必须给出同一个答案。
from common.core.asindata import _ASIN_DATA_COLUMN_SET
from common.core.results_sort import CursorExpired, DEFAULT_SORT, SORT_MODES


def _srv():
    from server import app as _s
    return _s


router = APIRouter()


# ==================== API: 结果查询 ====================

#: 单页行数上限。**这个数字有出处，改之前先看下面这段。**
#:
#: 上一版是 ``le=200``，来自旧 erpAPI（「单页上限 200 是服务端硬约束，调大无效」），
#: 移植时原样抄了过来、一行注释都没有。它在**旧库**上或许成立，在这套库上不成立：
#: 2026-08-07 用 15 万行 asin_data / 15 万 batch_asins / 30 万 asin_changes /
#: 15 万 screenshots 的 bench 库重测（列宽照真实采集填满：long_description 1755B、
#: bullet_points 1049B、image_urls 594B，整行 JSON 化 5675 B/行），
#: limit = 50/200/500/1000/2000/5000 逐档量翻页 SQL、count SQL、响应体字节：
#:
#:   PG，EXPLAIN (ANALYZE,BUFFERS) 的 Execution Time 中位数
#:     翻页 SQL（无筛选）   0.05 / 0.10 / 0.21 / 0.44 / 1.33 / 2.70 ms
#:     翻页 SQL（price_stock）0.22 / 0.69 / 1.79 / 3.46 / 6.96 / 18.52 ms
#:     count SQL（无筛选）  15.3 / 14.6 / 14.9 / 16.6 / 16.2 / 15.2 ms
#:     count SQL（price_stock）79.4 / 77.4 / 73.5 / 106.1 / 67.6 / 74.8 ms
#:   响应体（两个后端逐字节同量级）0.27 / 1.06 / 2.65 / 5.31 / 10.6 / 26.5 MB
#:
#: 三条结论，都不是 SQL 说了算：
#:
#: 1. **count 与 limit 无关**。它是每个请求都要付的**固定**成本（Phase 1 之后
#:    无筛选 ~15ms、change_filter 路径 ~70-105ms、SQLite 侧 change_filter ~120ms），
#:    翻多少行都一样。所以它是**支持**调大 limit 的论据，不是限制：
#:    拉 1 万行时，limit=200 要付 50 次 count，limit=1000 只付 10 次。
#:    实测端到端（HTTP 全栈）拉满 1 万行：
#:      change_filter 路径  limit=200 6315ms -> 500 3774ms -> 1000 3129ms -> 2000 2516ms
#:      无筛选              limit=200 3405ms -> 500 2928ms -> 1000 2466ms -> 2000 2508ms
#:    收益到 1000 就基本吃完，再往上换来的是更大的单次响应体，不是更高的吞吐。
#: 2. **翻页 SQL 根本不是瓶颈**。5000 行也只有 2.7ms（无筛选）/ 18.5ms（有筛选），
#:    比同一个请求里那条 count 还便宜一个数量级。Phase 1 已经把这条路径从
#:    1007ms 打到了亚毫秒，limit 在这上面没有拐点，**任何档位都没有 SQL 悬崖**。
#: 3. **真正的约束是响应体与序列化**，而且它是**线性**的、与 limit 无关：
#:    本端点 ``return result`` 没有 response_model，FastAPI 走
#:    ``jsonable_encoder`` -> ``json.dumps`` 的通用路径，纯 Python 逐字段递归。
#:    limit=2000 那一档拆开是 db.get_results 80.7ms / jsonable_encoder 227.9ms /
#:    json.dumps 153.0ms —— **82% 的账在 Python 序列化上**，每行约 230-250 µs，
#:    每档都一样。所以调大 limit 既不会变快也不会变慢（每行成本恒定），
#:    它只决定**一次请求要吐多大一坨**。
#:
#: 于是 1000 这个数是这么定的（三条都是"响应体/内存"口径，不是"SQL 耗时"口径）：
#:   * 响应体 5.3 MB/页。2000 就是 10.6 MB、5000 是 26.5 MB —— 一个 JSON 响应
#:     大到那个份上，中间的反代/网关默认体积限制就开始成为不可见的失败源。
#:   * 单请求 Python 峰值内存 37.5 MB（tracemalloc，limit=2000 是 74.9 MB、
#:     5000 是 187 MB）。PG_POOL_MAX 默认 10，1000 这一档满并发峰值约 375 MB，
#:     2000 就是 750 MB —— 这才是会把进程打死的那条线。
#:   * 与本仓库另外两个分页端点对齐：``server/api/export_incremental.py`` 与
#:     ``server/api/sync.py`` 的 MAX_LIMIT 都是 1000，而 /api/export/incremental
#:     的 1000 是**契约 v1**、沃尔玛侧已按它实现（R1，不许单方面改）。
#:     三个端点同一个天花板，调用方少记一个数。
#:
#: ⚠ **与旧 erpAPI 的语义差别，调用方必须知道**：旧系统是「调大无效」（听起来像
#:   静默截断）；这里是 ``le=``，FastAPI 对超限**直接 422 拒绝**，不截断。
#:   静默截断更危险 —— 消费者会把「只回了 N 条」读成「只有 N 条」——
#:   但它要求调用方**自己分页**而不是「传个大数看它给多少」。
#:   钉在 tests/test_results_cursor_liveness.py::test_page_size_ceiling_rejects_not_truncates。
#:
#: 什么时候该重估：
#:   * asin_data 平均行宽显著变化（新增宽列、或 long_description 的量级变了）
#:     —— 上面每一档的 MB 数直接按 5675 B/行 线性缩放；
#:   * 本端点加上 response_model 或换掉 ORJSONResponse 之类的序列化路径
#:     —— 那 82% 的账会塌下去，天花板可以再往上抬；
#:   * PG_POOL_MAX 或部署的内存配额变了 —— 上面第二条按 峰值内存 × 并发 重算。
#:   重测脚本的做法见本轮报告；口径要三样一起看：EXPLAIN 的 Execution Time、
#:   HTTP 全栈墙钟、响应体字节数。**只看 SQL 会得出「随便调多大都行」的错误结论。**
MAX_PAGE_LIMIT = 1000


#: `fields=` 一次最多接受多少个列名。防的是「拼一个超长 query string 让服务端
#: 去做无意义的集合运算」，不是功能限制 —— asin_data 一共也才 56 列。
MAX_FIELDS = 64


@router.get("/api/results")
async def api_results(batch_id: int = None,
                      cursor: int = None,
                      limit: int = Query(50, le=MAX_PAGE_LIMIT),
                      search: str = None,
                      change_filter: str = "all",
                      direction: str = "next",
                      fields: str = None,
                      with_total: bool = True,
                      sort: str = DEFAULT_SORT):
    """
    `fields` / `with_total` 是**可选的减负开关**，两个都默认关闭（= 保持原行为）。

    上面 MAX_PAGE_LIMIT 那段实测记着「82% 的账在 Python 序列化上」。这两个参数
    冲的就是那 82%，它们**不优化 SQL**：

        实测 100 万行、单页 50 行、宽列有真实内容：
          SELECT d.*（56 列）+ count      54.4 ms    274.2 KB
          只取 UI 渲染的 15 列              1.2 ms     17.8 KB
          其中 COUNT(*) 那一条             48.4 ms

    `fields=a,b,c` —— 只返回这些列。**必须默认全给**：`items[]` 的列集是对外
    契约（docs/erpapi_contract.md §3.2 允许单方面**加**字段、不许删），
    窄投影只能是调用方显式要的。

    ⚠ 服务端会**强制补上** `id` / `asin` / `screenshot_path` / `updated_at`，
    即使你没点名 —— 翻页游标与截图路径归一化都要它们。所以返回的键**可能比你
    要的多**，别按"键集恰好等于我要的"来写解析。

    `with_total=false` —— 不算 `total`，响应里 `total` 是 `null`。
    它是**全表 COUNT**，随行数线性增长，而翻页途中值恒定不变：只在首屏要一次
    就够了。

    非法列名 -> **422 拒绝，不静默丢弃**。与 `limit` 超限那条同一个纪律
    （见上面 MAX_PAGE_LIMIT 的注释）：静默丢弃会让调用方把「这个字段没返回」
    读成「这个字段是空的」。

    ------------------------------------------------------------------
    `sort` —— 排序键，默认 `id`（**默认行为不变**）
    ------------------------------------------------------------------
    * `id`（默认）：`ORDER BY d.id DESC`。`asin_data` 一 ASIN 一行、按 asin
      UPSERT，`id` 在**首次入库**时分配后永不改变 —— 所以这是"第一次见到这个
      ASIN"的倒序，**不是**"最近采集"。重采的老 ASIN 仍然沉在最底下。
    * `recent`：`ORDER BY d.updated_at DESC, d.id DESC`，即"最近采的排前面"。

    默认留在 `id` 是因为**游标语义是对外契约**：调用方拿着上一页的 `next_cursor`
    继续翻，如果服务端某天改了默认排序，同一个游标会翻出语义完全不同的一页，
    而调用方看不出来。控制台前端显式传 `sort=recent`。

    ⚠ `sort=recent` 且游标那一行已被删除（多半是刚刚在这个页面上删的）
    -> **422 `cursor_expired`**，调用方应从第一页重来。这里不降级成按 id 比较：
    那会给出一页语义错误的数据，而且没人看得出来。
    """
    if sort not in SORT_MODES:
        raise HTTPException(422, {
            "error": "invalid_parameter",
            "message": f"sort 只能是 {' / '.join(SORT_MODES)}，收到 {sort!r}。",
            "parameter": "sort"})
    cols = None
    if fields is not None:
        cols = [f.strip() for f in fields.split(",") if f.strip()]
        if not cols:
            raise HTTPException(422, {
                "error": "invalid_parameter",
                "message": "fields 给了但一个列名都没有。不想筛就别传这个参数。",
                "parameter": "fields"})
        if len(cols) > MAX_FIELDS:
            raise HTTPException(422, {
                "error": "invalid_parameter",
                "message": f"fields 最多 {MAX_FIELDS} 个列名，收到 {len(cols)} 个。",
                "parameter": "fields"})
        unknown = sorted(set(cols) - _ASIN_DATA_COLUMN_SET)
        if unknown:
            raise HTTPException(422, {
                "error": "invalid_parameter",
                "message": (f"未知列名: {', '.join(unknown)}。"
                            "拼错的列名会被静默丢弃的话，你会把「没返回」读成「是空的」。"),
                "parameter": "fields",
                "unknown_fields": unknown})

    try:
        result = await _srv().db.get_results(
            batch_id=batch_id,
            cursor_id=cursor,
            limit=limit,
            search=search,
            change_filter=change_filter,
            direction=direction,
            columns=cols,
            with_total=with_total,
            sort=sort,
        )
    except CursorExpired as e:
        # 游标那一行没了（多半是刚在这个页面上删掉的）。给一个**可操作**的
        # 422 而不是空页 —— 空页会被读成"数据到头了"。
        raise HTTPException(422, {
            "error": "cursor_expired",
            "message": (f"游标 {e.cursor_id} 指向的行已不存在（可能已被删除）。"
                        "请从第一页重新开始翻页。"),
            "parameter": "cursor"})
    return result


@router.get("/api/results/{asin}")
async def api_result_detail(asin: str):
    db = _srv().db
    data = await db.get_result_by_asin(asin)
    if not data:
        raise HTTPException(404, f"ASIN {asin} 不存在")
    changes = await db.get_asin_changes(asin)
    return {"data": data, "changes": changes}


@router.get("/api/changes/stats")
async def api_change_stats(batch_id: int = None):
    return await _srv().db.get_change_stats(batch_id)


# ==================== API: 结果删除 ====================
#
# 这两条原本待在 app.py 的「诊断 / 侦查」节头之后 —— 节头骗人，按域它们属于
# 结果面。路径与上面三条同族（/api/results*），一起搬走。

@router.post("/api/results/delete-by-file")
async def api_delete_by_file(file: UploadFile = File(...)):
    """上传文件识别 ASIN 后删除对应数据"""
    _s = _srv()
    db = _s.db
    content = await file.read()
    if len(content) > _s.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大：{len(content)//1024//1024}MB，上限 {_s.MAX_UPLOAD_BYTES//1024//1024}MB")
    filename = file.filename or ""

    asins = []
    if filename.endswith(".xlsx"):
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        try:
            ws = wb.active
            for row in ws.iter_rows(min_row=1, values_only=True):
                for cell in row:
                    if cell:
                        val = str(cell).strip().upper()
                        if re.match(r'^B[0-9A-Z]{9}$', val):
                            asins.append(val)
        finally:
            wb.close()
    elif filename.endswith(".csv"):
        text = content.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            for cell in row:
                val = cell.strip().upper()
                if re.match(r'^B[0-9A-Z]{9}$', val):
                    asins.append(val)
    else:
        text = content.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            val = line.strip().upper()
            if re.match(r'^B[0-9A-Z]{9}$', val):
                asins.append(val)

    # 去重
    asin_list = list(dict.fromkeys(asins))
    if not asin_list:
        raise HTTPException(400, "文件中未找到有效 ASIN")

    screenshot_files = await db.delete_asins(asin_list)

    _s._remove_screenshot_files(screenshot_files)
    return {"ok": True, "deleted": len(asin_list), "asin_count": len(asin_list)}


@router.delete("/api/results")
async def api_delete_results(request: Request):
    """按条件删除采集结果"""
    _s = _srv()
    db = _s.db
    body = await request.json()
    batch_id = body.get("batch_id")
    asins = body.get("asins")  # list of ASIN strings
    search = body.get("search")  # fuzzy search string

    # 构建 ASIN 列表
    target_asins = set()
    has_explicit_filter = bool(asins or search)

    if asins:
        # asins 列表也做上限保护，避免一次投递百万级 ASIN 触发巨量 SELECT
        if len(asins) > 100000:
            raise HTTPException(400, f"asins 列表过长（{len(asins)}），单次上限 100000")
        target_asins.update(asins)

    if search:
        # 限长防 DoS：500 字符 / 10 关键词 / 单词 100 字符
        search = str(search)[:500]
        terms = [t.strip()[:100] for t in search.split(",") if t.strip()][:10]
        if terms:
            target_asins.update(await db.find_asins_by_search(terms))

    # 纯 batch_id（无 asins/search）→ 删除该批次所有 ASIN
    if batch_id and not has_explicit_filter:
        target_asins = set(await db.get_batch_asin_set(batch_id))
    # batch_id + 其他条件 → 取交集
    elif batch_id and target_asins:
        target_asins &= set(await db.get_batch_asin_set(batch_id))

    if not target_asins:
        return {"ok": True, "deleted": 0}

    asin_list = list(target_asins)
    screenshot_files = await db.delete_asins(asin_list)

    # 删除物理截图文件
    _s._remove_screenshot_files(screenshot_files)
    return {"ok": True, "deleted": len(asin_list)}
