"use strict";
const video = document.getElementById("player-video"), notice = document.getElementById("player-message");
let csrf = "", stopUrl = "", offset = 0;
const frameId = location.pathname.split("/").pop();
async function json(url, options = {}) {
  const response = await fetch(url, {...options, credentials:"same-origin"});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : (response.status === 401 ? "登录已过期，请返回搜索页面登录。" : "播放请求失败。"));
  return data;
}
async function start() {
  try {
    const status = await json("/api/status"); csrf = status.csrf;
    const source = location.pathname.startsWith('/player/local/') ? 'local' : 'emby';
    const data = await json(`/api/${source}/open/${encodeURIComponent(frameId)}`, {method:"POST",headers:{"X-CSRF-Token":csrf}});
    stopUrl = data.stop_url; offset = data.offset_seconds;
    document.getElementById("player-title").textContent = data.name;
    document.title = data.name + (data.local ? " · 视频播放" : " · Emby 网页播放");
    notice.textContent = data.local ? "已找到同目录 MP4，正在从命中位置加载…" : (data.transcoding ? "由 Emby 转为浏览器可播放的视频，正在从命中位置加载…" : "正在从命中位置加载视频…");
    video.addEventListener("loadedmetadata", async () => {
      if (data.seek_seconds) video.currentTime = data.seek_seconds;
      try { await video.play(); notice.textContent = ""; }
      catch (_) { notice.textContent = "视频已准备好，点击播放器的播放按钮开始。"; }
    }, {once:true});
    video.src = data.stream_url;
  } catch (error) { notice.textContent = error.message; document.getElementById("player-retry").hidden = false; }
}
video.addEventListener("timeupdate", () => {
  const seconds = Math.floor(video.currentTime + offset);
  document.getElementById("player-position").textContent = `视频位置 ${[Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(value => String(value).padStart(2,"0")).join(":")}`;
});
video.addEventListener("error", () => { notice.textContent = "视频无法播放，请检查网络及浏览器是否支持该视频的编码格式。"; document.getElementById("player-retry").hidden = false; });
function stop() {
  if (stopUrl) { fetch(stopUrl,{method:"POST",headers:{"X-CSRF-Token":csrf},credentials:"same-origin",keepalive:true}).catch(() => {}); stopUrl = ""; }
}
window.addEventListener("pagehide", stop);
document.getElementById("player-retry").addEventListener("click", () => { stop(); location.reload(); });
start();
