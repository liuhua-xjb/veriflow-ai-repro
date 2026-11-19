from app.agents import AgentCreate, AgentRecord
from app.scheduler import Scheduler
from app.workflow_models import Node,SchedulerConfig
from datetime import datetime,timezone


def agent(id_,**extra):
    value=AgentCreate(id=id_,name=id_,role='analyst',capabilities=['analysis'],allowed_tools=['numeric_summary'],**extra)
    return AgentRecord(**value.model_dump(),created_at=datetime.now(timezone.utc),updated_at=datetime.now(timezone.utc))


def test_stable_scoring_load_permissions():
    scheduler=Scheduler();a=agent('a',concurrency_limit=2);b=agent('b',concurrency_limit=2)
    bad=agent('disabled',enabled=False)
    node=Node(id='task',kind='tool_task',tool='numeric_summary',capability='analysis')
    config=SchedulerConfig()
    chosen,trace=scheduler.choose(node,[b,a,bad],'dynamic',config,reserve=True)
    assert chosen['agent_id']=='a' and trace[2]['excluded']==['disabled']
    chosen,_=scheduler.choose(node,[a,b],'dynamic',config,reserve=True)
    assert chosen['agent_id']=='b'
    scheduler.release(a);scheduler.release(b)
    selected=[scheduler.choose(node,[b,a],'dynamic',config)[0]['agent_id'] for _ in range(5)]
    assert selected==['a']*5
    denied=agent('denied');denied.allowed_tools=[]
    _,trace=scheduler.choose(node,[denied],'dynamic',config)
    assert trace[0]['excluded']==['tool_permission_denied']


def test_concurrency_and_global_limit():
    scheduler=Scheduler();values=[agent('a'+str(i)) for i in range(5)]
    node=Node(id='task',kind='tool_task',tool='numeric_summary',capability='analysis')
    config=SchedulerConfig()
    for i in range(4):assert scheduler.choose(node,values,'dynamic',config,reserve=True)[0]['agent_id']==values[i].id
    assert scheduler.choose(node,values,'dynamic',config,reserve=True)[0] is None
    for a in values[:4]:scheduler.release(a)
    assert scheduler.active==0 and scheduler.loads=={}
