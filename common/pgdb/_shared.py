"""common/pgdb/_shared.py —— 与 SQLite 实现**共享**的纯 Python 符号。

本模块**只做再导出**，不得定义任何常量/函数的副本。
唯一真源自 Phase 4.1 起是 ``common/core/``（原先是 ``common/database.py``）；
``common/database.py`` 现在与本文件一样，也只是从 ``common/core`` 逐名再导出，
三个模块指向**同一批对象**（tests/pgdb/test_skeleton.py:85 的 ``is`` 断言钉这个）。

理由（规格里已经论证过，这里复述以免后来人"顺手抄一份"）：

* ``LOCK_STATS`` / ``record_stage`` —— ``/api/_debug/lock-stats``
  在 server/app.py:2625 里是 ``from common.database import LOCK_STATS``，
  按**模块全局对象**读。pgdb 若自己建一份，那个端点永远返回空容器，
  黄金基线 step 56 立刻炸（waits/holds/stage_timings 七个 key 全部"字段消失"）。
* ``ASIN_DATA_FIELDS`` / ``_ASIN_DATA_COLUMN_SET`` —— 驱动 ``_save_result_inner_unlocked``
  的动态列清单与 ``iter_results`` 的投影白名单。分叉 = 两个存储后端写入的列集悄悄不同。
* ``_compute_content_hash`` / ``_compute_title_bullets_hash`` —— md5 over ``"|".join(...)``。
  字段表分叉 = 每一条 hash 变、每一条 asin_changes 变。
* ``_is_parse_failure`` / ``_normalize_screenshot_path`` / ``_NA_VALUES`` ——
  server_reject 语义，**含有意保留的 bug**，必须逐字共享。
* ``_fail_cap`` / ``NO_AUTO_RETRY_ERROR_TYPES`` / ``LIMITED_RETRY_ERROR_TYPES`` ——
  重试上限策略，app.py:1239 直接 import。
* ``_parse_price_float`` 等四个比较器 —— 变动检测；app.py:1807 直接 import 第一个。
* ``ASIN_DELETE_CHUNK`` / ``ASIN_DELETE_TABLES`` —— ``delete_asins`` 的分块大小
  与表清单。分块边界分叉 = 两个后端发出的语句序列不同，出事时对不上号。
* ``search_like_pattern`` —— ``%term%``，**不转义**。读路径（``get_results``）
  与删除路径（``find_asins_by_search``）必须用同一个，否则同一个 search
  在 GET 和 DELETE 下选中不同的行（D-16 记的就是这个事故）。
  两侧现在都做到了：PG 侧 ``results_read._like_pattern = search_like_pattern``，
  SQLite 侧 ``get_results`` 的快慢两条路径也都调它（Phase 3.8 审计发现原来
  只有 PG 侧做到，SQLite 读路径还留着自己的 ``f"%{t}%"``，已一并收口）。
* ``CLEAR_TABLES`` —— ``clear_all_data`` 的删除清单。两侧各有一份实现
  （SQLite 删 ``sqlite_sequence``、PG 做 identity RESTART），但**删哪些表**
  必须是同一份，否则两个后端「清空」之后剩下的东西悄悄不同。

导入 ``common.core`` 没有任何代价：它只依赖 ``common.config`` 与标准库
（Phase 4.1 之前这里 import 的是 ``common.database``，那要顺带拖进 aiosqlite）。

**约束：本文件下方不得出现任何 ``def`` / 赋值新对象。只有 import。**
"""
# flake8: noqa: F401  —— 全部是有意的再导出
from common.core import (
    # ---- 重试策略 ----
    LIMITED_RETRY_ERROR_TYPES,
    NO_AUTO_RETRY_ERROR_TYPES,
    NO_RETRY_ERROR_TYPES,
    _fail_cap,
    # ---- 锁仪表（必须与 SQLite 实现共用同一个全局容器）----
    LOCK_STATS,
    TimedLock,
    _NamedLockCtx,
    _record_wait,
    _record_hold,
    record_stage,
    # ---- 解析失败 / 截图路径归一 ----
    _NA_VALUES,
    _normalize_screenshot_path,
    _is_parse_failure,
    # ---- 变动比较器 ----
    _parse_price_float,
    _compare_price,
    _compare_stock_qty,
    _compare_stock_status,
    # ---- hash ----
    _HASH_FIELDS,
    _TITLE_BULLETS_FIELDS,
    _compute_content_hash,
    _compute_title_bullets_hash,
    # ---- asin_data 列清单 ----
    ASIN_DATA_FIELDS,
    _ASIN_DATA_COLUMN_SET,
    # ---- 清库的表清单（DELETE /api/database）----
    CLEAR_TABLES,
    # ---- 按 ASIN 删除 / 模糊搜索（DELETE /api/results）----
    ASIN_DELETE_CHUNK,
    BATCH_DELETE_CHUNK,
    ASIN_DELETE_TABLES,
    search_like_pattern,
)

__all__ = [
    "LIMITED_RETRY_ERROR_TYPES",
    "NO_AUTO_RETRY_ERROR_TYPES",
    "NO_RETRY_ERROR_TYPES",
    "_fail_cap",
    "LOCK_STATS",
    "TimedLock",
    "_NamedLockCtx",
    "_record_wait",
    "_record_hold",
    "record_stage",
    "_NA_VALUES",
    "_normalize_screenshot_path",
    "_is_parse_failure",
    "_parse_price_float",
    "_compare_price",
    "_compare_stock_qty",
    "_compare_stock_status",
    "_HASH_FIELDS",
    "_TITLE_BULLETS_FIELDS",
    "_compute_content_hash",
    "_compute_title_bullets_hash",
    "ASIN_DATA_FIELDS",
    "_ASIN_DATA_COLUMN_SET",
    "CLEAR_TABLES",
    "ASIN_DELETE_CHUNK",
    "BATCH_DELETE_CHUNK",
    "ASIN_DELETE_TABLES",
    "search_like_pattern",
]
