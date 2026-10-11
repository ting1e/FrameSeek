const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
  constructor(){this.value='';this.hidden=false;this.listeners={};}
  addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}
  emit(name){return Promise.all((this.listeners[name]||[]).map(fn=>fn({preventDefault(){}})));}
  showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map(),calls=[];
const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let saved={enabled:false,token_set:false,jobs:0,queued:0};
const context=vm.createContext({$:element,window:{location:{origin:'https://example.test'}},navigator:{clipboard:{writeText:async text=>calls.push({copy:text})}},
  api:async(path,options)=>{
    calls.push({path,options});
    if(options?.method==='PUT'){
      const values=JSON.parse(options.body);
      if(values.generate)saved={...saved,token_set:true};
      if(values.enabled!==undefined)saved.enabled=values.enabled;
      if(values.clear)saved={...saved,enabled:false,token_set:false};
      return {...saved,...(values.generate?{token:'generated-test-token-abcdefghijklmnopqrstuvwxyz'}:{})};
    }
    return {...saved};
  },console});
vm.runInContext(fs.readFileSync('frameseek/static/api-settings.js','utf8'),context);
(async()=>{
  await element('settings-tab').emit('click');
  assert.equal(element('api-service-enabled').disabled,true);
  assert.match(element('api-service-example').textContent,/Bearer YOUR_TOKEN/);
  await element('api-token-generate').emit('click');
  assert.equal(element('api-service-enabled').disabled,false);
  assert.equal(element('api-token-once').hidden,false);
  assert.ok(element('api-token-issued').value.startsWith('generated'));
  await element('api-token-copy').emit('click');assert.ok(calls.some(c=>c.copy));
  element('api-service-enabled').checked=true;await element('api-service-enabled').emit('change');
  assert.equal(element('api-service-state').textContent,'已开启');
  await element('api-cache-clear').emit('click');assert.equal(element('api-cache-dialog').open,true);
  await element('api-cache-confirm').emit('click');assert.equal(element('api-cache-dialog').open,false);
  await element('api-token-clear').emit('click');assert.equal(element('api-service-enabled').checked,false);
  assert.equal(element('api-token-issued').value,'');
  const html=fs.readFileSync('frameseek/static/index.html','utf8');
  for(const id of elements.keys())assert.ok(html.includes(`id="${id}"`),`Missing ${id}`);
  assert.ok(!html.includes('id="library-files"'));
  assert.ok(html.includes('table table-zebra library-table'));
  const css=fs.readFileSync('frameseek/static/daisyui.css','utf8');
  for(const selector of ['.table-zebra','.breadcrumbs','.badge-success'])assert.ok(css.includes(selector),`Missing compiled DaisyUI style ${selector}`);
  console.log('API settings, token generation/copy, toggle, cache dialog and directory table wiring passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
