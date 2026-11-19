"""User-local post-review launcher. Phase P cannot get past phase_gate()."""
import os,sys
from .g4r2_policy import phase_gate
from .g4r2_approval import R2ApprovalSession
def main():
    phase_gate()
    if os.name!='nt' or not sys.stdin.isatty() or not sys.stdout.isatty():raise RuntimeError('LOCAL_INTERACTIVE_CONSOLE_REQUIRED')
    os.environ['VERIFLOW_LLM_ENABLED']='false'
    # Old round can never activate in this dedicated process.
    for name in ('G4_REAL_BACKEND_STARTED','G4_LLM_REAL_CALLS_AUTHORIZED','VERIFLOW_LLM_API_KEY'):os.environ.pop(name,None)
    session,token=R2ApprovalSession.issue()
    try:
        with open('CONOUT$','w',encoding='utf-8') as console:
            console.write('G4R2 temporary approval (not API Key; current PID / 30 min only):\n'+token+'\n');console.flush()
        token=None
        import uvicorn
        from .main import create_app
        uvicorn.run(create_app(g4r2_session=session),host='127.0.0.1',port=18000,workers=1,access_log=False)
    finally:
        token=None;session.close()
        for name in ('VERIFLOW_G4R2_API_KEY','G4R2_BACKEND_STARTED'):os.environ.pop(name,None)
if __name__=='__main__':main()
