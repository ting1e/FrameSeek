// Session startup regression tests using Node only; no browser or screenshots.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('frameseek/static/index.html', 'utf8');
const source = fs.readFileSync('frameseek/static/app.js', 'utf8');
async function check(outcome) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      hidden: new RegExp(`<[^>]*id="${id}"[^>]*\\bhidden\\b`).test(html),
      textContent: '', dataset: {}, setAttribute() {}, addEventListener() {},
      replaceChildren() {}, append() {}, classList: {toggle() {}}
    });
    return elements.get(id);
  };
  let resolve, reject;
  const request = new Promise((yes, no) => { resolve = yes; reject = no; });
  const context = vm.createContext({
    window: {innerHeight:800, addEventListener() {}},
    document: {documentElement:{scrollHeight:800,scrollTop:0}, getElementById: element, querySelectorAll: () => [], addEventListener() {}, createElement: () => element('created')},
    fetch: () => request, AbortController, setTimeout, clearTimeout,
    setInterval: () => 1, clearInterval, console
  });
  vm.runInContext(source, context);
  assert.equal(element('back-to-top').hidden, true);
  context.document.documentElement.scrollHeight = 2000;
  context.document.documentElement.scrollTop = 300;
  context.updateBackToTop();
  assert.equal(element('back-to-top').hidden, false);
  context.document.documentElement.scrollTop = 0;
  context.updateBackToTop();
  assert.equal(element('back-to-top').hidden, true);
  context.restoreSearchScope('sda','剧集/子目录');
  assert.equal(element('search-source').value, 'sda');
  assert.match(element('directory-selected').textContent, /剧集\/子目录/);
  assert.equal(element('directory-options').hidden, true);
  context.restoreSearchScope();
  assert.equal(element('search-source').value, '');
  assert.equal(element('directory-selected').textContent, '全部来源 / 全部目录');
  context.restoreSearchScope('', '', '目标');
  assert.equal(element('directory-keyword').value, '目标');
  assert.match(element('directory-selected').textContent, /目录包含/);
  context.restoreSearchScope('sda', '目标', 'ignored');
  assert.equal(element('directory-keyword').value, '');
  context.restoreSearchScope();
  const grouped = JSON.parse(JSON.stringify(context.groupResults([
    {id:'a',source:'sda',relpath:'one.bif',version:'v',score:.8,time_ms:0},
    {id:'b',source:'sda',relpath:'one.bif',version:'v',score:.9,time_ms:9000},
    {id:'c',source:'sdc',relpath:'one.bif',version:'v',score:.7,time_ms:0}
  ])));
  assert.equal(grouped.length, 2);
  assert.equal(grouped[0].id, 'b');
  assert.deepEqual(grouped[0].group.map(hit => hit.id), ['a','b']);
  const hits = [{id:'a',score:.8,time_ms:1000},{id:'b',score:.9,time_ms:9000},{id:'c',score:.9,time_ms:3000}];
  assert.deepEqual(Array.from(context.sortHits(hits), hit => hit.id), ['c','b','a']);
  assert.deepEqual(Array.from(context.sortHits(hits, 'time'), hit => hit.id), ['a','c','b']);
  assert.deepEqual(hits.map(hit => hit.id), ['a','b','c'], 'sorting must not mutate saved results');
  element('autosave-test-form').checkValidity = () => true;
  let saveCount = 0, finishSave;
  const saver = context.createAutoSave('autosave-test-form', 'autosave-test-message', async () => {
    saveCount++;
    if (saveCount === 1) await new Promise(resolve => { finishSave = resolve; });
  });
  saver.schedule({type:'input'});
  const saving = saver.flush();
  saver.schedule({type:'input'});
  const sameSave = saver.flush();
  assert.equal(saving, sameSave, 'concurrent saves must share one queue');
  finishSave(); await saving;
  assert.equal(saveCount, 2, 'edits during save must be saved after the first request');
  assert.equal(saver.dirty, false);
  assert.equal(element('autosave-test-message').textContent, '已自动保存');
  const parse = text => JSON.parse(JSON.stringify(context.parseMonitorDirectories(text)));
  assert.deepEqual(parse('/mnt/test/sda/video\n/mnt/test/sdc/video/国产剧'), ['/mnt/test/sda/video','/mnt/test/sdc/video/国产剧']);
  assert.deepEqual(parse('D:\\test-app\\bif\\sda\\mv\n/mnt/test/sda/video/mv/'), ['D:/test-app/bif/sda/mv','/mnt/test/sda/video/mv']);
  assert.deepEqual(parse(''), []);
  assert.deepEqual(parse('/mnt/new-directory\n/mnt/new-directory/'), ['/mnt/new-directory']);
  assert.throws(() => parse('relative/directory'));
  assert.throws(() => parse('/mnt/test/sda/video/../outside'));
  assert.throws(() => parse('/mnt/test/sda/video/*'));
  vm.runInContext('loadSettings = async () => {}; loadEmbySettings = async () => {}; refreshHistory = async () => {};', context);
  assert.equal(element('login-section').hidden, true, 'login must remain hidden while session check is pending');
  assert.equal(element('search-section').hidden, true);
  assert.equal(element('session-loading').hidden, false);
  const status = {
    frames: 0, ready_files: 0, pending: 0, sources: [], activity: 'disabled', events: [], model_ready: true,
    items: [], total: 0, snapshot: 0, has_more: false
  };
  if (outcome === 'network') reject(new Error('offline'));
  else resolve({status: outcome, ok: outcome === 200, json: async () => status});
  await new Promise(yes => setImmediate(yes));
  assert.equal(element('login-section').hidden, outcome !== 401);
  assert.equal(element('search-section').hidden, outcome !== 200);
  assert.equal(element('session-loading').hidden, outcome !== 'network');
  if (outcome === 'network') assert.equal(element('session-retry').hidden, false);
  if (outcome === 200) {
    assert.equal(element('scan').disabled, true); // No monitored directories.
    status.monitor_directories = ['/mnt/test/sda/video'];
    status.paused = true; status.activity = 'indexing:one';
    await vm.runInContext('refreshStatus()', context);
    assert.equal(element('pause').textContent, '恢复更新');
    assert.equal(element('scan').disabled, true);
    assert.match(element('task-label').textContent, /分块/);
    status.paused = false; status.scan_requested = true;
    await vm.runInContext('refreshStatus()', context);
    assert.equal(element('task-label').textContent, '检查请求等待执行');
    assert.equal(element('scan').disabled, true);
    status.scan_requested = false; status.activity = 'disabled';
    await vm.runInContext('refreshStatus()', context);
    assert.equal(element('scan').disabled, false);
    assert.equal(element('pause').textContent, '暂停更新');
    assert.match(context.taskEventMessage({kind:'scan',message:'{"observed":10,"modified":2}'}), /变动 2 个/);
    const windows=[], calls=[], popups=[];
    context.window={open:(...args)=>{windows.push(args); const popup={location:{href:args[0]},closed:false,close(){this.closed=true;}}; popups.push(popup); return popup;}};
    let openWeb=true;
    context.fetch=async (url,options)=>{ calls.push([url,options]); return {status:200,ok:true,json:async()=>url.includes('web-begin') ? {ticket:'ticket',open_web:openWeb,web_url:'https://emby.example/web/index.html'} : {ok:true,message:'已发送播放请求'}}; };
    vm.runInContext('embyEnabled=true;embySaved={playback_mode:"standalone",server_url:"https://emby.example"}',context);
    await vm.runInContext('playInEmby("frame1",$("scan"))',context);
    assert.equal(windows[0][0],'/emby/player/frame1');
    assert.equal(calls.length,0);
    vm.runInContext('embySaved.playback_mode="emby_web"',context);
    await vm.runInContext('playInEmby("frame2",$("scan"))',context);
    assert.equal(windows[1][0],'about:blank');
    assert.equal(popups[1].location.href,'https://emby.example/web/index.html');
    assert.equal(calls[0][0],'/api/emby/web-begin/frame2');
    assert.equal(calls[1][0],'/api/emby/web-play/frame2');
    assert.equal(JSON.parse(calls[1][1].body).ticket,'ticket');
    assert.equal(calls[0][1].method,'POST');
    assert.equal(calls[0][1].timeoutMs,undefined);
    openWeb=false;
    vm.runInContext('embySaved.web_target="recent"',context);
    await vm.runInContext('playInEmby("frame3",$("scan"))',context);
    assert.equal(popups.length,2); // Existing page is reused without opening another tab.
    element('emby-mode').value='emby_web'; vm.runInContext('updateEmbyMode()',context);
    assert.equal(element('emby-device-field').hidden,false);
    element('emby-mode').value='standalone'; vm.runInContext('updateEmbyMode()',context);
    assert.equal(element('emby-device-field').hidden,true);
  }
}
(async () => {
  for (const outcome of [200, 401, 'network']) await check(outcome);
  console.log('Session startup: pending, authenticated, expired and network failure checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
