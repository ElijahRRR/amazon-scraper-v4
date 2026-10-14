# tests/fixtures/offer —— buybox 报价相关字段的真实页面片段（美国站）

给 `tests/test_offer_price_seller_aod.py` 用。守三件事：accordion 布局的原价、
卖家链接里没有 `seller=` 时的卖家 ID、无推荐报价页走「查看全部报价」(aodAjaxMain)
兜底时的卖家与库存数。

`tests/test_accordion_buy_row.py` 也用这里的 accordion 片段：配送日期 / 天数 / 运费只读
下单那一行、会员价行被选中时现价与原价都取全新行。它在片段上做的改动（把二手行日期改成
Tomorrow、挪动选中行、改配送价、改会员价行的划线价等）都在**测试里现做**并写明，夹具文件本身是原样裁剪。

## 来源

全部来自 2026-10-11 05:49 (UTC) 前后抓的 `www.amazon.com` 商品页抽查语料（30 张，
邮编 10001）。

| 夹具 | 为什么选它 |
|---|---|
| `dp_us_B0CQWZR6L8_accordion.html.gz` | accordion：全新行选中、本身无划线价；未选中的二手行（Amazon Resale）有 "New Condition Price: $79.99"。原价曾被导出成 = 现价。两行各一张 `form#addToCart`，merchantID 不同（A1IOUXTP7ANG7D / A2L77EE7U53NWQ），二手行 `#merchant-info` 里是 Amazon Resale 的类目节点链接 `/b?node=10158976011` |
| `dp_us_B0CRGYZ4HV_accordion_list.html.gz` | accordion，全新行**自己**的中栏块（`apex_desktop_newAccordionRow`）标了 "List Price: $44.99" —— 修完不能丢 |
| `dp_us_B0CRH6FDD7_single_list.html.gz` | 传统单 offer buybox（没有 `#buyBoxAccordion`），"List Price: $49.99" —— 非 accordion 路径的对照 |
| `dp_us_B07JN24QCB_nfo.html.gz` | 无推荐报价（"No featured offers available"），`input#merchantID` 的 value 为空 |
| `dp_us_B0D12731YT_accordion_prime.html.gz` | accordion，选中的是会员价行（primeSavingsUpsellAccordionRow，$26.99，不带配送信息），全新行普通价 $35.99；"List Price: $34.99" 标在会员价行自己的中栏块里，全新行自己没有划线价 |
| `dp_us_B0F3CQT8G5_accordion_prime_list.html.gz` | accordion，选中的是会员价行（$79.99）；全新行普通价 $84.99，**全新行自己**的中栏块（apex_desktop_newAccordionRow）标了 "List Price: $99.99" —— 原价跟全新行取时不能丢 |

## 裁剪做了什么

1. 剥掉 `<script>` / `<style>` / `<noscript>` / `<svg>` / `<iframe>` / `<link>` / 注释；
2. 只留被测字段读到的子树（经 selectolax 序列化，标签与属性原样）：
   * accordion：`#apex_desktop`（中栏各行价格块）+ `#buyBoxAccordion`（右栏各行）；
   * 单 offer：`#apex_desktop` + `#buybox`；
   * 无推荐报价页：`#titleSection` + `#buybox`；
3. 超长 `data:image/...` 换成 `data:,`；gzip -9。全部 < 30KB。

裁剪后逐一核对过：现价 / buybox 价 / 原价 / 运费 / 配送日期 / 配送天数 / 品相 / 卖家 ID /
卖家名 / 库存状态 / 库存数在两条解析引擎上与完整页面**逐字段相同**，例外两处：
`dp_us_B07JN24QCB_nfo` 在 lxml 引擎上的配送日期 / 天数（lxml 从片段外的推荐卡片读到了
`Oct 15`；无推荐报价页的配送由 AOD 兜底覆盖，用例不断言它）、`dp_us_B0F3CQT8G5_accordion_prime_list`
的 `is_fba`（片段里没有整页上那段发货方文案，别拿它断言 is_fba）。

## 没有真实片段的部分

* **AOD 响应**：美国站 aodAjaxMain 在本沙箱里回 503（2026-10-14 试过两条 URL），30 张
  抽查页里也没有任何 AOD 标记，所以 `parse_offer_listing` 与无推荐报价端到端用例里的 AOD
  响应是在测试里**构造**的（`tests/test_offer_price_seller_aod.py::_aod_offer`）：被测选择器
  读到的那几层照真实 AOD 响应原样保留，店名 / ID 虚构。拿到真实美国站 AOD 响应后应换成夹具。
* **Warehouse / Amazon Resale 被选中的二手 buybox**：30 张页里没有，用例在
  `dp_us_B0CQWZR6L8_accordion` 上做标明的变体（把同页那个真实的 Amazon Resale 链接放进
  二手行的加购表单）。

## 会过期

Amazon 随时改版，这些片段是 2026-10 的结构。用例红了先抓一张新页面确认是页面变了
还是解析器退化了。
