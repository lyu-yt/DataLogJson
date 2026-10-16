% MATLAB smoke test. Verified with R2023a; synthetic fixture, no real acquisitions.
% Generate a local demo with create_demo.py, then set LOGGER_JSON_FIXTURE to
% the generated task.json path. Generated records are not stored in this repo.
fixture = getenv('LOGGER_JSON_FIXTURE');
if isempty(fixture)
    fixture = fullfile(fileparts(mfilename('fullpath')), 'synthetic_task_finalized.json');
end
assert(isfile(fixture), 'Set LOGGER_JSON_FIXTURE to a generated synthetic task.json file.');
r = jsondecode(fileread(fixture));
assert(strcmp(r.record_type, 'data'));
assert(strcmp(r.record_state, 'finalized'));
assert(strcmp(r.mouse_id, '007')); % string, leading zeros preserved
assert(r.metadata.training_day == 12);
assert(r.metadata.mouse_weight_g == 25);
assert(r.metadata.is_imaging_session == false);
assert(r.files(1).csv.columns(1).source_index == 1);
assert(strcmp(r.files(1).csv.columns(1).semantic_name, 'pos_x'));
assert(isempty(r.files(1).csv.time_reference.sample_interval_s)); % JSON null
assert(isempty(r.acquisition.started_at));
disp('Plain JSON compatibility smoke test passed (synthetic fixture).');
