"""worker 被 `kill -9` 之后，截图子进程与 Chromium 不许留下来。

------------------------------------------------------------------------
背景：为什么 PR #6 修完 Ctrl+C，`kill -9` 还是会留一堆 Chromium
------------------------------------------------------------------------
回收截图进程树的逻辑**全部**在 worker 那边（`engine.py` 的
``_reap_screenshot_descendants``，走 ``os.killpg``）。那条路要成立有个前提：
worker 还活着、还能跑代码。``kill -9`` 发的是 **SIGKILL——不可捕获**，
``_cleanup()`` 一行都执行不到，于是整棵树被 init 收养后继续跑。

**这不是启动方式的问题。** 实测对照过 ``start_new_session`` 的两种取值：

    start_new_session=True    CHILD 存活 ppid=1   GRANDCHILD 存活
    start_new_session=False   CHILD 存活 ppid=1   GRANDCHILD 存活

一模一样。所以修法只能是把清理从「父驱动」改成「子自查」：
``worker/screenshot.py`` 里的看门狗线程定期看一眼 ``getppid()``，变了就
``killpg(0, SIGKILL)`` 把自己整个进程组（含 Chromium 全家）一起收掉。

------------------------------------------------------------------------
这份文件怎么测
------------------------------------------------------------------------
**不起真的 Playwright。** 那要下浏览器、要服务器，慢且脆，而这里要证明的
命题很窄：「父进程没了之后，本进程组会不会自己消失」。所以用一个**结构等价**
的三层替身：

    中间进程（扮 worker）-> 子进程（扮 screenshot.py，setsid 成组长）
                              -> 孙进程（扮 Chromium）

子进程里跑的是**真的** ``_start_parent_death_watchdog``（从
``worker/screenshot.py`` import 进去，不是复制一份），所以测的是生产代码本身。

⚠ 这些用例会真的 fork 进程、真的 SIGKILL，并且必然要**等满**看门狗的轮询周期
＋宽限期。为了不让门禁变慢，用例里把那两个常数压到零点几秒——压的是
``worker.screenshot`` 模块上的属性，看门狗运行时读的就是它们。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 压缩后的轮询/宽限，用例总时长因此在 2~3 秒量级。
_INTERVAL = 0.3
_GRACE = 0.3


def _alive(pid: int) -> bool:
    """进程是否**真的**还在跑。

    不能只用 ``os.kill(pid, 0)``：僵尸进程（已死但父进程没 wait）对它同样
    返回成功，会把「已经杀掉了」误判成「还活着」。Linux 上再查一次
    ``/proc/<pid>/stat`` 的状态位排掉 Z。
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        return True          # 非 Linux：拿不到状态位，按活着算（保守方向）


#: 孙进程 = 扮 Chromium。只管睡，不参与任何清理 —— 真的 Chromium 也一样，
#: 它不知道 worker 是谁、更不会自己退。
_GRANDCHILD = "import time; time.sleep(120)"

#: 子进程 = 扮 screenshot.py。跑**真的**看门狗。
_CHILD = textwrap.dedent(f"""
    import logging, os, subprocess, sys, time
    sys.path.insert(0, {_REPO!r})
    logging.basicConfig(level=logging.CRITICAL)   # 用例不需要它的日志

    import worker.screenshot as S
    S._PARENT_WATCH_INTERVAL = {_INTERVAL!r}
    S._PARENT_DEATH_GRACE = {_GRACE!r}

    g = subprocess.Popen([sys.executable, "-c", {_GRANDCHILD!r}])
    # worker=None / loop=None：本用例不验「优雅收尾」那一步，只验硬杀那一步。
    # 看门狗对这两个参数是允许 None 的（那一段套了 if 判断）。
    started = S._start_parent_death_watchdog(None, None)
    print(f"{{os.getpid()}} {{os.getpgid(0)}} {{g.pid}} {{int(started is not None)}}", flush=True)
    time.sleep(120)
""")


def _worker_src(new_session: bool) -> str:
    """中间进程 = 扮 run_worker.py。它会被 SIGKILL 掉。"""
    return textwrap.dedent(f"""
        import os, subprocess, sys, time
        p = subprocess.Popen([sys.executable, "-c", {_CHILD!r}],
                             start_new_session={new_session},
                             stdout=subprocess.PIPE, text=True)
        print(str(os.getpid()) + " " + p.stdout.readline().strip(), flush=True)
        time.sleep(120)
    """)


@unittest.skipUnless(hasattr(os, "killpg"), "看门狗的硬杀走 killpg，非 POSIX 不适用")
class OrphanedScreenshotTreeTests(unittest.TestCase):

    def setUp(self):
        self._cleanup_pids: list[int] = []

    def tearDown(self):
        # 用例失败时别把进程留给下一条用例（以及跑门禁的人）
        for pid in self._cleanup_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _spawn_tree(self, new_session=True):
        """起三层，返回 (worker_pid, child_pid, grandchild_pid, 看门狗是否启用)。"""
        w = subprocess.Popen([sys.executable, "-c", _worker_src(new_session)],
                             stdout=subprocess.PIPE, text=True)
        line = w.stdout.readline().split()
        self.assertEqual(len(line), 5, f"子进程握手行不对：{line!r}")
        wp, cp, _cpg, gp, armed = (int(x) for x in line)
        self._cleanup_pids += [wp, cp, gp]
        self._w = w
        return wp, cp, gp, bool(armed)

    def _wait_gone(self, pid, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _alive(pid):
                return True
            time.sleep(0.1)
        return not _alive(pid)

    def test_kill_9_on_the_worker_takes_chromium_down_too(self):
        """**这条就是真机上那堆残留 Chromium。**

        worker 被 SIGKILL，来不及做任何清理；截图子进程与它的浏览器必须**自己**
        走掉，而不是等人来 pkill。
        """
        wp, cp, gp, armed = self._spawn_tree()
        self.assertTrue(armed, "看门狗没启用 —— 那这条用例什么都没测")
        self.assertTrue(_alive(gp), "孙进程（扮 Chromium）没起来")

        os.kill(wp, signal.SIGKILL)      # 不可捕获：worker 一行清理都跑不到
        self._w.wait()

        budget = _INTERVAL + _GRACE + 8   # 轮询 + 宽限 + 宽裕的调度余量
        self.assertTrue(
            self._wait_gone(cp, budget),
            f"截图子进程在父进程被 kill -9 之后 {budget:.0f}s 仍在运行 —— "
            "看门狗没生效。worker 侧的 killpg 在 SIGKILL 下根本不会被调到，"
            "这一层是唯一的防线。")
        self.assertTrue(
            self._wait_gone(gp, 3),
            "截图子进程走了，但它的浏览器（孙进程）留下了 —— "
            "硬杀必须走 killpg(0) 覆盖整个进程组，只 kill 自己是不够的。")

    def test_watchdog_leaves_the_tree_alone_while_the_parent_is_alive(self):
        """反向：别为了能自杀，把「父进程还好好的」也误杀了。

        没有这条，一个把 ppid 判断写反的实现照样能让上面那条全绿 ——
        而它会在生产上把正在干活的截图进程随机杀掉。
        """
        wp, cp, gp, armed = self._spawn_tree()
        self.assertTrue(armed)

        # 睡过好几个轮询周期，看门狗有充分机会误动手
        time.sleep(_INTERVAL * 5 + _GRACE + 1)

        self.assertTrue(_alive(cp), "父进程还活着，截图子进程却自杀了")
        self.assertTrue(_alive(gp), "父进程还活着，浏览器却被杀了")

        os.kill(wp, signal.SIGKILL)
        self._w.wait()

    def test_watchdog_declines_to_arm_when_ppid_is_already_init(self):
        """启动时 ppid 就是 1 -> 明确不启用，并留下日志，而不是假装有保护。

        「ppid 变了」这个信号在那种情形下永远不会触发；而改成硬判 `ppid == 1`
        又会在容器里误杀（那儿 1 号进程可能就是正常的父进程）。所以选择关掉。
        """
        src = textwrap.dedent(f"""
            import logging, os, sys
            sys.path.insert(0, {_REPO!r})
            logging.basicConfig(level=logging.CRITICAL)
            import worker.screenshot as S
            # 直接把 getppid 打桩成 1，比真造一个孤儿稳定得多
            os.getppid = lambda: 1
            print(int(S._start_parent_death_watchdog(None, None) is not None), flush=True)
        """)
        out = subprocess.run([sys.executable, "-c", src],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "0",
                         "ppid 已经是 1 时不该启用看门狗（那个信号永远不会来）")


if __name__ == "__main__":
    unittest.main()
