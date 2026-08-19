#!/usr/bin/env python3
"""按批次取数据与截图的消费端脚本 —— 直接可跑，不依赖本仓库任何模块。

    # 一次性拿走某一批的数据 + 截图
    python3 tools/consume_batch.py batch my_batch_20260819 \
        --server http://47.108.92.213/amazon-v4 --token "$EXPORT_TOKEN" \
        --out ./out --screenshots

    # 持续增量同步（游标存盘，可随时 Ctrl+C 再接着跑）
    python3 tools/consume_batch.py sync \
        --server http://47.108.92.213/amazon-v4 --token "$EXPORT_TOKEN" \
        --out ./out --state ./out/cursor.json

只用标准库（urllib + json + csv），拷到任何一台有 Python 3.8+ 的机器上就能跑。

------------------------------------------------------------------------
两个子命令解决的是两个不同的问题，别混用
------------------------------------------------------------------------
``batch``  「我刚推的这一批，到底采到了什么」。读
           ``/api/export/batch/{name}/records``，每条记录都是一次**真实发生过
           的采集**。批次内游标，拉完即止。

``sync``   「把服务端的新数据持续同步到我这边」。读
           ``/api/export/incremental``，全局游标，必须持久化。

⚠ **两个游标不可互换。** 数值同源于事件流的 ``seq``，把 ``batch`` 的
  ``next_cursor`` 喂进 ``sync`` 不会报错，会**静默跳过**中间所有别的批次的事件。
  所以本脚本把它们存在两个不同的键下，且 ``batch`` 根本不写状态文件。

------------------------------------------------------------------------
为什么不用 /api/results?batch_id=
------------------------------------------------------------------------
那个端点读 ``asin_data``（每个 ASIN 一行的**最新态**），``batch_id`` 只是个
成员过滤器。这批采失败的 ASIN——只要它以前采过——照样命中，返回的是**上一次
的旧行**，而响应里没有任何字段能让你看出它的年龄。摄进自己的库、盖上一个新鲜
的接收时间，陈旧数据就此看起来很新鲜，两侧都不会报错。

本脚本因此**只**从事件流读。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LIMIT = 500
#: 429/5xx 的重试次数与退避基数（秒）。指数退避，最后一次约 16s。
RETRIES = 5
BACKOFF = 2.0


# ============================================================ HTTP

def _get(server, path, token, params=None, raw=False, timeout=60):
    """GET；重试瞬时故障。返回 (status, body)。

    body：``raw`` 为真时是 bytes，否则是解析好的 JSON（解析不了则是原文 str）。

    **不重试 4xx**（除 429）：那是请求本身的问题，重试只会重复同一个错误。
    """
    url = server.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
    headers = {"Accept": "*/*"}
    if token:
        headers["X-Export-Token"] = token

    last = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, (body if raw else _json(body))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code == 429 or exc.code >= 500:
                last = (exc.code, (body if raw else _json(body)))
            else:
                return exc.code, (body if raw else _json(body))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = (0, {"error": "network", "detail": str(exc)})
        if attempt < RETRIES - 1:
            time.sleep(BACKOFF ** attempt)
    return last if last else (0, {"error": "unreachable"})


def _json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:                                  # noqa: BLE001
        return body.decode("utf-8", "replace")


def _die(msg, code=1):
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(code)


# ============================================================ 记录处理

def key_of(rec):
    """消费侧的分组键：**(asin, zipcode)**，不是 asin。

    同一个 ASIN 在不同邮编下是**两条不同的事实**（价格、配送、库存都可能不同）。
    只按 asin 归并，两个邮编的数据会互相覆盖，而且覆盖的方向取决于事件顺序
    —— 一个看不出来的错。
    """
    return (rec["asin"], rec["scrape_params"].get("zipcode"))


def zip_trustworthy(rec, strict=False):
    """这条记录的邮编到底生没生效。

    ``confirmed``   页面上看到的就是请求的邮编 —— 最强证据
    ``assumed``     切邮编那次验证过，本页没有页面级证据
    ``unverified``  什么证据都没有
    ``mismatch``    页面显示的是**别的**邮编 —— 这条数据不属于你要的邮编

    ``mismatch`` **一律丢弃**：它不是"证据弱"，是"证据表明是错的"。
    ``strict`` 再把 ``assumed`` / ``unverified`` 也挡掉。
    """
    v = rec["scrape_params"].get("zip_verify")
    if v == "mismatch":
        return False
    if strict and v not in ("confirmed",):
        return False
    return True


def usable(rec, strict_zip=False):
    """能不能拿去更新你的商品库。

    ``outcome != 'ok'`` 的记录**只该进快照表，不要 upsert 商品**：它的
    slow/fast 基本是空的，那是"本次没采到"，不是"值变成空了"。拿它覆盖
    已有数据 = 用一次失败把好数据擦掉。
    """
    return rec.get("outcome") == "ok" and zip_trustworthy(rec, strict_zip)


# ============================================================ 输出

#: CSV 的列。嵌套字段拍平成点号路径，顺序固定 —— 输出要能直接 diff。
CSV_COLUMNS = [
    ("cursor", lambda r: r["cursor"]),
    ("asin", lambda r: r["asin"]),
    ("zipcode", lambda r: r["scrape_params"].get("zipcode")),
    ("zip_verify", lambda r: r["scrape_params"].get("zip_verify")),
    ("outcome", lambda r: r.get("outcome")),
    ("scraped_at", lambda r: r.get("scraped_at")),
    ("title", lambda r: r["slow"].get("title")),
    ("brand", lambda r: r["slow"].get("brand")),
    ("category_path", lambda r: " > ".join(r["slow"].get("category_path") or [])),
    ("price", lambda r: r["fast"].get("price")),
    ("shipping", lambda r: r["fast"].get("shipping")),
    ("shipping_raw", lambda r: r["fast"].get("shipping_raw")),
    ("stock_state", lambda r: r["fast"].get("stock_state")),
    ("stock_count", lambda r: r["fast"].get("stock_count")),
    ("buybox_seller", lambda r: r["fast"].get("buybox_seller")),
    ("variant_theme", lambda r: (r["slow"].get("variant") or {}).get("theme")),
    ("parent_asin", lambda r: (r["slow"].get("variant") or {}).get("parent_asin")),
    ("completeness_ok", lambda r: r.get("completeness_ok")),
    ("error_type", lambda r: (r.get("raw") or {}).get("error_type")),
]


def write_outputs(records, out_dir, stem):
    os.makedirs(out_dir, exist_ok=True)
    jsonl = os.path.join(out_dir, f"{stem}.jsonl")
    with open(jsonl, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    csv_path = os.path.join(out_dir, f"{stem}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([c for c, _ in CSV_COLUMNS])
        for rec in records:
            w.writerow([fn(rec) for _, fn in CSV_COLUMNS])
    return jsonl, csv_path


# ============================================================ 截图

def fetch_screenshots(server, token, batch_name, out_dir):
    """把这一批已截好的图全下下来。返回 (下好的, 还没好的, 失败的)。

    走 `/api/screenshots` 列状态，只下 `status == "done"` 的那些 —— 它们的
    `url` 才非空。pending 的**不重试**：本脚本是一次性取数，等图该由调用方
    决定要不要再跑一次，而不是在这里死循环。
    """
    shots_dir = os.path.join(out_dir, "screenshots", batch_name)
    os.makedirs(shots_dir, exist_ok=True)

    done, pending, failed, cursor = [], [], [], None
    while True:
        st, body = _get(server, "/api/screenshots", token,
                        {"batch_name": batch_name, "cursor": cursor, "limit": 200})
        if st == 404:
            return [], [], []
        if st != 200:
            _die(f"列截图失败：HTTP {st} {body}")
        for item in body["items"]:
            if item["status"] == "done":
                done.append(item)
            elif item["status"] == "failed":
                failed.append(item)
            else:
                pending.append(item)
        cursor = body.get("next_cursor")
        if not cursor:
            break

    saved = []
    for item in done:
        # 用列表给的 url（绝对地址），而不是自己拼路径 —— 反代加了路径前缀时
        # 自己拼会拼错，服务端回显的那个是对的。
        path = urllib.parse.urlsplit(item["url"]).path
        st, blob = _get(server, path, token, raw=True)
        if st == 200:
            dest = os.path.join(shots_dir, f"{item['asin']}.png")
            with open(dest, "wb") as fh:
                fh.write(blob)
            saved.append(item["asin"])
        elif st == 409:
            pending.append(item)          # 列表到取图之间状态回退了，很罕见
        elif st == 410:
            failed.append(item)
        else:
            print(f"  ⚠ 取图失败 {item['asin']}: HTTP {st}", file=sys.stderr)
    return saved, pending, failed


# ============================================================ 子命令

def cmd_batch(args):
    records, cursor, pages = [], 0, 0
    while True:
        st, body = _get(args.server,
                        f"/api/export/batch/{urllib.parse.quote(args.batch_name)}/records",
                        args.token, {"cursor": cursor, "limit": args.limit})
        if st == 404:
            _die(f"批次不存在：{args.batch_name}")
        if st == 401:
            _die("X-Export-Token 缺失或不匹配（--token）")
        if st == 503:
            _die(f"事件流不可用：{body}")
        if st != 200:
            _die(f"HTTP {st}: {body}")

        records.extend(body["records"])
        pages += 1
        cursor = body["next_cursor"]
        if not body["has_more"]:
            break
        if pages > 10000:
            _die("分页超过 10000 页，疑似游标没推进")

    cov = body["coverage"]
    good = [r for r in records if usable(r, args.strict_zip)]
    grouped = {}
    for rec in good:
        # 同一个 (asin, zipcode) 若有多条（重试后成功），保留 cursor 最大的那条
        k = key_of(rec)
        if k not in grouped or rec["cursor"] > grouped[k]["cursor"]:
            grouped[k] = rec
    latest = sorted(grouped.values(), key=lambda r: r["cursor"])

    stem = f"batch_{args.batch_name}"
    jsonl, csv_path = write_outputs(latest, args.out, stem)

    print(f"批次 {args.batch_name}（id={body['batch']['id']}, "
          f"status={body['batch']['status']}）")
    print(f"  事件记录     {len(records)}  （含失败与重试）")
    print(f"  可用记录     {len(good)}     （outcome=ok 且邮编可信）")
    print(f"  去重后       {len(latest)}   （按 (asin, zipcode) 取最新）")
    print(f"  覆盖率       {cov['asin_with_event']}/{cov['asin_total']} 个 ASIN 有过采集事件")
    if cov["asin_with_event"] < cov["asin_total"]:
        missing = cov["asin_total"] - cov["asin_with_event"]
        print(f"  ⚠ {missing} 个 ASIN 一次事件都没有：还没采完，或事件已过保留期")
    print(f"  -> {jsonl}")
    print(f"  -> {csv_path}")

    if args.screenshots:
        saved, pending, failed = fetch_screenshots(
            args.server, args.token, args.batch_name, args.out)
        print(f"  截图         下好 {len(saved)}，还没好 {len(pending)}，"
              f"失败 {len(failed)}")
        if failed:
            print(f"     失败的：{', '.join(i['asin'] for i in failed[:10])}"
                  f"{' …' if len(failed) > 10 else ''}")


def cmd_sync(args):
    state = {}
    if args.state and os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as fh:
            state = json.load(fh)
    cursor = int(state.get("incremental_cursor", 0))

    total, batches = 0, 0
    while True:
        st, body = _get(args.server, "/api/export/incremental", args.token,
                        {"cursor": cursor, "limit": args.limit})
        if st == 409:
            # 契约要求：**告警并做一次全量对账**，不能静默跳过。
            _die("游标已掉出保留窗口（cursor_below_retention）。"
                 f"服务端 min_available_cursor={body.get('min_available_cursor')}，"
                 f"你的 cursor={cursor}。需要做一次全量对账，脚本不会自己跳过。", 2)
        if st == 401:
            _die("X-Export-Token 缺失或不匹配（--token）")
        if st != 200:
            _die(f"HTTP {st}: {body}")

        records = body["records"]
        if records:
            good = [r for r in records if usable(r, args.strict_zip)]
            batches += 1
            write_outputs(good, args.out, f"sync_{body['next_cursor']}")
            total += len(good)
            print(f"  +{len(good)}/{len(records)} 条  cursor -> {body['next_cursor']}")

        # 先落盘再推进游标：崩在这中间就重放一页，宁可重不可漏。
        cursor = body["next_cursor"]
        if args.state:
            state["incremental_cursor"] = cursor
            tmp = args.state + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, args.state)      # 原子替换，免得崩在写一半

        if not body["has_more"]:
            if args.once:
                break
            time.sleep(args.interval)

    print(f"追平。本次共 {total} 条可用记录，{batches} 页，cursor={cursor}")


# ============================================================ CLI

def _shared(ap, suppress=False):
    """公共选项。挂**两遍**：主解析器一遍、每个子命令一遍。

    为什么两遍：argparse 只认「选项在它所属的那一层」。只挂主解析器时，
    `consume_batch.py batch NAME --server ...` 会直接
    `error: unrecognized arguments` —— 而那正是最自然的写法（实测踩到了）。

    子命令那一遍用 `default=SUPPRESS`：没写就**不往 namespace 里放键**，
    于是主解析器已经解析到的值不会被子解析器的默认值覆盖回去。
    少了这一句，`--server X batch NAME` 会在子解析阶段把 server 清成 ""。
    """
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    ap.add_argument("--server", default=d(os.environ.get("SCRAPER_SERVER", "")),
                    help="服务地址，如 http://1.2.3.4:8899 "
                         "（反代带路径前缀就写全，如 http://1.2.3.4/amazon-v4）")
    ap.add_argument("--token", default=d(os.environ.get("EXPORT_TOKEN", "")),
                    help="X-Export-Token；服务端没配就留空")
    ap.add_argument("--out", default=d("./out"), help="输出目录")
    ap.add_argument("--limit", type=int, default=d(DEFAULT_LIMIT),
                    help=f"单页条数，上限 1000（默认 {DEFAULT_LIMIT}）")
    ap.add_argument("--strict-zip", action="store_true",
                    default=d(False),
                    help="只要 zip_verify=confirmed 的记录（默认还接受 "
                         "assumed/unverified，但 mismatch 一律丢）")


def main():
    ap = argparse.ArgumentParser(
        description="按批次 / 增量消费采集结果",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    _shared(ap)

    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("batch", help="拉某一个批次真正采到的数据")
    b.add_argument("batch_name")
    b.add_argument("--screenshots", action="store_true", help="顺便把截图下下来")
    _shared(b, suppress=True)
    b.set_defaults(func=cmd_batch)

    s = sub.add_parser("sync", help="全局增量同步（游标存盘）")
    s.add_argument("--state", default="./out/cursor.json", help="游标文件")
    s.add_argument("--once", action="store_true", help="追平就退出，不驻留")
    s.add_argument("--interval", type=float, default=60.0,
                   help="追平后的轮询间隔秒数（默认 60）")
    _shared(s, suppress=True)
    s.set_defaults(func=cmd_sync)

    args = ap.parse_args()
    if not args.server:
        _die("必须给 --server（或设环境变量 SCRAPER_SERVER）")
    args.func(args)


if __name__ == "__main__":
    main()
