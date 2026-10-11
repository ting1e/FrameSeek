// Index management interaction checks using Node, without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children=[]; this.listeners={}; this.value=''; this.hidden=false; this.checked=false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  emit(name) { return Promise.all((this.listeners[name]||[]).map(fn=>fn({preventDefault(){}}))); }
  showModal() { this.open=true; }
  close() { this.open=false; }
}
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
const directory = {source:'sda',directory:'剧集',path:'/media/剧集',files:3,bif:2,images:1,vectors:100,bytes:409600};
const calls=[];
const context = vm.createContext({
  $:element, node:(tag,cls,text)=>Object.assign(new Element(),{tag,cls,textContent:text}),
  directoryRoot:()=>'/media', switchWorkspace:name=>calls.push(name), URLSearchParams,
  setInterval:()=>1, setTimeout:()=>1, clearTimeout(){}, console,
  api:async(path,options)=> {
    calls.push({path,options});
    return options ? {id:'job',status:'pending'} : {directories:[directory],files:[],jobs:[],total_files:3,has_more:false};
  }
});
vm.runInContext(fs.readFileSync('frameseek/static/library.js','utf8'),context);
(async()=> {
  await element('library-tab').emit('click');
  await context.loadLibrary();
  assert.ok(calls.includes('library'));
  const row = element('library-directories').children[0];
  assert.equal(row.children[0].children[0].textContent,'/media/剧集');
  await row.children[0].children[0].emit('click');
  await context.loadLibrary();
  assert.ok(calls.some(call=>call.path?.includes('directory=%E5%89%A7%E9%9B%86')));
  await row.children[4].children[0].emit('click');
  assert.equal(element('library-delete-dialog').open,true);
  assert.equal(element('library-delete-exclude').checked,true);
  assert.match(element('library-delete-count').textContent,/100/);
  element('library-delete-exclude').checked=false;
  await element('library-delete-exclude').emit('change');
  assert.equal(element('library-delete-warning').hidden,false);
  await element('library-delete-confirm').emit('click');
  const request = calls.find(call=>call.options?.method==='POST');
  assert.deepEqual(JSON.parse(request.options.body),{source:'sda',directory:'剧集',exclude:false});
  assert.equal(element('library-delete-dialog').open,false);
  await element('library-home').emit('click');
  await context.loadLibrary();
  assert.equal(element('library-path').textContent,'全部已索引目录');
  const html = fs.readFileSync('frameseek/static/index.html','utf8');
  for (const id of elements.keys()) assert.ok(html.includes(`id="${id}"`),`Missing element ${id}`);
  console.log('Directory navigation, index deletion dialog, exclusion option and page wiring passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
