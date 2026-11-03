"""Read selected stimulation settings. No image decoding, MATLAB, or mask loading.

This module can be replaced independently of the UI and transfer service.
Values describe saved acquisition settings, not evidence of delivered pulses.
"""
import csv
import re
from pathlib import Path

from .io import Cancelled, LoggerError, signature

PARSER_VERSION = "1.0.0"
TEXT_PARAMETERS = frozenset({"comments", "mat_comments", "laser_power_text", "mat_laser_power_text",
                             "pipeline_version", "parameters_saved_at_local", "acquisition_number"})
# Original MATLAB field names remain explicit provenance, not JSON key names.
MAT_FIELDS = (
    ("pulse_parameter_sets", "stimPulseParams.nParamSets", None),
    ("instrument_power_percent", "stimPulseParams.power", "percent"),
    ("spiral_size", "stimPulseParams.spiralSize", "scanimage_unit"),
    ("pulse_duration_s", "stimPulseParams.durationStim", "s"),
    ("pause_duration_s", "stimPulseParams.durationPause", "s"),
    ("pulse_count", "stimPulseParams.pulsen", None),
    ("continuous_spiral", "stimPulseParams.continuousSpiral", None),
    ("spiral_count", "stimPulseParams.spiraln", None),
    ("pattern_count", "stimPatternParams.nPatterns", None),
    ("configured_stim_locations", "stimPatternParams.nStimLoc", None),
    ("stim_locations_is_array", "stimPatternParams.nStimLocIsArray", None),
    ("sequence_repeats", "stimSequenceParams.nSeqRepeat", None),
    ("initial_padding", "stimSequenceParams.nPadInit", None),
    ("parameter_sequence", "stimSequenceParams.StimSeq", None),
    ("raspberry_pi_sequence", "stimSequenceParams.StimSeqRaspPi", None),
    ("generated_sequence", "data.stimSeqSet", None),
    ("available_target_count", "data.nNeuronCorrected", None),
    ("activity_weighted", "options.useActivityWeights", None),
    ("pipeline_version", "version", None),
    ("parameters_saved_at_local", "outputs.savedAt", None),
    ("acquisition_number", "outputs.fileNumberStr", None),
)


def mouse_number(value):
    text = str(value).strip() if value is not None else ""
    match = re.fullmatch(r"(?:mouse_?)?([0-9]{3})(?:\.0)?", text, re.I)
    return match.group(1) if match else None


def _power(text):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:mW)?\s*", str(text), re.I)
    return float(match.group(1)) if match else None


def _h5_value(file, path, *, depth=0):
    """Bounded MATLAB v7.3 scalar/vector/matrix and scalar-reference reader."""
    import h5py
    import numpy as np
    if path not in file:
        return None
    node = file[path]
    if not isinstance(node, h5py.Dataset) or node.size > 100000 or depth > 8:
        raise LoggerError(f"Unsupported or oversized parameter field: {path}")
    if node.attrs.get("MATLAB_empty", 0):
        return None
    value = node[()]
    if h5py.check_dtype(ref=node.dtype):
        if value.size != 1:
            raise LoggerError(f"Expected a scalar MATLAB reference: {path}")
        ref = value.flat[0]
        return _h5_value(file, file[ref].name, depth=depth+1) if ref else None
    matlab_class = node.attrs.get("MATLAB_class", b"")
    if isinstance(matlab_class, bytes):
        matlab_class = matlab_class.decode("ascii", errors="replace")
    if matlab_class == "char":
        return value.astype("<u2").tobytes(order="F").decode("utf-16-le").rstrip("\0")
    if value.dtype.kind not in "biuf":
        raise LoggerError(f"Unsupported parameter type: {path}")
    if not np.isfinite(value).all():
        raise LoggerError(f"Nonfinite parameter value: {path}")
    if matlab_class == "logical":
        value = value.astype(bool)
    if not value.size:
        return None
    # HDF5 reverses MATLAB dimensions. Keep genuine matrices and flatten vectors.
    value = value.T
    if value.size == 1:
        return value.item()
    return value.reshape(-1).tolist() if value.ndim <= 1 or 1 in value.shape else value.tolist()


def extract_stim_parameters(csv_path=None, mat_path=None, *, cancel=None):
    """Return plain JSON settings with source fields and private stability stamps.

    Accepts the supplied v7.3 MAT layout. Unsupported MAT versions fail visibly.
    Does not hash entire recordings or read any ScanImage object/mask datasets.
    """
    parameters, identities, warnings, sources, stamps = [], [], [], [], {}

    def add(name, value, unit, role, field):
        parameters.append(dict(name=name, value=value, unit=unit,
                               source_role=role, source_field=field))

    def stop():
        if cancel and cancel():
            raise Cancelled("Stimulation parameter extraction cancelled")

    csv_mouse = mat_mouse = None
    for role, supplied in (("parameters_csv", csv_path), ("parameters_mat", mat_path)):
        if not supplied:
            continue
        stop()
        path = Path(supplied).resolve()
        before = signature(path)
        if role == "parameters_csv":
            if path.stat().st_size > 1024 * 1024:
                raise LoggerError("Parameter CSV is unexpectedly large; select stim_parameters.csv")
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                headers = reader.fieldnames or []
                if len(headers) != len(set(headers)) or not {"stim laser power (mW)", "mouse #", "comments"}.issubset(headers):
                    raise LoggerError("Parameter CSV needs unique columns: stim laser power (mW), mouse #, comments")
                rows = [row for row in reader if any(str(v or '').strip() for v in row.values())]
            if len(rows) != 1:
                raise LoggerError("Select a parameter CSV with exactly one experiment row")
            row = rows[0]
            if None in row:
                raise LoggerError("Parameter CSV row has extra columns; check its quoting")
            raw_power = (row["stim laser power (mW)"] or "").strip()
            power = _power(raw_power)
            if raw_power and power is None:
                warnings.append("CSV laser power could not be converted to mW; review its original text.")
            add("laser_power_mw", power, "mW", role, "stim laser power (mW)")
            add("laser_power_text", raw_power or None, None, role, "stim laser power (mW)")
            add("comments", row["comments"] or None, None, role, "comments")
            csv_mouse = mouse_number(row["mouse #"])
            if (row["mouse #"] or "").strip() and not csv_mouse:
                warnings.append("CSV mouse ID is not a three-digit ID; select the mouse manually.")
            identities.append({"source_role": role, "mouse_id": csv_mouse})
        else:
            try:
                import h5py
            except ImportError as exc:
                raise LoggerError("Install h5py in the datalogjson environment to read stimulation MAT files") from exc
            if not h5py.is_hdf5(path):
                raise LoggerError("This MAT file is not MATLAB v7.3/HDF5; save it with -v7.3 before importing")
            with h5py.File(path, "r") as file:
                if "session/stimPulseParams" not in file:
                    raise LoggerError("MAT file has no session.stimPulseParams settings")
                for name, field, unit in MAT_FIELDS:
                    stop()
                    value = _h5_value(file, "session/" + field.replace(".", "/"))
                    add(name, value, unit, role, "session." + field)
                mat_mouse = mouse_number(_h5_value(file, "session/experimentParams/mouseNumber"))
                identities.append({"source_role": role, "mouse_id": mat_mouse})
                add("mat_laser_power_text", _h5_value(file, "session/experimentParams/stimLaserPowerMw"),
                    None, role, "session.experimentParams.stimLaserPowerMw")
                add("mat_comments", _h5_value(file, "session/experimentParams/comments"),
                    None, role, "session.experimentParams.comments")
                for field in ("paths/saveDir", "outputs/parametersCsvPath"):
                    text = _h5_value(file, "session/" + field)
                    ids = [mouse_number(part) for part in re.split(r"[/\\]", text if isinstance(text, str) else "")]
                    for found in dict.fromkeys(i for i in ids if i):
                        if (csv_mouse or mat_mouse) and found != (csv_mouse or mat_mouse):
                            warnings.append(f"MAT {field.replace('/', '.')} refers to mouse {found}; parameter identity is {csv_mouse or mat_mouse}. Saved paths do not set the mouse ID.")
        if signature(path) != before:
            raise LoggerError(f"Parameter file changed during extraction: {path.name}")
        stamps[str(path)] = list(before)
        sources.append({"role": role, "original_filename": path.name, "file_id": None})
    if csv_mouse and mat_mouse and csv_mouse != mat_mouse:
        warnings.append(f"CSV mouse {csv_mouse} differs from MAT mouse {mat_mouse}; choose the correct identity.")
    priority = ("laser_power_mw", "instrument_power_percent", "pulse_duration_s", "pause_duration_s",
                "pulse_count", "spiral_size", "spiral_count", "continuous_spiral", "pattern_count",
                "configured_stim_locations", "sequence_repeats", "initial_padding")
    parameters.sort(key=lambda p: priority.index(p["name"]) if p["name"] in priority else len(priority))
    return {"report": {"parser_version": PARSER_VERSION, "parameters": parameters,
                       "identity_candidates": identities, "sources": sources, "warnings": warnings},
            "signatures": stamps}
