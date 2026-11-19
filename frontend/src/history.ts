// Read-only projections. A mode label alone is never evidence of a model response.
export type HistoryNode = {
  state: string; kind?: string; dependencies?: string[]; agent_id?: string;
  agent_version?: number; attempt_id?: string; error?: string; verification_status?: string;
};
export type HistoryRun = {
  run_id: string; workflow_id: string; state: string; nodes: Record<string, HistoryNode>;
  science_run_id?: string; execution_mode?: string; llm_inference?: string;
  test_label?: string | null; error?: string; verification_status?: string;
};
export type RoleReceipt = {
  mode: string; run_id: string; node_id: string; agent_id: string; agent_version?: number;
  attempt_id?: string; test_label?: string | null; provider?: string; model?: string;
  official_response_received?: boolean; official_model_response_received?: boolean;
  prompt_version?: string; usage?: { total_tokens?: number };
  proposal?: { decision?: string; evidence_ids?: string[]; strategy?: string };
};
export type Evidence = {
  kind: string; run_id: string; node_id?: string; evidence_id?: string; receipt_hash?: string;
  receipt?: RoleReceipt; science_run_id?: string;
};
export type Audit = {
  status?: string; receipts: Evidence[];
  integrity: { ok: boolean; records: number; head?: string; boundary?: string };
};
export type HistoryArtifact = {
  artifact_id: string; run_id: string; tool: string;
  payload: { model?: string; seed?: number; metrics?: Record<string, unknown> };
};
export type ScienceHistory = {
  run: HistoryRun; artifacts: HistoryArtifact[]; downloads: Record<string, string>;
};
export type HistoryBundle = { run: HistoryRun; audit: Audit; science?: ScienceHistory; scienceAudit?: Audit };
export type ReadHistory = <T>(path: string) => Promise<T>;
const validVersion = (n: unknown): n is number => Number.isInteger(n) && Number(n) > 0;

export function roleEvidence(run: HistoryRun, audit: Audit, role: string): Evidence | undefined {
  const node = run.nodes[role];
  if (!node || audit.integrity.ok !== true || !run.execution_mode?.startsWith('REAL_LLM') ||
      !['REAL_LLM', 'REAL_LLM_PLANNER_ONLY'].includes(run.llm_inference || '') || run.test_label != null) return undefined;
  // Use the corresponding terminal attempt, never a receipt from another run or role.
  return audit.receipts.find(e => {
    const r = e.receipt;
    return e.kind.endsWith('RoleResponse') && e.run_id === run.run_id && !!e.receipt_hash &&
      !!e.evidence_id && r?.run_id === run.run_id && r.node_id === role && r.mode === 'REAL_LLM' &&
      r.provider === 'deepseek' && r.official_response_received === true &&
      r.official_model_response_received === true && r.test_label == null &&
      !!node.agent_id && r.agent_id === node.agent_id &&
      (!node.attempt_id || r.attempt_id === node.attempt_id);
  });
}

export function agentLabel(run: HistoryRun, audit: Audit | undefined, role: string): string {
  const node = run.nodes[role];
  if (!node?.agent_id) return node?.kind === 'llm_role' ? 'Agent 未记录' : '控制 / 工具节点';
  const receipt = audit && roleEvidence(run, audit, role)?.receipt;
  const version = validVersion(node.agent_version) ? node.agent_version : receipt?.agent_version;
  return `${node.agent_id}${validVersion(version) ? `@${version}` : ' · 版本未记录'}`;
}

export function historyMode(run: HistoryRun, audit: Audit): string {
  if (run.test_label != null || run.execution_mode?.includes('SIMULATED')) return 'TEST_ONLY / SIMULATED · 非真实模型响应';
  const planner = roleEvidence(run, audit, 'planner');
  const reviewer = roleEvidence(run, audit, 'reviewer');
  if (planner && reviewer) return 'REAL_LLM · 历史真实 DeepSeek Planner + Reviewer 已执行';
  if (planner) return 'REAL_LLM · 历史真实 DeepSeek Planner 已执行；Reviewer 未证实';
  if (run.execution_mode?.startsWith('REAL_LLM')) return '历史 REAL_LLM 标签 · 对应官方角色收据未证实';
  return 'G4_OFFLINE · 确定性工具执行（此 Run 自身不调用 LLM）';
}

export function safeDownloads(science: ScienceHistory): [string, string][] {
  const prefix = `/api/science/runs/${science.run.run_id}/report/`;
  return Object.entries(science.downloads).filter(([kind, path]) =>
    ['json', 'csv', 'html'].includes(kind) && path === prefix + kind);
}

export function scienceHistoryRuns<T extends { run_id: string; workflow_id: string; science_run_id?: string; execution_mode?: string }>(runs: T[]): T[] {
  const children = new Set(runs.map(r => r.science_run_id).filter(Boolean));
  return runs.filter(r => r.science_run_id || r.execution_mode?.startsWith('REAL_LLM') ||
    children.has(r.run_id) || r.workflow_id.startsWith('g4_'));
}

export async function loadHistory(id: string, read: ReadHistory): Promise<HistoryBundle> {
  if (!/^run_(?:g4r[23]_)?[a-f0-9]{32}$/.test(id)) throw new Error('历史 Run ID 格式无效');
  const [run, audit] = await Promise.all([
    read<HistoryRun>(`/api/runs/${id}`), read<Audit>(`/api/runs/${id}/verification`),
  ]);
  if (run.run_id !== id || audit.receipts.some(e => e.run_id !== id)) throw new Error('父 Run / 验证收据关联不匹配');
  if (!run.science_run_id) return { run, audit };
  const child = run.science_run_id;
  if (!/^run_[a-f0-9]{32}$/.test(child) || child === id) throw new Error('科学子 Run ID 无效');
  const [science, scienceAudit] = await Promise.all([
    read<ScienceHistory>(`/api/science/runs/${child}`), read<Audit>(`/api/runs/${child}/verification`),
  ]);
  if (science.run.run_id !== child || science.artifacts.some(a => a.run_id !== child) ||
      scienceAudit.receipts.some(e => e.run_id !== child)) throw new Error('科学子 Run / 产物 / 验证收据关联不匹配');
  const reviewer = roleEvidence(run, audit, 'reviewer');
  if (reviewer && reviewer.science_run_id !== child) throw new Error('Reviewer 引用的科学子 Run 不匹配');
  return { run, audit, science, scienceAudit };
}
