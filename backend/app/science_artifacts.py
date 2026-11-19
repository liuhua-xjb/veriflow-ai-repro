"""Immutable JSON artifacts scoped to one run; no user-controlled filesystem paths."""
import json,re
from pathlib import Path
from uuid import uuid4
from .workflow_models import digest
from .event_store import utc
from .science_contracts import DATASET_SHA


class Artifacts:
    def __init__(self,root):self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)

    def directory(self,run_id):
        if not re.fullmatch(r'run_[a-f0-9]{32}',run_id):raise ValueError('invalid_run_scope')
        path=self.root/run_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):raise ValueError('artifact_path_rejected')
        return path

    def path(self,run_id,artifact_id):
        if not re.fullmatch(r'art_[a-f0-9]{32}',artifact_id):raise ValueError('invalid_artifact_id')
        path=self.directory(run_id)/(artifact_id+'.json')
        if path.is_symlink():raise ValueError('artifact_symlink_rejected')
        return path

    def save(self,run_id,node_id,attempt_id,tool,arguments,payload,execution):
        id_='art_'+uuid4().hex;path=self.path(run_id,id_);path.parent.mkdir(parents=True,exist_ok=True)
        record={'artifact_id':id_,'run_id':run_id,'node_id':node_id,'attempt_id':attempt_id,'tool':tool,'tool_version':'1.0.0',
            'schema_version':'1.0.0','timestamp':utc(),'data_sha256':DATASET_SHA,'input_digest':digest(arguments),'payload':payload,'execution':execution}
        record['artifact_digest']=digest(record)
        with path.open('x',encoding='utf-8') as f:json.dump(record,f,allow_nan=False)
        return {'artifact_id':id_,'artifact_digest':record['artifact_digest']}

    def load(self,run_id,id_):
        path=self.path(run_id,id_)
        if path.stat().st_size>2097152:raise ValueError('artifact_size_limit')
        record=json.loads(path.read_text(encoding='utf-8'))
        if record['run_id']!=run_id or record['artifact_id']!=id_ or record['artifact_digest']!=digest({k:v for k,v in record.items() if k!='artifact_digest'}):raise ValueError('artifact_integrity_failed')
        return record

    def list(self,run_id):
        directory=self.directory(run_id)
        if not directory.exists():return []
        return [self.load(run_id,p.stem) for p in sorted(directory.glob('art_*.json'))][:300]
