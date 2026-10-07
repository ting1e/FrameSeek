"use strict";
const $ = id => document.getElementById(id);
let embyEnabled = false, embySaved = null, embyWebRequest = 0;
let taskActionBusy = false;
let hitSorters = [];
let csrf = "", selectedFile = null, previewUrl = null, paused = false, polling = null, historyLoaded = false, searchBusy = false, historyOffset = 0;
function node(tag, cls, text) { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; }
function timeLabel(ms) { const seconds = Math.floor(ms / 1000); return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(v => String(v).padStart(2, "0")).join(":"); }
async function api(path, options = {}) {
  options.headers = {...options.headers};
  if (options.method && options.method !== "GET") options.headers["X-CSRF-Token"] = csrf;
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? ((path === "/api/search" || path.startsWith("/api/emby/")) ? 180000 : 20000);
  delete options.timeoutMs;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try { response = await fetch(path, {...options, credentials:"same-origin", signal:controller.signal}); }
  catch (error) { throw new Error(error.name === "AbortError" ? "请求超时，请稍后重试。" : "无法连接本地服务，请确认服务正在运行。"); }
  finally { clearTimeout(timeout); }
  if (response.status === 401) showLogin();
  if (!response.ok) { const data = await response.json().catch(() => ({})); const error = new Error(typeof data.detail === "string" ? data.detail : `请求失败 (${response.status})`); error.status = response.status; throw error; }
  return response.json();
}
function showLogin() { $("session-loading").hidden = true; historyLoaded = false;  $("login-section").hidden = false; $("search-section").hidden = true; $("header-status").hidden = true; if (polling) clearInterval(polling); polling = null; }
function showSearch() { $("session-loading").hidden = true; if (!historyLoaded) { historyLoaded = true; loadSettings(true).catch(() => {}); loadEmbySettings().catch(() => {}); refreshHistory().catch(() => { historyLoaded = false; }); }  $("login-section").hidden = true; $("search-section").hidden = false; $("header-status").hidden = false; if (!polling) polling = setInterval(() => refreshStatus().catch(() => {}), 10000); }
function taskEventLabel(kind) {
  return ({scan:"目录扫描",watch_update:"变动检查",watch_error:"监听异常",bif_modified:"文件变动",indexed:"特征更新完成",scan_error:"扫描异常",index_error:"处理失败",search_error:"搜索异常",history_error:"历史保存异常"})[kind] || "任务记录";
}
function taskEventMessage(event) {
  if (event.kind === "scan" || event.kind === "watch_update") {
    try { const counts = JSON.parse(event.message); return `发现 ${counts.observed || 0} 个文件，变动 ${counts.modified || 0} 个，加入处理 ${counts.queued || 0} 个，删除 ${counts.deleted || 0} 个，扫描异常 ${counts.scan_errors || 0} 个。`; } catch (_) {}
  }
  return event.message;
}
async function refreshStatus() {
  const status = await api("/api/status"); csrf = status.csrf; showSearch();
  $("frame-count").textContent = status.frames.toLocaleString();
  $("task-frames").textContent = status.frames.toLocaleString();
  const tasks = status.tasks || {queued:status.pending,processing:0,failed:0,stabilizing:0};
  $("pending-count").textContent = `等待稳定 ${tasks.stabilizing} · 待处理 ${tasks.queued} · 处理中 ${tasks.processing} · 失败待重试 ${tasks.failed}`;
  paused = status.paused; $("pause").textContent = paused ? "恢复更新" : "暂停更新";
  const directories = status.monitor_directories || [];
  const labels = {idle:tasks.stabilizing ? "等待文件稳定" : (status.manual_active ? "本次更新仍在进行" : (status.realtime_monitoring ? "正在监听文件变动" : "等待下一次检查")),paused:"更新已暂停",disabled:"手动更新模式",scanning:"正在扫描监控目录",checking_changes:"正在检查变动文件",cleanup:"正在清理旧版本向量",error:"更新遇到错误"};
  $("task-label").textContent = paused ? (status.activity === "paused" ? "更新已暂停" : "正在等待当前分块结束后暂停") : (!directories.length ? "未设置监控目录" : (status.scan_requested ? "检查请求等待执行" : (status.activity.startsWith("indexing:") ? "正在提取画面特征" : (labels[status.activity] || "正在读取任务状态"))));
  $("task-updated").textContent = `状态更新于 ${new Date().toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit",second:"2-digit"})}`;
  $("task-hint").textContent = paused ? "暂停后保留处理进度。点击“恢复更新”继续；已完成的画面仍可搜索。" : (!directories.length ? "到设置页填写监控目录后，再检查文件变动。" : (status.manual_active && !status.auto_update ? "正在执行一次手动更新；全部任务完成后回到手动模式。编码失败会按退避时间重试；扫描异常需修复后再次检查。" : (!status.auto_update ? "点击“检查文件变动”执行一次扫描和更新。只处理新增或修改的 BIF 和图片，文件稳定后再提取特征。" : "系统实时监听文件变动，并定期扫描补查。文件稳定后更新特征，已删除文件会清理索引。")));
  renderTaskProgress(status.progress);
  const current = status.current_task;
  $("task-progress").hidden = !current;
  if (current) $("task-progress").textContent = `${current.root_directory || directoryRoot(current.source)}/${current.relpath}\n已保存 ${current.cursor.toLocaleString()} / ${current.total.toLocaleString()} 帧。完整文件处理完后才可搜索。`;
  $("scan").disabled = taskActionBusy || paused || !directories.length || status.scan_requested || status.activity === "scanning";
  $("pause").disabled = taskActionBusy;
  $("task-error").textContent = !status.model_ready ? "模型尚未准备好，请先完成模型下载。" : (status.worker_error || (status.watcher_error ? "实时监听未启动，当前使用定时扫描。" : "") || (tasks.failed ? `${tasks.failed} 个任务失败待重试，可查看下方记录定位原因。` : ""));
  $("events").replaceChildren(...status.events.map(event => {
    const item = node("li", "");
    item.title = `${new Date(event.time * 1000).toLocaleString()} · ${taskEventLabel(event.kind)} · ${taskEventMessage(event)}`;
    item.append(node("span", "event-time", new Date(event.time * 1000).toLocaleString()), node("strong", "event-kind", taskEventLabel(event.kind)), node("span", "event-message", taskEventMessage(event)));
    return item;
  }));
}
$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("login-error").textContent = ""; const button = event.currentTarget.querySelector("button"); button.disabled = true; button.textContent = "正在登录…";
  try { const data = await api("/api/login", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:$("username").value,password:$("password").value})}); csrf = data.csrf; $("password").value = ""; showSearch(); message("登录成功，正在读取图库状态…"); try { await refreshStatus(); message("上传或粘贴截图，开始搜索。"); } catch (error) { message(error.message, true); } }
  catch (error) { $("login-error").textContent = error.message; } finally { button.disabled = false; button.textContent = "登录"; }
});
$("logout").addEventListener("click", async () => { try { await api("/api/logout", {method:"POST"}); csrf = ""; showLogin(); } catch (error) { message(error.message, true); } });
function setImage(file) {
  if (searchBusy) return message("正在搜索，请完成后再替换图片。");
  if (!file) return;
  if (!["image/jpeg","image/png","image/webp"].includes(file.type)) return message("请选择 JPG、PNG 或 WebP 图片。", true);
  if (file.size > 10 * 1024 * 1024) return message("图片最大 10MB。", true);
  hitSorters = []; $("results").replaceChildren(); $("result-count").textContent = ""; $("timing").textContent = ""; $("empty-state").hidden = false; $("empty-title").textContent = "图片已准备好"; $("empty-description").textContent = "点击“开始搜索”，查看相似画面。";
  $("results-title").textContent = "搜索结果"; $("image-name").textContent = file.name || "粘贴截图"; $("image-tools").hidden = false;
  selectedFile = file; if (previewUrl) URL.revokeObjectURL(previewUrl); previewUrl = URL.createObjectURL(file);
  $("query-preview").src = previewUrl; $("query-preview").hidden = false; $("upload-hint").hidden = true; $("search-button").disabled = false; message("图片已就绪，点击“开始搜索”。");
}
function message(text, error = false) { $("message").textContent = text; $("message").classList.toggle("error", error); }
$("dropzone").addEventListener("click", () => $("file-input").click());
$("dropzone").addEventListener("keydown", event => { if (["Enter"," "].includes(event.key)) { event.preventDefault(); $("file-input").click(); } });
$("file-input").addEventListener("change", event => setImage(event.target.files[0]));
for (const name of ["dragenter","dragover"]) $("dropzone").addEventListener(name, event => { event.preventDefault(); $("dropzone").classList.add("dragging"); });
for (const name of ["dragleave","drop"]) $("dropzone").addEventListener(name, event => { event.preventDefault(); $("dropzone").classList.remove("dragging"); if (name === "drop") setImage(event.dataTransfer.files[0]); });
document.addEventListener("paste", event => { if ($("search-section").hidden || ["INPUT","TEXTAREA"].includes(event.target.tagName)) return; for (const item of event.clipboardData.items) if (item.type.startsWith("image/")) { event.preventDefault(); setImage(item.getAsFile()); break; } });
let selectedDirectory = '', directoryTimer = null, directoryRequest = 0;
function directoryRoot(source) { return monitorSources.find(item => item.source === source)?.root || source; }
function restoreSearchScope(source = '', directory = '', keyword = '') {
  directoryRequest++; clearTimeout(directoryTimer);
  $("search-source").value = source;
  selectedDirectory = directory;
  $("directory-keyword").value = directory ? '' : keyword;
  $("directory-options").hidden = true;
  $("directory-message").textContent = '';
  $("directory-selected").textContent = directory ? `${directoryRoot(source)}/${directory}（含子目录）` : (keyword ? `目录包含“${keyword}”` : (source ? `${directoryRoot(source)} / 全部目录` : '全部来源 / 全部目录'));
}
async function findSearchDirectories() {
  const request = ++directoryRequest;
  const params = new URLSearchParams({source:$("search-source").value, q:$("directory-keyword").value, media_type:$("search-media-type").value || "all"});
  $("directory-message").textContent = '正在查找目录…';
  try {
    const data = await api(`/api/search/directories?${params}`);
    if (request !== directoryRequest) return;
    $("directory-options").replaceChildren(...data.directories.map(item => {
      const button = node('button', 'directory-choice', `${item.display_path || directoryRoot(item.source)+"/"+item.directory} · ${item.file_count ?? item.bif_count} 个文件`);
      button.type = 'button';
      button.addEventListener('click', () => restoreSearchScope(item.source, item.directory));
      return button;
    }));
    $("directory-options").hidden = !data.directories.length;
    $("directory-message").textContent = !data.directories.length ? '没有匹配的已索引目录。' : (data.has_more ? '显示前 50 个目录；直接搜索会覆盖所有匹配目录。' : '可选择具体目录；不选择时按关键词搜索所有匹配目录。');
  } catch (error) { if (request === directoryRequest) { $("directory-options").hidden = true; $("directory-message").textContent = error.message; } }
}
function updateMediaTypeControls() {
  const imagesOnly = $("search-media-type").value === "image";
  $("hit-sort").disabled = imagesOnly; $("collapse").disabled = imagesOnly;
}
$("search-media-type").addEventListener("change", () => { updateMediaTypeControls(); directoryRequest++; clearTimeout(directoryTimer); $("directory-options").hidden = true; findSearchDirectories(); });
$("clear-search-scope").addEventListener('click', () => restoreSearchScope());
$("directory-keyword").addEventListener('input', event => {
  clearTimeout(directoryTimer); directoryRequest++; $("directory-options").hidden = true;
  selectedDirectory = ''; $("search-source").value = "";
  const keyword = $("directory-keyword").value.trim();
  $("directory-selected").textContent = keyword ? `目录包含“${keyword}”` : ($("search-source").value ? `${directoryRoot($("search-source").value)} / 全部目录` : '全部来源 / 全部目录');
  if (!event.isComposing) directoryTimer = setTimeout(findSearchDirectories, 300);
});
$("directory-keyword").addEventListener('compositionend', () => { clearTimeout(directoryTimer); directoryTimer = setTimeout(findSearchDirectories, 300); });
$("directory-keyword").addEventListener('focus', () => findSearchDirectories());
function sortHits(hits, order = "similarity") {
  return [...hits].sort((a, b) => order === "time"
    ? a.time_ms - b.time_ms || b.score - a.score || String(a.id).localeCompare(String(b.id))
    : b.score - a.score || a.time_ms - b.time_ms || String(a.id).localeCompare(String(b.id)));
}
$("hit-sort").addEventListener("change", () => { for (const reorder of hitSorters) reorder(); });
function card(result, rank = 0) {
  const isImage = result.media_type === "image";
  const article = node("article", "card bg-base-100 shadow-sm result-card"), thumbnail = node("button", "thumbnail-button"), image = node("img");
  const unavailable = node("span", "preview-unavailable", "源帧已变化或不可用"); unavailable.hidden = true;
  image.alt = result.relpath; image.loading = "lazy";
  image.addEventListener("error", () => { image.hidden = true; unavailable.hidden = false; });
  image.addEventListener("load", () => { image.hidden = false; unavailable.hidden = true; });
  image.src = result.preview_url; thumbnail.append(unavailable, image, node("span", "time-badge", isImage ? "图片" : timeLabel(result.time_ms)), node("span", "rank-badge", rank + 1)); thumbnail.addEventListener("click", () => isImage ? neighbors(result.id) : (embyEnabled ? playInEmby(selected.id, thumbnail) : neighbors(selected.id)));
  const body = node("div", "card-body"), row = node("div", "card-row"); row.append(node("span", "score", `相似度 ${(result.score * 100).toFixed(1)}%`));
  const frameIndex = !isImage ? node("span", "frame-index", result.frame_no == null ? "命中画面" : `第 ${result.frame_no + 1} 帧`) : null;
  if (frameIndex) row.append(frameIndex);
  const parts = result.relpath.split("/"), filename = parts.pop(); const track = node("div", "score-track"), fill = node("div", "score-fill"); fill.style.width = `${Math.max(0, Math.min(1, result.score)) * 100}%`; track.append(fill); body.append(row, track, node("p", "filename", filename), node("p", "file-path", [result.root_directory || directoryRoot(result.source), ...parts].join("/")));
  if (isImage) {
    const bottom = node("div", "card-bottom"), view = node("button", "btn btn-ghost btn-xs text-primary", "查看图片 ↗");
    view.addEventListener("click", () => neighbors(result.id));
    bottom.append(node("span", "", "图片"), view); body.append(bottom); article.append(thumbnail, body); return article;
  }
  body.append(node("p", "bif-duration", result.duration_ms == null ? "时长未知" : `时长约 ${timeLabel(result.duration_ms)}`));
  const bottom = node("div", "card-bottom"), actions = node("div", "result-actions");
  const nearby = node("button", "btn btn-outline btn-sm result-action result-nearby", "附近画面");
  nearby.type = "button"; nearby.addEventListener("click", () => neighbors(selected.id));
  const play = node("button", "btn btn-primary btn-sm result-action emby-play-button", embyPlayLabel());
  play.type = "button"; play.addEventListener("click", () => playInEmby(selected.id, play)); play.hidden = !embyEnabled;
  actions.append(nearby, play);
  bottom.append(actions); body.append(bottom);
  const members = result.group?.length ? result.group : [result];
  body.append(node("p", "hit-label", `${members.length} 个命中位置`));
  const group = node("div", "group-detail");
  let selected = result;
  const buttons = [];
  const timeBadge = thumbnail.querySelector(".time-badge"), score = row.querySelector(".score");
  for (const member of members) {
    const item = node("button", "btn btn-outline btn-xs hit-position", timeLabel(member.time_ms));
    item.title = `相似度 ${(member.score * 100).toFixed(1)}% · 点击切换画面`;
    item.setAttribute("aria-pressed", String(member.id === selected.id));
    item.classList.toggle("btn-active", member.id === selected.id);
    item.addEventListener("click", () => {
      selected = member; unavailable.hidden = true; image.hidden = false; image.src = member.preview_url || `/api/frames/${member.id}`;
      timeBadge.textContent = timeLabel(member.time_ms); score.textContent = `相似度 ${(member.score * 100).toFixed(1)}%`;
      fill.style.width = `${Math.max(0, Math.min(1, member.score)) * 100}%`;
      frameIndex.textContent = member.frame_no == null ? "命中画面" : `第 ${member.frame_no + 1} 帧`;
      for (const [button, id] of buttons) { button.setAttribute("aria-pressed", String(id === member.id)); button.classList.toggle("btn-active", id === member.id); }
    });
    buttons.push([item, member.id]); group.append(item);
  }
  // Actions follow the currently selected hit, including nonadjacent scenes.
  const reorderHits = () => {
    const byId = new Map(buttons.map(([button, id]) => [id, button]));
    group.replaceChildren(...sortHits(members, $("hit-sort").value).map(hit => byId.get(hit.id)));
  };
  reorderHits(); hitSorters.push(reorderHits);
  body.append(group);
  article.append(thumbnail, body); return article;
}
function groupResults(rows) {
  const files = new Map();
  for (const row of rows) {
    const key = JSON.stringify([row.source, row.relpath, row.version]);
    let entry = files.get(key);
    if (!entry) { entry = {best:row, hits:new Map()}; files.set(key, entry); }
    if (row.score > entry.best.score) entry.best = row;
    for (const hit of row.group?.length ? row.group : [row]) entry.hits.set(hit.id, hit);
  }
  return [...files.values()].map(({best,hits}) => ({...best, group:[...hits.values()].sort((a,b) => a.time_ms-b.time_ms), group_count:hits.size})).sort((a,b) => b.score-a.score);
}
function prepareResults(data) {
  return (data.collapsed ?? data.collapse ?? true) ? groupResults(data.results) : data.results;
}
function embyPlayLabel() { return embySaved?.playback_mode === "emby_web" ? "Emby 网页播放" : "新窗口播放"; }
function updateEmbyButtons() {
  for (const button of document.querySelectorAll(".emby-play-button")) {
    button.hidden = !embyEnabled; button.textContent = embyPlayLabel();
  }
}
function remainingLabel(seconds) {
  if (seconds < 60) return "约 1 分钟以内";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `约 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  if (hours < 24) return `约 ${hours} 小时${rest ? ` ${rest} 分钟` : ""}`;
  return `约 ${Math.floor(hours / 24)} 天${hours % 24 ? ` ${hours % 24} 小时` : ""}`;
}
function renderTaskProgress(progress) {
  if (!progress) { $("processing-metrics").hidden = true; return; }
  $("processing-metrics").hidden = false;
  $("processed-frames").textContent = progress.completed_frames.toLocaleString();
  $("remaining-frames").textContent = progress.remaining_frames.toLocaleString();
  $("processing-speed").textContent = progress.frames_per_second == null ? "—" : `${progress.frames_per_second.toFixed(2)} 帧/秒`;
  const labels = {paused:"已暂停",idle:progress.remaining_frames ? "等待开始处理" : "暂无待处理帧",blocked:"等待文件稳定或失败任务恢复",warming_up:"等待速度稳定"};
  $("processing-eta").textContent = progress.eta_seconds == null ? (labels[progress.state] || "等待速度稳定") : remainingLabel(progress.eta_seconds);
}
function displayResults(data) {
  $("empty-title").textContent = "暂时没有找到可用的画面"; $("empty-description").textContent = "可以换一张截图试试，或等待后台处理更多文件。";
  hitSorters = [];
  const results = prepareResults(data);
  $("results").replaceChildren(...results.map(card)); $("empty-state").hidden = results.length > 0;
  const bifCount = new Set(results.map(row => JSON.stringify([row.source,row.relpath,row.version]))).size;
  const imageCount = new Set(results.filter(row => row.media_type === "image").map(row => JSON.stringify([row.source,row.relpath,row.version]))).size;
  const counts = []; if (bifCount - imageCount) counts.push(`${bifCount - imageCount} 个 BIF`); if (imageCount) counts.push(`${imageCount} 张图片`);
  $("result-count").textContent = counts.length ? `${counts.join(" · ")} · ${results.reduce((sum, row) => sum + (row.group_count ?? row.group?.length ?? 1), 0)} 个命中位置` : "0 个结果"; $("timing").textContent = `${(data.elapsed_ms / 1000).toFixed(2)} 秒`;
}
$("search-button").addEventListener("click", async () => {
  if (!selectedFile || searchBusy) return;
  searchBusy = true; const button = $("search-button"); button.disabled = true; button.textContent = "正在搜索…"; message("正在寻找相似画面，第一次搜索可能需要稍等片刻…");
  try {
    const form = new FormData(); form.append("image", selectedFile); form.append("top", $("top").value); form.append("collapse", $("collapse").checked); form.append("media_type", $("search-media-type").value || "all"); form.append("source", $("search-source").value); form.append("directory", selectedDirectory); form.append("directory_keyword", selectedDirectory ? "" : $("directory-keyword").value.trim());
    const data = await api("/api/search", {method:"POST",body:form}); $("results-title").textContent = "搜索结果"; displayResults(data);
    message(data.history_saved === false ? "搜索完成，但历史记录保存失败。" : (data.candidate_limit_reached ? "已达到候选上限，返回的结果可能少于所选数量。" : "搜索完成，已保存到历史记录。"));
    historyOffset = 0; await refreshHistory().catch(() => {});
  } catch (error) { message(error.message, true); }
  finally { searchBusy = false; button.disabled = !selectedFile; button.textContent = "开始搜索"; }
});

async function neighbors(id) {
  const dialog = $("frame-dialog"); $("neighbor-frames").replaceChildren(); $("dialog-error").textContent = ""; $("dialog-path").textContent = "正在读取…"; if (!dialog.open) dialog.showModal();
  try { const data = await api(`/api/frames/${id}/neighbors`); $("dialog-path").textContent = `${data.root_directory || directoryRoot(data.source)}/${data.relpath}`; for (const frame of data.frames) { const item = node("div", "neighbor"), image = node("img"); image.src = `/api/frames/${frame.id}`; image.alt = data.media_type === "image" ? data.relpath : timeLabel(frame.time_ms); item.append(image, node("p", "", data.media_type === "image" ? "原图预览" : `${timeLabel(frame.time_ms)} · 第 ${frame.frame_no + 1} 帧${frame.id === id ? " · 命中" : ""}`)); $("neighbor-frames").append(item); } }
  catch (error) { $("dialog-error").textContent = error.message; }
}
$("dialog-close").addEventListener("click", () => $("frame-dialog").close());
async function runTaskAction(action) {
  if (taskActionBusy) return;
  taskActionBusy = true; $("scan").disabled = true; $("pause").disabled = true;
  $("task-notice").textContent = "正在提交请求…";
  try {
    const result = await api(`/api/updates/${action}`, {method:"POST"});
    $("task-notice").textContent = result.message;
  } catch (error) { $("task-notice").textContent = error.message; }
  finally {
    taskActionBusy = false;
    try { await refreshStatus(); } catch (error) { $("task-error").textContent = `状态刷新失败：${error.message}`; $("pause").disabled = false; }
  }
}
$("scan").addEventListener("click", () => runTaskAction("scan"));
$("pause").addEventListener("click", () => runTaskAction(paused ? "resume" : "pause"));
$("refresh-tasks").addEventListener("click", async event => {
  const button = event.currentTarget; button.disabled = true;
  try { await refreshStatus(); } catch (error) { $("task-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("task-config").addEventListener("click", () => { switchWorkspace("settings"); loadSettings().catch(error => $("settings-message").textContent = error.message); loadEmbySettings().catch(error => $("emby-message").textContent = error.message); });
async function initializeSession() {
  $("session-loading").hidden = false; $("session-loading").setAttribute("aria-busy", "true");
  $("session-spinner").hidden = false; $("session-retry").hidden = true;
  $("session-message").textContent = "正在加载图库…";
  try { await refreshStatus(); }
  catch (error) {
    if (error.status === 401) return; // api() displays login only for an expired or missing session.
    $("session-loading").setAttribute("aria-busy", "false"); $("session-spinner").hidden = true;
    $("session-message").textContent = error.message; $("session-retry").hidden = false;
  }
}
$("session-retry").addEventListener("click", initializeSession);

function switchWorkspace(workspace) {
  if (typeof workspace === "boolean") workspace = workspace ? "history" : "search";
  for (const [name, tab] of [["search","search-tab"],["history","history-tab"],["tasks","background-tasks"],["settings","settings-tab"]]) {
    $(`${name}-workspace`).hidden = name !== workspace;
    $(tab).classList.toggle("tab-active", name === workspace);
    $(tab).setAttribute("aria-current", name === workspace ? "page" : "false");
  }
}
$("search-tab").addEventListener("click", () => switchWorkspace(false));
$("history-tab").addEventListener("click", () => { switchWorkspace(true); refreshHistory().catch(error => $("history-message").textContent = error.message); });
$("refresh-history").addEventListener("click", () => refreshHistory().catch(error => $("history-message").textContent = error.message));
let pendingDeletion = null, deletionBusy = false;
function requestHistoryDeletion(item) {
  if (deletionBusy) return;
  pendingDeletion = item;
  $("delete-record").textContent = `${item.filename} · ${new Date(item.created * 1000).toLocaleString("zh-CN")}`;
  $("delete-error").textContent = "";
  $("delete-dialog").showModal();
  $("delete-cancel").focus();
}
$("delete-cancel").addEventListener("click", () => { if (!deletionBusy) $("delete-dialog").close(); });
$("delete-dialog").addEventListener("cancel", event => { if (deletionBusy) event.preventDefault(); });
$("delete-dialog").addEventListener("close", () => { pendingDeletion = null; });
$("delete-confirm").addEventListener("click", async () => {
  if (!pendingDeletion || deletionBusy) return;
  deletionBusy = true;
  const button = $("delete-confirm"); button.disabled = true; button.textContent = "正在删除…";
  $("delete-cancel").disabled = true; $("delete-error").textContent = "";
  try {
    await api(`/api/history/${pendingDeletion.id}`, {method:"DELETE"});
    $("delete-dialog").close();
    await refreshHistory().catch(error => { $("history-message").textContent = `记录已删除，但列表刷新失败：${error.message}`; });
  } catch (error) { $("delete-error").textContent = error.message; }
  finally { deletionBusy = false; button.disabled = false; button.textContent = "删除记录"; $("delete-cancel").disabled = false; }
});
async function refreshHistory() {
  const data = await api(`/api/history?offset=${historyOffset}&limit=24`); if (historyOffset && historyOffset >= data.total) { historyOffset = Math.max(0, Math.floor((data.total - 1) / 24) * 24); return refreshHistory(); } $("history-prev").disabled = historyOffset === 0; $("history-next").disabled = !data.has_more; $("history-page").textContent = data.total ? `第 ${Math.floor(historyOffset / 24) + 1} / ${Math.ceil(data.total / 24)} 页 · 共 ${data.total} 次搜索` : ""; $("history-message").textContent = "";
  $("history-empty").hidden = data.items.length > 0;
  $("history-list").replaceChildren(...data.items.map(item => {
    const article = node("article", "card bg-base-100 shadow-sm history-card"), image = node("img", "history-thumbnail");
    image.src = item.thumbnail_url; image.alt = item.filename; image.loading = "lazy";
    const body = node("div", "card-body"), name = node("p", "history-name", item.filename), meta = node("div", "history-meta"); name.title = item.filename;
    meta.append(node("span", "", new Date(item.created * 1000).toLocaleString()), node("span", "badge badge-ghost badge-sm", `${item.returned} 个结果`));
    const actions = node("div", "history-actions"), open = node("button", "btn btn-primary btn-sm", "查看搜索结果"), remove = node("button", "btn btn-ghost btn-sm", "删除");
    open.addEventListener("click", () => openHistory(item.id).catch(error => $("history-message").textContent = error.message));
    remove.addEventListener("click", () => requestHistoryDeletion(item));
    actions.append(open, remove); body.append(name, meta, actions); article.append(image, body); return article;
  }));
}
async function openHistory(id) {
  if (searchBusy) throw new Error("请等待当前搜索完成后再查看历史结果。");
  const item = await api(`/api/history/${id}`);
  const response = await fetch(item.thumbnail_url, {credentials:"same-origin"}); if (!response.ok) throw new Error("历史截图读取失败，请重新登录后重试。");
  setImage(new File([await response.blob()], item.filename, {type:"image/jpeg"}));
  $("top").value = item.response.top; $("collapse").checked = item.response.collapse;
  $("search-media-type").value = item.response.media_type || "all"; updateMediaTypeControls();
  restoreSearchScope(item.response.source || '', item.response.directory || '', item.response.directory_keyword || '');
  displayResults(item.response); $("results-title").textContent = "历史搜索结果"; switchWorkspace(false);
  message(`${new Date(item.created * 1000).toLocaleString()} 的搜索结果。这是当时保存的结果。源文件改变后，部分画面可能无法打开；点击“开始搜索”可重新查找。`);
}
$("query-preview").addEventListener("load", () => { const image = $("query-preview"); $("image-size").textContent = `${image.naturalWidth} × ${image.naturalHeight} · ${(selectedFile.size / 1024).toFixed(0)} KB · 原比例显示`; });
$("replace-query").addEventListener("click", () => { if (!searchBusy) $("file-input").click(); });
$("clear-query").addEventListener("click", () => { if (searchBusy) return; selectedFile = null; if (previewUrl) URL.revokeObjectURL(previewUrl); previewUrl = null; $("query-preview").removeAttribute("src"); $("query-preview").hidden = true; $("upload-hint").hidden = false; $("image-tools").hidden = true; $("file-input").value = ""; $("search-button").disabled = true; message("选择新的截图，或按 Ctrl + V 粘贴。"); });
$("view-query").addEventListener("click", () => { if (!previewUrl) return; $("query-dialog-image").src = previewUrl; $("query-dialog-info").textContent = $("image-size").textContent; $("query-dialog").showModal(); });
$("query-dialog-close").addEventListener("click", () => $("query-dialog").close());

$("history-prev").addEventListener("click", () => { historyOffset = Math.max(0, historyOffset - 24); refreshHistory().catch(error => $("history-message").textContent = error.message); });
$("history-next").addEventListener("click", () => { historyOffset += 24; refreshHistory().catch(error => $("history-message").textContent = error.message); });

$("background-tasks").addEventListener("click", () => { switchWorkspace("tasks"); refreshStatus().catch(error => $("task-error").textContent = error.message); });


function parseMonitorDirectories(text) {
  const directories = [];
  const normalize = value => value.replace(/\\/g, "/").replace(/\/+$/, "");
  for (const line of text.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
    const directory = normalize(line);
    if (/[?*]/.test(directory) || directory.split("/").includes("..")) throw new Error("请填写具体完整目录，不支持通配符或 ..。");
    if (!directory.startsWith("/") && !/^[A-Za-z]:\//.test(directory)) throw new Error("请填写完整目录路径。");
    if (!directories.includes(directory)) directories.push(directory);
  }
  return directories;
}
let monitorSources = [], savedSettings = {};
const settingsControls = {device:"config-device",precision:"config-precision",cpu_threads:"config-cpu-threads",decode_workers:"config-decode-workers",batch:"config-batch",chunk_frames:"config-chunk",scan_interval_seconds:"config-interval",stable_seconds:"config-stable",auto_update:"config-auto-update",default_top:"config-top",collapse_results:"config-collapse"};
let inferenceOptions = [];
function inferenceDeviceHelp() {
  const selected = inferenceOptions.find(option => option.value === $("config-device").value);
  $("inference-device-help").textContent = selected?.reason || ($("config-device").value.startsWith("openvino:") ? "Intel 核显当前逐张推理，批次设置不会增加核显并行数。" : "");
  const device = $("config-device").value, precision = $("config-precision").value;
  $("inference-precision-help").textContent = precision === "fp32" ? "完整精度计算；输出向量保存为 float32。" :
    (device === "cpu" || device === "openvino:CPU") ? "CPU 使用 FP16 不一定更快；输出向量仍为 float32。" :
    device.startsWith("openvino:") ? "混合精度，核显启用激活缩放；输出向量仍为 float32。" :
    "混合精度计算；输出向量仍为 float32，速度以实际任务为准。";
}
$("config-device").addEventListener("change", inferenceDeviceHelp);
$("config-precision").addEventListener("change", inferenceDeviceHelp);
async function loadSettings(initial = false) {
  if (settingsAutoSave.dirty) return;
  const data = await api("/api/settings");
  if (settingsAutoSave.dirty) return;
  inferenceOptions = data.inference_options || [{value:"cpu",label:"CPU",available:true}];
  if (!inferenceOptions.some(option => option.value === data.saved.device)) inferenceOptions.push({value:data.saved.device,label:data.saved.device,available:false,reason:"当前设备不可用"});
  $("config-device").replaceChildren(...inferenceOptions.map(item => {
    const option = document.createElement("option"); option.value = item.value;
    option.textContent = item.label + (item.available ? "" : "（不可用）"); option.disabled = !item.available;
    return option;
  }));
  for (const [name,id] of Object.entries(settingsControls)) {
    const input = $(id); if (input.type === "checkbox") input.checked = data.saved[name];
    else input.value = name === "scan_interval_seconds" ? data.saved[name] / 3600 : data.saved[name];
  }
  savedSettings = data.saved;
  const runningDevice = inferenceOptions.find(option => option.value === data.running.device)?.label || data.running.device;
  $("inference-running").textContent = `当前运行：${runningDevice} / ${data.running.precision.toUpperCase()}`;
  inferenceDeviceHelp();
  $("settings-message").textContent = data.restart_required ? "已保存；推理设备、精度、线程或批次设置需重启应用。" : "";
  monitorSources = data.source_directories;
  $("monitor-directories").value = data.monitor_directories.join("\n");
  if (initial) { $("top").value = data.saved.default_top; $("collapse").checked = data.saved.collapse_results; }
}
$("settings-tab").addEventListener("click", () => { switchWorkspace("settings"); loadSettings().catch(error => $("settings-message").textContent = error.message); loadEmbySettings().catch(error => $("emby-message").textContent = error.message); });
$("reload-settings").addEventListener("click", () => loadSettings().catch(error => $("settings-message").textContent = error.message));
function createAutoSave(formId, messageId, save) {
  let timer = null, revision = 0, savedRevision = 0, running = null, successMessage = "已自动保存";
  const status = text => { $(messageId).textContent = text; };
  const flush = () => {
    clearTimeout(timer);
    if (running) return running;
    running = (async () => {
      while (savedRevision < revision) {
        const current = revision;
        if (!$(formId).checkValidity()) { status("尚未保存，请检查输入内容。"); throw new Error("请检查设置页的输入内容。"); }
        status("正在自动保存…");
        try { successMessage = (await save()) || "已自动保存"; savedRevision = current; }
        catch (error) { status(`保存失败：${error.message}。修改后会再次自动保存。`); throw error; }
      }
      status(successMessage);
    })().finally(() => { running = null; });
    return running;
  };
  const schedule = event => {
    if (event?.isComposing) return;
    revision++; clearTimeout(timer); status("等待自动保存…");
    timer = setTimeout(() => flush().catch(() => {}), event?.type === "change" ? 0 : 800);
  };
  $(formId).addEventListener("input", schedule);
  $(formId).addEventListener("change", schedule);
  $(formId).addEventListener("compositionend", schedule);
  $(formId).addEventListener("submit", event => { event.preventDefault(); flush().catch(() => {}); });
  return {flush, schedule, get dirty() { return savedRevision < revision; }};
}
const settingsAutoSave = createAutoSave("settings-form", "settings-message", async () => {
  const values = {...savedSettings};
  for (const [name,id] of Object.entries(settingsControls)) { const input = $(id); values[name] = input.type === "checkbox" ? input.checked : (["device","precision"].includes(name) ? input.value : Number(input.value)); }
  values.monitor_directories = parseMonitorDirectories($("monitor-directories").value);
  values.scan_interval_seconds = Math.round(values.scan_interval_seconds * 3600);
  const result = await api("/api/settings", {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(values)});
  monitorSources = result.source_directories;
  savedSettings = values;
  $("top").value = values.default_top; $("collapse").checked = values.collapse_results;
  return result.restart_required ? "已保存；推理设备、精度、线程或批次设置需重启应用。" : "已保存；监控目录在下一轮扫描生效。";
});

async function loadEmbySettings() {
  if (embyAutoSave.dirty) return;
  const config = await api("/api/emby/settings");
  if (embyAutoSave.dirty) return;
  embySaved = config; embyEnabled = config.enabled; updateEmbyButtons();
  $("emby-url").value = config.server_url; $("emby-key").value = "";
  $("emby-key").placeholder = config.api_key_set ? "已保存；留空保留原值" : "请输入 Emby API Key";
  $("emby-enabled").checked = config.enabled;
  $("emby-mode").value = config.playback_mode; $("emby-policy").value = config.web_target; updateEmbyMode();
  for (const [id, value] of [["emby-user",config.user_id],["emby-device",config.device_id]]) {
    const option = node("option", "", value ? "已保存，读取列表可重新选择" : (id === "emby-device" ? "不限网页设备" : "尚未选择")); option.value = value;
    $(id).replaceChildren(option);
  }

}
function updateEmbyMode() {
  const web = $("emby-mode").value === "emby_web";
  $("emby-policy-field").hidden = !web; $("emby-device-field").hidden = !web; $("emby-web-help").hidden = !web;
}
$("emby-mode").addEventListener("change", updateEmbyMode);
async function playInEmby(id, button) {
  if (!embyEnabled) { message("请先在设置页启用 Emby 播放。", true); return; }
  if (embySaved?.playback_mode !== "emby_web") {
    window.open(`/emby/player/${encodeURIComponent(id)}`, "_blank", "noopener,noreferrer"); return;
  }
  // New-tab mode reserves during the click; reuse mode creates no tab unless needed.
  let popup = embySaved.web_target === "recent" ? null : window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  const request = ++embyWebRequest;
  let opened = false;
  message("正在选择 Emby 网页端…"); button.disabled = true;
  try {
    const deadline = Date.now() + 90000;
    const plan = await api(`/api/emby/web-begin/${encodeURIComponent(id)}`, {method:"POST",timeoutMs:90000});
    const openWeb = url => {
      if (!popup) { popup = window.open("about:blank", "_blank"); if (popup) popup.opener = null; }
      if (!popup || popup.closed) throw new Error("未能打开 Emby 网页端，请允许弹出窗口后重试。");
      popup.location.href = url; opened = true;
    };
    if (plan.local) { openWeb(plan.player_url); message("Emby 未匹配到视频，已改为直接播放同目录 MP4。"); return; }
    if (plan.open_web) openWeb(plan.web_url);
    while (request === embyWebRequest && Date.now() < deadline) {
      const result = await api(`/api/emby/web-play/${encodeURIComponent(id)}`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket:plan.ticket}),timeoutMs:Math.max(1000,deadline-Date.now())});
      if (request !== embyWebRequest) return;
      if (result.local) { openWeb(result.player_url); message("已改为直接播放同目录 MP4。"); return; }
      if (result.open_web && !opened) openWeb(result.web_url);
      if (!result.waiting) {
        if (!opened && popup && !popup.closed) popup.close();
        message(`已向 ${result.target_device || "Emby 网页端"} 发送播放及定位请求。${result.shared_session ? "新标签页共享已有会话，按更新后的活动时间选择。" : ""}`); return;
      }
      message("正在等待 Emby 网页端登录或更新活动状态…");
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    if (request === embyWebRequest) message("等待 Emby 网页端超时。请登录所选用户，核对设备范围后重新点击结果。", true);
  } catch (error) { if (request === embyWebRequest) message(error.message, true); }
  finally {
    if (!opened && popup && !popup.closed) popup.close();
    button.disabled = false;
  }
}
const embyAutoSave = createAutoSave("emby-form", "emby-message", async () => {
  if ($("emby-enabled").checked && (!$("emby-url").value.trim() || !($("emby-key").value.trim() || embySaved?.api_key_set) || !$("emby-user").value)) throw new Error("请先填写连接信息、读取并选择播放用户，再启用播放。");
  const source_paths = {...(embySaved?.source_paths || {})};
  const key = $("emby-key").value.trim();
  const config = await api("/api/emby/settings", {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:$("emby-enabled").checked,playback_mode:$("emby-mode").value,web_target:$("emby-policy").value,device_id:$("emby-device").value,server_url:$("emby-url").value.trim(),api_key:key,user_id:$("emby-user").value,source_paths})});
  embySaved = config; embyEnabled = config.enabled; updateEmbyButtons();
  if ($("emby-key").value.trim() === key) $("emby-key").value = "";
  $("emby-key").placeholder = config.api_key_set ? "已保存；留空保留原值" : "请输入 Emby API Key";
});
$("emby-connect").addEventListener("click", async () => {
  const button = $("emby-connect"); button.disabled = true; $("emby-message").textContent = "正在连接 Emby…";
  try {
    if (embyAutoSave.dirty) await embyAutoSave.flush();
    const data = await api("/api/emby/clients");
    const user = $("emby-user").value, selectedDevice = $("emby-device").value;
    const placeholder = node("option", "", "请选择用户"); placeholder.value = "";
    $("emby-user").replaceChildren(placeholder, ...data.users.map(item => {const option = node("option", "", item.name); option.value = item.id; return option;}));
    $("emby-user").value = data.users.some(item => item.id === user) ? user : (data.users.length === 1 ? data.users[0].id : "");
    const renderDevices = () => {
      const placeholder = node("option", "", "不限网页设备"); placeholder.value = "";
      const devices = (data.devices || []).filter(item => item.user_id === $("emby-user").value);
      const unique = [...new Map(devices.map(item => [item.device_id,item])).values()];
      $("emby-device").replaceChildren(placeholder, ...unique.map(item => {const option = node("option", "", `${item.name} · ${item.client}`); option.value = item.device_id; return option;}));
      if (unique.some(item => item.device_id === selectedDevice)) $("emby-device").value = selectedDevice;
    };
    $("emby-user").onchange = renderDevices; renderDevices();
    embyAutoSave.schedule({type:"change"});
    $("emby-message").textContent = "连接成功。选择播放用户和方式；网页端模式可选择新开或优先复用策略，也可限制网页设备范围，修改后自动保存。";
  } catch (error) { $("emby-message").textContent = error.message; }
  finally { button.disabled = false; }
});
$("emby-refresh").addEventListener("click", async () => {
  const button = $("emby-refresh"); button.disabled = true; $("emby-message").textContent = "正在读取 Emby 视频路径…";
  try { const result = await api("/api/emby/refresh", {method:"POST"}); $("emby-message").textContent = result.message; }
  catch (error) { $("emby-message").textContent = error.message; }
  finally { button.disabled = false; }
});

function updateBackToTop() {
  const page = document.scrollingElement || document.documentElement;
  $("back-to-top").hidden = !(page.scrollHeight > window.innerHeight + 1 && page.scrollTop > 200);
}
$("back-to-top").addEventListener("click", () => {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({top:0, behavior:reducedMotion ? "instant" : "smooth"});
});
document.addEventListener("scroll", updateBackToTop, {passive:true});
window.addEventListener("resize", updateBackToTop, {passive:true});
updateBackToTop();

initializeSession();
