import numpy as np
import pytest
from PIL import Image

from experiments.subject_relief.compare import PRESETS, augmentations, fuse_predictions, resize_prediction, unique_views


def test_four_presets_increase_real_work_without_identical_repeats():
    assert [(p['level'], p['side'], p['passes']) for p in PRESETS] == [(0, 256, 1), (5, 512, 2), (7, 768, 4), (9, 1536, 8)]
    for preset in PRESETS:
        views = augmentations(preset['passes'])
        assert len(views) == len(set(views)) == preset['passes']
        assert views[0] == (1, False)


def test_single_prediction_is_not_renormalized_or_numerically_changed():
    depth = np.linspace(0.7, 4, 40, dtype=np.float32).reshape(5, 8)
    valid = np.ones(depth.shape, dtype=bool)
    fused, supported, _ = fuse_predictions([(depth, valid)])
    np.testing.assert_array_equal(fused, depth)
    np.testing.assert_array_equal(supported, valid)


def test_small_source_does_not_count_identical_scaled_inputs_as_extra_work():
    pixels = np.zeros((40, 60, 3), dtype=np.uint8)
    pixels[:, :20] = [240, 30, 20]
    views = list(unique_views(Image.fromarray(pixels), PRESETS[-1]))
    assert len(views) == 2
    assert views[0][0].shape == views[1][0].shape == pixels.shape
    np.testing.assert_array_equal(views[1][0], views[0][0][:, ::-1])


def test_symmetric_small_source_also_deduplicates_identical_mirrors():
    assert len(list(unique_views(Image.new('RGB', (60, 40), 'white'), PRESETS[-1]))) == 1


def test_global_scale_alignment_preserves_whole_image_geometry():
    inverse = np.linspace(0.25, 2, 60, dtype=np.float32).reshape(6, 10)
    valid = np.ones(inverse.shape, dtype=bool)
    fused, supported, transforms = fuse_predictions([(1 / inverse, valid), (1 / (inverse * 2 + 0.3), valid)])
    np.testing.assert_allclose(fused, 1 / inverse, rtol=2e-6)
    assert supported.all()
    assert transforms[1]['scale'] == pytest.approx(0.5, rel=1e-6)


def test_missing_support_cannot_become_fabricated_depth():
    depth = np.linspace(1, 4, 100, dtype=np.float32).reshape(10, 10)
    valid = np.ones(depth.shape, dtype=bool)
    valid[:2, :2] = False
    depth[:2, :2] = np.inf
    fused, supported, _ = fuse_predictions([(depth, valid)] * 4)
    assert not supported[:2, :2].any()
    assert np.all(fused[:2, :2] == 0)
    assert np.isfinite(fused).all()


def test_resize_ignores_infinite_invalid_samples():
    depth = np.ones((4, 4), dtype=np.float32) * 2
    valid = np.ones(depth.shape, dtype=bool)
    depth[0, 0], valid[0, 0] = np.inf, False
    resized, supported = resize_prediction(depth, valid, (8, 8))
    assert np.isfinite(resized).all()
    np.testing.assert_allclose(resized[supported], 2)


def test_flat_augmented_prediction_fails_explicitly():
    depth = np.arange(1, 26, dtype=np.float32).reshape(5, 5)
    valid = np.ones(depth.shape, dtype=bool)
    with pytest.raises(ValueError, match='flat'):
        fuse_predictions([(depth, valid), (np.ones(depth.shape), valid)])
