"""把 `tests/extension_page_spec.js` 接进 pytest。

------------------------------------------------------------------------
为什么绕这么一圈，而不是直接 `node tests/extension_page_spec.js`
------------------------------------------------------------------------
这个仓库的门禁是**一条命令**（`pytest tests/`，外加 `unittest discover`）。
一份只有知道它存在的人才会去跑的测试，等于没有 —— 插件的页面识别层恰恰是
最容易在无人察觉时腐坏的地方（Amazon 改个类名就静默失效，见那份 spec 的头注）。
所以让它跟着主门禁一起跑。

------------------------------------------------------------------------
依赖是**可选**的，缺了就 skip
------------------------------------------------------------------------
它需要 Node + jsdom，而这两样都不在仓库的 Python 依赖里。处理方式照抄
`tests/pgdb/`：连不上就 skip，不让一台没装 Node 的开发机变红。

    npm install jsdom      # 想跑这一列的话，仓库根目录执行一次

写成 `unittest.TestCase`：`python -m unittest discover -s tests` 那一列的
加载器只认 TestCase 子类，裸函数用例在那一列会被静默跳过。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = os.path.join(_REPO_ROOT, "tests", "extension_page_spec.js")


def _node() -> str | None:
    return shutil.which("node")


def _has_jsdom(node: str) -> bool:
    """jsdom 装没装。用 node 自己去 resolve，别猜 node_modules 的位置。"""
    probe = subprocess.run(
        [node, "-e", "require.resolve('jsdom')"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    return probe.returncode == 0


class ExtensionPageJsTests(unittest.TestCase):

    def test_page_detection_spec_passes(self):
        node = _node()
        if not node:
            self.skipTest("没装 Node，跳过插件页面识别层的用例")
        if not _has_jsdom(node):
            self.skipTest("没装 jsdom（仓库根目录 `npm install jsdom` 后可跑）")

        proc = subprocess.run(
            [node, _SPEC], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        # 失败详情在 stdout（spec 自己打的逐条 FAIL 行），一并塞进断言消息里 ——
        # 只报一个退出码的话，得手工再跑一遍才知道是哪一条炸了。
        self.assertEqual(
            proc.returncode, 0,
            f"extension/src/page.js 的规格用例失败：\n{proc.stdout}\n{proc.stderr}",
        )
