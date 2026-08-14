#!/usr/bin/env python3
"""上机环境体检：在**目标机器**上跑，确认这台机器真的能跑起来。

    python tools/preflight.py                 # 全套
    python tools/preflight.py --skip-slow     # 跳过建库/建表那几项

每一项都是**实测**，不是读配置：PostgreSQL 的版本/扩展/编码、asyncpg 能不能连、
DDL 能不能建、分区能不能创、磁盘够不够、依赖装没装齐。部署第一件事应该是把这些
假设变成实测，而不是直接起服务然后看日志报错。

退出码：0 全过；1 有硬失败；2 只有警告。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import List, Tuple

Result = Tuple[str, str, str]      # (状态, 项目, 说明)  状态 ∈ OK/WARN/FAIL

_results: List[Result] = []


def ok(item, note=""):   _results.append(("OK", item, note))
def warn(item, note=""): _results.append(("WARN", item, note))
def fail(item, note=""): _results.append(("FAIL", item, note))


# ---------------------------------------------------------------- 检查项

def check_python():
    v = sys.version_info
    (ok if v >= (3, 10) else fail)(
        "Python 版本", f"{v.major}.{v.minor}.{v.micro}（需 ≥ 3.10）")


def check_imports():
    need = {
        "asyncpg": "PG 驱动 —— 正式后端，必需",
        "fastapi": "服务端",
        "uvicorn": "服务端",
        "openpyxl": "导出",
        "aiosqlite": "SQLite 回滚兜底路径（DB_BACKEND=sqlite）",
    }
    prod_parser = {
        "selectolax": "**生产解析引擎**。不装它 worker 会走 lxml 回退路径，"
                      "采出来的数据与预期不同",
        "lxml": "回退解析引擎",
        "dateparser": "配送日期解析",
        "curl_cffi": "worker 的 HTTP 客户端",
    }
    for mod, why in need.items():
        try:
            __import__(mod)
            ok(f"import {mod}", why)
        except ImportError:
            fail(f"import {mod}", f"缺失 —— {why}")
    for mod, why in prod_parser.items():
        try:
            __import__(mod)
            ok(f"import {mod}", why)
        except ImportError:
            warn(f"import {mod}", f"缺失 —— {why}（只跑 server 可以不装）")


def check_env():
    for var, why, hard in (
        ("PG_DSN", "PostgreSQL 连接串", True),
        # DB_BACKEND 不设 = postgres（正式后端），所以「未设置」是正常的，
        # 不该报硬失败。只有显式设成别的值才值得提醒（见下面那条）。
        ("DB_BACKEND", "不设即 postgres（正式后端）", False),
        ("EXPORT_TOKEN", "增量导出鉴权。**不配就是无鉴权对公网开放**", False),
        ("SCRAPER_INSTANCE_ID", "实例标识。不配则两个克隆部署无法区分", False),
    ):
        v = os.environ.get(var, "").strip()
        if v:
            shown = "***" if "TOKEN" in var else v
            ok(f"env {var}", shown)
        elif hard:
            fail(f"env {var}", f"未设置 —— {why}")
        else:
            warn(f"env {var}", f"未设置 —— {why}")

    _backend = os.environ.get("DB_BACKEND", "").strip()
    if _backend == "sqlite":
        warn("env DB_BACKEND",
             "显式设成了 'sqlite' —— 那是回滚兜底路径，不是正式后端。"
             "新部署请留空或设 'postgres'。")
    elif _backend not in ("postgres", ""):
        fail("env DB_BACKEND", f"值是 {_backend!r}，只认 'postgres' / 'sqlite'")


def check_disk():
    try:
        from common import config
        path = config.PROJECT_DIR
    except Exception:
        path = os.getcwd()
    du = shutil.disk_usage(path)
    free_gb = du.free / 2**30
    total_gb = du.total / 2**30
    note = f"{free_gb:.1f} GB 可用 / {total_gb:.1f} GB 总量（{path}）"
    if free_gb < 10:
        fail("磁盘", note + " —— 低于 10GB，保留期会立刻进应急裁剪")
    elif free_gb < 30:
        warn("磁盘", note + " —— 低于 30GB，按 10 万/天约 200MB/天估，留意增长")
    else:
        ok("磁盘", note)


def check_cpu_mem():
    try:
        import multiprocessing
        cores = multiprocessing.cpu_count()
    except Exception:
        cores = 0
    mem_gb = 0.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_gb = int(line.split()[1]) / 2**20
                    break
    except Exception:
        pass
    note = f"{cores} 核 / {mem_gb:.1f} GB"
    if cores < 2:
        fail("CPU/内存", note + " —— 规划是 2 核；单核下 PG 与 uvicorn 抢同一个核")
    elif mem_gb < 3.5:
        warn("CPU/内存", note + " —— 规划是 4GB；shared_buffers 建议值需下调")
    else:
        ok("CPU/内存", note)


async def _pg_checks(dsn: str, skip_slow: bool):
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        ver = await conn.fetchval("SHOW server_version")
        major = int(str(ver).split(".")[0])
        (ok if major >= 14 else fail)(
            "PG 版本", f"{ver}（需 ≥ 14：声明式分区 + FOR UPDATE SKIP LOCKED；实测 16.x / 17.x 均通过）")

        trgm = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname='pg_trgm'")
        if trgm:
            ok("扩展 pg_trgm", trgm)
        else:
            can = await conn.fetchval(
                "SELECT 1 FROM pg_available_extensions WHERE name='pg_trgm'")
            (warn if can else fail)(
                "扩展 pg_trgm",
                "未安装但可安装（CREATE EXTENSION pg_trgm）" if can
                else "不可用 —— 全文搜索会退化")

        enc = await conn.fetchval("SHOW server_encoding")
        (ok if enc == "UTF8" else fail)("编码", enc)

        coll = await conn.fetchval(
            "SELECT datcollate FROM pg_database WHERE datname=current_database()")
        if coll in ("C", "C.UTF-8", "POSIX"):
            ok("排序规则", f"{coll} —— 与 SQLite 的 BINARY 一致")
        else:
            warn("排序规则",
                 f"{coll} —— 非 C，TEXT 的 ORDER BY 结果可能与 SQLite 不同。"
                 "建库时用 LC_COLLATE='C'，或接受排序差异")

        for guc, want in (("default_toast_compression", "lz4"),
                          ("synchronous_commit", "on")):
            got = await conn.fetchval(
                "SELECT setting FROM pg_settings WHERE name=$1", guc)
            (ok if got == want else warn)(f"参数 {guc}", f"{got}（建议 {want}）")

        sb = await conn.fetchval(
            "SELECT setting::bigint*8192 FROM pg_settings WHERE name='shared_buffers'")
        ok("shared_buffers", f"{sb/2**20:.0f} MB（4GB 机器建议 1024 MB）")

        su = await conn.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
        (ok if su else warn)(
            "建表权限", "当前角色是 superuser" if su
            else "非 superuser —— 需确认能建 schema/表/分区/扩展")

        if not skip_slow:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS _preflight_probe")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _preflight_probe.t "
                "(seq bigserial, v text) PARTITION BY RANGE (seq)")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _preflight_probe.t_p0 "
                "PARTITION OF _preflight_probe.t FOR VALUES FROM (MINVALUE) TO (100)")
            await conn.execute("INSERT INTO _preflight_probe.t(v) VALUES('x')")
            n = await conn.fetchval("SELECT count(*) FROM _preflight_probe.t")
            await conn.execute("DROP SCHEMA _preflight_probe CASCADE")
            (ok if n == 1 else fail)("分区表建/写/删", f"探针行数 {n}")

            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock(982451653)")
            if got:
                await conn.execute("SELECT pg_advisory_unlock(982451653)")
            (ok if got else fail)("advisory lock", "relay 单例靠它保证")
    finally:
        await conn.close()


def check_pg(skip_slow: bool):
    dsn = os.environ.get("PG_DSN", "").strip()
    if not dsn:
        try:
            from common import config
            dsn = config.PG_DSN
        except Exception:
            fail("PG 连接", "PG_DSN 未设置且无法从 config 读取")
            return
    try:
        import asyncio
        asyncio.run(_pg_checks(dsn, skip_slow))
    except Exception as e:                                   # noqa: BLE001
        fail("PG 连接", f"{type(e).__name__}: {e}")


def check_route_order():
    """增量导出端点必须注册在 /api/export/{batch_name} 之前，否则静默 404。

    判定逻辑的**唯一真源**是 `server/routing.py:route_order_ok`。这里以前是
    一份独立实现（连 `_flatten_routes` 都自己抄了一遍），而 ARCH_PLAN 当年就
    记着那份副本是坏的——`catch is None` 时落到 else 报绿，也就是查找逻辑失效
    时它反而说「没问题」。副本会独立腐坏，所以现在只留一份。

    注意本项只是**结构**层。完整守卫在
    `tests/test_incremental_export.py::RouteOrderTests`（结构 + 行为 + 源码
    三层），那才是 CI 里跑的那道；本项是上机时顺手再验一次。
    """
    try:
        from server.app import app
        from server.routing import route_order_ok
    except Exception as e:                                   # noqa: BLE001
        fail("路由顺序", f"import server 失败: {type(e).__name__}: {e}")
        return
    ok_, msg = route_order_ok(app.routes)
    (ok if ok_ else fail)("路由顺序", msg)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 5 上机前置检查")
    ap.add_argument("--skip-slow", action="store_true",
                    help="跳过建库/建表探针")
    a = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    check_python()
    check_cpu_mem()
    check_disk()
    check_imports()
    check_env()
    check_pg(a.skip_slow)
    check_route_order()

    width = max(len(i) for _, i, _ in _results) + 2
    print(f"\n{'='*78}\nPhase 5 上机前置检查\n{'='*78}")
    icon = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
    for status, item, note in _results:
        print(f"{icon[status]} {item:<{width}} {note}")

    n_fail = sum(1 for s, _, _ in _results if s == "FAIL")
    n_warn = sum(1 for s, _, _ in _results if s == "WARN")
    print(f"\n{'='*78}")
    if n_fail:
        print(f"❌ {n_fail} 项硬失败，{n_warn} 项警告 —— 先修硬失败再往下走")
        return 1
    if n_warn:
        print(f"⚠️  全部通过，但有 {n_warn} 项警告 —— 逐条确认是有意的再往下走")
        return 2
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
