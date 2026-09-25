#!/usr/bin/env python3
"""tools/probe_marketplace.py —— 实测一个站点的采集假设（F-012）。

------------------------------------------------------------------------
为什么需要它
------------------------------------------------------------------------
往 ``common/core/marketplace.py`` 的注册表里加一个新站点时，有几项是**猜的**，
必须拿真实页面验证过才能把那条记录标 ``verified=True``：

  1. ``zip_change_url`` 的路径是否与美国站同构，``zipCode`` 参数是否原样接受
     该国的邮编形状（带空格？无空格？要不要额外的 countryCode 字段？）。
  2. glow 挂件 ``id="glow-ingress-line2"`` 是否同名，文案里有没有完整邮编
     （还是只有前段 —— 加拿大的 FSA 那种）。
  3. 价格渲染成什么符号（``$`` / ``CDN$`` / ``US$`` …）。

猜错的后果都不是报错，是**一批看着正常、实则不对**的数据 —— 这正是 F-012
整个改造要消灭的故障形态，所以新站点一律从 ``verified=False`` 起步。

------------------------------------------------------------------------
⚠ 已知缺陷：这个脚本自己可能过不了连通性那一关
------------------------------------------------------------------------
它用的是**裸 curl_cffi Session**，没走 ``worker/session.py:AmazonSession``
的 ``initialize()``（首页预热 + cookie 建立 + 指纹轮换）。实测下来，同一台机器
同一个代理，**采集器跑得通、本脚本拿到 202 壳页**。

2026-09-19 验证 amazon.ca 时就是这样：三项假设最后是靠**真实采集会话 + 人工
核对商品页**定下来的，本脚本没能提供证据。注册表末尾的 VERIFIED 一节如实记了
这一点。

所以：**连通性那一关没过，不代表站点有问题，很可能只是这个脚本不够硬。**
真过不去就退回「起一个真实采集批次 + 人工核对」那条路 —— 它更慢，但它是
这个功能最终要跑的那条路，证据力更强。
（修法是让它复用 AmazonSession，还没做。）

------------------------------------------------------------------------
怎么用
------------------------------------------------------------------------
    # 用环境里已配的代理（与 worker 同一个 PROXY_URL）
    PROXY_URL='http://user:pass@host:port' \
        python -m tools.probe_marketplace --marketplace amazon.ca

    # 指定邮编与 ASIN（默认用注册表的 default_postal 和一个通用 ASIN）
    python -m tools.probe_marketplace --marketplace amazon.ca \
        --postal 'K1V 7P8' --asin B09S6W6H5B

    # 不走代理（只有直连能过 Amazon 的机器上才有意义）
    python -m tools.probe_marketplace --marketplace amazon.ca --no-proxy

跑完之后：把实测到的差异回填进注册表，把那条记录的 ``verified`` 改成 True，
并在 VERIFIED 一节写清楚**怎么测的**。"测过了"三个字没有信息量。

------------------------------------------------------------------------
它**不**做什么
------------------------------------------------------------------------
* 不写库、不建任务、不碰事件流 —— 它只发几个 GET/POST 然后打印。
* 不改任何配置文件。结论要人读完自己回填，因为"页面长这样"这件事
  需要人看一眼再定，自动回填等于把一次误判固化进注册表。
* **不排查字段级解析缺陷** —— 那是 ``tools/diag_parse.py`` 的活（配合
  ``DUMP_HTML=1`` 留档）。本脚本只管"站点级假设"这三项。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.core import marketplace as M  # noqa: E402

try:
    from curl_cffi.requests import Session
except ImportError:  # pragma: no cover - 只在缺依赖的机器上走到
    print("需要 curl_cffi：pip install curl_cffi", file=sys.stderr)
    raise SystemExit(2)

#: 拦截页的判据，与 worker/parser.py:2057 / worker/engine.py:1495 同源。
#: 实测这串在 amazon.ca 的拦截页上也一样（变的只是页尾两个链接）。
_BLOCK_MARK = "api-services-support@amazon.com"

_GLOW_RE = re.compile(r'id="glow-ingress-line2"[^>]*>\s*([^<]+)')
#: 价格串。宽松地抓，目的是看**符号长什么样**，不是精确解析。
_PRICE_RE = re.compile(r'(?:CDN\$|C\$|US\$|\$)\s?\d[\d,]*\.?\d*')

#: 一个在多数站点都存在的通用 ASIN。只是默认值，跑的时候建议换成你真要采的。
#: 实测用过的加拿大站 ASIN。只是默认值，跑的时候建议换成你真要采的。
_DEFAULT_ASIN = "B09S6W6H5B"


def _ok(msg):
    print(f"  \033[32m✓\033[0m {msg}")


def _bad(msg):
    print(f"  \033[31m✗\033[0m {msg}")


def _warn(msg):
    print(f"  \033[33m?\033[0m {msg}")


def _blocked(text: str) -> bool:
    return _BLOCK_MARK in text and len(text) < 20000


def _session(spec, proxy: str | None):
    kwargs = {"impersonate": "chrome131"}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    if ca and os.path.exists(ca):
        kwargs["verify"] = ca
    s = Session(**kwargs)
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": spec.accept_language,
        "Upgrade-Insecure-Requests": "1",
    })
    return s


# ======================================================================
# 三项探针
# ======================================================================

def probe_reachable(sess, spec) -> bool:
    """0) 这个出口 IP 到底能不能拿到真页面。"""
    print("\n[0] 连通性（出口 IP 有没有被 Amazon 拦）")
    try:
        r = sess.get(spec.base_url, timeout=40)
    except Exception as e:
        _bad(f"请求异常：{type(e).__name__}: {e}")
        return False
    print(f"      status={r.status_code} bytes={len(r.text)}")
    if _blocked(r.text):
        _bad("拿到的是拦截页（api-services-support@amazon.com）。"
             "这台机器的出口 IP 不可用 —— 换住宅代理再跑。")
        return False
    # ⚠ 这道闸门**故意收得很紧**：Amazon 对机房 IP 的常见回应不是 403，
    #   而是 202 / 503 配一个两千字节的壳页 —— 它看起来像"请求成功了"。
    #   放行的话，后面两个探针会把"IP 被拦"误报成"路径与美国站不同构"
    #   或"页面没有 glow 挂件"，而那种误报会被人回填进注册表。
    #   一个真实的 Amazon 首页在 100KB 以上，20KB 是很宽松的下界。
    if r.status_code != 200:
        _bad(f"status={r.status_code}（不是 200）。Amazon 对机房出口 IP 常回 "
             f"202/503 加一个壳页 —— 这几乎一定是出口 IP 问题，不是站点差异。"
             f"换住宅代理再跑。")
        return False
    if len(r.text) < 20000:
        _bad(f"200 但只有 {len(r.text)} bytes —— 真首页在 100KB 以上。"
             f"这是个壳页/软拦截，换住宅代理再跑。")
        return False
    _ok(f"拿到真页面（{len(r.text)} bytes）")
    return True


def probe_zip_change(sess, spec, postal: str) -> None:
    """1) 地址切换接口的形状 + zipCode 参数是否接受本站点邮编。"""
    print(f"\n[1] 地址切换接口  {spec.zip_change_url}")
    print(f"      邮编={postal!r}（注册表的规范形）")
    cookies = sess.cookies
    data = {
        "locationType": "LOCATION_INPUT",
        "zipCode": postal,
        "storeContext": "generic",
        "deviceType": "web",
        "pageType": "Gateway",
        "actionSource": "glow",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{spec.base_url}/",
        "Origin": spec.base_url,
        "anti-csrftoken-a2z": cookies.get("csm-hit", "") if cookies else "",
    }
    try:
        r = sess.post(spec.zip_change_url, data=data, headers=headers, timeout=40)
    except Exception as e:
        _bad(f"POST 异常：{type(e).__name__}: {e}")
        return
    print(f"      status={r.status_code} bytes={len(r.text)}")
    if r.status_code == 404:
        _bad("404 —— 路径与美国站**不同构**。注册表的 zip_change_url 要改。")
        return
    if r.status_code != 200:
        _bad(f"非 200。假设「路径同构」存疑。响应片段：{r.text[:200]!r}")
        return
    try:
        j = r.json()
    except Exception:
        _warn(f"200 但不是 JSON。响应片段：{r.text[:200]!r}")
        return
    print(f"      json={j}")
    if j.get("isValidAddress") == 1:
        _ok("isValidAddress=1 —— 路径同构、且接受带空格的本站点邮编。"
            "注册表这一项可以标 verified。")
    else:
        _bad("isValidAddress 不是 1。试试无空格形（见下）或补 countryCode 字段。")
        compact = postal.replace(" ", "")
        if compact != postal:
            print(f"      重试无空格形 {compact!r} …")
            data["zipCode"] = compact
            try:
                r2 = sess.post(spec.zip_change_url, data=data,
                               headers=headers, timeout=40)
                print(f"      status={r2.status_code} json={r2.json()}")
                if r2.json().get("isValidAddress") == 1:
                    _ok("无空格形被接受 —— 说明 normalize_postal 的规范形"
                        "应该改成无空格，或者在 POST 前去掉空格。")
            except Exception as e:
                _warn(f"重试失败：{type(e).__name__}: {e}")


def probe_glow_and_price(sess, spec, asin: str, postal: str) -> None:
    """2) glow 文案形状 + 3) 价格渲染形态。"""
    url = f"{spec.base_url}/dp/{asin}?th=1&psc=1"
    print(f"\n[2/3] 商品页  {url}")
    try:
        r = sess.get(url, timeout=40)
    except Exception as e:
        _bad(f"请求异常：{type(e).__name__}: {e}")
        return
    print(f"      status={r.status_code} bytes={len(r.text)}")
    if _blocked(r.text):
        _bad("拦截页 —— 换代理再跑")
        return
    html = r.text

    # --- glow ---
    print("\n[2] glow 配送地址挂件")
    m = _GLOW_RE.search(html)
    if not m:
        _bad('页面里没有 id="glow-ingress-line2"。'
             "ziputil 的邮编生效判定在这个站点上会恒返回 None（宽松放行）—— "
             "也就是说切邮编成没成功无从判断。")
    else:
        text = m.group(1).strip()
        print(f"      文案={text!r}")
        from worker.ziputil import _location_matches
        if _location_matches(postal, text, spec.id):
            _ok("当前实现判定为「已生效」")
        else:
            _bad(f"当前实现判定为「未生效」。期望 {postal!r} 与文案对不上 —— "
                 "看看文案里是不是只有城市名、或者邮编形状不同。")
        compact = re.sub(r"[\s-]+", "", postal).upper()
        got = re.sub(r"[\s-]+", "", text).upper()
        print(f"      完整邮编在文案里: {compact in got}")
        print(f"      仅 FSA(前三位)在文案里: {compact[:3] in got}")

    # --- price ---
    print("\n[3] 价格渲染形态")
    hits = _PRICE_RE.findall(html)
    if not hits:
        _warn("没抓到任何价格串（可能是缺货/需登录，换一个 ASIN 再试）")
        return
    from collections import Counter
    shapes = Counter()
    for h in hits:
        sym = re.match(r"(CDN\$|C\$|US\$|\$)", h).group(1)
        shapes[sym] += 1
    print(f"      共 {len(hits)} 处价格串，符号分布：{dict(shapes)}")
    print(f"      样例：{hits[:8]}")
    known = set(spec.price_symbols)
    unknown = set(shapes) - known
    if unknown:
        _bad(f"出现了注册表没收的符号 {sorted(unknown)} —— "
             f"price_symbols 要补，否则这些价格会被当成「不是价格」而丢掉")
    else:
        _ok(f"全部符号都在注册表的 price_symbols {sorted(known)} 里")
    top = shapes.most_common(1)[0][0]
    if top != spec.render_symbol:
        _warn(f"页面最常见的符号是 {top!r}，而注册表 render_symbol={spec.render_symbol!r}。"
              "只影响「页面只给裸数字、我们自己拼」的那条路径，不影响正常解析。")
    else:
        _ok(f"最常见符号与 render_symbol 一致（{top!r}）")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="实测一个 Amazon 站点的采集假设（F-012）")
    ap.add_argument("--marketplace", default="amazon.ca",
                    help="站点规范键，如 amazon.ca（默认 amazon.ca）")
    ap.add_argument("--postal", default=None,
                    help="投递地编码，默认用注册表的 default_postal")
    ap.add_argument("--asin", default=_DEFAULT_ASIN,
                    help=f"探针用的 ASIN（默认 {_DEFAULT_ASIN}）")
    ap.add_argument("--proxy", default=None,
                    help="代理 URL，默认读环境变量 PROXY_URL")
    ap.add_argument("--no-proxy", action="store_true",
                    help="强制不走代理")
    args = ap.parse_args()

    try:
        spec = M.get(args.marketplace)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    postal = args.postal or spec.default_postal
    norm = M.normalize_postal(postal, spec.id)
    if norm is None:
        print(f"错误：{postal!r} 不是{spec.label}的合法邮编形状", file=sys.stderr)
        return 2
    if norm != postal:
        print(f"（邮编归一化：{postal!r} -> {norm!r}）")
    postal = norm

    proxy = None if args.no_proxy else (args.proxy or os.environ.get("PROXY_URL") or None)

    print("=" * 68)
    print(f"站点     : {spec.id}（{spec.label}）  verified={spec.verified}")
    print(f"base_url : {spec.base_url}")
    print(f"币种     : {spec.currency}   符号: {spec.price_symbols}"
          f"   拼接用: {spec.render_symbol!r}")
    print(f"邮编     : {postal!r}")
    print(f"代理     : {'（不走代理）' if not proxy else proxy.split('@')[-1]}")
    print("=" * 68)

    sess = _session(spec, proxy)
    if not probe_reachable(sess, spec):
        print("\n连通性这一关没过，后面的探针没有意义。")
        print("⚠ 但**先别断定是出口 IP 的问题** —— 本脚本用的是裸 curl_cffi"
              " session，\n  没走 AmazonSession.initialize() 那套预热，"
              "实测出现过「采集器跑得通、\n  本脚本吃 202 壳页」的情况"
              "（见本文件顶部「已知缺陷」）。")
        print("\n两条路：")
        print("  1) 换住宅代理再跑一次 —— 真是 IP 问题的话这样就过了；")
        print("  2) 起一个真实采集批次 + 人工核对商品页 —— 更慢，但它就是这个"
              "功能\n     最终要跑的那条路，证据力更强。amazon.ca 当初就是这么"
              "验证的。")
        return 1
    probe_zip_change(sess, spec, postal)
    probe_glow_and_price(sess, spec, args.asin, postal)

    print("\n" + "=" * 68)
    print("跑完了。把上面的实测结果回填进 common/core/marketplace.py 的注册表，")
    print(f"确认无误后把 {spec.id} 那条的 verified 改成 True。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
