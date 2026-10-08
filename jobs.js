// Browser adapter contexts; actual per-key OS processes live in process_runtime.py.
const running=new Map();
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
export function cancelAll(reason='本人暂停',id=null){for(const [key,ctx] of running){if(!id||ctx.keyId===id)ctx.cancel(reason);}}
export function isRunning(key){return running.has(key);}
export async function run(key,work,{id=key,epoch=null,expiresAt=Date.now()+90000}={}){
  if(running.has(key))return {duplicate:true};
  const ctx={key,keyId:id,epoch,expiresAt,startedAt:Date.now(),cancelled:false,cancelCallbacks:new Set(),sideEffects:new Set()};
  running.set(key,ctx);
  ctx.assert=()=>{if(ctx.cancelled||running.get(key)!==ctx||Date.now()>=ctx.expiresAt)throw new Error(ctx.cancelReason||'本项已取消或过期，停止后续动作');};
  ctx.cancel=reason=>{if(ctx.cancelled)return;ctx.cancelled=true;ctx.cancelReason=reason;for(const fn of [...ctx.cancelCallbacks]){try{fn();}catch{}}};
  ctx.bound=async(factory,ms,label,sideEffect=false)=>{
    ctx.assert();let timer;let abort;
    const raw=Promise.resolve().then(()=>{ctx.assert();return factory();});
    if(sideEffect){ctx.sideEffects.add(raw);raw.then(()=>ctx.sideEffects.delete(raw),()=>ctx.sideEffects.delete(raw));}
    const stop=new Promise((_,reject)=>{abort=()=>reject(new Error(ctx.cancelReason||label+'已取消'));ctx.cancelCallbacks.add(abort);timer=setTimeout(()=>ctx.cancel(label+'超过'+Math.round(ms/1000)+'秒，本项停止'),ms);});
    try{const value=await Promise.race([raw,stop]);ctx.assert();return value;}finally{clearTimeout(timer);ctx.cancelCallbacks.delete(abort);}
  };
  ctx.stage=async stage=>{
    ctx.assert();ctx.currentStage=stage;
    await ctx.bound(()=>chrome.storage.local.set({['job:'+ctx.keyId]:{state:'running',keyId:ctx.keyId,stage,startedAt:ctx.startedAt,epoch,updatedAt:Date.now()}}),3000,'保存操作步骤');
  };
  ctx.sleep=ms=>ctx.bound(()=>sleep(ms),ms+1000,'等候页面');
  const timer=setTimeout(()=>ctx.cancel('本项总时间到限，其他key继续运行'),Math.max(1,Math.min(90000,expiresAt-Date.now())));
  try{await ctx.stage('准备');const result=await work(ctx);ctx.assert();await ctx.bound(()=>chrome.storage.local.set({['job:'+ctx.keyId]:{state:'idle',stage:'完成',updatedAt:Date.now()}}),3000,'保存完成状态');return result;}
  catch(error){void chrome.storage.local.set({['job:'+ctx.keyId]:{state:'error',stage:ctx.currentStage,error:error.message,updatedAt:Date.now()}}).catch(()=>{});throw error;}
  finally{clearTimeout(timer);if(running.get(key)===ctx)running.delete(key);for(const fn of [...ctx.cancelCallbacks]){try{fn();}catch{}}ctx.cancelCallbacks.clear();}
}
