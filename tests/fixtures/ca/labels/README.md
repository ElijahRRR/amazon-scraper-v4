# tests/fixtures/ca/labels —— 加拿大站详情表标签差异（原文切片）

给 `tests/test_ca_detail_labels.py` 用。守的是两处**标签对不上**的缺陷：

| 加拿大站标签 | 美国站同一位置 | 该进的字段 | 修复前 |
|---|---|---|---|
| `Country of Publication` | `Country of Origin` | `country_of_origin` | 13/36 张加拿大页有值，导出全是 N/A |
| `ProductIDType` | `UPC` | `upc_list` | 6/36 张加拿大页有值，导出全空 |

## 来源

2026-10-10 抽查时抓的真实页面（`verify_ca/pages/`、`verify_ca/us_pages/`，未设地址；
这两个字段与地区无关）。每个夹具是**一栏**详情表的原文切片：

| 夹具 | 来源页面 | 切的是哪一栏 | 为什么选它 |
|---|---|---|---|
| `B07JN24QCB.ca.html` | amazon.ca/dp/B07JN24QCB（13:52:06Z） | `#productDetails_expanderTables_depthLeftSections` | `Country of Publication` + `ProductIDType` **一格两个码** |
| `B07JN24QCB.us.html` | amazon.com/dp/B07JN24QCB（同批） | 同上 | 同一商品的美国站：标签是 `UPC`，值逐字相同 —— 两站形状对照 |
| `B0CR5D95NN.ca.html` | amazon.ca/dp/B0CR5D95NN（13:52:31Z） | `#productDetails_expanderTables_depthRightSections` | `Country of Publication` + `ProductIDType` 单码 |
| `B0CQWZR6L8.ca.html` | amazon.ca/dp/B0CQWZR6L8（13:51:53Z） | `#productDetails_expanderTables_depthLeftSections` | 加拿大页里少数写 `Country of Origin` 的（对照 / 优先级用例取它那一行） |

## 裁剪做了什么

1. 按 div 配平从原始 HTML 里**原文**切出那一栏（不经解析器重新序列化，标签、属性、空白都是原样）；
2. 去掉其中的 `<script>`（"Customer Reviews" 那一行里有两段）；
3. 外面套上真实页面上的两层祖先容器 `#prodDetails` / `#productDetails_expanderSectionTables`
   （id、class 照抄），中间纯布局用的 `a-row` / `a-column` div 省略。

裁剪脚本对每个夹具都核对过：两条引擎（selectolax / lxml）在夹具上解析出的
`country_of_origin` / `upc_list` 与整页解析结果**完全相同**，不同就不落盘。

## 不覆盖什么

只有详情表一栏，没有价格、卖家、图片、面包屑。**别拿这套夹具断言别的字段** ——
整页级别的真实夹具在上一层目录（`../B09S6W6H5B.html.gz` 等）。
