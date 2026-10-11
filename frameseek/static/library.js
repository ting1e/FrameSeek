/* Directory index management shares the authenticated API and navigation. */
let librarySource = '', libraryDirectory = '', libraryOffset = 0, librarySequence = 0;
let libraryDeletion = null, libraryDeleteBusy = false, libraryTimer = null;
let libraryJobsSignature = '', libraryPolling = false, librarySizesPending = false;
function libraryBytes(value) {
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
}
async function refreshLibrary() {
  const sequence = ++librarySequence;
  $("library-message").textContent = '正在读取目录索引…';
  const data = await api('/api/index/library?' + new URLSearchParams({source:librarySource,
    directory:libraryDirectory, keyword:$("library-keyword").value.trim(), offset:libraryOffset}));
  if (sequence !== librarySequence) return;
  libraryJobsSignature = JSON.stringify(data.jobs);
  librarySizesPending = data.directories.some(item => item.size_pending);
  $("library-path").textContent = librarySource ? `${directoryRoot(librarySource)}${libraryDirectory ? '/'+libraryDirectory : ''}（含子目录）` : '全部已索引目录';
  $("library-up").disabled = !librarySource;
  $("library-message").textContent = data.directories_truncated ? '显示前 200 个目录，请细化关键词。' : (data.directories.length ? '' : '没有匹配的目录索引。');
  $("library-directories").replaceChildren(...data.directories.map(item => {
    const row = node('tr'), name = node('td'), open = node('button', 'btn btn-ghost btn-sm library-directory', item.path);
    open.addEventListener('click', () => { librarySource = item.source; libraryDirectory = item.directory; libraryOffset = 0; $("library-keyword").value = ''; loadLibrary(); });
    name.append(open);
    const actions = node('td'), remove = node('button', 'btn btn-outline btn-error btn-sm', '删除索引');
    remove.addEventListener('click', () => {
      libraryDeletion = item;
      $("library-delete-path").textContent = item.path;
      $("library-delete-count").textContent = `${item.files.toLocaleString()} 个文件 · ${item.vectors.toLocaleString()} 个可搜索向量 · ${item.size_pending ? '分块大小统计中' : libraryBytes(item.bytes)+' 原始分块'}`;
      $("library-delete-exclude").checked = true; $("library-delete-warning").hidden = true;
      $("library-delete-error").textContent = ''; $("library-delete-dialog").showModal();
    });
    actions.append(remove);
    row.append(name, node('td','',`${item.bif.toLocaleString()} / ${item.images.toLocaleString()}`),
      node('td','',item.vectors.toLocaleString()), node('td','',item.size_pending ? '统计中' : libraryBytes(item.bytes)), actions);
    return row;
  }));
  const statuses = {ready:'已完成', queued:'待处理', pending:'待处理', processing:'处理中', failed:'待重试', observed:'等待稳定', deleted:'已移除'};
  $("library-files").replaceChildren(...data.files.map(item => {
    const row = node('tr');
    row.append(node('td','library-filename',item.relpath), node('td','',item.media_type === 'image' ? '图片' : 'BIF'),
      node('td','',statuses[item.status] || item.status), node('td','',item.vectors.toLocaleString()),
      node('td','',item.updated ? new Date(item.updated*1000).toLocaleString() : '—'));
    return row;
  }));
  $("library-page").textContent = librarySource ? `共 ${data.total_files.toLocaleString()} 个文件 · 第 ${Math.floor(libraryOffset/50)+1} 页` : '选择目录查看文件';
  $("library-prev").disabled = libraryOffset === 0; $("library-next").disabled = !data.has_more;
  $("library-jobs").replaceChildren(...data.jobs.map(job => {
    const item = node('p','library-job');
    const state = job.status === 'done' ? '已完成' : (job.error ? '失败，稍后重试' : (job.status === 'pending' ? '等待当前操作让出资源' : '清理中'));
    item.textContent = `${directoryRoot(job.source)}${job.directory ? '/'+job.directory : ''} · ${state} · ${job.completed}/${job.total} 个文件版本${job.error ? ' · '+job.error : ''}`;
    return item;
  }));
  if (!data.jobs.length) $("library-jobs").textContent = '暂无清理任务。';
}
function loadLibrary() { return refreshLibrary().catch(error => $("library-message").textContent = error.message); }
$("library-tab").addEventListener('click', () => { switchWorkspace('library'); loadLibrary(); });
$("library-refresh").addEventListener('click', loadLibrary);
$("library-home").addEventListener('click', () => { librarySource = ''; libraryDirectory = ''; libraryOffset = 0; $("library-keyword").value = ''; loadLibrary(); });
$("library-up").addEventListener('click', () => {
  if (libraryDirectory) libraryDirectory = libraryDirectory.split('/').slice(0,-1).join('/'); else librarySource = '';
  libraryOffset = 0; $("library-keyword").value = ''; loadLibrary();
});
$("library-keyword").addEventListener('input', () => { clearTimeout(libraryTimer); librarySequence++; libraryOffset = 0; libraryTimer = setTimeout(loadLibrary,300); });
$("library-prev").addEventListener('click', () => { libraryOffset = Math.max(0,libraryOffset-50); loadLibrary(); });
$("library-next").addEventListener('click', () => { libraryOffset += 50; loadLibrary(); });
$("library-delete-exclude").addEventListener('change', () => $("library-delete-warning").hidden = $("library-delete-exclude").checked);
$("library-delete-cancel").addEventListener('click', () => { if (!libraryDeleteBusy) $("library-delete-dialog").close(); });
$("library-delete-dialog").addEventListener('cancel', event => { if (libraryDeleteBusy) event.preventDefault(); });
$("library-delete-confirm").addEventListener('click', async () => {
  if (!libraryDeletion || libraryDeleteBusy) return;
  libraryDeleteBusy = true; $("library-delete-confirm").disabled = true; $("library-delete-cancel").disabled = true;
  try {
    await api('/api/index/removals', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source:libraryDeletion.source, directory:libraryDeletion.directory, exclude:$("library-delete-exclude").checked})});
    $("library-delete-dialog").close(); libraryDeletion = null; await loadLibrary();
  } catch (error) { $("library-delete-error").textContent = error.message; }
  finally { libraryDeleteBusy = false; $("library-delete-confirm").disabled = false; $("library-delete-cancel").disabled = false; }
});
setInterval(async () => {
  if (libraryPolling || $("library-workspace").hidden || $("search-section").hidden) return;
  libraryPolling = true;
  try {
    const data = await api('/api/index/removals');
    if (librarySizesPending || JSON.stringify(data.jobs) !== libraryJobsSignature) await loadLibrary();
  } catch (error) { $("library-message").textContent = error.message; }
  finally { libraryPolling = false; }
},10000);
