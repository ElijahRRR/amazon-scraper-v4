"""启动 worker"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import asyncio
import logging
import signal
import time
from worker.engine import Worker

logger = logging.getLogger("run_worker")


def main():
    parser = argparse.ArgumentParser(description="Amazon Scraper v4 Worker")
    parser.add_argument("--server", required=True, help="Server URL (e.g., http://x.x.x.x:8899)")
    parser.add_argument("--worker-id", default=None, help="Worker ID (auto-generated if not set)")
    parser.add_argument("--concurrency", type=int, default=None, help="Initial concurrency")
    parser.add_argument("--zip-code", default=None, help="Delivery zip code")
    parser.add_argument("--no-screenshot", action="store_true", help="Disable screenshot")
    parser.add_argument("--api-key", default=None, help="ERP Server Worker API Key (or set WORKER_API_KEY env)")
    parser.add_argument("--auto-restart-hours", type=float, default=None,
                        help="定时自动重启小时数（0 或省略=关闭；也可用环境变量 WORKER_AUTO_RESTART_HOURS）")
    args = parser.parse_args()

    # CLI 优先，回落到环境变量
    auto_restart_hours = args.auto_restart_hours
    if auto_restart_hours is None:
        try:
            auto_restart_hours = float(os.environ.get("WORKER_AUTO_RESTART_HOURS", "0") or 0)
        except ValueError:
            auto_restart_hours = 0

    worker = Worker(
        server_url=args.server,
        worker_id=args.worker_id,
        concurrency=args.concurrency,
        zip_code=args.zip_code,
        enable_screenshot=not args.no_screenshot,
        api_key=args.api_key,
        auto_restart_hours=auto_restart_hours,
    )

    # 优雅退出
    loop = asyncio.new_event_loop()

    def signal_handler(sig, frame):
        try:
            loop.create_task(worker.stop())
        except Exception:
            pass

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        loop.run_until_complete(worker.start())
    finally:
        loop.close()

    # 定时自动重启：execv 重载进程（全新 Python、全新 Playwright/Chromium 状态）
    if getattr(worker, "_wants_restart", False):
        print(f"🔁 自动重启：execv {sys.executable} {' '.join(sys.argv)}", flush=True)
        time.sleep(1)
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
