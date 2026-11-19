import {useEffect,useState} from 'react';
type Receipt={kind:string;seq:number;node_id?:string;attempt_id?:string;status?:string;reason?:string;action?:string;test_label?:string;timestamp:string;evidence_id:string;[key:string]:unknown};
type Audit={status:string;nodes:Record<string,{state:string;verification_status?:string;verification_reason?:string}>;receipts:Receipt[];integrity:{ok:boolean;records:number}};
const base=(import.meta.env.VITE_API_BASE_URL||'http://127.0.0.1:18000').replace(/\/$/,'');
export default function VerificationConsole({runId,state}:{runId:string;state:string}){
 const [audit,setAudit]=useState<Audit|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true);
 useEffect(()=>{let active=true;setAudit(null);setLoading(true);
 async function load(){try{const response=await fetch(`${base}/api/runs/${runId}/verification`,{signal:AbortSignal.timeout(5000),cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);const value:Audit=await response.json();if(active){setAudit(value);setError('');}}catch(e){if(active)setError(String(e));}finally{if(active)setLoading(false);}}
 void load();const timer=window.setInterval(()=>void load(),600);return()=>{active=false;window.clearInterval(timer);};},[runId,state]);
 return <section className="panel" aria-label="验证与修复"><h3>G3 验证与修复 · {loading?'加载中':audit?.status||'不可达'}</h3>
 <p>确定性 Hard Gate；模型角色调用以同 Run 官方收据为准。TEST_ONLY 故障注入是测试，不代表真实模型自行修复。</p>
 {error&&<p role="alert" className="offline">审计 API 错误：{error}</p>}
 {audit&&<><p>证据链检查：{audit.integrity.ok?'完整':'失败'} · {audit.integrity.records} 条；数据库管理员仍可改写整条链。</p>
 {Object.entries(audit.nodes).map(([id,node])=><p key={id}>{id} · {node.state} · {node.verification_status||'未执行验证'} · {node.verification_reason||''}</p>)}
 <p>G2 显式循环按节点路径计数；G3 修复按 attempt 计数。剩余预算和差异见逐条收据。</p>
 {audit.receipts.length===0?<p>无 G3 证据，历史结果不得视为已验证。</p>:audit.receipts.map(r=><details key={r.seq}><summary>#{r.seq} {r.kind} · {r.attempt_id||'run'} · {r.status||r.action||r.reason||''} {r.test_label||''}</summary><p>{r.timestamp}</p><pre>{JSON.stringify(r,null,2)}</pre></details>)}</>}
 </section>;
}
