"""
截图子进程 v3 — 融合 v2 截图准确性 + 测试脚本防泄漏模式。

架构：1 个 Playwright + 1 个 Chromium（常驻复用），Semaphore 控制并发。
渲染：base 注入 + 资源拦截 + 智能等主图 + 空白检测重试。
防泄漏：三道，缺一道都会留下 Chromium ——
  1. signal 优雅停机 + finally page.close（正常退出）；
  2. 父进程的进程组级兜底清理（worker 还活着、能跑代码时）；
  3. **父进程死亡看门狗**（worker 被 kill -9 / OOM，跑不到第 2 道时）——
     见文件末尾 `_start_parent_death_watchdog` 的注释。
"""
import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from typing import Optional

import httpx

logger = logging.getLogger("screenshot_worker")


class ScreenshotWorker:
    def __init__(self, server_url: str, base_dir: str = None,
                 browsers_count: int = 1, pages_per_browser: int = 5,
                 proxy_url: str = None, api_key: str = None):
        default_base_dir = os.environ.get("SCREENSHOT_BASE_DIR") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "screenshot_cache"
        )
        self.server_url = server_url
        self._api_key = api_key or os.environ.get("WORKER_API_KEY", "")
        self.base_dir = base_dir or default_base_dir
        self.html_dir = os.path.join(self.base_dir, "html")
        self._concurrency = pages_per_browser
        self._pw = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._render_count = 0
        self._restart_every = 500
        self._running = True
        self._http_client: Optional[httpx.AsyncClient] = None

    # ==================== 浏览器生命周期 ====================

    def request_stop(self, reason: str = "signal"):
        """请求优雅退出，让 finally 负责收尾资源。"""
        if not self._running:
            return
        self._running = False
        logger.info(f"收到停止请求，准备退出当前截图循环 ({reason})")

    async def _ensure_browser(self):
        if self._browser:
            return
        async with self._browser_lock:
            if self._browser:
                return
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
                      "--disable-extensions", "--disable-software-rasterizer"],
            )
            logger.info(f"浏览器启动（并发 {self._concurrency}）")

    async def _close_browser(self):
        async with self._browser_lock:
            b, self._browser = self._browser, None
            p, self._pw = self._pw, None
        if b:
            try:
                await b.close()
            except Exception:
                pass
        if p:
            try:
                await p.stop()
            except Exception:
                pass

    # ==================== 主循环 ====================

    async def start(self):
        os.makedirs(self.html_dir, exist_ok=True)
        server_headers = {}
        if self._api_key:
            server_headers["X-Worker-Api-Key"] = self._api_key
        self._http_client = httpx.AsyncClient(timeout=30, headers=server_headers)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop, sig.name)
            except NotImplementedError:
                signal.signal(sig, lambda *_args, _sig=sig: self.request_stop(_sig.name))
        # ⚠ 上面那两个处理器覆盖不到 worker 被 kill -9 的情形：SIGKILL 不可捕获，
        # worker 的 `_reap_screenshot_descendants()` 根本不会被调到，我们就成了
        # 孤儿、Chromium 跟着一起留。看门狗补的正是这一段，理由见它的文件内注释。
        _start_parent_death_watchdog(self, loop)
        logger.info(f"截图进程启动（并发: {self._concurrency}, 监控: {self.html_dir}）")

        _last_completion_check = 0.0
        _completion_check_interval = 10.0  # 最多每 10 秒检查一次批次完成情况
        try:
            while self._running:
                pending = self._scan_pending()
                now = time.time()
                if not pending:
                    # 空闲时才检查完成度，且有 10s 最小间隔，避免高频轮询服务端
                    if now - _last_completion_check >= _completion_check_interval:
                        await self._check_batch_completion()
                        _last_completion_check = now
                    await asyncio.sleep(1)
                    continue
                await self._process_batch(pending)
                if now - _last_completion_check >= _completion_check_interval:
                    await self._check_batch_completion()
                    _last_completion_check = now
        except KeyboardInterrupt:
            pass
        finally:
            await self._close_browser()
            if self._http_client:
                await self._http_client.aclose()
            logger.info("截图进程退出")

    # ==================== 扫描 ====================

    def _scan_pending(self) -> list:
        pending = []
        if not os.path.isdir(self.html_dir):
            return pending
        for batch_name in os.listdir(self.html_dir):
            batch_dir = os.path.join(self.html_dir, batch_name)
            if not os.path.isdir(batch_dir):
                continue
            if os.path.exists(os.path.join(self.base_dir, f"_uploaded_{batch_name}")):
                continue
            self._recover_inflight_files(batch_dir)
            for fname in os.listdir(batch_dir):
                if fname.endswith(".html") and not fname.startswith("_"):
                    pending.append((batch_name, fname[:-5], os.path.join(batch_dir, fname)))
        return pending

    # ==================== 批处理 ====================

    async def _process_batch(self, pending: list):
        logger.info(f"处理 {len(pending)} 张截图")
        await self._ensure_browser()
        sem = asyncio.Semaphore(self._concurrency)

        async def do_one(batch_name, asin, html_path):
            async with sem:
                await self._render_upload(batch_name, asin, html_path)

        tasks = [asyncio.create_task(do_one(b, a, p)) for b, a, p in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"截图任务未捕获异常: {type(result).__name__}: {result}")

        self._render_count += len(pending)
        if self._render_count >= self._restart_every:
            logger.info(f"已渲染 {self._render_count} 张，重启浏览器")
            await self._close_browser()
            self._render_count = 0

    # ==================== 单张截图 ====================

    async def _render_upload(self, batch_name: str, asin: str, html_path: str):
        logger.info(f"开始截图: {asin}")
        processing_path = self._claim_html_file(html_path)
        if not processing_path:
            logger.warning(f"截图源文件在占用前消失: {asin}")
            return
        logger.info(f"占用截图源文件: {asin} -> {os.path.basename(processing_path)}")

        # 读取 HTML
        try:
            with open(processing_path, "r", encoding="utf-8", errors="replace") as f:
                html = f.read()
        except FileNotFoundError:
            logger.warning(f"截图源文件在占用后消失: {asin}")
            return
        except Exception as e:
            logger.error(f"读取截图 HTML 失败 {asin}: {e}")
            self._restore_inflight_file(processing_path)
            return

        if not html or len(html) < 500:
            logger.warning(f"HTML 过短: {asin} ({len(html)}B)")
            await self._mark_terminal_failure(batch_name, asin, processing_path, "html_too_short")
            return

        # 注入 <base>
        lower = html[:2000].lower()
        if "<base " not in lower:
            pos = lower.find("<head")
            if pos != -1:
                close = html.index(">", pos) + 1
                html = html[:close] + '<base href="https://www.amazon.com/">' + html[close:]

        # 渲染（含空白检测重试，最多 3 次）
        png_bytes = None
        for attempt in range(3):
            try:
                png_bytes, has_content = await asyncio.wait_for(
                    self._render_one(html, asin), timeout=45
                )
            except asyncio.TimeoutError:
                logger.error(f"截图超时: {asin} (attempt={attempt+1}/3)")
                png_bytes, has_content = None, False
                await self._close_browser()
                await self._ensure_browser()

            if png_bytes and (len(png_bytes) >= 10240 or has_content):
                break  # 正常

            if png_bytes and len(png_bytes) < 10240 and not has_content:
                logger.warning(f"空白截图 {asin} ({len(png_bytes)}B) 第{attempt+1}次，重试...")
                png_bytes = None
                await asyncio.sleep(1)
            else:
                break  # 渲染异常，不重试

        # 上传
        if png_bytes and len(png_bytes) > 0:
            ok = await self._upload(batch_name, asin, png_bytes)
            logger.info(f"截图上传结果: {asin} ok={ok}")
            if ok is True:
                logger.info(f"截图完成: {asin} ({len(png_bytes)//1024}KB)")
                try:
                    os.remove(processing_path)
                except OSError:
                    pass
            elif ok == "batch_gone":
                # 批次/任务在服务端已不存在：清理整个本地批次目录，中止对该批次的后续处理
                self._purge_stale_batch(batch_name)
            else:
                logger.warning(f"上传失败: {asin}")
                self._restore_inflight_file(processing_path)
        else:
            await self._mark_terminal_failure(batch_name, asin, processing_path, "render_failed")

    async def _mark_terminal_failure(self, batch_name: str, asin: str, html_path: str, error: str):
        logger.warning(f"截图最终失败: {asin} ({error})")
        # 重命名为 .failed 保留供排查，服务端直接记为 failed 终态。
        try:
            os.replace(html_path, self._failed_path(html_path))
        except OSError:
            pass
        try:
            await self._http_client.post(
                f"{self.server_url}/api/tasks/screenshot/fail",
                json={"asin": asin, "batch_name": batch_name, "error": error},
                timeout=5,
            )
        except Exception as e:
            logger.error(f"上报截图失败异常 {asin}: {e}")

    async def _render_one(self, html: str, asin: str) -> tuple:
        """渲染单张，返回 (png_bytes, has_content)。参照 v2 渲染逻辑。"""
        page = None
        try:
            page = await self._browser.new_page(viewport={"width": 1280, "height": 1300})

            # 资源拦截：放行 CSS/图片，屏蔽 JS/字体/广告
            async def block_resources(route):
                rt = route.request.resource_type
                url = route.request.url
                if rt in ("stylesheet", "image"):
                    await route.continue_()
                elif rt in ("script", "font", "media", "websocket", "manifest", "other"):
                    await route.abort()
                elif any(x in url for x in ("analytics", "tracking", "beacon",
                                            "ads", "doubleclick", "facebook")):
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", block_resources)

            try:
                await page.set_content(html, wait_until="domcontentloaded", timeout=5000)
            except Exception:
                pass

            # 智能等待主图加载（最多 7 秒）
            try:
                await page.evaluate("""() => new Promise((resolve) => {
                    const selectors = [
                        '#landingImage', '#imgBlkFront', '#main-image',
                        '#imgTagWrapperId img', '#imageBlock img[src*="images-amazon"]'
                    ];
                    let img = null;
                    for (const sel of selectors) {
                        img = document.querySelector(sel);
                        if (img) break;
                    }
                    if (!img) return resolve(false);
                    if (img.complete && img.naturalWidth > 0) return resolve(true);
                    img.addEventListener('load', () => resolve(true), {once: true});
                    img.addEventListener('error', () => resolve(false), {once: true});
                    setTimeout(() => resolve(false), 7000);
                })""")
            except Exception:
                pass

            # 空白检测
            has_content = await page.evaluate("""() => {
                if (!document.body) return false;
                const text = document.body.innerText || '';
                if (text.trim().length > 50) return true;
                const imgs = document.querySelectorAll('img[src]');
                if (imgs.length > 0) return true;
                return false;
            }""")

            png_bytes = await page.screenshot(
                type="png", clip={"x": 0, "y": 0, "width": 1280, "height": 1300}
            )
            return png_bytes, has_content

        except Exception as e:
            err = str(e)
            if "browser has been closed" in err or "Target closed" in err:
                logger.error(f"浏览器崩溃: {asin}")
                await self._close_browser()
            else:
                logger.warning(f"渲染异常 {asin}: {e}")
            return None, False
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    # ==================== 上传 ====================

    async def _upload(self, batch_name: str, asin: str, png_bytes: bytes):
        """返回:
          True           上传成功
          "batch_gone"   服务端批次不存在/任务不存在（永久失败，不可重试）
          False          其他可重试错误
        """
        fname = f"{asin}.png"
        for attempt in range(3):
            try:
                resp = await self._http_client.post(
                    f"{self.server_url}/api/tasks/screenshot",
                    files={"file": (fname, png_bytes, "image/png")},
                    data={"batch_name": batch_name, "asin": asin},
                )
                if resp.status_code == 200:
                    return True
                # 400 批次不存在 / 404 截图任务不存在 / 409 worker 已离线：永久错误，不重试
                if resp.status_code in (400, 404, 409):
                    try:
                        detail = resp.json().get("detail", "")
                    except Exception:
                        detail = resp.text[:200]
                    logger.warning(
                        f"上传永久失败 {asin}: HTTP {resp.status_code} ({detail}) — 批次/任务已失效"
                    )
                    return "batch_gone"
                logger.warning(f"上传失败 {asin}: HTTP {resp.status_code} ({attempt+1}/3)")
            except Exception as e:
                logger.error(f"上传异常 {asin}: {e} ({attempt+1}/3)")
            if attempt < 2:
                await asyncio.sleep(1)
        return False

    def _purge_stale_batch(self, batch_name: str):
        """服务端已不存在的批次：删除本地目录 + 写 uploaded marker，防止下轮再被扫描。"""
        batch_dir = os.path.join(self.html_dir, batch_name)
        if os.path.isdir(batch_dir):
            shutil.rmtree(batch_dir, ignore_errors=True)
            logger.warning(f"已清理过期批次目录: {batch_name}")
        marker = os.path.join(self.base_dir, f"_uploaded_{batch_name}")
        try:
            with open(marker, "w") as f:
                f.write(f"purged:{time.time()}")
        except OSError:
            pass

    # ==================== 批次完成 ====================

    async def _check_batch_completion(self):
        if not os.path.isdir(self.html_dir):
            return
        for batch_name in os.listdir(self.html_dir):
            batch_dir = os.path.join(self.html_dir, batch_name)
            if not os.path.isdir(batch_dir):
                continue
            uploaded_marker = os.path.join(self.base_dir, f"_uploaded_{batch_name}")
            if os.path.exists(uploaded_marker):
                continue
            if not os.path.exists(os.path.join(batch_dir, "_scraping_done")):
                continue
            remaining = [f for f in os.listdir(batch_dir)
                         if (f.endswith(".html") or f.endswith(".processing")) and not f.startswith("_")]
            if remaining:
                continue
            progress = await self._get_screenshot_progress(batch_name)
            # 服务端批次已删除 → 直接清理本地残留
            if progress.get("_batch_gone"):
                logger.warning(f"批次 {batch_name} 在服务端已不存在，清理本地残留")
                self._purge_stale_batch(batch_name)
                continue
            total = progress.get("total", 0)
            finished = progress.get("done", 0) + progress.get("failed", 0)
            # 本 worker 的 HTML 已清空（无 .html/.processing），无论服务端整体是否完成，
            # 本 worker 对此批次的贡献都结束了——写 uploaded marker 并清理本地目录。
            # 剩余的服务端 pending 由其他 worker 完成或由服务端超时机制兜底失败。
            if total > 0 and finished < total:
                logger.info(
                    f"批次 {batch_name} 本地已完成，等待其他 worker: "
                    f"done={progress.get('done', 0)} failed={progress.get('failed', 0)} total={total}"
                )
            else:
                logger.info(
                    f"批次完成: {batch_name} "
                    f"(done={progress.get('done', 0)} failed={progress.get('failed', 0)} total={total})"
                )
            try:
                with open(uploaded_marker, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
            shutil.rmtree(batch_dir, ignore_errors=True)

    async def _get_screenshot_progress(self, batch_name: str) -> dict:
        try:
            resp = await self._http_client.get(
                f"{self.server_url}/api/batches/{batch_name}/screenshots/progress",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                # 服务端批次已删除，调用方应清理本地残留
                return {"_batch_gone": True, "pending": 0, "processing": 0,
                        "done": 0, "failed": 0, "total": 0}
        except Exception:
            pass
        return {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0}

    def _processing_path(self, html_path: str) -> str:
        if html_path.endswith(".html"):
            return html_path[:-5] + ".processing"
        return html_path

    def _failed_path(self, inflight_path: str) -> str:
        if inflight_path.endswith(".html"):
            return inflight_path[:-5] + ".failed"
        if inflight_path.endswith(".processing"):
            return inflight_path[:-11] + ".failed"
        return inflight_path + ".failed"

    def _html_path(self, inflight_path: str) -> str:
        if inflight_path.endswith(".processing"):
            return inflight_path[:-11] + ".html"
        return inflight_path

    def _claim_html_file(self, html_path: str) -> Optional[str]:
        processing_path = self._processing_path(html_path)
        try:
            os.replace(html_path, processing_path)
            return processing_path
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning(f"标记截图处理中失败 {os.path.basename(html_path)}: {e}")
            return None

    def _restore_inflight_file(self, inflight_path: str):
        html_path = self._html_path(inflight_path)
        if inflight_path == html_path or not os.path.exists(inflight_path):
            return
        try:
            os.replace(inflight_path, html_path)
        except OSError as e:
            logger.warning(f"恢复截图任务失败 {os.path.basename(inflight_path)}: {e}")

    def _recover_inflight_files(self, batch_dir: str):
        for fname in os.listdir(batch_dir):
            if not fname.endswith(".processing") or fname.startswith("_"):
                continue
            inflight_path = os.path.join(batch_dir, fname)
            html_path = self._html_path(inflight_path)
            if os.path.exists(html_path):
                continue
            try:
                os.replace(inflight_path, html_path)
                logger.warning(f"恢复中断截图任务: {os.path.basename(html_path)}")
            except OSError as e:
                logger.warning(f"恢复中断截图任务失败 {fname}: {e}")


# ==================== 父进程死亡看门狗 ====================
#
# 为什么需要它
# ------------
# 回收截图进程树的逻辑**全部**在 worker 那边（engine.py 的
# `_reap_screenshot_descendants`，走 `os.killpg`）。那条路要成立有个前提：
# worker 还活着、还能跑代码。凡是它来不及反应的死法——`kill -9`、OOM killer、
# 断电——整棵树就都留下了：Chromium 连同它自己派生的 renderer / GPU 进程，
# 一个都不会走，直到有人手动 pkill。
#
# ⚠ **这不是启动方式的问题，别去动 `start_new_session=True`。** 实测对照过：
# 带与不带这个参数，`kill -9` 父进程之后子孙**同样**全部存活、同样被 init
# 收养（ppid=1）。SIGKILL 不可捕获，父进程一行清理代码都执行不到；而 Unix
# 的规矩本来就是父死子不死。去掉 `start_new_session` 反而更糟——子进程会和
# worker 同处一个进程组，`killpg` 会把 worker 自己也杀掉，重启截图子进程时
# 也没法只收掉旧的那一棵树。
#
# 所以修法只能是把清理从「父驱动」改成「子自查」：本进程定期看一眼父进程还
# 在不在，不在了就自己连整个进程组一起收掉。本进程正是进程组组长
# （`start_new_session=True` 让它 setsid 了，engine.py:2074 还校验过
# `pgid == pid`），所以 `killpg(0, ...)` 一刀就能带走 Chromium 全家。
#
# 为什么用轮询 getppid 而不是 PR_SET_PDEATHSIG
# --------------------------------------------
# `prctl(PR_SET_PDEATHSIG)` 是内核级、零延迟，但**只有 Linux 有**。本项目的
# worker 也跑在 macOS 上（deploy 里有 start.sh/start.bat 两套），那边没有这个
# 调用。轮询 `getppid()` 是可移植的，代价是最多晚 `_PARENT_WATCH_INTERVAL`
# 秒才发现——而这个场景下（进程已经没人管了）晚几秒毫无影响。

#: 多久查一次父进程。几秒的延迟在「父进程已经死了」这个场景下无所谓，
#: 但别调太大：这段时间里 Chromium 还占着内存和 CPU。
_PARENT_WATCH_INTERVAL = 3.0

#: 发现父进程没了之后，留给优雅收尾（关浏览器、落盘）的时间。
#: 到点无论如何硬杀——**这一步不允许再依赖任何可能挂住的代码**，
#: 否则就是把「关停卡死」那个 bug 原样搬进看门狗里。
_PARENT_DEATH_GRACE = 5.0


def _hard_kill_own_process_group():
    """连同 Chromium 全家一起收掉。本函数**不可能**挂住，也不返回。"""
    if hasattr(os, "killpg"):
        try:
            # 0 = 本进程所在的进程组。我们是组长，所以这一刀覆盖
            # screenshot.py + Chromium + 它派生的 renderer/GPU 进程。
            os.killpg(0, signal.SIGKILL)
        except Exception:                                      # noqa: BLE001
            pass
    # killpg 不可用（Windows）或没生效时的兜底：至少让自己走掉。
    # 用 os._exit 而不是 sys.exit —— 后者靠抛异常退出，可能被上层 except 吞掉。
    os._exit(9)


def _start_parent_death_watchdog(worker: "ScreenshotWorker" = None,
                                 loop: asyncio.AbstractEventLoop = None):
    """起一个守护线程：父进程一没，就把本进程组整个收掉。

    返回该线程（未启动的情形返回 None，便于调用方与用例判断）。
    """
    import threading

    orig_ppid = os.getppid()

    if orig_ppid <= 1:
        # 启动时父进程就已经是 init/launchd —— 要么我们本来就是被 detach 起来的，
        # 要么父进程在这几毫秒里已经死了。无论哪种，"ppid 变了"这个信号都用不了，
        # 硬判 ppid==1 又会在容器里误杀（那儿 1 号进程可能就是正常的父进程）。
        # 明确关掉并留一行日志，好过装作有保护。
        logger.warning(
            "父进程死亡看门狗未启用：启动时 ppid=%s。"
            "本进程若被孤儿化，Chromium 需要外部清理（pkill -f chromium）。",
            orig_ppid)
        return None

    def _watch():
        while True:
            time.sleep(_PARENT_WATCH_INTERVAL)
            if os.getppid() == orig_ppid:
                continue
            # ppid 变了 = 原来的父进程没了，我们被 init/launchd 收养了。
            logger.error(
                "父进程已消失（ppid %s -> %s）——大概率是 worker 被 kill -9 / OOM。"
                "%.0f 秒后强制收掉本进程组（含 Chromium）。",
                orig_ppid, os.getppid(), _PARENT_DEATH_GRACE)

            # 先礼：让主循环自己退出、把浏览器关干净，少留一堆 Chromium 临时目录。
            # 跨线程碰 asyncio 只能走 call_soon_threadsafe。
            if worker is not None and loop is not None:
                try:
                    loop.call_soon_threadsafe(worker.request_stop, "parent-died")
                except Exception:                              # noqa: BLE001
                    pass

            # 后兵：**无条件**硬杀。这里不 join、不等任何 future ——
            # 优雅收尾能成最好，不成也绝不允许把看门狗自己卡住。
            time.sleep(_PARENT_DEATH_GRACE)
            logger.error("宽限期结束，强制收掉本进程组。")
            _hard_kill_own_process_group()

    t = threading.Thread(target=_watch, name="parent-death-watchdog", daemon=True)
    t.start()
    logger.info("父进程死亡看门狗已启动（父 pid=%s，每 %.0fs 查一次）",
                orig_ppid, _PARENT_WATCH_INTERVAL)
    return t


# ==================== 入口 ====================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [SCREENSHOT] %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 2:
        print("Usage: python screenshot.py <server_url> [browsers_count] [pages_per_browser]")
        sys.exit(1)

    server_url = sys.argv[1].rstrip("/")
    browsers_count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    pages_per_browser = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    worker = ScreenshotWorker(
        server_url=server_url,
        browsers_count=browsers_count,
        pages_per_browser=pages_per_browser,
    )

    asyncio.run(worker.start())


if __name__ == "__main__":
    main()
