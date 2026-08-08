"""
Amazon 产品采集系统 v4 - 自适应并发控制器

TPS 模式: 全局 AIMD（单信号量 + 单 Metrics）

算法：
  - Additive Increase: 一切顺利 → 并发 +2
  - Multiplicative Decrease: 出问题 → 并发 × 0.7
  - 带宽感知: 带宽饱和时停止增长
  - 冷却机制: 减速后进入冷却期，防止连续多次减速形成雪崩
"""
import asyncio
import random
import time
import logging
from typing import Optional

from common import config
from worker.metrics import MetricsCollector

logger = logging.getLogger(__name__)


# ============================================================
# 共享信号量 resize 工具函数
# ============================================================

async def resize_semaphore(sem: asyncio.Semaphore, old: int, new: int,
                           drain_task: Optional[asyncio.Task]) -> Optional[asyncio.Task]:
    """安全调整信号量大小，返回新的 drain task（或 None）。"""
    if drain_task and not drain_task.done():
        drain_task.cancel()
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):
            pass
    diff = new - old
    if diff > 0:
        for _ in range(diff):
            sem.release()
        return None
    elif diff < 0:
        return asyncio.create_task(_drain_permits(sem, abs(diff)))
    return None


async def _drain_permits(sem: asyncio.Semaphore, count: int):
    """后台排空 permit（可取消安全）。"""
    drained = 0
    try:
        for _ in range(count):
            await sem.acquire()
            drained += 1
    except asyncio.CancelledError:
        for _ in range(drained):
            sem.release()


# ============================================================
# 主控制器
# ============================================================

class AdaptiveController:
    """
    自适应并发控制器（TPS 模式）

    全局单信号量 + 单 Metrics

    核心接口：
    - current_concurrency: 当前允许的最大并发数
    - acquire() / release(): 获取/释放并发槽位
    - record_result(...): 记录请求结果
    - start() / stop(): 启动/停止后台评估
    """

    def __init__(
        self,
        initial: int = None,
        min_c: int = None,
        max_c: int = None,
        metrics: MetricsCollector = None,
    ):
        self._min = min_c or config.MIN_CONCURRENCY
        self._max = max_c or config.MAX_CONCURRENCY

        self._concurrency = initial or config.INITIAL_CONCURRENCY
        self._concurrency = max(self._min, min(self._max, self._concurrency))
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._drain_task: Optional[asyncio.Task] = None
        self.metrics = metrics or MetricsCollector()

        self._adjust_lock = asyncio.Lock()
        self._cooldown_until: float = 0.0
        self._running = False
        self._task: Optional[asyncio.Task] = None

        self._adjust_interval = config.ADJUST_INTERVAL_S
        self._target_latency = config.TARGET_LATENCY_S
        self._max_latency = config.MAX_LATENCY_S
        self._target_success = config.TARGET_SUCCESS_RATE
        self._min_success = config.MIN_SUCCESS_RATE
        self._block_threshold = config.BLOCK_RATE_THRESHOLD
        self._bw_soft_cap = config.BANDWIDTH_SOFT_CAP

        self._cooldown_duration = 15
        self._block_decrease_factor = 0.7
        self._recovery_jitter = 0.5

    @property
    def current_concurrency(self) -> int:
        return self._concurrency

    async def acquire(self, channel_id: int = None):
        """获取并发槽位"""
        await self._semaphore.acquire()
        self.metrics.request_start()

    def release(self, channel_id: int = None):
        """释放并发槽位"""
        self.metrics.request_end()
        self._semaphore.release()

    def record_result(self, latency_s: float, success: bool, blocked: bool,
                      resp_bytes: int = 0, channel_id: int = None):
        """记录请求结果"""
        self.metrics.record(latency_s, success, blocked, resp_bytes)

    async def start(self):
        """启动后台评估协程"""
        self._running = True
        self._task = asyncio.create_task(self._adjust_loop())
        logger.info(f"自适应控制器启动 | 初始并发={self._concurrency} | 范围=[{self._min}, {self._max}]")

    async def stop(self):
        """停止控制器（幂等：可重复调用）"""
        if not self._running and self._task is None:
            return
        self._running = False
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _adjust_loop(self):
        """后台循环：每 ADJUST_INTERVAL_S 秒评估一次"""
        while self._running:
            try:
                await asyncio.sleep(self._adjust_interval)
                if not self._running:
                    break
                await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自适应控制器异常: {e}")

    async def _evaluate(self):
        """
        TPS 模式评估逻辑 — Gradient2-AIMD 混合

        优先级：
        1. 冷却期 → 不动
        2. 被封率高 → AIMD 乘性降低 + 冷却
        3. 成功率低 → AIMD 乘性降低 + 短冷却
        4. 延迟高（p50 超标）→ 减速
        5. RTT gradient 上升 → 预防性 -1（Gradient2，不需要冷却）
        6. 带宽饱和 → 不动
        7. 一切正常且 gradient 稳定 → +2
        8. 其他 → 不动
        """
        snap = self.metrics.snapshot()

        if snap["total"] < 5:
            logger.debug(f"样本不足 ({snap['total']}), 跳过调整")
            return

        async with self._adjust_lock:
            old_c = self._concurrency
            now = time.monotonic()
            in_cooldown = now < self._cooldown_until
            reason = ""

            if in_cooldown:
                new_c = self._concurrency
                remaining = int(self._cooldown_until - now)
                reason = f"冷却中 (剩余 {remaining}s) -> 维持"

            elif snap["block_rate"] > self._block_threshold:
                new_c = max(self._min, int(self._concurrency * self._block_decrease_factor))
                self._cooldown_until = now + self._cooldown_duration
                reason = f"封锁率 {snap['block_rate']:.0%} -> ×{self._block_decrease_factor}+冷却{self._cooldown_duration}s"

            elif snap["success_rate"] < self._min_success:
                new_c = max(self._min, int(self._concurrency * self._block_decrease_factor))
                soft_cooldown = max(15, self._cooldown_duration // 2)
                self._cooldown_until = now + soft_cooldown
                reason = f"成功率={snap['success_rate']:.0%} -> ×{self._block_decrease_factor}+冷却{soft_cooldown}s"

            elif snap["latency_p50"] > self._max_latency:
                new_c = max(self._min, int(self._concurrency * self._block_decrease_factor))
                soft_cooldown = max(15, self._cooldown_duration // 2)
                self._cooldown_until = now + soft_cooldown
                reason = f"延迟 p50={snap['latency_p50']:.2f}s > {self._max_latency:.0f}s -> 减速"

            # Gradient2 预防性降速：RTT 上升趋势
            elif (snap["rtt_gradient"] < 0.85 and snap["ewma_short"] > 0
                  and snap["bandwidth_pct"] > 0.50):
                new_c = max(config.INITIAL_CONCURRENCY, self._concurrency - 1)
                reason = (f"RTT↑ gradient={snap['rtt_gradient']:.2f} bw={snap['bandwidth_pct']:.0%} "
                          f"(short={snap['ewma_short']:.2f}s > long={snap['ewma_long']:.2f}s)")

            elif snap["bandwidth_pct"] > self._bw_soft_cap:
                new_c = self._concurrency
                reason = f"带宽 {snap['bandwidth_pct']:.0%} > {self._bw_soft_cap:.0%} -> 维持"

            elif (snap["success_rate"] >= self._target_success
                  and snap["latency_p50"] < self._target_latency
                  and snap["rtt_gradient"] >= 0.90):
                if random.random() < (0.3 + 0.7 * self._recovery_jitter):
                    increment = 2
                    new_c = min(self._max, self._concurrency + increment)
                    reason = f"OK gradient={snap['rtt_gradient']:.2f} p50={snap['latency_p50']:.2f}s -> +{increment}"
                else:
                    new_c = self._concurrency
                    reason = f"OK gradient={snap['rtt_gradient']:.2f} -> 维持(抖动跳过)"

            else:
                new_c = self._concurrency
                reason = f"稳态 | gradient={snap['rtt_gradient']:.2f} p50={snap['latency_p50']:.2f}s"

            if new_c != old_c:
                await self._resize_semaphore(old_c, new_c)
                self._concurrency = new_c
                logger.info(f"并发调整 {old_c} -> {new_c} | {reason}")
            else:
                logger.debug(f"{reason} | 并发={self._concurrency}")

        logger.info(self.metrics.format_summary())

    async def _resize_semaphore(self, old_value: int, new_value: int):
        """安全地调整信号量大小"""
        if not self._semaphore:
            return
        self._drain_task = await resize_semaphore(
            self._semaphore, old_value, new_value, self._drain_task)


class TokenBucket:
    """
    令牌桶限流器

    控制请求发起速率（QPS），与 Semaphore（并发连接数）互补：
    - Semaphore 控制同时在飞的请求数
    - TokenBucket 控制新请求的产生速率
    """

    def __init__(self, rate: float = None, burst: int = None,
                 initial_tokens: float = 0.0):
        self._rate = rate or config.TOKEN_BUCKET_RATE
        self._burst = burst or 1
        self._tokens = initial_tokens
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """获取一个令牌，不够时等待"""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate

            await asyncio.sleep(wait)

    def _refill(self):
        """补充令牌"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)

    @property
    def burst(self) -> int:
        return self._burst

    @burst.setter
    def burst(self, value: int):
        self._burst = max(1, value)

    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, value: float):
        """动态调整速率"""
        self._rate = max(0.1, value)
