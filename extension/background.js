// Sahm browser integration:
//  1. intercepts normal downloads and hands them to the desktop app
//  2. sniffs video/audio streams on each tab (badge count, like IDM)
//  3. receives "Download this video" clicks from content.js
const APP = "http://127.0.0.1:9614";

const DEFAULTS = { enabled: true, minSizeKB: 0, skipTypes: "", videoButton: true };
const getOpts = () => chrome.storage.local.get(DEFAULTS);

// ---------------------------------------------------------------- app bridge
async function post(path, body) {
  const r = await fetch(APP + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Sahm-Client": "extension" },
    body: JSON.stringify(body)
  });
  if (!r.ok) throw new Error("Sahm returned " + r.status);
}

async function cookieHeader(url) {
  try { return (await chrome.cookies.getAll({ url })).map(c => `${c.name}=${c.value}`).join("; "); }
  catch (e) { return ""; }
}

async function cookieList(url) {
  try { return await chrome.cookies.getAll({ url }); } catch (e) { return []; }
}

async function sendFile(url, filename, referrer) {
  await post("/add", {
    url, filename: filename || "", referrer: referrer || "",
    cookies: await cookieHeader(url), userAgent: navigator.userAgent
  });
}

async function sendVideo(tabId, pageUrl, title, extraSrc) {
  const media = (await getMedia(tabId)).map(m => m.url);
  if (extraSrc && !media.includes(extraSrc)) media.unshift(extraSrc);
  await post("/video", { url: pageUrl, title: title || "", media, cookieList: await cookieList(pageUrl) });
}

// ---------------------------------------------------------------- download interception
const passThrough = new Set();   // URLs we hand back to the browser when Sahm isn't running
const recent = new Map();        // url -> times intercepted recently (loop guard)
chrome.downloads.onCreated.addListener(async (item) => {
  const opts = await getOpts();
  const url = item.finalUrl || item.url;
  if (!opts.enabled || !/^https?:/i.test(url)) return;
  if (passThrough.has(url) || passThrough.has(item.url)) {          // our own fallback re-download
    passThrough.delete(url); passThrough.delete(item.url); return;
  }
  if (item.byExtensionId && item.byExtensionId === chrome.runtime.id) return;
  const n = (recent.get(url) || 0) + 1;                            // never intercept the same URL in a loop
  recent.set(url, n); setTimeout(() => recent.delete(url), 30000);
  if (n > 2) return;
  if (item.state && item.state !== "in_progress") return;        // old / finished / interrupted items
  if (item.startTime && Date.now() - Date.parse(item.startTime) > 30000) return;  // not a new download
  if (opts.minSizeKB > 0 && item.totalBytes > 0 && item.totalBytes < opts.minSizeKB * 1024) return;
  const ext = (item.filename || url).split(/[?#]/)[0].split(".").pop().toLowerCase();
  const skip = opts.skipTypes.split(/[,\s]+/).filter(Boolean).map(s => s.replace(".", "").toLowerCase());
  if (skip.includes(ext)) return;

  try { await chrome.downloads.cancel(item.id); } catch (e) {}
  try {
    const name = item.filename ? item.filename.split(/[\\/]/).pop() : "";
    await sendFile(url, name, item.referrer);
    chrome.downloads.erase({ id: item.id });
  } catch (e) {
    passThrough.add(url);               // app not running: give it back to the browser
    chrome.downloads.erase({ id: item.id });
    chrome.downloads.download({ url });
  }
});

// ---------------------------------------------------------------- media sniffing
const MEDIA_EXT = /\.(m3u8|mpd|mp4|webm|mkv|mov|m4a|mp3|flv)(\?|#|$)/i;
const MEDIA_TYPE = /^(video\/|audio\/|application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml))/i;
// segment/fragment requests are noise – the playlist is what we want
const NOISE = /(\.ts|\.m4s|\.aac|\/seg[-_]?\d|\/frag|\/chunk|range=|googlevideo\.com\/videoplayback)/i;

const key = id => "media_" + id;
async function getMedia(tabId) {
  const o = await chrome.storage.session.get(key(tabId));
  return o[key(tabId)] || [];
}
async function addMedia(tabId, entry) {
  const list = await getMedia(tabId);
  if (list.some(m => m.url === entry.url)) return;
  list.push(entry);
  if (list.length > 30) list.shift();
  await chrome.storage.session.set({ [key(tabId)]: list });
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#1a73e8" });
  chrome.action.setBadgeText({ tabId, text: String(list.length) });
}

chrome.webRequest.onHeadersReceived.addListener(details => {
  if (details.tabId < 0 || details.statusCode >= 400) return;
  const url = details.url;
  if (NOISE.test(url) || url.startsWith(APP)) return;
  const h = (details.responseHeaders || []).reduce((a, x) => (a[x.name.toLowerCase()] = x.value, a), {});
  const type = (h["content-type"] || "").split(";")[0];
  const len = parseInt(h["content-length"] || "0", 10);
  const isPlaylist = /\.(m3u8|mpd)(\?|$)/i.test(url) || /mpegurl|dash\+xml/i.test(type);
  if (!isPlaylist && !(MEDIA_TYPE.test(type) || MEDIA_EXT.test(url))) return;
  if (!isPlaylist && len && len < 300 * 1024) return;          // skip tiny clips / sounds
  addMedia(details.tabId, { url, type, size: len || null });
}, { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] }, ["responseHeaders"]);

// reset the list when a tab navigates to a new page
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url) {
    chrome.storage.session.remove(key(tabId));
    chrome.action.setBadgeText({ tabId, text: "" });
  }
});
chrome.tabs.onRemoved.addListener(tabId => chrome.storage.session.remove(key(tabId)));

// ---------------------------------------------------------------- messages from content.js / popup
const isHttp = u => typeof u === "string" && /^https?:\/\//i.test(u) && u.length < 8192;
const fromPopup = sender => !sender.tab && sender.id === chrome.runtime.id;

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !msg || typeof msg !== "object") return;
  if (msg.src && !isHttp(msg.src)) msg.src = "";
  if (msg.type === "sahm-video") {
    if (!sender.tab || !isHttp(sender.tab.url)) { reply({ ok: false }); return; }
    const tab = sender.tab || {};
    const pageUrl = tab.url;                      // top page (from the browser, not the page), even in iframes
    sendVideo(tab.id, pageUrl, tab.title || msg.title, msg.src)
      .then(() => reply({ ok: true })).catch(() => reply({ ok: false }));
    return true;
  }
  if (msg.type === "sahm-popup-video") {
    if (!fromPopup(sender) || !isHttp(msg.pageUrl)) { reply({ ok: false }); return; }
    sendVideo(msg.tabId, msg.pageUrl, msg.title, msg.src)
      .then(() => reply({ ok: true })).catch(() => reply({ ok: false }));
    return true;
  }
  if (msg.type === "sahm-get-media") {
    if (!fromPopup(sender)) { reply([]); return; }
    getMedia(msg.tabId).then(list => reply(list));
    return true;
  }
});

// ---------------------------------------------------------------- right-click menu
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "sahm-link", title: "Download with Sahm", contexts: ["link", "image", "audio"] });
  chrome.contextMenus.create({ id: "sahm-video", title: "Download this video with Sahm", contexts: ["video", "page"] });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (!tab || !isHttp(tab.url)) return;
    if (info.menuItemId === "sahm-video") {
      const src = info.srcUrl && !info.srcUrl.startsWith("blob:") ? info.srcUrl : "";
      await sendVideo(tab.id, tab.url, tab.title, src);
    } else {
      const url = info.linkUrl || info.srcUrl;
      if (url) await sendFile(url, "", tab && tab.url);
    }
  } catch (e) {
    const url = info.linkUrl || info.srcUrl;
    if (url && !url.startsWith("blob:")) chrome.downloads.download({ url });
  }
});
