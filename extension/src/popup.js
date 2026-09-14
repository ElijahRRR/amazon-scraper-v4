/**
 * popup.js —— 操作界面。**两个宿主共用这一份**：
 *
 *   1. 工具栏那个弹窗（manifest 的 action.default_popup）
 *   2. 页内右侧面板 —— content.js 用 iframe 装的就是同一个 popup.html
 *
 * 复用而不是写两份，是因为两份 UI 一定会分叉：以后每加一个按钮都要改
 * 两处，漏掉的那处**不会报错**，只会安静地少一个功能。
 *
 * 三种页面对应三套面板，由 content script 的识别结果决定显示哪一套。
 * popup 自己**不碰 DOM 也不发网络请求**：前者要问 content script，
 * 后者要问 service worker（跨源只有它能做，见 background.js 头注）。
 *
 * ⚠ 两个宿主的生命周期**完全不同**，这决定了刷新策略：
 *
 *   弹窗：每次打开都重新执行，关掉即销毁 —— 打开时拉一次就够。
 *   面板：**一直开着**，页面在它眼皮底下翻页、导航、异步渲染 ——
 *         拉一次远远不够，必须持续接收 content script 的广播。
 *
 * 所以这里不存任何状态（关掉再打开会看到过期数据），并且同时监听两条
 * 更新通道：postMessage（在 iframe 里时）与 runtime 消息（在弹窗里时）。
 */

const $ = (id) => document.getElementById(id);

let pageInfo = null;
let settings = null;
let activeTabId = null;

//: 自己是不是被 content.js 用 iframe 装在页面右侧。
//  两个宿主的差别只有两处：面板要显示"收起"按钮，且要持续接收广播。
const IN_PANEL = window.top !== window.self;

// ──────────────────────────────────────────────────────────────
// 与两侧通信
// ──────────────────────────────────────────────────────────────

function bg(type, payload = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, ...payload }, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(resp || { ok: false, error: '后台无响应' });
    });
  });
}

function tab(type, payload = {}) {
  return new Promise((resolve) => {
    if (activeTabId == null) {
      resolve({ ok: false, error: '拿不到当前标签页' });
      return;
    }
    chrome.tabs.sendMessage(activeTabId, { type, ...payload }, (resp) => {
      if (chrome.runtime.lastError) {
        // 常见且无害的一种：当前页不是亚马逊，content script 根本没注入。
        // 说清楚比抛一个 "Could not establish connection" 有用。
        resolve({ ok: false, error: '当前页面没有采集脚本（不是亚马逊页面？）' });
        return;
      }
      resolve(resp || { ok: false, error: '页面无响应' });
    });
  });
}

// ──────────────────────────────────────────────────────────────
// 状态条
// ──────────────────────────────────────────────────────────────

function status(text, kind = 'busy') {
  const el = $('status');
  el.textContent = text;
  el.className = `status ${kind}`;
  el.classList.remove('hidden');
}

function clearStatus() {
  $('status').classList.add('hidden');
}

// ──────────────────────────────────────────────────────────────
// 渲染
// ──────────────────────────────────────────────────────────────

const BADGE = {
  list:    ['badge-list', '商品列表页'],
  seller:  ['badge-seller', '卖家店铺页'],
  detail:  ['badge-detail', '商品详情页'],
  unknown: ['badge-unknown', '未识别的页面'],
};

function renderPage(info) {
  const [cls, label] = BADGE[info.type] || BADGE.unknown;
  $('pageBadge').className = `badge ${cls}`;
  $('pageBadge').textContent = label;

  ['listPanel', 'sellerPanel', 'detailPanel'].forEach((id) => $(id).classList.add('hidden'));

  if (info.type === 'list' || info.type === 'seller') {
    $('listPanel').classList.remove('hidden');
    // 按「会推多少」来显示，而不是「读到多少」。
    //
    // 这两个数不一样：Amazon 页面顶部那个 "1-24 of over 40,000" 只数自然位，
    // 而结果区里还夹着广告位（顶部 Sponsored Brands 横幅 + 网格里穿插的
    // Sponsored 卡片）。推送时默认丢弃广告位，所以只报总数的话，用户看到 32、
    // 实际入队 24，差额没有任何地方能看出来 —— 跟"抓漏了 8 个"表现完全一样。
    const keep = settings.includeSponsored ? info.itemCount : info.naturalCount;
    let countText = `本页将采 ${keep} 个`;
    if (info.sponsoredCount) {
      countText += settings.includeSponsored
        ? `（含 ${info.sponsoredCount} 个广告位）`
        : `（另有 ${info.sponsoredCount} 个广告位已排除）`;
    }
    $('listCount').textContent = countText;
    $('listCount').title =
      `结果区共 ${info.itemCount} 个商品卡片：自然位 ${info.naturalCount}、` +
      `广告位 ${info.sponsoredCount}。\n` +
      'Amazon 页面顶部写的 "1-24 of ..." 只数自然位，所以总数比它大是正常的。';
    $('pageMeta').textContent = info.keyword ? `关键词：${info.keyword}` : '';
    syncPagesRow();
    if (!info.hasNextPage) {
      // 这一页就是最后一页时，"翻页采集"点了也只会采这一页。
      // 让开关自己说出来，比让用户点完发现只有一页强。
      $('paginate').title = '这一页已经没有下一页了';
    }
  }

  if (info.type === 'seller') {
    $('sellerPanel').classList.remove('hidden');
    $('sellerId').textContent = info.sellerId || '(未识别)';
    $('collectSeller').disabled = !info.sellerId;
    $('pageMeta').textContent = info.sellerName ? `店铺：${info.sellerName}` : '';
    if (info.isAmazonSelf) {
      $('sellerWarn').textContent =
        '这是 Amazon 自营店铺，商品数以百万计，整店采集会产生海量任务。' +
        '建议改用关键词采集或只采本页。';
      $('sellerWarn').classList.remove('hidden');
    }
  }

  if (info.type === 'detail') {
    $('detailPanel').classList.remove('hidden');
    $('detailAsin').textContent = info.asin || '(未识别 ASIN)';
    $('detailTitle').textContent = info.title || (info.ready ? '' : '页面加载中…');
    $('detailTitle').title = info.title || '';
    $('addQueue').disabled = !info.asin;
    $('pageMeta').textContent = '';

    const sellerBox = $('detailSeller');
    const expand = $('expandSeller');
    // ⚠ `ready === false` 是「认出来了但骨架还没渲染完」，与「渲染完了但
    //   读不到卖家」是两回事。不分开的话，页面刚打开那一瞬会显示
    //   "这一页读不到三方卖家" —— 一个过两秒就自己变对的错误结论，
    //   用户只会记住它错过一次。
    if (!info.ready) {
      sellerBox.textContent = '页面还在加载，卖家信息稍后出现…';
      expand.disabled = true;
    } else if (info.sellerId) {
      sellerBox.textContent = `卖家：${info.sellerName || info.sellerId}（${info.sellerId}）`;
      expand.disabled = false;
    } else if (info.isAmazonSelf) {
      // 有意不给伪造的 sellerId：Amazon 自营店的"全部商品"不是用户想要的东西。
      sellerBox.textContent = '该商品由 Amazon 自营，不支持整店采集';
      expand.disabled = true;
      expand.checked = false;
    } else {
      sellerBox.textContent =
        '这一页读不到三方卖家（可能是自营或页面结构变化）。' +
        '仍可加入队列；提交前点"补全卖家"可以问服务端要（该 ASIN 以前采过就有）。';
      expand.disabled = true;
      expand.checked = false;
    }
  }

  if (info.type === 'unknown') {
    $('pageMeta').textContent = '这个页面上没找到可采集的商品。';
  }
}

function renderRun(run) {
  const panel = $('runPanel');
  if (!run || !run.active) {
    panel.classList.add('hidden');
    // 上一次任务的结局也值得说一声 —— 否则用户只会看到"翻页突然停了"。
    if (run && run.finishedReason && run.finishedReason !== 'stopped_by_user') {
      const why = {
        max_pages: `已达设定的 ${run.maxPages} 页上限`,
        no_more_pages: '已经翻到最后一页',
        push_failed: `推送失败：${run.lastError || '未知错误'}`,
        tab_closed: '标签页被关闭',
      }[run.finishedReason] || run.finishedReason;
      status(`上次翻页采集结束（${why}）：共 ${run.pagesDone} 页 / 入队 ${run.pushedAsins} 个新 ASIN`,
             run.finishedReason === 'push_failed' ? 'err' : 'ok');
    }
    return;
  }
  panel.classList.remove('hidden');
  $('runProgress').textContent = `${run.pagesDone} / ${run.maxPages} 页`;
  $('runDetail').textContent =
    `批次 ${run.batchName}，已入队 ${run.pushedAsins} 个新 ASIN。` +
    '保持该标签页打开，翻页会自动进行。';
}

function renderQueue(queue) {
  $('queueCount').textContent = String(queue.length);
  $('submitQueue').disabled = queue.length === 0;
  $('clearQueue').disabled = queue.length === 0;
  // 只有"确实存在读不到卖家的条目"时才可点 —— 否则这个按钮点下去必然是
  // "补全了 0 个"，一个永远没用的按钮比没有按钮更让人怀疑功能坏了。
  $('resolveSellers').disabled = !queue.some((q) => !q.sellerId);

  const ul = $('queueList');
  ul.innerHTML = '';
  for (const item of queue) {
    const li = document.createElement('li');

    const asin = document.createElement('span');
    asin.className = 'q-asin';
    asin.textContent = item.asin;
    li.appendChild(asin);

    const title = document.createElement('span');
    title.className = 'q-title';
    title.textContent = item.title || '';
    title.title = item.title || '';
    li.appendChild(title);

    if (item.expandSeller && item.sellerId) {
      const s = document.createElement('span');
      s.className = 'q-seller';
      s.textContent = '整店';
      s.title = `连同卖家 ${item.sellerId} 的全部商品一起采`;
      li.appendChild(s);
    }

    const del = document.createElement('button');
    del.className = 'q-del';
    del.textContent = '×';
    del.title = '从队列移除';
    del.addEventListener('click', async () => {
      const r = await bg('REMOVE_FROM_QUEUE', { asin: item.asin });
      if (r.ok) renderQueue(r.queue);
    });
    li.appendChild(del);

    ul.appendChild(li);
  }

  const unknown = queue.filter((q) => !q.sellerId).length;
  $('queueHint').textContent = queue.length
    ? `提交后会创建一个商品批次${queue.some((q) => q.expandSeller && q.sellerId) ? '，外加一个卖家批次' : ''}。` +
      (unknown ? ` 其中 ${unknown} 条读不到卖家，可点"补全卖家"问服务端。` : '')
    : '在商品详情页点"加入待采队列"，攒够了一次性提交。';
}

// ──────────────────────────────────────────────────────────────
// 动作
// ──────────────────────────────────────────────────────────────

async function doCollectList() {
  const paginate = $('paginate').checked;
  const maxPages = parseInt($('maxPages').value, 10) || 5;
  const kind = pageInfo.type === 'seller' ? 'seller' : 'list';

  $('collectList').disabled = true;
  try {
    if (paginate) {
      // 翻页模式：只负责起头。后面每一页由 content script 自己报到，
      // 由 service worker 决定继续还是收尾（跨页状态只能存在那边）。
      const started = await bg('START_RUN', {
        tabId: activeTabId, kind, maxPages, pageUrl: pageInfo.url,
      });
      if (!started.ok) throw new Error(started.error);
      status('翻页采集已启动，保持该标签页打开…', 'busy');
      // 立刻踢一脚当前页，不然要等用户手动刷新才会采第一页
      await tab('KICK_RUN');
      const runResp = await bg('GET_RUN');
      renderRun(runResp.run);
      setTimeout(refreshRun, 1500);
      return;
    }

    status('正在读取本页商品…', 'busy');
    const page = await tab('COLLECT_NOW', { includeSponsored: settings.includeSponsored });
    if (!page.ok) throw new Error(page.error);
    if (!page.asins.length) throw new Error('本页没读到任何 ASIN');

    status(`读到 ${page.asins.length} 个 ASIN，推送中…`, 'busy');
    const pushed = await bg('COLLECT_PAGE', {
      asins: page.asins, kind, pageUrl: pageInfo.url,
    });
    if (!pushed.ok) throw new Error(pushed.error);

    const b = pushed.result.asin_batch;
    status(
      `已推送到批次 ${b.batch_name}：${page.asins.length} 个 ASIN，` +
      `新建 ${b.inserted_tasks} 个采集任务` +
      (page.sponsoredSkipped ? `（跳过 ${page.sponsoredSkipped} 个广告位）` : ''),
      'ok'
    );
  } catch (e) {
    status(e.message, 'err');
  } finally {
    $('collectList').disabled = false;
  }
}

async function doCollectSeller() {
  $('collectSeller').disabled = true;
  try {
    status('提交整店采集任务…', 'busy');
    const r = await bg('COLLECT_PAGE', {
      sellerIds: [pageInfo.sellerId], kind: 'seller_full', pageUrl: pageInfo.url,
    });
    if (!r.ok) throw new Error(r.error);
    const sb = r.result.seller_batch;
    status(
      `已创建卖家批次 ${sb.batch_name}：${sb.inserted_tasks} 个发现任务。` +
      'Server 会翻遍该店铺并把发现的 ASIN 送进详情队列。',
      'ok'
    );
  } catch (e) {
    status(e.message, 'err');
  } finally {
    $('collectSeller').disabled = false;
  }
}

async function doAddQueue() {
  try {
    const item = {
      asin: pageInfo.asin,
      title: (pageInfo.title || document.title || '').slice(0, 200),
      sellerId: pageInfo.sellerId || null,
      sellerName: pageInfo.sellerName || null,
      expandSeller: $('expandSeller').checked && !!pageInfo.sellerId,
      url: pageInfo.url,
      addedAt: Date.now(),
    };
    const r = await bg('ADD_TO_QUEUE', { item });
    if (!r.ok) throw new Error(r.error);
    renderQueue(r.queue);
    status(r.added ? `已加入队列：${item.asin}` : `已更新队列中的 ${item.asin}`, 'ok');
  } catch (e) {
    status(e.message, 'err');
  }
}

async function doSubmitQueue() {
  $('submitQueue').disabled = true;
  try {
    status('提交队列…', 'busy');
    const r = await bg('SUBMIT_QUEUE');
    if (!r.ok) throw new Error(r.error);
    const ab = r.result.asin_batch;
    const sb = r.result.seller_batch;
    let msg = `已提交 ${r.submitted} 个商品到批次 ${ab.batch_name}（新建 ${ab.inserted_tasks} 个任务）`;
    if (sb) msg += `；另建卖家批次 ${sb.batch_name}，${sb.inserted_tasks} 个整店发现任务`;
    status(msg, 'ok');
    renderQueue([]);
  } catch (e) {
    status(e.message, 'err');
    $('submitQueue').disabled = false;
  }
}

async function refreshRun() {
  const r = await bg('GET_RUN');
  renderRun(r.run);
  if (r.run && r.run.active) setTimeout(refreshRun, 1500);
}

// ──────────────────────────────────────────────────────────────
// 启动
// ──────────────────────────────────────────────────────────────

async function init() {
  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTabId = activeTab?.id ?? null;

  const s = await bg('GET_SETTINGS');
  settings = s.settings || {};
  $('paginate').checked = !!settings.autoPaginate;
  $('maxPages').value = settings.maxPages || 5;
  syncPagesRow();

  if (!settings.serverUrl) {
    status('还没配置 Server 地址 —— 点右上角"选项"填一下', 'err');
  }

  const q = await bg('GET_QUEUE');
  renderQueue(q.queue || []);

  const runResp = await bg('GET_RUN');
  renderRun(runResp.run);
  if (runResp.run && runResp.run.active) setTimeout(refreshRun, 1500);

  const analyzed = await tab('ANALYZE_PAGE');
  if (analyzed.ok) {
    pageInfo = analyzed.info;
    renderPage(pageInfo);
  } else {
    renderPage({ type: 'unknown', itemCount: 0 });
    $('pageMeta').textContent = analyzed.error;
  }
}

// ──────────────────────────────────────────────────────────────
// 页面变化的两条更新通道
// ──────────────────────────────────────────────────────────────
//
// content.js 每次重新识别出**不一样**的页面就广播一次。面板常驻，
// 页面在它眼皮底下翻页/导航/异步渲染完，全靠这条通道跟上 ——
// 没有它，面板会一直显示打开那一刻的状态，而用户完全看不出它已经过期。

function onPageInfo(info) {
  if (!info) return;
  pageInfo = info;
  renderPage(info);
}

// 通道 A：在面板 iframe 里 —— content.js 的 postMessage。
window.addEventListener('message', (ev) => {
  if (ev.data?.source !== 'amz-scraper') return;
  if (ev.data.type === 'PAGE_CHANGED') onPageInfo(ev.data.info);
});

// 通道 B：在工具栏弹窗里 —— content.js 经 runtime 广播。
// （弹窗开着时页面也可能变，比如详情页刚渲染完卖家信息。）
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === 'PAGE_CHANGED_FROM_CONTENT') onPageInfo(msg.info);
});

if (IN_PANEL) {
  // 面板里多一个"收起"入口，并且开局主动要一次最新状态 ——
  // iframe 可能比 content.js 的第一次广播晚加载完，那一发会打空。
  document.body.classList.add('in-panel');
  window.parent.postMessage({ source: 'amz-scraper-panel', type: 'REQUEST_REFRESH' }, '*');
}

$('openOptions').addEventListener('click', (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

// 手动刷新：实时检测已经覆盖了绝大多数情况，但留一个显式入口 ——
// 万一 Amazon 用了某种 MutationObserver 看不见的渲染方式，用户至少
// 有办法自救，而不是只能关掉重开。
$('refreshPage').addEventListener('click', async (e) => {
  e.preventDefault();
  if (IN_PANEL) {
    window.parent.postMessage({ source: 'amz-scraper-panel', type: 'REQUEST_REFRESH' }, '*');
  }
  const analyzed = await tab('ANALYZE_PAGE');
  if (analyzed.ok) onPageInfo(analyzed.info);
  else status(analyzed.error, 'err');
});
$('collectList').addEventListener('click', doCollectList);
$('collectSeller').addEventListener('click', doCollectSeller);
$('addQueue').addEventListener('click', doAddQueue);
$('submitQueue').addEventListener('click', doSubmitQueue);
$('resolveSellers').addEventListener('click', async () => {
  $('resolveSellers').disabled = true;
  try {
    status('向服务端查询已采到的卖家…', 'busy');
    const r = await bg('RESOLVE_SELLERS');
    if (!r.ok) throw new Error(r.error);
    renderQueue(r.queue);
    // 补不上就如实说"补不上"，不含糊。服务端只回库里真有的 —— 查不到
    // 就是这些 ASIN 还没被采过，不是出了错。
    status(r.resolved
      ? `补全了 ${r.resolved} 条的卖家，现在可以在队列里勾"整店"了`
      : '没有可补全的：这些 ASIN 服务端还没采过，所以库里也没有卖家信息',
      r.resolved ? 'ok' : 'busy');
  } catch (e) {
    status(e.message, 'err');
    $('resolveSellers').disabled = false;
  }
});
$('clearQueue').addEventListener('click', async () => {
  const r = await bg('CLEAR_QUEUE');
  if (r.ok) { renderQueue([]); clearStatus(); }
});
$('stopRun').addEventListener('click', async () => {
  const r = await bg('STOP_RUN');
  renderRun(r.run);
  status('已停止翻页采集', 'ok');
});
/** 翻页页数只在开了翻页时才有意义，关掉就藏起来。 */
function syncPagesRow() {
  $('pagesRow').classList.toggle('hidden', !$('paginate').checked);
}

$('paginate').addEventListener('change', (e) => {
  syncPagesRow();
  bg('SET_SETTINGS', { patch: { autoPaginate: e.target.checked } });
});

// 选项页改了「保留广告位」之后重新打开弹窗，上面那行数字要跟着变。
// settings 是 init() 里拉的快照，renderPage 读它 —— 所以只要 init 顺序
// 是「先拉 settings 再 renderPage」就自动正确（当前就是）。这里留个注，
// 免得将来有人把两者调换顺序：那会让数字停在上一次的开关状态上。
$('maxPages').addEventListener('change', (e) => {
  bg('SET_SETTINGS', { patch: { maxPages: parseInt(e.target.value, 10) || 5 } });
});

init();
