/**
 * content.js —— 跑在亚马逊页面上的那一半。
 *
 * 三件事：**读 DOM**、**翻页**、**页内 UI（悬浮球 + 右侧面板）**。
 * 所有网络请求、所有跨页状态都在 service worker（background.js）里，
 * 理由见那个文件的头注。
 *
 * 依赖 page.js 先注入（manifest 的 content_scripts.js 数组里排在前面），
 * 通过 `globalThis.AmzPage` 拿页面识别与抽取的纯函数。
 *
 * ──────────────────────────────────────────────────────────────
 * 为什么要做**实时**检测，而不是打开弹窗时算一次
 * ──────────────────────────────────────────────────────────────
 * 两个都实测踩到了：
 *
 *   1. Amazon 详情页的骨架是**异步渲染**的。打开页面就点插件，那时
 *      `#productTitle` 还不存在 —— 旧实现判成 unknown，要关掉弹窗重开
 *      才好。用户看到的是一个时灵时不灵的功能。
 *   2. Amazon 的翻页/筛选很多是 **pushState 导航**，不重新加载文档 ——
 *      content script 不会重新注入，页面类型和商品数变了但没人重算。
 *
 * 所以这里用 MutationObserver（DOM 变化）+ history 补丁（URL 变化）
 * 持续重算，结果缓存在 `lastInfo` 里，随时可取；变了就广播出去，
 * 面板和弹窗跟着刷新。
 */

(function () {
  'use strict';

  const P = globalThis.AmzPage;
  if (!P) {
    console.error('[amz-scraper] page.js 没加载，content script 停止');
    return;
  }

  // 同一个页面只注入一次。Amazon 偶尔会在 pushState 之后重跑注入逻辑，
  // 不设闸的话会出现两个悬浮球。
  if (window.__amzScraperInjected) return;
  window.__amzScraperInjected = true;

  const HOST_ID = 'amz-scraper-host';
  let lastInfo = null;
  let lastKey = '';

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

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // ────────────────────────────────────────────────────────────
  // 实时页面检测
  // ────────────────────────────────────────────────────────────

  /**
   * 识别结果的"指纹"。只有它变了才算页面真的变了。
   *
   * 不比对整个 info 对象是有意的：那里面 `itemCount` 会随懒加载一路往上跳，
   * 每跳一次都广播的话，面板会在页面滚动时疯狂重渲染。这几个键足以覆盖
   * 「用户看得见的变化」，其余的等下一次真变化再一起带出去。
   */
  function fingerprint(info) {
    return [info.type, info.asin, info.sellerId, info.itemCount,
            info.ready, info.keyword].join('|');
  }

  function detectNow() {
    try {
      return P.detectPage(location.href, document);
    } catch (e) {
      console.error('[amz-scraper] 页面识别失败', e);
      return null;
    }
  }

  /** 重新识别；变了就更新缓存并广播。返回最新的 info。 */
  function refresh(reason) {
    const info = detectNow();
    if (!info) return lastInfo;
    const key = fingerprint(info);
    if (key !== lastKey) {
      lastKey = key;
      lastInfo = info;
      broadcast(info, reason);
    } else {
      lastInfo = info;   // 指纹没变也要更新（itemCount 之类会悄悄涨）
    }
    return info;
  }

  /** 把最新识别结果推给面板 iframe 与已打开的弹窗。 */
  function broadcast(info, reason) {
    const panel = document.getElementById(HOST_ID);
    const frame = panel && panel.shadowRoot &&
                  panel.shadowRoot.querySelector('iframe');
    if (frame && frame.contentWindow) {
      // 面板是扩展自己的页面，targetOrigin 用扩展源而不是 '*' ——
      // 页面上任何脚本都能监听 window message，用 '*' 等于把识别结果
      // （含 ASIN / 卖家）广播给整个页面。
      try {
        frame.contentWindow.postMessage(
          { source: 'amz-scraper', type: 'PAGE_CHANGED', info, reason },
          chrome.runtime.getURL('').slice(0, -1)
        );
      } catch (e) { /* iframe 还没加载完，下次广播会补上 */ }
    }
    // 弹窗（工具栏那个）如果开着也告诉它一声。没开的话这条消息没人收，
    // 会走 lastError —— 读掉它，别让控制台刷警告。
    chrome.runtime.sendMessage(
      { type: 'PAGE_CHANGED_FROM_CONTENT', info },
      () => void chrome.runtime.lastError
    );
  }

  /**
   * 装上实时监听。
   *
   * MutationObserver 只看 `childList`/`subtree`，**不看 attributes** ——
   * Amazon 页面上属性变化（hover 态、懒加载图片的 src）每秒能有几百次，
   * 收下来只会让 debounce 永远不落地。
   */
  let watching = false;
  function watchPage() {
    // 本函数会被调用两次（readyState=interactive 一次、load 之后 boot() 再一次）。
    // 没有这道闸的话会装上两个 MutationObserver、两个轮询，并把 history
    // 补丁打两层 —— 每次页面变化都重复广播，而且卸不掉。
    if (watching) return;
    watching = true;

    let timer = null;
    const schedule = (reason) => {
      clearTimeout(timer);
      // 400ms debounce：Amazon 渲染详情页时 DOM 会连续变动几百次，
      // 每次都重算的话 CPU 会明显发热（detectPage 要扫全部 data-asin）。
      timer = setTimeout(() => refresh(reason), 400);
    };

    new MutationObserver(() => schedule('dom')).observe(document.documentElement, {
      childList: true,
      subtree: true,
    });

    // URL 变化：Amazon 的翻页/筛选很多走 pushState，不触发任何事件。
    // 补丁 history 是唯一能感知它的办法。
    let lastHref = location.href;
    const onUrlMaybeChanged = () => {
      if (location.href === lastHref) return;
      lastHref = location.href;
      // URL 变了通常意味着整块内容要换，等一拍再算，避免拿到上一页的 DOM
      setTimeout(() => refresh('url'), 250);
    };
    for (const m of ['pushState', 'replaceState']) {
      const orig = history[m];
      history[m] = function () {
        const r = orig.apply(this, arguments);
        onUrlMaybeChanged();
        return r;
      };
    }
    window.addEventListener('popstate', onUrlMaybeChanged);
    window.addEventListener('hashchange', onUrlMaybeChanged);

    // 兜底轮询：上面两条都漏掉的情况（比如 Amazon 用了 replaceState 的
    // 变体、或某些 widget 在 shadow root 里渲染，MutationObserver 看不见）。
    // 2 秒一次很便宜，而"面板信息停在上一页"这件事用户很难自己想明白。
    setInterval(() => refresh('poll'), 2000);
  }

  // ────────────────────────────────────────────────────────────
  // 页内 UI：悬浮球 + 右侧面板
  // ────────────────────────────────────────────────────────────

  /**
   * 整个 UI 挂在 **Shadow DOM** 里。
   *
   * 不这么做的话两边互相污染：Amazon 的全局 CSS 会把按钮样式冲掉
   * （它有 `button { ... }` 这种裸标签规则），而我们的样式也会漏进页面。
   * Shadow DOM 是唯一能同时挡住两个方向的办法。
   *
   * 面板内容是一个 **iframe 装 popup.html** —— 不重写一份 UI。
   * popup 已经是完整的操作界面，而且它跑在扩展页面上下文里，
   * `chrome.*` API 全都能用，与工具栏弹窗**逐字同一份代码**。
   * 复制一份的话，以后每加一个按钮都要改两处，漏掉的那处不会报错。
   */
  function mountUI() {
    if (document.getElementById(HOST_ID)) return;

    const host = document.createElement('div');
    host.id = HOST_ID;
    // 宿主本身不占位、不拦事件；里面的元素各自开 pointer-events。
    host.style.cssText = 'position:fixed;inset:0;z-index:2147483646;pointer-events:none;';
    const root = host.attachShadow({ mode: 'open' });

    root.innerHTML = `
      <style>
        :host { all: initial; }
        .ball, .panel { pointer-events: auto; font: 13px/1.5 -apple-system,
          BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
        .ball {
          position: fixed; right: 16px; top: 45%;
          width: 44px; height: 44px; border-radius: 50%;
          background: #2563eb; color: #fff; border: none; cursor: pointer;
          box-shadow: 0 2px 10px rgba(0,0,0,.28);
          display: flex; align-items: center; justify-content: center;
          font-size: 18px; transition: transform .15s, background .15s;
        }
        .ball:hover { transform: scale(1.08); background: #1d4ed8; }
        .ball .dot {
          position: absolute; top: -3px; right: -3px; min-width: 17px; height: 17px;
          border-radius: 9px; background: #0f9d58; color: #fff;
          font-size: 10px; line-height: 17px; text-align: center; padding: 0 4px;
          box-shadow: 0 0 0 2px #fff;
        }
        .ball .dot[hidden] { display: none; }
        .panel {
          position: fixed; top: 0; right: 0; height: 100vh; width: 360px;
          background: #fff; box-shadow: -2px 0 14px rgba(0,0,0,.18);
          display: flex; flex-direction: column;
          transform: translateX(100%); transition: transform .18s ease-out;
        }
        .panel.open { transform: translateX(0); }
        .panel-bar {
          display: flex; align-items: center; justify-content: space-between;
          padding: 6px 10px; background: #f3f4f6; border-bottom: 1px solid #e3e6ea;
          color: #1c1f23;
        }
        .panel-bar strong { font-size: 12px; font-weight: 600; }
        .panel-bar button {
          border: none; background: none; cursor: pointer; font-size: 16px;
          line-height: 1; color: #6b7280; padding: 2px 6px;
        }
        .panel-bar button:hover { color: #1c1f23; }
        .panel iframe { flex: 1; width: 100%; border: none; background: #fff; }
        @media (prefers-color-scheme: dark) {
          .panel { background: #1b1d20; }
          .panel-bar { background: #24272b; border-bottom-color: #34383d; color: #e8eaed; }
          .panel iframe { background: #1b1d20; }
        }
      </style>
      <button class="ball" title="Amazon Scraper 采集助手">
        <span>🔍</span><span class="dot" hidden></span>
      </button>
      <div class="panel">
        <div class="panel-bar">
          <strong>Amazon Scraper v4</strong>
          <button class="close" title="收起">✕</button>
        </div>
        <iframe src="${chrome.runtime.getURL('src/popup.html')}"
                allow="clipboard-write"></iframe>
      </div>
    `;

    document.documentElement.appendChild(host);

    const ball = root.querySelector('.ball');
    const panel = root.querySelector('.panel');
    const dot = root.querySelector('.dot');

    const setOpen = (open) => {
      panel.classList.toggle('open', open);
      ball.style.display = open ? 'none' : 'flex';
      // 面板一打开就把当前识别结果推过去，不用等下一次页面变化 ——
      // 否则刚展开的面板会空着，看起来像坏了。
      if (open) broadcast(lastInfo || refresh('open'), 'open');
      // 记住开合状态：翻页导航之后面板该自己回来，不然翻页采集途中
      // 每翻一页面板就消失一次。
      try { localStorage.setItem('amzScraperPanelOpen', open ? '1' : '0'); } catch (e) {}
    };

    ball.addEventListener('click', () => setOpen(true));
    root.querySelector('.close').addEventListener('click', () => setOpen(false));

    // 角标：显示待采队列长度，让用户不打开面板也知道攒了多少
    const syncDot = async () => {
      const r = await send('GET_QUEUE');
      const n = (r.ok && r.queue) ? r.queue.length : 0;
      dot.textContent = String(n);
      dot.hidden = n === 0;
    };
    syncDot();
    setInterval(syncDot, 3000);

    let wasOpen = false;
    try { wasOpen = localStorage.getItem('amzScraperPanelOpen') === '1'; } catch (e) {}
    if (wasOpen) setOpen(true);
  }

  // ────────────────────────────────────────────────────────────
  // 采集
  // ────────────────────────────────────────────────────────────

  /**
   * 亚马逊的搜索结果是懒加载的：直接读 DOM 只能拿到首屏那十几个。
   * 滚到底再读，才是这一页真正的全部结果。
   *
   * 分几次滚而不是一步到底，是因为 Amazon 的 IntersectionObserver 按
   * 视口位置触发，一步跳到底会**跳过**中间那些观察点，反而加载不全。
   */
  async function scrollToLoadAll() {
    const startY = window.scrollY;
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
    // 滚回用户原来的位置，而不是无脑回顶部 —— 面板常驻之后，
    // 用户往往是看着某个商品点的采集，把他弹回顶部很讨厌。
    window.scrollTo(0, startY);
    await sleep(120);
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
  // 面板 / 弹窗发来的请求
  // ────────────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.type === 'ANALYZE_PAGE') {
      // 用缓存里最新的那份；没有就现算一次。
      sendResponse({ ok: true, info: lastInfo || refresh('ask') });
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
    if (msg?.type === 'TOGGLE_PANEL') {
      const host = document.getElementById(HOST_ID);
      const panel = host && host.shadowRoot.querySelector('.panel');
      if (panel) {
        const open = !panel.classList.contains('open');
        panel.classList.toggle('open', open);
        host.shadowRoot.querySelector('.ball').style.display = open ? 'none' : 'flex';
        if (open) broadcast(lastInfo || refresh('open'), 'open');
      }
      sendResponse({ ok: true });
      return false;
    }
    return false;
  });

  // 面板 iframe 里的 popup 通过 postMessage 要一次刷新（它自己够不到 document）
  window.addEventListener('message', (ev) => {
    if (ev.data?.source !== 'amz-scraper-panel') return;
    if (ev.data.type === 'REQUEST_REFRESH') broadcast(refresh('panel-ask'), 'panel-ask');
    if (ev.data.type === 'CLOSE_PANEL') {
      const host = document.getElementById(HOST_ID);
      if (!host) return;
      host.shadowRoot.querySelector('.panel').classList.remove('open');
      host.shadowRoot.querySelector('.ball').style.display = 'flex';
      try { localStorage.setItem('amzScraperPanelOpen', '0'); } catch (e) {}
    }
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

  // ────────────────────────────────────────────────────────────
  // 启动
  // ────────────────────────────────────────────────────────────

  function boot() {
    refresh('boot');
    watchPage();
    mountUI();
    resumeRunIfAny();
  }

  // document_idle 时 Amazon 的结果网格通常已经在了；个别慢的情况下再等一拍。
  if (document.readyState === 'complete') {
    boot();
  } else {
    window.addEventListener('load', () => setTimeout(boot, 300), { once: true });
    // 别干等 load：Amazon 的图片/广告能把 load 拖到好几秒，
    // 而那时页面主体早就能用了。先挂上 UI 与监听，load 之后再补一次。
    if (document.readyState === 'interactive') {
      refresh('interactive');
      watchPage();
      mountUI();
    }
  }
})();
