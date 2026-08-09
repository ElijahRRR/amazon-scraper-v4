"""common/core/textclean.py —— 剔除亚马逊页面里漏出来的不可见控制字符。

**零依赖**（连标准库都不 import）。这是硬要求：``worker/parser.py`` import 它，
而 worker 的运行环境不装 aiosqlite / asyncpg——同 ``completeness.py`` 的约束。

------------------------------------------------------------------------
它修的是什么
------------------------------------------------------------------------
亚马逊详情表那块 HTML 会在字段值前面插 **U+200E（LEFT-TO-RIGHT MARK）**，
于是采下来的品牌是 ``"\\u200eBBRGIRL"`` 而不是 ``"BBRGIRL"``。这东西**不可见**：
网页上看不出来、导出的 Excel 里看不出来、肉眼比对两个字符串也看不出来，
只有做字符串比较的下游会踩到。

实测（2026-08-06 真机采集，10 条里 2 条中招）最能说明问题的一例：同一个
商品的 ``brand`` 是干净的 ``"AYLIFU"``、``manufacturer`` 却是 ``"\\u200eAYLIFU"``
—— **同一个值、两个字段、一个干净一个脏**，因为它们取自页面不同位置。

两个后果：

1. **下游匹配失败**。``"\\u200eBBRGIRL" != "BBRGIRL"``，按品牌 join / 去重 /
   建索引全都会把它当成另一个品牌。
2. **哈希误翻转的隐患**。``brand`` / ``manufacturer`` / ``model_number`` /
   ``part_number`` / ``item_weight`` / ``package_dimensions`` 全在
   ``SLOW_HASH_FIELDS`` 里。现在哈希是稳定的（字符一直在），但只要亚马逊哪天
   不再输出这个标记，这些 ASIN 会集体翻转一次哈希 = 一批假的「商品有变更」。

``slowhash.normalize_text`` 拦不住它：那里做的是 NFKC，而 **NFKC 不消除
U+200E**（实测）。而且 ``normalize_text`` 只作用于哈希输入，不改**存下来的值**，
所以就算在那里洗，下游拿到的仍是脏数据。因此清洗必须发生在**解析出口**。

------------------------------------------------------------------------
为什么是显式清单，不是「删掉所有 Cf 类字符」
------------------------------------------------------------------------
``unicodedata.category(c) == "Cf"`` 一扫了之看着更省事，但 **U+200D（ZWJ，
零宽连接符）也是 Cf**，而它是 emoji 组合序列的粘合剂：``👨‍👩‍👧`` 去掉 ZWJ 会
碎成三个独立 emoji。商品标题和五点描述里确实有 emoji（实测数据里就有 🧩💨）。

所以这里**逐个列出要删的字符**，并刻意保留 U+200C / U+200D。
"""

#: 要剔除的不可见字符。逐个列，不按 Unicode 类别一扫了之（见模块 docstring）。
#:
#: 刻意**不含** U+200C（ZWNJ）与 U+200D（ZWJ）：它们在 emoji 组合序列与部分
#: 文字系统里是有语义的，删掉会改变内容本身。
_INVISIBLE = (
    "​"              # ZERO WIDTH SPACE
    "‎"              # LEFT-TO-RIGHT MARK      ← 亚马逊详情表的元凶
    "‏"              # RIGHT-TO-LEFT MARK
    "‪‫‬"  # bidi embedding / pop
    "‭‮"        # bidi override
    "⁠"              # WORD JOINER
    "⁦⁧⁨⁩"  # bidi isolates
    "﻿"              # ZERO WIDTH NO-BREAK SPACE / BOM
    "­"              # SOFT HYPHEN
)

#: str.translate 的删除表。建一次，复用。
_DELETE_MAP = {ord(c): None for c in _INVISIBLE}


def clean_text(value):
    """剔除 ``value`` 里的不可见控制字符；非字符串原样返回。

    只删字符，**不做** strip / 大小写 / NFKC —— 那些是
    ``slowhash.normalize_text`` 的职责，混在一起会让「存下来的值」和
    「用来算哈希的值」的差别更难说清。

    洗完变成空串是**正确结果**（该字段本来就没有可见内容），这里不替换成
    ``"N/A"``：那是解析层的哨兵约定，由调用方决定，不该由清洗函数偷偷代劳。
    """
    if isinstance(value, str):
        return value.translate(_DELETE_MAP)
    return value


def clean_deep(obj):
    """递归清洗 dict / list / tuple / str，其余类型原样返回。

    **返回新对象，不原地改**：解析结果里可能混着调用方还要复用的容器
    （比如 ``_default_result`` 的模板），原地改会波及它们。
    """
    if isinstance(obj, str):
        return obj.translate(_DELETE_MAP)
    if isinstance(obj, dict):
        return {k: clean_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_deep(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(clean_deep(v) for v in obj)
    return obj


__all__ = ["clean_text", "clean_deep"]
