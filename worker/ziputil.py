"""
邮编生效判定（纯函数，无重依赖，便于单测）。

Tier 2a：把"切邮编后是否生效"的判断从一次独立的验证 GET，折叠到我们本来就要采的
商品页 HTML 上。核心信号是 Amazon 每个页面顶部的 glow 配送地址挂件
`id="glow-ingress-line2"`（形如 "New York 10001" / "Altoona 16602"）。

F-012（多站点）：本模块原先把「美国」写死在两处 —— 5 位邮编的字符串包含判定、
以及一张固定的「非美国货币标识」清单。两处现在都从
``common/core/marketplace.py`` 的注册表取，**美国站的行为逐字节不变**
（那张清单原样搬成了 amazon.com 的 ``foreign_currency_markers``）。
"""
import re
from typing import Optional

from common.core import marketplace as _marketplace

# glow 配送地址第二行：城市 + 邮编
_GLOW_LINE2_RE = re.compile(r'id="glow-ingress-line2"[^>]*>\s*([^<]+)')


def zip_effective_in_html(zip_code: str, text: str,
                          marketplace: Optional[str] = None) -> Optional[bool]:
    """从任意 Amazon 页面 HTML 判断目标配送邮编是否已生效。

    返回：
      True  —— glow 挂件显示了目标邮编（已生效）
      False —— 明确未生效：glow 显示的是**其它**邮编，或页面出现不属于本站点的
               货币标识（串区 / 代理落错国家）
      None  —— 无法判定：页面既无 glow 挂件、也无异常货币标识
               （调用方按"宽松通过"处理，与旧版独立验证 GET 的行为一致）

    关键：与 parser 的 `_slx_parse_zip_code` 不同，这里**不做**"取不到就回退成请求值"
    的兜底——取不到返回 None，取到但不符返回 False，避免把脏数据误判为已生效。

    ``marketplace=None`` -> 美国站，行为与 F-012 之前完全一致。
    """
    if not zip_code or not text:
        return None
    try:
        m = _GLOW_LINE2_RE.search(text)
        if m:
            location_text = m.group(1).strip()
            return _location_matches(zip_code, location_text, marketplace)
        head = text[:50000]
        if _marketplace.looks_foreign(head, marketplace):
            return False
        return None
    except Exception:
        return None


def _location_matches(zip_code: str, location_text: str,
                      marketplace: Optional[str]) -> bool:
    """glow 文案里是否确实是目标投递地。

    美国站是**子串包含**（``"10001" in "New York 10001"``）—— 这是 F-012 之前
    的原始实现，一个字节没改。

    加拿大站不能照搬子串包含，两个理由：

      1. 形状会变。我们 POST 出去的是规范形 ``"M5V 3L9"``，而 glow 回显的
         可能是 ``"Toronto M5V 3L9"``、也可能是 ``"M5V3L9"``（无空格）。
         直接 ``in`` 会把后者判成不匹配，凭空造出一次"切换失败"并触发
         回滚 + 冷轮换 —— 代价是一次本可成功的采集变成一次换 session。
      2. Amazon 常常**只回显 FSA**（前三位，``"Toronto M5V"``）。这时候
         完整邮编根本不在文案里，但地址其实是对的。

    所以加拿大站的判定是：把两边都压成无空白大写，然后看 glow 文案里是否
    出现了目标邮编的**前三位**（FSA）。FSA 唯一确定一个投递区域，对
    "地址切对了吗" 这个问题已经够用；要更严就得赌 Amazon 的回显格式，
    而那个格式我们没有实测过（见 marketplace.py 末尾的 VERIFIED 一节）。
    """
    spec = _marketplace.get(marketplace)
    if spec.postal_zero_fill:
        # 美国站：原始实现，逐字节不变。
        return zip_code in location_text

    want = re.sub(r"[\s-]+", "", str(zip_code)).upper()
    got = re.sub(r"[\s-]+", "", str(location_text)).upper()
    if not want:
        return False
    if want in got:
        return True
    # 回退到 FSA（前三位）—— glow 只显示前段时走这里。
    return len(want) >= 3 and want[:3] in got
