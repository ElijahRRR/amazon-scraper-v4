"""common/core/error_types.py —— error_type 取值表（唯一真源）。

worker 上报失败任务时填的 ``error_type`` 短代码，历史上是散落在
``worker/engine.py`` 里的字符串字面量，没有任何地方集中列出、也没有任何
校验——HTTP 入口直接 ``data.get("error_type", "")`` 收，谁手滑打错一个词
就悄悄多出一种"新错误类型"，下游（重试策略、outcome 分类、ERP 对接文档）
都不知道。

这里收拢成一份权威列表 + 人类可读说明：
- worker 侧新代码应该 import 这里的常量，不再手写字符串；
- ``server/api/worker_queue.py`` 用 :func:`normalize_fields` 在入口兜底：
  不在这张表里的值一律改写成 :data:`UNKNOWN`，原始值保留进
  ``error_detail`` 开头（不丢信息，但不让脏值污染 ``error_type`` 列）；
- API 文档（``docs/erpapi_contract.md``）把这张表整份抄过去，作为对外契约。

``common/core/retry.py`` 的 ``LIMITED_RETRY_ERROR_TYPES`` /
``NO_AUTO_RETRY_ERROR_TYPES``、``common/pgdb/relay.py`` 的
``_OUTCOME_BY_ERROR_TYPE`` 都是"这张表的一个子集要走特殊策略"，不是另一份
真源——它们从这里 import 常量，不重复定义字符串。
"""
from typing import Dict

NETWORK = "network"
TIMEOUT = "timeout"
BLOCKED = "blocked"
CAPTCHA = "captcha"
PARSE_ERROR = "parse_error"
ZIP_SWITCH_FAILED = "zip_switch_failed"
VARIANT_OFFSET = "variant_offset"
ZIP_NOT_EFFECTIVE = "zip_not_effective"
SESSION_NOT_READY = "session_not_ready"
DISCOVER_FAILED = "discover_failed"
SERVER_REJECT = "server_reject"
UNKNOWN = "unknown"

#: error_type -> 人类可读说明。给 API 文档 / 前端展示用，也是这张表的
#: 权威清单（key 的全集 = 所有已知合法取值）。
DESCRIPTIONS: Dict[str, str] = {
    NETWORK: "网络请求失败（连接错误、DNS 解析失败、连接被重置等）",
    TIMEOUT: "请求超时",
    BLOCKED: "被 Amazon 判定为异常流量并拦截（403/503，非验证码）",
    CAPTCHA: "遇到验证码页面",
    PARSE_ERROR: "页面拿到了，但解析不出预期字段（HTML 结构变化、空页面等）",
    ZIP_SWITCH_FAILED: "切换配送邮编失败",
    VARIANT_OFFSET: "Amazon 把请求重定向到了兄弟 variant 页面（不是目标 ASIN 本身）",
    ZIP_NOT_EFFECTIVE: "邮编设置多次重发仍未生效（页面仍显示默认配送地区）",
    SESSION_NOT_READY: "worker 本地 session 迟迟未就绪（冷启动/轮换中超时）",
    DISCOVER_FAILED: "卖家店铺发现阶段失败（找不到任何在售 ASIN）",
    SERVER_REJECT: "server 端二次校验判定这条结果本身不合法，直接判失败"
                    "（不是 worker 上报的原始错误类型，是 server 改写的）",
    UNKNOWN: "worker 上报的 error_type 不在此表中，已被改写为 unknown；"
             "原始值保留在 error_detail 开头（形如 [unrecognized_type:xxx]）",
}

#: 全部已知合法取值（含兜底值 UNKNOWN）。
ALL_ERROR_TYPES = frozenset(DESCRIPTIONS)


def normalize_fields(data: dict) -> None:
    """原地规范化 ``data`` 里的 ``error_type``（没有这个 key 就什么都不做）。

    只处理"不在表里的脏值"——空字符串/None 一律放行，那是"还没判定出具体
    类型"的合法瞬时状态（比如 worker 侧默认值），不算未知类型，不该被
    改写成 UNKNOWN。
    """
    et = data.get("error_type")
    if et and et not in ALL_ERROR_TYPES:
        detail = data.get("error_detail") or ""
        data["error_detail"] = f"[unrecognized_type:{et}] {detail}".strip()
        data["error_type"] = UNKNOWN


__all__ = [
    "NETWORK", "TIMEOUT", "BLOCKED", "CAPTCHA", "PARSE_ERROR",
    "ZIP_SWITCH_FAILED", "VARIANT_OFFSET", "ZIP_NOT_EFFECTIVE",
    "SESSION_NOT_READY", "DISCOVER_FAILED", "SERVER_REJECT", "UNKNOWN",
    "DESCRIPTIONS", "ALL_ERROR_TYPES", "normalize_fields",
]
