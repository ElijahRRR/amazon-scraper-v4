"""common/core/asindata.py —— 采集结果的判定 / 比较 / hash / 列清单（唯一真源）。

这批东西决定**写进库的是什么**，两个存储后端分叉一个字段就是两份数据：

* ``_is_parse_failure`` / ``_normalize_screenshot_path`` / ``_NA_VALUES`` ——
  server_reject 语义，**含有意保留的 bug**，必须逐字共享。
* 四个比较器 —— 变动检测（asin_changes 的 up/down/changed）。
* ``_HASH_FIELDS`` / ``_compute_content_hash`` / ``_compute_title_bullets_hash`` ——
  md5 over ``"|".join(...)``：字段表分叉 = 每一条 hash 变、每一条 asin_changes 变。
* ``ASIN_DATA_FIELDS`` / ``_ASIN_DATA_COLUMN_SET`` —— 驱动 ``_save_result_inner_unlocked``
  的动态列清单与 ``iter_results`` 的投影白名单。
"""
import hashlib
import re
from typing import Any, Optional

# ==================== 变动对比辅助函数 ====================

_NA_VALUES = {"", "N/A", "n/a", "None", "none", None}


def _normalize_screenshot_path(path: Any) -> Optional[str]:
    """将无效占位值统一视为缺失截图路径。"""
    if path is None:
        return None
    value = str(path).strip()
    if not value or value.lower() in {"none", "null"}:
        return None
    return value


def _is_parse_failure(data: dict) -> bool:
    """检测采集结果是否为解析失败（真正的空壳数据才算失败）
    v3: 如果有有效标题和品牌，即使价格为N/A也不算失败（可能是变体/NFO页面）
    """
    # 有有效标题和品牌的数据不是解析失败
    title = data.get("title", "")
    brand = data.get("brand", "")
    has_valid_info = (title and title not in _NA_VALUES and not title.startswith("[")
                      and brand and brand not in _NA_VALUES)
    if has_valid_info:
        return False

    key_fields = ["current_price", "buybox_price", "stock_count", "stock_status", "brand"]
    all_empty = all(data.get(f) in _NA_VALUES for f in key_fields)
    return all_empty


def _parse_price_float(s) -> Optional[float]:
    if not s:
        return None
    s = str(s).strip().replace(",", "")
    s = re.sub(r'^[^\d.-]+', '', s)
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _compare_price(old_str, new_str) -> Optional[str]:
    old_val = _parse_price_float(old_str)
    new_val = _parse_price_float(new_str)
    if old_val is None or new_val is None:
        return None
    if new_val > old_val:
        return "up"
    elif new_val < old_val:
        return "down"
    return None


def _compare_stock_qty(old_str, new_str) -> Optional[str]:
    def parse_int(s):
        if not s:
            return None
        s = str(s).strip().replace(",", "")
        m = re.search(r'(\d+)', s)
        return int(m.group(1)) if m else None
    old_val = parse_int(old_str)
    new_val = parse_int(new_str)
    if old_val is None or new_val is None:
        return None
    if new_val > old_val:
        return "up"
    elif new_val < old_val:
        return "down"
    return None


def _compare_stock_status(old_str, new_str) -> Optional[str]:
    def normalize(s):
        v = str(s or "").strip().lower()
        return None if v in ("", "n/a", "none") else v
    old_n = normalize(old_str)
    new_n = normalize(new_str)
    if old_n is None or new_n is None:
        return None
    return "changed" if old_n != new_n else None


# 内容 hash 字段（排除价格/库存等高波动字段）
_HASH_FIELDS = [
    "title", "brand", "product_type", "manufacturer", "model_number",
    "part_number", "country_of_origin", "is_customized", "best_sellers_rank",
    "bullet_points", "long_description", "image_urls",
    "upc_list", "ean_list", "parent_asin", "variation_asins",
    "root_category_id", "category_ids", "category_tree",
    "first_available_date", "package_dimensions", "package_weight",
    "item_dimensions", "item_weight",
]

# 标题/五点描述 hash 字段
_TITLE_BULLETS_FIELDS = ["title", "bullet_points"]


def _compute_content_hash(data: dict) -> str:
    parts = [str(data.get(f, "") or "") for f in _HASH_FIELDS]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _compute_title_bullets_hash(data: dict) -> str:
    parts = [str(data.get(f, "") or "") for f in _TITLE_BULLETS_FIELDS]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


# ASIN 数据字段列表（对应 asin_data 表列）
ASIN_DATA_FIELDS = [
    "asin", "title", "brand", "product_type", "manufacturer", "model_number",
    "part_number", "country_of_origin", "is_customized", "best_sellers_rank",
    "original_price", "current_price", "buybox_price", "buybox_shipping",
    "is_fba", "stock_count", "stock_status", "delivery_date", "delivery_time",
    "image_urls", "bullet_points", "long_description", "upc_list", "ean_list",
    "parent_asin", "variation_asins", "variant_attributes",
    "root_category_id", "category_ids",
    "category_tree", "first_available_date", "package_dimensions",
    "package_weight", "item_dimensions", "item_weight", "product_url",
    "site", "zip_code", "crawl_time", "screenshot_path",
    "content_hash", "title_bullets_hash",
    # 评分 + 卖家信息（v3 后期新增）
    "rating", "review_count", "seller_id", "seller_name",
    # 副标题（2026-08 Amazon 把标题拆成两段，后半段在
    # div.dp-title-differentiators 里；worker/parser.py:_title_differentiator）。
    # ⚠ 顺序必须与两份 DDL 一致 —— 本清单与 DDL 的列序由
    # tests/test_asin_data_field_table_guard.py 逐位比对。新列只能落在末尾，
    # 理由见 DDL 里的注释（老库靠 ALTER 追加）。
    "subtitle",
]

# asin_data 合法列名集合（含内部列）：iter_results 收窄投影时用作白名单，
# 防止调用方传入的列名拼进 SQL 造成注入或引用不存在的列。
_ASIN_DATA_COLUMN_SET = frozenset(ASIN_DATA_FIELDS) | {"id", "updated_at", "created_at"}


__all__ = [
    "_NA_VALUES",
    "_normalize_screenshot_path",
    "_is_parse_failure",
    "_parse_price_float",
    "_compare_price",
    "_compare_stock_qty",
    "_compare_stock_status",
    "_HASH_FIELDS",
    "_TITLE_BULLETS_FIELDS",
    "_compute_content_hash",
    "_compute_title_bullets_hash",
    "ASIN_DATA_FIELDS",
    "_ASIN_DATA_COLUMN_SET",
]
