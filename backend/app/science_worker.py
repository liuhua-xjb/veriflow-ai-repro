"""Code-owned isolated entrypoint; no loading pickles, expressions or plugins."""
import json,os,sys,time
from .science_data import load_data,profile,approved_features
from .science_contracts import FEATURES,EXCLUDED,DATASET_SHA


def split_data(rows,seed):
    import numpy as np
    from sklearn.model_selection import train_test_split
    labels=np.array([int(r['Machine failure']) for r in rows])
    train,test=train_test_split(np.arange(len(rows)),test_size=.2,stratify=labels,random_state=seed)
    return labels,train,test


def calculate(y,pred,probability):
    from sklearn.metrics import (accuracy_score,precision_score,recall_score,f1_score,average_precision_score,roc_auc_score,confusion_matrix)
    return {'accuracy':float(accuracy_score(y,pred)), 'precision':float(precision_score(y,pred,zero_division=0)),
            'recall':float(recall_score(y,pred,zero_division=0)), 'f1':float(f1_score(y,pred,zero_division=0)),
            'macro_f1':float(f1_score(y,pred,average='macro',zero_division=0)),
            'pr_auc_average_precision':float(average_precision_score(y,probability)), 'roc_auc':float(roc_auc_score(y,probability)),
            'confusion_matrix':confusion_matrix(y,pred,labels=[0,1]).tolist()}


def execute(operation,params,inputs):
    rows=load_data()
    if operation=='data_profiler':return profile(rows)
    if operation=='leakage_check':
        return {'safe':approved_features(params['features']),'features':params['features'],'excluded':EXCLUDED,
                'reason':'exact_allowlist' if approved_features(params['features']) else 'forbidden_or_unknown_features','data_sha256':DATASET_SHA}
    if operation=='model_trainer':
        if not inputs['leakage_id']['safe'] or not approved_features(inputs['leakage_id']['features']):raise ValueError('leakage_rejected')
        import numpy as np
        import sklearn
        from sklearn.compose import ColumnTransformer
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder,StandardScaler
        from sklearn.dummy import DummyClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.ensemble import RandomForestClassifier
        from threadpoolctl import threadpool_limits
        y,train,test=split_data(rows,params['seed'])
        x=np.array([[r[FEATURES[0]]]+[float(r[k]) for k in FEATURES[1:]] for r in rows],dtype=object)
        preprocessing=ColumnTransformer([('category',OneHotEncoder(handle_unknown='ignore',sparse_output=False),[0]),('numeric',StandardScaler(),list(range(1,6)))],remainder='drop')
        estimators={'dummy':DummyClassifier(strategy='most_frequent'),
            'logistic':LogisticRegression(max_iter=1000,class_weight='balanced',random_state=params['seed']),
            'random_forest':RandomForestClassifier(n_estimators=60,max_depth=10,min_samples_leaf=2,class_weight='balanced',random_state=params['seed'],n_jobs=1)}
        estimator=estimators[params['model']];pipeline=Pipeline([('preprocess',preprocessing),('classifier',estimator)])
        started=time.perf_counter()
        with threadpool_limits(limits=1):
            pipeline.fit(x[train],y[train]);training_ms=(time.perf_counter()-started)*1000
            predicted=pipeline.predict(x[test]);scores=pipeline.predict_proba(x[test])[:,1]
        from .workflow_models import digest
        return {'model':params['model'],'seed':params['seed'],'sklearn_version':sklearn.__version__,'features':FEATURES,'excluded':EXCLUDED,
            'config':estimator.get_params(),'train_indices':train.tolist(),'test_indices':test.tolist(),
            'train_split_sha256':digest(train.tolist()),'test_split_sha256':digest(test.tolist()),
            'train_distribution':{str(k):int(sum(y[train]==k)) for k in (0,1)},'test_distribution':{str(k):int(sum(y[test]==k)) for k in (0,1)},
            'fit_scope':'TRAIN_ONLY','scaler_mean':preprocessing.named_transformers_['numeric'].mean_.tolist(),
            'encoder_categories':[c.tolist() for c in preprocessing.named_transformers_['category'].categories_],
            'labels':y[test].tolist(),'predictions':predicted.tolist(),'probabilities':scores.tolist(),
            'training_ms':training_ms,'data_sha256':DATASET_SHA}
    if operation=='model_evaluator':
        trained=inputs['training_id'];truth=[int(rows[i]['Machine failure']) for i in trained['test_indices']]
        return {'model':trained['model'],'seed':trained['seed'],'data_sha256':DATASET_SHA,
            'training_id':params['training_id'],'train_split_sha256':trained['train_split_sha256'],
            'test_split_sha256':trained['test_split_sha256'],'training_ms':trained['training_ms'],
            'metrics':calculate(truth,trained['predictions'],trained['probabilities'])}
    if operation=='report_exporter':
        from .science_data import source
        return {'source':source(),'mode':'G4_OFFLINE','llm':'BLOCKED_NOT_AUTHORIZED','tokens':0,'cost':0,
                'models':[inputs[key] for key in ('dummy_id','logistic_id','forest_id')],
                'features':FEATURES,'excluded':EXCLUDED,'model_selection':'Fixed models/hyperparameters; test scores are descriptive, no test tuning.'}
    raise ValueError('unregistered_operation')


def main():
    raw=sys.stdin.buffer.read(1048577)
    if len(raw)>1048576:raise ValueError('input_size_limit')
    request=json.loads(raw)
    if request.get('operation')=='__TEST_ONLY_SLEEP' and request.get('test_only') is True:
        time.sleep(3);result={'test_label':'TEST_ONLY'}
    else:result=execute(request['operation'],request['params'],request['inputs'])
    sys.stdout.write(json.dumps({'ok':True,'payload':result,'worker_pid':os.getpid()},allow_nan=False))


if __name__=='__main__':
    try:main()
    except Exception:
        sys.stdout.write(json.dumps({'ok':False,'error':'science_worker_failed'}));sys.exit(1)
