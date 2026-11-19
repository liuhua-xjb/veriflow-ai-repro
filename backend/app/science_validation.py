"""Independent standard-library reference for split safety and classifier metrics."""
import math
from .science_contracts import FEATURES,EXCLUDED,DATASET_SHA
from .science_data import load_data,approved_features,profile
from .workflow_models import digest


def reference_metrics(labels,predictions,scores):
    if not (len(labels)==len(predictions)==len(scores)==2000):raise ValueError('prediction_count_mismatch')
    if any(type(p) is not int or p not in (0,1) for p in predictions) or any(not math.isfinite(s) or not 0<=s<=1 for s in scores):raise ValueError('invalid_predictions')
    tn=sum(y==0 and p==0 for y,p in zip(labels,predictions));fp=sum(y==0 and p==1 for y,p in zip(labels,predictions))
    fn=sum(y==1 and p==0 for y,p in zip(labels,predictions));tp=sum(y==1 and p==1 for y,p in zip(labels,predictions))
    precision=tp/(tp+fp) if tp+fp else 0;recall=tp/(tp+fn) if tp+fn else 0
    f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0;negative=2*tn/(2*tn+fp+fn) if 2*tn+fp+fn else 0
    positives=sum(labels);negatives=len(labels)-positives
    groups={}
    for y,s in zip(labels,scores):
        counts=groups.setdefault(s,[0,0]);counts[y]+=1
    seen=positive_seen=0;ap=0.;negative_below=0;favorable=0.
    for score,(n,p) in sorted(groups.items(),reverse=True):
        seen+=n+p;positive_seen+=p;ap+=p/positives*(positive_seen/seen)
    for score,(n,p) in sorted(groups.items()):
        favorable+=p*(negative_below+n/2);negative_below+=n
    return {'accuracy':(tp+tn)/len(labels),'precision':precision,'recall':recall,'f1':f1,'macro_f1':(f1+negative)/2,
            'pr_auc_average_precision':ap,'roc_auc':favorable/(positives*negatives),'confusion_matrix':[[tn,fp],[fn,tp]]}


def check_training(payload,rows):
    train,test=payload['train_indices'],payload['test_indices']
    if len(train)!=8000 or len(test)!=2000 or len(set(train))!=8000 or len(set(test))!=2000 or set(train)&set(test) or set(train)|set(test)!=set(range(10000)):raise ValueError('split_leakage')
    if not approved_features(payload['features']) or payload['excluded']!=EXCLUDED or payload['fit_scope']!='TRAIN_ONLY':raise ValueError('feature_leakage')
    if payload['train_split_sha256']!=digest(train) or payload['test_split_sha256']!=digest(test):raise ValueError('split_digest_failed')
    means=[math.fsum(float(rows[i][k]) for i in train)/8000 for k in FEATURES[1:]]
    if any(not math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-9) for a,b in zip(means,payload['scaler_mean'])) or len(payload['scaler_mean'])!=5:raise ValueError('preprocess_not_train_only')
    truth=[int(rows[i]['Machine failure']) for i in test]
    if truth!=payload['labels'] or sum(truth)!=68:raise ValueError('labels_or_stratification_failed')
    if payload['encoder_categories']!=[sorted({rows[i]['Type'] for i in train})]:raise ValueError('encoder_fit_failed')
    reference_metrics(truth,payload['predictions'],payload['probabilities'])


def validate_artifact(tool,arguments,record,artifacts,run_id):
    if record['tool']!=tool or record['data_sha256']!=DATASET_SHA or record['input_digest']!=digest(arguments):raise ValueError('provenance_mismatch')
    rows=load_data();payload=record['payload']
    if tool=='data_profiler':
        if payload!=profile(rows):raise ValueError('profile_mismatch')
    elif tool=='leakage_check':
        if not payload['safe'] or not approved_features(payload['features']) or arguments['features']!=payload['features']:raise ValueError('feature_leakage')
    elif tool=='model_trainer':check_training(payload,rows)
    elif tool=='model_evaluator':
        parent=artifacts.load(run_id,arguments['training_id'])
        if parent['tool']!='model_trainer':raise ValueError('wrong_training_source')
        trained=parent['payload'];check_training(trained,rows)
        expected=reference_metrics([int(rows[i]['Machine failure']) for i in trained['test_indices']],trained['predictions'],trained['probabilities'])
        if set(payload['metrics'])!=set(expected):raise ValueError('metrics_schema_mismatch')
        for key,value in expected.items():
            if key=='confusion_matrix':
                if payload['metrics'][key]!=value:raise ValueError('confusion_matrix_mismatch')
            elif not math.isclose(payload['metrics'][key],value,rel_tol=1e-10,abs_tol=1e-12):raise ValueError('metric_mismatch')
        if payload['model']!=trained['model'] or payload['seed']!=trained['seed']:raise ValueError('model_identity_mismatch')
    elif tool=='report_exporter':
        expected=[artifacts.load(run_id,arguments[k])['payload'] for k in ('dummy_id','logistic_id','forest_id')]
        if payload['models']!=expected or payload['features']!=FEATURES or payload['source']['sha256']!=DATASET_SHA:raise ValueError('export_provenance_mismatch')
        if [p['model'] for p in expected]!=['dummy','logistic','random_forest'] or len({p['test_split_sha256'] for p in expected})!=1:raise ValueError('incomparable_models')
    else:raise ValueError('unknown_science_validator')
