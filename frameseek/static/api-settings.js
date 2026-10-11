let apiSettingsBusy = false, apiCacheBusy = false;
function renderApiSettings(data) {
  $("api-service-enabled").checked=data.enabled;
  $("api-service-enabled").disabled=!data.token_set;
  $("api-service-token").placeholder=data.token_set ? '已有 Token，填写新值可替换' : '输入至少 32 位 Token，或点击生成';
  $("api-service-state").textContent=data.enabled ? '已开启' : '未开启';
  $("api-service-state").className=data.enabled ? 'badge badge-success badge-outline' : 'badge badge-ghost';
  $("api-token-clear").disabled=!data.token_set;
  $("api-cache-status").textContent=`已提交 ${data.jobs} 个任务 · 等待或处理中 ${data.queued} 个`;
  $("api-service-url").value=window.location.origin+'/api/v1/embeddings/upload';
  $("api-service-example").textContent=`curl -X POST '${$("api-service-url").value}' \\\n  -H 'Authorization: Bearer YOUR_TOKEN' \\\n  -F 'file=@example.bif'`;
}
async function loadApiSettings() {
  $("api-token-once").hidden=true; $("api-token-issued").value='';
  try { renderApiSettings(await api('/api/embedding-service')); }
  catch(error) { $("api-service-message").textContent=error.message; }
}
async function saveApiSettings(values) {
  if(apiSettingsBusy) return;
  apiSettingsBusy=true;
  for(const id of ['api-service-enabled','api-service-token','api-token-generate','api-token-clear']) $(id).disabled=true;
  try {
    const data=await api('/api/embedding-service',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});
    renderApiSettings(data); $("api-service-token").value='';
    if(values.generate || values.token || values.clear) {
      $("api-token-issued").value=data.token || ''; $("api-token-once").hidden=!data.token;
    }
    $("api-service-message").textContent='已更新';
  } catch(error) { $("api-service-message").textContent=error.message; await loadApiSettings(); }
  finally { apiSettingsBusy=false; for(const id of ['api-service-token','api-token-generate']) $(id).disabled=false; }
}
$("settings-tab").addEventListener('click',loadApiSettings);
$("reload-settings").addEventListener('click',loadApiSettings);
$("api-service-form").addEventListener('submit',event=>event.preventDefault());
$("api-service-enabled").addEventListener('change',()=>saveApiSettings({enabled:$("api-service-enabled").checked}));
$("api-service-token").addEventListener('change',()=>{const token=$("api-service-token").value.trim(); if(token)saveApiSettings({token});});
$("api-token-generate").addEventListener('click',()=>saveApiSettings({generate:true}));
$("api-token-clear").addEventListener('click',()=>saveApiSettings({clear:true}));
$("api-token-copy").addEventListener('click',async()=>{
  try { await navigator.clipboard.writeText($("api-token-issued").value); $("api-service-message").textContent='Token 已复制'; }
  catch(_) { $("api-service-message").textContent='请选择 Token 文字复制'; }
});
$("api-cache-clear").addEventListener('click',()=>{ $("api-cache-error").textContent=''; $("api-cache-dialog").showModal(); });
$("api-cache-cancel").addEventListener('click',()=>{if(!apiCacheBusy)$("api-cache-dialog").close();});
$("api-cache-dialog").addEventListener('cancel',event=>{if(apiCacheBusy)event.preventDefault();});
$("api-cache-confirm").addEventListener('click',async()=>{
  if(apiCacheBusy)return; apiCacheBusy=true; $("api-cache-confirm").disabled=true; $("api-cache-cancel").disabled=true;
  try { renderApiSettings(await api('/api/embedding-service/cache',{method:'DELETE',timeoutMs:180000})); $("api-cache-dialog").close(); $("api-service-message").textContent='API 缓存已清理'; }
  catch(error){$("api-cache-error").textContent=error.message;}
  finally{apiCacheBusy=false;$("api-cache-confirm").disabled=false;$("api-cache-cancel").disabled=false;}
});
