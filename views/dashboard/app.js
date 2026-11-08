const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

let state = null;
let settingsLoaded = false;
let settingsDirty = false;
let pollTimer = null;
let loadedVersion = null; // version when the page was opened; reload page after an update
const drafts = {}; // id -> edited text, survives re-render and failed sends

// ---------------------------------------------------------------- helpers
// 抖音的内容来自外部，一律用 textContent 渲染，避免 XSS
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}

function ago(ts) {
  if (!ts) return "还没运行过";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return fmtTime(ts);
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 5000 : 2800);
}

// window.confirm() is blocked inside AstrBot's sandboxed plugin iframe, so use our own dialog
function askConfirm(message, okText = "确定", danger = false) {
  return new Promise((resolve) => {
    const dlg = $("confirm");
    $("confirm-text").textContent = message;
    const ok = $("confirm-ok");
    ok.textContent = okText;
    ok.className = "btn " + (danger ? "stop" : "primary");
    dlg.classList.remove("hidden");
    ok.focus();
    const done = (v) => {
      dlg.classList.add("hidden");
      ok.onclick = $("confirm-cancel").onclick = dlg.onclick = document.onkeydown = null;
      resolve(v);
    };
    ok.onclick = () => done(true);
    $("confirm-cancel").onclick = () => done(false);
    dlg.onclick = (e) => e.target === dlg && done(false);
    document.onkeydown = (e) => e.key === "Escape" && done(false);
  });
}

async function call(endpoint, body, button) {
  if (button) button.disabled = true;
  try {
    const res = await bridge.apiPost(endpoint, body || {});
    if (res && res.message) toast(res.message);
    await refresh();
    return res;
  } catch (e) {
    toast(e.message || String(e), true);
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== name));
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.goto)));

// ---------------------------------------------------------------- render
function render() {
  const s = state;
  const t = s.today;

  // 头部
  $("account-line").textContent = s.logged_in ? `已登录：${s.nickname || "抖音账号"}` : "未登录抖音";
  const pill = $("run-pill");
  pill.className = "status-pill" + (s.running ? " on" : s.busy ? " busy" : "");
  $("run-text").textContent = s.running
    ? s.in_active_hours ? "自动运行中" : "自动运行中（休息时段）"
    : s.busy ? "正在执行任务…" : "已停止";

  $("login-banner").classList.toggle("hidden", s.logged_in);
  $("error-banner").classList.toggle("hidden", !s.last_error);
  $("error-banner").textContent = s.last_error ? `最近一次出错：${s.last_error}` : "";

  // 统计
  $("s-comments").textContent = t.comments;
  $("s-comments-max").textContent = `/ ${t.max_comments}`;
  $("b-comments").style.width = `${Math.min(100, (t.comments / Math.max(1, t.max_comments)) * 100)}%`;
  $("s-replies").textContent = t.replies;
  $("s-replies-max").textContent = `/ ${t.max_replies}`;
  $("b-replies").style.width = `${Math.min(100, (t.replies / Math.max(1, t.max_replies)) * 100)}%`;
  $("s-pending").textContent = s.pending.length;
  $("s-mode").textContent = s.settings.review_mode ? "人工审核模式" : "全自动模式（不经审核）";
  const last = Math.max(s.last_browse, s.last_reply);
  $("s-last").textContent = ago(last);
  $("s-hours").textContent = `活跃时间：${s.settings.active_hours || "全天"}`;

  // 控制按钮
  const toggle = $("btn-toggle");
  toggle.textContent = s.running ? "⏹ 停止自动运行" : "▶ 开始自动运行";
  toggle.className = "btn " + (s.running ? "stop" : "primary");
  $("btn-browse").disabled = !s.logged_in || s.busy;
  $("btn-reply").disabled = !s.logged_in || s.busy;
  $("quick-review").checked = !!s.settings.review_mode;

  const badge = $("tab-badge");
  badge.textContent = s.pending.length;
  badge.classList.toggle("hidden", !s.pending.length);

  renderPending();
  renderEvents();
  renderHistory();
  renderAccount();
  renderUpdate();
  if (!settingsLoaded || !settingsDirty) fillSettings();
}

function renderPending() {
  const list = $("pending-list");
  const ids = new Set(state.pending.map((p) => String(p.id)));
  // 只增删卡片，不重建已有卡片，避免冲掉正在编辑的文字
  list.querySelectorAll(".card").forEach((c) => { if (!ids.has(c.dataset.id)) c.remove(); });
  for (const p of state.pending) {
    if (list.querySelector(`.card[data-id="${CSS.escape(String(p.id))}"]`)) continue;
    list.prepend(pendingCard(p));
  }
  $("pending-empty").classList.toggle("hidden", state.pending.length > 0);
  $("btn-reject-all").classList.toggle("hidden", state.pending.length === 0);
}

function pendingCard(p) {
  const isComment = p.type === "comment";
  const textarea = el("textarea", { rows: "2" });
  textarea.value = drafts[p.id] ?? p.text;
  const count = el("span", { class: "count" }, `${textarea.value.length} 字`);
  textarea.addEventListener("input", () => {
    drafts[p.id] = textarea.value;
    count.textContent = `${textarea.value.length} 字`;
  });

  const ctx = isComment
    ? el("div", { class: "ctx" },
        el("span", { class: "ctx-label" }, "视频："),
        el("a", { href: `https://www.douyin.com/video/${p.aweme_id}`, target: "_blank", rel: "noopener" },
          `@${p.author || "未知作者"}  ${p.desc || "（无文案）"}`))
    : el("div", { class: "ctx" },
        el("div", {}, el("span", { class: "ctx-label" }, "你的作品："), p.desc || "—"),
        el("div", {}, el("span", { class: "ctx-label" }, `${p.comment?.user || "网友"}：`), p.comment?.text || ""));

  const sendBtn = el("button", { class: "btn small primary" }, "发送");
  const rejectBtn = el("button", { class: "btn small ghost danger" }, "放弃");
  sendBtn.addEventListener("click", async () => {
    const text = textarea.value.trim();
    if (!text) return toast("内容不能为空", true);
    sendBtn.textContent = "发送中…";
    const res = await call("pending/approve", { id: p.id, text: text === p.text ? "" : text }, sendBtn);
    sendBtn.textContent = "发送";
    if (res) delete drafts[p.id];
  });
  rejectBtn.addEventListener("click", async () => {
    if (await call("pending/reject", { id: p.id }, rejectBtn)) delete drafts[p.id];
  });

  return el("div", { class: "card", "data-id": String(p.id) },
    el("div", { class: "card-top" },
      el("span", { class: `tag ${isComment ? "comment" : "reply"}` }, isComment ? "评论" : "回复"),
      el("span", { class: "card-id" }, `#${p.id}`),
      el("span", { class: "card-time" }, fmtTime(p.created))),
    ctx,
    el("div", { class: "ctx-label", style: "font-size:12px;margin-bottom:4px" }, "AI 想说："),
    textarea,
    el("div", { class: "card-actions" }, count, rejectBtn, sendBtn));
}

function renderEvents() {
  const list = $("event-list");
  list.replaceChildren(...state.events.map((e) =>
    el("li", { class: e.level || "" }, el("span", { class: "t-time" }, fmtTime(e.t)), el("span", { class: "t-text" }, e.text))));
  $("events-empty").classList.toggle("hidden", state.events.length > 0);
  list.classList.toggle("hidden", !state.events.length);
}

function renderHistory() {
  const list = $("history-list");
  list.replaceChildren(...state.history.map((h) =>
    el("li", {},
      el("span", { class: "t-time" }, fmtTime(h.t)),
      el("div", {},
        el("div", { class: "h-target" },
          el("span", { class: `tag ${h.kind === "comment" ? "comment" : "reply"}` }, h.kind === "comment" ? "评论" : "回复"),
          " ", h.target),
        el("div", { class: "t-text" }, h.text)))));
  $("history-empty").classList.toggle("hidden", state.history.length > 0);
  list.classList.toggle("hidden", !state.history.length);
}

function renderAccount() {
  const s = state;
  $("account-in").classList.toggle("hidden", !s.logged_in);
  $("account-out").classList.toggle("hidden", s.logged_in);
  $("acc-name").textContent = s.nickname || "抖音账号";
  $("acc-avatar").textContent = (s.nickname || "抖").slice(0, 1);

  const lg = s.login || {};
  const img = $("qr-img");
  if (lg.qr) {
    if (img.src !== lg.qr) img.src = lg.qr;
    img.classList.remove("hidden");
    $("qr-placeholder").classList.add("hidden");
  } else {
    img.classList.add("hidden");
    $("qr-placeholder").classList.remove("hidden");
    $("qr-placeholder").textContent = lg.status === "loading" ? "正在获取二维码…" : "点击下面的按钮获取二维码";
  }
  $("qr-msg").textContent = lg.msg || "";
  const loginBtn = $("btn-login");
  const inProgress = lg.status === "loading" || lg.status === "qr";
  loginBtn.disabled = inProgress;
  loginBtn.textContent = inProgress ? "等待扫码…" : lg.status === "timeout" || lg.status === "error" ? "重新获取二维码" : "获取登录二维码";
}

function renderUpdate() {
  const u = state.update || {};
  // The plugin was reloaded with a new version: reload this page to get new assets too
  if (loadedVersion && u.current && u.current !== loadedVersion) {
    toast(`✅ 已更新到 v${u.current}，正在刷新页面…`);
    loadedVersion = u.current;
    setTimeout(() => location.reload(), 1500);
    return;
  }
  loadedVersion ??= u.current;

  $("ver").textContent = u.current ? `v${u.current}` : "";
  $("u-current").textContent = u.current ? `v${u.current}` : "—";
  const latest = $("u-latest");
  latest.textContent = u.latest ? `v${u.latest}` : "—";
  latest.classList.toggle("new", !!u.has_update);

  const updating = u.status === "updating";
  const checking = u.status === "checking";
  $("update-banner").classList.toggle("hidden", !u.has_update);
  $("update-banner-text").textContent = updating
    ? `⏳ ${u.msg || "正在更新…"}`
    : `🆕 发现新版本 v${u.latest}（当前 v${u.current}）`;
  $("update-badge").classList.toggle("hidden", !u.has_update);

  for (const id of ["btn-update-run", "btn-update-banner"]) {
    const b = $(id);
    b.disabled = updating;
    b.textContent = updating ? "更新中…" : "立即更新";
  }
  // Without AstrBot's update method, point to the WebUI plugin page instead
  const canSelfUpdate = u.self_update !== false;
  $("btn-update-run").classList.toggle("hidden", !u.has_update || !canSelfUpdate);
  $("btn-update-banner").classList.toggle("hidden", !canSelfUpdate);
  if (u.has_update && !canSelfUpdate && !updating) {
    $("update-banner-text").textContent =
      `🆕 发现新版本 v${u.latest}，请到 AstrBot 的「插件」页面点「更新」`;
  }
  $("btn-update-check").disabled = checking || updating;

  const st = $("u-status");
  st.className = "update-status" + (u.status === "error" ? " error" : updating || checking ? " busy" : "");
  st.textContent = u.msg
    ? u.msg + (u.checked_at && !updating && !checking ? `（检查于 ${fmtTime(u.checked_at)}）` : "")
    : u.checked_at ? `上次检查：${fmtTime(u.checked_at)}` : "";

  $("u-notes-wrap").classList.toggle("hidden", !(u.has_update && u.notes));
  $("u-notes").textContent = u.notes || "";
  const repo = $("u-repo");
  if (u.repo) {
    repo.href = u.repo;
    repo.textContent = u.repo.replace(/^https?:\/\//, "");
  }
}

// ---------------------------------------------------------------- settings
const form = $("settings-form");

function fillSettings() {
  const st = state.settings;
  for (const [k, v] of Object.entries(st)) {
    const input = form.elements[k];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = v;
  }
  $("prob-out").textContent = `${Math.round(st.comment_probability * 100)}%`;
  settingsLoaded = true;
  setDirty(false);
}

function setDirty(v) {
  settingsDirty = v;
  $("settings-dirty").textContent = v ? "有未保存的修改" : "";
}

form.addEventListener("input", (e) => {
  setDirty(true);
  if (e.target.name === "comment_probability") $("prob-out").textContent = `${Math.round(e.target.value * 100)}%`;
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") body[input.name] = input.checked;
    else if (input.type === "number" || input.type === "range") body[input.name] = Number(input.value);
    else body[input.name] = input.value;
  }
  const res = await call("settings", body, form.querySelector("button[type=submit]"));
  if (res) fillSettings();
});

$("btn-settings-reset").addEventListener("click", fillSettings);

// ---------------------------------------------------------------- actions
$("btn-toggle").addEventListener("click", (e) =>
  call("control", { action: state?.running ? "stop" : "start" }, e.currentTarget));
$("btn-browse").addEventListener("click", (e) => call("control", { action: "browse" }, e.currentTarget));
$("btn-reply").addEventListener("click", (e) => call("control", { action: "reply" }, e.currentTarget));
$("quick-review").addEventListener("change", (e) => call("settings", { review_mode: e.target.checked }));
$("btn-reject-all").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (await askConfirm(`确定放弃全部 ${state.pending.length} 条待审核内容吗？`, "全部放弃", true))
    call("pending/reject", { id: "all" }, btn);
});
$("btn-update-check").addEventListener("click", (e) => call("update/check", {}, e.currentTarget));
for (const id of ["btn-update-run", "btn-update-banner"]) {
  $(id).addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const u = state.update;
    if (await askConfirm(`更新到 v${u.latest}？更新期间插件会重载，大约需要几十秒。`, "立即更新"))
      call("update/run", {}, btn);
  });
}
$("btn-login").addEventListener("click", (e) => call("login/start", {}, e.currentTarget));
$("btn-cookie").addEventListener("click", async (e) => {
  const cookie = $("cookie-input").value.trim();
  if (!cookie) return toast("请先粘贴 Cookie", true);
  const res = await call("login/cookie", { cookie }, e.currentTarget);
  if (res) $("cookie-input").value = "";
});
$("btn-logout").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (await askConfirm("退出后需要重新扫码登录，自动运行也会停止。确定退出吗？", "退出登录", true))
    call("login/logout", {}, btn);
});

// ---------------------------------------------------------------- polling
async function refresh() {
  try {
    state = await bridge.apiGet("state");
    render();
  } catch (e) {
    $("account-line").textContent = `加载失败：${e.message || e}`;
  }
}

function schedule() {
  clearTimeout(pollTimer);
  // 扫码或执行任务时刷新快一些
  const fast =
    state &&
    (state.busy ||
      ["loading", "qr"].includes(state.login?.status) ||
      ["checking", "updating"].includes(state.update?.status));
  pollTimer = setTimeout(async () => {
    if (!document.hidden) await refresh();
    schedule();
  }, fast ? 2000 : 5000);
}

await bridge.ready();
await refresh();
schedule();
