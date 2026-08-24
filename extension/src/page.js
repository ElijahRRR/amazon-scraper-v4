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
      itemCount: 0,
      isAmazonSelf: false,
      keyword: null,
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
    // URL 是首要判据；`#ASIN` 隐藏输入是兜底（有些促销落地页的路径不带 /dp/）。
    const urlAsin = asinFromUrl(u.pathname + u.search);
    const domAsin = normalizeAsin(
      (doc.querySelector('#ASIN') || {}).value ||
      (doc.querySelector('input#ASIN') || {}).value ||
      (doc.querySelector('[data-asin][data-component-type="s-product-image"]') || {}).getAttribute?.('data-asin')
    );
    // `#dp` / `#dp-container` 是详情页骨架的稳定锚点；只靠 URL 的话，
    // 搜索结果页里带 /dp/ 的**跳转链接**在某些 A/B 分桶下也会进 URL。
    const looksDetail = !!doc.querySelector('#dp, #dp-container, #productTitle, #centerCol');
    if ((urlAsin || domAsin) && looksDetail) {
      out.type = PAGE_TYPES.DETAIL;
      out.asin = urlAsin || domAsin;
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
      out.itemCount = countGridItems(doc);
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
      out.itemCount = countGridItems(doc);
      return out;
    }

    return out;
  }

  // ────────────────────────────────────────────────────────────
  // 抽取：列表 / 卖家页的 ASIN 网格
  // ────────────────────────────────────────────────────────────

  /** 判断一个结果卡片是不是广告位。与 worker/parser.py 的三条选择器一一对应。 */
  function isSponsoredCard(node) {
    if (node.querySelector('[data-component-type="sp-sponsored-result"]')) return true;
    if (node.querySelector('.puis-sponsored-label-text, .s-sponsored-label-text')) return true;
    // 兜底：部分布局只有一个纯文本 "Sponsored" 标签，没有专用 class。
    const label = node.querySelector('.puis-label-popover-default, .a-color-secondary');
    if (label && /^\s*sponsored\s*$/i.test(label.textContent || '')) return true;
    return false;
  }

  function countGridItems(doc) {
    return collectListItems(doc).length;
  }

  /**
   * 抓当前页所有商品卡片。
   *
   * 返回 [{ asin, title, price, image, sponsored, rank }]，rank 是**页内**序号
   * （1 起），与服务端 `search_discoveries.rank` 同语义。
   *
   * 三条来源合并去重，因为 Amazon 在不同布局下只满足其中一条：
   *   1. `div[data-asin]`          —— 搜索/店铺网格的主力
   *   2. `[data-csa-c-item-id]`    —— 榜单页（Best Sellers）用这个，没有 data-asin
   *   3. `a[href*="/dp/"]`         —— 轮播/推荐位的兜底
   * 少一条就会在对应的页面类型上安静地采到 0 个。
   */
  function collectListItems(doc) {
    const seen = new Set();
    const items = [];

    const push = (asin, node) => {
      const a = normalizeAsin(asin);
      if (!a || seen.has(a)) return;
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

    doc.querySelectorAll('div[data-asin]').forEach((n) => push(n.getAttribute('data-asin'), n));

    doc.querySelectorAll('[data-csa-c-item-id]').forEach((n) => {
      // 形如 amzn1.asin.B0XXXXXXXX
      const m = (n.getAttribute('data-csa-c-item-id') || '').match(/asin\.([A-Z0-9]{10})(?![A-Z0-9])/i);
      if (m) push(m[1], n);
    });

    doc.querySelectorAll('a[href*="/dp/"]').forEach((a) => {
      const m = (a.getAttribute('href') || '').match(/\/dp\/([A-Z0-9]{10})(?![A-Z0-9])/i);
      // 卡片容器优先，取不到就用链接本身（标题/价格会空，ASIN 仍然是对的）
      if (m) push(m[1], a.closest('div[data-asin], .a-carousel-card, li') || a);
    });

    // rank = 插入顺序。第一条来源 `div[data-asin]` 是 querySelectorAll 的
    // **DOM 顺序**，也就是真实搜索名次；后两条来源补上的那些（轮播、榜单卡片）
    // 排在其后，本来也不属于主网格的名次序列。
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
