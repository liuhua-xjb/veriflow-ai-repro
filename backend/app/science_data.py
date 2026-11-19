"""Read-only, pinned public synthetic data. Exact feature allowlist."""
import csv,hashlib,io,json
from pathlib import Path
from .science_contracts import DATASET_SHA,FEATURES,EXCLUDED

ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT/'data/ai4i2020'


def load_data():
    path=DATA/'ai4i2020.csv'
    if path.is_symlink() or not path.resolve().is_relative_to(DATA.resolve()):raise ValueError('dataset_path_rejected')
    if path.stat().st_size>1048576:raise ValueError('dataset_size_limit')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=DATASET_SHA:raise ValueError('dataset_integrity_failed')
    rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    if len(rows)!=10000 or set(rows[0])!=set(FEATURES+EXCLUDED):raise ValueError('dataset_schema_failed')
    return rows


def source():
    record=json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
    if record['sha256']!=DATASET_SHA or record['synthetic'] is not True:raise ValueError('provenance_failed')
    return record


def profile(rows):
    return {'rows':len(rows),'columns':list(rows[0]),'missing':{k:sum(r[k]=='' for r in rows) for k in rows[0]},
            'class_distribution':{str(k):sum(int(r['Machine failure'])==k for r in rows) for k in (0,1)},
            'target':'Machine failure','features':FEATURES,'excluded':EXCLUDED,'synthetic':True,'data_sha256':DATASET_SHA}


def approved_features(features):
    # Exact order and exact set prevents aliases, newly derived target columns or IDs.
    return features==FEATURES
