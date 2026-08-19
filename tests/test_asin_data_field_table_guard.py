"""``asin_data`` 的字段表守卫（Phase 4.8）—— **列集与列序都是 API 契约**。

------------------------------------------------------------------------
为什么需要它
------------------------------------------------------------------------
同一张 ``asin_data`` 表的列清单，仓库里有**五份**手写副本：

  1. ``common/models.py:AsinData``            —— dataclass，推导 ``EXPORTABLE_FIELDS``
  2. ``common/core/asindata.py:ASIN_DATA_FIELDS`` —— 写入侧动态列清单 + 读投影白名单
  3. ``common/database.py``   的 SQLite DDL
  4. ``common/pgdb/schema.py`` 的 PG DDL
  5. ``common/pgdb/schema.py:EXPECTED_COLUMNS['asin_data']`` —— ``verify_schema()`` 的期望

4.8 之前**没有任何东西**在比对它们。实际的漂移就摆在那儿：``AsinData`` 缺
``title_bullets_hash``（2/3/4/5 都有），于是 ``_INTERNAL_FIELDS`` 里那个
``"title_bullets_hash"`` 在排除一个根本不存在的字段——一条看上去在干活、
实际是死字符串的排除项。

``common/pgdb/schema.py`` 自己写着（:44-46 / :115 一带）：

    「列集与**列序**是 API 契约的一部分」

因为 ``SELECT d.*`` 没有 ``response_model``，列序会**整个泄进 erpAPI 的响应**。

------------------------------------------------------------------------
⚠ 只测集合差集是不够的 —— 这份文件的核心
------------------------------------------------------------------------
``AsinData`` 今天把 ``variant_attributes`` 排在 ``parent_asin`` /
``variation_asins`` **之前**，而 ``ASIN_DATA_FIELDS`` 与两份 DDL 都排在**之后**。
一个纯集合差集的守卫对这个**完全看不见**。

所以本文件的顺序断言落在 ``ASIN_DATA_FIELDS`` 与两份 DDL 之间（外加
``EXPECTED_COLUMNS``）—— 那四者是**真的**在决定 SQL 与响应形状的东西。
``AsinData`` 只参与集合断言，它的顺序**故意不测**：

  * 它不产生任何 SQL，也不决定任何响应的列序；
  * 它唯一的产物 ``EXPORTABLE_FIELDS`` 的顺序**是**契约
    （``/api/export/fields`` 是黄金基线的一步，导出表头按它排），
    把 ``variant_attributes`` 挪回去会当场改掉那一步和所有导出文件的列序。

那处顺序分叉因此是**已登记的既有状态**，不是本文件要修的东西。

------------------------------------------------------------------------
自检（每次改这份守卫都要重做一遍）
------------------------------------------------------------------------
把任一份 DDL 里两列对调 -> 本文件必须红。实测见提交说明。

写成 ``unittest.TestCase``：``python -m unittest discover -s tests`` 的加载器
只认 TestCase 子类，四道 runner 门才都覆盖得到。本文件**不碰数据库**
（纯文本 + 纯 import），两个后端列跑出来完全一样。
"""
from __future__ import annotations

import dataclasses
import os
import re
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _sqlite_alter_added_columns() -> set:
    """SQLite 侧只通过 `ALTER TABLE asin_data ADD COLUMN` 补上的列。

    只认 asin_data 那几段（文件里还有 batches / tasks 的 ALTER 阶梯）。
    """
    with open(os.path.join(_REPO_ROOT, "common", "database.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    cols = set()
    for m in re.finditer(
            r'for col in \[([^\]]*)\]:\s*\n\s*try:\s*\n\s*await self\._db\.execute\('
            r'f"ALTER TABLE asin_data ADD COLUMN', src):
        cols |= {c.strip().strip('"\'') for c in m.group(1).split(",") if c.strip()}
    return cols


def _pg_alter_added_columns() -> set:
    """PG 侧 `DDL_ALTERS` 里对 asin_data 的 ADD COLUMN。"""
    from common.pgdb.schema import DDL_ALTERS
    cols = set()
    for stmt in DDL_ALTERS:
        m = re.search(r'ALTER TABLE asin_data ADD COLUMN IF NOT EXISTS\s+"?(\w+)"?',
                      stmt)
        if m:
            cols.add(m.group(1))
    return cols


# ==========================================================================
# 显式白名单：四份清单之间**允许**存在的全部差异。
# 每一条都要能说出「为什么它只在某几份里」。多一条少一条都要人来改这里。
# ==========================================================================

#: DB 自己维护的三列。不在 ``ASIN_DATA_FIELDS`` 里，因为那是**写入侧**要
#: 逐列 bind 的清单，这三列由 DDL 的默认值 / identity 负责。
_DB_MANAGED = frozenset({"id", "created_at", "updated_at"})

#: 「基线」六列：变动检测的上一次取值快照。只有 DDL 有；写入侧走
#: 独立的 UPDATE（不是 ASIN_DATA_FIELDS 那条动态 INSERT），
#: dataclass 也从来没有过它们。
_BASELINE_COLUMNS = frozenset({
    "baseline_price",
    "baseline_buybox_price",
    "baseline_stock_count",
    "baseline_stock_status",
    "baseline_title_bullets_hash",
    "baseline_updated_at",
})

#: **已登记的既有分叉**：这些列历史上是靠 ALTER 补的，但在两份 DDL 里被写在
#: 表**中间**（``created_at`` / ``updated_at`` 之前）。后果是：一个从很老版本
#: 一路 ALTER 上来的 SQLite 库，物理列序与新建库不同。
#:
#: 这不是本轮引入的，也**不修** —— 修它要重建表、要改
#: ``EXPECTED_COLUMNS``、要重录黄金基线，而收益只对"存在这样一个老库"的
#: 假设成立（PG 生产库是新建的，列序与 EXPECTED_COLUMNS 一致，已实测）。
#: 登记在这里是为了让下面那条守卫**只管住往后新增的列**，同时把这个已知的
#: 历史状态写在明处，而不是让守卫悄悄放行一整类问题。
#:
#: ⚠ 往这个集合里加名字 = 明知故犯。新列请一律追加到 DDL 末尾。
_LEGACY_MID_TABLE_MIGRATIONS = frozenset({
    "variant_attributes",
    "baseline_price", "baseline_buybox_price", "baseline_stock_count",
    "baseline_stock_status", "baseline_title_bullets_hash",
    "baseline_updated_at",
    "rating", "review_count", "seller_id", "seller_name",
})

#: DDL 有、``ASIN_DATA_FIELDS`` 没有的全部列。
_DDL_MINUS_FIELDS = _DB_MANAGED | _BASELINE_COLUMNS

#: DDL 有、``AsinData`` 没有的全部列。
#: ⚠ ``title_bullets_hash`` **不在这里**——4.8 之前它在，那正是被修掉的漂移。
_DDL_MINUS_DATACLASS = _BASELINE_COLUMNS

#: ``AsinData`` 有、``ASIN_DATA_FIELDS`` 没有的全部列。
_DATACLASS_MINUS_FIELDS = _DB_MANAGED


# ==========================================================================
# 从源码里抽列名（不连库：两个后端列都要能跑，且要能抓住「DDL 改了但库还没建」）
# ==========================================================================

_CONSTRAINT_KEYWORDS = (
    "primary", "unique", "foreign", "constraint", "check", "exclude", "like",
)


def _columns_from_create_table(sql: str, table: str) -> list:
    """从一段 ``CREATE TABLE ... (...)`` SQL 文本里按出现顺序抽列名。"""
    m = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?%s\s*\(" % re.escape(table),
        sql, re.IGNORECASE)
    assert m, "在给定 SQL 里找不到 CREATE TABLE %s" % table

    # 从左括号起做括号配平，取出表体（列定义里有 text COLLATE "C" 之类，
    # 但没有嵌套括号以外的陷阱；配平比 [^)]* 稳）。
    i = m.end() - 1
    depth = 0
    for j in range(i, len(sql)):
        if sql[j] == "(":
            depth += 1
        elif sql[j] == ")":
            depth -= 1
            if depth == 0:
                body = sql[i + 1:j]
                break
    else:                                     # pragma: no cover - 语法坏了才会到
        raise AssertionError("CREATE TABLE %s 的括号没配平" % table)

    cols = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        head = line.split()[0].strip('",')
        if head.lower() in _CONSTRAINT_KEYWORDS:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head):
            continue
        cols.append(head)
    return cols


def _sqlite_ddl_columns() -> list:
    with open(os.path.join(_REPO_ROOT, "common", "database.py"),
              encoding="utf-8") as fh:
        return _columns_from_create_table(fh.read(), "asin_data")


def _pg_ddl_columns() -> list:
    from common.pgdb import schema
    blocks = [d for d in schema.DDL_TABLES
              if re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?asin_data\s*\(",
                           d, re.IGNORECASE)]
    assert len(blocks) == 1, "DDL_TABLES 里 asin_data 的建表语句有 %d 条" % len(blocks)
    return _columns_from_create_table(blocks[0], "asin_data")


def _dataclass_columns() -> list:
    from common.models import AsinData
    return [f.name for f in dataclasses.fields(AsinData)]


def _asin_data_fields() -> list:
    from common.core.asindata import ASIN_DATA_FIELDS
    return list(ASIN_DATA_FIELDS)


class AsinDataFieldTableGuard(unittest.TestCase):

    # ---------------------------------------------------------------- 抽取自检
    def test_extractor_actually_found_columns(self):
        """先证明抽取器没有静默地抽出空列表 —— 否则下面每一条都会假绿。"""
        for name, cols in (("sqlite DDL", _sqlite_ddl_columns()),
                           ("pg DDL", _pg_ddl_columns()),
                           ("AsinData", _dataclass_columns()),
                           ("ASIN_DATA_FIELDS", _asin_data_fields())):
            with self.subTest(source=name):
                self.assertGreater(len(cols), 40, name)
                self.assertEqual(len(cols), len(set(cols)), "%s 有重复列名" % name)
                self.assertIn("asin", cols)
                self.assertIn("title_bullets_hash", cols,
                              "%s 缺 title_bullets_hash" % name)

    # ---------------------------------------------------------------- 列集
    def test_ddl_minus_fields_is_exactly_the_whitelist(self):
        for name, ddl in (("sqlite", _sqlite_ddl_columns()),
                          ("pg", _pg_ddl_columns())):
            with self.subTest(ddl=name):
                fields = set(_asin_data_fields())
                self.assertEqual(set(ddl) - fields, set(_DDL_MINUS_FIELDS),
                                 "%s DDL 与 ASIN_DATA_FIELDS 的差集变了" % name)
                self.assertEqual(fields - set(ddl), set(),
                                 "ASIN_DATA_FIELDS 里有 %s DDL 没有的列 —— "
                                 "写入侧会 bind 一个不存在的列" % name)

    def test_ddl_minus_dataclass_is_exactly_the_whitelist(self):
        for name, ddl in (("sqlite", _sqlite_ddl_columns()),
                          ("pg", _pg_ddl_columns())):
            with self.subTest(ddl=name):
                dc = set(_dataclass_columns())
                self.assertEqual(set(ddl) - dc, set(_DDL_MINUS_DATACLASS),
                                 "%s DDL 与 AsinData 的差集变了" % name)
                self.assertEqual(dc - set(ddl), set(),
                                 "AsinData 里有 %s DDL 没有的字段" % name)

    def test_dataclass_minus_fields_is_exactly_the_whitelist(self):
        dc, fields = set(_dataclass_columns()), set(_asin_data_fields())
        self.assertEqual(dc - fields, set(_DATACLASS_MINUS_FIELDS))
        self.assertEqual(fields - dc, set(),
                         "ASIN_DATA_FIELDS 里有 AsinData 没有的列 —— "
                         "4.8 修掉的正是这一条（title_bullets_hash）")

    # ---------------------------------------------------------------- 列序
    # ⚠ 这一组才是「只测集合差集抓不到」的那部分。
    def test_the_two_ddls_are_column_for_column_identical(self):
        """两份 DDL 必须逐列同名同序（C4：两个后端的响应形状一致）。"""
        self.assertEqual(_sqlite_ddl_columns(), _pg_ddl_columns())

    def test_asin_data_fields_order_matches_both_ddls(self):
        """``ASIN_DATA_FIELDS`` 的顺序 == 两份 DDL 去掉白名单列之后的顺序。

        它驱动 ``iter_results`` 的投影与写入侧的动态列清单；
        与 DDL 换位不会报错，只会让导出的列悄悄换位。
        """
        expected = _asin_data_fields()
        for name, ddl in (("sqlite", _sqlite_ddl_columns()),
                          ("pg", _pg_ddl_columns())):
            with self.subTest(ddl=name):
                self.assertEqual(
                    [c for c in ddl if c not in _DDL_MINUS_FIELDS], expected,
                    "%s DDL 与 ASIN_DATA_FIELDS 的**列序**不一致" % name)

    def test_expected_columns_matches_the_real_ddl(self):
        """``EXPECTED_COLUMNS`` 是 ``verify_schema()`` 的期望，也是第五份副本。

        它比对的是**线上库**的 ``ordinal_position``；本条比对的是**源码里的
        DDL 文本**。两者都要，因为「DDL 改了但库还没重建」时只有本条会响。
        """
        from common.pgdb.schema import EXPECTED_COLUMNS
        self.assertEqual(_pg_ddl_columns(), EXPECTED_COLUMNS["asin_data"])
        self.assertEqual(_sqlite_ddl_columns(), EXPECTED_COLUMNS["asin_data"])

    # ---------------------------------------------------------------- 导出面
    def test_adding_title_bullets_hash_did_not_leak_into_exports(self):
        """4.8 那次补字段**不许**改导出面 —— 它在 ``_INTERNAL_FIELDS`` 里。"""
        from common.models import EXPORTABLE_FIELDS, _INTERNAL_FIELDS
        self.assertIn("title_bullets_hash", _INTERNAL_FIELDS)
        self.assertNotIn("title_bullets_hash", EXPORTABLE_FIELDS)
        # 43 -> 44：2026-08 新增 subtitle，它**是**导出面的字段（不是内部字段）。
        # 这个数字不是形式主义：它逼着"往 AsinData 加字段"这件事必须是一次
        # 有意识的编辑 —— 加错了（比如把本该内部的哈希列加进来）这里当场红。
        self.assertEqual(len(EXPORTABLE_FIELDS), 44)
        self.assertIn("subtitle", EXPORTABLE_FIELDS)
        # subtitle 落在 total_price **之前**：total_price 是 EXPORTABLE_FIELDS
        # 末尾追加的合成列，不是 dataclass 字段。既有列一列没动、没有重排。
        self.assertEqual(EXPORTABLE_FIELDS[-1], "total_price")
        self.assertEqual(EXPORTABLE_FIELDS[-2], "subtitle")

    # ---------------------------------------------------------------- 迁移面
    def test_every_alter_added_column_sits_at_the_tail_of_both_ddls(self):
        """老库靠 ALTER 升级，而 ALTER **只能追加到末尾**。

        所以「只在 ALTER 阶梯里出现的列」必须同时是两份 DDL 的**末尾若干列**，
        否则新建库与升级库的物理列序会分叉 —— 而 `SELECT d.*` 没有
        response_model，列序整个泄进 erpAPI 的响应：同一份代码、两种响应，
        且 `verify_schema` 只能对上其中一种。

        这条守的是**规则**而不是某一个列名：以后再加列，放错位置就当场红。

        ⚠ 排除 ``_LEGACY_MID_TABLE_MIGRATIONS``：那批列历史上就写在表中间，
        是**已登记的既有状态**（见那个集合的注释）。本条只管住往后新增的列。
        """
        migrated = ((_sqlite_alter_added_columns() | _pg_alter_added_columns())
                    - _LEGACY_MID_TABLE_MIGRATIONS)
        self.assertTrue(migrated, "一条 ALTER 迁移都没抽到 —— 抽取逻辑坏了")
        for name, ddl in (("sqlite", _sqlite_ddl_columns()),
                          ("pg", _pg_ddl_columns())):
            in_ddl = [c for c in ddl if c in migrated]
            self.assertTrue(in_ddl, "%s DDL 里一个迁移列都没有" % name)
            tail = ddl[len(ddl) - len(in_ddl):]
            self.assertEqual(
                sorted(tail), sorted(in_ddl),
                "%s DDL 里的迁移列没有全部落在末尾：%r 之后还有别的列。"
                "老库 ALTER 只能追加到末尾，放在中间会让新建库与升级库列序分叉"
                % (name, in_ddl))

    def test_internal_fields_all_exist_on_the_dataclass(self):
        """``_INTERNAL_FIELDS`` 里不许再出现「排除一个不存在的字段」。

        这正是 4.8 逮到的那条缺陷的**通用**版本：一个排除项拼错了，
        或者被排除的字段后来改了名，结果就是它悄悄泄进 ``EXPORTABLE_FIELDS``
        —— 或者像 ``title_bullets_hash`` 那样，看着在干活其实是死字符串。
        """
        from common.models import _INTERNAL_FIELDS
        dc = set(_dataclass_columns())
        self.assertEqual(set(_INTERNAL_FIELDS) - dc, set(),
                         "_INTERNAL_FIELDS 在排除 AsinData 上不存在的字段")


if __name__ == "__main__":
    unittest.main()
