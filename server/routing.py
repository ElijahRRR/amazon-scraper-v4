"""路由自省助手 —— 展平 `app.routes`、定位注册顺序。

------------------------------------------------------------------------
为什么单独有这么个模块
------------------------------------------------------------------------
`flatten_routes` 以前**同时存在两份复制**：`tests/test_incremental_export.py`
和 `tools/preflight.py` 各写了一遍。两份逻辑相同、注释各写各的，而它们
守的是同一条承重不变量（增量导出端点必须排在 catch-all 之前，见
`route_order_ok`）。

这个仓库对**存储层**是有共享纪律的：`common/core/` 是两个后端的唯一真源，
`common/pgdb/__init__.py` 还有 `PUBLIC_API` 双向断言，分叉了会当场炸。但
**服务端自省这类助手一直没有对应的层**——没地方放，就近塞进了 tools/ 的
一次性脚本里，然后源码注释开始引用那个脚本，一个本该退役的工具就这么长出了
承重意义。本模块就是补上那个缺口：这类东西属于 `server/`，不属于 tools/，
更不该在 tests/ 里各留一份。

（`server/authz.py` 是同一类：`server/` 下不是 router 的正式模块。）
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

#: catch-all 导出端点。任何"必须排在它前面"的端点都拿它当基准。
EXPORT_CATCH_ALL = "/api/export/{batch_name}"

#: 必须排在 `EXPORT_CATCH_ALL` 之前的端点。排到后面会被 catch-all 吃掉，
#: 返回一个**语义完全错误**的 404「批次不存在: incremental」——不是 500、
#: 不是 405，是一个看起来很正常的 404，消费侧极易误读成"暂无数据"。
EXPORT_INCREMENTAL = "/api/export/incremental"


def flatten_routes(routes: Sequence[Any]) -> List[Any]:
    """把 ``app.routes`` 展平成与 Starlette 匹配顺序一致的扁平列表。

    FastAPI ≥ 0.141 的 ``include_router`` 不再把子路由摊平进父容器，而是插入
    一个惰性的 ``_IncludedRouter`` 包装对象（``path`` 是 ``None``、
    ``original_router`` 指向被包含的那个 ``APIRouter``）。Phase 3.7 之后
    ``/api/export/incremental`` 与 ``/api/export/{batch_name}`` **两条都在
    包装对象里面**（前者还多套一层：app → export.router → _incr.router），
    只扫顶层一定两个都找不到 —— 而"找不到"必须是红，不能是绿。

    包含是递归的，展开也必须是递归的；展开顺序就是注册顺序，也就是
    Starlette 的匹配顺序。
    """
    flat: List[Any] = []
    for r in routes:
        sub = getattr(r, "original_router", None)
        if sub is not None:
            flat.extend(flatten_routes(sub.routes))
        else:
            flat.append(r)
    return flat


def route_paths(routes: Sequence[Any]) -> List[Optional[str]]:
    """展平后的 ``path`` 序列，顺序即 Starlette 的匹配顺序。"""
    return [getattr(r, "path", None) for r in flatten_routes(routes)]


def route_order_ok(routes: Sequence[Any]) -> tuple[bool, str]:
    """增量导出端点是否仍排在 catch-all 之前。

    返回 ``(ok, 说明)``。**三种失败都必须是失败**，特别是后两种：

      * 增量端点找不到 —— `export.py` 里的 `include_router(_incr.router)` 掉了；
      * **catch-all 找不到** —— 这条最阴险：它不代表"没有 catch-all 所以安全"，
        而代表**本函数的查找逻辑失效了**（路径改名、FastAPI 换形态、又多包了
        一层 router）。前提没了就没资格判定，必须报失败。
        tools 里那份旧副本当年正是在这里落到 else 报绿的；
      * 顺序反了 —— 会静默 404。

    注意这只是**结构**层。完整守卫在
    `tests/test_incremental_export.py::RouteOrderTests`，那里还有行为层
    （真打一次，响应体不含「批次不存在」）与源码层（`include_router` 必须
    出现在第一个 `@router.get` 之前）。结构断言会被下一次 FastAPI 版本变化
    绕过（0.141 那次就绕过了一半），另外两层不会。
    """
    paths = route_paths(routes)
    incr = next((i for i, p in enumerate(paths) if p == EXPORT_INCREMENTAL), None)
    catch = next((i for i, p in enumerate(paths) if p == EXPORT_CATCH_ALL), None)

    if incr is None:
        return False, f"{EXPORT_INCREMENTAL} 没挂上 —— include_router(_incr.router) 掉了？"
    if catch is None:
        return False, (f"找不到 {EXPORT_CATCH_ALL} —— 本检查的前提失效了，不是「安全」。"
                       "先修查找逻辑（是不是又多包了一层 router？）")
    if incr > catch:
        return False, (f"{EXPORT_INCREMENTAL} 被 {EXPORT_CATCH_ALL} 吞掉了 —— "
                       "会静默返回 404「批次不存在」")
    return True, f"{EXPORT_INCREMENTAL} 在 catch-all 之前"


__all__ = [
    "EXPORT_CATCH_ALL",
    "EXPORT_INCREMENTAL",
    "flatten_routes",
    "route_paths",
    "route_order_ok",
]
