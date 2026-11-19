import ScienceConsole from './ScienceConsole';
import { useEffect, useState } from 'react';
import AgentManager from './AgentManager';
import WorkflowConsole from './WorkflowConsole';

type Health = { status: 'ok'; service: 'veriflow-api'; version: string };
type Connection = { kind: 'checking' } | { kind: 'online'; health: Health } | { kind: 'offline'; message: string };
const endpoint = `${(import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:18000').replace(/\/$/, '')}/api/health`;

export default function App() {
  const [connection, setConnection] = useState<Connection>({ kind: 'checking' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    setConnection({ kind: 'checking' });
    async function check() {
      try {
        const response = await fetch(endpoint, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: unknown = await response.json();
        if (!data || typeof data !== 'object' || !('status' in data) || data.status !== 'ok' ||
            !('service' in data) || data.service !== 'veriflow-api' ||
            !('version' in data) || typeof data.version !== 'string' || !data.version) {
          throw new Error('健康检查响应格式无效');
        }
        if (active) setConnection({ kind: 'online', health: data as Health });
      } catch (error) {
        if (active) setConnection({ kind: 'offline', message: error instanceof Error ? error.message : '请求失败' });
      } finally { window.clearTimeout(timeout); }
    }
    void check();
    return () => { active = false; window.clearTimeout(timeout); controller.abort(); };
  }, [attempt]);

  return <main>
    <header><span className="brand">VF / VERIFLOW</span><span className="badge">G4 · 历史证据回放 / 当前无新增收费授权</span></header>
    <section className="intro"><p className="eyebrow">智流求真队 / VERIFLOW TEAM</p>
      <h1>VeriFlow AI <span>· 智流</span></h1>
      <p className="lead">让智能协作有序，让每个结果有据可验。</p>
      <p>2026 第五届“未来杯”创新大赛候选项目</p><p>赛题一 · Agent 编排创新应用</p>
    </section>
    <section className="panel" aria-labelledby="health-title">
      <p className="eyebrow">LOCAL SERVICE</p><h2 id="health-title">后端健康状态</h2>
      <div role="status" aria-live="polite" className={`connection ${connection.kind}`}>
        {connection.kind === 'checking' && '正在检查…'}
        {connection.kind === 'online' && `在线 · ${connection.health.service} · v${connection.health.version}`}
        {connection.kind === 'offline' && `离线 · ${connection.message}`}
      </div>
      <p className="endpoint">{endpoint}</p><button onClick={() => setAttempt(n => n + 1)} disabled={connection.kind === 'checking'}>重新检查</button>
    </section>
    <ScienceConsole />
    <WorkflowConsole />
    <details><summary>Agent 注册管理（保留 G1 能力）</summary><AgentManager /></details>
    <footer><strong>当前阶段：G4 · 历史证据回放</strong><p>支持确定性 Hard Gate 和白名单工具重执行。真实模型响应按所属 Run 的官方收据展示。</p><p>当前无新增收费授权，G4仍BLOCKED；等待 ChatGPT Gate 审核，G5未授权。</p></footer>
  </main>;
}
