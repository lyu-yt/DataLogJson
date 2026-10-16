from pathlib import Path
import subprocess
import pytest

from experiment_logger.analysis import run_python
from experiment_logger.io import LoggerError, read_json


def test_analysis_snapshot_parameters_rerun_failure(store, draft, tmp_path):
    store.finalize(draft, source_complete=True)
    code = tmp_path / "code"
    code.mkdir()
    (code / "run.py").write_text('''import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
(Path(sys.argv[3])/"result.json").write_text(json.dumps({"result":p["value"]*2}))
print("synthetic analysis")
sys.exit(p.get("exit_code",0))
''')
    def git(*args):
        subprocess.run(["git", "-C", str(code), *args], check=True, capture_output=True)
    git("init")
    git("add", "run.py")
    git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@localhost", "commit", "-m", "Synthetic analysis")
    ids = [draft["record"]["record_id"]]
    first = run_python(store, code, "run.py", ids, {"value": 3}, trusted=True)
    second = run_python(store, code, "run.py", ids, {"value": 5, "exit_code": 2}, trusted=True)
    assert first["record_ids"] != second["record_ids"]
    r1 = store.read_record(first["record_ids"][0])
    r2 = store.read_record(second["record_ids"][0])
    assert r1["run"]["status"] == "success"
    assert r2["run"]["status"] == "failure"
    assert r1["run"]["parameters"] == {"value": 3}
    assert r1["run"]["environment"]["python"]
    (code / "run.py").write_text("raise Exception('modified original')")
    workspace = Path(store.roots["archive"]) / r1["run"]["working_directory"]["relative_path"]
    assert "modified original" not in (workspace / "code" / "run.py").read_text()
    assert read_json(workspace / "outputs" / "result.json") == {"result": 6}
    with pytest.raises(LoggerError, match="dirty"):
        run_python(store, code, "run.py", ids, {}, trusted=True)
