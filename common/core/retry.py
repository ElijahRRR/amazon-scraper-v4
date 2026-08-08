"""common/core/retry.py —— error_type → 重试上限策略（唯一真源）。

两个存储后端 + server 的重试判定共用这一份。定义在这里而不是
``common/database.py``：它是纯 Python，不该为了拿三个常量去 import aiosqlite。
"""
from common import config
from common.core.error_types import VARIANT_OFFSET

# ============================================================
# error_type → 失败次数上限映射
# 用于 fail_task / accept_results_batch
#
# 当任务的 retry_count >= cap 时直接 status='failed' 终态。
# 不在 dict 中的 error_type 默认用 config.MAX_RETRIES（=3）。
#
# - variant_offset: 不重试（cap=1）
#   理由：这是 Amazon 返回了兄弟 variant 页面，继续重试容易浪费配额并污染队列。
# ============================================================
LIMITED_RETRY_ERROR_TYPES = {
    VARIANT_OFFSET: 1,   # 首次失败即终态，不回 pending
}

# ============================================================
# 不进入"循环类"重试的 error_type 集合
# 用于 auto_retry_failed_tasks / api_retry_batch（默认）
#
# 进入此集合的失败不会被 server 周期任务或用户默认手动按钮重新激活。
# 批次手动重试也会跳过这些类型。
# ============================================================
NO_AUTO_RETRY_ERROR_TYPES = frozenset({VARIANT_OFFSET})

# 向后兼容：之前一些地方引用了 NO_RETRY_ERROR_TYPES
NO_RETRY_ERROR_TYPES = NO_AUTO_RETRY_ERROR_TYPES


def _fail_cap(error_type: str) -> int:
    """返回该 error_type 的失败上限（达到即终态，不回 pending）。"""
    return LIMITED_RETRY_ERROR_TYPES.get(error_type or "", config.MAX_RETRIES)


__all__ = [
    "LIMITED_RETRY_ERROR_TYPES",
    "NO_AUTO_RETRY_ERROR_TYPES",
    "NO_RETRY_ERROR_TYPES",
    "_fail_cap",
]
