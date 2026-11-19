import G4R3Console from './G4R3Console';
import DeepSeekTrial from './DeepSeekTrial';
import G4R2Console from './G4R2Console';
import {useEffect,useRef,useState} from 'react';
import VerificationConsole from './VerificationConsole';
import HistoricalRun, { ToolRunEvidence } from './HistoricalRun';
import { scienceHistoryRuns } from './history';
const base=(import.meta.env.VITE_API_BASE_URL||'http://127.0.0.1:18000').replace(/\/$/,'');
type Run={run_id:string;workflow_id:string;state:string;science_run_id?:string;execution_mode?:string;verification_status?:string;error?:string;nodes:Record<string,{state:string;agent_id?:string;tool?:string;verification_status?:string}>;metrics?:{wall_ms:number;max_concurrency:number}};
type Artifact={artifact_id:string;tool:string;attempt_id:string;artifact_digest:string;payload:{model?:string;seed?:number;metrics?:Record<string,number|number[][]>;[k:string]:unknown};execution:unknown};
type Detail={run:Run;artifacts:Artifact[];downloads:Record<string,string>;mode:string;llm:{status:string;reason:string;roles:Record<string,string>}};
type Source={source:{rows:number;sha256:string;source:string;license:string;synthetic:boolean};profile:{class_distribution:Record<string,number>;features:string[];excluded:string[]}};
async function api<T>(path:string,body?:unknown):Promise<T>{const response=await fetch(base+path,{signal:AbortSignal.timeout(5000),cache:'no-store',...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});const value=await response.json();if(!response.ok)throw new Error(`HTTP ${response.status} · ${value.error?.code||'request_failed'}`);return value as T;}
const done=(state:string)=>['SUCCEEDED','FAILED','CANCELLED'].includes(state);
export default function ScienceConsole(){
 const [source,setSource]=useState<Source|null>(null),[llm,setLlm]=useState<{status:string;reason:string;roles:Record<string,string>}|null>(null);
 const [strategy,setStrategy]=useState('dynamic'),[verification,setVerification]=useState('verify_and_repair'),[featureSet,setFeatureSet]=useState('safe'),[seed,setSeed]=useState(42);
 const [detail,setDetail]=useState<Detail|null>(null),[recent,setRecent]=useState<Run[]>([]),[events,setEvents]=useState<unknown[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [parentId,setParentId]=useState('');
 const selectionRequest=useRef(0);
 async function refresh(){try{const [data,status,runs]=await Promise.all([api<Source>('/api/science/source'),api<NonNullable<typeof llm>>('/api/science/llm'),api<Run[]>('/api/runs')]);setSource(data);setLlm(status);setRecent(scienceHistoryRuns(runs));}catch(e){setError(String(e));}}
 async function load(id:string){const ticket=++selectionRequest.current;try{const value=await api<Detail>('/api/science/runs/'+id);const all:unknown[]=[];let after=0;for(let page=0;page<10;page++){const data=await api<{events:unknown[];next_after:number}>(`/api/runs/${id}/events?after=${after}&limit=100`);all.push(...data.events);after=data.next_after;if(data.events.length<100)break;}if(ticket===selectionRequest.current){setDetail(value);setEvents(all);setError('');}}catch(e){if(ticket===selectionRequest.current)setError(String(e));}}
 useEffect(()=>{void refresh();},[]);
 useEffect(()=>{if(!detail||done(detail.run.state))return;const timer=window.setInterval(()=>void load(detail.run.run_id),800);return()=>window.clearInterval(timer);},[detail?.run.run_id,detail?.run.state]);
 async function start(reasoning='offline'){setBusy(true);setError('');try{const run=await api<Run>('/api/science/demos',{seed,strategy,verification,feature_set:featureSet,reasoning});setParentId('');await load(run.run_id);await refresh();}catch(e){setError(String(e));}finally{setBusy(false);}}
 return <section aria-labelledby="science-title" className="panel"><p className="eyebrow">G4 OFFLINE · REPRODUCIBLE SCIENCE</p><h2 id="science-title">AI4I 合成数据科学演示</h2>
 <p>历史真实 DeepSeek 角色与科学工具子 Run 分开溯源。当前无新增收费授权，G4仍BLOCKED；不得使用旧费用门发起新请求。</p>
 {!source?<p role="status">读取官方数据来源…</p>:<><p><a href={source.source.source} target="_blank" rel="noreferrer">UCI AI4I 2020</a> · {source.source.license} · {source.source.rows} 条合成样本；不代表真实工厂验证。</p><p className="endpoint">数据 SHA256：{source.source.sha256}</p><p>类别：{JSON.stringify(source.profile.class_distribution)}；排除：{source.profile.excluded.join('、')}</p></>}
 {llm&&<details open><summary>当前本机 LLM 资源门：{llm.status}（与历史调用分开）</summary><p>当前离线接口 Planner：{llm.roles.Planner} · Reviewer：{llm.roles.Reviewer}</p><p>{llm.reason}</p><p>此状态不否定已归档的真实角色响应；历史事实以对应 Run 收据为准。</p></details>}
 {error&&<p role="alert" className="offline">数据科学任务错误 · {error}</p>}
 <div className="agent-grid"><label>实验 seed<select aria-label="实验 seed" value={seed} onChange={e=>setSeed(Number(e.target.value))}>{[42,43,44,45,46].map(n=><option key={n} value={n}>{n}</option>)}</select></label>
 <label>科学调度<select aria-label="科学调度" value={strategy} onChange={e=>setStrategy(e.target.value)}><option value="dynamic">动态多执行 Agent</option><option value="fixed_serial">固定串行 / 单 Agent</option></select></label>
 <label>科学验证<select aria-label="科学验证" value={verification} onChange={e=>setVerification(e.target.value)}><option value="verify_and_repair">G3 Hard Gate 与有限修复</option><option value="verify_only">仅验证</option><option value="execution_only">仅执行 · UNVERIFIED</option></select></label>
 <label>特征配置<select aria-label="特征配置" value={featureSet} onChange={e=>setFeatureSet(e.target.value)}><option value="safe">6项安全特征</option><option value="leakage_negative">负例：请求包含 TWF，必须拒绝</option></select></label></div>
 <div className="actions"><button disabled={busy||!source} onClick={()=>void start()}>{busy?'提交中…':'运行离线数据科学任务'}</button><button disabled={busy} onClick={()=>void start('real_llm')}>检查真实 LLM 资源门</button><button onClick={()=>void refresh()}>刷新科学历史</button></div>
 {parentId&&<HistoricalRun runId={parentId} base={base}/>}
 {!parentId&&detail&&<section className="panel"><h3>科学运行：{detail.run.state} · {detail.run.verification_status}</h3><p className="endpoint">{detail.run.run_id}</p><p>{detail.mode} · 工具子 Run 自身不调用 LLM；父角色证据见对应历史回放。</p>{detail.run.error&&<p className="offline">终止原因：{detail.run.error} · 这是工具故障/拒绝，不代表真实模型自然失败。</p>}
 <div className="actions"><button onClick={()=>void load(detail.run.run_id)}>刷新科学运行</button>{!done(detail.run.state)&&<button onClick={()=>void api('/api/runs/'+detail.run.run_id+'/cancel',{}).then(()=>load(detail.run.run_id)).catch(e=>setError(String(e)))}>取消科学任务</button>}</div>
 {detail.run.metrics&&<p>真实墙钟 {detail.run.metrics.wall_ms.toFixed(1)} ms · 工具并发 {detail.run.metrics.max_concurrency}</p>}
 <ToolRunEvidence run={detail.run}/>
 <h3>真实模型指标（测试集，正类为故障）</h3><div className="table-scroll"><table><thead><tr><th>模型 / seed</th><th>Precision</th><th>Recall</th><th>F1</th><th>Macro F1</th><th>PR-AUC / AP</th><th>ROC-AUC</th></tr></thead><tbody>{detail.artifacts.filter(a=>a.tool==='model_evaluator').map(a=><tr key={a.artifact_id}><td>{a.payload.model} / {a.payload.seed} · {a.attempt_id}</td>{['precision','recall','f1','macro_f1','pr_auc_average_precision','roc_auc'].map(k=><td key={k}>{typeof a.payload.metrics?.[k]==='number'?(a.payload.metrics[k] as number).toFixed(4):'—'}</td>)}</tr>)}</tbody></table></div>
 {detail.artifacts.filter(a=>a.tool==='model_evaluator').length===0&&<p>暂无模型评价产物，不能显示成功指标。</p>}
 <div className="actions">{Object.entries(detail.downloads).map(([kind,path])=><a href={base+path} key={kind} download>下载报告 {kind.toUpperCase()}</a>)}</div>
 <VerificationConsole runId={detail.run.run_id} state={detail.run.state}/>
 <details><summary>真实调度与运行事件（{events.length}）</summary><pre>{JSON.stringify(events,null,2)}</pre></details>
 <details><summary>数据划分、模型配置、执行进程与产物摘要（{detail.artifacts.length}）</summary><pre>{JSON.stringify(detail.artifacts,null,2)}</pre></details></section>}
 <details open><summary>最近科学与真实角色历史（{recent.length}，后端最多100条）</summary>{recent.length===0?<p>暂无运行记录</p>:recent.map(r=><p key={r.run_id}><button onClick={()=>{++selectionRequest.current;setError('');setDetail(null);if(r.science_run_id||r.execution_mode?.startsWith('REAL_LLM')){setParentId(r.run_id);}else{setParentId('');void load(r.run_id);}}}>{r.run_id}</button> {r.state} · {r.science_run_id?'真实角色父 Run → 科学子 Run':r.execution_mode?.startsWith('REAL_LLM')?'真实角色历史':'科学工具 Run'}</p>)}</details>
 <DeepSeekTrial /><G4R2Console /><G4R3Console /><p>随机分层 80/20，预处理仅训练集拟合；固定超参数，无测试集调参。五个固定 seed 仅作重复波动描述；合成数据和时间相关性限制外推。</p></section>;
}
