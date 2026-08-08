"""``GET /api/worker/download`` —— Worker 安装包下载。

这个端点的存在理由是补一个**长期的前后端不一致**：``settings.html`` 与
``workers.html`` 里三个"下载 Worker"链接从加进模板那天起就指向一个不存在的
路由，点下去 404。所以本文件的第一条断言就是"前端指到的地方真的有东西"。

写成 ``unittest.TestCase``：``unittest discover`` 只认 TestCase 子类。
断言与存储后端无关（纯打包，不碰库），两个后端跑出来一样。
"""
from __future__ import annotations

import io
import re
import unittest
import zipfile

from tests.golden.harness import isolated_server

_TEMPLATE_LINK_RE = re.compile(r"/api/worker/download")


def _zip_from(client, query=""):
    r = client.get(f"/api/worker/download{query}")
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


class WorkerPackageDownloadTests(unittest.TestCase):

    def test_every_template_link_actually_resolves(self):
        """前端模板里出现的每一个 /api/worker/download 都必须真能下到东西。

        这条是本端点的**存在理由**，放在第一条。以前这三个链接是 404。
        """
        import os

        from common import config

        found = 0
        tpl_dir = config.TEMPLATE_DIR
        for fn in os.listdir(tpl_dir):
            if not fn.endswith(".html"):
                continue
            with open(os.path.join(tpl_dir, fn), encoding="utf-8") as f:
                found += len(_TEMPLATE_LINK_RE.findall(f.read()))
        self.assertGreater(found, 0, "模板里一个下载链接都没有了，这个测试该跟着删")

        with isolated_server() as (client, _ctx):
            for mode in ("", "?mode=full", "?mode=update"):
                r = client.get(f"/api/worker/download{mode}")
                self.assertEqual(r.status_code, 200, f"{mode}: {r.text[:200]}")
                self.assertEqual(r.headers["content-type"], "application/zip")

    def test_full_package_can_actually_run_the_worker(self):
        """完整包必须自带 worker 跑起来所需的一切，缺一件都等于下了个跑不了的包。"""
        with isolated_server() as (client, _ctx):
            zf = _zip_from(client, "?mode=full")
            names = set(zf.namelist())

        # 入口 + 启动脚本 + 依赖清单
        for must in ("run_worker.py", "requirements.txt",
                      "start.sh", "start.bat", "README.txt"):
            self.assertIn(f"worker-package/{must}", names, f"完整包缺 {must}")

        # 两个代码包都在，且 engine 这个真正的入口模块在
        self.assertIn("worker-package/worker/engine.py", names)
        self.assertTrue(any(n.startswith("worker-package/common/") for n in names))

    def test_update_package_is_python_only(self):
        """更新包解压是要直接覆盖到已装好的目录上的 —— 混进 requirements
        或启动脚本就会把用户改过的配置盖掉。"""
        with isolated_server() as (client, _ctx):
            zf = _zip_from(client, "?mode=update")
            names = zf.namelist()

        non_py = [n for n in names if not n.endswith(".py")]
        self.assertEqual(non_py, [], f"更新包混进了非 .py 文件: {non_py[:5]}")
        self.assertIn("worker-package/worker/engine.py", names)

    def test_server_side_and_junk_files_stay_out(self):
        """打包的是 worker，不是整个仓库。

        - ``__pycache__`` / ``.pyc``：体积噪声，且可能与目标机 Python 版本不符；
        - ``common/pgdb/``：服务端专属存储后端，import 期就要 asyncpg，而
          worker 的 requirements 里没有 asyncpg —— 打进去会让"解压即用"变成
          一个 ImportError；
        - ``server/``：模板与服务端配置，worker 一个都用不到。
        """
        with isolated_server() as (client, _ctx):
            names = _zip_from(client, "?mode=full").namelist()

        for n in names:
            self.assertNotIn("__pycache__", n)
            self.assertFalse(n.endswith(".pyc"), n)
            self.assertNotIn("/pgdb/", n, "PG 后端不该进 worker 包（worker 不装 asyncpg）")
            self.assertFalse(n.startswith("worker-package/server/"), n)

    def test_start_script_bakes_in_the_server_url(self):
        """下载的人未必知道 --server 该填什么，所以地址要烘进启动脚本。"""
        with isolated_server() as (client, _ctx):
            zf = _zip_from(client, "?mode=full")
            sh = zf.read("worker-package/start.sh").decode()
            bat = zf.read("worker-package/start.bat").decode()

        self.assertIn("SERVER_URL=", sh)
        self.assertIn("run_worker.py", sh)
        self.assertIn("SERVER_URL=", bat)
        # .bat 必须是 CRLF，否则部分 Windows cmd.exe 解析会出错
        self.assertIn("\r\n", bat)

    def test_start_sh_is_executable(self):
        """start.sh 没有可执行位的话，UI 说的"双击即可"就是假的。"""
        with isolated_server() as (client, _ctx):
            zf = _zip_from(client, "?mode=full")
            info = next(i for i in zf.filelist if i.filename.endswith("start.sh"))

        mode = info.external_attr >> 16
        self.assertTrue(mode & 0o111, f"start.sh 缺可执行位: {oct(mode)}")

    def test_bad_mode_is_rejected(self):
        with isolated_server() as (client, _ctx):
            r = client.get("/api/worker/download?mode=nope")
            self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
