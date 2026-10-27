# tests/fixtures/ca —— 加拿大站真实商品页（裁剪 + gzip）

## 这是什么

2026-09-19 从 `www.amazon.ca` 真实采集下来的两张商品页，用于
`tests/test_ca_real_pages.py`。

| 夹具 | 商品 | 为什么选它 |
|---|---|---|
| `B09S6W6H5B.html.gz` | Gildan 长袖 T 恤三件装 | **Amazon.ca 自营**（无第三方卖家链接）；detailBullets 版详情表；BSR 有大类 + 子类两级 |
| `B0F673BSBL.html.gz` | YATINEY 电脑桌 | **第三方卖家** YATINEY + Amazon 发货；tech-spec 表格版详情；`ManufacturerPartNumber` 标签**无空格** |

两张合起来覆盖了 F-012 加拿大站实测暴露的**全部** 5 个字段级缺陷，
每个缺陷在两张页面里至少有一张能复现。

## 为什么用真实页面，不用构造的 HTML

这几个缺陷的共同点是：**构造的 HTML 复现不了**。
能构造出来就说明我们已经知道页面长什么样，那就不叫排查了。

实际发生的事情就是这样 —— 第一轮我构造了一张带 `#merchant-info` 的
"加拿大站页面"，解析器**正确**识别出了 `("AMAZON", "Amazon.ca")`，
于是我以为卖家那条已经修好了。真实页面拿来一跑才发现：那个容器
**根本不存在**，2026 版 buybox 用的是 `#merchantInfoFeature_feature_div`，
而且自营文案是 `Shipper / Seller Amazon.ca`，不是 `Sold by`。

## 裁剪做了什么

原页面 2.9MB / 2.3MB，直接进仓库会把 git 历史撑大。裁剪规则：

1. 剥掉 `<script>` / `<style>` / `<noscript>` / `<svg>` / `<iframe>` / 注释
2. 超长 `data:` URI 图片换成 `data:,`
3. gzip -9

结果 170KB / 141KB。**除下面那一条外，裁剪前后解析结果逐字段相同**
（裁剪脚本会逐字段比对，不一致就不落盘）。

### ⚠ 已知不覆盖：`image_urls`

商品主图列表来自 Amazon 内联的 `colorImages` JSON —— 在 `<script>` 里，
被第 1 步剥掉了。整页能解析出 5 张图，夹具只有 1 张。

**别拿这套夹具断言 `image_urls`**，那会得到一个与生产不符的数字。
要测图片解析，另找一张保留 `<script>` 的夹具。

保留脚本就能修好这一条，代价是夹具回到 MB 级 —— 这两张夹具是为
**那 5 个字段缺陷**准备的，图片不在其中，所以选了小的那一边。

## 怎么更新 / 新增夹具

```bash
# 1) 采集时留档（用完记得关）
DUMP_HTML=1 python run_worker.py ...

# 2) 先看解析结果对不对
python -m tools.diag_parse 'worker/degraded_dump/ok/amazon.ca_*.html' --raw

# 3) 裁剪脚本见 tests/test_ca_real_pages.py 顶部的说明
```

⚠ 这些是**真实页面快照**，Amazon 随时会改版。夹具红了不一定是代码坏了 ——
先用 `tools/diag_parse.py` 对一张**新抓的**页面跑一遍，确认页面结构是不是变了。
