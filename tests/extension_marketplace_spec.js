/**
 * 插件站点（F-012 blocker 3）的规格用例。由 tests/test_extension_marketplace.py
 * 驱动（`node tests/extension_marketplace_spec.js`），退出码非 0 即失败，
 * 失败详情走 stdout。
 *
 * ──────────────────────────────────────────────────────────────
 * 为什么不依赖 jsdom（与 tests/extension_page_spec.js 不同）
 * ──────────────────────────────────────────────────────────────
 * 站点这条链路坏掉的方式是**静默**的：加拿大页面上采的东西推成了美国站
 * 任务，或者把美国邮编发给了加拿大站 —— 批次名、进度全正常，只有数据是
 * 另一个国家的。这种东西必须在每台机器上都真跑，不能因为没装 jsdom 就 skip。
 * 而被测的东西本来就不需要 DOM：
 *
 *   第一部分  page.js 里的站点纯函数（直接 require）。
 *   第二部分  background.js 的消息处理器 —— 用一个假的 `chrome`（storage 存在
 *             内存里、onMessage 把监听器交出来）和假的 `fetch`（记下每次请求体）
 *             把 service worker 真正加载起来，从消息进、从请求体出，断言
 *             「推给服务端的 marketplace / zip_code / batch_name」。
 *             这是插件与服务端之间唯一的契约面，断言它而不是断言内部函数。
 *
 * background.js 是 module service worker（顶部 `import './page.js'`），
 * 所以把这两个文件拷进一个带 `{"type":"module"}` 的临时目录再动态 import ——
 * 与浏览器里的求值方式一致（page.js 作为模块求值、把 AmzPage 挂到 globalThis）。
 *
 * 可选输入：环境变量 `AMZ_REGISTRY_JSON`（`[{id, host}, ...]`，由 pytest 从
 * `common/core/marketplace.py` 的注册表导出），用来断言插件推出来的站点键与
 * 服务端注册表同形。没给就跳过这一小段。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const SRC = path.join(__dirname, '..', 'extension', 'src');
const P = require(path.join(SRC, 'page.js'));

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

function checkTrue(name, cond, detail) {
  checks += 1;
  if (!cond) {
    failures += 1;
    console.log(`FAIL  ${name}${detail ? `\n        ${detail}` : ''}`);
  }
}

// ════════════════════════════════════════════════════════════
// 第一部分：page.js 的站点纯函数
// ════════════════════════════════════════════════════════════

// ---- marketplaceFromUrl ----
[
  ['https://www.amazon.ca/s?k=mouse', 'amazon.ca'],
  ['https://www.amazon.com/dp/B0XXXXXXXX', 'amazon.com'],
  ['https://smile.amazon.com/dp/B0XXXXXXXX', 'amazon.com'],
  ['https://amazon.ca/', 'amazon.ca'],
  ['https://WWW.AMAZON.CA/s?k=x', 'amazon.ca'],
  ['https://www.amazon.ca./dp/B0XXXXXXXX', 'amazon.ca'],
  ['https://www.amazon.co.uk/', 'amazon.co.uk'],
  ['https://www.amazon.com.au/s?k=x', 'amazon.com.au'],
  ['https://www.amazon.com.mx/', 'amazon.com.mx'],
  ['https://www.amazon.co.jp/', 'amazon.co.jp'],
  // 下面这些**必须**认不出来：认成 amazon.ca 等于让任意域名冒充站点
  ['https://www.amazon.ca.evil.com/', null],
  ['https://notamazon.ca/', null],
  ['https://evil-amazon.com/', null],
  ['https://www.google.com/?q=amazon.ca', null],
  ['', null],
  [null, null],
  ['garbage', null],
].forEach(([url, want]) => check(`marketplaceFromUrl(${JSON.stringify(url)})`, P.marketplaceFromUrl(url), want));

// manifest 里 content script 注入的每个主机都要翻成它自己：插件能在哪个站点上
// 识别页面，就必须能说出那个站点是谁（支不支持由服务端判）。
const manifest = JSON.parse(fs.readFileSync(path.join(SRC, '..', 'manifest.json'), 'utf8'));
for (const m of manifest.content_scripts[0].matches) {
  const host = m.replace('*://*.', '').replace('/*', '');
  check(`manifest 主机 ${host} -> 站点键`, P.marketplaceFromUrl(`https://www.${host}/s?k=x`), host);
}

// 与服务端注册表同形：插件推的键，服务端 normalize 后得是注册表里那个键
if (process.env.AMZ_REGISTRY_JSON) {
  for (const { id, host } of JSON.parse(process.env.AMZ_REGISTRY_JSON)) {
    check(`注册表 ${id} 的主页 -> 站点键`, P.marketplaceFromUrl(`https://${host}/`), id);
  }
}

// detectPage 带出站点（不需要 DOM：一个永远查不到元素的桩就够判 URL 那一档）
const EMPTY_DOC = { querySelector: () => null, querySelectorAll: () => [] };
check('detectPage: 加拿大详情页 marketplace',
      P.detectPage('https://www.amazon.ca/dp/B0CA000001', EMPTY_DOC).marketplace, 'amazon.ca');
check('detectPage: 加拿大详情页仍认作详情页',
      P.detectPage('https://www.amazon.ca/dp/B0CA000001', EMPTY_DOC).type, 'detail');
check('detectPage: 美国列表页 marketplace',
      P.detectPage('https://www.amazon.com/s?k=mouse', EMPTY_DOC).marketplace, 'amazon.com');
check('detectPage: 解析不了的 URL marketplace 为 null',
      P.detectPage('not a url', EMPTY_DOC).marketplace, null);

// ---- zipFor：美国站读老键、其它站点只读 zipCodes，绝不交叉 ----
check('zipFor: 美国站用 zipCode（与 v1.1.0 一致）', P.zipFor({ zipCode: '10001' }, 'amazon.com'), '10001');
check('zipFor: 美国站留空 -> null（服务端取默认）', P.zipFor({ zipCode: '' }, 'amazon.com'), null);
check('zipFor: 站点缺省按美国站', P.zipFor({ zipCode: '90210' }, null), '90210');
check('zipFor: 加拿大站用 zipCodes[amazon.ca]',
      P.zipFor({ zipCode: '10001', zipCodes: { 'amazon.ca': 'M5V 3L9' } }, 'amazon.ca'), 'M5V 3L9');
check('zipFor: 加拿大站绝不回落到美国 zipCode',
      P.zipFor({ zipCode: '10001' }, 'amazon.ca'), null);
check('zipFor: 加拿大站填了空白 -> null',
      P.zipFor({ zipCode: '10001', zipCodes: { 'amazon.ca': '   ' } }, 'amazon.ca'), null);
check('zipFor: 别的站点不吃加拿大的邮编',
      P.zipFor({ zipCodes: { 'amazon.ca': 'M5V 3L9' } }, 'amazon.de'), null);
check('zipFor: settings 缺失', P.zipFor(undefined, 'amazon.ca'), null);

// ---- 批次名的站点标记 ----
check("siteTag('amazon.com') 为空（美国站批次名不变）", P.siteTag('amazon.com'), '');
check("siteTag('amazon.ca')", P.siteTag('amazon.ca'), 'ca');
check("siteTag('amazon.co.uk') 不含点（批次名进截图路径）", P.siteTag('amazon.co.uk'), 'co_uk');
check("siteKind('queue', 'amazon.com')", P.siteKind('queue', 'amazon.com'), 'queue');
check("siteKind('list', 'amazon.ca')", P.siteKind('list', 'amazon.ca'), 'list_ca');

// ---- 队列条目的站点 / 去重键 / 分组 ----
check('队列条目: 显式站点优先', P.queueItemMarketplace({ marketplace: 'amazon.ca', url: 'https://www.amazon.com/dp/B0X' }), 'amazon.ca');
check('队列条目: v1.1.0 老条目按 url 推', P.queueItemMarketplace({ asin: 'B0X', url: 'https://www.amazon.ca/dp/B0X' }), 'amazon.ca');
check('队列条目: 没 url 的老条目算美国站', P.queueItemMarketplace({ asin: 'B0X' }), 'amazon.com');
check('队列条目: url 是垃圾也算美国站', P.queueItemMarketplace({ asin: 'B0X', url: '???' }), 'amazon.com');
check('去重键: 同 ASIN 不同站点不是同一条',
      P.sameQueueItem({ asin: 'B0X', marketplace: 'amazon.com' }, { asin: 'B0X', marketplace: 'amazon.ca' }), false);
check('去重键: 同 ASIN 同站点（一条老条目）是同一条',
      P.sameQueueItem({ asin: 'B0X', url: 'https://www.amazon.ca/dp/B0X' }, { asin: 'B0X', marketplace: 'amazon.ca' }), true);
check('分组: 按首次出现顺序，组内保持原顺序',
      P.groupByMarketplace([
        { asin: 'A', marketplace: 'amazon.ca' },
        { asin: 'B', marketplace: 'amazon.com' },
        { asin: 'C', url: 'https://www.amazon.ca/dp/C' },
      ]).map(([m, items]) => [m, items.map((i) => i.asin)]),
      [['amazon.ca', ['A', 'C']], ['amazon.com', ['B']]]);

// ---- 翻页任务的站点 ----
check('run: 显式站点', P.runMarketplace({ marketplace: 'amazon.ca', startUrl: 'https://www.amazon.com/s?k=x' }), 'amazon.ca');
check('run: v1.1.0 的 run 按 startUrl 推', P.runMarketplace({ startUrl: 'https://www.amazon.ca/s?k=x' }), 'amazon.ca');
check('run: 什么都没有算美国站', P.runMarketplace({}), 'amazon.com');
check('run: 同站点翻页不拦', P.pageLeavesRunSite({ marketplace: 'amazon.ca' }, 'https://www.amazon.ca/s?k=x&page=2'), false);
check('run: smile 与 www 是同一个站点',
      P.pageLeavesRunSite({ marketplace: 'amazon.com' }, 'https://smile.amazon.com/s?k=x&page=2'), false);
check('run: 切到别的站点要拦', P.pageLeavesRunSite({ marketplace: 'amazon.com' }, 'https://www.amazon.ca/s?k=x'), true);
check('run: 页面 URL 推不出站点时不拦', P.pageLeavesRunSite({ marketplace: 'amazon.ca' }, ''), false);

// ════════════════════════════════════════════════════════════
// 第二部分：background.js —— 从消息进，从推给服务端的请求体出
// ════════════════════════════════════════════════════════════

/** 一个够用的假 chrome：storage 在内存里，onMessage 的监听器交出来给测试调。 */
function makeChrome() {
  const state = { store: {}, listener: null };
  const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
  state.chrome = {
    storage: {
      local: {
        async get(key) {
          return key in state.store ? { [key]: clone(state.store[key]) } : {};
        },
        async set(obj) { Object.assign(state.store, clone(obj)); },
        async remove(key) { delete state.store[key]; },
      },
    },
    action: { async setBadgeBackgroundColor() {}, async setBadgeText() {} },
    runtime: {
      onMessage: { addListener(fn) { state.listener = fn; } },
      getManifest: () => manifest,
    },
    tabs: { onRemoved: { addListener() {} } },
  };
  return state;
}

/** 假 fetch：记下每次请求，回包由 `respond(path, body)` 决定（默认按 collect 的形状回 200）。 */
function makeFetch() {
  const state = { calls: [], respond: null };
  state.fetch = async (url, opts) => {
    const pathOnly = url.replace(/^https?:\/\/[^/]+/, '');
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    state.calls.push({ path: pathOnly, body });
    const [status, data] = (state.respond || defaultRespond)(pathOnly, body);
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: String(status),
      async text() { return JSON.stringify(data); },
    };
  };
  return state;
}

function defaultRespond(pathOnly, body) {
  if (pathOnly === '/api/extension/collect') {
    return [200, {
      asin_batch: body.asins && body.asins.length
        ? { batch_name: body.batch_name, inserted_tasks: body.asins.length } : null,
      seller_batch: body.seller_ids && body.seller_ids.length
        ? { batch_name: `${body.batch_name}_sellers`, inserted_tasks: body.seller_ids.length } : null,
    }];
  }
  if (pathOnly === '/api/extension/resolve-sellers') return [200, { sellers: {}, queried: 0 }];
  return [404, { detail: 'not found' }];
}

const STAMP = /\d{8}_\d{6}$/;
const US_KEYS_V110 = ['asins', 'batch_name', 'needs_screenshot', 'page_url',
                      'seller_discover_mode', 'seller_ids', 'source', 'zip_code'];

async function loadBackground() {
  // 拷进带 {"type":"module"} 的临时目录：background.js 顶部是 `import './page.js'`，
  // 必须按 ES 模块求值，page.js 也随之按模块求值 —— 与 Chrome 的 module service worker 一致。
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amz-ext-sw-'));
  fs.writeFileSync(path.join(dir, 'package.json'), '{"type":"module"}');
  for (const f of ['page.js', 'background.js']) {
    fs.copyFileSync(path.join(SRC, f), path.join(dir, f));
  }
  const chromeState = makeChrome();
  const fetchState = makeFetch();
  globalThis.chrome = chromeState.chrome;
  globalThis.fetch = fetchState.fetch;
  // 拷过去的 page.js 会再挂一次 AmzPage（同一份代码，覆盖无害）
  await import(pathToFileURL(path.join(dir, 'background.js')).href);
  if (typeof chromeState.listener !== 'function') throw new Error('background.js 没有注册 onMessage 监听器');
  const send = (msg, sender = {}) =>
    new Promise((resolve) => chromeState.listener(msg, sender, resolve));
  return { dir, chromeState, fetchState, send };
}

async function backgroundSpecs() {
  const { dir, chromeState, fetchState, send } = await loadBackground();
  const reset = (settings, extra = {}) => {
    chromeState.store = { settings: { serverUrl: 'http://srv:8899', ...settings }, ...extra };
    fetchState.calls = [];
    fetchState.respond = null;
  };
  const collects = () => fetchState.calls.filter((c) => c.path === '/api/extension/collect');

  try {
    // ---- 单页采集：美国站 ----
    reset({ zipCode: '10001' });
    let r = await send({ type: 'COLLECT_PAGE', asins: ['B0US000001'], kind: 'list',
                         pageUrl: 'https://www.amazon.com/s?k=mouse' });
    check('美国单页: ok', r.ok, true);
    let b = collects()[0].body;
    check('美国单页: marketplace', b.marketplace, 'amazon.com');
    check('美国单页: 邮编还是 settings.zipCode', b.zip_code, '10001');
    checkTrue('美国单页: 批次名与 v1.1.0 同形 ext_list_<ts>', /^ext_list_\d{8}_\d{6}$/.test(b.batch_name), b.batch_name);
    check('美国单页: 请求体 = v1.1.0 的键 + marketplace',
          Object.keys(b).sort(), [...US_KEYS_V110, 'marketplace'].sort());

    // ---- 单页采集：加拿大站，只配了美国邮编 ----
    reset({ zipCode: '10001' });
    r = await send({ type: 'COLLECT_PAGE', asins: ['B0CA000001'], kind: 'list',
                     pageUrl: 'https://www.amazon.ca/s?k=mouse' });
    b = collects()[0].body;
    check('加拿大单页: marketplace', b.marketplace, 'amazon.ca');
    check('加拿大单页: 绝不带美国邮编（留给服务端按站点取默认）', b.zip_code, null);
    checkTrue('加拿大单页: 批次名带站点标记 ext_list_ca_<ts>', /^ext_list_ca_\d{8}_\d{6}$/.test(b.batch_name), b.batch_name);
    check('加拿大单页: 回包带出站点给 popup', r.marketplace, 'amazon.ca');

    // ---- 单页采集：加拿大站，配了加拿大邮编 ----
    reset({ zipCode: '10001', zipCodes: { 'amazon.ca': 'M5V 3L9' } });
    await send({ type: 'COLLECT_PAGE', asins: ['B0CA000002'], kind: 'list',
                 pageUrl: 'https://www.amazon.ca/s?k=mouse' });
    check('加拿大单页: 用 zipCodes[amazon.ca]', collects()[0].body.zip_code, 'M5V 3L9');

    // ---- 整店采集：加拿大店铺页 ----
    reset({ zipCode: '10001' });
    await send({ type: 'COLLECT_PAGE', sellerIds: ['A2L77EE7U53NWQ'], kind: 'seller_full',
                 pageUrl: 'https://www.amazon.ca/s?me=A2L77EE7U53NWQ', marketplace: 'amazon.ca' });
    b = collects()[0].body;
    check('加拿大整店: marketplace', b.marketplace, 'amazon.ca');
    check('加拿大整店: seller_ids', b.seller_ids, ['A2L77EE7U53NWQ']);
    checkTrue('加拿大整店: 批次名 ext_seller_full_ca_<ts>', /^ext_seller_full_ca_\d{8}_\d{6}$/.test(b.batch_name), b.batch_name);

    // ---- 服务端不支持的站点：插件照推，把服务端的 400 detail 原样交给 popup ----
    reset({ zipCode: '10001' });
    fetchState.respond = () => [400, { detail: "不支持的站点: 'amazon.de'" }];
    r = await send({ type: 'COLLECT_PAGE', asins: ['B0DE000001'], kind: 'list',
                     pageUrl: 'https://www.amazon.de/s?k=maus' });
    check('德国站: 推的是 amazon.de 而不是悄悄改成美国站', collects()[0].body.marketplace, 'amazon.de');
    check('德国站: 不带美国邮编', collects()[0].body.zip_code, null);
    check('德国站: 失败', r.ok, false);
    checkTrue('德国站: 错误里有服务端 detail', /不支持的站点/.test(r.error || ''), r.error);

    // ---- 队列：键是 (asin, 站点) ----
    reset({ zipCode: '10001' });
    await send({ type: 'ADD_TO_QUEUE', item: { asin: 'B0Q0000001', url: 'https://www.amazon.com/dp/B0Q0000001', marketplace: 'amazon.com' } });
    await send({ type: 'ADD_TO_QUEUE', item: { asin: 'B0Q0000001', url: 'https://www.amazon.ca/dp/B0Q0000001', marketplace: 'amazon.ca' } });
    r = await send({ type: 'ADD_TO_QUEUE', item: { asin: 'B0Q0000001', url: 'https://www.amazon.ca/dp/B0Q0000001',
                                                   marketplace: 'amazon.ca', sellerId: 'A1CASELLER0001', expandSeller: true } });
    check('队列: 同 ASIN 两个站点是两条', r.queue.length, 2);
    check('队列: 同站点再加一次是更新不是新增', [r.added, r.updated], [false, true]);
    check('队列: 更新落在加拿大那条上',
          r.queue.map((q) => [q.marketplace, q.sellerId || null]),
          [['amazon.com', null], ['amazon.ca', 'A1CASELLER0001']]);
    r = await send({ type: 'REMOVE_FROM_QUEUE', asin: 'B0Q0000001', marketplace: 'amazon.ca' });
    check('队列: 按 (asin, 站点) 删，另一个站点那条还在', r.queue.map((q) => q.marketplace), ['amazon.com']);

    // ---- 队列：v1.1.0 存下来的老条目 ----
    reset({ zipCode: '10001' }, { queue: [
      { asin: 'B0LEGACY01', url: 'https://www.amazon.ca/dp/B0LEGACY01', addedAt: 1 },
      { asin: 'B0LEGACY02', url: 'https://www.amazon.com/dp/B0LEGACY02', addedAt: 2 },
      { asin: 'B0LEGACY03', addedAt: 3 },
    ] });
    r = await send({ type: 'GET_QUEUE' });
    check('老条目: 读出来就补上站点（按 url 推，没 url 算美国站）',
          r.queue.map((q) => [q.asin, q.marketplace]),
          [['B0LEGACY01', 'amazon.ca'], ['B0LEGACY02', 'amazon.com'], ['B0LEGACY03', 'amazon.com']]);

    // ---- 提交队列：只有美国站 -> 与 v1.1.0 相同，一次推送 ----
    reset({ zipCode: '10001' }, { queue: [
      { asin: 'B0US000010', url: 'https://www.amazon.com/dp/B0US000010', marketplace: 'amazon.com' },
      { asin: 'B0US000011', url: 'https://www.amazon.com/dp/B0US000011', marketplace: 'amazon.com',
        sellerId: 'A1USSELLER0001', expandSeller: true },
    ] });
    r = await send({ type: 'SUBMIT_QUEUE' });
    check('美国队列: ok', r.ok, true);
    check('美国队列: 只推一次', collects().length, 1);
    b = collects()[0].body;
    checkTrue('美国队列: 批次名 ext_queue_<ts>（与 v1.1.0 同形）', /^ext_queue_\d{8}_\d{6}$/.test(b.batch_name), b.batch_name);
    check('美国队列: 请求体', [b.marketplace, b.zip_code, b.asins, b.seller_ids, b.source, b.page_url],
          ['amazon.com', '10001', ['B0US000010', 'B0US000011'], ['A1USSELLER0001'], 'detail_queue', '']);
    check('美国队列: 提交后队列清空', chromeState.store.queue, []);
    check('美国队列: 回包', [r.submitted, r.sellers, r.results.length], [2, 1, 1]);

    // ---- 提交队列：混了两个站点 -> 一个站点一个批次 ----
    reset({ zipCode: '10001', zipCodes: { 'amazon.ca': 'K2P 1L4' } }, { queue: [
      { asin: 'B0MIX00001', url: 'https://www.amazon.com/dp/B0MIX00001', marketplace: 'amazon.com',
        sellerId: 'A1USSELLER0001', expandSeller: true },
      { asin: 'B0MIX00002', url: 'https://www.amazon.ca/dp/B0MIX00002', marketplace: 'amazon.ca',
        sellerId: 'A1CASELLER0001', expandSeller: true },
      { asin: 'B0MIX00001', url: 'https://www.amazon.ca/dp/B0MIX00001' },   // v1.1.0 老条目，加拿大页
    ] });
    r = await send({ type: 'SUBMIT_QUEUE' });
    check('混合队列: ok', r.ok, true);
    check('混合队列: 两个站点两次推送', collects().map((c) => c.body.marketplace), ['amazon.com', 'amazon.ca']);
    const [us, ca] = collects().map((c) => c.body);
    check('混合队列: 美国组', [us.asins, us.seller_ids, us.zip_code], [['B0MIX00001'], ['A1USSELLER0001'], '10001']);
    check('混合队列: 加拿大组（卖家也按站点分；邮编用加拿大的）',
          [ca.asins, ca.seller_ids, ca.zip_code], [['B0MIX00002', 'B0MIX00001'], ['A1CASELLER0001'], 'K2P 1L4']);
    checkTrue('混合队列: 美国组批次名不带标记', /^ext_queue_\d{8}_\d{6}$/.test(us.batch_name), us.batch_name);
    checkTrue('混合队列: 加拿大组批次名 ext_queue_ca_<ts>', /^ext_queue_ca_\d{8}_\d{6}$/.test(ca.batch_name), ca.batch_name);
    check('混合队列: 两个批次名同一个时间戳', us.batch_name.match(STAMP)[0], ca.batch_name.match(STAMP)[0]);
    check('混合队列: 队列清空', chromeState.store.queue, []);
    check('混合队列: 回包按站点列出', r.results.map((x) => [x.marketplace, x.submitted, x.sellers]),
          [['amazon.com', 1, 1], ['amazon.ca', 2, 1]]);

    // ---- 提交队列：第二组失败 -> 第一组移出队列，第二组留着 ----
    reset({ zipCode: '10001' }, { queue: [
      { asin: 'B0PART0001', marketplace: 'amazon.com' },
      { asin: 'B0PART0002', marketplace: 'amazon.ca' },
    ] });
    fetchState.respond = (p, body) => (body.marketplace === 'amazon.ca'
      ? [409, { detail: '批次已经是美国站的批次' }] : defaultRespond(p, body));
    r = await send({ type: 'SUBMIT_QUEUE' });
    check('部分失败: 报错', r.ok, false);
    checkTrue('部分失败: 说清楚哪组成功、哪组失败、服务端怎么说',
              /amazon\.com -> ext_queue_/.test(r.error) && /amazon\.ca 这一组推送失败/.test(r.error) &&
              /已经是美国站/.test(r.error), r.error);
    check('部分失败: 只剩没推成功的那组', chromeState.store.queue.map((q) => [q.asin, q.marketplace]),
          [['B0PART0002', 'amazon.ca']]);

    // ---- 补全卖家：按站点分开问 ----
    reset({ zipCode: '10001' }, { queue: [
      { asin: 'B0RS000001', marketplace: 'amazon.com' },
      { asin: 'B0RS000001', marketplace: 'amazon.ca' },
      { asin: 'B0RS000002', marketplace: 'amazon.ca' },
    ] });
    fetchState.respond = (p, body) => {
      if (p !== '/api/extension/resolve-sellers') return defaultRespond(p, body);
      const seller = body.marketplace === 'amazon.ca' ? 'A1CASELLER0001' : 'A1USSELLER0001';
      return [200, { sellers: { B0RS000001: { seller_id: seller, seller_name: body.marketplace } }, queried: body.asins.length }];
    };
    r = await send({ type: 'RESOLVE_SELLERS' });
    const resolveCalls = fetchState.calls.filter((c) => c.path === '/api/extension/resolve-sellers');
    check('补全卖家: 每个站点问一次、带上站点',
          resolveCalls.map((c) => [c.body.marketplace, c.body.asins]),
          [['amazon.com', ['B0RS000001']], ['amazon.ca', ['B0RS000001', 'B0RS000002']]]);
    check('补全卖家: 按 (asin, 站点) 对回去，不串站',
          chromeState.store.queue.map((q) => [q.asin, q.marketplace, q.sellerId || null]),
          [['B0RS000001', 'amazon.com', 'A1USSELLER0001'],
           ['B0RS000001', 'amazon.ca', 'A1CASELLER0001'],
           ['B0RS000002', 'amazon.ca', null]]);
    check('补全卖家: resolved 计数', r.resolved, 2);

    // ---- 翻页：加拿大站一次 run ----
    reset({ zipCode: '10001' });
    r = await send({ type: 'START_RUN', tabId: 7, kind: 'list', maxPages: 3,
                     pageUrl: 'https://www.amazon.ca/s?k=mouse' });
    check('加拿大翻页: run 站点', r.run.marketplace, 'amazon.ca');
    checkTrue('加拿大翻页: 批次名 ext_list_ca_<ts>', /^ext_list_ca_\d{8}_\d{6}$/.test(r.run.batchName), r.run.batchName);
    const caBatch = r.run.batchName;
    r = await send({ type: 'CONTENT_READY', pageUrl: 'https://www.amazon.ca/s?k=mouse' }, { tab: { id: 7 } });
    check('加拿大翻页: 同站点页报到 -> 该采', r.shouldCollect, true);
    r = await send({ type: 'PAGE_COLLECTED', asins: ['B0RUN00001'], hasNext: true,
                     pageUrl: 'https://www.amazon.ca/s?k=mouse' }, { tab: { id: 7 } });
    check('加拿大翻页: 继续', r.next, 'continue');
    b = collects()[0].body;
    check('加拿大翻页: 推送带 run 的站点、批次名、不带美国邮编',
          [b.marketplace, b.batch_name, b.zip_code], ['amazon.ca', caBatch, null]);
    // 用户把跑批的标签页手动切到了美国站
    r = await send({ type: 'PAGE_COLLECTED', asins: ['B0RUN00002'], hasNext: true,
                     pageUrl: 'https://www.amazon.com/s?k=mouse' }, { tab: { id: 7 } });
    check('加拿大翻页: 切站点 -> 停，原因 marketplace_changed', [r.next, r.reason], ['stop', 'marketplace_changed']);
    check('加拿大翻页: 切站点那页一个都没推', collects().length, 1);
    check('加拿大翻页: run 收尾', [chromeState.store.activeRun.active, chromeState.store.activeRun.finishedReason],
          [false, 'marketplace_changed']);

    // ---- 翻页：报到时就发现站点变了 ----
    reset({ zipCode: '10001' });
    await send({ type: 'START_RUN', tabId: 8, kind: 'list', maxPages: 3, pageUrl: 'https://www.amazon.com/s?k=x' });
    r = await send({ type: 'CONTENT_READY', pageUrl: 'https://www.amazon.ca/s?k=x' }, { tab: { id: 8 } });
    check('报到换站: 不采', r.shouldCollect, false);
    check('报到换站: run 停下', chromeState.store.activeRun.finishedReason, 'marketplace_changed');

    // ---- 翻页：美国站与 v1.1.0 相同 ----
    reset({ zipCode: '10001' });
    r = await send({ type: 'START_RUN', tabId: 9, kind: 'seller', maxPages: 2,
                     pageUrl: 'https://www.amazon.com/s?me=A2L77EE7U53NWQ' });
    checkTrue('美国翻页: 批次名 ext_seller_<ts>', /^ext_seller_\d{8}_\d{6}$/.test(r.run.batchName), r.run.batchName);
    await send({ type: 'PAGE_COLLECTED', asins: ['B0USRUN001'], hasNext: true,
                 pageUrl: 'https://www.amazon.com/s?me=A2L77EE7U53NWQ&page=2' }, { tab: { id: 9 } });
    b = collects()[0].body;
    check('美国翻页: 推送体', [b.marketplace, b.zip_code, b.source], ['amazon.com', '10001', 'seller']);

    // ---- 翻页：v1.1.0 存下来的 run（没有 marketplace）按 startUrl 推站点 ----
    reset({ zipCode: '10001' }, { activeRun: {
      active: true, tabId: 10, kind: 'list', batchName: 'ext_list_20260101_000000',
      pagesDone: 1, maxPages: 5, pushedAsins: 3, startedAt: 1,
      startUrl: 'https://www.amazon.ca/s?k=x', lastError: null, finishedReason: null,
    } });
    r = await send({ type: 'PAGE_COLLECTED', asins: ['B0OLDRUN01'], hasNext: true,
                     pageUrl: 'https://www.amazon.ca/s?k=x&page=2' }, { tab: { id: 10 } });
    b = collects()[0].body;
    check('老 run: 按 startUrl 推出加拿大站', [b.marketplace, b.batch_name, b.zip_code],
          ['amazon.ca', 'ext_list_20260101_000000', null]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

backgroundSpecs()
  .catch((e) => {
    failures += 1;
    console.log(`FAIL  background.js 规格用例异常退出: ${e && e.stack ? e.stack : e}`);
  })
  .finally(() => {
    console.log(`${checks - failures}/${checks} passed`);
    process.exit(failures ? 1 : 0);
  });
