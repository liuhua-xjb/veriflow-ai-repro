import {useEffect,useState} from 'react';
// Approval credentials must never follow a configurable API base or redirect.
const base='http://127.0.0.1:18000';
type Status={status:string;authorization_valid:boolean;key_configured:boolean|null;budget?:{requests_remaining:number;tokens_remaining:number;usd_remaining:number};active_run_ids?:string[]};
export default function DeepSeekTrial(){
 const [state,setState]=useState<Status|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[result,setResult]=useState<unknown>(null),[approval,setApproval]=useState('');
 const approved=/^[0-9a-f]{64}$/.test(approval);
 async function refresh(){try{const r=await fetch(base+'/api/deepseek/trial/status',{cache:'no-store',signal:AbortSignal.timeout(5000)});if(!r.ok)throw new Error(`HTTP ${r.status}`);setState(await r.json());}catch(e){setError(String(e));}}
 useEffect(()=>{void refresh();const id=window.setInterval(()=>void refresh(),3000);return()=>window.clearInterval(id);},[]);
 async function submit(path:string,body?:object){
  if(!approved){setError('请在本机终端取得独立临时审批令牌后主动输入。');return;}
  const response=await fetch(base+path,{method:'POST',headers:{'Content-Type':'application/json','X-Veriflow-Trial-Approval':approval},
   body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(185000),redirect:'error',credentials:'omit',referrerPolicy:'no-referrer'});
  const data=await response.json();
  if(!response.ok){if(response.status===401||response.status===403)setApproval('');throw new Error(`请求被拒绝：HTTP ${response.status}，请核查本机授权状态。`);}
  return data;
 }
 async function start(){if(!approved||busy)return;setBusy(true);setError('');try{setResult(await submit('/api/deepseek/trial/start',{intent:'比较三种模型'}));}catch{setError('试验未成功，请核查后端状态和预算；不会自动重试。');}finally{setBusy(false);void refresh();}}
 async function cancel(id:string){if(!approved)return;try{await submit(`/api/deepseek/trial/${id}/cancel`);void refresh();}catch{setError('取消请求被拒绝或失败，请核查本机审批状态。');}}
 return <section className="panel"><h3>DeepSeek 首次真实限额试验</h3><p>0.20 USD / 最多6请求 / 总预留12000 tokens；截止北京时间2026-11-01 15:51:03。失败和未知计费不退还预留，不自动重试。</p>
 <p>服务端状态：{state?.status||'读取中'} · Key：{state?.key_configured===true?'已在后端进程配置':state?.key_configured===false?'未配置':'尚未启动授权后端'}</p>
 {state?.budget&&<p>剩余：{state.budget.requests_remaining}个预留名额 / {state.budget.tokens_remaining} tokens / ${state.budget.usd_remaining.toFixed(6)}</p>}
 <p>安全整改待独立复核：现在不要输入真实Key。复核通过后的流程为终端隐藏输入DeepSeek Key，再将独立临时审批令牌输入下方。两种凭据均不得发到聊天。令牌30分钟或原授权到期失效，刷新/关闭页面需重新手动输入。</p>
 <label>本机会话审批令牌（不是DeepSeek API Key）<input type="password" autoComplete="off" spellCheck={false} maxLength={64} value={approval} onChange={e=>setApproval(e.target.value)} /></label>
 <button onClick={()=>setApproval('')}>清除本页审批令牌</button>
 <button disabled={busy||state?.status!=='READY'||!approved} onClick={()=>void start()}>{busy?'真实流程执行中…':'运行已授权的小试验'}</button>
 {state?.active_run_ids?.map(id=><button key={id} disabled={!approved} onClick={()=>void cancel(id)}>取消 {id}</button>)}
 {error&&<p role="alert" className="offline">{error}</p>}{result!==null&&<details open><summary>真实运行返回值（以mode和收据为准）</summary><pre>{JSON.stringify(result,null,2)}</pre></details>}</section>;
}
