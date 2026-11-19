import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

type Spec = { name: string; role: 'planner' | 'analyst' | 'validator'; description: string;
  capabilities: string[]; allowed_tools: string[]; model_profile: 'offline' | 'default';
  memory_policy: 'none' | 'session'; concurrency_limit: number; enabled: boolean };
type Agent = Spec & { id: string; version: number; created_at: string; updated_at: string };
type Tool = { name: string; description: string; version: string };
const base = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:18000').replace(/\/$/, '');
const blank: Spec = { name: '', role: 'planner', description: '', capabilities: ['planning'],
  allowed_tools: [], model_profile: 'offline', memory_policy: 'none', concurrency_limit: 1, enabled: true };

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(base + path, { ...options, signal: AbortSignal.timeout(5000), cache: 'no-store' });
  const data = await response.json();
  if (!response.ok) throw new Error(`HTTP ${response.status} · ${data.error?.code || 'request_failed'}${data.error?.details?.[0]?.message ? ' · ' + data.error.details[0].message : ''}`);
  return data as T;
}
const json = (value: unknown) => ({ headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });

export default function AgentManager() {
  const [items, setItems] = useState<Agent[]>([]);
  const [templates, setTemplates] = useState<Spec[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [form, setForm] = useState<Spec>(blank);
  const [caps, setCaps] = useState('planning');
  const [id, setId] = useState('');
  const [selected, setSelected] = useState<Agent | null>(null);
  const [versions, setVersions] = useState<Agent[]>([]);
  const [filter, setFilter] = useState('');
  const [availability, setAvailability] = useState('');
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  async function load() {
    setState('loading'); setError('');
    try {
      const query = new URLSearchParams();
      if (filter) query.set('capability', filter);
      if (availability) query.set('enabled', availability);
      const values = await api<Agent[]>('/api/agents?' + query);
      setItems(values); setState('ready');
    } catch (e) { setError(String(e)); setState('error'); }
  }
  useEffect(() => {
    let active = true;
    Promise.all([api<Spec[]>('/api/agent-templates'), api<Tool[]>('/api/tools')])
      .then(([t, v]) => { if (active) { setTemplates(t); setTools(v); } })
      .catch(e => { if (active) setError(String(e)); });
    void load();
    return () => { active = false; };
  }, []);

  function apply(spec: Spec) { setForm(spec); setCaps(spec.capabilities.join(', ')); }
  async function details(agent: Agent) {
    setBusy(true); setError(''); setNotice('');
    try {
      const history = await api<Agent[]>(`/api/agents/${agent.id}/versions`);
      setVersions(history); setSelected(agent); setId(agent.id);
      apply({ name: agent.name, role: agent.role, description: agent.description,
        capabilities: agent.capabilities, allowed_tools: agent.allowed_tools, model_profile: agent.model_profile,
        memory_policy: agent.memory_policy, concurrency_limit: agent.concurrency_limit, enabled: agent.enabled });
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    const value = { ...form, capabilities: caps.split(',').map(s => s.trim()).filter(Boolean) };
    try {
      const saved = selected
        ? await api<Agent>(`/api/agents/${selected.id}/versions`, { method: 'POST', ...json({ ...value, base_version: selected.version }) })
        : await api<Agent>('/api/agents', { method: 'POST', ...json({ ...value, ...(id ? { id } : {}) }) });
      setNotice(`成功 · ${saved.name} · v${saved.version}`);
      setSelected(null); setId(''); apply(blank); setVersions([]);
      await load();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function toggle(agent: Agent) {
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = await api<Agent>(`/api/agents/${agent.id}/enabled`, { method: 'PATCH',
        ...json({ enabled: !agent.enabled, base_version: agent.version }) });
      setNotice(`成功 · ${saved.name} · ${saved.enabled ? '已启用' : '已禁用'} · v${saved.version}`);
      if (selected?.id === agent.id) { setSelected(null); setVersions([]); setId(''); apply(blank); }
      await load();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }

  return <section className="agents" aria-labelledby="agents-title">
    <div className="section-heading"><div><p className="eyebrow">AGENT REGISTRY</p><h2 id="agents-title">Agent 注册管理</h2></div><span className="badge">G1 · 定义与能力画像</span></div>
    <p>角色模板仅定义元数据与权限，尚未实现自主协作。LLM 默认离线。</p>
    {error && <p role="alert" className="offline">请求失败 · {error}</p>}
    {notice && <p role="status" className="online">{notice}</p>}
    <div className="agent-grid"><section className="panel">
      <h3>Agent 列表</h3><div className="filters">
        <label>能力筛选<input value={filter} onChange={e => setFilter(e.target.value)} placeholder="如 analysis" /></label>
        <label>启用筛选<select value={availability} onChange={e => setAvailability(e.target.value)}><option value="">全部</option><option value="true">已启用</option><option value="false">已禁用</option></select></label>
        <button onClick={() => void load()} disabled={busy || state === 'loading'}>刷新列表</button>
      </div>
      {state === 'loading' && <p role="status">加载中…</p>}
      {state === 'error' && <p>列表不可用，请确认后端运行后刷新。</p>}
      {state === 'ready' && items.length === 0 && <p>暂无匹配的 Agent。可在右侧创建。</p>}
      {state === 'ready' && items.map(a => <article className="agent-card" key={a.id}>
        <h4>{a.name} <small>v{a.version} · {a.enabled ? '已启用' : '已禁用'}</small></h4>
        <p className="endpoint">{a.id} · {a.role}</p><p>能力：{a.capabilities.join(' / ')}</p><p>工具：{a.allowed_tools.join(' / ') || '无'}</p>
        <div className="actions"><button disabled={busy} onClick={() => void details(a)}>详情 / 版本</button><button disabled={busy} onClick={() => void toggle(a)}>{a.enabled ? '禁用' : '启用'}</button></div>
      </article>)}
    </section><section className="panel"><h3>{selected ? `编辑 ${selected.name}（基于 v${selected.version}）` : '创建 Agent'}</h3>
      <label>角色模板<select value={form.role} disabled={!!selected} onChange={e => { const template = templates.find(t => t.role === e.target.value); if (template) apply(template); }}>
        <option value="">选择模板（可继续编辑）</option>{templates.map(t => <option value={t.role} key={t.role}>{t.name}</option>)}
      </select></label>
      <form onSubmit={e => void save(e)}>
        <label>Agent ID（留空自动生成）<input value={id} disabled={!!selected} maxLength={64} pattern="[a-z][a-z0-9_-]*" onChange={e => setId(e.target.value)} /></label>
        <label>名称<input required maxLength={100} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
        <label>角色<select value={form.role} onChange={e => setForm({ ...form, role: e.target.value as Spec['role'] })}><option value="planner">planner</option><option value="analyst">analyst</option><option value="validator">validator</option></select></label>
        <label>简介<textarea maxLength={2000} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label>
        <label>能力（英文标识，逗号分隔）<input required value={caps} onChange={e => setCaps(e.target.value)} /></label>
        <fieldset><legend>工具权限（只读内置工具）</legend>{tools.map(t => <label className="check" key={t.name}><input type="checkbox" checked={form.allowed_tools.includes(t.name)} onChange={e => setForm({ ...form, allowed_tools: e.target.checked ? [...form.allowed_tools, t.name] : form.allowed_tools.filter(n => n !== t.name) })} />{t.name} · {t.version}</label>)}</fieldset>
        <label>模型配置引用<select value={form.model_profile} onChange={e => setForm({ ...form, model_profile: e.target.value as Spec['model_profile'] })}><option value="offline">offline</option><option value="default">default（后端环境配置；本页不调用）</option></select></label>
        <label>记忆策略（仅元数据）<select value={form.memory_policy} onChange={e => setForm({ ...form, memory_policy: e.target.value as Spec['memory_policy'] })}><option value="none">none</option><option value="session">session</option></select></label>
        <label>并发上限（调度约束）<input type="number" min={1} max={16} required value={form.concurrency_limit} onChange={e => setForm({ ...form, concurrency_limit: Number(e.target.value) })} /></label>
        <label className="check"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />启用</label>
        <div className="actions"><button type="submit" disabled={busy}>{busy ? '提交中…' : selected ? '创建新版本' : '注册 Agent'}</button>
        {selected && <button type="button" onClick={() => { setSelected(null); setId(''); apply(blank); setVersions([]); }}>返回新建</button>}</div>
      </form>
      {versions.length > 0 && <section><h4>历史版本（保留原定义）</h4>{versions.map(v => <p key={v.version}>v{v.version} · {v.name} · {v.enabled ? '启用' : '禁用'} · {v.updated_at}</p>)}</section>}
    </section></div>
  </section>;
}
