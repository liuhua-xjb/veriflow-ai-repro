# VeriFlow AI - Real HTTP Reproducibility Candidate

Team: Zhi Liu Qiu Zhen / 2026 Future Cup Track 1.

This clean release candidate contains source/data/license files copied without changes
from the existing sealed submission package, plus new localhost launch and smoke-test helpers.

2026-11-19 Windows real HTTP test evidence:
- GET /api/health returned HTTP 200.
- GET /api/science/source reported public synthetic AI4I dataset, 10000 rows.
- POST /api/science/demos with seed 42, dynamic, verify_and_repair, safe features,
  offline reasoning: HTTP 202, final SUCCEEDED / VERIFIED, all 9 science nodes SUCCEEDED,
  and json/csv/html report links were available.
- The same request with feature_set leakage_negative ended FAILED / REJECTED,
  with leakage FAILED and all seven downstream nodes SKIPPED.
- This was tested in a fresh isolated project copy, not a screenshot replay;
  no real LLM/API key, dependencies installation, or original database changes.

WINDOWS REQUIREMENTS
Python 3.13 x64 and a local virtualenv with FastAPI/uvicorn/numpy/scipy/scikit-learn.
The bundled frontend/dist is a previous local prebuilt copy and was not rebuilt from
the staged source in this step. Full source is under backend/ and frontend/.
Node 24 is only required if the reviewer wants to rebuild the front end.

FIRST-TIME SETUP (requires PyPI access for Python dependencies):
  py -3.13 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r backend\requirements.lock.txt
  .\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r backend\requirements-g4.lock.txt
  .\.venv\Scripts\python.exe -m pip check

START (new PowerShell terminal):
  .\.venv\Scripts\python.exe RUN_REAL_DEMO.py
or double-click RUN_REAL_DEMO.cmd. The launcher binds to localhost only
and uses a narrow environment-variable allowlist to avoid API keys.

Local UI: http://127.0.0.1:15173/
Local API: http://127.0.0.1:18000/api/health

TEST (second terminal while both services run):
  .\.venv\Scripts\python.exe TEST_REAL_HTTP.py

Expected output: REAL_HTTP_REPRO_PASS; the test starts real offline ML jobs and
writes a new JSON report under repro_evidence/ within this copy.
Both services can be stopped by Ctrl+C in the launch terminal.

Four boundaries:
1. No real Planner/Reviewer LLM calls occur in this offline science test.
2. AI4I is synthetic public data, not live industrial training/validation.
3. No full clean machine reinstallation or cloud deployment was tested here.
4. localhost endpoints are unprotected; never publish the services to the Internet.

The existing GitHub CLI on the author's Windows computer is currently not logged
in. No GitHub/Gitee repository URL is confirmed yet. Do not invent a link.
Before public publishing, the team must approve ownership, third-party licenses,
and disclosure of the source. Use a NEW repository after approval, never reuse an
unrelated older repository. See PUBLISH_GITHUB_AFTER_SIGNOFF.md.

The original sealed competition ZIP remains unchanged.
