"""tests/pgdb/sqlite_snapshot.py —— SQLite 裁判快照的录制 / 校验 CLI（Phase 0.2）。

  # 趁 SQLite 还在，把它的返回录成快照（**只允许在 SQLite 侧跑**）
  .venv/bin/python -m tests.pgdb.sqlite_snapshot record

  # 拿 PG 侧对快照（tests/pgdb/test_sqlite_snapshot.py 跑的就是这个）
  DB_BACKEND=postgres .venv/bin/python -m tests.pgdb.sqlite_snapshot verify

  # 连跑两次 record 比对自身，证明录的东西是确定性的（不写盘）
  .venv/bin/python -m tests.pgdb.sqlite_snapshot selfcheck

做法照抄 ``tests/golden/run.py`` 的 record / verify / selfcheck 三件套。

**这份快照录的是「改动之前的答案」**，用途有二：
  1. 它是 Phase 1 三条查询改写的第二道网 —— 黄金基线的搜索样本全是单一大小写
     ASCII、``brand`` 恒为 ``GoldenBrand``，等价性推理错了黄金不会红，这里会红。
  2. SQLite 万一将来退役，这批「以 SQLite 为唯一裁判」的答案不会跟着夹具一起消失。

**本轮只录，不拆。** ``seeded_sqlite`` 夹具与 88 条差分用例一个字没动：
SQLite 去留未决，删夹具等于提前替它做决定。两套并存。

用例清单不在这里重抄一遍，而是**直接从既有用例的 parametrize 标记里读出来**
（``_params()``）—— 抄一份就会漂，读出来不会。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
RESULTS_READ_SNAPSHOT = os.path.join(SNAP_DIR, "sqlite_results_read.json")
ADMIN_SNAPSHOT = os.path.join(SNAP_DIR, "sqlite_admin.json")

VERSION = 1

# ===================================================================== 文件头
#: 写进 JSON 里的 ``__header__``。JSON 没有注释，而这段话必须跟着文件走 ——
#: 拿到快照的人第一眼要看见「这不是手写的期望值」。
_COMMON_HEADER = [
    "本文件是录制品，不是手写的期望值。",
    "",
    "期望值的来源是 SQLite（common/database.py 的实现）在本仓库当时的**真实返回**，",
    "不是「我们认为对的」。裁判是 SQLite 这个既有实现本身。",
    "",
    "因此：",
    "  * PG 侧对不上时，先假定是 PG 改错了，不是快照错了。",
    "  * 任何一处期望值的修改都必须先在 SQLite 侧重新论证 —— 写明改的是哪一条行为、",
    "    为什么改，然后重录（`python -m tests.pgdb.sqlite_snapshot record`）。",
    "    直接编辑 JSON 让用例转绿，等于把差分网当场剪断。",
    "  * 重录只允许在 SQLite 侧跑：record 直接构造 common.database.Database，",
    "    不读 DB_BACKEND；拿 PG 的返回覆盖这份文件就没有裁判了。",
]

_FIXTURE_HEADER = [
    "",
    "————————————————————————————————————————————————",
    "夹具里每一批数据各自守着一条决策。改这些行 = 改判据，不是「改测试数据」：",
    "",
    "  * B0BSLASH05 / title 'back\\slash item' / brand 'Back\\Brand'",
    "      守 D-16：每一处 LIKE 都带 ESCAPE ''。SQLite 的 LIKE 没有转义字符，",
    "      PG 默认拿反斜杠当转义 —— 不显式 ESCAPE '' 的话，模式里的反斜杠会静默",
    "      改变命中的行集（D-16 实测：同一个 search，sqlite 删 back\\slash 那行、",
    "      pg 删 backslash 那行，两边都回 deleted:1，调用方察觉不到）。",
    "      守它的 case：search=back\\slash / search=\\ / search=\\\\。",
    "",
    "  * B0ACCENT06 'ÉCLAIR CAFÉ' / B0ACCENT07 'éclair café'",
    "      守 D-5：谓词是 ascii_lower(col) LIKE ascii_lower(?)，**不是 ILIKE**。",
    "      SQLite 的 LIKE 只折 ASCII 大小写，É 与 é 不相等；ILIKE 折全 Unicode，",
    "      会把两行都匹上。这两行大小写互为镜像，就是为了让「折不折 Unicode」",
    "      在返回里现形。",
    "      守它的 case（录到的值，别记反）：",
    "        search=ÉCLAIR -> [B0ACCENT06]   （É 不折，CLAIR 折 -> 只中大写那行）",
    "        search=Éclair -> [B0ACCENT06]   （与上一条同解：差别只在 ASCII 部分）",
    "        search=éclair -> [B0ACCENT07]   （é 不折 -> 只中小写那行）",
    "      判据是「É 组和 é 组必须给出不同的那一行、且各只有一行」；",
    "      退化成 ILIKE 的表现是三条全变成两行都命中。",
    "",
    "  * B0PCT08 / title 'Gol%rand pct' / brand 'Pct_Brand'",
    "      守 LIKE 通配符字面量：用户输入里的 % 和 _ 今天是**活的通配符**",
    "      （search=Gol_en 能匹到 'GoldenBrand Widget'，search=% 匹全表）。",
    "      两个引擎在这点上一致，所以这个「bug」要免费保留 —— 谁顺手给用户输入",
    "      加转义，这几条就会红。",
    "      守它的 case：search=Gol%rand / search=Gol_en / search=% /",
    "      search=___ / search=%%%。",
    "",
    "  * 值为 [\"raise\"] 的条目：这里**曾经**有 5 条 D-8（>=3 字符的 search 配 cursor_id",
    "      必 500，count 查询把搜索谓词连同参数一起剔了）。D-8 已修 —— count 的谓词",
    "      与参数改成同一时刻快照，不再靠 \"d.id\" 文本匹配猜哪个是游标谓词。",
    "      那 5 条已按本文件的规矩重录成正常返回值。若将来又冒出 [\"raise\"]，",
    "      那是**新**的崩溃，不是历史包袱，先查清楚再决定录不录。",
]

RESULTS_READ_HEADER = _COMMON_HEADER + [
    "",
    "————————————————————————————————————————————————",
    "内容：tests/pgdb/test_results_read.py 里依赖 seeded_sqlite 夹具的 93 条用例的",
    "SQLite 侧返回。用例清单是从那些用例的 parametrize 标记里**读出来**的，不是抄的。",
    "每条的值是 (\"ok\", 返回值) 或 (\"raise\",)，与该文件的 _call/_iter 逐字同源。",
] + _FIXTURE_HEADER

ADMIN_HEADER = _COMMON_HEADER + [
    "",
    "————————————————————————————————————————————————",
    "内容：tests/pgdb/test_admin.py 的 run_sqlite() 输出（scenario() 的逐步记录）。",
    "顺序有意义：diff() 先比键序列再比值，步骤少一条 / 多一条同样是差异。",
    "",
    "这里面同样有几条不是「随便记的数」：",
    "  * like.* 六条守 D-5（SQLite 的 LIKE 折 ASCII 大小写，PG 的不折 ——",
    "    golden / GOLDEN / GoldenBrand / acme / ACME 必须给出同一批 ASIN）。",
    "  * clear.next_batch_id / clear.next_task_id / burn.ids 守自增烧号的语义",
    "    （DELETE 全表 + 删 sqlite_sequence == identity RESTART；冲突的 INSERT 仍然烧号）。",
    "  * types.* 五条守落库形状：时间戳是 str 且形如 'YYYY-MM-DD HH:MM:SS'、",
    "    布尔是 int 不是 True/False。",
]


# ============================================================ 用例清单（读出来的）
def _params(func) -> Tuple[str, List[Any]]:
    """从既有用例的 ``@pytest.mark.parametrize`` 里把参数清单读出来。

    **刻意不在这里重抄一份清单**：抄一份就多一个漂移源，而这份快照的全部价值
    就在于它和那 93 条差分用例说的是同一件事。
    """
    marks = [m for m in getattr(func, "pytestmark", []) if m.name == "parametrize"]
    if len(marks) != 1:
        raise RuntimeError(f"{func.__name__} 的 parametrize 标记不是恰好一个：{marks!r}")
    argnames, argvalues = marks[0].args[0], list(marks[0].args[1])
    return argnames, argvalues


def _kw_id(kw: dict) -> str:
    """与 test_results_read.py 里 ids=lambda 的写法逐字一致。"""
    return ",".join(f"{a}={b}" for a, b in kw.items()) or "default"


def build_cases() -> List[Tuple[str, str, str, str, tuple, dict]]:
    """-> [(group, case_id, kind, method, args, kwargs)]，顺序即录制顺序。"""
    from tests.pgdb import test_results_read as T

    out: List[Tuple[str, str, str, str, tuple, dict]] = []

    for name, args in _params(T.test_simple_reads_match_sqlite)[1]:
        out.append(("simple_reads", f"{name}{args!r}", "call", name, tuple(args), {}))

    for group, func, kind in (
        ("get_results", T.test_get_results_matches_sqlite, "call"),
        ("count_bug", T.test_search_with_cursor_no_longer_500s, "call"),
        ("short_term_search", T.test_short_term_search_with_cursor_still_works, "call"),
        ("iter_results", T.test_iter_results_matches_sqlite, "iter"),
    ):
        for kw in _params(func)[1]:
            method = "get_results" if kind == "call" else "iter_results"
            out.append((group, _kw_id(kw), kind, method, (), dict(kw)))

    seen = set()
    for group, cid, *_ in out:
        if (group, cid) in seen:
            raise RuntimeError(f"case_id 撞名：{group}/{cid} —— 快照是个 map，撞名会丢用例")
        seen.add((group, cid))
    return out


# ============================================================ 跑一遍，取返回
def _jsonable(v: Any) -> Any:
    """录进 JSON 前先过一遍：tuple -> list，其余原样。

    比对时两侧都走这个函数 + 一次 json round-trip，所以 tuple/list 的差别不会
    被误报成差异（``_call`` 返回的是 tuple，从 JSON 读回来的是 list）。
    """
    return json.loads(json.dumps(v, ensure_ascii=False, sort_keys=True, allow_nan=False))


async def _run_case(db, kind: str, method: str, args: tuple, kwargs: dict):
    """用 test_results_read.py 自己的 _call / _iter 跑 —— 崩不崩的判定必须同源。"""
    from tests.pgdb.test_results_read import _call, _iter
    if kind == "iter":
        return await _iter(db, **kwargs)
    return await _call(db, method, *args, **kwargs)


async def capture_results_read_sqlite() -> Dict[str, Dict[str, Any]]:
    """SQLite 侧：**每条 case 一个全新的库**，和 seeded_sqlite 夹具（function 作用域）
    的隔离度一致（count_bug 那三条历史上会在库里抛异常，共用连接等于给自己埋雷；
    D-8 修好之后它们不再抛，但每条一个新库这条纪律照旧——它保的是隔离，不是崩溃）。"""
    from common.database import Database as SqliteDatabase
    from tests.pgdb.test_results_read import _seed

    out: Dict[str, Dict[str, Any]] = {}
    for group, cid, kind, method, args, kwargs in build_cases():
        tmp = tempfile.mkdtemp(prefix="snap_sqlite_")
        db = SqliteDatabase(os.path.join(tmp, "cmp.db"))
        try:
            await db.connect()
            await _seed(db._db)
            out.setdefault(group, {})[cid] = _jsonable(await _run_case(
                db, kind, method, args, kwargs))
        finally:
            await db.close()
            shutil.rmtree(tmp, ignore_errors=True)
    return out


async def capture_results_read_pg() -> Dict[str, Dict[str, Any]]:
    """PG 侧：全部是只读调用，共用**一个**临时库。

    93 条各建一个 scratch database 要多花约 50s（实测每条 setup ~0.55s），
    而这些 case 一行都不写库 —— 隔离在这里买不到任何东西。
    """
    from tests.pgdb.helpers import scratch_database
    from tests.pgdb.test_results_read import _patch_media, _seed

    out: Dict[str, Dict[str, Any]] = {}
    async with scratch_database("snapshot") as db:
        _patch_media(db)
        await _seed(db._db)
        for group, cid, kind, method, args, kwargs in build_cases():
            out.setdefault(group, {})[cid] = _jsonable(await _run_case(
                db, kind, method, args, kwargs))
    return out


async def capture_admin_sqlite() -> List[List[Any]]:
    from tests.pgdb.test_admin import run_sqlite
    return _jsonable([list(step) for step in await run_sqlite()])


async def capture_admin_pg() -> List[List[Any]]:
    from tests.pgdb.test_admin import run_postgres
    return _jsonable([list(step) for step in await run_postgres()])


# ============================================================ 比对
def _short(v: Any, n: int = 100) -> str:
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[:n] + f"…(共{len(s)}字符)"


def _diff_paths(g: Any, a: Any, path: str = "", out: List[str] = None,
                limit: int = 6) -> List[str]:
    """定位到**具体字段**再打印。

    一条 get_results 的返回是 8 行 × 50 列；整包对拼出来能刷满一屏，红一条就没法读了。
    这里递归下钻到第一批不等的叶子，最多报 limit 处。
    """
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(g, dict) and isinstance(a, dict):
        for k in sorted(set(g) | set(a)):
            if len(out) >= limit:
                break
            if k not in g:
                out.append(f"{path}.{k}: 快照里没有这个键，实测有 {_short(a[k])}")
            elif k not in a:
                out.append(f"{path}.{k}: 实测少了这个键（快照={_short(g[k])}）")
            elif g[k] != a[k]:
                _diff_paths(g[k], a[k], f"{path}.{k}", out, limit)
    elif isinstance(g, list) and isinstance(a, list):
        if len(g) != len(a):
            out.append(f"{path or '.'}: 长度 快照={len(g)} 实测={len(a)}"
                       f"  快照={_short(g)}  实测={_short(a)}")
        for i, (x, y) in enumerate(zip(g, a)):
            if len(out) >= limit:
                break
            if x != y:
                _diff_paths(x, y, f"{path}[{i}]", out, limit)
    else:
        out.append(f"{path or '.'}: 快照={_short(g)}  实测={_short(a)}")
    return out


def diff_results_read(golden: Dict[str, Dict[str, Any]],
                      actual: Dict[str, Dict[str, Any]]) -> List[str]:
    problems: List[str] = []
    for group in sorted(set(golden) | set(actual)):
        g, a = golden.get(group, {}), actual.get(group, {})
        for cid in sorted(set(g) | set(a)):
            if cid not in g:
                problems.append(f"{group}[{cid}]: 快照里没有这条（用例清单变了？先重录）")
            elif cid not in a:
                problems.append(f"{group}[{cid}]: 本次没跑到这条（用例清单变了？先重录）")
            elif g[cid] != a[cid]:
                for line in _diff_paths(g[cid], a[cid]):
                    problems.append(f"{group}[{cid}] {line}")
    return problems


def diff_admin(golden: List[List[Any]], actual: List[List[Any]]) -> List[str]:
    """键序列也比 —— 与 test_admin.diff() 同构。"""
    problems: List[str] = []
    kg = [k for k, _ in golden]
    ka = [k for k, _ in actual]
    if kg != ka:
        problems.append(f"步骤序列不一致: {set(kg) ^ set(ka)}")
    dg, da = dict((k, v) for k, v in golden), dict((k, v) for k, v in actual)
    for k in kg:
        if k in da and dg[k] != da[k]:
            for line in _diff_paths(dg[k], da[k]):
                problems.append(f"{k} {line}")
    return problems


def load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(path: str, payload: dict) -> None:
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ============================================================ CLI
def cmd_record() -> int:
    groups = asyncio.run(capture_results_read_sqlite())
    _dump(RESULTS_READ_SNAPSHOT, {
        "__header__": RESULTS_READ_HEADER,
        "version": VERSION,
        "expected_source": "sqlite",
        "recorded_from": "tests/pgdb/test_results_read.py :: seeded_sqlite",
        "groups": groups,
    })
    n = sum(len(v) for v in groups.values())
    print(f"✅ 已录制 {n} 条（{ {k: len(v) for k, v in groups.items()} }）"
          f" → {RESULTS_READ_SNAPSHOT}")

    steps = asyncio.run(capture_admin_sqlite())
    _dump(ADMIN_SNAPSHOT, {
        "__header__": ADMIN_HEADER,
        "version": VERSION,
        "expected_source": "sqlite",
        "recorded_from": "tests/pgdb/test_admin.py :: run_sqlite()",
        "steps": steps,
    })
    print(f"✅ 已录制 {len(steps)} 步 → {ADMIN_SNAPSHOT}")
    return 0


def cmd_verify() -> int:
    rc = 0
    for path, capture, differ, key in (
        (RESULTS_READ_SNAPSHOT, capture_results_read_pg, diff_results_read, "groups"),
        (ADMIN_SNAPSHOT, capture_admin_pg, diff_admin, "steps"),
    ):
        if not os.path.exists(path):
            print(f"❌ 快照不存在：{path}\n   先跑 `python -m tests.pgdb.sqlite_snapshot record`")
            return 2
        golden = load(path)[key]
        problems = differ(golden, asyncio.run(capture()))
        name = os.path.basename(path)
        if problems:
            rc = 1
            print(f"❌ {name}：{len(problems)} 处与 SQLite 快照不符\n")
            for line in problems:
                print(f"  {line}")
        else:
            n = sum(len(v) for v in golden.values()) if key == "groups" else len(golden)
            print(f"✅ {name}：{n} 条与 SQLite 快照完全一致")
    return rc


def cmd_selfcheck() -> int:
    """录制本身必须是确定性的。这一步红 = 录出来的快照是废的。"""
    rc = 0
    a = asyncio.run(capture_results_read_sqlite())
    b = asyncio.run(capture_results_read_sqlite())
    d1 = diff_results_read(a, b)
    print(f"{'✅' if not d1 else '❌'} results_read：两次独立录制"
          f"{'完全一致' if not d1 else f'有 {len(d1)} 处差异'}"
          f"（{sum(len(v) for v in a.values())} 条）")
    for line in d1:
        print(f"  {line}")
    a2 = asyncio.run(capture_admin_sqlite())
    b2 = asyncio.run(capture_admin_sqlite())
    d2 = diff_admin(a2, b2)
    print(f"{'✅' if not d2 else '❌'} admin：两次独立录制"
          f"{'完全一致' if not d2 else f'有 {len(d2)} 处差异'}（{len(a2)} 步）")
    for line in d2:
        print(f"  {line}")
    return 1 if (d1 or d2) else rc


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    return {
        "record": cmd_record,
        "verify": cmd_verify,
        "selfcheck": cmd_selfcheck,
    }.get(cmd, lambda: (print(__doc__), 2)[1])()


if __name__ == "__main__":
    raise SystemExit(main())
