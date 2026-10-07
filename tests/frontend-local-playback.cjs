const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('frameseek/static/app.js', 'utf8');
const code = source.slice(source.indexOf('async function playInEmby('), source.indexOf('const embyAutoSave ='));
async function check(policy) {
  const urls = [], calls = [];
  const popup = {closed:false, location:{set href(value) {urls.push(value);}}, close() {this.closed=true;}};
  const context = vm.createContext({embyEnabled:true, embySaved:{playback_mode:'emby_web',web_target:policy}, embyWebRequest:0,
    window:{open() {return popup;}}, message() {}, Date, encodeURIComponent, setTimeout,
    api:async path => {calls.push(path); return {local:true,player_url:'/player/local/frame'};}});
  vm.runInContext(code, context);
  const button = {disabled:false};
  await context.playInEmby('frame',button);
  assert.deepEqual(urls,['/player/local/frame']);
  assert.deepEqual(calls,['/api/emby/web-begin/frame']);
  assert.equal(popup.closed,false);
  assert.equal(button.disabled,false);
}
(async () => {await check('new_tab'); await check('recent'); console.log('MP4 fallback opens the local player and skips Emby session polling.');})().catch(error => {console.error(error); process.exitCode=1;});
