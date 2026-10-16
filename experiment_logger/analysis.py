"""Explicit, trusted Python analysis execution; never invoked by record viewing."""
from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

from .io import LoggerError, Cancelled, atomic_json, digest, hash_file, now, read_json, resolve, uid, lock
from .models import envelope, seal_record


def run_python(store, code_repo, entry_point, input_ids, parameters, *, trusted=False, seed=None, cancel=None):
    if not trusted:
        raise LoggerError("Only explicitly selected, trusted analysis entry points may run; pass trusted=True")
    code_repo = Path(code_repo).resolve()
    def git(*args):
        p = subprocess.run(["git", "-C", str(code_repo), *args], capture_output=True, text=True)
        if p.returncode:
            raise LoggerError(p.stderr)
        return p.stdout.strip()
    if git("status", "--porcelain", "--untracked-files=all"):
        raise LoggerError("Analysis code tree is dirty or contains untracked helpers")
    tracked = git("ls-files", "--stage")
    if any(line.startswith(("160000 ", "120000 ")) for line in tracked.splitlines()):
        raise LoggerError("Submodules and symbolic-link helpers require an explicit capture strategy; unsupported in this runner")
    commit = git("rev-parse", "HEAD")
    rid = uid()
    root = Path(store.roots["archive"])
    run_relative = f"runs/{rid}"
    workspace = resolve(root, run_relative)
    workspace.mkdir(parents=True)
    snapshot = workspace / "code.zip"
    git("archive", "--format=zip", f"--output={snapshot}", commit)
    code = workspace / "code"
    code.mkdir()
    with zipfile.ZipFile(snapshot) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            target = resolve(code, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    script = resolve(code, entry_point)
    if not script.is_file() or script.suffix != ".py":
        raise LoggerError("Entry point must be a tracked Python file inside the captured code tree")
    inputs_dir = workspace / "inputs"
    inputs_dir.mkdir()
    outputs = workspace / "outputs"
    outputs.mkdir()
    inputs = []
    file_inputs = []
    for input_id in input_ids:
        record = store.read_record(input_id)
        inputs.append({"record_id": input_id, "sha256": store.index()[input_id]["sha256"]})
        for f in record.get("files", []):
            candidates = store.file_locations(f["file_id"])
            valid = None
            for candidate in candidates:
                if candidate["available"] and hash_file(candidate["path"]) == {k: f[k] for k in ("sha256", "size_bytes")}:
                    valid = candidate["path"]
                    break
            if valid is None:
                raise LoggerError(f"Missing or changed input file {f['file_id']}")
            dest = inputs_dir / f["file_id"]
            shutil.copyfile(valid, dest)
            if hash_file(dest) != {k: f[k] for k in ("sha256", "size_bytes")}:
                raise LoggerError("Input changed during snapshot copy")
            file_inputs.append({"file_id": f["file_id"], "sha256": f["sha256"],
                                "snapshot_path": f"inputs/{f['file_id']}"})
    # The contract for the user's wrapper is three arguments: parameters, inputs, outputs.
    atomic_json(workspace / "parameters.json", parameters)
    atomic_json(workspace / "input_manifest.json", {"records": inputs, "files": file_inputs})
    command = [sys.executable, f"code/{entry_point}", "parameters.json", "input_manifest.json", "outputs"]
    r = envelope("analysis_run")
    r["record_id"] = rid
    r["operator"] = "analysis-runner"
    r["run"] = {"analysis_name": entry_point, "inputs": inputs, "input_files": file_inputs,
                "upstream_run_ids": [x for x in input_ids if store.read_record(x)["record_type"] == "analysis_run"],
                "command": command, "entry_point": entry_point, "working_directory": {"root_id": "archive", "relative_path": run_relative},
                "parameters": parameters, "seed": seed, "code_commit": commit,
                "code_snapshot_sha256": hash_file(snapshot)["sha256"], "status": "running",
                "started_at": now(), "ended_at": None, "return_code": None,
                "environment": {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                                "packages": sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()),
                                "external_helpers": [], "external_helpers_policy": "not captured; wrapper must not depend on uncaptured local helpers"},
                "metrics": {}, "execution_log_file_id": None}
    # Machine-local executable paths are retained only in analysis provenance as needed
    # for reproduction; data locations are still root-relative.
    journal = store.local / "runs" / f"{rid}.json"
    atomic_json(journal, r)
    log_path = workspace / "execution.log"
    with log_path.open("wb") as log:
        try:
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
            proc = subprocess.Popen(command, cwd=workspace, stdout=log, stderr=subprocess.STDOUT, env=env)
            interrupted = False
            while True:
                try:
                    returncode = proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel and cancel():
                        interrupted = True
                        proc.terminate()
                        try:
                            returncode = proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            returncode = proc.wait()
                        break
            r["run"].update(status="interrupted" if interrupted else "success" if returncode == 0 else "failure", return_code=returncode)
        except Exception as exc:
            log.write(str(exc).encode("utf-8"))
            r["run"]["status"] = "failure"
    r["run"]["ended_at"] = now()
    atomic_json(journal, r)
    return _publish_run(store, r, workspace, journal)


def _publish_run(store, r, workspace, journal):
    entries = []
    r["files"] = []
    files = [(workspace / "code.zip", "code_snapshot"), (workspace / "execution.log", "execution_log")]
    files.extend((p, "analysis_output") for p in sorted((workspace / "outputs").rglob("*")) if p.is_file())
    for path, role in files:
        relative = path.relative_to(Path(store.roots["archive"])).as_posix()
        resolve(store.roots["archive"], relative)
        f = {"file_id": uid(), "role": role, "format": path.suffix.lstrip(".") or "binary",
             "media_type": "application/octet-stream", "original_filename": path.name, **hash_file(path)}
        r["files"].append(f)
        if role == "execution_log":
            r["run"]["execution_log_file_id"] = f["file_id"]
        entries.append({**f, "locations": [{"root_id": "archive", "relative_path": relative,
                                           "verified_at": now(), "verification_status": "verified"}]})
    r = seal_record(r, r["operator"])
    with lock(store.metadata):
        receipt = store._publish([r], entries, r["operator"])
    atomic_json(journal, r)
    return receipt


def recover_interrupted(store, run_id):
    """Explicit operator recovery only: ensure the old process has stopped first."""
    journal = store._path(run_id, store.local / "runs")
    r = read_json(journal)
    if run_id in store.index():
        raise LoggerError("Run already finalized")
    r["run"].update(status="interrupted", ended_at=now())
    workspace = resolve(store.roots["archive"], r["run"]["working_directory"]["relative_path"])
    (workspace / "execution.log").touch(exist_ok=True)
    return _publish_run(store, r, workspace, journal)
