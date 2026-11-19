"""Reproducible project file integrity preflight; no network or code imports."""
import hashlib,json,pathlib,ast
P=pathlib.Path(__file__).resolve().parent
data=json.loads((P/"FILE_SHA256_MANIFEST.json").read_text(encoding="utf-8"))
for relative,h in data["files"].items():
    f=P/relative
    assert f.is_file() and f.resolve().is_relative_to(P.resolve())
    assert hashlib.sha256(f.read_bytes()).hexdigest()==h,relative
    if f.suffix==".py":ast.parse(f.read_text(encoding="utf-8-sig"),filename=relative)
for f in P.rglob("*"):
    if f.is_file():
        rel=f.relative_to(P).as_posix()
        assert not any(part in rel.split("/") for part in ("node_modules",".git",".venv","runtime","repro_evidence",".cache","__pycache__"))
        assert not f.name.startswith(".env")
        assert f.suffix.lower() not in (".sqlite3",".db",".key",".pfx",".pem")
print("SOURCE_AUDIT_PASS",len(data["files"]))
