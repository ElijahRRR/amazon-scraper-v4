/**
 * options.js —— 选项页。
 *
 * 除了存设置，这里还负责一件容易被忽略但**必须有**的事：向浏览器申请访问
 * 用户填的那个 Server 地址的权限。
 *
 * manifest 里只有 `optional_host_permissions`（通配全部 http / https 源），
 * 没有直接把它写进 `host_permissions`。差别是：写进 host_permissions 等于装插件
 * 时就要用户同意"读取你在所有网站上的数据"，而这个插件真正需要的只是**一个**
 * 用户自己指定的地址。所以改成运行时按需申请 —— 用户填了哪个地址就只要哪个。
 *
 * 没有这一步的话，service worker 的 fetch 会被 CORS 拦下，表现是弹窗里一条
 * "连不上 Server"，而地址其实完全正确 —— 最难查的那种。
 */

const $ = (id) => document.getElementById(id);

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

function status(text, kind = 'busy') {
  const el = $('status');
  el.textContent = text;
  el.className = `status ${kind}`;
  el.classList.remove('hidden');
}

function normalizeServerUrl(raw) {
  let s = String(raw || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
  return s.replace(/\/+$/, '');
}

/** `http://1.2.3.4:8899` -> `http://1.2.3.4:8899/*`（permissions API 要的形状）。 */
function originPattern(serverUrl) {
  const u = new URL(serverUrl);
  return `${u.protocol}//${u.host}/*`;
}

async function load() {
  const r = await bg('GET_SETTINGS');
  const s = r.settings || {};
  $('serverUrl').value = s.serverUrl || '';
  $('adminToken').value = s.adminToken || '';
  $('zipCode').value = s.zipCode || '';
  $('sellerDiscoverMode').value = s.sellerDiscoverMode || 'with_detail';
  $('maxPages').value = s.maxPages ?? 5;
  $('pageDelayMs').value = s.pageDelayMs ?? 2500;
  $('needsScreenshot').checked = !!s.needsScreenshot;
  $('includeSponsored').checked = !!s.includeSponsored;
  $('autoPaginate').checked = !!s.autoPaginate;
}

async function save() {
  const serverUrl = normalizeServerUrl($('serverUrl').value);
  if (serverUrl) {
    try {
      new URL(serverUrl);
    } catch (e) {
      status(`Server 地址不是合法 URL：${$('serverUrl').value}`, 'err');
      return null;
    }
  }
  const patch = {
    serverUrl,
    adminToken: $('adminToken').value.trim(),
    zipCode: $('zipCode').value.trim(),
    sellerDiscoverMode: $('sellerDiscoverMode').value,
    maxPages: parseInt($('maxPages').value, 10) || 5,
    pageDelayMs: parseInt($('pageDelayMs').value, 10) || 2500,
    needsScreenshot: $('needsScreenshot').checked,
    includeSponsored: $('includeSponsored').checked,
    autoPaginate: $('autoPaginate').checked,
  };
  const r = await bg('SET_SETTINGS', { patch });
  if (!r.ok) {
    status(r.error, 'err');
    return null;
  }
  $('serverUrl').value = serverUrl;
  status('已保存', 'ok');
  return serverUrl;
}

$('save').addEventListener('click', save);

$('grant').addEventListener('click', async () => {
  const serverUrl = await save();
  if (!serverUrl) {
    status('先填一个 Server 地址', 'err');
    return;
  }
  let pattern;
  try {
    pattern = originPattern(serverUrl);
  } catch (e) {
    status(`地址解析失败：${e.message}`, 'err');
    return;
  }
  // 必须由用户手势直接触发，所以只能放在 click 回调里，不能塞进 async 链的深处。
  chrome.permissions.request({ origins: [pattern] }, (granted) => {
    if (chrome.runtime.lastError) {
      status(`授权失败：${chrome.runtime.lastError.message}`, 'err');
      return;
    }
    status(granted
      ? `已授权访问 ${pattern}，现在可以点"测试连接"了`
      : `你拒绝了授权。没有它插件无法访问 ${pattern}，采集会一直报"连不上 Server"。`,
      granted ? 'ok' : 'err');
  });
});

$('test').addEventListener('click', async () => {
  const serverUrl = await save();
  if (!serverUrl) {
    status('先填一个 Server 地址', 'err');
    return;
  }
  status('连接中…', 'busy');
  const r = await bg('PING_SERVER');
  if (!r.ok) {
    status(r.error, 'err');
    return;
  }
  const i = r.info || {};
  status(
    `连接正常。\n服务端版本 ${i.version || '?'}，默认邮编 ${i.default_zip_code || '?'}，` +
    `在线 worker ${i.workers_online ?? '?'} 个。`,
    'ok'
  );
});

load();
