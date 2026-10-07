// Result-card and progress behavior checks without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag='div') { this.tagName=tag.toUpperCase(); this.children=[]; this.listeners={}; this.style={}; this.dataset={}; this.hidden=false; this.className=''; this.attributes={}; this.textContent=''; this.value=''; this.classList={toggle(){},add(){},remove(){}}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  setAttribute(key,value) { this.attributes[key]=value; }
  get firstChild() { return this.children[0]; }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
  querySelectorAll(selector) { const matches=[]; for (const child of this.children) { if (selector.startsWith('.') ? child.className.split(' ').includes(selector.slice(1)) : child.tagName===selector.toUpperCase()) matches.push(child); matches.push(...child.querySelectorAll(selector)); } return matches; }
  emit(name) { for (const fn of this.listeners[name]||[]) fn({}); }
}
const elements=new Map(), cards=new Element();
const element=id=> { if (!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
element('hit-sort').value='similarity';
const context=vm.createContext({document:{getElementById:element,createElement:tag=>new Element(tag),querySelectorAll:selector=>cards.querySelectorAll(selector),addEventListener(){},documentElement:{scrollHeight:800,scrollTop:0}},window:{innerHeight:800,addEventListener(){}},fetch:()=>new Promise(()=>{}),AbortController,setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},console});
vm.runInContext(fs.readFileSync('frameseek/static/app.js','utf8'),context);
const first={id:'one',source:'sda',relpath:'dir/movie.bif',version:'v',score:.9,time_ms:10000,frame_no:1,preview_url:'/api/frames/one',duration_ms:100000};
const second={...first,id:'two',score:.8,time_ms:30000,frame_no:3,preview_url:'/api/frames/two'};
assert.equal(context.prepareResults({results:[first,second],collapsed:false}).length,2);
assert.equal(context.prepareResults({results:[first,second],collapsed:true}).length,1);
assert.equal(context.prepareResults({results:[first,second],collapse:false}).length,2);
const article=context.card({...first,group:[first,second],group_count:2}); cards.append(article);
assert.equal(article.querySelector('.card-row').querySelector('.frame-index').textContent,'第 2 帧');
assert.equal(article.querySelector('.card-bottom').querySelector('.frame-index'),undefined);
const play=article.querySelector('.emby-play-button'); assert.equal(play.hidden,true);
assert.ok(play.className.includes('btn-primary'));
const nearby=article.querySelector('.result-nearby'); assert.equal(nearby.textContent,'附近画面'); assert.ok(nearby.className.includes('btn-outline'));
assert.equal(article.querySelector('.result-actions').children.length,2);
const image=article.querySelector('img'), unavailable=article.querySelector('.preview-unavailable');
image.emit('error'); assert.equal(image.hidden,true); assert.equal(unavailable.hidden,false);
article.querySelectorAll('.hit-position')[1].emit('click'); assert.equal(image.src,second.preview_url); assert.equal(unavailable.hidden,true);
assert.equal(article.querySelector('.card-row').querySelector('.frame-index').textContent,'第 4 帧');
image.emit('error'); assert.equal(unavailable.hidden,false);
image.emit('load'); assert.equal(unavailable.hidden,true); assert.equal(image.hidden,false);
vm.runInContext('embyEnabled=true;embySaved={playback_mode:"emby_web"};updateEmbyButtons()',context);
assert.equal(play.hidden,false); assert.equal(play.textContent,'Emby 网页播放');
vm.runInContext('embyEnabled=false;updateEmbyButtons()',context); assert.equal(play.hidden,true);
context.renderTaskProgress({completed_frames:100,remaining_frames:200,frames_per_second:2,eta_seconds:100,state:'stable'});
assert.equal(element('processed-frames').textContent,'100'); assert.equal(element('processing-speed').textContent,'2.00 帧/秒'); assert.equal(element('processing-eta').textContent,'约 2 分钟');
context.renderTaskProgress({completed_frames:100,remaining_frames:200,frames_per_second:null,eta_seconds:null,state:'paused'});
assert.equal(element('processing-eta').textContent,'已暂停');
context.displayResults({results:[first,second],collapsed:false,elapsed_ms:1000});
assert.equal(element('results').children.length,2);
assert.equal(element('result-count').textContent,'1 个 BIF · 2 个命中位置');
const still={...first,id:'photo',relpath:'dir/photo.png',version:'photo-version',media_type:'image',duration_ms:null};
const photo=context.card(still); cards.append(photo);
assert.equal(photo.querySelector('.time-badge').textContent,'图片');
assert.equal(photo.querySelector('.emby-play-button'),undefined);
assert.equal(photo.querySelector('.bif-duration'),undefined);
assert.equal(photo.querySelector('.hit-position'),undefined);
assert.equal(photo.querySelector('.frame-index'),undefined);
vm.runInContext('embyEnabled=true;updateEmbyButtons()',context);
assert.equal(photo.querySelector('.emby-play-button'),undefined);
context.displayResults({results:[first,second,still],collapsed:true,elapsed_ms:1000});
assert.equal(element('results').children.length,2);
assert.equal(element('result-count').textContent,'1 个 BIF · 1 张图片 · 3 个命中位置');
element('search-media-type').value='image'; context.updateMediaTypeControls();
assert.equal(element('hit-sort').disabled,true); assert.equal(element('collapse').disabled,true);
element('search-media-type').value='all'; context.updateMediaTypeControls();
assert.equal(element('hit-sort').disabled,false); assert.equal(element('collapse').disabled,false);
context.displayResults({results:[first,second],collapsed:true,elapsed_ms:1000});
assert.equal(element('results').children.length,1);
assert.equal(element('result-count').textContent,'1 个 BIF · 2 个命中位置');
console.log('Mixed image/BIF results, type controls, grouping, previews, Emby buttons and progress checks passed.');
