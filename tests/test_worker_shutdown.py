"""Worker 关停：`stop()` 之后每个协程都必须退出，一个都不能赖着。

------------------------------------------------------------------------
背景：真机上按 Ctrl+C 卡死
------------------------------------------------------------------------
用户带 ``--auto-restart-hours 6`` 跑 worker，Ctrl+C 之后进程挂住不动，
日志停在：

    ⚙️ 工人池退出
    📡 任务补给协程退出
    （然后没有「🛑 Worker 已停止」，也不退出）

根因：``_auto_restart_timer`` 当时是裸 ``await asyncio.sleep(delay)``，
只认 ``CancelledError``、**不认关停信号**。而 ``run()`` 是
``await asyncio.gather(*coroutines)`` —— 要等**全部**协程结束。于是其它协程
几秒内退完并打日志，这一个还在睡 6 小时，gather 永不返回，``_cleanup()``
不执行，收尾日志永远不出现。

本文件的第一条用例就守这个。第二条守「别为了修它把功能改没了」——睡满
之后仍然要真的触发重启。

------------------------------------------------------------------------
为什么逐个协程测，而不是端到端起进程
------------------------------------------------------------------------
端到端要过 90s 启动自检、要代理、要服务器，慢且脆。而这里要证明的命题
很窄：「``stop()`` 之后，这个协程会不会在时限内返回」。逐个 await 它们
既快又能**指名道姓**说出是谁赖着——上面那个 bug 如果用端到端测，只会得到
一句「超时了」，还得再查一轮谁没退。
"""
from __future__ import annotations

import asyncio
import unittest

from worker.engine import Worker


def _mk(**kw):
    w = Worker(server_url="http://127.0.0.1:19999", worker_id="shutdown-test", **kw)
    w._running = True
    return w


class AutoRestartTimerShutdownTests(unittest.IsolatedAsyncioTestCase):

    async def test_timer_exits_on_stop_instead_of_sleeping_for_hours(self):
        """**这条对应真机上那次 Ctrl+C 卡死。**

        `--auto-restart-hours 6` 意味着 6 小时的等待。`run()` 的 gather 要等
        全部协程，所以这个协程不响应关停 = 整个进程不退出。
        """
        w = _mk(auto_restart_hours=6)
        task = asyncio.ensure_future(w._auto_restart_timer())
        await asyncio.sleep(0.2)          # 让它进入等待

        await w.stop()

        # ⚠ 这里必须用 `asyncio.wait`，**不能用 `asyncio.wait_for`**。
        #
        # `wait_for` 超时的做法是**取消**内部任务，而这段代码里有
        # `except asyncio.CancelledError: return` —— 取消被吞掉、协程"正常
        # 完成"，于是 `wait_for` 返回 None 而不是抛 TimeoutError，用例永远绿。
        # 我第一版就是这么写的，对着有 bug 的代码照样通过。
        # `asyncio.wait` 只观察、不取消，才能如实反映"它还在睡"。
        done, pending = await asyncio.wait([task], timeout=3)
        if pending:
            task.cancel()
            self.fail(
                "_auto_restart_timer 在 stop() 之后仍在等待。\n"
                "run() 是 asyncio.gather(*coroutines)，要等全部协程结束 —— "
                "这个协程不退，整个 worker 就卡在 Ctrl+C 之后不动，\n"
                "既不执行 _cleanup()，也不打印「🛑 Worker 已停止」。\n"
                "修法：用 wait_for(self._shutdown_event.wait(), timeout=delay)，"
                "与本文件其它长等待保持一致，别用裸 asyncio.sleep()。")

        self.assertFalse(
            w._wants_restart,
            "关停触发的退出**不该**被当成「到点了该重启」——那会让 Ctrl+C 变成重启")

    async def test_restart_still_fires_when_the_wait_completes(self):
        """反向：别为了让它能被打断，把自动重启本身改没了。

        把 delay 压到 ~0，验「睡满 -> 置位 _wants_restart」这条路还在。
        """
        w = _mk(auto_restart_hours=6)
        w._auto_restart_hours = 6

        # 直接把等待压掉：patch 掉 shutdown_event.wait，让 wait_for 立刻超时
        async def _never():
            await asyncio.sleep(3600)
        w._shutdown_event.wait = _never          # type: ignore[method-assign]

        import worker.engine as E
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(aw, timeout):
            # 只压这一处长等待，其余原样
            if timeout and timeout > 60:
                timeout = 0.05
            return await real_wait_for(aw, timeout)

        E.asyncio.wait_for = _fast_wait_for      # type: ignore[assignment]
        try:
            await real_wait_for(w._auto_restart_timer(), timeout=10)
        finally:
            E.asyncio.wait_for = real_wait_for   # type: ignore[assignment]

        self.assertTrue(
            w._wants_restart,
            "等待正常走完之后没有触发重启 —— 修 Ctrl+C 的同时把功能改坏了")


class OtherLongWaitersShutdownTests(unittest.IsolatedAsyncioTestCase):
    """其余几个长等待协程同样要能被 stop() 叫醒。

    它们本来就是对的（用的都是 wait_for(shutdown_event)），这里钉住是为了
    防止将来有人"顺手简化成 asyncio.sleep"，重演一遍同一个 bug。
    """

    async def _assert_exits(self, coro_factory, name, timeout=3):
        w = _mk()
        task = asyncio.ensure_future(coro_factory(w))
        await asyncio.sleep(0.2)
        await w.stop()
        # 同样用 asyncio.wait 而不是 wait_for —— 理由见上面那条用例的注释
        done, pending = await asyncio.wait([task], timeout=timeout)
        if pending:
            task.cancel()
            self.fail(f"{name} 在 stop() 之后没退出 —— gather 会一直等它，"
                      f"整个 worker 卡在 Ctrl+C 之后不动")

    async def test_settings_sync_exits(self):
        await self._assert_exits(lambda w: w._settings_sync(), "_settings_sync")

    async def test_screenshot_gate_monitor_exits(self):
        await self._assert_exits(lambda w: w._screenshot_gate_monitor(),
                                 "_screenshot_gate_monitor")

    async def test_startup_watchdog_exits(self):
        await self._assert_exits(lambda w: w._startup_watchdog(),
                                 "_startup_watchdog")


if __name__ == "__main__":
    unittest.main()
