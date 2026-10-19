import pytest

from experiments.subject_relief.lab_config import DEFAULTS, config_payload, validate_boxes, validate_parameters


def test_presets_and_editable_fields_use_the_same_validated_contract():
    config = config_payload()
    assert {field['key'] for field in config['fields']} == set(DEFAULTS)
    for preset in config['presets']:
        assert validate_parameters(preset['parameters']) == preset['parameters']
    config['defaults']['scales'].append(0.5)
    assert DEFAULTS['scales'] == [1]
    assert all(field['help'] and field['effect'] for field in config['fields'])


def test_main_branch_preset_matches_the_production_worker_settings():
    main = next(preset for preset in config_payload()['presets'] if preset['id'] == 'main')['parameters']
    assert {key: main[key] for key in ('depth_level', 'input_side', 'grid_side', 'mirror', 'scales',
                                       'background_flatten', 'amplitude', 'low_percentile', 'high_percentile')} == {
        'depth_level': 0, 'input_side': 256, 'grid_side': 256, 'mirror': False, 'scales': [1.0],
        'background_flatten': 0.0, 'amplitude': 0.32, 'low_percentile': 2.0, 'high_percentile': 98.0,
    }


@pytest.mark.parametrize('parameters', [
    {'depth_level': 10}, {'depth_level': True}, {'threads': 0}, {'input_side': 9999},
    {'amplitude': float('nan')}, {'scales': [1, 1]}, {'scales': [0.5]},
    {'scales': [1, 0.1]}, {'scales': [True]}, {'mirror': 'true'}, {'fusion': 'invented'},
    {'path': 'C:/secret'}, {'mask_mode': 'shell'}, {'grid_side': 512.0},
])
def test_invalid_or_unknown_parameters_are_not_silently_coerced(parameters):
    with pytest.raises(ValueError):
        validate_parameters(parameters)


def test_scale_order_is_canonical_and_zero_filtering_is_explicitly_supported():
    parameters = validate_parameters({'scales': [0.75, 1, 0.5], 'min_area_fraction': 0, 'background_flatten': 0})
    assert parameters['scales'] == [1, 0.75, 0.5]
    assert parameters['min_area_fraction'] == parameters['background_flatten'] == 0


@pytest.mark.parametrize('boxes', [
    [[-0.1, 0, 1, 1]], [[0, 0, 1.1, 1]], [[0.5, 0, 0.5, 1]],
    [[0, 0, float('nan'), 1]], [[0, 0, 1]], [[False, 0, 1, 1]], [[0, 0, 1, 1]] * 9,
])
def test_invalid_normalized_boxes_are_rejected(boxes):
    with pytest.raises(ValueError):
        validate_boxes(boxes)


def test_box_coordinates_stay_normalized_and_are_copied():
    boxes = [[0.2, 0.3, 0.5, 0.8]]
    normalized = validate_boxes(boxes)
    normalized[0][0] = 0
    assert boxes[0][0] == 0.2
