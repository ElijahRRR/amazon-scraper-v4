# tests/fixtures/parser_text —— 文本类字段的真实页面片段（美国站）

## 这是什么

2026-10-10 抽查时从 `www.amazon.com` 真实抓下来的商品页里**原文截取**的片段，
供下面几个测试文件用：

| 片段 | 用在 | 守的缺陷 |
|---|---|---|
| `us_B0CG19X7RN` | `tests/test_parser_detail_fixes.py` | `#feature-bullets`（卖点去重 / lxml 多 class 选择器的变体底稿）、`#merchantInfoFeature_feature_div`（2026 版卖家挂件，第三方卖家 callalily；自营变体的底稿）、th/td 详情表的 6 行（Item Dimensions D x W x H、Item Weight、Item Dimensions、Manufacturer、Manufacturer Part Number、Best Sellers Rank —— BSR 的两条排名粘连、重量 / 尺寸 / 料号标签变体的底稿） |
| `us_B0F3JB4D4Z` | `tests/test_parser_detail_fixes.py` | 只截了 byline 和 Product Overview，**有意不带**后面的 th/td 详情表：整页上详情表那一行会覆盖 po- 行，只留 po- 行才测得到制造商 / 品牌在双 td 分支里的取值（带分隔符取值后 span 两侧的缩进会变成首尾空格） |
| `us_B0D4QGVN6F`、`us_B0D45YH9FL` | `tests/test_parser_text_fields.py` | `#feature-bullets`：卖家以自己的小标题开头、正文提到 "customer service" 的那条卖点（Smiley Customer Service: … / Instruction and High Quality Wood: … customer service team …），基线按黑名单子串整条删掉 |
| `us_B0D86XBV23` | `tests/test_parser_text_fields.py` | `#a-popover-cReturnsPolicyPopover`：黑名单当初要挡的退货弹层模板句（"Go to your orders and start the return" 等三句） |
| `us_B0FFGFT343` | `tests/test_parser_text_fields.py` | `colorImages` 所在的整段 `<script>` 原样保留（解析器先按"含 colorImages 的第一个 script"选段，这一步也要测到）：第 9 张 PT08 `"hiRes":null`，基线整张丢掉 |

## 截取规则

整页 0.8–2.6MB，进不了仓库，只留缺陷用得到的那几块。**片段一律按原始字节区间切**，
不经过任何 HTML 解析器再序列化 —— 标签、属性顺序、空白、实体原样保留
（详情值的空白归一测的就是空白，重新序列化会把证据洗掉）：

1. 按 id 取的块（`#feature-bullets`、`#merchantInfoFeature_feature_div`、
   `#productOverview_feature_div`、`#bylineInfo_feature_div`、`#a-popover-cReturnsPolicyPopover`）：
   从开标签到配对的闭标签，同名标签计数配平。
2. 详情表的单行：所在 `<table>` 的开标签原文 + 那一行 `<tr>…</tr>` 原文 + `</table>`，
   多行按页面上的先后排列（`_map_detail` 后写的赢，顺序本身是被测的东西）。
3. 外面套一层 `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>`，
   第二行注释写明出处。

3a. `colorImages` 所在的整段 `<script>` 原样保留（不是只留数组）。

被测字段在片段上与整页逐字段相同（两条解析引擎都核对过）：`us_B0CG19X7RN` 的
BSR / 料号 / 商品重量与尺寸 / 制造商 / 卖家 / 卖点，`us_B0D4QGVN6F` / `us_B0D45YH9FL`
的卖点，`us_B0FFGFT343` 的图片列表。

## 构造变体

30 张美国对照页上没有现成例子的边界（Shipping Weight 标签、尺寸行里带重量、
无空格的 ManufacturerPartNumber、"See more" 展开区里的卖点副本、多 class 的
卖点 span、Amazon 自营的卖家挂件、卖家档案链接没有 `sellerProfileTriggerId` 且挂件前面
先出现配送消息里带 `seller=` 的「Details」链接、`&nbsp;`），测试在上面的真实片段上做**一处**
标明的改动；每条用例的 docstring 写了改的是哪一处、为什么。

## 已知不覆盖

* 片段里没有 JSON-LD、价格、buybox —— 别拿它们断言那些字段。
