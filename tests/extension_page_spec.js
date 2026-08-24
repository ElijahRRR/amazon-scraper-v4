/**
 * extension/src/page.js 的规格用例。由 tests/test_extension_page_js.py 驱动
 * （`node tests/extension_page_spec.js`），退出码非 0 即失败，失败详情走 stdout。
 *
 * ──────────────────────────────────────────────────────────────
 * 为什么这一层值得单独测
 * ──────────────────────────────────────────────────────────────
 * `page.js` 是整个插件里唯一**依赖 Amazon 前端实现细节**的地方：
 * `s-result-item` / `s-pagination-next` / `#sellerProfileTriggerId` 这些
 * 类名和 id 都是 Amazon 的产物，改版就变。而它坏掉的方式全部是**静默**的：
 *
 *   页面类型判错   -> 弹窗里出现的是另一套按钮，用户以为功能就长这样
 *   ASIN 抓不到    -> "本页 0 个商品"，看着像这一页真的没有商品
 *   卖家抠不出来   -> "整店采集" 灰掉，看着像这个商品没有三方卖家
 *   翻页链接找不到 -> 采完第一页就停，看着像只有一页
 *
 * 没有一条会报错。所以这里用**固定的 HTML 夹具**把每条判定钉死：夹具不变
 * 结论就不该变，将来 Amazon 真改了版，改夹具的人会被迫同时想清楚"新结构下
 * 这条判定还成不成立"。
 *
 * 夹具是按真实页面结构手写的最小骨架，不是抓下来的完整页面：完整页面
 * 一是几 MB，二是里面 99% 的内容与被测逻辑无关，改一次版整份夹具作废。
 */

'use strict';

const path = require('path');
const { JSDOM } = require('jsdom');

// page.js 是个 IIFE，会往 globalThis 上挂 AmzPage。require 它之前
// 先不需要 DOM —— 它的函数都接收 doc 参数，不摸全局 document。
const P = require(path.join(__dirname, '..', 'extension', 'src', 'page.js'));

let failures = 0;
let checks = 0;

function check(name, actual, expected) {
  checks += 1;
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures += 1;
    console.log(`FAIL  ${name}\n        期望 ${e}\n        实际 ${a}`);
  }
}

function docOf(html) {
  return new JSDOM(html).window.document;
}

// ────────────────────────────────────────────────────────────
// 夹具
// ────────────────────────────────────────────────────────────

/** 搜索结果页 / 卖家店铺页共用的结果网格（两者 DOM 完全一样，见 page.js 头注）。 */
const GRID = `
<div class="s-result-item" data-asin="B0AAAAAAA1">
  <div data-component-type="sp-sponsored-result"></div>
  <h2><span>Sponsored Widget</span></h2>
  <span class="a-price"><span class="a-offscreen">$9.99</span></span>
  <img class="s-image" src="https://m.media-amazon.com/1.jpg">
</div>
<div class="s-result-item" data-asin="B0AAAAAAA2">
  <h2><a><span>Organic Widget</span></a></h2>
  <span class="a-price" data-a-color="base"><span class="a-offscreen">$19.99</span></span>
  <img class="s-image" src="https://m.media-amazon.com/2.jpg">
</div>
<div class="s-result-item" data-asin=""></div>
<div class="s-result-item" data-asin="NOTANASIN"></div>
<a class="s-pagination-next" href="/s?k=widget&page=2">Next</a>
`;

const DETAIL = `
<div id="dp" class="a-container">
  <div id="centerCol">
    <h1><span id="productTitle">  A Real   Product  </span></h1>
  </div>
  <div id="merchant-info">
    Ships from Amazon. Sold by
    <a id="sellerProfileTriggerId"
       href="/sp?seller=A2L77EE7U53NWQ&amp;asin=B0DETAIL01">Cool Shop</a>
  </div>
  <div id="similar"><div data-asin="B0RELATED1"></div></div>
</div>
`;

// ────────────────────────────────────────────────────────────
// 页面类型
// ────────────────────────────────────────────────────────────

{
  const d = docOf(`<html><body>${DETAIL}</body></html>`);
  const info = P.detectPage('https://www.amazon.com/Some-Slug/dp/B0DETAIL01?th=1', d);
  check('detail: type', info.type, 'detail');
  check('detail: asin', info.asin, 'B0DETAIL01');
  check('detail: title 压空白', info.title, 'A Real Product');
  check('detail: sellerId', info.sellerId, 'A2L77EE7U53NWQ');
  check('detail: sellerName', info.sellerName, 'Cool Shop');
  check('detail: 不是自营', info.isAmazonSelf, false);
}

{
  // 详情页上有"相关商品"卡片。判定顺序错了的话这一页会被认成列表页 ——
  // 于是"加入待采队列"消失，取而代之的是"采集本页"，而用户不会觉得哪里不对。
  const d = docOf(`<html><body>${DETAIL}${GRID}</body></html>`);
  const info = P.detectPage('https://www.amazon.com/dp/B0DETAIL01', d);
  check('detail 优先于 list', info.type, 'detail');
}

{
  // 卖家店铺页与搜索页 DOM 一模一样，只有 URL 上的 me= 能区分。
  const d = docOf(`<html><body>${GRID}</body></html>`);
  const info = P.detectPage('https://www.amazon.com/s?me=A2L77EE7U53NWQ&page=1', d);
  check('seller: type', info.type, 'seller');
  check('seller: sellerId', info.sellerId, 'A2L77EE7U53NWQ');
  check('seller: itemCount', info.itemCount, 2);
}

{
  const d = docOf(`<html><body>${GRID}</body></html>`);
  const info = P.detectPage('https://www.amazon.com/s?k=wireless+mouse', d);
  check('list: type', info.type, 'list');
  check('list: keyword', info.keyword, 'wireless mouse');
  check('list: 没有 sellerId', info.sellerId, null);
  check('list: hasNextPage', info.hasNextPage, true);
}

{
  // 榜单页：路径命中白名单，但没有 s-result-item 网格
  const d = docOf(`<html><body>
    <div data-csa-c-item-id="amzn1.asin.B0BEST0001"><span class="p13n-sc-truncate">Top 1</span></div>
    <div data-csa-c-item-id="amzn1.asin.B0BEST0002"></div>
  </body></html>`);
  const info = P.detectPage('https://www.amazon.com/gp/bestsellers/electronics', d);
  check('bestsellers: type', info.type, 'list');
  check('bestsellers: itemCount', info.itemCount, 2);
}

{
  const d = docOf('<html><body><p>nothing here</p></body></html>');
  const info = P.detectPage('https://www.amazon.com/gp/help/customer/display.html', d);
  check('帮助页: unknown', info.type, 'unknown');
}

// ────────────────────────────────────────────────────────────
// ASIN 抽取
// ────────────────────────────────────────────────────────────

{
  const d = docOf(`<html><body>${GRID}</body></html>`);
  const items = P.collectListItems(d);
  check('grid: 只留合法 ASIN', items.map((i) => i.asin), ['B0AAAAAAA1', 'B0AAAAAAA2']);
  check('grid: rank 是 DOM 顺序', items.map((i) => i.rank), [1, 2]);
  check('grid: 广告位识别', items.map((i) => i.sponsored), [true, false]);
  check('grid: 标题', items[1].title, 'Organic Widget');
  check('grid: 价格', items[1].price, '$19.99');
  check('grid: 图片', items[1].image, 'https://m.media-amazon.com/2.jpg');
}

{
  // 三条来源合并去重：同一个 ASIN 同时出现在 data-asin 和 /dp/ 链接里只算一次
  const d = docOf(`<html><body>
    <div class="s-result-item" data-asin="B0DUPE0001"><h2><span>A</span></h2></div>
    <a href="/Some/dp/B0DUPE0001">同一个商品的另一个入口</a>
    <a href="/Other/dp/B0ONLYLINK">只以链接形式出现</a>
  </body></html>`);
  const items = P.collectListItems(d);
  check('去重 + 链接兜底', items.map((i) => i.asin), ['B0DUPE0001', 'B0ONLYLINK']);
}

{
  // 三种广告位标记都要认。少认一种，rank 这一列就会把广告算成自然排名。
  const variants = [
    '<div data-component-type="sp-sponsored-result"></div>',
    '<span class="puis-sponsored-label-text">Sponsored</span>',
    '<span class="s-sponsored-label-text">Sponsored</span>',
    '<span class="a-color-secondary">Sponsored</span>',
  ];
  variants.forEach((marker, i) => {
    const d = docOf(`<html><body>
      <div class="s-result-item" data-asin="B0SPON0000">${marker}</div>
    </body></html>`);
    check(`广告位标记 #${i}`, P.collectListItems(d)[0].sponsored, true);
  });
  const clean = docOf('<html><body><div class="s-result-item" data-asin="B0CLEAN000"><span class="a-color-secondary">Best Seller</span></div></body></html>');
  check('非广告不误判', P.collectListItems(clean)[0].sponsored, false);
}

// ────────────────────────────────────────────────────────────
// 卖家抽取
// ────────────────────────────────────────────────────────────

{
  // Amazon 自营：**不**伪造一个 sellerId。整店采集对它没意义（数百万商品），
  // 给一个 ID 会让用户点下去炸出一个跑不完的任务。
  const d = docOf(`<html><body><div id="dp"><div id="productTitle">x</div>
    <div id="merchant-info">Ships from and sold by Amazon.com.</div>
  </div></body></html>`);
  const info = P.detectPage('https://www.amazon.com/dp/B0SELFOWN1', d);
  check('自营: 没有 sellerId', info.sellerId, null);
  check('自营: 标记出来了', info.isAmazonSelf, true);
}

{
  // buybox 结构变了、没有卖家链接时：如实返回 null，不猜。
  const d = docOf(`<html><body><div id="dp"><div id="productTitle">x</div>
    <div id="tabular-buybox">Ships from SomeWarehouse</div>
  </div></body></html>`);
  const info = P.detectPage('https://www.amazon.com/dp/B0NOSELLR1', d);
  check('无卖家信息: null', info.sellerId, null);
  check('无卖家信息: 也不是自营', info.isAmazonSelf, false);
}

{
  // 兜底分支：卖家链接不在已知选择器上，但 buybox 的 HTML 里有 seller=
  const d = docOf(`<html><body><div id="dp"><div id="productTitle">x</div>
    <div id="buybox"><div><span data-x="1"><a class="brand-new-class"
      href="/gp/help/seller/at-a-glance.html?seller=A1PA6795UKMFR9">Shop</a></span></div></div>
  </div></body></html>`);
  const info = P.detectPage('https://www.amazon.com/dp/B0FALLBAK1', d);
  check('兜底扫 seller=', info.sellerId, 'A1PA6795UKMFR9');
}

// ────────────────────────────────────────────────────────────
// 翻页
// ────────────────────────────────────────────────────────────

{
  const d = docOf(`<html><body>${GRID}</body></html>`);
  check('有下一页', P.hasNextPage(d), true);
  check('下一页链接', P.nextPageLink(d).endsWith('/s?k=widget&page=2'), true);
}

{
  const d = docOf('<html><body><a class="s-pagination-next s-pagination-disabled">Next</a></body></html>');
  check('置灰的下一页不算', P.hasNextPage(d), false);
}

{
  const d = docOf('<html><body><li class="a-last"><a href="/next">Next</a></li></body></html>');
  check('老版分页兜底', P.hasNextPage(d), true);
  const dd = docOf('<html><body><li class="a-last a-disabled"><a href="/next">Next</a></li></body></html>');
  check('老版分页置灰', P.hasNextPage(dd), false);
}

// ────────────────────────────────────────────────────────────
// 归一化
// ────────────────────────────────────────────────────────────

{
  // 11 位串**不能**被截成前 10 位。截出来的东西格式完全合法（B + 9 位），
  // normalizeAsin 验不出来，于是会当成一个真 ASIN 推给服务端 ——
  // 采回来的是另一个商品或者 404，而没有任何一处会报错。
  // （本文件第一版的夹具就误写成 11 位，正是它暴露了这条缺口。）
  check('URL 里 11 位不被截断', P.asinFromUrl('/dp/B0ELEVENCH1'), null);
  check('URL 里正好 10 位', P.asinFromUrl('/dp/B0TENCHARS'), 'B0TENCHARS');
  check('URL 里 10 位后跟分隔符', P.asinFromUrl('/dp/B0TENCHARS/ref=sr_1_1'), 'B0TENCHARS');
  const d11 = docOf('<html><body><a href="/x/dp/B0ELEVENCH1">a</a></body></html>');
  check('链接里 11 位不被截断', P.collectListItems(d11).length, 0);
}

{
  check('ASIN 归一化: 小写转大写', P.normalizeAsin('b0abcdefgh'), 'B0ABCDEFGH');
  check('ASIN 归一化: 长度不对', P.normalizeAsin('B0TOOLONG123'), null);
  check('ASIN 归一化: 不以 B 开头', P.normalizeAsin('X012345678'), null);
  check('seller 归一化', P.normalizeSellerId('a2l77ee7u53nwq'), 'A2L77EE7U53NWQ');
  check('seller 归一化: 太短', P.normalizeSellerId('A123'), null);
}

console.log(`${checks - failures}/${checks} 项通过`);
process.exit(failures ? 1 : 0);
