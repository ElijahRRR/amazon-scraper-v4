"""运行时设置（3 个端点）—— 从 `server/app.py` 拆出。

    GET  /api/settings
    PUT  /api/settings
    POST /api/settings/reset

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **`_runtime_settings` / `_settings_version` 一个都不搬。** 它们留在
   `server/app.py`，这里一律 `_srv().xxx` 属性访问，**禁止 from-import**。
   两个理由，第二个是决定性的：
     * 黄金夹具与 PG 夹具按名字给 `server.app` 打补丁（每个样本前重置设置），
       from-import 拿到的是快照，补丁打空；
     * 这两个名字会被**整体重新赋值**（`api_reset_settings` 换掉整个 dict、
       `_settings_version` 自增），from-import 之后本模块看到的永远是旧对象。

   同理，写回也必须是 `_s._settings_version = ...` 这种**属性赋值**，
   不能用 `global`——`global` 只作用于本模块，改不到 `server.app` 上的名字。

2. **`_default_settings` / `_save_settings` 留在 `app.py`**：
   `_load_settings` 在 lifespan 里调、`_save_settings` 还被
   `_auto_scrape_scheduler` 那条后台协程调，都不在本模块的职责里。

3. **函数名 / docstring / 路径一个字不改** —— 它们被编码进 `operationId` /
   `summary` / `description`，而 `/openapi.json` 是黄金基线的一步、逐字节钉死。

4. **router 光秃**：`APIRouter()`，不带 `tags=` / `prefix=` /
   `include_in_schema`——整份 schema 里没有 `tags` 键。

5. 三条全是静态路径，`/api/settings` 与 `/api/settings/reset` 不互吃
   （没有 `/api/settings/{x}` catch-all），注册次序不影响匹配。
"""

from fastapi import APIRouter, Request


def _srv():
    from server import app as _s
    return _s


router = APIRouter()


@router.get("/api/settings")
async def api_get_settings():
    _s = _srv()
    return {"settings": _s._runtime_settings, "version": _s._settings_version}


@router.put("/api/settings")
async def api_update_settings(request: Request):
    _s = _srv()
    body = await request.json()

    for key, value in body.items():
        if key in _s._runtime_settings:
            _s._runtime_settings[key] = value

    _s._settings_version += 1
    _s._save_settings()
    return {"ok": True, "version": _s._settings_version, "settings": _s._runtime_settings}


@router.post("/api/settings/reset")
async def api_reset_settings():
    """恢复默认设置"""
    _s = _srv()
    _s._runtime_settings = _s._default_settings()
    _s._settings_version += 1
    _s._save_settings()
    return {"ok": True, "settings": _s._runtime_settings}
