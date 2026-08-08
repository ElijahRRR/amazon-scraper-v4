"""黄金样本 CLI。

  # 对当前实现录制基线（SQLite 版跑一次，PG 移植期间不要再跑）
  python -m tests.golden.run record

  # 校验当前实现是否仍与基线一致（移植期间反复跑）
  python -m tests.golden.run verify

  # 连跑两次 record 比对自身，证明场景是确定性的（不写盘）
  python -m tests.golden.run selfcheck
"""
from __future__ import annotations

import json
import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from tests.golden import scenario                       # noqa: E402
from tests.golden.harness import (                      # noqa: E402
    Recorder, diff_steps, isolated_server,
)

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

#: **只有一份基线，两个后端 verify 比的是同一份。** 三条推论，都是踩过才写下来的：
#:
#: 1. ``record`` 在 ``DB_BACKEND=postgres`` 下会**直接覆盖这份 sqlite 基线**。
#:    重录一律在 sqlite 侧做。
#: 2. 任何新增步骤，**两个后端的响应必须逐字节相同**，否则根本加不进来 ——
#:    这是选步骤时的硬筛，不是加完再调。
#: 3. 重录不是禁忌，但**未经声明的飘红一律先查错，不许顺手重录**。
#:    规矩是：先在提交信息里写明改了什么行为、为什么 -> 再 record ->
#:    基线 blob 的 diff 就是评审物。
#:
#: ⚠ 这份基线**盖不住的东西**，别把「全绿」读成「验过了」：
#:    * 六次 ``/api/results`` 一次都没传 ``batch_id``，``change_filter`` 只测了 ``new``
#:      （``scenario.py:212-233``）—— 翻页/筛选的查询改写在这里是零覆盖。
#:    * 从不执行 ``worker/parser.py``（场景直接 POST 造好的 dict），
#:      所以解析器改动在这里是**结构性**零覆盖，不是「可能红」。
#:    * 不比任何响应头（``harness.py`` 的 ``diff_steps`` 只比 status/content_type/body）。
#:    * 样本值太干净（价格全 19.99/33.50/0.00、brand 恒 GoldenBrand、
#:      搜索样本全是单一大小写 ASCII），哨兵语义与价格解析类改动改完这里逐字节不变。
BASELINE = os.path.join(SAMPLE_DIR, "baseline.json")


def capture(strict: bool = True) -> List[dict]:
    with isolated_server() as (client, ctx):
        rec = Recorder(client, ctx.tmp_root, strict=strict)
        scenario.run(rec)
        return rec.steps


def cmd_record() -> int:
    steps = capture()
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "steps": steps}, f,
                  ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"✅ 已录制 {len(steps)} 步 → {BASELINE}")
    return 0


def cmd_verify() -> int:
    if not os.path.exists(BASELINE):
        print(f"❌ 基线不存在：{BASELINE}\n   先跑 `python -m tests.golden.run record`")
        return 2
    with open(BASELINE, encoding="utf-8") as f:
        golden = json.load(f)["steps"]

    # 非 strict：跑完全程，一次给出全部差异
    actual = capture(strict=False)
    diffs = diff_steps(golden, actual)
    if not diffs:
        print(f"✅ {len(actual)} 步与基线完全一致")
        return 0
    print(f"❌ 与基线有 {len(diffs)} 处差异：\n")
    for line in diffs:
        print(f"  {line}")
    return 1


def cmd_selfcheck() -> int:
    """场景本身必须是确定性的。这一步失败说明夹具还有没擦干净的不可重复源，
    此时录出来的基线是废的——移植时会报一堆假差异，把真差异淹掉。"""
    first = capture()
    second = capture()
    diffs = diff_steps(first, second)
    if not diffs:
        print(f"✅ 两次独立运行完全一致（{len(first)} 步），场景是确定性的")
        return 0
    print(f"❌ 场景不确定：两次运行有 {len(diffs)} 处差异\n")
    for line in diffs:
        print(f"  {line}")
    return 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    return {
        "record": cmd_record,
        "verify": cmd_verify,
        "selfcheck": cmd_selfcheck,
    }.get(cmd, lambda: (print(__doc__), 2)[1])()


if __name__ == "__main__":
    raise SystemExit(main())
