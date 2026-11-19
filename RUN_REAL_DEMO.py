"""Launch published VeriFlow source as local HTTP + existing prebuilt frontend.

Run with a dedicated Python virtualenv containing pinned dependencies:
python RUN_REAL_DEMO.py

No LLM or external networking. Refuses to kill unknown listeners.
Press Ctrl+C to stop both child servers.
"""
import os,pathlib,socket,subprocess,sys,time,urllib.request
BASE=pathlib.Path(__file__).resolve().parent
BACKEND=BASE/"backend"
WEB=BASE/"frontend"/"dist"
def available(port):
    with socket.socket() as s:
        s.bind(("127.0.0.1",port))
def child_env():
    allow=("PATH","SystemRoot","WINDIR","TEMP","TMP","COMSPEC","USERPROFILE","LOCALAPPDATA","APPDATA","PROGRAMDATA","PROCESSOR_ARCHITECTURE")
    e={key:os.environ[key] for key in allow if key in os.environ}
    e.update({"PYTHONUTF8":"1","PYTHONDONTWRITEBYTECODE":"1","VERIFLOW_LLM_ENABLED":"false","NO_PROXY":"127.0.0.1,localhost"})
    return e
def get_status(url):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url,timeout=2) as r:
        return r.status
def main():
    if not (BACKEND/"app"/"main.py").is_file():
        raise SystemExit("ERROR: source code missing.")
    if not (WEB/"index.html").is_file():
        raise SystemExit("ERROR: compiled frontend missing; build using npm ci && npm run build, or use source-only API.")
    try:
        import fastapi,numpy,sklearn,uvicorn
    except ImportError:
        raise SystemExit("ERROR: install pinned requirements in a separate .venv first; see README_REPRO.md.")
    for port in (18000,15173):
        try:available(port)
        except OSError:raise SystemExit(f"ERROR: localhost port {port} unavailable; do not terminate unrelated processes.")
    env=child_env()
    cmds=[
      [sys.executable,"-m","uvicorn","app.main:app","--app-dir",str(BACKEND),"--host","127.0.0.1","--port","18000","--workers","1","--no-access-log"],
      [sys.executable,"-m","http.server","15173","--bind","127.0.0.1","--directory",str(WEB)],
    ]
    children=[]
    try:
        for command in cmds:
            children.append(subprocess.Popen(command,cwd=BASE,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if any(p.poll() is not None for p in children):
                raise RuntimeError("Child service exited unexpectedly")
            try:
                if get_status("http://127.0.0.1:18000/api/health")==200 and get_status("http://127.0.0.1:15173/")==200:
                    break
            except Exception:
                time.sleep(.4)
        else:raise RuntimeError("Timed out waiting for locally bound HTTP services.")
        print("\nVERIFLOW_REAL_LOCAL_DEMO_READY",flush=True)
        print("UI: http://127.0.0.1:15173/",flush=True)
        print("API: http://127.0.0.1:18000/api/health",flush=True)
        print("Offline model mode only; 127.0.0.1; Ctrl+C stops both services.",flush=True)
        while all(p.poll() is None for p in children):time.sleep(.5)
        raise RuntimeError("A child service stopped")
    except KeyboardInterrupt:
        print("\nStopping demo services...",flush=True)
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait(timeout=5)
if __name__=="__main__":main()
