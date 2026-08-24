/**
 * content.js —— 跑在亚马逊页面上的那一半。
 *
 * 只做两件事：**读 DOM** 和 **翻页**。所有网络请求、所有跨页状态都在
 * service worker（background.js）里，理由见那个文件的头注。
 *
 * 依赖 page.js 先注入（manifest 的 content_scripts.js 数组里排在前面），
 * 通过 `globalThis.AmzPage` 拿页面识别与抽取的纯函数。
 */

(function () {
  'use strict';

  const P = globalThis.AmzPage;
  if (!P) {
    console.error('[amz-scraper] page.js 没加载，content script 停止');
    return;
  }

  function send(type, payload = {}) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type, ...payload }, (resp) => {
        // service worker 被回收/重启时 sendMessage 会走 lastError 而不是抛异常。
        // 不读一下 lastError 的话，Chrome 会在控制台刷 "Unchecked runtime.lastError"，
        // 而调用方拿到的是 undefined —— 下游一路 TypeError，看不出根因。
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
          return;
        }
        resolve(resp || { ok: false, error: '后台无响应' });
      });
    });
  }

  /**
   * 亚马逊的搜索结果是懒加载的：直接读 DOM 只能拿到首屏那十几个。
   * 滚到底再读，才是这一页真正的全部结果。
   *
   * 分几次滚而不是一步到底，是因为 Amazon 的 IntersectionObserver 按
   * 视口位置触发，一步跳到底会**跳过**中间那些观察点，反而加载不全。
   */
  async function scrollToLoadAll() {
    const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
    let lastCount = -1;
    for (let i = 0; i < 25; i++) {
      window.scrollBy(0, step);
      await sleep(180);
      const count = document.querySelectorAll('div[data-asin]').length;
      const atBottom = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 50);
      if (atBottom && count === lastCount) break;
      lastCount = count;
    }
    window.scrollTo(0, 0);
    await sleep(120);
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  /** 当前页的采集结果（已按设置过滤广告位）。 */
  async function collectCurrentPage(includeSponsored) {
    await scrollToLoadAll();
    const items = P.collectListItems(document);
    const kept = includeSponsored ? items : items.filter((it) => !it.sponsored);
    return {
      items: kept,
      asins: kept.map((it) => it.asin),
      sponsoredSkipped: items.length - kept.length,
      hasNext: P.hasNextPage(document),
      nextUrl: P.nextPageLink(document),
    };
  }

  // ────────────────────────────────────────────────────────────
  // popup 发来的即时请求
  // ────────────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.type === 'ANALYZE_PAGE') {
      const info = P.detectPage(location.href, document);
      sendResponse({ ok: true, info });
      return false;
    }
    if (msg?.type === 'COLLECT_NOW') {
      collectCurrentPage(!!msg.includeSponsored)
        .then((r) => sendResponse({ ok: true, ...r }))
        .catch((e) => sendResponse({ ok: false, error: e.message }));
      return true;
    }
    if (msg?.type === 'KICK_RUN') {
      // popup 刚起了一个翻页任务，但当前这一页早就 load 完了 ——
      // 下面那个 load 监听不会再触发。没有这一脚的话，第一页要等用户
      // 手动刷新才会被采，表现为"点了开始却什么都没发生"。
      resumeRunIfAny()
        .then(() => sendResponse({ ok: true }))
        .catch((e) => sendResponse({ ok: false, error: e.message }));
      return true;
    }
    return false;
  });

  // ────────────────────────────────────────────────────────────
  // 翻页任务：每页加载完自动报到
  // ────────────────────────────────────────────────────────────

  async function resumeRunIfAny() {
    const ready = await send('CONTENT_READY');
    if (!ready.ok || !ready.shouldCollect) return;

    const settingsResp = await send('GET_SETTINGS');
    const includeSponsored = !!settingsResp?.settings?.includeSponsored;

    const page = await collectCurrentPage(includeSponsored);
    const verdict = await send('PAGE_COLLECTED', {
      asins: page.asins,
      hasNext: page.hasNext,
      pageUrl: location.href,
    });

    if (!verdict.ok || verdict.next !== 'continue') return;

    // 翻页前等一下：连着打 Amazon 会很快吃到验证码，那会让整个任务从
    // "慢一点" 变成 "这一页开始全是空的"。延迟可在选项页调。
    await sleep(verdict.delayMs || 2500);

    const nextUrl = page.nextUrl || P.nextPageLink(document);
    if (!nextUrl) {
      // 到这一步还没有下一页链接，说明上面那次判断和现在的 DOM 对不上
      // （懒加载把分页控件换掉了）。如实报个 hasNext=false 让后台收尾，
      // 而不是静默停住 —— 静默停住的话 popup 上那个任务会永远显示"进行中"。
      await send('PAGE_COLLECTED', { asins: [], hasNext: false, pageUrl: location.href });
      return;
    }
    location.href = nextUrl;
  }

  // document_idle 时 Amazon 的结果网格通常已经在了；个别慢的情况下再等一拍。
  if (document.readyState === 'complete') {
    resumeRunIfAny();
  } else {
    window.addEventListener('load', () => setTimeout(resumeRunIfAny, 300), { once: true });
  }
})();
