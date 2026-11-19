"""Owned worker process, hard deadline/cancel and Windows Job Object limits."""
import ctypes,json,os,subprocess,sys,time
from ctypes import wintypes
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


class WorkerError(Exception):
    def __init__(self,code,record):self.code,self.record=code,record;super().__init__(code)


class WindowsJob:
    def __init__(self,process):
        if os.name!='nt':raise RuntimeError('windows_job_limits_required')
        class Basic(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),('LimitFlags',wintypes.DWORD),
                ('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),
                ('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ['ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount']]
        class Extended(ctypes.Structure):
            _fields_=[('BasicLimitInformation',Basic),('IoInfo',Counters),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];self.api.CreateJobObjectW.restype=wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.api.CloseHandle.argtypes=[wintypes.HANDLE]
        self.handle=self.api.CreateJobObjectW(None,None)
        if not self.handle:raise RuntimeError('job_create_failed')
        limits=Extended();limits.BasicLimitInformation.LimitFlags=0x2000|0x100|0x200|0x8
        # Windows venv python.exe is a redirector which owns one interpreter child.
        limits.BasicLimitInformation.ActiveProcessLimit=2
        limits.ProcessMemoryLimit=1024**3;limits.JobMemoryLimit=1024**3
        if not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not self.api.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(process._handle))):
            self.close();raise RuntimeError('job_limits_unavailable')

    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None


def isolated(request,signal,deadline):
    raw=json.dumps(request,allow_nan=False).encode()
    if len(raw)>1048576:raise WorkerError('ipc_input_limit',{})
    env={k:os.environ[k] for k in ('SystemRoot','WINDIR','COMSPEC') if k in os.environ}
    temporary=ROOT/'.cache/g4-worker';temporary.mkdir(parents=True,exist_ok=True)
    env.update(PYTHONUTF8='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',NUMEXPR_NUM_THREADS='1',TEMP=str(temporary),TMP=str(temporary),PATH=str(Path(sys.executable).parent))
    process=subprocess.Popen([sys.executable,'-m','app.science_worker'],cwd=ROOT/'backend',env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    start=time.monotonic();job=None
    record={'launcher_pid':process.pid,'memory_limit_bytes':1024**3,'max_active_processes':2,'venv_redirector':True,'threads_limit':1,'test_label':'TEST_ONLY' if request.get('test_only') else None}
    try:
        job=WindowsJob(process);record['job_limits_enforced']=True
        pending=raw
        while True:
            if signal.is_set():raise WorkerError('cancelled',record)
            if time.monotonic()>=deadline:raise WorkerError('science_timeout',record)
            try:
                stdout,stderr=process.communicate(input=pending,timeout=.025);break
            except subprocess.TimeoutExpired:pending=None
        record.update(exit_code=process.returncode,wall_ms=(time.monotonic()-start)*1000)
        if len(stdout)>2097152:raise WorkerError('ipc_output_limit',record)
        result=json.loads(stdout)
        if process.returncode or result.get('ok') is not True:raise WorkerError('science_worker_failed',record)
        record['worker_pid']=result['worker_pid']
        return result['payload'],record
    except WorkerError:raise
    except Exception:raise WorkerError('science_isolation_error',record) from None
    finally:
        if process.poll() is None:
            if job:job.close();job=None
            if process.poll() is None:process.kill()
            record['terminated']=True
        process.communicate(timeout=5)
        if job:job.close()
        record['exit_code']=process.returncode
