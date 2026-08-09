"""``POST /api/batches`` —— JSON 推送采集任务。

------------------------------------------------------------------------
这份文件钉的是什么
------------------------------------------------------------------------
在这之前，把任务推给采集器**只有** ``POST /api/upload`` 一条路，而它收的是
multipart 文件。上游系统手里本来就是一个 ASIN 列表，为了调它得先拼一个
xlsx/csv 再上传。``POST /api/batches`` 是同一件事的 JSON 入口。

关键点是「同一件事」——两个端点共用 ``_create_batch_with_tasks``，所以撞名
409 的响应体、回调注册、回显读回值这些性质**必须**在两条路上完全一致。
本文件里 :class:`ParityWithUploadTests` 就是钉这个的：它不是在测 JSON 端点
「能用」，而是测它和文件端点「**一样**」。这两者不是一回事 —— 抄一份实现出来
也能让「能用」的用例全绿，但撞名语义漂了之后**两边都不报错**。

另外两个类分别钉用户明确要的两个「自由选择」：
  * :class:`ZipCodeChoiceTests`   —— 邮编三档（逐 ASIN / 整批 / 服务端默认）
  * :class:`ScreenshotChoiceTests` —— 截图开关，不传即不截

写成 ``unittest.TestCase``：门禁里 ``python -m unittest discover -s tests``
的加载器只认 TestCase 子类，裸函数会被**静默跳过**。跟着 ``DB_BACKEND`` 走
``isolated_server``，四道 runner 门（pytest × 2 后端、unittest × 2 后端）
全都盖得到。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server

#: 公网 IP 字面量：``_is_safe_callback_url`` 对 IP 字面量**不做 DNS 解析**，
#: 用例因此不碰网络、结果不随 DNS 抖动。用域名会走 ``loop.getaddrinfo``。
_CB = "http://8.8.8.8/hook"


def _submit(client, **body):
    return client.post("/api/batches", json=body)


def _upload(client, asins, **data):
    """文件版，用来做对照。"""
    return client.post(
        "/api/upload",
        files={"file": ("x.txt", "\n".join(asins).encode(), "text/plain")},
        data={k: str(v) for k, v in data.items()},
    )


class JsonSubmitBasicsTests(unittest.TestCase):

    def test_plain_asin_list_creates_a_batch(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00JSON001", "B00JSON002"],
                        batch_name="json_basic")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["batch_name"], "json_basic")
            self.assertEqual(body["total_asins"], 2)
            self.assertEqual(body["inserted"], 2)
            self.assertTrue(body["status_url"].endswith(
                "/api/batches/json_basic/status"), body["status_url"])

            # 真进了库，不是只回了个 200
            st = client.get("/api/batches/json_basic/status")
            self.assertEqual(st.status_code, 200, st.text)
            self.assertEqual(st.json()["stats"]["total"], 2)

    def test_asins_are_deduped_in_order(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00JSON002", "B00JSON001", "B00JSON002"],
                        batch_name="json_dedup")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total_asins"], 2)

    def test_items_and_asins_can_be_mixed(self):
        """两个键同时给 = 合并。调用方混着写不该是个错误。"""
        with isolated_server() as (client, _ctx):
            r = _submit(client,
                        asins=["B00JSON001"],
                        items=[{"asin": "B00JSON002", "zip_code": "10001"},
                               "B00JSON003"],
                        batch_name="json_mixed")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total_asins"], 3)
            self.assertEqual(r.json()["per_asin_zip_count"], 1)

    def test_no_valid_asin_is_400(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["", "not-an-asin", None],
                        batch_name="json_empty")
            self.assertEqual(r.status_code, 400, r.text)

    def test_malformed_json_is_400_not_500(self):
        """坏请求体要回 400。500 会让调用方以为是服务端挂了而去重试。"""
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", content=b"{not json",
                            headers={"content-type": "application/json"})
            self.assertEqual(r.status_code, 400, r.text)

    def test_json_array_body_is_400(self):
        """顶层必须是对象。传数组是个常见手滑，要明确拒绝而不是 500。"""
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json=["B00JSON001"])
            self.assertEqual(r.status_code, 400, r.text)

    def test_batch_name_that_would_escape_the_screenshot_dir_is_400(self):
        """批次名会拼成截图目录名 —— 穿越串必须在**建批次之前**挡掉。

        挡晚了的后果不是安全问题（写入侧另有校验），而是批次建好了、
        截图却永远落不了盘，一个只在开了截图时才暴露的静默半残批次。
        """
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00JSON001"], batch_name="../evil")
            self.assertEqual(r.status_code, 400, r.text)


class ZipCodeChoiceTests(unittest.TestCase):
    """邮编三档，互相独立。"""

    def test_omitting_zip_code_falls_back_to_the_server_default(self):
        with isolated_server() as (client, _ctx):
            default = client.get("/api/settings").json()["settings"]["zip_code"]
            r = _submit(client, asins=["B00ZIP0001"], batch_name="zip_default")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["per_asin_zip_count"], 0)

            # 任务上落的就是服务端默认邮编
            rows = client.get("/api/batches").json()["batches"]
            bid = next(b["id"] for b in rows if b["name"] == "zip_default")
            self.assertEqual(_task_zips(client)[(bid, "B00ZIP0001")], default)

    def test_null_zip_code_means_the_same_as_omitting_it(self):
        """显式 ``null`` 与不传同义 —— 上游序列化时把缺省写成 null 太常见了。"""
        with isolated_server() as (client, _ctx):
            default = client.get("/api/settings").json()["settings"]["zip_code"]
            r = _submit(client, asins=["B00ZIP0001"], zip_code=None,
                        batch_name="zip_null")
            self.assertEqual(r.status_code, 200, r.text)
            rows = client.get("/api/batches").json()["batches"]
            bid = next(b["id"] for b in rows if b["name"] == "zip_null")
            self.assertEqual(_task_zips(client)[(bid, "B00ZIP0001")], default)

    def test_batch_level_zip_code_applies_to_every_asin(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00ZIP0001", "B00ZIP0002"],
                        zip_code="90001", batch_name="zip_batch")
            self.assertEqual(r.status_code, 200, r.text)
            rows = client.get("/api/batches").json()["batches"]
            bid = next(b["id"] for b in rows if b["name"] == "zip_batch")
            zips = _task_zips(client)
            self.assertEqual(zips[(bid, "B00ZIP0001")], "90001")
            self.assertEqual(zips[(bid, "B00ZIP0002")], "90001")

    def test_per_asin_zip_beats_the_batch_level_one(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client,
                        items=[{"asin": "B00ZIP0001", "zip_code": "10001"},
                               {"asin": "B00ZIP0002"}],
                        zip_code="90001", batch_name="zip_per_asin")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["per_asin_zip_count"], 1)
            rows = client.get("/api/batches").json()["batches"]
            bid = next(b["id"] for b in rows if b["name"] == "zip_per_asin")
            zips = _task_zips(client)
            self.assertEqual(zips[(bid, "B00ZIP0001")], "10001")
            self.assertEqual(zips[(bid, "B00ZIP0002")], "90001")

    def test_illegal_batch_level_zip_is_400(self):
        """整批邮编写错了必须当场失败——它影响每一条任务，静默回退到默认
        等于让调用方拿到一整批**采错邮编**的价格，而且没有任何迹象。"""
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00ZIP0001"], zip_code="abcde",
                        batch_name="zip_bad")
            self.assertEqual(r.status_code, 400, r.text)

    def test_illegal_per_asin_zip_is_counted_not_fatal(self):
        """逐 ASIN 邮编写错了只影响那一个，退回批次邮编 + 计数上报。

        这与上一条的不对称是有意的，也与 ``/api/upload`` 处理 B 列的方式一致：
        一列几万行里有一格脏数据就整批 400，比退回默认更难用。
        """
        with isolated_server() as (client, _ctx):
            r = _submit(client,
                        items=[{"asin": "B00ZIP0001", "zip_code": "nope"}],
                        zip_code="90001", batch_name="zip_bad_item")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["invalid_zip_rows"], 1)
            self.assertEqual(r.json()["per_asin_zip_count"], 0)
            rows = client.get("/api/batches").json()["batches"]
            bid = next(b["id"] for b in rows if b["name"] == "zip_bad_item")
            self.assertEqual(_task_zips(client)[(bid, "B00ZIP0001")], "90001")


class ScreenshotChoiceTests(unittest.TestCase):

    def test_screenshot_is_off_by_default(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00SHOT001"], batch_name="ss_off")
            self.assertEqual(r.status_code, 200, r.text)
            prog = client.get("/api/batches/ss_off/screenshots/progress").json()
            self.assertEqual(prog["total"], 0,
                             "没要截图却排了截图任务")

    def test_screenshot_can_be_turned_on(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00SHOT001", "B00SHOT002"],
                        needs_screenshot=True, batch_name="ss_on")
            self.assertEqual(r.status_code, 200, r.text)
            prog = client.get("/api/batches/ss_on/screenshots/progress").json()
            self.assertEqual(prog["total"], 2)
            self.assertEqual(prog["pending"], 2)


class ParityWithUploadTests(unittest.TestCase):
    """JSON 端点与文件端点必须是**同一件事**，不只是各自能用。

    这几条针对的是「抄一份实现」这种改法：抄出来之后上面所有用例照样绿，
    只有下面这种跨端点的对照能看出漂移。
    """

    def test_same_response_shape_as_upload(self):
        with isolated_server() as (client, _ctx):
            a = _upload(client, ["B00PAR0001"], batch_name="par_file",
                        zip_code="10001", needs_screenshot="false")
            b = _submit(client, asins=["B00PAR0001"], batch_name="par_json",
                        zip_code="10001", needs_screenshot=False)
            self.assertEqual(a.status_code, 200, a.text)
            self.assertEqual(b.status_code, 200, b.text)
            self.assertEqual(sorted(a.json()), sorted(b.json()),
                             "两个端点的响应键集合漂了 —— 调用方没法照着一份文档写")

    def test_duplicate_name_is_409_here_too(self):
        with isolated_server() as (client, _ctx):
            self.assertEqual(
                _submit(client, asins=["B00PAR0001"],
                        batch_name="par_dup").status_code, 200)
            r = _submit(client, asins=["B00PAR0002"], batch_name="par_dup")
            self.assertEqual(r.status_code, 409, r.text)
            detail = r.json()["detail"]
            self.assertEqual(detail["error"], "batch_name_conflict")
            self.assertEqual(detail["batch_name"], "par_dup")
            self.assertTrue(detail["status_url"].endswith(
                "/api/batches/par_dup/status"))

    def test_a_json_submit_conflicts_with_a_file_upload_of_the_same_name(self):
        """撞名是**跨端点**的。两条路各建各的批次表就等于 409 形同虚设。"""
        with isolated_server() as (client, _ctx):
            self.assertEqual(
                _upload(client, ["B00PAR0001"], batch_name="par_cross",
                        zip_code="10001").status_code, 200)
            r = _submit(client, asins=["B00PAR0002"], batch_name="par_cross")
            self.assertEqual(r.status_code, 409, r.text)

    def test_callback_url_is_validated_and_echoed_from_storage(self):
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00PAR0001"], batch_name="par_cb",
                        callback_url=_CB, external_id="ext-json")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["callback_url"], _CB)
            self.assertEqual(r.json()["external_id"], "ext-json")

    def test_illegal_callback_url_is_rejected_here_too(self):
        """SSRF 防线不能只挡文件上传那条路。"""
        with isolated_server() as (client, _ctx):
            r = _submit(client, asins=["B00PAR0001"], batch_name="par_ssrf",
                        callback_url="http://127.0.0.1:8899/steal")
            self.assertEqual(r.status_code, 400, r.text)

    def test_both_endpoints_go_through_the_one_shared_core(self):
        """静态守卫：两个端点都必须调 ``_create_batch_with_tasks``。

        上面几条是行为对照，会在漂了之后才红；这一条在**源码层**钉住共享，
        让「抄一份」这个改法当场失败，而不是等某个语义先漂掉。
        """
        import inspect

        from server.api import batches as B

        for fn in (B.api_upload, B.api_create_batch):
            src = inspect.getsource(fn)
            self.assertIn(
                "_create_batch_with_tasks", src,
                f"{fn.__name__} 没走共享核心 —— 撞名 409 的语义会在两条路上分叉")


#: 每个 zip 用例只建一个批次、任务个位数，一次 pull 就能全拿到。
_PULL_COUNT = 50


def _task_zips(client):
    """一次拉完，返回 ``{(batch_id, asin): zip_code}``。全程走 HTTP。

    为什么不直接问 db：PG 连接池绑在 TestClient 那个事件循环上，用例另起一个
    loop 去 ``srv.db.read()`` 会跨循环借连接。而 worker 拉任务本来就要这个
    字段，``/api/tasks/pull`` 直接回 ``zip_code`` —— 用它既不碰内部状态，
    验的还正好是**采集侧真正会看到的那个值**。

    ⚠ **必须一次拉完再查**。``pull_tasks`` 会给拉走的任务上租约，第二次调用
    拿到的是空列表 —— 每验一个 ASIN 就 pull 一次的写法，第二个 ASIN 起全都
    会误报「任务未入队」。

    这里不归还租约：每个用例都在自己的 ``isolated_server`` 里，进程退出即销毁。
    """
    r = client.get("/api/tasks/pull",
                   params={"worker_id": "zip-probe", "count": _PULL_COUNT})
    assert r.status_code == 200, r.text
    return {(t["batch_id"], t["asin"]): t["zip_code"] for t in r.json()["tasks"]}


if __name__ == "__main__":
    unittest.main()
