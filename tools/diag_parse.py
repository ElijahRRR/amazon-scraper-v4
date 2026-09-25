#!/usr/bin/env python3
"""tools/diag_parse.py —— 拿一张存下来的商品页 HTML，离线看解析器都取到了什么。

------------------------------------------------------------------------
为什么需要它
------------------------------------------------------------------------
F-012 开加拿大站时，真实采集暴露了一批**字段级**缺陷：自营卖家漏采、
包装尺寸存进了商品尺寸、大类排名和料号漏采。这类缺陷有三个共同点：

  1. 只在**真实页面**上出现（构造的测试 HTML 复现不了 —— 复现得了就说明
     我们已经知道页面长什么样，那就不叫排查了）；
  2. 采集**成功**，completeness 也过得去，所以没有任何告警；
  3. 光看库里那一行看不出是解析错了还是页面上本来就没有。

要定位它们只有一条路：把真实页面存下来，离线跑解析器，**逐字段**对照
页面上的真值。本工具就干这件事。

------------------------------------------------------------------------
怎么用
------------------------------------------------------------------------
1) 采集时把页面留档（用完记得关，一张页 0.5-2MB）：

       DUMP_HTML=1 python run_worker.py ...

   存到 ``worker/degraded_dump/ok/<站点>_<asin>_<邮编>_<时间>.html``

2) 离线看解析结果：

       python -m tools.diag_parse worker/degraded_dump/ok/amazon.ca_B09S6W6H5B_*.html

   默认从文件名推断站点。也可以显式指定：

       python -m tools.diag_parse page.html --marketplace amazon.ca --zip 'K1V 7P8'

3) 只看某几个字段，并打印页面上相关的原始片段（定位用）：

       python -m tools.diag_parse page.html --fields seller_name,model_number --raw

4) 两个站点的同一个 ASIN 做对照（找站点差异最快的办法）：

       python -m tools.diag_parse ca.html --diff us.html

------------------------------------------------------------------------
它不做什么
------------------------------------------------------------------------
不联网、不写库、不改配置。纯粹是「HTML 进，解析结果出」。
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.core import marketplace as M          # noqa: E402
from worker.parser import AmazonParser            # noqa: E402

#: 最常出问题的那些字段排在前面 —— 看输出的人一眼能扫到。
#: 顺序是**按排查价值**排的，不是按 ASIN_DATA_FIELDS 的列序。
_FIELD_GROUPS = [
    ("身份", ["asin", "title", "subtitle", "brand", "model_number",
              "part_number", "manufacturer", "upc_list"]),
    ("价格/库存", ["current_price", "buybox_price", "original_price",
                   "buybox_shipping", "offer_condition",
                   "stock_status", "stock_count", "is_fba"]),
    ("卖家", ["seller_id", "seller_name"]),
    ("配送/邮编", ["delivery_date", "delivery_time", "zip_code",
                   "_zip_observed", "_zip_verify"]),
    ("尺寸/重量", ["package_dimensions", "package_weight",
                   "item_dimensions", "item_weight"]),
    ("类目/排名", ["best_sellers_rank", "root_category_id", "category_ids",
                   "category_tree", "product_type"]),
    ("内容", ["bullet_points", "long_description", "image_urls"]),
    ("变体", ["parent_asin", "variation_asins", "variant_attributes"]),
    ("其他", ["country_of_origin", "is_customized", "first_available_date",
              "rating", "review_count", "product_url", "site", "marketplace",
              "_completeness", "_parse_engine"]),
]

#: 「这个字段没取到」的判据。与服务端 `_NA_VALUES` 同口径，
#: 但这里是给人看的，所以把空列表/空串也算进去。
_MISSING = {"", "N/A", "n/a", "None", "null", "-", "0"}


def _guess_marketplace(path: str) -> str:
    """从文件名推断站点（DUMP_HTML 存的文件名就是 ``<站点>_<asin>_...``）。"""
    base = os.path.basename(path)
    for mid in M.all_ids():
        if base.startswith(mid + "_"):
            return mid
    # 兜底：页面里出现哪个站点的域名更多
    return M.DEFAULT_MARKETPLACE


def _guess_asin(path: str, html: str) -> str:
    m = re.search(r"_(B[0-9A-Z]{9})_", os.path.basename(path))
    if m:
        return m.group(1)
    m = re.search(r'"(?:ASIN|asin)"\s*:\s*"(B[0-9A-Z]{9})"', html)
    return m.group(1) if m else "B0UNKNOWN0"


def _short(v, width=100) -> str:
    s = str(v).replace("\n", " ⏎ ")
    return s if len(s) <= width else s[:width - 1] + "…"


def _is_missing(v) -> bool:
    if v is None:
        return True
    return str(v).strip() in _MISSING


def _print_result(result: dict, only: set | None, width: int) -> None:
    missing = []
    for group, fields in _FIELD_GROUPS:
        rows = [(f, result.get(f)) for f in fields
                if f in result and (not only or f in only)]
        if not rows:
            continue
        print(f"\n\033[1m{group}\033[0m")
        for f, v in rows:
            mark = "\033[31m✗\033[0m" if _is_missing(v) else " "
            if _is_missing(v):
                missing.append(f)
            print(f"  {mark} {f:<24} {_short(v, width)}")

    # 解析器产出了、但不在上面任何一组里的键 —— 加了新字段时会出现在这里
    known = {f for _, fs in _FIELD_GROUPS for f in fs}
    extra = [k for k in result if k not in known and not k.startswith("_")]
    if extra and not only:
        print(f"\n\033[1m未分组（分组表该补了）\033[0m")
        for k in sorted(extra):
            print(f"    {k:<24} {_short(result[k], width)}")

    if missing:
        print(f"\n\033[31m没取到的字段（{len(missing)}）\033[0m: "
              + ", ".join(missing))
        print("  ⚠ 「没取到」不等于「解析错了」—— 页面上本来就没有这一项也会是这样。"
              "\n    用 --raw 看页面里相关的原始片段，再决定是谁的问题。")


#: `--raw` 时按字段去页面里捞原始片段。key 是字段名，值是若干正则。
#: 这些正则**只为人眼定位服务**，不参与任何判定 —— 宽松一点没关系。
_RAW_PROBES = {
    "seller_name": [r'sellerProfileTriggerId[^>]*>([^<]{1,80})',
                    r'(Sold by[^<]{0,60})', r'(Ships from[^<]{0,60})',
                    r'(merchantID"\s*:\s*"[A-Z0-9]+)'],
    "seller_id": [r'seller=([A-Z0-9]{5,20})', r'merchantID"\s*:\s*"([A-Z0-9]+)"'],
    "model_number": [r'([Ii]tem model number[^<]{0,60})',
                     r'(Model Number[^<]{0,60})', r'(模[^<]{0,40})'],
    "part_number": [r'(art [Nn]umber[^<]{0,60})'],
    "best_sellers_rank": [r'([Bb]est ?[Ss]ellers ?[Rr]ank[^<]{0,120})',
                          r'(SalesRank[^<]{0,80})'],
    "package_dimensions": [r'([Pp]ackage [Dd]imensions[^<]{0,80})',
                           r'([Pp]roduct [Dd]imensions[^<]{0,80})'],
    "item_dimensions": [r'([Pp]roduct [Dd]imensions[^<]{0,80})',
                        r'([Ii]tem [Dd]imensions[^<]{0,80})'],
    "package_weight": [r'([Pp]ackage [Ww]eight[^<]{0,60})',
                       r'([Ss]hipping [Ww]eight[^<]{0,60})',
                       r'([Ii]tem [Ww]eight[^<]{0,60})'],
    "item_weight": [r'([Ii]tem [Ww]eight[^<]{0,60})'],
    "_zip_observed": [r'glow-ingress-line2[^>]*>\s*([^<]{1,60})'],
    "current_price": [r'a-offscreen"?>\s*([^<]{1,20})'],
}


def _print_raw(html: str, fields: list) -> None:
    print("\n\033[1m页面原始片段（只为人眼定位，不参与判定）\033[0m")
    for f in fields:
        probes = _RAW_PROBES.get(f)
        if not probes:
            continue
        print(f"\n  \033[36m{f}\033[0m")
        found = False
        for pat in probes:
            for m in list(re.finditer(pat, html))[:4]:
                frag = re.sub(r"\s+", " ", m.group(1)).strip()
                if frag:
                    print(f"      {_short(frag, 110)}")
                    found = True
        if not found:
            print("      （页面里没找到相关片段 —— 很可能页面上本来就没有这一项）")


def _parse_one(path: str, marketplace: str | None, zip_code: str | None) -> dict:
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    mid = marketplace or _guess_marketplace(path)
    spec = M.get(mid)
    asin = _guess_asin(path, html)
    zc = zip_code or spec.default_postal
    result = AmazonParser().parse_product(html, asin, zc, mid)
    result["_html"] = html          # 给 --raw 用，打印时会跳过
    result["_source"] = path
    result["_marketplace_used"] = mid
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="离线跑解析器，逐字段看取到了什么（F-012 排查用）")
    ap.add_argument("html", help="商品页 HTML 文件（支持通配符，取第一个匹配）")
    ap.add_argument("--marketplace", default=None,
                    help="站点，默认从文件名推断")
    ap.add_argument("--zip", dest="zip_code", default=None,
                    help="请求邮编，默认用该站点的 default_postal")
    ap.add_argument("--fields", default=None,
                    help="只看这几个字段，逗号分隔")
    ap.add_argument("--raw", action="store_true",
                    help="同时打印页面里相关的原始片段")
    ap.add_argument("--diff", default=None,
                    help="与另一张 HTML 做逐字段对照（找站点差异最快）")
    ap.add_argument("--width", type=int, default=100, help="值的截断宽度")
    args = ap.parse_args()

    matches = sorted(glob.glob(args.html)) or [args.html]
    path = matches[0]
    if not os.path.exists(path):
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    if len(matches) > 1:
        print(f"（{args.html} 匹配到 {len(matches)} 个文件，用第一个）")

    only = set(f.strip() for f in args.fields.split(",")) if args.fields else None

    a = _parse_one(path, args.marketplace, args.zip_code)
    print("=" * 74)
    print(f"文件   : {a['_source']}")
    print(f"站点   : {a['_marketplace_used']}  ({M.get(a['_marketplace_used']).label})")
    print(f"ASIN   : {a['asin']}   请求邮编: {a['zip_code']}")
    print(f"引擎   : {a.get('_parse_engine')}   完整度: {a.get('_completeness')}")
    print("=" * 74)

    html = a.pop("_html")
    a.pop("_source", None)
    a.pop("_marketplace_used", None)

    if args.diff:
        b_matches = sorted(glob.glob(args.diff)) or [args.diff]
        b = _parse_one(b_matches[0], None, None)
        b_html = b.pop("_html")
        b.pop("_source", None)
        b.pop("_marketplace_used", None)
        print(f"\n\033[1m逐字段对照\033[0m  A={os.path.basename(path)}  "
              f"B={os.path.basename(b_matches[0])}")
        diffs = 0
        for _, fields in _FIELD_GROUPS:
            for f in fields:
                if only and f not in only:
                    continue
                va, vb = a.get(f), b.get(f)
                if va == vb:
                    continue
                diffs += 1
                print(f"\n  \033[36m{f}\033[0m")
                print(f"      A: {_short(va, args.width)}")
                print(f"      B: {_short(vb, args.width)}")
        print(f"\n共 {diffs} 处不同。")
        print("  ⚠ 不同**不等于**有 bug：两个站点的同一个 ASIN 本来就该有不同的"
              "\n    价格、卖家、配送。要看的是**结构性**差异 —— 一边有值一边空。")
        return 0

    _print_result(a, only, args.width)
    if args.raw:
        _print_raw(html, sorted(only) if only
                   else [f for f in _RAW_PROBES if _is_missing(a.get(f))])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
