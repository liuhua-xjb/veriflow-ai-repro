import VerificationConsole from './VerificationConsole';
import HistoricalRun from './HistoricalRun';
import { agentLabel } from './history';
import { useEffect, useRef, useState } from 'react';

type Workflow = { workflow_id: string; version?: number; description: string; nodes: {id:string;kind:string;gate?:{condition:string;equals:boolean}; workflow?:{workflow_id:string;version:number};iterations?:number;inputs:unknown}[]; edges:{source:string;target:string}[]; input_schema:unknown };
type NodeResult = {state:string;kind:string;dependencies:string[];agent_id?:string;agent_version?:number;started_at?:string;finished_at?:string;error?:string;result?:unknown;parent_node_id?:string};
type Run = {run_id:string;workflow_id:string;workflow_version:number;state:string;error?:string;execution_mode:string;llm_inference:string;nodes:Record<string,NodeResult>;metrics?:{wall_ms:number;scheduling_ms:number;max_concurrency:number}};
type Event = {seq:number;timestamp:string;node_id:string|null;attempt_id:string|null;kind:string;from_state:string|null;to_state:string;metadata:unknown};
const base=(import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:18000').replace(/\/$/,'');
async function request<T>(path:string,body?:unknown):Promise<T>{
  const response=await fetch(base+path,{signal:AbortSignal.timeout(5000),cache:'no-store',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  const value=await response.json();
  if(!response.ok)throw new Error(`HTTP ${response.status} · ${value.error?.code || 'request_failed'}`);
  return value as T;
}
const terminal=(state:string)=>['SUCCEEDED','FAILED','CANCELLED'].includes(state);

export default function WorkflowConsole(){
  const [templates,setTemplates]=useState<Workflow[]>([]),[workflows,setWorkflows]=useState<Workflow[]>([]);
  const [selection,setSelection]=useState(''),[input,setInput]=useState('{}'),[strategy,setStrategy]=useState('dynamic');
  const [run,setRun]=useState<Run|null>(null),[events,setEvents]=useState<Event[]>([]),[recent,setRecent]=useState<Run[]>([]);
  const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true);
  const [automatic,setAutomatic]=useState(true);
  const [mode,setMode]=useState('execution_only');
  const runRequest=useRef(0);
  const selected=workflows.find(w=>w.workflow_id===selection) || templates.find(w=>w.workflow_id===selection);
  async function refreshDefinitions(){
    setError('');setLoading(true);
    try{const [available,samples,history]=await Promise.all([request<Workflow[]>('/api/workflows'),request<{templates:Workflow[];sample_input:unknown}>('/api/workflow-templates'),request<Run[]>('/api/runs')]);
      setWorkflows(available);setTemplates(samples.templates);setRecent(history);
      setSelection(s=>s || available[0]?.workflow_id || samples.templates[0]?.workflow_id || '');
      setInput(JSON.stringify(samples.sample_input,null,2));
    }catch(e){setError(String(e));}finally{setLoading(false);}
  }
  async function refreshRun(id:string){
    const ticket=++runRequest.current;
    try{const value=await request<Run>('/api/runs/'+id);let after=0;const collected:Event[]=[];
      for(let page=0;page<12;page++){const data=await request<{events:Event[];next_after:number}>(`/api/runs/${id}/events?after=${after}&limit=100`);collected.push(...data.events);after=data.next_after;if(data.events.length<100)break;}
      if(ticket===runRequest.current){setRun(value);setEvents(collected);setError('');}
    }catch(e){if(ticket===runRequest.current)setError(String(e));}
  }
  useEffect(()=>{void refreshDefinitions();},[]);
  useEffect(()=>{if(!automatic || !run || terminal(run.state))return;const id=run.run_id;const timer=window.setInterval(()=>void refreshRun(id),300);return()=>window.clearInterval(timer);},[run?.run_id,run?.state,automatic]);
  async function start(){
    setBusy(true);setError('');setNotice('');
    try{const inputs:unknown=JSON.parse(input);const value=await request<Run>('/api/runs',{workflow_id:selection,workflow_version:selected?.version || 1,inputs,strategy,verification:{mode}});
      setRun(value);setEvents([]);setNotice('运行已提交 · '+value.run_id);void refreshRun(value.run_id);
    }catch(e){setError(String(e));}finally{setBusy(false);}
  }
  async function register(){setBusy(true);setError('');try{if(!selected)throw new Error('请选择流程');
    await request('/api/workflows',selected);setNotice('工作流已注册');await refreshDefinitions();
  }catch(e){setError(String(e));}finally{setBusy(false);}}
  const all=[...workflows,...templates.filter(t=>!workflows.some(w=>w.workflow_id===t.workflow_id))];
  return <section className="workflows" aria-labelledby="workflows-title"><p className="eyebrow">WORKFLOW RUN CONSOLE</p><h2 id="workflows-title">工作流运行台 · G3</h2>
    <p>按所选 Run 展示执行模式与证据；G3 提供确定性验证与有界同工具重执行。历史官方角色响应见只读回放。</p>
    {loading&&<p role="status">读取工作流中…</p>}{error&&<p role="alert" className="offline">运行台错误 · {error}</p>}{notice&&<p role="status">{notice}</p>}
    <div className="agent-grid"><section className="panel"><label>工作流模板<select value={selection} onChange={e=>setSelection(e.target.value)}>{all.map(w=><option value={w.workflow_id} key={w.workflow_id}>{w.workflow_id} · {w.version ? `v${w.version}`:'未注册'}</option>)}</select></label>
      <p>{selected?.description}</p><div className="actions"><button disabled={busy||loading} onClick={()=>void refreshDefinitions()}>刷新工作流</button><button disabled={busy||!!selected?.version} onClick={()=>void register()}>注册所选模板</button></div>
      <details open><summary>只读拓扑与分支</summary>{selected?.nodes.map(n=><p key={n.id}><strong>{n.id}</strong> · {n.kind} · 依赖：{selected.edges?.filter(e=>e.target===n.id).map(e=>e.source).join(', ')||'入口'}{n.gate&&` · 分支 ${n.gate.condition}=${n.gate.equals}`}{n.iterations&&` · 上限 ${n.iterations} 次`}{n.workflow&&` · 子流程 ${n.workflow.workflow_id}@${n.workflow.version}`}</p>)}</details>
    </section><section className="panel"><label>运行输入 JSON<textarea aria-label="运行输入 JSON" className="run-input" value={input} onChange={e=>setInput(e.target.value)} /></label>
      <label>验证策略<select aria-label="验证策略" value={mode} onChange={e=>setMode(e.target.value)}><option value="execution_only">仅执行 · UNVERIFIED</option><option value="verify_only">仅验证 · Hard Gate</option><option value="verify_and_repair">验证及修复 · 最多 2 次</option></select></label>
      <label>调度策略<select value={strategy} onChange={e=>setStrategy(e.target.value)}><option value="dynamic">dynamic · 能力与负载评分</option><option value="fixed_serial">fixed_serial · 固定串行基线</option></select></label>
      <div className="actions"><button disabled={busy||!selected?.version} onClick={()=>void start()}>{busy?'提交中…':'启动工作流'}</button>{run&&!terminal(run.state)&&<button onClick={()=>void request('/api/runs/'+run.run_id+'/cancel',{}).then(()=>refreshRun(run.run_id)).catch(e=>setError(String(e)))}>取消运行</button>}</div>
      <p>先运行 README 中的 G2 样例安装命令，注册样例 Agent 与子流程；模板数据来自后端。</p>
    </section></div>
    {run&&<section className="panel run-result"><h3>运行状态：{run.state}</h3><p className="endpoint">{run.run_id} · {run.workflow_id}@{run.workflow_version}</p><p>{run.execution_mode} · LLM {run.llm_inference}</p>{run.error&&<p className="offline">失败原因：{run.error}</p>}
      <label className="check"><input type="checkbox" checked={automatic} onChange={e=>setAutomatic(e.target.checked)} />自动刷新真实 API（300ms）</label><button onClick={()=>void refreshRun(run.run_id)}>刷新运行状态</button>
      {run.metrics&&<p>耗时 {run.metrics.wall_ms.toFixed(2)} ms · 调度 {run.metrics.scheduling_ms.toFixed(2)} ms · 实测最大并发 {run.metrics.max_concurrency}</p>}
      {run.execution_mode.startsWith('REAL_LLM') ? <HistoricalRun runId={run.run_id} base={base}/> : <div className="table-scroll"><table><thead><tr><th>节点 / 类型</th><th>状态</th><th>依赖</th><th>Agent 版本</th><th>耗时 / 错误</th></tr></thead><tbody>{Object.entries(run.nodes).map(([id,n])=><tr key={id}><td>{id}<small>{n.kind}</small></td><td>{n.state}</td><td>{n.dependencies.join(', ')||'入口'}</td><td>{agentLabel(run,undefined,id)}</td><td>{n.error|| (n.started_at&&n.finished_at ? `${Date.parse(n.finished_at)-Date.parse(n.started_at)} ms`:'—')}</td></tr>)}</tbody></table></div>}
      <VerificationConsole runId={run.run_id} state={run.state}/><details open><summary>真实事件与调度候选 / 排除原因（{events.length} 条）</summary><div className="events">{events.length===0?<p>暂无事件</p>:events.map(e=><details key={e.seq}><summary>#{e.seq} {e.kind} · {e.node_id||'run'} · {e.to_state}</summary><p>{e.timestamp} · attempt {e.attempt_id||'—'}</p><pre>{JSON.stringify(e.metadata,null,2)}</pre></details>)}</div></details>
    </section>}
    <details><summary>最近运行（真实记录，最多100项）</summary>{recent.length===0?<p>暂无运行记录</p>:recent.map(r=><p key={r.run_id}><button onClick={()=>void refreshRun(r.run_id)}>{r.run_id}</button> {r.state}</p>)}</details>
  </section>;
}
