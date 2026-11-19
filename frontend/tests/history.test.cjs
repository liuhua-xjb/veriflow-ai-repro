// Archive-backed UI binding tests. The transport below is TEST_ONLY, not live HTTP/browser evidence.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '../..');
const scratch = path.join(root, '.cache', `g4-fix-f2-ui-${randomUUID()}`);
fs.mkdirSync(scratch); // unique new directory only; never enumerate old caches
const inputs = path.join(scratch, 'src');
fs.mkdirSync(inputs);
for (const file of ['history.ts', 'HistoricalRun.tsx']) {
  fs.copyFileSync(path.join(root, 'frontend/src', file), path.join(inputs, file));
}
fs.writeFileSync(path.join(scratch, 'package.json'), '{"type":"commonjs"}');
process.env.NODE_PATH = path.join(root, 'frontend/node_modules');
require('node:module').Module._initPaths();
execFileSync(process.execPath, [path.join(root, 'frontend/node_modules/typescript/bin/tsc'),
  '--ignoreConfig', '--target', 'ES2022', '--module', 'node16', '--moduleResolution', 'node16',
  '--jsx', 'react-jsx', '--noCheck', '--outDir', path.join(scratch, 'dist'),
  path.join(inputs, 'history.ts'), path.join(inputs, 'HistoricalRun.tsx')], { stdio: 'inherit' });
// This step only emits test modules. npm run typecheck checks the original source separately.
const h = require(path.join(scratch, 'dist/history.js'));
const { HistoryEvidenceView, ToolRunEvidence } = require(path.join(scratch, 'dist/HistoricalRun.js'));
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const archives = [];
for (const round of ['g4r2', 'g4r3']) {
  const dir = path.join(root, 'reports/G4/deepseek/real', round, 'real-runs');
  for (const name of fs.readdirSync(dir).filter(x => /^run_.*\.json$/.test(x) && !x.endsWith('-science.json'))) {
    const archive = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8').replace(/^\uFEFF/, ''));
    archives.push(archive);
  }
}
assert.equal(archives.length, 7);
const main = archives.filter(x => x.science);
assert.equal(main.length, 5);
const makeBundle = x => ({ run: x.run, audit: { receipts: x.role_receipts, integrity: x.integrity },
  ...(x.science ? { science: { run: x.science.run, artifacts: x.science.artifacts,
    downloads: Object.fromEntries(['json', 'csv', 'html'].map(k => [k, `/api/science/runs/${x.science.run.run_id}/report/${k}`])) },
    scienceAudit: { receipts: x.science.verification, integrity: x.science.integrity } } : {}) });
const clone = x => JSON.parse(JSON.stringify(x));
const render = b => renderToStaticMarkup(React.createElement(HistoryEvidenceView, { data: b, base: 'http://127.0.0.1:18000' }));
const bind = b => {
  const calls = [];
  const routes = { [`/api/runs/${b.run.run_id}`]: b.run,
    [`/api/runs/${b.run.run_id}/verification`]: b.audit };
  if (b.science) {
    routes[`/api/science/runs/${b.run.science_run_id}`] = b.science;
    routes[`/api/runs/${b.run.science_run_id}/verification`] = b.scienceAudit;
  }
  return { calls, read: async route => { calls.push(route); assert.ok(Object.hasOwn(routes, route)); return clone(routes[route]); } };
};

for (const a of main) {
  test(`real archive parent-child / versions / 15 metrics / exports: ${a.run.run_id}`, async () => {
    const b = makeBundle(a), transport = bind(b);
    const got = await h.loadHistory(b.run.run_id, transport.read);
    assert.equal(got.science.run.run_id, a.run.science_run_id);
    assert.equal(transport.calls.length, 4);
    assert.equal(Object.keys(got.science.run.nodes).length, 9);
    const html = render(got);
    assert.ok(html.includes('历史真实 DeepSeek Planner + Reviewer 已执行'));
    assert.ok(html.includes('g4r' + (a.run.run_id.includes('g4r2') ? '2' : '3') + '_real_planner_v1@1'));
    assert.ok(html.includes('real_reviewer_v1@1'));
    assert.ok(html.includes(a.run.run_id) && html.includes(a.run.science_run_id));
    assert.ok(html.includes('全部匹配该子 Run 原始证据'));
    assert.ok(html.includes(a.science.integrity.head));
    const metrics = got.science.artifacts.filter(x => x.tool === 'model_evaluator');
    assert.equal(metrics.length, 3);
    for (const m of metrics) for (const k of ['precision', 'recall', 'f1', 'pr_auc_average_precision', 'roc_auc']) {
      assert.equal(typeof m.payload.metrics[k], 'number');
      assert.ok(html.includes(m.payload.metrics[k].toFixed(4)));
    }
    assert.equal(h.safeDownloads(got.science).length, 3);
    assert.ok(!html.includes('@undefined') && !html.includes('LLM 尚未执行'));
  });
}
for (const a of archives.filter(x => !x.science)) {
  test(`real Planner-only remains honest: ${a.run.run_id}`, async () => {
    const b = makeBundle(a), transport = bind(b), got = await h.loadHistory(b.run.run_id, transport.read);
    assert.equal(transport.calls.length, 2);
    assert.ok(h.historyMode(got.run, got.audit).includes('Planner 已执行；Reviewer 未证实'));
    const html = render(got);
    assert.ok(html.includes('没有科学子 Run 关联'));
    assert.ok(!html.includes('Reviewer 已执行'));
  });
}
test('history list retains all five R2/R3 parents and bound children, not workflow prefix only', () => {
  const parents = main.map(x => x.run), children = main.map(x => x.science.run);
  assert.equal(h.scienceHistoryRuns([...parents, ...children]).length, 10);
  assert.equal(h.scienceHistoryRuns([{ run_id: 'unrelated', workflow_id: 'other' }]).length, 0);
});
test('a REAL_LLM string without official receipt is not a real response', () => {
  const b = makeBundle(main[0]); b.audit.receipts = [];
  assert.ok(h.historyMode(b.run, b.audit).includes('未证实'));
  assert.ok(h.agentLabel(b.run, b.audit, 'planner').includes('版本未记录'));
});
for (const [name, mutate] of [
  ['foreign run', r => { r.run_id = 'other'; }],
  ['foreign node', r => { r.node_id = 'reviewer'; }],
  ['foreign agent', r => { r.agent_id = 'other'; }],
  ['foreign attempt', r => { r.attempt_id = 'planner:2'; }],
  ['unofficial', r => { r.official_response_received = false; }],
  ['TEST_ONLY', r => { r.test_label = 'TEST_ONLY'; }],
  ['SIMULATED', r => { r.mode = 'SIMULATED'; }],
]) test(`reject ${name} role receipt and version fallback`, () => {
  const b = clone(makeBundle(main[0]));
  mutate(b.audit.receipts.find(x => x.receipt?.node_id === 'planner').receipt);
  assert.equal(h.roleEvidence(b.run, b.audit, 'planner'), undefined);
  assert.ok(h.agentLabel(b.run, b.audit, 'planner').includes('未记录'));
});
test('corrupt chain cannot certify an official model response', () => {
  const b = makeBundle(main[0]); b.audit.integrity = { ok: false, records: 3 };
  assert.equal(h.roleEvidence(b.run, b.audit, 'planner'), undefined);
});
test('missing / invalid versions never become guessed @1 or @undefined', () => {
  const b = clone(makeBundle(main[0]));
  delete b.audit.receipts.find(x => x.receipt?.node_id === 'planner').receipt.agent_version;
  for (const version of [undefined, 0, -1, 1.5]) {
    b.run.nodes.planner.agent_version = version;
    const text = h.agentLabel(b.run, b.audit, 'planner');
    assert.ok(text.includes('未记录') && !text.includes('@'));
  }
  b.run.nodes.planner.agent_version = 7;
  assert.ok(h.agentLabel(b.run, b.audit, 'planner').endsWith('@7'));
});
test('TEST_ONLY runs are explicitly separated from historical REAL_LLM', () => {
  const b = clone(makeBundle(main[0])); b.run.test_label = 'TEST_ONLY';
  assert.ok(h.historyMode(b.run, b.audit).startsWith('TEST_ONLY'));
  assert.ok(!render(b).includes('官方响应已归档'));
});
for (const [name, mutate] of [
  ['foreign child', b => { b.science.run.run_id = 'run_' + 'a'.repeat(32); }],
  ['foreign artifact', b => { b.science.artifacts[0].run_id = 'other'; }],
  ['foreign child receipt', b => { b.scienceAudit.receipts[0].run_id = 'other'; }],
  ['wrong Reviewer child binding', b => { b.audit.receipts.find(x => x.receipt?.node_id === 'reviewer').science_run_id = 'other'; }],
  ['foreign parent', b => { b.run.run_id = 'run_' + 'a'.repeat(32); }],
]) test(`cross-run data is refused: ${name}`, async () => {
  const b = clone(makeBundle(main[0])), id = b.run.run_id;
  // Keep route keys stable while deliberately corrupting their response values.
  mutate(b);
  const read = async p => {
    if (p === `/api/runs/${id}`) return clone(b.run);
    if (p === `/api/runs/${id}/verification`) return clone(b.audit);
    if (p.startsWith('/api/science/runs/')) return clone(b.science);
    return clone(b.scienceAudit);
  };
  await assert.rejects(h.loadHistory(id, read), /不匹配/);
});
test('invalid child path and arbitrary export URL cannot escape the run context', async () => {
  const b = clone(makeBundle(main[0])); b.run.science_run_id = '../../other';
  const t = bind(b);
  await assert.rejects(h.loadHistory(b.run.run_id, t.read), /无效/);
  assert.equal(t.calls.length, 2);
  b.science.downloads = { json: 'https://evil.invalid', csv: '/api/science/runs/other/report/csv', html: 'javascript:alert(1)' };
  assert.deepEqual(h.safeDownloads(b.science), []);
});
test('missing evaluator metrics stay missing rather than showing success', () => {
  const b = clone(makeBundle(main[0])); b.science.artifacts = [];
  assert.ok(render(b).includes('无模型评价产物，不能显示成功指标'));
});
test('Reviewer evidence must be exact IDs in the bound child chain', () => {
  const b = clone(makeBundle(main[0]));
  b.audit.receipts.find(x => x.receipt?.node_id === 'reviewer').receipt.proposal.evidence_ids = ['foreign'];
  assert.ok(render(b).includes('引用未证实'));
  assert.ok(!render(b).includes('全部匹配该子 Run 原始证据'));
  const corrupt = clone(makeBundle(main[0])); corrupt.scienceAudit.integrity.ok = false;
  assert.ok(!render(corrupt).includes('全部匹配该子 Run 原始证据'));
});
test('frozen real TWF tool rejection is FAILED / REJECTED, training skipped, not model failure', () => {
  const baseline = JSON.parse(fs.readFileSync(path.join(root, 'reports/G4/final-20261109/baseline.json'), 'utf8').replace(/^\uFEFF/, ''));
  const row = baseline.databases['orchestration.sqlite3'].runs.find(r => r[0] === 'run_811a94ad4c3b4d1bbfc1e82e5ac8658d');
  assert.ok(row);
  const run = JSON.parse(row[1]);
  assert.equal(run.state, 'FAILED');
  assert.equal(run.verification_status, 'REJECTED');
  assert.equal(run.nodes.leakage.state, 'FAILED');
  for (const model of ['dummy', 'logistic', 'random_forest']) assert.equal(run.nodes['train_' + model].state, 'SKIPPED');
  const html = renderToStaticMarkup(React.createElement(ToolRunEvidence, { run }));
  assert.ok(html.includes('FAILED') && html.includes('SKIPPED') && html.includes('leakage'));
  assert.ok(!html.includes('官方响应已归档'));
  assert.ok(h.historyMode(run, { receipts: [], integrity: { ok: true, records: 0 } }).includes('G4_OFFLINE'));
});
