import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from experiment_logger.store import Store
from experiment_logger.io import hash_file, uid
from experiment_logger.models import inspect_csv, new_task, seal_record


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "source" / "261015" / "007" / "001.csv"
    path.parent.mkdir(parents=True)
    # SYNTHETIC: positional numeric format only; no scientific task semantics.
    path.write_text("\n".join("\t".join(map(str, [i / 10, i / 20, 0, 0, 0, 0, 0, 0, 0, i % 2, 3, 0, i]))
                              for i in range(40)) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "archive"
    s = Store(root / "metadata-json", {"archive": str(root)}, tmp_path / "local")
    s.initialize()
    return s


@pytest.fixture
def draft(store, sample):
    d = store.new(sample)
    d["record"].update(operator="Synthetic Tester", experiment_type="water linear push")
    d["record"]["metadata"].update(mouse_weight_g=25.1, training_day=12, is_imaging_session=False)
    return store.save_draft(d)


@pytest.fixture
def synthetic_task_record(sample):
    """Generate test metadata in memory; no acquisition/record files are shipped."""
    record = new_task(sample)
    record.update(operator="SYNTHETIC TEST", experiment_type="water linear push",
                  notes="SYNTHETIC: not an animal experiment")
    record["metadata"].update(mouse_weight_g=25.0, training_day=12, is_imaging_session=False)
    mapping, _ = inspect_csv(sample)
    record["files"] = [{"file_id": uid(), "role": "task_events", "format": "csv",
                        "media_type": "text/tab-separated-values", "original_filename": sample.name,
                        **hash_file(sample), "csv": mapping}]
    return seal_record(record, record["operator"])
