# tests/fixtures/parser_text —— 文本类字段的真实页面片段

## 这是什么

2026-10-10 抽查时从 `www.amazon.ca` / `www.amazon.com` 真实抓下来的商品页里
**原文截取**的片段，供 `tests/test_parser_text_fields.py` 用。守的是三个缺陷：

| 缺陷 | 字段 | 片段 |
|---|---|---|
| G3 黑名单按子串整条删，误删卖家真实卖点 | `bullet_points` | `ca_B0D4QGVN6F`（Smiley Customer Service: …）、`ca_B09Y3DKJKQ`（Customer Support - …）、`ca_B0D45YH9FL`（Instruction and Customer Service: …）；黑名单当初要挡的模板句 `us_B0D86XBV23`（退货弹层 "Go to your orders and start the return" 等三句） |
| G4 `"hiRes":null` 的图整张丢 | `image_urls` | `ca_B09Y3DKJKQ`（**主图** MAIN hiRes=null）、`ca_B0FFGFT343`（第 9 张 PT08 hiRes=null） |
| G5 双 td 布局取值带首尾空格 | `brand` 及详情值 | `ca_B09Y3DKJKQ`（`tr.po-brand` 的 td 里 span 两侧有缩进 → `' MoKitDora '`）、`us_B0F3JB4D4Z`（只截了 byline 和 Product Overview，**有意不带**后面的 th/td 详情表：整页上详情表那一行会覆盖 po- 行，只留 po- 行才测得到制造商在双 td 分支里的 `' CHANGZHOU JUTAI ELECTRONIC CO.,LTD '`）、`ca_B0B3RDLC1J`（`#detailBullets_feature_div` 整块，测 `li > span > span.a-text-bold` 分支） |

### 随 main（#26）进来的美国站片段

main 上这批修复是按美国站重写的，测试只用美国站片段。合进本分支后原样保留，给下面两个文件用
（`tests/test_parser_text_fields_us.py` 是 main 上 `tests/test_parser_text_fields.py` 的原样副本）：

| 片段 | 用在 | 守的缺陷 |
|---|---|---|
| `us_B0CG19X7RN` | `tests/test_parser_detail_fixes.py` | `#feature-bullets`（卖点去重 / lxml 多 class 选择器的变体底稿）、`#merchantInfoFeature_feature_div`（2026 版卖家挂件，第三方卖家 callalily；自营变体的底稿）、th/td 详情表的 6 行（Item Dimensions D x W x H、Item Weight、Item Dimensions、Manufacturer、Manufacturer Part Number、Best Sellers Rank —— BSR 的两条排名粘连、重量 / 尺寸 / 料号标签变体的底稿） |
| `us_B0D4QGVN6F`、`us_B0D45YH9FL` | `tests/test_parser_text_fields_us.py` | `#feature-bullets`：卖家以自己的小标题开头、正文提到 "customer service" 的那条卖点，基线按黑名单子串整条删掉 |
| `us_B0FFGFT343` | `tests/test_parser_text_fields_us.py` | `colorImages` 所在的整段 `<script>`：第 9 张 PT08 `"hiRes":null`，基线整张丢掉 |

`tests/test_parser_detail_fixes.py` 在 30 张美国对照页上没有现成例子的边界（Shipping Weight 标签、
尺寸行里带重量、无空格的 ManufacturerPartNumber、"See more" 展开区里的卖点副本、多 class 的卖点 span、
Amazon 自营的卖家挂件、卖家档案链接没有 `sellerProfileTriggerId` 且挂件前面先出现配送消息里带
`seller=` 的「Details」链接、`&nbsp;`）上，在真实片段上做**一处**标明的改动；每条用例的 docstring
写了改的是哪一处、为什么。

## 截取规则

整页 0.8–2.6MB，进不了仓库，只留缺陷用得到的那几块。**片段一律按原始字节区间切**，
不经过任何 HTML 解析器再序列化 —— 标签、属性顺序、空白、实体原样保留
（G5 测的就是空白，重新序列化会把证据洗掉）：

1. 按 id 取的块（`#feature-bullets`、`#productOverview_feature_div`、
   `#bylineInfo_feature_div`、`#a-popover-cReturnsPolicyPopover`、`#detailBullets_feature_div`）：
   从开标签到配对的闭标签，同名标签计数配平。
2. 详情表的单行（`Manufacturer` 等）：
   所在 `<table>` 的开标签原文 + 那一行 `<tr>…</tr>` 原文 + `</table>`。
3. `colorImages` 所在的整段 `<script>` 原样保留（不是只留数组 ——
   解析器先按"含 colorImages 的第一个 script"选段，这一步也要测到）。
4. 外面套一层 `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>`，
   第二行注释写明出处。

截出来的片段在两条解析引擎上复现的结论与整页一致（基线代码上：卖点少一条、
图少一张、品牌带首尾空格；修复后全部对上整页的正确值）。

## 已知不覆盖

* 片段里没有 JSON-LD、价格、buybox —— 别拿它们断言那些字段。

片段里可能还留着当初为商品类型（`product_type`）截的 `input#productCategory` / `Item Type Name`
等几行：商品类型的修复已按用户决定撤掉（下游用不上，而它会让两站全部商品复审一次），
这些行不再被任何用例断言，留着不影响别的测试。
