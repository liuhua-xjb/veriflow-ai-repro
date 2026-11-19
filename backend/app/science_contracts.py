"""Closed G4 tool contracts; no paths, URLs, code or open model parameters."""
from typing import Annotated, Literal
from pydantic import Field
from .contracts import StrictModel

FEATURES=['Type','Air temperature [K]','Process temperature [K]','Rotational speed [rpm]','Torque [Nm]','Tool wear [min]']
EXCLUDED=['UDI','Product ID','Machine failure','TWF','HDF','PWF','OSF','RNF']
DATASET_SHA='dc6630cd9b1f0f853922fad78a1b6436570d3f1ec863f1dd5c4340ac56bc8a8e'
SEEDS=(42,43,44,45,46)
ArtifactID=Annotated[str,Field(pattern=r'^art_[a-f0-9]{32}$')]
Column=Literal['Type','Air temperature [K]','Process temperature [K]','Rotational speed [rpm]','Torque [Nm]',
               'Tool wear [min]','UDI','Product ID','Machine failure','TWF','HDF','PWF','OSF','RNF']


class DataProfiler(StrictModel):
    dataset: Literal['ai4i2020']


class LeakageCheck(StrictModel):
    profile_id: ArtifactID
    features: list[Column] = Field(min_length=1,max_length=14)


class ModelTrainer(StrictModel):
    leakage_id: ArtifactID
    model: Literal['dummy','logistic','random_forest']
    seed: Literal[42,43,44,45,46]


class ModelEvaluator(StrictModel):
    training_id: ArtifactID


class ReportExporter(StrictModel):
    dummy_id: ArtifactID
    logistic_id: ArtifactID
    forest_id: ArtifactID


class ArtifactOutput(StrictModel):
    artifact_id: ArtifactID
    artifact_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


CONTRACTS={'data_profiler':DataProfiler,'leakage_check':LeakageCheck,'model_trainer':ModelTrainer,
           'model_evaluator':ModelEvaluator,'report_exporter':ReportExporter}
TOOL_TYPES={
 'data_profiler':({'dataset':'string'},{'artifact_id':'string','artifact_digest':'string'}),
 'leakage_check':({'profile_id':'string','features':'strings'},{'artifact_id':'string','artifact_digest':'string'}),
 'model_trainer':({'leakage_id':'string','model':'string','seed':'integer'},{'artifact_id':'string','artifact_digest':'string'}),
 'model_evaluator':({'training_id':'string'},{'artifact_id':'string','artifact_digest':'string'}),
 'report_exporter':({'dummy_id':'string','logistic_id':'string','forest_id':'string'},{'artifact_id':'string','artifact_digest':'string'})}
