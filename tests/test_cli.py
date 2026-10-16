from experiment_logger.cli import main
from experiment_logger.io import atomic_json, loads, read_json
from experiment_logger.models import validate
from pathlib import Path


def test_cli_draft_and_browse(store, sample, tmp_path, capsys):
    config = tmp_path / "cli" / "config.json"
    atomic_json(config, {"roots": store.roots, "operator": "Synthetic CLI"})
    assert main(["--config", str(config), "draft", str(sample)]) == 0
    d = loads(capsys.readouterr().out)
    assert d["revision"] == 1 and d["record"]["mouse_id"] == "007"
    assert main(["--config", str(config), "browse"]) == 0
    assert loads(capsys.readouterr().out) == {}


def test_matlab_fixture_schema_and_seal(store, synthetic_task_record, tmp_path):
    from experiment_logger.io import digest
    path = tmp_path / "synthetic_task_finalized.json"
    atomic_json(path, synthetic_task_record)
    seal = store._seal(synthetic_task_record, path.read_bytes())
    assert digest(path.read_bytes()) == seal["sha256"]
    assert not validate(read_json(path), True)
