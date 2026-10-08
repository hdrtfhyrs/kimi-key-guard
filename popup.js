const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=time=>Number.isFinite(time)?new Date(time).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'-';
const pct=value=>Number.isFinite(value)?Math.round(value*100)/100+'%':'未核';
const localInput=time=>Number.isFinite(time)?new Date(time+8*3600000).toISOString().slice(0,16):'';
let cache={};let selection=null;let renderCount=0;let localError='';const manualClicks=new Set();const shownManualResults=new Set();
$('version').textContent=chrome.runtime.getManifest().version;
const manualAvailable=chrome.runtime.getManifest().version.split('.').map(Number).reduce((n,value)=>n*1000+value,0)>=2000002;
async function send(message){localError='';await chrome.storage.local.set({uiError:null});try{const response=await chrome.runtime.sendMessage(message);if(!response?.ok)throw new Error(response?.error||'后台未受理');}catch(error){localError=error.message;}await render();}
function fields(id){const track=cache.tracks?.[id];if(!track)return;selection=id;
  $('sentAt').value=localInput(track.sentAt);$('baseline').value=Number.isFinite(track.startBaseline)?track.startBaseline:'';
  $('carry').value=Number.isFinite(track.carry)?track.carry:'0';$('crossedWindow').checked=false;$('exclusive').checked=true;}
function card(track){
  const job=cache['job:'+track.id];const phase=track.done?'已结束':track.phase==='awaiting_delivery'?'已自动登记，持续观察':track.phase==='awaiting_evidence'?'待取证撤销':track.phase==='revoking'?'撤销结果待确认':track.phase==='paused_due'?'到期，自动删除已暂停':'独立守卫中';
  const activeJob=job?.state==='running'?'正在 '+esc(job.stage)+' · '+Math.floor((Date.now()-job.startedAt)/1000)+'秒':job?.state==='error'?'本次浏览器动作停止：'+esc(job.error):'';
  const manual=track.manualRevoke;const busy=manualClicks.has(track.id)||(manual?.status==='pending'&&manual.expiresAt>Date.now());
  const manualStatus=manual?.status==='completed'?'手动撤销完成：完整列表已确认目标不存在':manual?.status==='failed'?'手动撤销未完成：'+esc(manual.error):busy?'手动请求已受理，先刷新真实页并存D，再删除':manual?.status==='expired'?'手动请求已过期，可以重新确认':manual?.status==='cancelled'?'手动撤销请求已取消':'';
  const buttons=track.done?'':`<br><button class="danger" data-revoke="${esc(track.id)}" ${busy||!manualAvailable?'disabled':''}>${!manualAvailable?'重新加载2.0.2启用此按钮':busy?'撤销处理中…':'立即撤销此key（先截图）'}</button><button data-control="${esc(track.id)}" data-pause="${track.paused?'false':'true'}">${track.paused?'恢复此key':'暂停此key删除'}</button>`;
  return `<div class="card"><b>${esc(track.name)}</b> · ${phase}<br>API ID：${esc(track.id)}<br><b>进程 PID ${track.pid||'待监督启动'}</b> · 心跳 ${fmt(track.heartbeat)}<br>自动登记 ${fmt(track.firstSeen)}<br>实际发出 ${fmt(track.sentAt)} · 到期 ${fmt(track.deadline)}<br>最近累计 ${pct(track.consumed)}；结转 ${pct(track.carry)}；账号当前窗口 ${pct(track.lastUsed)}<br><span class="muted">${esc(track.quotaStatus||'用量待读取')}<br>最近观察 ${fmt(track.lastCheck)}；下次 ${fmt(track.nextCheck)}</span><br>${activeJob}<br><span class="error">${esc(track.lastError||'')}</span>${buttons}<div>${manualStatus}</div>${esc(track.doneReason||'')}<br>删除前证据：${esc(track.resultEvidence?.screenshotPath||track.beforeEvidence?.screenshotPath||track.beforeEvidence?.evidenceId||'尚未触发')}${track.resultEvidence?.resultPath?'<br>删除结果记录：'+esc(track.resultEvidence.resultPath):''}</div>`;
}
async function render(){
  const ticket=++renderCount;const state=await chrome.storage.local.get(null);if(ticket!==renderCount)return;cache=state;
  const all=Object.values(state.tracks||{});const live=all.filter(track=>!track.done);const paused=state.dryRun!==false;
  $('dry').checked=paused;$('modeText').textContent=paused?'自动删除已暂停；自动登记和各进程观察继续':'独立进程守卫已开启';$('modeBox').className='mode'+(paused?'':' live');$('enable').disabled=!paused;
  $('job').textContent='监督 PID '+(state.supervisor?.pid||'尚未启动')+' · 心跳 '+fmt(state.supervisor?.heartbeat)+'\nChrome桥 '+(state.bridgeConnected?'已连接':'等待连接')+' · 最近扫描 '+fmt(state.lastScanAt);
  $('active').innerHTML=live.map(card).join('')||'<div class="card">持续扫描中；发现新的5小时ID自动登记并启动进程，无需另点登记。</div>';
  $('history').innerHTML=all.filter(track=>track.done).sort((a,b)=>(b.endedAt||0)-(a.endedAt||0)).slice(0,10).map(card).join('')||'暂无历史单';
  for(const track of all){if(track.manualRevoke?.status==='completed'&&!shownManualResults.has(track.id)){$('historyDetails').open=true;shownManualResults.add(track.id);}}
  document.querySelectorAll('button[data-control]').forEach(button=>{button.onclick=()=>send({type:'setDryRun',id:button.dataset.control,value:button.dataset.pause==='true'});});
  document.querySelectorAll('button[data-revoke]').forEach(button=>{button.onclick=()=>{
    const id=button.dataset.revoke;const track=cache.tracks?.[id];if(!track||track.done||manualClicks.has(id))return;
    if(!confirm(`将真实删除这枚key，删除后它会立即失效。\n名称：${track.name}\nAPI ID：${id}\n\n先刷新真实页面并截图存D成功，再删除；截图失败不删。\n这次是本人手动撤销，不修改实际发出时间或伪造用量。\n\n确认现在撤销这一枚？`))return;
    manualClicks.add(id);void render();
    void send({type:'manualRevoke',id,confirmed:true,confirmationId:crypto.randomUUID()}).finally(()=>{setTimeout(()=>{manualClicks.delete(id);void render();},3000);});
  };});
  $('candidateBox').classList.toggle('hide',!live.length);
  const keep=live.some(item=>item.id===$('candidate').value)?$('candidate').value:live[0]?.id;
  $('candidate').innerHTML=live.map(track=>`<option value="${esc(track.id)}">${fmt(track.createTime)} · ${esc(track.id)}</option>`).join('');
  if(keep){$('candidate').value=keep;if(selection!==keep)fields(keep);const track=state.tracks[keep];$('observed').textContent='本ID已自动登记；创建 '+fmt(track.createTime)+'，首次发现 '+fmt(track.firstSeen)+'。补充交付时间不会等待其他key。';}
  $('evidenceStatus').textContent=state.evidenceConnection?'D盘存档连接 '+fmt(Date.parse(state.evidenceConnection.connectedAt))+(state.lastCurrentEvidence?'；最近真实截图 '+state.lastCurrentEvidence.screenshotPath:''):'真实页面存档等待连接';
  $('error').textContent=localError||state.uiError||state.bridgeError||state.scanError||state.evidenceError||'';
  const scan=state['job:discovery'];$('log').textContent=scan?.state==='error'?'最近扫描错误：'+scan.error:'每枚key独立处理、错误与重试；一枚失败不停止发现其他ID。';
}
$('candidate').onchange=()=>fields($('candidate').value);
$('dry').onchange=()=>send({type:'setDryRun',value:$('dry').checked});$('enable').onclick=()=>send({type:'setDryRun',value:false});
$('scan').onclick=()=>send('scan');$('check').onclick=()=>send('checkAll');$('cancel').onclick=()=>send('cancel');$('evidence').onclick=()=>send('saveCurrentEvidence');
$('confirmLease').onclick=()=>{
  const id=$('candidate').value,sentAt=$('sentAt').value,baseline=Number($('baseline').value),carry=Number($('carry').value||0);
  if(!id||!sentAt||!$('baseline').value.trim()){localError='填写这枚key的实际发出时间与当时起点；自动登记和观察已经在运行';void render();return;}
  void send({type:'confirmLease',id,sentAt:(sentAt.length===16?sentAt+':00':sentAt)+'+08:00',baseline,carry,crossedWindow:$('crossedWindow').checked||carry>0,exclusiveUsage:$('exclusive').checked});
};
chrome.storage.onChanged.addListener(()=>void render());setInterval(()=>void render(),1000);void render();void send({type:'bindConsole'});
