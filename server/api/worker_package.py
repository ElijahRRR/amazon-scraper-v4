"""Worker 安装包下载（1 个端点）。

    GET /api/worker/download?mode=full|update

------------------------------------------------------------------------
为什么会有这个模块
------------------------------------------------------------------------
`server/templates/settings.html` 与 `workers.html` 里一直有三个指向
``/api/worker/download`` 的链接（"下载 Worker 安装包" / "完整安装包" /
"代码更新包"），但**后端从来没有实现过这个路由** —— 点下去是 404。
UI 文案写得很具体（解压后双击 start.sh / start.bat，首次启动自动建 venv
装依赖），显然是有意的功能，只是没落地。这里把它补上，而不是把 UI 删掉。

------------------------------------------------------------------------
两种模式
------------------------------------------------------------------------
``full``（默认）
    首次安装用。打包 worker 跑起来需要的全部东西：``run_worker.py``、
    ``worker/``、``common/``、``requirements.txt``，外加现场生成的
    ``start.sh`` / ``start.bat`` / ``README.txt``。

``update``
    已经装过的机器更新代码用。**只有 .py 文件**，不含 requirements 与启动
    脚本 —— 解压覆盖即可，不动 venv、不重装依赖。

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------
1. **router 光秃**：``APIRouter()``，不带 ``tags=`` / ``prefix=``。
   ``/openapi.json`` 是黄金基线的一步、逐字节钉死，整份 schema 里没有
   ``tags`` 键。

2. **只打包 worker 真正需要的三个来源**（``run_worker.py`` / ``worker/`` /
   ``common/``）。特别是**不含 ``server/``**：那里有 ``templates/`` 与
   settings 持久化，worker 一个都用不到，打进去只是把服务端代码散出去。

3. **`common/` 整个收进来，不做裁剪。** worker 只 import 了
   ``common.config`` / ``common.core.*`` / ``common.slowhash``，但
   ``common/core/__init__.py`` 有跨模块再导出，按 import 图裁剪会在运行期
   炸在一个很难查的地方。``common/`` 才 14k 行，整包进去代价可以忽略。
   ``common/pgdb/`` 是唯一的例外（见 ``_EXCLUDE_DIRS``）：它是服务端专属的
   存储后端，且 import 期就要 asyncpg，而 worker 的 requirements 里没有它。

4. **服务器地址烘进启动脚本**：``run_worker.py --server`` 是必填参数，
   而下载的人手上未必知道该填什么。这里用**发起这次下载的请求**的
   base_url 作为默认值写进 start.sh / start.bat，同时保留命令行覆盖。
"""
from __future__ import annotations

import io
import os
import zipfile

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from common import config

router = APIRouter()


#: 打进包里的三个来源。相对 PROJECT_DIR。
_SOURCES = ("run_worker.py", "worker", "common")

#: 无论哪种模式都不打包的目录名（任意层级，按目录名匹配）。
_EXCLUDE_DIRS = frozenset({
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    # 服务端专属：PG 后端，import 期就要 asyncpg，而 worker 不装它。
    "pgdb",
})

#: 无论哪种模式都不打包的文件后缀。
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".so", ".log")


def _iter_files(root: str, rel_base: str):
    """遍历 ``root`` 下要打包的文件，产出 ``(绝对路径, 包内相对路径)``。"""
    if os.path.isfile(root):
        yield root, rel_base
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # 就地裁剪 dirnames，os.walk 不会再下探被删掉的目录
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in sorted(filenames):
            if fn.endswith(_EXCLUDE_SUFFIXES):
                continue
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, os.path.dirname(root))
            yield abs_path, rel


def _server_url(request: Request) -> str:
    """这次下载请求打到的地址，作为 worker 的默认 --server。"""
    base = str(request.base_url).rstrip("/")
    return base or f"http://127.0.0.1:{config.SERVER_PORT}"


def _start_sh(server_url: str) -> str:
    return f"""#!/usr/bin/env bash
# Amazon Scraper Worker 启动脚本
# 首次运行会自动创建虚拟环境并安装依赖，之后直接启动。
#
#   ./start.sh                      使用下载时的服务器地址
#   ./start.sh --server http://...  覆盖服务器地址
#   ./start.sh --concurrency 8      其余参数原样透传给 run_worker.py
set -euo pipefail
cd "$(dirname "$0")"

SERVER_URL="{server_url}"

if [ ! -d .venv ]; then
    echo "==> 首次启动：创建虚拟环境"
    python3 -m venv .venv
    echo "==> 安装依赖（可能需要几分钟）"
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi

# 调用方自己带了 --server 就不再补默认值，避免重复参数
for arg in "$@"; do
    if [ "$arg" = "--server" ]; then
        exec ./.venv/bin/python run_worker.py "$@"
    fi
done
exec ./.venv/bin/python run_worker.py --server "$SERVER_URL" "$@"
"""


def _start_bat(server_url: str) -> str:
    # CRLF 换行：Windows 的 cmd.exe 对 LF-only 的 .bat 有时会解析出错
    lines = [
        "@echo off",
        "REM Amazon Scraper Worker 启动脚本",
        "REM 首次运行会自动创建虚拟环境并安装依赖，之后直接启动。",
        "setlocal",
        'cd /d "%~dp0"',
        "",
        f'set "SERVER_URL={server_url}"',
        "",
        "if not exist .venv (",
        "    echo ==^> 首次启动：创建虚拟环境",
        "    python -m venv .venv",
        "    echo ==^> 安装依赖（可能需要几分钟）",
        "    .venv\\Scripts\\python.exe -m pip install --upgrade pip",
        "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt",
        ")",
        "",
        "echo %* | findstr /C:\"--server\" >nul",
        "if %errorlevel%==0 (",
        "    .venv\\Scripts\\python.exe run_worker.py %*",
        ") else (",
        '    .venv\\Scripts\\python.exe run_worker.py --server "%SERVER_URL%" %*',
        ")",
        "endlocal",
        "",
    ]
    return "\r\n".join(lines)


def _readme(server_url: str) -> str:
    return f"""Amazon Scraper Worker
=====================

服务器地址（已写进启动脚本，可用 --server 覆盖）：
    {server_url}

启动
----
macOS / Linux:
    chmod +x start.sh
    ./start.sh

Windows:
    双击 start.bat

首次启动会自动创建 .venv 并安装 requirements.txt 里的依赖，
需要几分钟；之后再启动会直接复用。

需要截图功能的话，装完依赖后还要下载浏览器内核：
    ./.venv/bin/python -m playwright install chromium      (macOS/Linux)
    .venv\\Scripts\\python.exe -m playwright install chromium  (Windows)
不需要截图就加 --no-screenshot 启动。

常用参数
--------
    --server URL            服务器地址
    --worker-id NAME        worker 标识（默认自动生成）
    --concurrency N         初始并发
    --zip-code 10001        配送邮编
    --no-screenshot         关闭截图
    --auto-restart-hours N  定时自动重启

更新代码
--------
在控制台「下载 Worker」里选「代码更新包」，解压覆盖本目录即可，
不需要重建虚拟环境。
"""


@router.get("/api/worker/download")
async def api_worker_download(request: Request, mode: str = Query("full")):
    """下载 Worker 安装包（`mode=full` 完整包 / `mode=update` 仅 .py 代码更新包）。

    `full` 含 `run_worker.py`、`worker/`、`common/`、`requirements.txt` 与
    自动生成的 `start.sh` / `start.bat` / `README.txt`，服务器地址已烘进
    启动脚本；`update` 只含 .py 文件，解压覆盖即可，不动虚拟环境。
    """
    mode = (mode or "full").strip().lower()
    if mode not in ("full", "update"):
        raise HTTPException(400, f"mode 只能是 full 或 update，收到: {mode}")

    only_py = mode == "update"
    server_url = _server_url(request)

    buf = io.BytesIO()
    # ZIP_DEFLATED + 固定 date_time：同一份代码打出的包字节稳定，便于校验
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in _SOURCES:
            root = os.path.join(config.PROJECT_DIR, src)
            if not os.path.exists(root):
                continue
            for abs_path, rel in _iter_files(root, src):
                if only_py and not rel.endswith(".py"):
                    continue
                try:
                    with open(abs_path, "rb") as f:
                        zf.writestr(f"worker-package/{rel}", f.read())
                except OSError:
                    # 单个文件读不到不该让整个下载失败
                    continue

        if not only_py:
            req = os.path.join(config.PROJECT_DIR, "requirements.txt")
            if os.path.isfile(req):
                with open(req, "rb") as f:
                    zf.writestr("worker-package/requirements.txt", f.read())
            zf.writestr("worker-package/start.sh", _start_sh(server_url))
            zf.writestr("worker-package/start.bat", _start_bat(server_url))
            zf.writestr("worker-package/README.txt", _readme(server_url))
            # start.sh 需要可执行位：ZIP 的 external_attr 高 16 位是 st_mode
            for info in zf.filelist:
                if info.filename.endswith("start.sh"):
                    info.external_attr = (0o755 & 0xFFFF) << 16

    buf.seek(0)
    filename = "worker-update.zip" if only_py else "worker-package.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
