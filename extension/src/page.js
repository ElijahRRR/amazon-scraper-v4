/**
 * page.js —— 页面识别 + 数据抽取（纯函数层，无 chrome.* 调用）
 *
 * 作为 content script 的**第一个**文件注入，把 `window.AmzPage` 挂上去供
 * content.js 用。写成纯函数是为了能在 Node 里直接跑单测
 * （tests/extension/ 那套用 jsdom 灌 HTML 进来断言），不必起浏览器。
 *
 * ──────────────────────────────────────────────────────────────
 * 页面类型的判定顺序是**承重的**
 * ──────────────────────────────────────────────────────────────
 * 卖家店铺页 `/s?me=A2L77EE7U53NWQ` 同时也是一个商品列表页 —— DOM 完全一样，
 * 都是 `s-result-item` 网格。所以必须**先判卖家、再判列表**：
 * 反过来的话卖家页会被认成普通列表，"采集该卖家全部商品"这个入口
 * 永远不出现，而用户看到的是一个能正常工作的列表采集，不会觉得哪里不对。
 *
 * 详情页排在最前：`/dp/ASIN` 是无歧义的，且详情页上也有
 * "相关商品" 之类的 `[data-asin]` 卡片，先判列表会把详情页认成列表。
 */

(function () {
  'use strict';

  /**
   * 与服务端 `common/core/idents.py:ASIN_RE` **同一条规则**。
   *
   * 分叉的后果是静默丢数据：插件认得、服务端不认（反之亦然）的 ASIN
   * 会在推送时被服务端悄悄丢掉，插件这边显示"已推送 N 个"，实际入队的更少。
   * 服务端那条是 `^B[0-9A-Z]{9}$`，这里逐字对齐（含只认大写这一点，
   * 归一化同样放在调用侧）。
   */
  const ASIN_RE = /^B[0-9A-Z]{9}$/;

  /** 卖家 ID：A 开头的 13-14 位串。与 server/api/sellers.py 的 _BARE_SELLER_RE 对齐。 */
  const SELLER_ID_RE = /^A[A-Z0-9]{12,14}$/;

  /** 从任意 URL / href 里抠卖家 ID（`me=` 与 `seller=` 两种参数名）。 */
  const SELLER_PARAM_RE = /[?&](?:me|seller|isAmazonFulfilled|merchant)=([A-Z0-9]{10,16})/i;

  /** Amazon 自营的 merchant ID（美国站）。整店采集对它没意义，见下。 */
  const AMAZON_SELF_MERCHANTS = new Set([
    'ATVPDKIKX0DER',   // amazon.com
    'A3P5ROKL5A1OLE',  // amazon.com（部分类目）
  ]);

  const PAGE_TYPES = {
    DETAIL: 'detail',
    SELLER: 'seller',
    LIST: 'list',
    UNKNOWN: 'unknown',
  };

  function normalizeAsin(raw) {
    if (!raw) return null;
    const s = String(raw).trim().toUpperCase();
    return ASIN_RE.test(s) ? s : null;
  }

  function normalizeSellerId(raw) {
    if (!raw) return null;
    const s = String(raw).trim().toUpperCase();
    return SELLER_ID_RE.test(s) ? s : null;
  }

  // ────────────────────────────────────────────────────────────
  // 页面类型识别
  // ────────────────────────────────────────────────────────────

  /** URL 路径里的详情页 ASIN。覆盖 Amazon 现役的 5 种详情页路径形态。 */
  function asinFromUrl(url) {
    // ⚠ 每条都以 `(?![A-Z0-9])` 收尾。少了它，一个 11 位的串会被**静默截成**
    //   前 10 位当作 ASIN —— 那个截出来的东西格式完全合法（B 开头 + 9 位），
    //   验不出来，采回来的是另一个商品或者干脆 404。
    const patterns = [
      /\/dp\/([A-Z0-9]{10})(?![A-Z0-9])/i,          // /dp/B0XXXXXXXX 与 /<slug>/dp/B0XXXXXXXX
      /\/gp\/product\/([A-Z0-9]{10})(?![A-Z0-9])/i, // 老式详情页
      /\/gp\/aw\/d\/([A-Z0-9]{10})(?![A-Z0-9])/i,   // 移动版
      /\/product\/([A-Z0-9]{10})(?![A-Z0-9])/i,
      /\/ASIN\/([A-Z0-9]{10})(?![A-Z0-9])/i,
    ];
    for (const re of patterns) {
      const m = url.match(re);
      if (m) {
        const a = normalizeAsin(m[1]);
        if (a) return a;
      }
    }
    return null;
  }

  /** URL 里的卖家 ID（店铺页 `/s?me=`、卖家资料页 `/sp?seller=`）。 */
  function sellerIdFromUrl(url) {
    const m = url.match(SELLER_PARAM_RE);
    return m ? normalizeSellerId(m[1]) : null;
  }

  /**
   * 判定当前页面类型。
   *
   * 返回 { type, asin, sellerId, sellerName, hasGrid, isAmazonSelf, ... }
   *
   * `doc` 显式传进来（而不是直接摸全局 document）是为了能在 jsdom 下测。
   */
  function detectPage(url, doc) {
    const out = {
      type: PAGE_TYPES.UNKNOWN,
      url,
      asin: null,
      title: '',
      sellerId: null,
      sellerName: null,
      hasGrid: false,
      hasNextPage: false,
      // itemCount 是结果区里的**全部**商品卡片，含广告位。
      // natural/sponsored 是它的拆分 —— 弹窗必须按拆分显示，理由见 countGridItems。
      itemCount: 0,
      naturalCount: 0,
      sponsoredCount: 0,
      isAmazonSelf: false,
      keyword: null,
      // 页面骨架是否已渲染。只对详情页有意义（列表页有网格就说明已渲染）。
      ready: true,
    };

    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return out;
    }

    out.hasGrid = doc.querySelectorAll('div[data-asin], [data-component-type="s-search-result"]').length > 0;
    out.hasNextPage = hasNextPage(doc);

    // ── 1) 详情页 ──────────────────────────────────────────────
    //
    // ⚠ **pathname 与 search 分两档判**，这是承重的：
    //
    //   pathname 里的 `/dp/ASIN` 是**无歧义**的 —— 只有详情页会长这样。
    //   所以它一命中就直接认，不等 DOM。
    //
    //   search 里匹配到的则**必须**有 DOM 佐证：搜索结果页的 URL 上
    //   （`ref=`、`url=` 之类参数里）也会出现 `/dp/XXXX`，只看 URL 会误判。
    //
    // 合并成一个判据（旧写法：`(urlAsin || domAsin) && looksDetail`）的后果
    // 实测踩到了：Amazon 详情页的 `#dp` / `#productTitle` 是**异步渲染**的，
    // 页面刚打开时还不存在 —— 这时点插件会显示"未识别到页面"，关掉重开
    // 才好。用户看到的是一个时灵时不灵的功能，而不是"还没加载完"。
    const pathAsin = asinFromUrl(u.pathname);
    const queryAsin = pathAsin ? null : asinFromUrl(u.search);
    const domAsin = normalizeAsin(
      (doc.querySelector('#ASIN') || {}).value ||
      (doc.querySelector('input#ASIN') || {}).value ||
      (doc.querySelector('[data-asin][data-component-type="s-product-image"]') || {}).getAttribute?.('data-asin')
    );
    // `#dp` / `#dp-container` 是详情页骨架的稳定锚点。
    const looksDetail = !!doc.querySelector('#dp, #dp-container, #productTitle, #centerCol');
    const urlAsin = pathAsin || queryAsin;
    if (pathAsin || ((queryAsin || domAsin) && looksDetail)) {
      out.type = PAGE_TYPES.DETAIL;
      out.asin = urlAsin || domAsin;
      // 详情页骨架渲染好了没有。false = 认出来了但页面还在加载，
      // 标题/卖家可能暂时是空的 —— 界面据此显示"加载中"而不是"读不到"。
      out.ready = looksDetail;
      const t = doc.querySelector('#productTitle, #title');
      out.title = t ? (t.textContent || '').trim().replace(/\s+/g, ' ') : '';
      const seller = extractDetailSeller(doc);
      out.sellerId = seller.sellerId;
      out.sellerName = seller.sellerName;
      out.isAmazonSelf = seller.isAmazonSelf;
      return out;
    }

    // ── 2) 卖家店铺页（必须排在列表之前，见文件头注）────────────
    const sellerId = sellerIdFromUrl(u.search) || sellerIdFromDom(doc);
    if (sellerId) {
      out.type = PAGE_TYPES.SELLER;
      out.sellerId = sellerId;
      out.sellerName = sellerNameFromDom(doc);
      out.isAmazonSelf = AMAZON_SELF_MERCHANTS.has(sellerId);
      applyCounts(out, countGridItems(doc));
      return out;
    }

    // ── 3) 商品列表页 ──────────────────────────────────────────
    // 路径白名单 **或** 页面上真有结果网格。两个判据是 OR 而不是 AND：
    //   * 只看路径 -> 品牌旗舰店 /stores/... 这类新路径漏判；
    //   * 只看网格 -> 详情页的"相关商品"卡片会误判（已被上面第 1 步拦掉，
    //     但 Best Sellers 之类**没有** s-result-item 的榜单页也会漏判）。
    const listPaths = [
      '/s', '/b', '/gp/bestsellers', '/gp/new-releases', '/gp/movers-and-shakers',
      '/gp/most-wished-for', '/gp/browse.html', '/stores/', '/deal/', '/deals',
      '/gp/search',
    ];
    const pathHit = listPaths.some((p) => u.pathname === p || u.pathname.startsWith(p));
    if (pathHit || out.hasGrid) {
      out.type = PAGE_TYPES.LIST;
      out.keyword = u.searchParams.get('k') || u.searchParams.get('keywords') || null;
      applyCounts(out, countGridItems(doc));
      return out;
    }

    return out;
  }

  /** 把 countGridItems 的拆分摊进 detectPage 的返回值。 */
  function applyCounts(out, counts) {
    out.itemCount = counts.total;
    out.naturalCount = counts.natural;
    out.sponsoredCount = counts.sponsored;
  }

  // ────────────────────────────────────────────────────────────
  // 抽取：列表 / 卖家页的 ASIN 网格
  // ────────────────────────────────────────────────────────────

  //: 搜索结果的容器。按优先级依次尝试，命中第一个就把抽取限定在它里面。
  //
  //  为什么必须限定：整页扫描会把顶部 Sponsored Brands 横幅、底部
  //  "Products related to this search" / "Best Sellers" / "Customers also
  //  viewed" 全部算成"本页商品"。实测搜索 "Wall Cabinet"，页面自己写着
  //  1-24，整页扫描得到 81 —— 三倍的噪音，而且推给服务端之后每个都要烧
  //  一份代理配额采详情，数据里还分不出哪些是真结果。
  //
  //  ⚠ 找不到任何一个容器时**不报错也不返回空**，退回整页扫描：榜单页
  //  （/gp/bestsellers）、品牌旗舰店（/stores/）没有 s-main-slot，那时整页
  //  扫描才是对的。代价是那些页面上的噪音仍在，但那些页面本来也没有
  //  "结果区"这个概念。
  const RESULTS_ROOT_SELECTORS = [
    'div.s-main-slot.s-result-list',            // 搜索页主结果列表（最精确）
    '[data-component-type="s-search-results"]', // 新版包裹层
    'div.s-main-slot',                          // 经典容器
    'span.rush-component[data-component-type="s-search-results"]',
    '.s-search-results',
  ];

  //: 横向滑动的推荐条。Amazon 会把它们**插进结果区内部**，所以即使限定了
  //  容器也要再排掉一层。
  const CAROUSEL_SELECTOR = [
    '.a-carousel',
    '.a-carousel-container',
    '[data-a-carousel-options]',
    '[cel_widget_id*="carousel" i]',
    '[data-cel-widget*="carousel" i]',
  ].join(',');

  /** 结果容器；找不到就返回 doc 本身（调用方据此决定要不要跑兜底扫描）。 */
  function resultsRoot(doc) {
    for (const sel of RESULTS_ROOT_SELECTORS) {
      const el = doc.querySelector(sel);
      if (el) return el;
    }
    return doc;
  }

  /** 判断一个结果卡片是不是广告位。与 worker/parser.py 的三条选择器一一对应。 */
  function isSponsoredCard(node) {
    // 卡片自身带 AdHolder：顶部 Sponsored Brands 横幅走的是这条 ——
    // 它**没有**下面那三种 label，只看 label 的话它会被当成自然位，
    // 于是广告商品混进排名数据，而且默认丢弃广告位时也丢不掉它。
    if (node.classList && node.classList.contains('AdHolder')) return true;
    if (node.closest && node.closest('.AdHolder')) return true;
    if (node.querySelector('[data-component-type="sp-sponsored-result"]')) return true;
    if (node.querySelector('.puis-sponsored-label-text, .s-sponsored-label-text')) return true;
    // 兜底：部分布局只有一个纯文本 "Sponsored" 标签，没有专用 class。
    const label = node.querySelector('.puis-label-popover-default, .a-color-secondary');
    if (label && /^\s*sponsored\s*$/i.test(label.textContent || '')) return true;
    return false;
  }

  /**
   * 结果区商品数的**拆分**：{ total, natural, sponsored }。
   *
   * ⚠ 必须拆开给弹窗看，不能只报 total ——
   *
   *   Amazon 页面顶部写的 "1-24 of over 40,000" 只数**自然位**；结果区里
   *   还夹着广告位（顶部 Sponsored Brands 横幅 + 网格里穿插的 Sponsored
   *   卡片），它们不计入那个 24，但确实在这一页上。所以 total 比页面上
   *   那个数大是**正常的**，不是抓多了。
   *
   *   而推送时默认**丢弃广告位**（`include_sponsored` 开关）。只报 total
   *   的话，用户看到 32、实际入队 24，差额没有任何地方能看出来 ——
   *   跟"抓漏了 8 个"的表现完全一样，没法自证清白。
   */
  function countGridItems(doc) {
    const items = collectListItems(doc);
    const sponsored = items.filter((it) => it.sponsored).length;
    return { total: items.length, natural: items.length - sponsored, sponsored };
  }

  /**
   * 抓当前页所有商品卡片。
   *
   * 返回 [{ asin, title, price, image, sponsored, rank }]，rank 是**页内**序号
   * （1 起），与服务端 `search_discoveries.rank` 同语义。
   *
   * **抽取范围先被限定在结果容器内**（见 RESULTS_ROOT_SELECTORS），
   * 容器内再排掉轮播（CAROUSEL_SELECTOR）。两条来源合并去重：
   *   1. `div[data-asin]`       —— 搜索/店铺网格的主力
   *   2. `[data-csa-c-item-id]` —— 榜单页（Best Sellers）用这个，没有 data-asin
   * 外加一条**仅在没找到容器时**才跑的兜底：`a[href*="/dp/"]`。
   */
  function collectListItems(doc) {
    const root = resultsRoot(doc);
    const scoped = root !== doc;
    const seen = new Set();
    const items = [];

    const push = (asin, node) => {
      const a = normalizeAsin(asin);
      if (!a || seen.has(a)) return;
      // 轮播卡片一律不要：Amazon 会把"相关商品"/"看了又看"这类横向滑动条
      // **插进结果区内部**，它们不是这一页的搜索结果。
      if (node && node.closest && node.closest(CAROUSEL_SELECTOR)) return;
      seen.add(a);
      items.push({
        asin: a,
        title: node ? textOf(node, 'h2 span, h2 a span, .p13n-sc-truncate, [class*="truncate"]') : '',
        price: node ? textOf(node, '.a-price[data-a-color="base"] .a-offscreen, .a-price .a-offscreen') : '',
        image: node ? (node.querySelector('img.s-image, img[data-image-index], img') || {}).src || '' : '',
        sponsored: node ? isSponsoredCard(node) : false,
        rank: items.length + 1,
      });
    };

    root.querySelectorAll('div[data-asin]').forEach((n) => push(n.getAttribute('data-asin'), n));

    root.querySelectorAll('[data-csa-c-item-id]').forEach((n) => {
      // 形如 amzn1.asin.B0XXXXXXXX
      const m = (n.getAttribute('data-csa-c-item-id') || '').match(/asin\.([A-Z0-9]{10})(?![A-Z0-9])/i);
      if (m) push(m[1], n);
    });

    // ⚠ 裸链接扫描**只在找不到结果容器时**才跑，而且是**兜底**不是补充。
    //
    // 它曾经是无条件跑的，后果实测：搜索 "Wall Cabinet"（页面自己写着
    // 1-24 of over 40,000）插件报"本页 81 个商品" —— 多出来的 57 个是顶部
    // Sponsored Brands 横幅、底部"Products related to this search" /
    // "Best Sellers" / "Customers also viewed" 一堆推荐轮播里的商品。
    // 它们会被当成这次搜索的结果推给服务端，每个烧一份代理配额采详情，
    // 而数据里没有任何一列能把它们和真正的搜索结果区分开。
    //
    // 有容器时它一条都不该跑：容器内的东西两条 data 来源已经全覆盖了。
    if (!scoped) {
      doc.querySelectorAll('a[href*="/dp/"]').forEach((a) => {
        const m = (a.getAttribute('href') || '').match(/\/dp\/([A-Z0-9]{10})(?![A-Z0-9])/i);
        // 卡片容器优先，取不到就用链接本身（标题/价格会空，ASIN 仍然是对的）
        if (m) push(m[1], a.closest('div[data-asin], .a-carousel-card, li') || a);
      });
    }

    // rank = 插入顺序，也就是结果容器内的 DOM 顺序 —— 真实搜索名次。
    return items;
  }

  function textOf(node, selector) {
    const el = node.querySelector(selector);
    return el ? (el.textContent || '').trim().replace(/\s+/g, ' ') : '';
  }

  // ────────────────────────────────────────────────────────────
  // 抽取：详情页的卖家
  // ────────────────────────────────────────────────────────────

  /**
   * 详情页上的卖家 ID / 名称。
   *
   * 拿不到时返回 { sellerId: null }，**不猜**。调用方据此把"采集该卖家全部
   * 商品"这个勾选禁用掉并说明原因 —— 猜一个错的卖家会让用户采回一整店
   * 无关商品，而且完全看不出是错的。
   */
  function extractDetailSeller(doc) {
    const out = { sellerId: null, sellerName: null, isAmazonSelf: false };

    // 1) 最稳的锚点：buybox 里的卖家链接
    const linkSelectors = [
      '#sellerProfileTriggerId',
      '#merchant-info a[href*="seller="]',
      '#tabular-buybox a[href*="seller="]',
      '#buybox a[href*="seller="]',
      '#offer-display-features a[href*="seller="]',
      'a[href*="/sp?"][href*="seller="]',
      'a[href*="seller="]',
    ];
    for (const sel of linkSelectors) {
      const el = doc.querySelector(sel);
      if (!el) continue;
      const sid = normalizeSellerId((el.getAttribute('href') || '').match(SELLER_PARAM_RE)?.[1]);
      if (sid) {
        out.sellerId = sid;
        out.sellerName = (el.textContent || '').trim() || null;
        break;
      }
    }

    // 2) 兜底：整个 buybox 区块的 HTML 里扫一遍 seller=
    if (!out.sellerId) {
      const scope = doc.querySelector('#merchant-info, #tabular-buybox, #buybox, #centerCol');
      if (scope) {
        const sid = normalizeSellerId((scope.innerHTML || '').match(SELLER_PARAM_RE)?.[1]);
        if (sid) out.sellerId = sid;
      }
    }

    // 3) Amazon 自营：没有卖家链接，只有 "Ships from and sold by Amazon.com" 文案。
    //    这一支只用来给出**提示**，不用来伪造一个 sellerId：
    //    Amazon 自营店有数百万商品，整店采集不是用户想要的东西。
    if (!out.sellerId) {
      const info = doc.querySelector('#merchant-info, #tabular-buybox, #sellerProfileTriggerId');
      const txt = (info?.textContent || '').replace(/\s+/g, ' ');
      if (/sold by amazon(\.[a-z.]+)?\b/i.test(txt) || /亚马逊/.test(txt)) {
        out.isAmazonSelf = true;
        out.sellerName = 'Amazon';
      }
    } else if (AMAZON_SELF_MERCHANTS.has(out.sellerId)) {
      out.isAmazonSelf = true;
    }

    return out;
  }

  function sellerIdFromDom(doc) {
    const el = doc.querySelector('#sellerProfileTriggerId, a[href*="/s?me="], a[href*="seller="]');
    if (!el) return null;
    return normalizeSellerId((el.getAttribute('href') || '').match(SELLER_PARAM_RE)?.[1]);
  }

  function sellerNameFromDom(doc) {
    const el = doc.querySelector('#sellerName, .a-spacing-none h1, #s-all-results h1, [data-testid="seller-name"]');
    const t = el ? (el.textContent || '').trim() : '';
    return t || null;
  }

  // ────────────────────────────────────────────────────────────
  // 翻页
  // ────────────────────────────────────────────────────────────

  /** 下一页链接（可点、且没被置灰）。没有就返回 null。 */
  function nextPageLink(doc) {
    const a = doc.querySelector('a.s-pagination-next');
    if (a && !(a.className || '').includes('s-pagination-disabled') && a.href) return a.href;
    // 榜单页/老版分页的兜底
    const alt = doc.querySelector('li.a-last:not(.a-disabled) a, a[aria-label*="Next"], a[title="Next page"]');
    if (alt && alt.href) return alt.href;
    return null;
  }

  function hasNextPage(doc) {
    return !!nextPageLink(doc);
  }

  // 挂到 globalThis 而不是 window：content script 里两者等价，但单测在
  // 纯 Node（无 jsdom 全局）下跑时只有 globalThis，写 window 会当场 ReferenceError。
  const AmzPage = {
    PAGE_TYPES,
    ASIN_RE,
    SELLER_ID_RE,
    AMAZON_SELF_MERCHANTS,
    RESULTS_ROOT_SELECTORS,
    CAROUSEL_SELECTOR,
    resultsRoot,
    normalizeAsin,
    normalizeSellerId,
    asinFromUrl,
    sellerIdFromUrl,
    detectPage,
    collectListItems,
    isSponsoredCard,
    extractDetailSeller,
    nextPageLink,
    hasNextPage,
  };

  globalThis.AmzPage = AmzPage;

  // Node 下（单元测试）也导出一份。浏览器里没有 module，判断一下免得报错。
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = AmzPage;
  }
})();
