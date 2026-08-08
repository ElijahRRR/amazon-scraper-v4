"""common/dbfactory.py —— 存储后端开关。

    DB_BACKEND=postgres（默认，未设置时也是它）-> common.pgdb.Database
    DB_BACKEND=sqlite                          -> common.database.Database

用法：

    from common.dbfactory import create_database
    db = create_database()
    await db.connect()

**迁移已完成，PostgreSQL 是正式后端。** 默认值因此从 sqlite 改成了 postgres：
不配 ``DB_BACKEND`` 的部署应该落在正式后端上，而不是悄悄退回一个已经退场的
存储实现——那种"默认值和事实不一致"正是最难查的一类事故。

SQLite 那条路径**暂时保留**作为回滚兜底（切换刚完成，先别把退路拆了），
但它不再是默认、也不再是新部署的选项。等生产稳定后整条删除，届时
``common/database.py``、本文件的分支、以及 ``tests/pgdb`` 里那批拿 SQLite
当参照实现的等价性用例会一起退役。

设计约束：
* pgdb 是**惰性** import 的；同样，选 postgres 时不会 import
  ``common.database``，反之亦然。哪条路径都不用为另一条付出 import 代价。
* 两个后端共享的纯 Python 符号（LOCK_STATS / ASIN_DATA_FIELDS / 比较器等）
  真源在 ``common/core/``——两边都从那里再导出，分叉一份副本就等于制造两个
  真源。见 common/core/__init__.py 与 common/pgdb/_shared.py 的说明。
  （Phase 4.1 之前 pgdb 是 import common.database 来拿这些；现在**这条**依赖没了。
  pgdb 仍会在导入期 import common.database，但只为 common/pgdb/__init__.py 的
  ``_assert_nothing_unregistered`` 做双向公开面比对——那是有意的契约断言。）
"""
from __future__ import annotations

import os
from typing import Any, Optional

SQLITE = "sqlite"
POSTGRES = "postgres"
_ALIASES = {
    "sqlite": SQLITE, "sqlite3": SQLITE, "": SQLITE,
    "postgres": POSTGRES, "postgresql": POSTGRES, "pg": POSTGRES,
}


def get_backend() -> str:
    """当前后端名（每次读环境变量，测试可以随时改）。"""
    raw = (os.environ.get("DB_BACKEND") or POSTGRES).strip().lower()
    try:
        return _ALIASES[raw]
    except KeyError:
        raise ValueError(
            f"DB_BACKEND={raw!r} 不认识，只支持 {sorted(set(_ALIASES.values()))}")


def is_postgres() -> bool:
    return get_backend() == POSTGRES


def get_database_class() -> type:
    """返回当前后端的 Database **类**。

    tests/golden/harness.py 需要它：那里是按**类属性**给 start_maintenance /
    run_startup_optimize 打 no-op 补丁的，打错类等于没打。
    """
    if get_backend() == POSTGRES:
        from common.pgdb import Database as PgDatabase
        return PgDatabase
    from common.database import Database as SqliteDatabase
    return SqliteDatabase


def create_database(db_path: Optional[str] = None) -> Any:
    """按 DB_BACKEND 构造 Database（未连接）。

    db_path 只对 SQLite 有意义；PG 后端接受它只是为了签名兼容，DSN 来自
    ``PG_DSN`` 环境变量 / config.PG_DSN。
    """
    cls = get_database_class()
    return cls(db_path) if db_path is not None else cls()
