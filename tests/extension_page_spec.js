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
// 详情页判定的时机：骨架还没渲染完时也要认出来
// ────────────────────────────────────────────────────────────
//
// 回归用例：真机上打开一个产品页**立刻**点插件，显示"未识别到页面"，
// 关掉弹窗重开才好 —— 因为 Amazon 详情页的 `#productTitle` / `#dp`
// 是异步渲染的，而旧判据要求它们存在。用户看到的是一个时灵时不灵的功能。

{
  // 骨架一点都没渲染（只有 <body>），但 URL 路径里有 /dp/ASIN。
  // 这是无歧义的 —— 必须当场认出来。
  const d = docOf('<html><body></body></html>');
  const info = P.detectPage('https://www.amazon.com/Some-Slug/dp/B0EARLY001', d);
  check('骨架未渲染：仍认出详情页', info.type, 'detail');
  check('骨架未渲染：ASIN 拿得到', info.asin, 'B0EARLY001');
  check('骨架未渲染：ready=false', info.ready, false);
  check('骨架未渲染：标题暂时为空', info.title, '');
}

{
  // 渲染完之后 ready 变 true，标题/卖家跟着出现。
  const d = docOf(`<html><body>${DETAIL}</body></html>`);
  const info = P.detectPage('https://www.amazon.com/dp/B0DETAIL01', d);
  check('渲染完：ready=true', info.ready, true);
  check('渲染完：标题有了', info.title, 'A Real Product');
}

{
  // 反向：URL 的 **query** 里出现 /dp/ 时**不能**只凭 URL 认。
  // 搜索结果页的 ref/url 参数里真的会带它 —— 那一档必须要 DOM 佐证，
  // 否则搜索页会被认成详情页，整个列表采集入口消失。
  const d = docOf(`<html><body>${GRID}</body></html>`);
  const info = P.detectPage('https://www.amazon.com/s?k=x&ref=/dp/B0QUERY001', d);
  check('query 里的 /dp/ 不算详情页', info.type, 'list');
}

{
  // 但 query 里有 /dp/ **且**页面确实是详情页骨架时，仍然要认
  // （有些促销落地页的路径不带 /dp/，ASIN 只在参数里）。
  const d = docOf(`<html><body>${DETAIL}</body></html>`);
  const info = P.detectPage('https://www.amazon.com/promo?target=/dp/B0PROMO001', d);
  check('query + 骨架 = 详情页', info.type, 'detail');
  check('query + 骨架：ASIN 取 query 的', info.asin, 'B0PROMO001');
}

// ────────────────────────────────────────────────────────────
// 抽取范围：只要结果容器里的，不要页面上其它带 ASIN 的东西
// ────────────────────────────────────────────────────────────
//
// 这一组是**回归用例**：真机上搜 "Wall Cabinet"，页面自己写着
// 1-24 of over 40,000，插件却报"本页 81 个商品"。多出来的是顶部
// Sponsored Brands 横幅 + 底部三四条推荐轮播。它们会被当成搜索结果推给
// 服务端，每个烧一份代理配额采详情，而数据里分不出哪些是真结果。

{
  // 一个贴近真实布局的搜索页骨架：结果容器外有横幅和推荐轮播，
  // 容器内还夹了一条"相关商品"轮播（Amazon 真的会这么插）。
  const REALISTIC = `
    <div id="nav-belt"><a href="/Deal/dp/B0NAVLINK1">导航里的商品链接</a></div>

    <!-- 结果容器**之外**：顶部 Sponsored Brands 横幅 -->
    <div class="AdHolder s-widget-spacing-large">
      <div class="s-result-item" data-asin="B0BANNER01"><h2><span>Banner A</span></h2></div>
      <div class="s-result-item" data-asin="B0BANNER02"><h2><span>Banner B</span></h2></div>
    </div>

    <!-- 结果容器 -->
    <div class="s-main-slot s-result-list">
      <div class="s-result-item" data-asin="B0REAL0001"><h2><span>Real 1</span></h2></div>
      <div class="s-result-item" data-asin="B0REAL0002"><h2><span>Real 2</span></h2></div>
      <!-- Amazon 把推荐轮播插进结果区内部 -->
      <div class="a-carousel-container">
        <li class="a-carousel-card"><div data-asin="B0CAROU001"><h2><span>Carousel</span></h2></div></li>
      </div>
      <div class="s-result-item" data-asin="B0REAL0003"><h2><span>Real 3</span></h2></div>
      <a class="s-pagination-next" href="/s?k=x&page=2">Next</a>
    </div>

    <!-- 结果容器**之外**：底部 "Products related to this search" -->
    <div class="a-carousel-container">
      <li class="a-carousel-card"><div data-asin="B0RELATE01"></div></li>
      <li class="a-carousel-card"><div data-asin="B0RELATE02"></div></li>
    </div>
    <div><a href="/Other/dp/B0FOOTLNK1">页脚推荐链接</a></div>
  `;

  const d = docOf(`<html><body>${REALISTIC}</body></html>`);
  const items = P.collectListItems(d);
  check('限定容器：只留结果区内的自然位',
        items.map((i) => i.asin),
        ['B0REAL0001', 'B0REAL0002', 'B0REAL0003']);
  check('限定容器：rank 连续（轮播不占位）', items.map((i) => i.rank), [1, 2, 3]);
  check('限定容器：itemCount 与之一致',
        P.detectPage('https://www.amazon.com/s?k=wall+cabinet', d).itemCount, 3);
  check('限定容器：真的选中了 s-main-slot',
        P.resultsRoot(d).className.includes('s-main-slot'), true);
}

{
  // 没有结果容器的页面（榜单页 / 品牌旗舰店）必须退回整页扫描，
  // 否则那些页面会安静地变成"本页 0 个商品"。
  const d = docOf(`<html><body>
    <div data-csa-c-item-id="amzn1.asin.B0BEST0001"></div>
    <a href="/x/dp/B0BESTLINK">榜单里的链接</a>
  </body></html>`);
  check('无容器：退回整页扫描', P.collectListItems(d).map((i) => i.asin),
        ['B0BEST0001', 'B0BESTLINK']);
  check('无容器：resultsRoot 返回 doc 本身', P.resultsRoot(d) === d, true);
}

{
  // 计数必须拆成自然位 / 广告位。
  //
  // 真机上这条的表现：Amazon 页面顶部写 "1-24 of over 40,000"，插件报 32 ——
  // 差的 8 个是广告位（顶部 Sponsored Brands 横幅 + 网格里穿插的 Sponsored
  // 卡片），Amazon 那个 24 只数自然位。而推送时默认丢广告位，所以只报总数
  // 的话用户看到 32、实际入队 24，差额没处可查，跟"抓漏了"表现一样。
  const d = docOf(`<html><body><div class="s-main-slot">
    <div class="AdHolder"><div class="s-result-item" data-asin="B0ADBANNR1"></div></div>
    <div class="s-result-item" data-asin="B0NAT00001">
      <span class="puis-sponsored-label-text">Sponsored</span></div>
    <div class="s-result-item" data-asin="B0NAT00002"></div>
    <div class="s-result-item" data-asin="B0NAT00003"></div>
  </div></body></html>`);
  const info = P.detectPage('https://www.amazon.com/s?k=x', d);
  check('计数：总数含广告位', info.itemCount, 4);
  check('计数：自然位', info.naturalCount, 2);
  check('计数：广告位', info.sponsoredCount, 2);
  check('计数：三者自洽', info.naturalCount + info.sponsoredCount, info.itemCount);
}

{
  // Sponsored Brands 横幅走的是 AdHolder，没有那三种 label。
  // 只看 label 的话它会被当成自然位 —— 默认丢广告位时也丢不掉。
  const d = docOf(`<html><body><div class="s-main-slot">
    <div class="AdHolder"><div class="s-result-item" data-asin="B0ADHOLD01"></div></div>
    <div class="s-result-item" data-asin="B0NATURL01"></div>
  </div></body></html>`);
  const items = P.collectListItems(d);
  check('AdHolder 算广告位', items.map((i) => i.sponsored), [true, false]);
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

// ────────────────────────────────────────────────────────────
// 夹具自检：ASIN 字面量必须正好 10 位
// ────────────────────────────────────────────────────────────
//
// 这个坑踩过两次，两次的症状都是"用例莫名返回空数组"，而根因
// （夹具里写了 11 位）完全看不出来 —— page.js 正确地拒收了它们，
// 只是拒得很安静。与其下次再花十分钟查，不如让夹具自己报出来。
{
  //: **故意**不是 10 位的字面量，它们本身就是被测的反例。
  //  加新反例时要同步登记在这儿，否则本自检会把它当成手滑。
  const DELIBERATE_BAD = new Set([
    'B0TOOLONG123',   // 12 位 —— normalizeAsin 必须拒
    'B0ELEVENCH1',    // 11 位 —— 截断防护必须拒（不能截成前 10 位）
  ]);

  const src = require('fs').readFileSync(__filename, 'utf8');
  const bad = [...new Set((src.match(/\bB0[A-Z0-9]{5,}/g) || []))]
    .filter((a) => a.length !== 10 && !DELIBERATE_BAD.has(a));
  check('夹具里的 ASIN 都是 10 位（反例已登记的除外）', bad, []);
}

console.log(`${checks - failures}/${checks} 项通过`);
process.exit(failures ? 1 : 0);
