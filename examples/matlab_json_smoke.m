% MATLAB smoke test using synthetic fixtures, no real acquisitions.
% Histology checks are unexecuted: the local R2023a license is unavailable.
% Generate a local demo with create_demo.py, then set LOGGER_JSON_FIXTURE to
% a generated task, expression, imaging, stim or histology JSON. Fixtures stay out of Git.
fixture = getenv('LOGGER_JSON_FIXTURE');
if isempty(fixture)
    fixture = fullfile(fileparts(mfilename('fullpath')), 'synthetic_task_finalized.json');
end
assert(isfile(fixture), 'Set LOGGER_JSON_FIXTURE to a generated synthetic JSON file.');
r = jsondecode(fileread(fixture));
assert(strcmp(r.record_type, 'data'));
assert(strcmp(r.record_state, 'finalized'));
assert(strcmp(r.mouse_id, '007')); % string, leading zeros preserved
if strcmp(r.data_type, 'histology')
    assert(strcmp(r.experiment_type, 'histology'));
    assert(r.metadata.reviewed);
    assert(strcmp(r.metadata.slice_index, '3-1C'));
    if isempty(r.metadata.linked_record)
        assert(isfield(r.metadata.colony_profile, 'profile_id'));
        assert(strcmp(r.metadata.colony_profile.mouse_id, r.mouse_id));
        assert(r.metadata.colony_profile.revision >= 1);
        assert(strcmp(r.metadata.colony_profile.snapshot.profile_id, r.metadata.colony_profile.profile_id));
    else
        assert(isfield(r.metadata.linked_record, 'record_id'));
    end
    assert(~isempty(r.metadata.images));
    assert(isfield(r.metadata.images(1).czi, 'bounds'));
    assert(~isempty(r.files));
elseif strcmp(r.data_type, 'stim')
    assert(strcmp(r.experiment_type, 'stimulation'));
    assert(r.metadata.reviewed);
    assert(islogical(r.metadata.behavior_session));
    assert(isfield(r.metadata.extraction, 'parameters'));
    assert(isfield(r.metadata.extraction, 'sources'));
    assert(numel(r.files) >= 4);
    p = r.metadata.extraction.parameters;
    spiral = p(strcmp({p.name}, 'spiral_size'));
    assert(strcmp(spiral.unit, 'scanimage_unit'));
    assert(all(spiral.value(:) == [3; 3]));
elseif strcmp(r.data_type, 'imaging')
    assert(strcmp(r.experiment_type, 'in_vivo_imaging'));
    assert(islogical(r.metadata.behavior_session));
    assert(isfield(r.metadata.sides, 'left') && isfield(r.metadata.sides, 'right'));
    assert(isfield(r.metadata.sides.left, 'laser_power'));
    assert(islogical(r.metadata.sides.left.channels.green));
    assert(~isempty(r.files));
    assert(all(arrayfun(@(f) strcmp(f.format, 'tiff'), r.files)));
elseif strcmp(r.data_type, 'expression')
    assert(strcmp(r.experiment_type, 'expression_check'));
    channels = {'left_red', 'left_green', 'right', 'other'};
    for k = 1:numel(channels)
        assert(isfield(r.metadata.channels, channels{k}));
        assert(islogical(r.metadata.channels.(channels{k}).selected));
        assert(isfield(r.metadata.channels.(channels{k}), 'file_ids'));
    end
    assert(~isempty(r.files));
    assert(all(arrayfun(@(f) strcmp(f.format, 'tiff'), r.files)));
    assert(all(arrayfun(@(f) strcmp(f.media_type, 'image/tiff'), r.files)));
else
    assert(strcmp(r.data_type, 'task'));
    assert(r.metadata.training_day == 12);
    assert(r.metadata.mouse_weight_g == 25);
    assert(r.metadata.is_imaging_session == false);
    assert(r.files(1).csv.columns(1).source_index == 1);
    assert(strcmp(r.files(1).csv.columns(1).semantic_name, 'pos_x'));
    assert(isempty(r.files(1).csv.time_reference.sample_interval_s)); % JSON null
    assert(isempty(r.acquisition.started_at));
end
fprintf('Plain JSON compatibility smoke test passed (synthetic %s fixture).\n', r.data_type);
