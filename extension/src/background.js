/**
 * background.js —— MV3 service worker：设置、待采队列、翻页任务的状态机、
 * 以及**所有**对 Server 的 HTTP 调用。
 *
 * ──────────────────────────────────────────────────────────────
 * 为什么 Server 调用必须全部走这里，而不是 content script 直接 fetch
 * ──────────────────────────────────────────────────────────────
 * content script 跑在 `https://www.amazon.com` 这个源上，直接 fetch 自建
 * Server 是跨源请求，会被 CORS 拦下 —— 而且拦下的形式是浏览器控制台里一条
 * 与业务无关的报错，用户只会看到"点了没反应"。
 *
 * service worker 的 fetch 走扩展自己的源，配合 `host_permissions` 里
 * 用户授权过的 Server 地址就不受 CORS 约束。所以分工是：
 *   content script  只负责读 DOM 和翻页（它是唯一能碰页面的地方）
 *   service worker  只负责存状态和发请求（它是唯一能跨源的地方）
 *
 * ──────────────────────────────────────────────────────────────
 * 翻页为什么是个状态机，而不是一个 for 循环
 * ──────────────────────────────────────────────────────────────
 * 翻页 = 页面导航，而导航会**销毁 content script 的整个执行环境**。
 * 一个跨页的 for 循环根本活不到第二页。所以进度只能存在 content script
 * 之外：`chrome.storage.local` 里的 `activeRun`。每张新页面加载完，
 * content script 报到一次，service worker 查"这个 tab 上有没有在跑的任务"，
 * 有就让它继续采、采完再翻。
 *
 * service worker 自己也随时会被浏览器回收（MV3 没有常驻后台），所以状态
 * **不能**放在模块级变量里 —— 必须每次从 storage 读。下面所有状态访问都是
 * 现读现写，看着啰嗦，但那是 MV3 唯一正确的写法。
 */

const DEFAULTS = {
  serverUrl: '',
  zipCode: '',
  needsScreenshot: false,
  includeSponsored: false,
  autoPaginate: false,
  maxPages: 5,
  pageDelayMs: 2500,
  sellerDiscoverMode: 'with_detail',
  adminToken: '',
};

// ──────────────────────────────────────────────────────────────
// 存储helpers（现读现写，见文件头注）
// ──────────────────────────────────────────────────────────────

async function getSettings() {
  const stored = await chrome.storage.local.get('settings');
  return { ...DEFAULTS, ...(stored.settings || {}) };
}

async function setSettings(patch) {
  const current = await getSettings();
  const next = { ...current, ...patch };
  await chrome.storage.local.set({ settings: next });
  return next;
}

async function getQueue() {
  const stored = await chrome.storage.local.get('queue');
  return stored.queue || [];
}

async function setQueue(queue) {
  await chrome.storage.local.set({ queue });
  await refreshBadge();
  return queue;
}

async function getRun() {
  const stored = await chrome.storage.local.get('activeRun');
  return stored.activeRun || null;
}

async function setRun(run) {
  if (run) await chrome.storage.local.set({ activeRun: run });
  else await chrome.storage.local.remove('activeRun');
  await refreshBadge();
  return run;
}

/** 角标：跑批时显示已采页数，否则显示队列长度。0 就不显示。 */
async function refreshBadge() {
  try {
    const run = await getRun();
    if (run && run.active) {
      await chrome.action.setBadgeBackgroundColor({ color: '#1f8a4c' });
      await chrome.action.setBadgeText({ text: String(run.pagesDone || 0) });
      return;
    }
    const queue = await getQueue();
    await chrome.action.setBadgeBackgroundColor({ color: '#555555' });
    await chrome.action.setBadgeText({ text: queue.length ? String(queue.length) : '' });
  } catch (e) {
    // 角标失败不该影响任何业务路径
  }
}

// ──────────────────────────────────────────────────────────────
// Server 客户端
// ──────────────────────────────────────────────────────────────

function normalizeServerUrl(raw) {
  let s = String(raw || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
  return s.replace(/\/+$/, '');
}

/**
 * 调 Server。失败一律抛 Error，**消息里带上服务端的 detail**。
 *
 * 不吞错是有意的：这条链路上每一环出问题的表现都是"点了没反应"，
 * 用户没有别的诊断手段。把 400 的 detail 原样弹到 popup 上是唯一能让
 * "价格区间填反了"这种事当场可见的办法。
 */
async function callServer(path, { method = 'GET', body = null } = {}) {
  const settings = await getSettings();
  const base = normalizeServerUrl(settings.serverUrl);
  if (!base) throw new Error('还没配置 Server 地址，请先打开插件选项页填写');

  const headers = {};
  if (body) headers['Content-Type'] = 'application/json';
  // 服务端配了 ADMIN_TOKEN 时，破坏性端点要它。采集端点目前不在保护名单里，
  // 但带上无害，且用户换了服务端配置后不必回来改插件。
  if (settings.adminToken) headers['X-Admin-Token'] = settings.adminToken;

  let resp;
  try {
    resp = await fetch(base + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error(
      `连不上 Server (${base})。检查地址是否正确、服务是否在跑，` +
      `以及是否在选项页点过"授权访问"。原始错误: ${e.message}`
    );
  }

  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (e) {
    // 非 JSON 响应（反向代理的错误页等）：原样截一段出来，比 "解析失败" 有用
    if (!resp.ok) throw new Error(`Server ${resp.status}: ${text.slice(0, 200)}`);
    throw new Error(`Server 返回了非 JSON 响应: ${text.slice(0, 200)}`);
  }
  if (!resp.ok) {
    const detail = (data && (data.detail || data.error)) || resp.statusText;
    throw new Error(`Server ${resp.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  }
  return data;
}

/** 把一批 ASIN / 卖家推给 Server。批次名相同 = 追加进同一批次。 */
async function pushCollect({ asins = [], sellerIds = [], batchName = null, source = 'extension', pageUrl = '' }) {
  const settings = await getSettings();
  return callServer('/api/extension/collect', {
    method: 'POST',
    body: {
      batch_name: batchName,
      asins,
      seller_ids: sellerIds,
      zip_code: settings.zipCode || null,
      needs_screenshot: !!settings.needsScreenshot,
      seller_discover_mode: settings.sellerDiscoverMode || 'with_detail',
      source,
      page_url: pageUrl,
    },
  });
}

// ──────────────────────────────────────────────────────────────
// 批次名：一次"采集动作"用一个名字，翻页期间保持不变
// ──────────────────────────────────────────────────────────────

/**
 * `ext_<类型>_<本地时间戳>`。
 *
 * 用**本地**时间而不是 UTC，是因为这个名字只给人看（在控制台批次列表里
 * 找"我刚才点的那次"）；用 ISO 的 UTC 会让人对不上自己点击的时刻。
 * 服务端不解析它，只当唯一键。
 */
function makeBatchName(kind) {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  return `ext_${kind}_${stamp}`;
}

// ──────────────────────────────────────────────────────────────
// 消息路由
// ──────────────────────────────────────────────────────────────

const handlers = {
  // ---------- 设置 ----------
  async GET_SETTINGS() {
    return { settings: await getSettings() };
  },

  async SET_SETTINGS({ patch }) {
    return { settings: await setSettings(patch) };
  },

  async PING_SERVER() {
    const info = await callServer('/api/extension/ping');
    return { info };
  },

  // ---------- 待采队列（详情页用）----------
  async GET_QUEUE() {
    return { queue: await getQueue() };
  },

  async ADD_TO_QUEUE({ item }) {
    const queue = await getQueue();
    const idx = queue.findIndex((q) => q.asin === item.asin);
    if (idx >= 0) {
      // 已在队列里：合并而不是新增。用户重新打开同一个商品页勾上
      // "采集该卖家全部商品" 时，那个勾选要生效。
      queue[idx] = { ...queue[idx], ...item };
      await setQueue(queue);
      return { queue, added: false, updated: true };
    }
    queue.push(item);
    await setQueue(queue);
    return { queue, added: true, updated: false };
  },

  async REMOVE_FROM_QUEUE({ asin }) {
    const queue = (await getQueue()).filter((q) => q.asin !== asin);
    await setQueue(queue);
    return { queue };
  },

  async SET_QUEUE_ITEM({ asin, patch }) {
    const queue = await getQueue();
    const idx = queue.findIndex((q) => q.asin === asin);
    if (idx >= 0) {
      queue[idx] = { ...queue[idx], ...patch };
      await setQueue(queue);
    }
    return { queue };
  },

  async CLEAR_QUEUE() {
    await setQueue([]);
    return { queue: [] };
  },

  /**
   * 提交整个队列。
   *
   * 勾了"采集该卖家全部商品"的条目，其 sellerId 会去重后一并送出 ——
   * 服务端把它们放进一个**独立的**卖家批次（`<批次名>_sellers`），
   * 不和商品批次混在一起。理由见 server/api/extension.py 的说明。
   */
  async SUBMIT_QUEUE() {
    const queue = await getQueue();
    if (!queue.length) throw new Error('队列是空的');

    const asins = queue.map((q) => q.asin);
    const sellerIds = [...new Set(
      queue.filter((q) => q.expandSeller && q.sellerId).map((q) => q.sellerId)
    )];

    const result = await pushCollect({
      asins,
      sellerIds,
      batchName: makeBatchName('queue'),
      source: 'detail_queue',
    });
    await setQueue([]);
    return { result, submitted: asins.length, sellers: sellerIds.length };
  },

  /**
   * 队列里那些 DOM 上读不到卖家的条目，问问服务端认不认识
   * （这个 ASIN 以前采过的话，asin_data 里就有 seller_id）。
   *
   * 这是对"详情页拿不到卖家"的补救，不是猜：服务端返回的是**实采到的**
   * 卖家，不是推测出来的。查不到就还是查不到，界面照实说。
   */
  async RESOLVE_SELLERS() {
    const queue = await getQueue();
    const unknown = queue.filter((q) => !q.sellerId).map((q) => q.asin);
    if (!unknown.length) return { resolved: 0, queue };

    const data = await callServer('/api/extension/resolve-sellers', {
      method: 'POST',
      body: { asins: unknown },
    });
    const found = data.sellers || {};
    let resolved = 0;
    for (const item of queue) {
      if (!item.sellerId && found[item.asin]) {
        item.sellerId = found[item.asin].seller_id;
        item.sellerName = found[item.asin].seller_name || item.sellerName;
        resolved += 1;
      }
    }
    await setQueue(queue);
    return { resolved, queue };
  },

  // ---------- 单页采集（不翻页）----------

  async COLLECT_PAGE({ asins, sellerIds, kind, pageUrl }) {
    const result = await pushCollect({
      asins: asins || [],
      sellerIds: sellerIds || [],
      batchName: makeBatchName(kind || 'page'),
      source: kind || 'list',
      pageUrl,
    });
    return { result };
  },

  // ---------- 翻页任务 ----------

  async START_RUN({ tabId, kind, maxPages, pageUrl }) {
    const settings = await getSettings();
    const run = {
      active: true,
      tabId,
      kind,                                   // 'list' | 'seller'
      batchName: makeBatchName(kind),
      pagesDone: 0,
      maxPages: Math.max(1, parseInt(maxPages || settings.maxPages, 10) || 5),
      pushedAsins: 0,
      startedAt: Date.now(),
      startUrl: pageUrl,
      lastError: null,
      finishedReason: null,
    };
    await setRun(run);
    return { run };
  },

  async STOP_RUN() {
    const run = await getRun();
    if (run) {
      run.active = false;
      run.finishedReason = 'stopped_by_user';
      await setRun(run);
    }
    return { run };
  },

  async GET_RUN() {
    return { run: await getRun() };
  },

  /**
   * content script 每页加载完报到一次。
   *
   * 返回 `{ shouldCollect }` —— 只有"这个 tab 上确实有个在跑的任务"才为真。
   * 用 tabId 而不是 URL 做归属判据：同一个采集任务跨的是同一个标签页，
   * 而 URL 每页都在变；反过来用户在**另一个**标签页打开亚马逊时，
   * 不该被卷进这次采集。
   */
  async CONTENT_READY({ tabId }) {
    const run = await getRun();
    if (!run || !run.active || run.tabId !== tabId) return { shouldCollect: false };
    return { shouldCollect: true, run };
  },

  /**
   * content script 交一页的成果。
   *
   * 返回 `{ next: 'continue' | 'stop', delayMs }`，由 service worker 决定
   * 要不要翻页 —— 判据（页数上限、有没有下一页、出没出错）都在这一侧，
   * content script 只管执行。把决策留在这里是因为它跨页存活，
   * content script 不跨页。
   */
  async PAGE_COLLECTED({ tabId, asins, hasNext, pageUrl }) {
    const run = await getRun();
    if (!run || !run.active || run.tabId !== tabId) return { next: 'stop', reason: 'no_active_run' };

    const settings = await getSettings();

    if (asins && asins.length) {
      try {
        const result = await pushCollect({
          asins,
          batchName: run.batchName,
          source: run.kind,
          pageUrl,
        });
        // 记的是 `inserted_tasks`（**真正新建的任务数**），不是这一页读到的
        // ASIN 数。翻页时前后页会有重叠、也可能撞上批次里已有的 ASIN，
        // 那些被 ON CONFLICT 吞掉了 —— 报"读到多少"会比实际入队的多，
        // 用户拿这个数去和批次进度对不上。
        run.pushedAsins += (result?.asin_batch?.inserted_tasks ?? asins.length);
      } catch (e) {
        // 推送失败就地停 —— 继续翻页只会把后面的页也丢掉，而且用户看到
        // 页数在涨会以为一切正常。停下来把错误留在 run 上，popup 会显示。
        run.active = false;
        run.lastError = e.message;
        run.finishedReason = 'push_failed';
        await setRun(run);
        return { next: 'stop', reason: 'push_failed', error: e.message };
      }
    }

    run.pagesDone += 1;

    if (run.pagesDone >= run.maxPages) {
      run.active = false;
      run.finishedReason = 'max_pages';
      await setRun(run);
      return { next: 'stop', reason: 'max_pages' };
    }
    if (!hasNext) {
      run.active = false;
      run.finishedReason = 'no_more_pages';
      await setRun(run);
      return { next: 'stop', reason: 'no_more_pages' };
    }

    await setRun(run);
    return { next: 'continue', delayMs: settings.pageDelayMs };
  },
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handler = handlers[msg?.type];
  if (!handler) {
    sendResponse({ ok: false, error: `未知消息类型: ${msg?.type}` });
    return false;
  }
  // content script 不知道自己的 tabId，由 sender 补上；popup 发来的
  // 消息没有 sender.tab，那时以 payload 里带的为准。
  const payload = { ...msg, tabId: msg.tabId ?? sender?.tab?.id };
  handler(payload)
    .then((data) => sendResponse({ ok: true, ...data }))
    .catch((e) => sendResponse({ ok: false, error: e.message }));
  return true; // 异步响应
});

// 标签页关掉了就把任务收掉，不然角标会一直挂着一个永远不会推进的数字
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const run = await getRun();
  if (run && run.tabId === tabId && run.active) {
    run.active = false;
    run.finishedReason = 'tab_closed';
    await setRun(run);
  }
});

refreshBadge();
