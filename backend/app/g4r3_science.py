"""Re-derive Reviewer context from the actual linked scientific run, never HTTP input."""
from .deepseek_preflight import build_review_summary
from .deepseek_repeat_prep import MODELS,review_summary
from .science_contracts import DATASET_SHA
from .science_data import load_data
from .science_validation import check_training,reference_metrics
from .llm import ProviderError

def science_summary(state,parent,child,unit):
    owner=state.orchestration.get(parent);run=state.orchestration.get(child)
    if owner.get('science_run_id')!=child or owner.get('seed')!=unit.seed or owner.get('unit_key')!=f'main:{unit.seed}':raise ProviderError('G4R3_CHILD_SCOPE_MISMATCH')
    if run['state']!='SUCCEEDED' or run.get('verification_status')!='VERIFIED' or len(run['nodes'])!=9 or any(n['state']!='SUCCEEDED' for n in run['nodes'].values()):raise ProviderError('G4R3_HARD_GATE_REJECTED')
    if not state.verification.integrity(child)['ok']:raise ProviderError('G4R3_AUDIT_CORRUPT')
    receipts=state.verification.receipts(child)
    if not any(r['kind']=='RunVerificationFinished' and r.get('status')=='VERIFIED' for r in receipts):raise ProviderError('G4R3_FAKE_VERIFIED')
    records=state.controller.catalog.artifacts.list(child);training={a['payload']['model']:a['payload'] for a in records if a['tool']=='model_trainer'}
    evaluated={a['payload']['model']:a['payload'] for a in records if a['tool']=='model_evaluator'}
    if set(training)!=set(MODELS) or set(evaluated)!=set(MODELS):raise ProviderError('G4R3_THREE_MODELS_REQUIRED')
    rows=load_data()
    for model,payload in training.items():
        if payload['seed']!=unit.seed or payload['data_sha256']!=DATASET_SHA:raise ProviderError('G4R3_ACTUAL_SEED_MISMATCH')
        check_training(payload,rows)
        expected=reference_metrics(payload['labels'],payload['predictions'],payload['probabilities'])
        if evaluated[model]['metrics']!=expected:
            import math
            if any(evaluated[model]['metrics'][k]!=v if k=='confusion_matrix' else not math.isclose(evaluated[model]['metrics'][k],v,rel_tol=1e-10,abs_tol=1e-12) for k,v in expected.items()):raise ProviderError('G4R3_METRIC_FORGERY')
    if len({p['train_split_sha256'] for p in training.values()})!=1 or len({p['test_split_sha256'] for p in training.values()})!=1:raise ProviderError('G4R3_SPLIT_MISMATCH')
    sample=training['dummy']
    summary={**build_review_summary(state,child),'seed':unit.seed,'data_sha256':DATASET_SHA,
        'train_split_sha256':sample['train_split_sha256'],'test_split_sha256':sample['test_split_sha256']}
    review_summary(unit,summary);return summary
