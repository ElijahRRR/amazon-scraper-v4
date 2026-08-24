"""common/core —— 后端无关的纯 Python 真源（**定义在这里**，别处只许再导出）。

本包是 `common/pgdb/_shared.py` 那批共享符号的**唯一定义处**。
`common/database.py`（SQLite 后端）与 `common/pgdb/_shared.py`（PG 后端）
现在都只是**逐名再导出**，三个模块指向同一批对象。

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
* ``as_int`` —— HTTP 边界的整数强转。原先在 ``common/pgdb/pool.py``，
  而那个模块是模块级 ``import asyncpg``：纯函数被锁在只有 PG 才能 import
  的模块里，没人能安全地复用（Phase 4.1 一并搬来）。

  ⚠ **它与上面那批的再导出路径不同，别套用同一条不变量**（Phase 4.1 审计指出）：
  上面那批走 ``common/database.py`` 与 ``common/pgdb/_shared.py`` 两处再导出，
  所以「三个模块对同一个名字给出同一个对象」对它们成立；
  ``as_int`` **不在** ``_shared.__all__`` 里、``common/database.py`` 里也没有它，
  它的再导出点是 ``common/pgdb/pool.py``（``pool.as_int is coerce.as_int`` 为真），
  外加 ``PoolMixin.as_int`` 那个 4.1 之前就有的 staticmethod 薄壳。
  验它要验这条链，验 ``getattr(common.database, "as_int")`` 会 AttributeError —— 那不是分叉，是它本来就不在那儿。

（原 ``_shared.py`` 在这里还有一句「导入 common.database 的代价：它 import
aiosqlite」——那正是这次搬迁消除掉的东西，故删去。本包只依赖 ``common.config``
与标准库。）

**逐名再导出，不要写成 ``from common.core import *``。**

理由要说准，因为这里最初写错过一次（Phase 4.1 审计逮到）：
「星号导入跳过下划线开头的名字」这条规则**只在模块没有 ``__all__`` 时成立**。
本模块自己定义了 ``__all__`` 并把 16 个下划线名全列了进去，所以今天
``from common.core import *`` 其实**能正常工作** —— 审计做过对照实验：
照现状星号导入，28 个名字一个不少、全套门禁绿；**把 ``__all__`` 删掉**再星号导入，
才出现 ``ImportError: cannot import name '_parse_price_float'`` 并连带 31 failed。

所以真正的理由不是「会当场炸」，而是：

* **逐名清单是显式的**。星号导入把「导出面」这件事的控制权交给了
  ``__all__`` 的维护状态 —— 谁哪天在 ``__all__`` 里加一行、删一行，
  下游的可见符号就跟着变，而**改动点和受影响点不在同一个文件里**。
* 逐名写法下，``common/database.py`` 与 ``common/pgdb/_shared.py`` 的导入清单
  本身就是那份契约，评审 diff 就能看见面的增减。
* 而且一旦有人**顺手删掉本模块的 ``__all__``**（它看起来只是个冗余清单），
  星号导入的下游会在导入期整批 NameError —— 逐名写法对这种改动免疫。

**约束：本文件下方不得出现任何 ``def`` / 赋值新对象。只有 import。**
"""
# flake8: noqa: F401  —— 全部是有意的再导出
from common.core.retry import (
    LIMITED_RETRY_ERROR_TYPES,
    NO_AUTO_RETRY_ERROR_TYPES,
    NO_RETRY_ERROR_TYPES,
    _fail_cap,
)
from common.core.results_sort import (
    CursorExpired,
    DEFAULT_SORT,
    SORT_MODES,
    is_next,
    keyset_predicate,
    normalize_sort,
    order_by,
)
from common.core.lockmeter import (
    LOCK_STATS,
    TimedLock,
    _NamedLockCtx,
    _record_wait,
    _record_hold,
    record_pool_wait,
    record_stage,
)
from common.core.asindata import (
    _NA_VALUES,
    _normalize_screenshot_path,
    _is_parse_failure,
    _parse_price_float,
    _compare_price,
    _compare_stock_qty,
    _compare_stock_status,
    _HASH_FIELDS,
    _TITLE_BULLETS_FIELDS,
    _compute_content_hash,
    _compute_title_bullets_hash,
    ASIN_DATA_FIELDS,
    _ASIN_DATA_COLUMN_SET,
)
from common.core.dbtables import (
    CLEAR_TABLES,
    ASIN_DELETE_CHUNK,
    ASIN_DELETE_TABLES,
    search_like_pattern,
)
from common.core.coerce import as_int

__all__ = [
    "LIMITED_RETRY_ERROR_TYPES",
    "NO_AUTO_RETRY_ERROR_TYPES",
    "NO_RETRY_ERROR_TYPES",
    "_fail_cap",
    "CursorExpired",
    "DEFAULT_SORT",
    "SORT_MODES",
    "is_next",
    "keyset_predicate",
    "normalize_sort",
    "order_by",
    "LOCK_STATS",
    "TimedLock",
    "_NamedLockCtx",
    "_record_wait",
    "_record_hold",
    "record_pool_wait",
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
    "ASIN_DELETE_TABLES",
    "search_like_pattern",
    "as_int",
]
