import {run,cancelAll,isRunning} from './jobs.js';
import {HOST,read,patch,shortNative,bindConsole,keys,usage,observe,revoke,currentScreenshot} from './browser-operations.js';

let port=null;let reconnect=null;let retryDelay=1000;let booting=null;let pumping=false;let scanning=false;let sequence=0;
const awaiting=new Map();const activeCommands=new Map();
async function limit(factory,label='Chrome状态',ms=3000){let timer;try{return await Promise.race([Promise.resolve().then(factory),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(label+'超时')),ms);})]);}finally{clearTimeout(timer);}}
const set=value=>limit(()=>chrome.storage.local.set(value));
const get=fields=>limit(()=>chrome.storage.local.get(fields));
function connectPort(){
  if(port)return port;
  const live=chrome.runtime.connectNative(HOST);port=live;
  live.onMessage.addListener(message=>{const pending=awaiting.get(message.requestId);if(!pending)return;clearTimeout(pending.timer);awaiting.delete(message.requestId);message.ok?pending.resolve(message):pending.reject(new Error(message.error||'本机进程响应失败'));});
  live.onDisconnect.addListener(()=>{
    const error=chrome.runtime.lastError?.message||'本机桥连接断开';if(port!==live)return;port=null;
    for(const pending of awaiting.values()){clearTimeout(pending.timer);pending.reject(new Error(error));}awaiting.clear();
    cancelAll('浏览器桥断开；本机各key进程继续记录和计时');
    void set({bridgeError:error,bridgeConnected:false}).catch(()=>{});
    clearTimeout(reconnect);reconnect=setTimeout(()=>{void boot().catch(()=>{});},retryDelay);retryDelay=Math.min(10000,retryDelay*2);
  });return live;
}
function native(payload){
  return new Promise((resolve,reject)=>{
    const requestId=Date.now()+':'+(++sequence);
    try{const live=connectPort();const timer=setTimeout(()=>{awaiting.delete(requestId);reject(new Error('本机桥8秒内未响应；各守卫进程独立重试'));if(port===live){port=null;try{live.disconnect();}catch{}}clearTimeout(reconnect);reconnect=setTimeout(()=>void boot().catch(()=>{}),retryDelay);},8000);
      awaiting.set(requestId,{resolve,reject,timer});live.postMessage({...payload,requestId});
    }catch(error){awaiting.delete(requestId);reject(error);}
  });
}
async function mirror(status){
  await set({tracks:status.tracks||{},workers:status.workers||{},supervisor:status.supervisor||null,
    dryRun:status.paused!==false,processJobs:status.jobs||[],processVersion:status.runtimeVersion,
    activeId:null,schema:4,bridgeError:null,bridgeConnected:true,lastBridgeAt:Date.now()});
}
async function boot(){
  if(booting)return booting;
  booting=(async()=>{
    const previous=await get(['schema','tracks','migrationTracks','dryRun']);
    if(previous.schema!==4&&!previous.migrationTracks)await set({migrationTracks:previous.tracks||{}});
    const receipt=await native({action:'status',extensionVersion:chrome.runtime.getManifest().version});
    await set({evidenceConnection:receipt,evidenceError:null});
    if(previous.schema!==4&&previous.dryRun===false)await native({action:'process_control',paused:false});
    const status=await native({action:'process_status'});await mirror(status);retryDelay=1000;
    const alarms=await limit(()=>chrome.alarms.getAll(),'读取闹钟');
    for(const alarm of alarms){if(alarm.name==='scan'||alarm.name.startsWith('check:')||alarm.name.startsWith('deadline:'))await limit(()=>chrome.alarms.clear(alarm.name),'清旧闹钟');}
    if(!await limit(()=>chrome.alarms.get('process-discover'),'发现闹钟'))await limit(()=>chrome.alarms.create('process-discover',{periodInMinutes:0.5}),'恢复发现');
    if(!await limit(()=>chrome.alarms.get('process-bridge'),'桥闹钟'))await limit(()=>chrome.alarms.create('process-bridge',{periodInMinutes:0.5}),'恢复桥');
  })().catch(async error=>{await set({bridgeError:error.message,bridgeConnected:false});throw error;}).finally(()=>{booting=null;});
  return booting;
}
async function discover(){
  if(scanning)return;scanning=true;
  try{
    await run('discovery',async ctx=>{
      await ctx.stage('持续发现新ID');const complete=await keys(ctx);const observedAt=Date.now();const saved=await read(ctx);
      // Persist and spawn immediately. Quota fetch cannot block registering a new ID.
      const first=await shortNative(ctx,{action:'process_sync',keys:complete,completeList:true,usage:null,knownTracks:{...(saved.migrationTracks||{}),...(saved.tracks||{})},observedAt});
      await mirror(first);await patch(ctx,{lastScanAt:Date.now(),scanError:null});
    },{id:'discovery',expiresAt:Date.now()+55000});
  }catch(error){await set({scanError:error.message});}finally{scanning=false;}
}
async function deliver(message){
  return run('delivery:'+message.id,async ctx=>{
    await bindConsole(ctx);const all=await keys(ctx);const key=all.find(item=>item.id===message.id&&item.name==='5小时');
    if(!key)throw new Error('本ID不在有效完整列表，保留原记录');
    const sentAt=Date.parse(message.sentAt),created=Date.parse(key.createTime),baseline=Number(message.baseline),carry=Number(message.carry||0);
    if(!Number.isFinite(sentAt)||sentAt<created-60000||sentAt>Date.now()+60000)throw new Error('请填本ID实际发出时间，不能早于创建或在未来');
    if(!Number.isFinite(baseline)||baseline<0||baseline>100||!Number.isFinite(carry)||carry<0)throw new Error('起点须0到100%，结转须非负数');
    const expired=Date.now()>=sentAt+18000000;const fresh=expired?null:await usage(ctx);const crossed=message.crossedWindow===true||carry>0;
    if(fresh&&sentAt<fresh.resetTime-18000000-60000&&!crossed)throw new Error('本笔已跨额度窗口，请补实际结转；原起点不再扣当前窗口');
    if(fresh&&!crossed&&baseline>fresh.used+0.5)throw new Error('起点高于当前读数，需核跨窗口记录');
    const track={id:key.id,name:key.name,createTime:created,sentAt,deadline:sentAt+18000000,startBaseline:baseline,baseline:expired?null:crossed?0:baseline,
      carry:expired?null:carry,lastUsed:fresh?.used??null,lastReset:fresh?.resetTime??null,consumed:expired?null:carry+Math.max(0,fresh.used-(crossed?0:baseline)),
      lastUsageAt:expired?null:Date.now(),exclusiveUsage:message.exclusiveUsage===true,startSource:'本人补充实际交付记录',phase:'active',done:false};
    const status=await shortNative(ctx,{action:'process_delivery',track});await mirror(status);return status;
  },{id:message.id,expiresAt:Date.now()+55000});
}
async function recordResult(result){await set({['result:'+result.jobId]:result});}
async function manualRevoke(message){
  return run('manual-request:'+message.id,async ctx=>{
    await bindConsole(ctx);const complete=await keys(ctx);const observedAt=Date.now();
    const key=complete.find(row=>row.id===message.id&&row.name==='5小时'&&!['STATUS_DISABLED','STATUS_REVOKED','STATUS_DELETED'].includes(row.status));
    if(!key)throw new Error('这枚key已不在有效列表，没有发删除');
    await shortNative(ctx,{action:'process_sync',keys:complete,completeList:true,observedAt});
    const status=await shortNative(ctx,{action:'process_manual_revoke',id:message.id,confirmed:message.confirmed===true,confirmationId:message.confirmationId});
    cancelAll('本人明确发起此ID手动撤销，旧观察任务停止',message.id);
    await mirror(status);return status;
  },{id:'manual-request:'+message.id,expiresAt:Date.now()+30000});
}
function dispatch(command){
  if(activeCommands.has(command.jobId)||activeCommands.size>=4||!['observe','revoke'].includes(command.kind))return;
  if([...activeCommands.values()].some(item=>item.id===command.id))return;
  activeCommands.set(command.jobId,command);
  void run('command:'+command.jobId,ctx=>command.kind==='observe'?observe(ctx,command):revoke(ctx,command),
    {id:command.id,epoch:command.epoch,expiresAt:Math.min(command.expiresAt-1000,Date.now()+85000)})
    .then(result=>recordResult({jobId:command.jobId,ok:true,result}))
    .catch(error=>recordResult({jobId:command.jobId,ok:false,error:error.message}))
    .catch(error=>set({bridgeError:'本ID结果保存失败：'+error.message}).catch(()=>{}))
    .finally(()=>activeCommands.delete(command.jobId));
}
async function pump(){
  if(pumping)return;pumping=true;
  try{
    if(!port)await boot();const saved=await get(null);
    const resultFields=Object.keys(saved).filter(key=>key.startsWith('result:')).slice(0,40);const results=resultFields.map(key=>saved[key]);
    const response=await native({action:'process_exchange',results});await mirror(response);
    if(resultFields.length)await limit(()=>chrome.storage.local.remove(resultFields),'提交回执');
    for(const command of response.commands||[]){if(command.expiresAt>Date.now())dispatch(command);}
  }catch(error){await set({bridgeError:error.message,bridgeConnected:false});}finally{pumping=false;}
}
async function control(message){
  const paused=message.value!==false;cancelAll(paused?'本人暂停本ID后续删除':'重新取得本ID授权',message.id||null);
  if(paused){
    if(!message.id)void set({dryRun:true}).catch(()=>{});
    const status=await native({action:'process_control',paused:true,id:message.id||null});await mirror(status).catch(()=>{});return status;
  }
  return run('control:'+(message.id||'global'),async ctx=>{
    if(!paused)await bindConsole(ctx);
    const status=await shortNative(ctx,{action:'process_control',paused,id:message.id||null});await mirror(status);return status;
  },{id:'control:'+(message.id||'global'),expiresAt:Date.now()+15000});
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id||!sender.url?.startsWith(chrome.runtime.getURL('popup.html'))){reply({ok:false,error:'只接受守卫弹窗动作'});return false;}
  let task;
  if(message==='scan')task=discover();
  else if(message==='checkAll')task=pump();
  else if(message==='cancel')task=control({value:true});
  else if(message==='saveCurrentEvidence')task=run('current-screenshot',ctx=>currentScreenshot(ctx,null),{id:'current-screenshot',expiresAt:Date.now()+75000});
  else if(message?.type==='bindConsole')task=run('bind-console',bindConsole,{id:'bind-console',expiresAt:Date.now()+10000});
  else if(message?.type==='setDryRun')task=control(message);
  else if(message?.type==='confirmLease')task=deliver(message);
  else if(message?.type==='manualRevoke')task=manualRevoke(message);
  else {reply({ok:false,error:'未知动作'});return false;}
  reply({ok:true,accepted:true});
  void task.catch(error=>set({uiError:error.message}).catch(()=>{}));return false;
});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='process-discover')void discover();else if(alarm.name==='process-bridge')void pump();});
chrome.runtime.onInstalled.addListener(()=>void boot().then(discover).catch(()=>{}));
chrome.runtime.onStartup.addListener(()=>void boot().then(discover).catch(()=>{}));
setInterval(()=>void pump(),2000);setInterval(()=>void discover(),30000);
void boot().then(discover).catch(()=>{});
