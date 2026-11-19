"""Run real HTTP live E2E smoke tests against locally started VeriFlow app.

The services must already be running at localhost 18000/15173.
Does not open external network, require tokens, or execute real LLM.
"""
import json,pathlib,time,urllib.error,urllib.request
from datetime import datetime,timezone
ROOT=pathlib.Path(__file__).resolve().parent
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE="http://127.0.0.1:18000"
def api(path,body=None):
    kw={"timeout":10}
    if body is None:
        request=urllib.request.Request(BASE+path,method="GET")
    else:
        request=urllib.request.Request(BASE+path,method="POST",
            headers={"Content-Type":"application/json"},
            data=json.dumps(body).encode("utf-8"))
    with opener.open(request,**kw) as response:
        data=response.read(2_000_000)
        return response.status,json.loads(data.decode("utf-8"))
def poll(run_id,limit=70):
    until=time.monotonic()+limit
    while time.monotonic()<until:
        status,result=api("/api/science/runs/"+run_id)
        item=result["run"]
        if item["state"] in ("SUCCEEDED","FAILED","CANCELLED"):
            return {"HTTP":status,"state":item["state"],"verification":item.get("verification_status"),
               "node_states":{k:v.get("state") for k,v in item.get("nodes",{}).items()},
               "report_links":sorted(result.get("downloads",{}).keys())}
        time.sleep(1)
    raise RuntimeError("run_poll_timeout")
def main():
    report={"schema":"VERIFLOW_REAL_LOCAL_HTTP_REPRO_V1","utc":datetime.now(timezone.utc).isoformat(),
       "source_mode":"staged_original_source_not_legacy_replay","internet_requests":0,"model_calls_requested":0,
       "external_provider_default_deny":True}
    status,health=api("/api/health")
    assert status==200 and health["status"]=="ok"
    report["health"]=True
    with opener.open("http://127.0.0.1:15173/",timeout=10) as res:
        index=res.read(256000)
        assert res.status==200 and b"<html" in index.lower()
    report["frontend_http"]=True
    status,dataset=api("/api/science/source")
    assert status==200 and dataset["profile"]["rows"]==10000
    report["dataset_rows"]=10000
    for label,features in (("positive","safe"),("leakage","leakage_negative")):
        status,created=api("/api/science/demos",{
           "seed":42,"strategy":"dynamic","verification":"verify_and_repair",
           "feature_set":features,"reasoning":"offline"})
        assert status==202 and isinstance(created.get("run_id"),str)
        report[label]=poll(created["run_id"])
    pos=report["positive"]; neg=report["leakage"]
    assert pos["state"]=="SUCCEEDED" and pos["verification"]=="VERIFIED"
    assert len([v for v in pos["node_states"].values() if v=="SUCCEEDED"])==9
    assert pos["report_links"]==["csv","html","json"]
    assert neg["state"]=="FAILED" and neg["verification"]=="REJECTED"
    assert neg["node_states"].get("leakage")=="FAILED"
    assert all(neg["node_states"][k]=="SKIPPED" for k in ("train_dummy","train_logistic","train_random_forest","eval_dummy","eval_logistic","eval_random_forest","export"))
    report["result"]="PASS_REAL_LOCAL_HTTP_UI_AND_SCIENCE"
    dest=ROOT/"repro_evidence"
    dest.mkdir(exist_ok=True)
    (dest/"HTTP_REPRO_REPORT.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("REAL_HTTP_REPRO_PASS, positive=SUCCEEDED/VERIFIED 9 nodes, leakage=FAILED/REJECTED",flush=True)
if __name__=="__main__":
    main()
