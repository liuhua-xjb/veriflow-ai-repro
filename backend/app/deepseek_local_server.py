"""Interactive Windows bootstrap: generate approval inside the actual server PID."""
import os,sys
from .deepseek_approval import ApprovalSession
from .deepseek_real_provider import startup_guard

def main():
    if os.name!='nt' or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('LOCAL_INTERACTIVE_CONSOLE_REQUIRED')
    startup_guard()
    os.environ['VERIFLOW_LLM_ENABLED']='false'
    session,token=ApprovalSession.issue()
    try:
        # CONOUT$ bypasses stdout/stderr log redirection; never print this via logging.
        with open('CONOUT$','w',encoding='utf-8') as console:
            console.write('Local trial approval (30 minutes, this process only). Enter in the local page password field:\n')
            console.write(token+'\n');console.flush()
        token=None
        import uvicorn
        from .main import create_app
        uvicorn.run(create_app(approval_session=session),host='127.0.0.1',port=18000,workers=1,access_log=False)
    finally:
        token=None;session.close()
        for name in ('VERIFLOW_LLM_API_KEY','G4_REAL_BACKEND_STARTED','G4_LLM_REAL_CALLS_AUTHORIZED'):
            os.environ.pop(name,None)

if __name__=='__main__':main()
