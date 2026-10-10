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
 *
 * ──────────────────────────────────────────────────────────────
 * 站点（F-012）：每一次推送都带上 `marketplace`
 * ──────────────────────────────────────────────────────────────
 * 服务端缺省按美国站建任务（老插件不发这个字段，行为必须不变），所以插件
 * 这边不说清楚站点，加拿大页面上采的东西就会**静默**变成美国站任务 ——
 * 批次名、进度、发现数全正常，只有数据是另一个国家的。
 *
 * 规则都在 page.js 的「站点」一节（纯函数，content script 与这里共用、
 * 可在 Node 里单测），这里只负责把它们接到每一条推送路径上：
 *   单页采集 / 整店采集  站点取自页面 URL
 *   翻页采集            站点在 START_RUN 时定下，一次 run 只属于一个站点
 *   详情队列            键是 (asin, 站点)，提交时按站点分组、一个站点一个批次
 *   邮编                美国站读老键 zipCode，其它站点读 zipCodes[站点]，
 *                       绝不把美国邮编发给别的站点
 */

// page.js 是纯函数层（无 chrome.*）。本文件是 module service worker
// （manifest 的 background.type = "module"），import 进来求值一次，
// 它会把 AmzPage 挂到 globalThis 上 —— 与 content script 用的是同一份。
import './page.js';

const P = globalThis.AmzPage;

const DEFAULTS = {
  serverUrl: '',
  // 美国站邮编。键名沿用 v1.1.0 的 `zipCode`，升级后不用重填。
  zipCode: '',
  // 其它站点的邮编：{ 'amazon.ca': 'M5V 3L9' }。留空 = 由服务端按站点取默认。
  // 单独一个对象而不是复用 zipCode：见 page.js 的 zipFor。
  zipCodes: {},
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
  // v1.1.0 存下来的条目没有 marketplace —— 读出来时就补上：一律美国站
  // （v1.1.0 推什么都是美国站，见 page.js storedMarketplace）。下游（去重键、
  // 按站点分组、popup 上的站点标记）于是只需要认一种形状。
  return (stored.queue || []).map((q) =>
    (q && q.marketplace) ? q : { ...q, marketplace: P.queueItemMarketplace(q) });
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

/**
 * 把一批 ASIN / 卖家推给 Server。批次名相同 = 追加进同一批次。
 *
 * `marketplace` 由调用方给（它知道这批东西来自哪个页面 / 哪组队列）；
 * 没给就从 pageUrl 推。站点不支持（amazon.de 等）由服务端回 400，
 * callServer 把 detail 原样抛出来，popup 显示给用户 —— 插件不自己判。
 */
async function pushCollect({ asins = [], sellerIds = [], batchName = null, source = 'extension', pageUrl = '', marketplace = null }) {
  const settings = await getSettings();
  const mkt = P.resolveMarketplace(marketplace, pageUrl);
  const result = await callServer('/api/extension/collect', {
    method: 'POST',
    body: {
      batch_name: batchName,
      asins,
      seller_ids: sellerIds,
      marketplace: mkt,
      zip_code: P.zipFor(settings, mkt),
      needs_screenshot: !!settings.needsScreenshot,
      seller_discover_mode: settings.sellerDiscoverMode || 'with_detail',
      source,
      page_url: pageUrl,
    },
  });
  // 美国站不核对：老服务端、新服务端对美国站的回包都不带 marketplace（v1.1.0 的形状）。
  if (mkt !== P.DEFAULT_MARKETPLACE) assertServerKeptSite(result, mkt);
  return result;
}

/**
 * 非美国站推送之后核对：服务端真的按这个站点建了批次吗？
 *
 * 插件比服务端新（插件 1.2.0、服务端还是 F-012 之前的版本）时会**静默**出错：
 * 老服务端不认 `marketplace` 字段，照样回 200，建的却是美国站任务、带美国默认
 * 邮编 —— worker 去 amazon.com 采加拿大的 ASIN，popup 还显示"已推送（amazon.ca）"。
 * 认这个字段的服务端对非美国站的 `asin_batch` / `seller_batch` 都回显
 * `marketplace`（server/api/extension.py），没回显或对不上 = 服务端没按这个站点建。
 *
 * 到这一步批次已经建了（只能事后发现），所以报错要点名是哪几个批次、该怎么收拾。
 * 抛错让各条推送路径都按"失败"处理：单页采集弹错、翻页停下、队列里那一组留着。
 */
function assertServerKeptSite(result, mkt) {
  const wrong = ['asin_batch', 'seller_batch']
    .map((k) => result && result[k])
    .filter((b) => b && b.marketplace !== mkt)
    .map((b) => `${b.batch_name}（${b.marketplace || 'amazon.com'}）`);
  if (!wrong.length) return;
  throw new Error(
    `服务端没有按 ${mkt} 建任务（多半是服务端版本过旧、不认 marketplace 字段）：` +
    `批次 ${wrong.join('、')} 已经建成了别的站点的任务。` +
    '请先升级服务端，到控制台删掉这些批次，再重新推送。');
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
    // 站点在入队时就钉死在条目上：popup 给的是页面识别出的站点，没给就按
    // 这一页的 url 推。之后它就是"存下来的数据"，只认这个字段。
    const it = { ...item, marketplace: P.resolveMarketplace(item.marketplace, item.url) };
    // 去重键是 (asin, 站点)：同一个 ASIN 的美国站与加拿大站是两条，见 page.js sameQueueItem
    const idx = queue.findIndex((q) => P.sameQueueItem(q, it));
    if (idx >= 0) {
      // 已在队列里：合并而不是新增。用户重新打开同一个商品页勾上
      // "采集该卖家全部商品" 时，那个勾选要生效。
      queue[idx] = { ...queue[idx], ...it };
      await setQueue(queue);
      return { queue, added: false, updated: true };
    }
    queue.push(it);
    await setQueue(queue);
    return { queue, added: true, updated: false };
  },

  /** 按 (asin, 站点) 删一条。没给站点就删掉这个 ASIN 的全部条目（v1.1.0 的语义）。 */
  async REMOVE_FROM_QUEUE({ asin, marketplace }) {
    const queue = (await getQueue()).filter(
      (q) => !(q.asin === asin && (!marketplace || q.marketplace === marketplace)));
    await setQueue(queue);
    return { queue };
  },

  async SET_QUEUE_ITEM({ asin, marketplace, patch }) {
    const queue = await getQueue();
    const idx = queue.findIndex(
      (q) => q.asin === asin && (!marketplace || q.marketplace === marketplace));
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
   * 提交整个队列：**按站点分组，一个站点一个批次**。
   *
   * 勾了"采集该卖家全部商品"的条目，其 sellerId 会去重后一并送出 ——
   * 服务端把它们放进一个**独立的**卖家批次（`<批次名>_sellers`），
   * 不和商品批次混在一起。理由见 server/api/extension.py 的说明。
   * 卖家也按站点分：加拿大商品页上勾的卖家，翻的是他的加拿大店铺。
   *
   * 为什么必须拆批次：一个批次只能属于一个站点（tasks 的唯一键是
   * (batch_id, asin)，下游按任务行的站点取数据），混着推服务端会 409。
   * 只有美国站条目时，行为与 v1.1.0 相同：一次推送、批次名 `ext_queue_<时间戳>`。
   *
   * 某一组推送失败：**那一组留在队列里，接着推下一组**，最后一起报告。
   * 不能停在失败的那组：服务端拒掉一个站点（amazon.de 等不支持的站点）就把
   * 别的站点 —— 包括美国站 —— 一起堵住，用户得先找出那条删掉才提交得出去，
   * 而 v1.1.0 里同一个队列一次就能提交成功。推成功的组立刻移出队列：留着的话
   * 重试会把它再推进一个新批次（批次名带时间戳，每次都不同）。
   *
   * 返回 `{ results, failures, submitted, sellers }`。一组都没推成才抛错；
   * 只有一组时错误就是服务端原话（纯美国站队列失败时与 v1.1.0 一字不差）。
   */
  async SUBMIT_QUEUE() {
    const queue = await getQueue();
    if (!queue.length) throw new Error('队列是空的');

    // 名字一次性先定好：同一秒内生成，各组只差站点标记，
    // 控制台里一眼能看出这几个批次是同一次提交。
    const plans = P.groupByMarketplace(queue).map(([mkt, items]) => ({
      mkt, items, batchName: makeBatchName(P.siteKind('queue', mkt)),
    }));

    const results = [];
    const failures = [];
    let submitted = 0;
    let sellers = 0;
    for (const plan of plans) {
      const asins = plan.items.map((q) => q.asin);
      const sellerIds = [...new Set(
        plan.items.filter((q) => q.expandSeller && q.sellerId).map((q) => q.sellerId)
      )];

      let result;
      try {
        result = await pushCollect({
          asins,
          sellerIds,
          batchName: plan.batchName,
          source: 'detail_queue',
          marketplace: plan.mkt,
        });
      } catch (e) {
        failures.push({ marketplace: plan.mkt, count: asins.length, error: e.message });
        continue;
      }

      // 只移除推成功的这一组。重新读一次队列而不是拿开头那份：推送期间
      // 用户可能又加了条目，拿旧快照写回去会把它们弄丢。
      const sent = new Set(plan.items.map((q) => `${q.asin}|${plan.mkt}`));
      await setQueue((await getQueue()).filter((q) => !sent.has(`${q.asin}|${q.marketplace}`)));

      results.push({ marketplace: plan.mkt, result, submitted: asins.length, sellers: sellerIds.length });
      submitted += asins.length;
      sellers += sellerIds.length;
    }
    if (!results.length) throw new Error(describeFailures(failures));
    return { results, failures, submitted, sellers };
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
    const unknown = queue.filter((q) => !q.sellerId);
    if (!unknown.length) return { resolved: 0, queue };

    // 按站点分开问：同一个 ASIN 在美国站和加拿大站可以是不同的卖家。
    // 混在一起问的话，加拿大条目会拿到美国卖家，再拿它去建加拿大站的
    // 整店任务 —— 翻的是另一个国家的店铺，而界面上一切正常。
    //
    // 某个站点问失败了（比如服务端不支持那个站点）就记下来、接着问下一组，
    // 理由同 SUBMIT_QUEUE：一个被拒的站点不能把美国站条目的补全也堵死。
    const groups = P.groupByMarketplace(unknown);
    const failures = [];
    let resolved = 0;
    for (const [mkt, items] of groups) {
      let data;
      try {
        data = await callServer('/api/extension/resolve-sellers', {
          method: 'POST',
          body: { asins: items.map((q) => q.asin), marketplace: mkt },
        });
      } catch (e) {
        failures.push({ marketplace: mkt, count: items.length, error: e.message });
        continue;
      }
      const found = data.sellers || {};
      for (const item of items) {   // items 里的对象就是 queue 里的那些，原地补
        if (!item.sellerId && found[item.asin]) {
          item.sellerId = found[item.asin].seller_id;
          item.sellerName = found[item.asin].seller_name || item.sellerName;
          resolved += 1;
        }
      }
    }
    // 一组都没问成：与 v1.1.0 一样直接报错，队列原样不动。
    if (failures.length === groups.length) throw new Error(describeFailures(failures));
    await setQueue(queue);
    return { resolved, queue, failures };
  },

  // ---------- 单页采集（不翻页）----------

  async COLLECT_PAGE({ asins, sellerIds, kind, pageUrl, marketplace }) {
    // 站点取自页面：加拿大店铺页上点"整店采集"，翻的必须是加拿大店铺。
    const mkt = P.resolveMarketplace(marketplace, pageUrl);
    const result = await pushCollect({
      asins: asins || [],
      sellerIds: sellerIds || [],
      batchName: makeBatchName(P.siteKind(kind || 'page', mkt)),
      source: kind || 'list',
      pageUrl,
      marketplace: mkt,
    });
    return { result, marketplace: mkt };
  },

  // ---------- 翻页任务 ----------

  async START_RUN({ tabId, kind, maxPages, pageUrl, marketplace }) {
    const settings = await getSettings();
    // 一次翻页任务只属于一个站点，在这里定下来；后面每一页都按它推，
    // 页面离开了这个站点就停（见 CONTENT_READY / PAGE_COLLECTED）。
    const mkt = P.resolveMarketplace(marketplace, pageUrl);
    const run = {
      active: true,
      tabId,
      kind,                                   // 'list' | 'seller'
      marketplace: mkt,
      batchName: makeBatchName(P.siteKind(kind, mkt)),
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
  async CONTENT_READY({ tabId, pageUrl }) {
    const run = await getRun();
    if (!run || !run.active || run.tabId !== tabId) return { shouldCollect: false };
    // 归属只看 tabId，所以用户在跑批的标签页里手动切到另一个站点时，
    // 这里会说"该采"。先拦一道，省得那一页白滚一遍（PAGE_COLLECTED 还有一道）。
    if (P.pageLeavesRunSite(run, pageUrl)) {
      await stopForSiteChange(run, pageUrl);
      return { shouldCollect: false, run };
    }
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

    // 推送前的最后一道站点闸：这一页不属于这次 run 的站点就不推、直接停。
    // 推过去服务端也会 409（一个批次一个站点），但那会表现成"推送失败"，
    // 而真实原因是用户切了站点 —— 这里给出说得清的停止原因。
    if (P.pageLeavesRunSite(run, pageUrl)) {
      await stopForSiteChange(run, pageUrl);
      return { next: 'stop', reason: 'marketplace_changed' };
    }

    const settings = await getSettings();

    if (asins && asins.length) {
      try {
        const result = await pushCollect({
          asins,
          batchName: run.batchName,
          source: run.kind,
          pageUrl,
          // 用 run 的站点而不是这一页的：两者已经由上面那道闸保证相同，
          // 而 v1.1.0 存下来的 run 没有 marketplace 字段（= 美国站）。
          marketplace: P.runMarketplace(run),
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

/** 翻页任务的标签页离开了任务的站点：停下并记清楚原因（popup 会显示）。 */
async function stopForSiteChange(run, pageUrl) {
  run.active = false;
  run.finishedReason = 'marketplace_changed';
  // 升级前开始的翻页没有站点字段、按美国站算（它前面那些页也确实推成了美国站），
  // 在加拿大页面上开始的老翻页会停在这里 —— 点明原因，免得看着像插件认错了站点。
  run.lastError = `本次翻页属于 ${P.runMarketplace(run)}` +
    (run.marketplace ? '' : '（升级插件前开始的翻页一律按美国站算）') +
    `，标签页切到了 ${P.marketplaceFromUrl(pageUrl)}`;
  await setRun(run);
}

/**
 * 按站点分组的请求一组都没成功时的报错。只有一组就是那一组的原话（纯美国站
 * 队列与 v1.1.0 一字不差）；多组就逐个站点列出来，并说清楚条目都还在队列里。
 */
function describeFailures(failures) {
  if (failures.length === 1) return failures[0].error;
  return '全部失败，条目都还在队列里：' +
    failures.map((f) => `${f.marketplace}（${f.count} 条）：${f.error}`).join('；');
}

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
// ⚠ 这里**故意没有** chrome.action.onClicked：manifest 里配了
//   `action.default_popup`，配了之后 onClicked **永远不会触发**（浏览器
//   直接开弹窗）。想让图标改成开合页内面板，就得删掉 default_popup ——
//   那会让「工具栏图标」这个入口在非亚马逊页面上完全失效（没有 content
//   script 可以通信），得不偿失。
//
//   所以分工是：工具栏图标 = 弹窗（任何页面都能开，用来配置和看队列），
//   页内悬浮球 = 右侧面板（只在亚马逊页面上有，用来干活）。
//   两者装的是同一份 popup.html。

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const run = await getRun();
  if (run && run.tabId === tabId && run.active) {
    run.active = false;
    run.finishedReason = 'tab_closed';
    await setRun(run);
  }
});

refreshBadge();
