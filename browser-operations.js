import {evaluate} from './logic.js';
const ORIGIN='https://www.kimi.com';
export const HOST='com.user.kimi_key_guard_evidence';
const consoleUrl=url=>{try{const u=new URL(url);return u.origin===ORIGIN&&u.pathname==='/code/console';}catch{return false;}};
const iso=time=>Number.isFinite(time)?new Date(time).toISOString():null;
let captureOwner=null;
function releaseBrowser(owner){
  void Promise.allSettled([...owner.ctx.sideEffects]).then(()=>{
    if(captureOwner!==owner)return;captureOwner=null;
    void chrome.storage.local.get('captureFence').then(s=>{if(!captureOwner&&s.captureFence?.owner===owner.token)return chrome.storage.local.remove('captureFence');}).catch(()=>{});
  });
}
export async function read(ctx){return ctx.bound(()=>chrome.storage.local.get(null),3000,'读取本机设置');}
export async function patch(ctx,value){return ctx.bound(()=>chrome.storage.local.set(value),3000,'保存界面记录');}
export async function shortNative(ctx,payload){
  return ctx.bound(()=>new Promise((resolve,reject)=>{
    let port;let settled=false;
    const finish=(error,value)=>{if(settled)return;settled=true;ctx.cancelCallbacks.delete(cancel);try{port?.disconnect();}catch{}error?reject(error):resolve(value);};
    const cancel=()=>finish(new Error(ctx.cancelReason||'本机调用已取消'));
    try{port=chrome.runtime.connectNative(HOST);ctx.cancelCallbacks.add(cancel);port.onMessage.addListener(value=>value?.ok?finish(null,value):finish(new Error(value?.error||'本机未保存成功')));port.onDisconnect.addListener(()=>finish(new Error(chrome.runtime.lastError?.message||'本机调用断开')));port.postMessage(payload);}catch(error){finish(error);}
  }),8000,'本机进程或存档');
}
async function loaded(ctx,tabId){const end=Date.now()+12000;while(Date.now()<end){const tab=await ctx.bound(()=>chrome.tabs.get(tabId),3000,'读取页面');if(tab.status==='complete')return;await ctx.sleep(300);}throw new Error('Kimi页面12秒内未载入');}
export async function tabFor(ctx){
  const s=await read(ctx);
  if(s.evidenceTabId){try{const tab=await ctx.bound(()=>chrome.tabs.get(s.evidenceTabId),3000,'读取绑定页面');if(consoleUrl(tab.url)&&!tab.discarded)return tab;}catch(error){ctx.assert();}}
  const tabs=await ctx.bound(()=>chrome.tabs.query({url:ORIGIN+'/code/console*'}),3000,'查找Kimi页面');const tab=tabs.find(item=>consoleUrl(item.url)&&!item.discarded);
  if(!tab)throw new Error('没有已载入的Kimi控制台；其他key进程仍计时，浏览器动作等待恢复');return tab;
}
export async function bindConsole(ctx){const tabs=await ctx.bound(()=>chrome.tabs.query({active:true,currentWindow:true}),3000,'读取当前页');if(!consoleUrl(tabs[0]?.url))throw new Error('请从Kimi控制台打开守卫以授权这一页截图');await patch(ctx,{evidenceTabId:tabs[0].id});return tabs[0];}
async function api(ctx,method,body){
  const tab=await tabFor(ctx);
  const request=()=>ctx.bound(()=>chrome.scripting.executeScript({target:{tabId:tab.id},world:'MAIN',args:[method,body],func:async(method,body)=>{
    let token=localStorage.getItem('access_token')||'';if(token.startsWith('"'))token=JSON.parse(token);
    const response=await fetch('/apiv2/'+method,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','Connect-Protocol-Version':'1',Authorization:'Bearer '+token},body:JSON.stringify(body),signal:AbortSignal.timeout(6000)});
    return {status:response.status,text:await response.text()};
  }}),8000,method.split('/').pop(),method.endsWith('/DeleteAPIKey'));
  let rows=await request();let result=rows[0]?.result;
  if(result?.status===401){
    if(captureOwner&&captureOwner.ctx!==ctx)throw new Error('登录刷新正由另一项取证协调，本ID稍后重试');
    const own=!captureOwner;const owner=own?{ctx,token:crypto.randomUUID()}:captureOwner;
    if(own)captureOwner=owner;
    try{if(own)await patch(ctx,{captureFence:{owner:owner.token,until:Date.now()+90000,keyId:ctx.keyId}});await ctx.bound(()=>chrome.tabs.reload(tab.id),3000,'恢复登录页面',true);await loaded(ctx,tab.id);rows=await request();result=rows[0]?.result;}
    finally{if(own)releaseBrowser(owner);}
  }
  if(!result)throw new Error('Kimi接口没有返回结果');
  if(result.status!==200)throw new Error(method.split('/').pop()+' 返回'+result.status+'；本ID稍后重试');
  return result.text?JSON.parse(result.text):{};
}
export async function keys(ctx){
  const all=[];const seen=new Set();let pageToken='';
  for(let n=0;n<5;n++){
    const data=await api(ctx,'kimi.gateway.credentials.v1.APIKeyService/ListAPIKeys',{pageSize:100,pageToken,scope:['FEATURE_CODING']});
    if(!data||data.code||data.error||(data.apiKeys!==undefined&&!Array.isArray(data.apiKeys)))throw new Error('未取得有效完整key列表，不能判断目标消失');
    all.push(...(data.apiKeys||[]).map(key=>({id:key.id,name:key.name,createTime:key.createTime,status:key.status})));if(!data.nextPageToken)return all;
    if(seen.has(data.nextPageToken))throw new Error('Key分页循环');seen.add(data.nextPageToken);pageToken=data.nextPageToken;
  }throw new Error('Key列表分页未完成');
}
export async function usage(ctx){
  const data=await api(ctx,'kimi.gateway.billing.v1.BillingService/GetUsages',{scope:['FEATURE_CODING']});
  const coding=(data.usages||[]).find(row=>row.scope==='FEATURE_CODING');const window=(coding?.limits||[]).find(row=>Number(row.window?.duration)===300&&row.window?.timeUnit==='TIME_UNIT_MINUTE');
  const limit=Number(window?.detail?.limit),used=Number(window?.detail?.used??0),resetTime=Date.parse(window?.detail?.resetTime);
  if(!(limit>0)||!Number.isFinite(used)||!Number.isFinite(resetTime))throw new Error('五小时频限数据无效');return {used:used/limit*100,resetTime};
}
export async function observe(ctx,command){const live=await keys(ctx);const observedAt=Date.now();let fresh=null;let usageError=null;if(!(Number.isFinite(command?.track?.deadline)&&Date.now()>=command.track.deadline)){try{fresh=await usage(ctx);}catch(error){ctx.assert();usageError=error.message;}}return {keys:live,completeList:true,usage:fresh,usageError,observedAt,usageObservedAt:fresh?Date.now():null};}
function snapshot(t){return {id:t.id,name:t.name,sentAt:iso(t.sentAt),createdAt:iso(t.createTime),deadline:iso(t.deadline),baseline:t.startBaseline??t.baseline,
  windowBaseline:t.baseline,carry:t.carry,windowPercent:t.lastUsed,consumed:t.consumed,resetTime:iso(t.lastReset),lastCheck:iso(t.lastCheck),
  usageObservedAt:iso(t.lastUsageAt),usageSnapshotFresh:t.captureUsageFresh,quotaReliable:t.quotaReliable,quotaStatus:t.quotaStatus,done:Boolean(t.done),phase:t.phase,source:t.startSource};}
async function page(ctx,tabId,id){
  const rows=await ctx.bound(()=>chrome.scripting.executeScript({target:{tabId},world:'MAIN',args:[id],func:id=>{
    const text=document.body.innerText;const label=[...document.querySelectorAll('p')].find(p=>p.textContent.trim()==='频限明细');let box=label;
    for(let n=0;box&&n<6&&!/\d+(?:\.\d+)?%/.test(box.innerText);n++)box=box.parentElement;
    const visibleUsage=box?.innerText||'';const match=visibleUsage.match(/(\d+(?:\.\d+)?)%/);return {url:location.href,title:document.title,text,visibleUsage,visiblePercent:match?Number(match[1]):null,targetPresent:Boolean(id&&text.includes(id)),ready:Boolean(match)};
  }}),4000,'读取真实页面');return rows[0]?.result;
}
async function capture(ctx,input,purpose,command=null){
  if(captureOwner)throw new Error('另一枚key正在截取真实页面，本ID稍后重试；发现和计时继续');
  const state=await read(ctx);if(!state.evidenceTabId)throw new Error('真实截图页尚未授权；请从Kimi页打开扩展，先留证成功才会删除');
  if(state.captureFence?.until>Date.now())throw new Error('上次取证未结束，本ID稍后重试');
  const owner={ctx,token:crypto.randomUUID()};captureOwner=owner;let previous;let tab;
  try{
    await patch(ctx,{captureFence:{owner:owner.token,until:Date.now()+90000,keyId:input.id}});
    tab=await ctx.bound(()=>chrome.tabs.get(state.evidenceTabId),3000,'读取取证页');if(!consoleUrl(tab.url))throw new Error('取证页已离开Kimi控制台');
    previous=(await ctx.bound(()=>chrome.tabs.query({active:true,windowId:tab.windowId}),3000,'读取原页'))[0];
    await ctx.stage('刷新本ID真实证据页');await ctx.bound(()=>chrome.tabs.update(tab.id,{active:true}),3000,'切换取证页',true);
    await ctx.bound(()=>chrome.tabs.reload(tab.id),3000,'刷新页面',true);const refreshedAt=new Date().toISOString();await loaded(ctx,tab.id);
    let observed;const end=Date.now()+10000;
    while(Date.now()<end){observed=await page(ctx,tab.id,input.id);if(observed?.ready&&(purpose==='current'||observed.targetPresent))break;await ctx.sleep(300);}
    if(!observed?.ready||(purpose!=='current'&&!observed.targetPresent))throw new Error('刷新后没有目标ID或完整额度，不能留证删除');
    const track={...input};const manual=Boolean(command?.manualRequestId);
    let reason=manual?'本人确认立即撤销，验证真实截图与删除链':purpose==='current'?'本人保存当前真实页面，未执行删除':'已满5小时';
    if(manual){track.captureUsageFresh=false;}
    else if(purpose==='before'&&Date.now()<track.deadline){
      if(!track.quotaReliable)throw new Error('本笔额度归属未知，时间未到，停止删除');
      const fresh=await usage(ctx);const judged=evaluate(track,fresh,Date.now());Object.assign(track,judged.track);
      if(!judged.del)throw new Error('刷新后停止条件未满足');reason=judged.reason;track.lastUsageAt=Date.now();track.captureUsageFresh=true;
    }else if(purpose==='before'){track.captureUsageFresh=false;}
    await ctx.bound(()=>chrome.scripting.executeScript({target:{tabId:tab.id},world:'MAIN',func:()=>window.scrollTo(0,0)}),3000,'回到页面顶部',true);
    const selected=(await ctx.bound(()=>chrome.tabs.query({active:true,windowId:tab.windowId}),3000,'核截图目标'))[0];
    if(selected?.id!==tab.id||!consoleUrl(selected.url))throw new Error('取证期间活动页被切换');
    await ctx.stage('截图并写D盘');const pixels=await ctx.bound(()=>chrome.tabs.captureVisibleTab(tab.windowId,{format:'png'}),5000,'截图',true);
    const after=(await ctx.bound(()=>chrome.tabs.query({active:true,windowId:tab.windowId}),3000,'核实际截取页'))[0];
    const afterPage=await page(ctx,tab.id,track.id);
    if(after?.id!==tab.id||!consoleUrl(after.url)||!afterPage?.ready||(purpose!=='current'&&!afterPage.targetPresent)||afterPage.visiblePercent!==observed.visiblePercent)throw new Error('截图期间页面或额度发生变化，本ID重新取证');
    const receipt=await shortNative(ctx,{action:purpose==='before'?'save_before':'save_current',targetId:track.id||null,purpose:purpose==='before'?'before_deletion':'current_page_only',
      screenshot:pixels,capturedAt:new Date().toISOString(),reason,page:{...observed,refreshedAt},snapshot:snapshot(track),
      manualRequestId:command?.manualRequestId,jobId:command?.jobId,epoch:command?.epoch});
    if(!receipt.evidenceId||!receipt.screenshotPath||!receipt.sha256)throw new Error('D盘截图回执不完整');return {track,receipt,reason};
  }finally{
    if(!ctx.cancelled&&tab&&previous&&previous.id!==tab.id){try{const current=(await ctx.bound(()=>chrome.tabs.query({active:true,windowId:tab.windowId}),3000,'读取截图后页面'))[0];if(current?.id===tab.id)await ctx.bound(()=>chrome.tabs.update(previous.id,{active:true}),3000,'恢复原标签页',true);}catch{}}
    // A raced timeout does not release a shared browser resource while its raw action is outstanding.
    releaseBrowser(owner);
  }
}
async function deleteAllowed(ctx,command){ctx.assert();const s=await read(ctx);if(!command?.manualRequestId&&s.dryRun!==false)throw new Error('自动删除已暂停');}
export async function revoke(ctx,command){
  await deleteAllowed(ctx,command);const initial=await keys(ctx);
  await shortNative(ctx,{action:'process_sync',keys:initial,completeList:true,usage:null,observedAt:Date.now()});
  if(!initial.some(key=>key.id===command.id))return {confirmedAbsent:true,responseConfirmed:false,evidence:command.track.beforeEvidence||null,reason:'完整列表已确认目标不在，未发新的删除',confirmedAbsentAt:Date.now(),deletedAt:null};
  const result=await capture(ctx,command.track,'before',command);
  await ctx.stage('核本ID进程和删除授权');await deleteAllowed(ctx,command);
  const permission=await shortNative(ctx,{action:'process_authorize',jobId:command.jobId,id:command.id,epoch:command.epoch,evidenceId:result.receipt.evidenceId,sha256:result.receipt.sha256,snapshot:snapshot(result.track),manualRequestId:command.manualRequestId});
  if(permission.permit!==true)throw new Error('本ID未取得当前删除许可');
  await deleteAllowed(ctx,command);await ctx.stage('撤销本ID');
  await api(ctx,'kimi.gateway.credentials.v1.APIKeyService/DeleteAPIKey',{id:command.id});
  const deleteResponseAt=Date.now();
  await ctx.stage('确认完整列表');const after=await keys(ctx);if(after.some(key=>key.id===command.id))throw new Error('删除返回后目标仍在，本ID保留意图继续确认');
  const at=Date.now();await shortNative(ctx,{action:'save_result',targetId:command.id,evidenceId:result.receipt.evidenceId,
    deletedAt:iso(deleteResponseAt),confirmedAbsentAt:iso(at),reason:result.reason,confirmedAbsent:true,deleteResponseConfirmed:true,state:{...snapshot(result.track),done:true,phase:'ended'}});
  return {confirmedAbsent:true,responseConfirmed:true,evidence:result.receipt,reason:result.reason,confirmedAbsentAt:at,deletedAt:deleteResponseAt};
}
export async function currentScreenshot(ctx,id){await bindConsole(ctx);const s=await read(ctx);const track=s.tracks?.[id]||{id:id||null,name:'5小时',sentAt:null,deadline:null};const result=await capture(ctx,track,'current');await patch(ctx,{lastCurrentEvidence:result.receipt});return result.receipt;}
