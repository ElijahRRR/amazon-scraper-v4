"""``GET /api/screenshots`` 与 ``GET /api/screenshots/{batch_name}/{asin}``。

------------------------------------------------------------------------
这份文件钉的是什么
------------------------------------------------------------------------
在这两条端点之前，程序化调用方拿截图只有两条路，都不好用：

  1. ``GET /api/export/{batch}/screenshots`` —— 整批 ZIP。要一张也得拉全批，
     而且批次没截完就是 404，中途什么都取不到。
  2. 猜 ``/static/screenshots/<batch>/<asin>.png`` —— 那是磁盘布局，契约外的
     实现细节；更要命的是那条路上 **404 有三种含义**（没这个 ASIN / 还没截 /
     截失败了），调用方无法据此判断该不该再等。

所以本文件的重头戏是 :class:`ScreenshotFetchStatusCodesTests`：**四个状态码
必须互不相同**。它们全是 4xx，"能取到图"这件事测起来很容易全绿，但把 409
（还没好，回头再来）和 410（失败了，别等了）合并成一个 404，调用方就只能
无限轮询一个永远不会出现的文件 —— 而且服务端不会有任何异常。

写成 ``unittest.TestCase``：门禁里 ``unittest discover`` 只认 TestCase 子类。
跟着 ``DB_BACKEND`` 走 ``isolated_server``，四道 runner 门都盖得到。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server

_BATCH = "shot_batch"
_A1 = "B00SHOTA01"
_A2 = "B00SHOTA02"

#: 最小合法 PNG（1x1，透明）。内容无所谓，但必须是真 PNG ——
#: 上传端点会原样落盘，取图端点回的 media_type 得对得上。
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000b49444154789c6360000200000500017a5eab3f0000"
    "000049454e44ae426082"
)


def _make_batch(client, asins=(_A1, _A2), screenshot=True, name=_BATCH):
    r = client.post("/api/batches", json={
        "asins": list(asins), "batch_name": name,
        "needs_screenshot": screenshot,
    })
    assert r.status_code == 200, r.text
    return r.json()["batch_id"]


def _register_worker(client, worker_id="shot-worker"):
    """截图上传端点会拒绝「未注册 / 心跳超时」的 worker。

    最省事的注册方式就是拉一次任务 —— ``/api/tasks/pull`` 里就有
    ``_register_worker``，不必去猜内部注册表的结构。
    """
    r = client.get("/api/tasks/pull",
                   params={"worker_id": worker_id, "count": 50,
                           "enable_screenshot": "true"})
    assert r.status_code == 200, r.text
    return worker_id


def _upload_shot(client, asin, worker_id, name=_BATCH):
    return client.post(
        "/api/tasks/screenshot",
        files={"file": (f"{asin}.png", _PNG, "image/png")},
        data={"asin": asin, "batch_name": name, "worker_id": worker_id},
    )


def _fail_shot(client, asin, error="renderer boom", name=_BATCH):
    return client.post("/api/tasks/screenshot/fail",
                       json={"asin": asin, "batch_name": name, "error": error})


class ScreenshotListTests(unittest.TestCase):

    def test_lists_every_asin_with_its_status(self):
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            r = client.get("/api/screenshots", params={"batch_name": _BATCH})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["batch_name"], _BATCH)
            self.assertEqual([i["asin"] for i in body["items"]], [_A1, _A2])
            self.assertEqual({i["status"] for i in body["items"]}, {"pending"})
            self.assertEqual(body["progress"]["total"], 2)
            self.assertEqual(body["progress"]["pending"], 2)

    def test_url_is_null_until_the_shot_is_done(self):
        """没截好就给 URL，等于请调用方去撞 404。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            wid = _register_worker(client)

            before = client.get("/api/screenshots",
                                params={"batch_name": _BATCH}).json()
            self.assertTrue(all(i["url"] is None for i in before["items"]),
                            before["items"])

            self.assertEqual(_upload_shot(client, _A1, wid).status_code, 200)
            after = client.get("/api/screenshots",
                               params={"batch_name": _BATCH}).json()
            by_asin = {i["asin"]: i for i in after["items"]}
            self.assertEqual(by_asin[_A1]["status"], "done")
            self.assertTrue(by_asin[_A1]["url"].endswith(
                f"/api/screenshots/{_BATCH}/{_A1}"), by_asin[_A1]["url"])
            self.assertIsNone(by_asin[_A2]["url"])

    def test_the_url_it_hands_out_actually_works(self):
        """列表给的 ``url`` 必须是能直接 GET 的，否则那个字段只是装饰。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            wid = _register_worker(client)
            self.assertEqual(_upload_shot(client, _A1, wid).status_code, 200)

            item = next(i for i in client.get(
                "/api/screenshots", params={"batch_name": _BATCH}
            ).json()["items"] if i["asin"] == _A1)
            # base_url 在 TestClient 下是 http://testserver
            path = item["url"].split("testserver", 1)[-1]
            got = client.get(path)
            self.assertEqual(got.status_code, 200, got.text)
            self.assertEqual(got.headers["content-type"], "image/png")
            self.assertEqual(got.content, _PNG)

    def test_status_filter(self):
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            wid = _register_worker(client)
            self.assertEqual(_upload_shot(client, _A1, wid).status_code, 200)

            done = client.get("/api/screenshots",
                              params={"batch_name": _BATCH, "status": "done"}).json()
            self.assertEqual([i["asin"] for i in done["items"]], [_A1])
            # progress 是**整批**的，不受过滤影响 —— 一次请求同时回答
            # 「好了几张」和「这一页是哪几张」
            self.assertEqual(done["progress"]["total"], 2)

    def test_asin_filter(self):
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            r = client.get("/api/screenshots",
                           params={"batch_name": _BATCH, "asin": _A2})
            self.assertEqual([i["asin"] for i in r.json()["items"]], [_A2])

    def test_batch_id_works_as_well_as_batch_name(self):
        with isolated_server() as (client, _ctx):
            bid = _make_batch(client)
            r = client.get("/api/screenshots", params={"batch_id": bid})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["batch_name"], _BATCH)
            self.assertEqual([i["asin"] for i in r.json()["items"]], [_A1, _A2])

    def test_cursor_pagination_walks_the_whole_batch_without_repeats(self):
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            page1 = client.get("/api/screenshots",
                               params={"batch_name": _BATCH, "limit": 1}).json()
            self.assertEqual([i["asin"] for i in page1["items"]], [_A1])
            self.assertEqual(page1["next_cursor"], _A1)

            page2 = client.get("/api/screenshots",
                               params={"batch_name": _BATCH, "limit": 1,
                                       "cursor": page1["next_cursor"]}).json()
            self.assertEqual([i["asin"] for i in page2["items"]], [_A2])

            page3 = client.get("/api/screenshots",
                               params={"batch_name": _BATCH, "limit": 1,
                                       "cursor": page2["next_cursor"]}).json()
            self.assertEqual(page3["items"], [])

    def test_last_partial_page_has_no_next_cursor(self):
        """装不满就必然到底了。还给 cursor 会让调用方多打一次空请求。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            r = client.get("/api/screenshots",
                           params={"batch_name": _BATCH, "limit": 50}).json()
            self.assertEqual(len(r["items"]), 2)
            self.assertIsNone(r["next_cursor"])

    def test_needs_either_batch_name_or_batch_id(self):
        with isolated_server() as (client, _ctx):
            self.assertEqual(client.get("/api/screenshots").status_code, 400)

    def test_unknown_batch_is_404(self):
        with isolated_server() as (client, _ctx):
            r = client.get("/api/screenshots", params={"batch_name": "nope"})
            self.assertEqual(r.status_code, 404, r.text)

    def test_illegal_status_is_400(self):
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            r = client.get("/api/screenshots",
                           params={"batch_name": _BATCH, "status": "kinda-done"})
            self.assertEqual(r.status_code, 400, r.text)

    def test_batch_without_screenshots_lists_nothing(self):
        """没开截图的批次不该凭空长出截图行。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client, screenshot=False, name="shot_off")
            r = client.get("/api/screenshots", params={"batch_name": "shot_off"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["items"], [])
            self.assertEqual(r.json()["progress"]["total"], 0)


class ScreenshotFetchStatusCodesTests(unittest.TestCase):
    """**本文件的重点。** 四种"取不到"必须是四个不同的状态码。

    合并任意两个，调用方就失去了「该不该重试」的判据，而服务端一切正常。
    """

    def test_done_returns_the_png(self):
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            wid = _register_worker(client)
            self.assertEqual(_upload_shot(client, _A1, wid).status_code, 200)

            r = client.get(f"/api/screenshots/{_BATCH}/{_A1}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.headers["content-type"], "image/png")
            self.assertEqual(r.content, _PNG)

    def test_pending_is_409_with_retry_after_not_404(self):
        """还没截好 = **回头再来**。回 404 的话调用方分不清「等」和「别等」。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            r = client.get(f"/api/screenshots/{_BATCH}/{_A1}")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual(r.json()["detail"]["error"], "screenshot_pending")
            self.assertEqual(r.json()["detail"]["status"], "pending")
            self.assertIn("retry-after", {k.lower() for k in r.headers},
                          "409 要带 Retry-After，否则调用方只能自己猜轮询间隔")

    def test_failed_is_410_not_409(self):
        """截失败了 = **不会再有**。回 409 会让调用方永远轮询下去。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            self.assertEqual(_fail_shot(client, _A1).status_code, 200)

            r = client.get(f"/api/screenshots/{_BATCH}/{_A1}")
            self.assertEqual(r.status_code, 410, r.text)
            detail = r.json()["detail"]
            self.assertEqual(detail["error"], "screenshot_failed")
            self.assertEqual(detail["error_detail"], "renderer boom",
                             "失败原因要透出来，否则调用方还得去翻服务器日志")

    def test_asin_with_no_screenshot_row_is_404(self):
        """这个批次里根本没有这个 ASIN 的截图记录。别重试。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            r = client.get(f"/api/screenshots/{_BATCH}/B00NOTHER1")
            self.assertEqual(r.status_code, 404, r.text)

    def test_unknown_batch_is_404(self):
        with isolated_server() as (client, _ctx):
            r = client.get(f"/api/screenshots/nope/{_A1}")
            self.assertEqual(r.status_code, 404, r.text)

    def test_all_four_outcomes_are_distinct(self):
        """把上面四条摆在一起 —— 这才是真正要守的不变量。

        单独看，每条都可能被「统一成 404 更简单」改掉而只红一条；
        摆在一起，任何两者合并都会让这条直接指出「哪两个撞了」。
        """
        with isolated_server() as (client, _ctx):
            _make_batch(client, asins=(_A1, _A2, "B00SHOTA03"))
            wid = _register_worker(client)
            self.assertEqual(_upload_shot(client, _A1, wid).status_code, 200)
            self.assertEqual(_fail_shot(client, _A2).status_code, 200)

            codes = {
                "done": client.get(f"/api/screenshots/{_BATCH}/{_A1}").status_code,
                "failed": client.get(f"/api/screenshots/{_BATCH}/{_A2}").status_code,
                "pending": client.get(f"/api/screenshots/{_BATCH}/B00SHOTA03").status_code,
                "absent": client.get(f"/api/screenshots/{_BATCH}/B00NOTHER1").status_code,
            }
            self.assertEqual(
                len(set(codes.values())), 4,
                f"四种结局必须是四个不同的状态码，实际 {codes}")
            self.assertEqual(codes, {"done": 200, "failed": 410,
                                     "pending": 409, "absent": 404})


class ScreenshotFetchInputTests(unittest.TestCase):

    def test_png_suffix_is_optional(self):
        """列表给的 url 不带后缀，但调用方常把文件名整个抄过来。两种都收。"""
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            wid = _register_worker(client)
            self.assertEqual(_upload_shot(client, _A1, wid).status_code, 200)

            bare = client.get(f"/api/screenshots/{_BATCH}/{_A1}")
            suffixed = client.get(f"/api/screenshots/{_BATCH}/{_A1}.png")
            self.assertEqual(bare.status_code, 200, bare.text)
            self.assertEqual(suffixed.status_code, 200, suffixed.text)
            self.assertEqual(bare.content, suffixed.content)

    def test_path_traversal_in_batch_name_is_rejected(self):
        """``batch_name`` 直接拼进磁盘路径。

        写入侧（``POST /api/tasks/screenshot``）校验的是**当时**那个值，
        这里收到的是**另一个**请求里的值 —— 两处都得校验，不是重复。
        """
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            # 路由层就会把带 %2F 的段拆开，这里主要挡的是 '..' 这类
            r = client.get(f"/api/screenshots/..%2F..%2Fetc/{_A1}")
            self.assertIn(r.status_code, (400, 404), r.text)

    def test_illegal_asin_is_400(self):
        with isolated_server() as (client, _ctx):
            _make_batch(client)
            r = client.get(f"/api/screenshots/{_BATCH}/..")
            self.assertIn(r.status_code, (400, 404), r.text)

    def test_done_in_db_but_file_gone_is_404(self):
        """库说 done、盘上没有（批次删除半途失败之类）—— 对调用方就是没有。

        不能 500：这不是服务端错误，而且 500 会诱发调用方重试一个
        永远不会回来的文件。
        """
        import os

        from common import config

        with isolated_server() as (client, _ctx):
            _make_batch(client)
            wid = _register_worker(client)
            self.assertEqual(_upload_shot(client, _A1, wid).status_code, 200)

            os.remove(os.path.join(config.SCREENSHOT_DIR, _BATCH, f"{_A1}.png"))
            r = client.get(f"/api/screenshots/{_BATCH}/{_A1}")
            self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
