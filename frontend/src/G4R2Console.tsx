import {useEffect,useState} from 'react';
const base='http://127.0.0.1:18000';
type Unit={unit_key:string;run_id:string;state:string;reason?:string;roles?:Record<string,string>;verification_status?:string;science_run_id?:string;mode?:string};
type Status={status:string;mode:string;expires_at:string;budget:{requests_remaining:number;tokens_remaining:number;usd_remaining:number;stop_reason:string|null;units:Unit[]}|null;active_run_ids:string[]};
export default function G4R2Console(){
 const [state,setState]=useState<Status|null>(null),[approval,setApproval]=useState(''),[busy,setBusy]=useState(false),[result,setResult]=useState<unknown>(null),[error,setError]=useState('');
 const approved=/^[0-9a-f]{64}$/.test(approval),units=state?.budget?.units||[];
 const ready=state?.status==='READY'&&!busy&&!state?.budget?.stop_reason&&approved;
 async function refresh(){try{const r=await fetch(base+'/api/g4r2/status',{cache:'no-store',signal:AbortSignal.timeout(5000),redirect:'error',credentials:'omit'});if(!r.ok)throw new Error();setState(await r.json());}catch{setError('第二轮后端不可达或状态读取失败。');}}
 useEffect(()=>{void refresh();const id=window.setInterval(()=>void refresh(),3000);return()=>window.clearInterval(id);},[]);
 async function submit(path:string,body?:object){
  if(!approved||busy||state?.status!=='READY')return;
  setBusy(true);setError('');
  try{const r=await fetch(base+path,{method:'POST',headers:{'Content-Type':'application/json','X-Veriflow-G4R2-Approval':approval},body:body?JSON.stringify(body):undefined,
   redirect:'error',credentials:'omit',referrerPolicy:'no-referrer',signal:AbortSignal.timeout(185000)});
   if(!r.ok){if(r.status===401||r.status===403)setApproval('');throw new Error();}setResult(await r.json());
  }catch{setError('请求拒绝或失败；不会自动重试，请查看服务端停止原因。');}finally{setBusy(false);void refresh();}
 }
 async function cancel(id:string){
  if(!approved)return;
  try{const r=await fetch(base+`/api/g4r2/${id}/cancel`,{method:'POST',headers:{'X-Veriflow-G4R2-Approval':approval},redirect:'error',credentials:'omit',referrerPolicy:'no-referrer',signal:AbortSignal.timeout(5000)});
   if(!r.ok){if(r.status===401||r.status===403)setApproval('');throw new Error();}void refresh();
  }catch{setError('取消被拒绝或失败。');}
 }
 const claimed=(key:string)=>units.some(u=>u.unit_key===key);
 const mainDone=[42,43,44,45,46].every(s=>units.some(u=>u.unit_key===`main:${s}`&&u.state==='SUCCEEDED'));
 return <section className="panel"><h3>G4R2 第二轮 · 独立授权与账本</h3>
 <p>G4-OFFLINE为真实工具/零LLM；首次REAL_LLM仅历史参考；本面板专属G4R2，G4仍BLOCKED，G5未授权。</p>
 <p>0.05 USD / 12请求 / 50000预留tokens；截止北京时间2026-11-01 21:26:58。Phase P等待独立审核，请勿输入真实Key。</p>
 <p>状态：{state?.status||'读取中'} · 模式：{state?.mode||'未知'} · 不自动连续执行。</p>
 {state?.budget&&<p>剩余 {state.budget.requests_remaining} 请求 / {state.budget.tokens_remaining} tokens / ${state.budget.usd_remaining.toFixed(6)}；停止原因 {state.budget.stop_reason||'无'}</p>}
 <label>第二轮临时审批令牌（不是API Key）<input type="password" autoComplete="off" maxLength={64} spellCheck={false} value={approval} onChange={e=>setApproval(e.target.value)} /></label>
 <button onClick={()=>setApproval('')}>清除第二轮审批</button>
 {[42,43,44,45,46].map(seed=>{const u=units.find(u=>u.unit_key===`main:${seed}`);return <div key={seed}><p><button disabled={!ready||claimed(`main:${seed}`)} onClick={()=>void submit('/api/g4r2/main',{seed})}>主队列 seed{seed} · balanced_comparison</button> {u?.state||'NOT_EXECUTED'} · {u?.mode||'尚未执行'}</p><p>{Object.entries(u?.roles||{}).map(([name,state])=>`${name}: ${state}`).join(' · ')} {u?.verification_status||''} {u?.reason||''}</p>{u?.science_run_id&&<p>{['json','csv','html'].map(format=><a key={format} href={base+`/api/science/runs/${u.science_run_id}/report/${format}`} download>导出 {format.toUpperCase()} </a>)}</p>}</div>;})}
 {['recall_quality','latency'].map(intent=><p key={intent}><button disabled={!ready||!mainDone||claimed(`extra:${intent}`)} onClick={()=>void submit('/api/g4r2/extra',{intent})}>可选 Planner-only · {intent}</button> {units.find(u=>u.unit_key===`extra:${intent}`)?.state||'NOT_EXECUTED'}</p>)}
 {state?.active_run_ids.map(id=><button key={id} disabled={!approved} onClick={()=>void cancel(id)}>取消第二轮 {id}</button>)}
 {error&&<p role="alert" className="offline">{error}</p>}
 {result!==null&&<details open><summary>本单元角色、科学子run、工具及验证收据（以mode为准）</summary><pre>{JSON.stringify(result,null,2)}</pre></details>}
 </section>;
}
