# tests/fixtures/offer —— buybox 报价相关字段的真实页面片段

给 `tests/test_offer_price_seller_aod.py` 用。守三件事：accordion 布局的原价、
Warehouse Deals 的卖家 ID、无推荐报价页走「查看全部报价」(aodAjaxMain) 兜底时的
卖家与库存数。

`tests/test_accordion_buy_row.py` 也用这里的 accordion 片段：配送日期 / 天数 / 运费只读
下单那一行、会员价行被选中时现价与原价都取全新行。它在片段上做的改动（把二手行日期改成
Tomorrow、挪动选中行、改配送价、改会员价行的划线价等）都在**测试里现做**并写明，夹具文件本身是原样裁剪。

`tests/test_offer_price_seller_aod_us.py` / `tests/test_accordion_buy_row_us.py` 是 main 上同名测试
（不带 `_us`）的原样副本，只用美国站片段，在这个分支上守住美国站的同一批行为；下表里标「main」的
三份片段只给它们用。

## 来源

| 夹具 | 来源 | 抓取时间 (UTC) | 为什么选它 |
|---|---|---|---|
| `dp_ca_B09Y3DKJKQ_accordion.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:55 | accordion：全新行选中、本身无划线价；二手行有 "New condition price: $112.99"。原价曾被导出成 = 现价 |
| `dp_us_B0CQWZR6L8_accordion.html.gz` | www.amazon.com 商品页 | 2026-10-11 05:49 | 同一个 bug 的美国站形状（标签大写 "New Condition Price"） |
| `dp_ca_B0B9R8R8NC_accordion_was.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:53 | accordion，全新行**自己**有 "Was: $169.99" —— 修完不能丢 |
| `dp_us_B0D12731YT_accordion_prime.html.gz` | www.amazon.com 商品页 | 2026-10-11 05:49 | accordion，选中的是会员价行（primeSavingsUpsellAccordionRow），"List Price: $34.99" |
| `dp_us_B0F3CQT8G5_accordion_prime_list.html.gz` | www.amazon.com 商品页 | 2026-10-11 05:49 | accordion，选中的是会员价行（$79.99）；全新行普通价 $84.99，**全新行自己**的中栏块（apex_desktop_newAccordionRow）标了 "List Price: $99.99" —— 原价跟全新行取时不能丢 |
| `dp_ca_B0CRH6FDD7_single_was.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:52 | 传统单 offer buybox，"Was: $64.99" —— 非 accordion 路径的对照 |
| `dp_ca_B0CG19X7RN_accordion_delivery.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:52 | accordion：选中的全新行只有 Friday, October 16；未选中的二手行有 October 14 / Monday, October 12。配送日期曾被导出成 October 12 |
| `dp_ca_B0GKNHN7Q5_warehouse.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:53 | 二手 buybox，卖家 Warehouse Deals，链接是 `/b?node=8929975011`（无 `seller=`），ID 在同一表单的 `input#merchantID` |
| `dp_ca_B0D45YH9FL_nfo.html.gz` | www.amazon.ca 商品页 | 2026-10-10 13:53 | 无推荐报价（"No featured offers available"），`input#merchantID` 的 value 为空 |
| `dp_us_B0CRGYZ4HV_accordion_list.html.gz`（main） | www.amazon.com 商品页 | 2026-10-11 05:49 | accordion，全新行**自己**的中栏块（`apex_desktop_newAccordionRow`）标了 "List Price: $44.99" —— 修完不能丢 |
| `dp_us_B0CRH6FDD7_single_list.html.gz`（main） | www.amazon.com 商品页 | 2026-10-11 05:49 | 传统单 offer buybox（没有 `#buyBoxAccordion`），"List Price: $49.99" —— 非 accordion 路径的对照 |
| `dp_us_B07JN24QCB_nfo.html.gz`（main） | www.amazon.com 商品页 | 2026-10-11 05:49 | 无推荐报价（"No featured offers available"），`input#merchantID` 的 value 为空 |
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

`dp_ca_B0CG19X7RN_accordion_delivery.html.gz` 按同一裁剪法（`#apex_desktop` + `#buyBoxAccordion`）
后加；它与 `dp_us_B0D12731YT_accordion_prime` / `dp_us_B0CQWZR6L8_accordion` 两份一起核过：
现价 / buybox 价 / 原价 / 运费 / 配送日期 / 配送天数 / 品相在两条引擎上与完整页面逐字段相同。

`dp_us_B0F3CQT8G5_accordion_prime_list.html.gz` 同法后加（会员价页的原价改跟全新行取，2026-10-14），
同样 7 个字段两条引擎与完整页面逐字段相同（改动前后的解析代码上都核过）。

标「main」的三份美国片段（随 main 的 #26 进来）按同一裁剪法做，核对时有两处例外：
`dp_us_B07JN24QCB_nfo` 在 lxml 引擎上的配送日期 / 天数（lxml 从片段外的推荐卡片读到了
`Oct 15`；无推荐报价页的配送由 AOD 兜底覆盖，用例不断言它）、`dp_us_B0F3CQT8G5_accordion_prime_list`
的 `is_fba`（片段里没有整页上那段发货方文案，别拿它断言 is_fba）。

美国站的 aodAjaxMain 没有真实响应（沙箱里回 503）：`*_us.py` 里的 AOD 响应是照上面那批
**加拿大站**真实 AOD 的结构在测试里构造的（`_aod_offer`），店名 / ID 虚构。

## 会过期

Amazon 随时改版，这些片段是 2026-10 的结构。用例红了先抓一张新页面确认是页面变了
还是解析器退化了（`tools/diag_parse.py`）。
