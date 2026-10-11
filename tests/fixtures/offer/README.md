# tests/fixtures/offer —— buybox 报价相关字段的真实页面片段

给 `tests/test_offer_price_seller_aod.py` 用。守三件事：accordion 布局的原价、
Warehouse Deals 的卖家 ID、无推荐报价页走「查看全部报价」(aodAjaxMain) 兜底时的
卖家与库存数。

## 来源

| 夹具 | 来源 | 抓取时间 (UTC) | 为什么选它 |
|---|---|---|---|
| `dp_ca_B09Y3DKJKQ_accordion.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:55 | accordion：全新行选中、本身无划线价；二手行有 "New condition price: $112.99"。原价曾被导出成 = 现价 |
| `dp_us_B0CQWZR6L8_accordion.html.gz` | www.amazon.com 商品页 | 2026-10-11 05:49 | 同一个 bug 的美国站形状（标签大写 "New Condition Price"） |
| `dp_ca_B0B9R8R8NC_accordion_was.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:53 | accordion，全新行**自己**有 "Was: $169.99" —— 修完不能丢 |
| `dp_us_B0D12731YT_accordion_prime.html.gz` | www.amazon.com 商品页 | 2026-10-11 05:49 | accordion，选中的是会员价行（primeSavingsUpsellAccordionRow），"List Price: $34.99" |
| `dp_ca_B0CRH6FDD7_single_was.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:52 | 传统单 offer buybox，"Was: $64.99" —— 非 accordion 路径的对照 |
| `dp_ca_B0GKNHN7Q5_warehouse.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:53 | 二手 buybox，卖家 Warehouse Deals，链接是 `/b?node=8929975011`（无 `seller=`），ID 在同一表单的 `input#merchantID` |
| `dp_ca_B0D45YH9FL_nfo.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:53 | 无推荐报价（"No featured offers available"），`input#merchantID` 的 value 为空 |
| `aod_ca_B0D45YH9FL.html.gz` | 同一 ASIN 的 aodAjaxMain 响应 | 2026-10-10 14:14 | 第一个报价 Botley Store / A15CBTP5BTSNTZ / $246.62 / maxQty 3，与用户导出那一行的价格、运费、配送逐字段对上 |
| `aod_ca_B098QJXZLS.html.gz` | www.amazon.ca aodAjaxMain 响应 | 2026-10-11 06:18 | 无推荐报价；第一个报价 GiantRun LLC / ADHQ1EIRR50GJ / $503.56 / maxQty 20，价格与导出那一行（库存数 5）相同 |
| `aod_ca_B0D4QGVN6F.html.gz` | www.amazon.ca aodAjaxMain 响应 | 2026-10-11 06:19 | 无推荐报价；第一个报价卖家名 "Amazon US" / ADQ8PPI9HA4SX / $238.23 / maxQty 5，价格与导出那一行相同 |
| `aod_ca_B0DJ231D92.html.gz` | www.amazon.ca aodAjaxMain 响应 | 2026-10-11 06:21 | 有推荐报价的 AOD：pinned offer 是 CHOEZON DIRECT，maxQty 5 |
| `dp_ca_B0DJ231D92_qty.html.gz` | 同一时刻的商品页 | 2026-10-11 06:21 | 只留 `select#quantity`：1..5。与上一份对照 —— **maxQty = 商品页下拉最大项**，AOD 自己的静态下拉只到 4 |

商品页来自 `verify_ca/pages`（加拿大，未设地址）与 `verify_ca/us_pages`（美国）这批抽查
语料；AOD 用 `verify_ca/work_k9/fetch_aod.py` 的方式抓（先 GET 商品页拿 cookie，再带
`X-Requested-With: XMLHttpRequest` GET `/gp/product/ajax/aodAjaxMain/...`）。

## 裁剪做了什么

1. 剥掉 `<script>` / `<style>` / `<noscript>` / `<svg>` / `<iframe>` / `<link>` / 注释；
2. 只留被测字段读到的子树，标签与属性原样：
   * accordion 原价：`#apex_desktop`（中栏各行价格块）+ `#buyBoxAccordion`（右栏各行）；
   * 单 offer 原价：`#apex_desktop` + `#buybox`；
   * 卖家：`#buybox`；无推荐报价页：`#titleSection` + `#buybox`；
   * AOD：`#aod-pinned-offer` + 前两个 `#aod-offer`（B0DJ231D92 只留 pinned）；
3. 超长 `data:image/...` 换成 `data:,`；gzip -9。全部 < 30KB。

`B098QJXZLS` / `B0D4QGVN6F` 的 AOD 比用户那次采集晚了约 17 小时，但第一个报价的价格与
导出逐分相同；`B0D45YH9FL` 用的是晚 33 分钟抓的那份（价格 / 运费 / 配送三项都对上）。
三行导出的库存数都是写死的 5、卖家两列都是 N/A —— 这正是 G7 要修的。

裁剪后逐一核对过：被测字段（原价 / 卖家 ID / 卖家名 / 品相 / 无推荐报价页的价格与库存 /
`parse_offer_listing` 的整个返回值 / 数量下拉）在两条解析引擎上与完整页面**逐字段相同**。

## 会过期

Amazon 随时改版，这些片段是 2026-10 的结构。用例红了先抓一张新页面确认是页面变了
还是解析器退化了（`tools/diag_parse.py`）。
