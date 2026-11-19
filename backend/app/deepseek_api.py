from fastapi import APIRouter
from .registry import RegistryError
router=APIRouter()

@router.get('/api/deepseek/preflight')
def preflight():
    return {'status':'AWAITING_COST_AUTHORIZATION','provider':'deepseek','model':'deepseek-flash',
      'base_url':'https://api.deepseek.com','thinking':'disabled','response_format':'json_object',
      'real_calls_authorized':False,'real_calls':0,'actual_spend_usd':0,'usd_hard_limit':None,
      'roles':{'Planner':'IMPLEMENTED_SIMULATED_ONLY','Reviewer':'IMPLEMENTED_SIMULATED_ONLY'},
      'proposed_only':{'max_requests':6,'max_total_tokens':12000,'max_output_tokens':800,'timeout_seconds':30},
      'reason':'需要用户明确USD硬上限、请求/token限额和有效时间；API Key不等于消费授权。'}

@router.post('/api/deepseek/real')
def real():raise RegistryError(409,'AWAITING_COST_AUTHORIZATION')
