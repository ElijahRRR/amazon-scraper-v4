"""``GET /api/batches/{batch_id}/failures`` 的契约钉子。

``/api/batches/{batch_name}/errors``（旧接口）已经删除——它硬截断到 200 条、
排序也不带 tiebreaker，`/failures` 才是唯一还在维护的失败明细端点。这里钉住
它对外承诺的两个关键点，防止将来有人手滑把上限调小或把过滤参数删掉。

写成 ``unittest.TestCase``：``unittest discover`` 只认 TestCase 子类。
断言与后端无关（纯静态签名检查），两个后端跑出来一样。
"""
from __future__ import annotations

import inspect
import unittest


class BatchFailuresEndpointContractTests(unittest.TestCase):

    def test_limit_ceiling_stays_far_above_the_old_200_cap(self):
        """/failures 存在的意义就是不再硬截断到 200 条，limit 上限必须远大于它。"""
        from server.api.batches import api_batch_failures

        params = inspect.signature(api_batch_failures).parameters
        # pydantic v2 下 `Query(..., le=N)` 的上限落在 `.metadata` 里的 `Le(le=N)`
        ceilings = [m.le for m in params["limit"].default.metadata
                    if hasattr(m, "le")]
        self.assertEqual(ceilings, [100000],
                          "/failures 的 limit 上限被改小了，等于变相恢复了旧接口的截断问题")

    def test_supports_error_type_filter(self):
        """error_type 过滤是 /failures 相对旧接口的另一个关键能力，不能丢。"""
        from server.api.batches import api_batch_failures

        params = inspect.signature(api_batch_failures).parameters
        self.assertIn("error_type", params)


if __name__ == "__main__":
    unittest.main()
