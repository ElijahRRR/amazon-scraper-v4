"""
Amazon ASIN 采集系统 v4 - 数据模型
"""
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime


@dataclass
class Task:
    id: int = 0
    batch_id: int = 0
    asin: str = ""
    zip_code: str = "10001"
    status: str = "pending"  # pending / processing / done / failed
    priority: int = 0
    needs_screenshot: bool = False
    worker_id: str = ""
    retry_count: int = 0
    error_type: str = ""
    error_detail: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AsinData:
    """当前 ASIN 数据（主表，每个 ASIN 一行）"""
    id: int = 0
    asin: str = ""
    title: str = ""
    brand: str = ""
    product_type: str = ""
    manufacturer: str = ""
    model_number: str = ""
    part_number: str = ""
    country_of_origin: str = ""
    is_customized: str = ""
    best_sellers_rank: str = ""
    original_price: str = ""
    current_price: str = ""
    buybox_price: str = ""
    buybox_shipping: str = ""
    is_fba: str = ""
    stock_count: str = ""
    stock_status: str = ""
    delivery_date: str = ""
    delivery_time: str = ""
    image_urls: str = ""
    bullet_points: str = ""
    long_description: str = ""
    upc_list: str = ""
    ean_list: str = ""
    variant_attributes: str = ""
    parent_asin: str = ""
    variation_asins: str = ""
    root_category_id: str = ""
    category_ids: str = ""
    category_tree: str = ""
    first_available_date: str = ""
    package_dimensions: str = ""
    package_weight: str = ""
    item_dimensions: str = ""
    item_weight: str = ""
    product_url: str = ""
    site: str = "amazon.com"
    zip_code: str = "10001"
    crawl_time: str = ""
    screenshot_path: str = ""
    content_hash: str = ""
    # P4.8：这一行以前**不存在**，而 `common/core/asindata.py:ASIN_DATA_FIELDS`
    # 和两份 DDL（`common/database.py` / `common/pgdb/schema.py`）都有它。
    # 后果是下面 `_INTERNAL_FIELDS` 里的 "title_bullets_hash" 在排除一个**根本
    # 不存在的 dataclass 字段** —— 一条看上去在干活、实际是死字符串的排除项。
    # 补它是安全的（已实测，不是推断）：`AsinData` 除本文件外**零引用**
    # （`grep -rn AsinData --include=*.py` 只有 class 定义与下面那行推导），
    # 唯一用途就是推导 `EXPORTABLE_FIELDS`，而它已经在 `_INTERNAL_FIELDS` 里
    # 被排除 -> `/api/export/fields` 与所有导出列**一字不变**
    # （改动前后 EXPORTABLE_FIELDS 的 sha256 相同：43 项，
    #  9bed0d6465699a82a718cf88e17c654b3e9a64a17426d214a9e0fe964eceb419）。
    # 位置也照 DDL 放在 content_hash 之后，别挪。
    title_bullets_hash: str = ""
    # 评分 + 卖家信息（v3 后期新增）
    rating: str = ""
    review_count: str = ""
    seller_id: str = ""
    seller_name: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Batch:
    id: int = 0
    name: str = ""
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    needs_screenshot: bool = False
    # 状态机 + 完成时间
    status: str = "running"          # running / completed / failed
    completed_at: str = ""
    # 调用方原样回传
    external_id: str = ""
    # 完成通知
    callback_url: str = ""
    callback_status: str = ""        # pending / sent / failed / disabled / ""(无回调)
    callback_attempts: int = 0
    callback_next_retry_at: str = ""
    callback_last_error: str = ""
    callback_sent_at: str = ""
    created_at: str = ""
    updated_at: str = ""


# 变动类型枚举
CHANGE_TYPE_PRICE_STOCK = "price_stock"
CHANGE_TYPE_TITLE_BULLETS = "title_bullets"
CHANGE_TYPE_NEW = "new"

# 导出可选字段（排除内部字段，加虚拟字段 total_price）
# ean_list 已逻辑下线（amazon.com 基本不暴露 EAN，实测 100% 为空）：不再导出，
# 其槽位由 variant_attributes（变体属性）顶上；DB 物理列保留不动。
_INTERNAL_FIELDS = {"id", "content_hash", "title_bullets_hash", "created_at", "updated_at", "screenshot_path", "ean_list"}
EXPORTABLE_FIELDS = [f.name for f in __import__("dataclasses").fields(AsinData) if f.name not in _INTERNAL_FIELDS] + ["total_price"]
