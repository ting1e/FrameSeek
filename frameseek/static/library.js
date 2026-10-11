/* Directory index management shares the authenticated API and navigation. */
let librarySource = '', libraryDirectory = '', librarySequence = 0;
let libraryDeletion = null, libraryDeleteBusy = false, libraryTimer = null;
let libraryJobsSignature = '', libraryPolling = false, librarySizesPending = false;
function libraryBytes(value) {
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
}
async function refreshLibrary() {
  const sequence = ++librarySequence;
  $("library-message").textContent = '正在读取目录索引…';
  const data = await api('/api/index/library?' + new URLSearchParams({source:librarySource,
    directory:libraryDirectory, keyword:$("library-keyword").value.trim()}));
  if (sequence !== librarySequence) return;
  libraryJobsSignature = JSON.stringify(data.jobs);
  librarySizesPending = data.directories.some(item => item.size_pending);
  $("library-path").textContent = librarySource ? `${directoryRoot(librarySource)}${libraryDirectory ? '/'+libraryDirectory : ''}（含子目录）` : '全部已索引目录';
  $("library-up").disabled = !librarySource;
  const crumbs = [], addCrumb = (label, source, directory) => {
    const item = node('li'), button = node('button','',label);
    button.addEventListener('click', () => { librarySource=source; libraryDirectory=directory; $("library-keyword").value=''; loadLibrary(); });
    item.append(button); crumbs.push(item);
  };
  addCrumb('全部目录','','');
  if (librarySource) {
    addCrumb(directoryRoot(librarySource),librarySource,'');
    const parts = libraryDirectory ? libraryDirectory.split('/') : [];
    parts.forEach((part,index) => addCrumb(part,librarySource,parts.slice(0,index+1).join('/')));
  }
  $("library-breadcrumbs").replaceChildren(...crumbs);
  $("library-message").textContent = data.directories_truncated ? '显示前 200 个目录，请细化关键词。' : (data.directories.length ? '' : '没有匹配的目录索引。');
  $("library-directories").replaceChildren(...data.directories.map(item => {
    const row = node('tr','hover'), name = node('td','library-name'), open = node('button', 'btn btn-ghost btn-sm library-directory', item.path);
    open.addEventListener('click', () => { librarySource = item.source; libraryDirectory = item.directory; $("library-keyword").value = ''; loadLibrary(); });
    name.append(open);
    const copy = node('button','btn btn-ghost btn-xs library-copy','复制');
    copy.title='复制完整目录';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(item.path); copy.textContent='已复制'; }
      catch (_) { $("library-message").textContent='无法复制，请选择目录文字复制。'; }
    });
    name.append(copy);
    const actions = node('td'), remove = node('button', 'btn btn-outline btn-error btn-sm', '删除索引');
    remove.addEventListener('click', () => {
      libraryDeletion = item;
      $("library-delete-path").textContent = item.path;
      $("library-delete-count").textContent = `${item.files.toLocaleString()} 个文件 · ${item.vectors.toLocaleString()} 个可搜索向量 · ${item.size_pending ? '分块大小统计中' : libraryBytes(item.bytes)+' 原始分块'}`;
      $("library-delete-exclude").checked = true; $("library-delete-warning").hidden = true;
      $("library-delete-error").textContent = ''; $("library-delete-dialog").showModal();
    });
    actions.append(remove);
    row.append(name, node('td','library-number',`${item.bif.toLocaleString()} / ${item.images.toLocaleString()}`),
      node('td','library-number',item.vectors.toLocaleString()), node('td','library-number',item.size_pending ? '统计中' : libraryBytes(item.bytes)), actions);
    return row;
  }));
  if (!data.directories.length) {
    const row=node('tr'), cell=node('td','library-empty','没有匹配的目录。可以返回上一级，或换一个关键词。');
    cell.colSpan=5; row.append(cell); $("library-directories").append(row);
  }
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
$("library-home").addEventListener('click', () => { librarySource = ''; libraryDirectory = ''; $("library-keyword").value = ''; loadLibrary(); });
$("library-up").addEventListener('click', () => {
  if (libraryDirectory) libraryDirectory = libraryDirectory.split('/').slice(0,-1).join('/'); else librarySource = '';
  $("library-keyword").value = ''; loadLibrary();
});
$("library-keyword").addEventListener('input', () => { clearTimeout(libraryTimer); librarySequence++; libraryTimer = setTimeout(loadLibrary,300); });
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
