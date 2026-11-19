import { useEffect, useState } from 'react';
import { agentLabel, historyMode, loadHistory, roleEvidence, safeDownloads } from './history';
import type { Audit, HistoryBundle, HistoryRun } from './history';

const metricKeys = ['precision', 'recall', 'f1', 'pr_auc_average_precision', 'roc_auc'];
function Chain({ audit, title }: { audit: Audit; title: string }) {
  return <div><strong>{title}：{audit.integrity.ok ? '完整' : '校验失败'} · {audit.integrity.records} 条</strong>
    <p className="endpoint">Head：{audit.integrity.head || '未记录'}</p>
    <small>数据库内哈希链，无外部锚定；管理员可重写整条链。</small></div>;
}

export function ToolRunEvidence({ run }: { run: HistoryRun }) {
  return <>
    {run.error && <p className="offline">工具执行失败：{run.error}（不代表真实模型自然失败）</p>}
    <div className="table-scroll"><table><thead><tr><th>工具节点（{Object.keys(run.nodes).length}）</th><th>执行状态</th><th>Agent 版本</th><th>验证 / 错误</th></tr></thead>
      <tbody>{Object.entries(run.nodes).map(([id, n]) => <tr key={id}><td>{id}</td><td>{n.state}</td><td>{agentLabel(run, undefined, id)}</td><td>{n.error || n.verification_status || '未记录'}</td></tr>)}</tbody></table></div>
  </>;
}

// Pure view used by both history entry points and by archive-backed rendering tests.
export function HistoryEvidenceView({ data, base }: { data: HistoryBundle; base: string }) {
  const { run, audit, science, scienceAudit } = data;
  const reviewer = roleEvidence(run, audit, 'reviewer');
  const refs = reviewer?.receipt?.proposal?.evidence_ids || [];
  const ids = new Set(scienceAudit?.receipts.map(e => e.evidence_id));
  const bound = scienceAudit?.integrity.ok === true && refs.length > 0 && refs.every(id => ids.has(id));
  return <section className="panel" aria-label="真实历史运行回放">
    <h3>历史父 Run · {run.state}</h3><p className="endpoint">{run.run_id}</p>
    <p role="status">{historyMode(run, audit)}</p>
    <p>当前资源与历史事实分开：本页只读历史收据，不代表当前有 Key 或可再次收费。正式 R3 已耗尽 8/8，禁止再次请求。</p>
    {run.error && <p className="offline">父 Run 错误：{run.error}</p>}
    <div className="table-scroll"><table><thead><tr><th>角色</th><th>Agent 版本 / 来源</th><th>官方响应 / 模型</th><th>历史 Token / 决策</th></tr></thead>
      <tbody>{['planner', 'reviewer'].map(role => {
        const e = roleEvidence(run, audit, role), r = e?.receipt;
        const nodeVersion = run.nodes[role]?.agent_version;
        const versionSource = Number.isInteger(nodeVersion) && Number(nodeVersion) > 0 ? '本节点版本' :
          Number.isInteger(r?.agent_version) && Number(r?.agent_version) > 0 ? '同 Run 对应角色收据' : '无版本证据';
        return <tr key={role}><td>{role} · {run.nodes[role]?.state || '未记录'}</td>
          <td>{agentLabel(run, audit, role)}<small>{versionSource}</small></td>
          <td>{r ? `${r.provider} / ${r.model} · 官方响应已归档` : '未证实'}</td>
          <td>{r?.usage?.total_tokens ?? '未记录'} · {r?.proposal?.decision || r?.proposal?.strategy || '—'}</td></tr>;
      })}</tbody></table></div>
    <Chain audit={audit} title="父 Run 验证哈希链" />
    {science && <section aria-label="关联科学子运行"><h3>关联科学子 Run · {science.run.state} · {science.run.verification_status}</h3>
      <p className="endpoint">{run.run_id} → {science.run.run_id}</p>
      <p>G4_OFFLINE 工具子流程；真实模型角色证据属于上方父 Run。此处指标取自该子 Run 原始产物。</p>
      <ToolRunEvidence run={science.run} />
      <h4>原始测试集模型指标（正类为故障）</h4><div className="table-scroll"><table><thead><tr><th>模型 / seed</th><th>Precision</th><th>Recall</th><th>F1</th><th>AP</th><th>ROC-AUC</th></tr></thead>
        <tbody>{science.artifacts.filter(a => a.tool === 'model_evaluator').map(a => <tr key={a.artifact_id}><td>{a.payload.model} / {a.payload.seed}</td>{metricKeys.map(k => <td key={k}>{typeof a.payload.metrics?.[k] === 'number' ? (a.payload.metrics[k] as number).toFixed(4) : '未记录'}</td>)}</tr>)}</tbody></table></div>
      {!science.artifacts.some(a => a.tool === 'model_evaluator') && <p>无模型评价产物，不能显示成功指标。</p>}
      {scienceAudit && <Chain audit={scienceAudit} title="科学子 Run G3 验证哈希链" />}
      <p>Reviewer 决策：{reviewer?.receipt?.proposal?.decision || '未记录'} · 引用 {refs.length} 条 · {bound ? '全部匹配该子 Run 原始证据' : '引用未证实'}</p>
      <div className="actions">{safeDownloads(science).map(([kind, path]) => <a key={kind} href={base + path} download>原始报告 {kind.toUpperCase()}</a>)}</div>
    </section>}
    {!science && <p>此父 Run 没有科学子 Run 关联，不补造模型指标或 Reviewer 响应。</p>}
    <details><summary>官方角色收据与 Reviewer 原始引用（只读）</summary><pre>{JSON.stringify(audit.receipts.filter(e => e.kind.endsWith('RoleResponse')), null, 2)}</pre></details>
    <p>科研边界：五次主队列 Planner 均选择 fixed_serial；离线动态实验不能归因于真实 LLM 动态提速。TWF 负例是工具拒绝，TEST_ONLY 异常不是自然模型故障。</p>
  </section>;
}

export default function HistoricalRun({ runId, base }: { runId: string; base: string }) {
  const [state, setState] = useState<{ id: string; data?: HistoryBundle; error?: string }>({ id: runId });
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setState({ id: runId });
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    void loadHistory(runId, async <T,>(path: string): Promise<T> => {
      const response = await fetch(base + path, { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json() as Promise<T>;
    }).then(data => { if (active) setState({ id: runId, data }); })
      .catch(error => { if (active) setState({ id: runId, error: String(error) }); })
      .finally(() => window.clearTimeout(timeout));
    return () => { active = false; controller.abort(); window.clearTimeout(timeout); };
  }, [runId, base]);
  // Guard even the render before the new effect has started.
  if (state.id !== runId || (!state.data && !state.error)) return <p role="status">读取所选历史证据：{runId}</p>;
  if (state.error) return <p role="alert" className="offline">历史证据不可用：{state.error} · {runId}</p>;
  return <HistoryEvidenceView data={state.data!} base={base} />;
}
