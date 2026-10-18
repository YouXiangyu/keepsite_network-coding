import numpy as np
import pytest

from experiments.subject_relief.geometry import cell_patch_arrays, soften_subject_edges, subject_heights


def test_near_subject_is_higher_without_using_bright_pixels_or_background_depth():
    depth = np.array([[0.01, 0.01, 0.01, 0.01], [0.01, 1, 2, 0.01], [0.01, 3, 4, 0.01], [0.01] * 4])
    mask = np.zeros((4, 4), dtype=bool)
    mask[1:3, 1:3] = True
    heights, stats = subject_heights(depth, np.ones_like(mask), mask)
    assert np.all(heights[~mask] == 0)
    assert heights[1, 1] > heights[1, 2] > heights[2, 1] > heights[2, 2] > 0
    depth[~mask] = 1e20
    changed, _ = subject_heights(depth, np.ones_like(mask), mask)
    np.testing.assert_array_equal(changed, heights)
    assert stats['valid_fraction'] == 1


def test_invalid_subject_samples_are_filled_without_becoming_background():
    depth = np.arange(1, 17, dtype=np.float32).reshape(4, 4)
    depth[1, 1], depth[1, 2] = np.inf, np.nan
    mask = np.ones((4, 4), dtype=bool)
    heights, stats = subject_heights(depth, mask, mask, lift=0.07)
    assert np.isfinite(heights).all()
    assert heights.min() >= 0.07
    assert stats['filled_invalid_pixels'] == 2
    assert stats['valid_fraction'] == 14 / 16


def test_flat_depth_is_honestly_a_raised_flat_patch():
    mask = np.ones((3, 4), dtype=bool)
    heights, stats = subject_heights(np.ones_like(mask, dtype=float), mask, mask)
    np.testing.assert_allclose(heights, 0.04)
    assert stats['flat_prediction']


def test_edge_softening_reduces_tall_rims_without_expanding_or_lowering_subjects():
    mask = np.zeros((30, 30), dtype=bool)
    mask[5:25, 5:25] = True
    heights = np.where(mask, 0.22, 1000).astype(np.float32)
    softened = soften_subject_edges(heights, mask, lift=0.04, bevel_pixels=4)
    assert np.all(softened[~mask] == 0)
    assert softened[5, 15] < softened[7, 15] < softened[15, 15]
    assert softened[mask].min() >= 0.04
    assert softened[mask].max() <= 0.220001
    heights[~mask] = -1000
    np.testing.assert_array_equal(softened, soften_subject_edges(heights, mask, lift=0.04, bevel_pixels=4))


def test_edge_softening_preserves_holes_and_flat_lift():
    mask = np.ones((12, 12), dtype=bool)
    mask[4:8, 4:8] = False
    result = soften_subject_edges(np.full(mask.shape, 0.04), mask, lift=0.04, bevel_pixels=3)
    np.testing.assert_allclose(result[mask], 0.04)
    assert np.all(result[~mask] == 0)


@pytest.mark.parametrize('depth', [np.zeros((3, 3)), np.full((3, 3), np.nan), np.full((3, 3), np.inf)])
def test_unusable_depth_is_not_silently_replaced_by_a_fake_model(depth):
    mask = np.ones(depth.shape, dtype=bool)
    with pytest.raises(ValueError, match='valid depth samples'):
        subject_heights(depth, mask, mask)


@pytest.mark.parametrize('lift,relief', [(0, 0.2), (-1, 0.2), (0.04, -1), (np.nan, 0.2)])
def test_bad_height_parameters_are_rejected(lift, relief):
    mask = np.ones((4, 4), dtype=bool)
    with pytest.raises(ValueError):
        subject_heights(np.ones(mask.shape), mask, mask, lift=lift, relief=relief)


def test_disjoint_patches_cover_the_image_without_raising_floor_or_bridging_objects():
    owners = np.zeros((6, 8), dtype=np.uint16)
    owners[1:4, 1:4] = 1
    owners[2:5, 4:7] = 2  # touches the first object
    owners[2, 2] = 0       # a real hole inside its silhouette
    heights = np.where(owners == 1, 0.1, np.where(owners == 2, 0.2, 0)).astype(np.float32)
    top_cells = 0
    for owner in (0, 1, 2):
        vertices, faces, uv, stats = cell_patch_arrays(heights, owners, owner, aspect=4 / 3)
        assert np.isfinite(vertices).all()
        assert faces.min() >= 0 and faces.max() < len(vertices)
        assert np.all((uv >= 0) & (uv <= 1))
        assert stats['top_triangles'] == 2 * np.count_nonzero(owners == owner)
        triangles = vertices[faces[:stats['top_triangles']]]
        centroids = triangles.mean(axis=1)
        cols = np.clip(np.floor((centroids[:, 0] / (4 / 3) + 1) / 2 * 8).astype(int), 0, 7)
        rows = np.clip(np.floor((1 - centroids[:, 1]) / 2 * 6).astype(int), 0, 5)
        assert np.all(owners[rows, cols] == owner)
        if owner == 0:
            assert np.all(vertices[:, 2] == 0)
            assert stats['wall_triangles'] == 0
        else:
            np.testing.assert_allclose(triangles[..., 2], owner * 0.1)
            assert vertices[:, 2].min() == 0  # boundary walls reach the base
            assert stats['boundary_edges'] > 0
        # Top surfaces face +Z. Wrong winding can disappear with culling.
        normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        assert np.all(normals[:, 2] > 0)
        top_cells += stats['cells']
    assert top_cells == owners.size


def test_crop_inference_is_separate_and_does_not_include_other_objects(tmp_path):
    from experiments.subject_relief.run import compose
    from experiments.subject_relief.segmentation import Region

    rgb = np.zeros((40, 60, 3), dtype=np.uint8)
    rgb[:] = [30, 120, 30]
    masks = []
    for box, color in [((5, 6, 18, 30), [255, 255, 255]), ((36, 8, 52, 28), [220, 20, 20])]:
        x0, y0, x1, y1 = box
        mask = np.zeros(rgb.shape[:2], dtype=bool)
        mask[y0:y1, x0:x1] = True
        rgb[mask] = color
        masks.append(mask)
    calls = []

    def predict(image):
        calls.append(image.copy())
        h, w = image.shape[:2]
        depth = np.broadcast_to(np.linspace(1, 3, w), (h, w)).copy()
        return depth, np.ones((h, w), dtype=bool)

    regions = [Region('white-device', masks[0]), Region('red-object', masks[1])]
    heights, owners, records, _ = compose(rgb, regions, predict, tmp_path)
    assert len(calls) == len(records) == 2
    assert np.any(np.all(calls[0] == 255, axis=-1))
    assert not np.any(np.all(calls[0] == [220, 20, 20], axis=-1))
    assert not np.any(np.all(calls[1] == 255, axis=-1))
    assert np.all(heights[owners == 0] == 0)
    assert np.all(heights[owners > 0] >= 0.04)
    assert (tmp_path / 'subject-01-depth.npy').is_file()
    assert (tmp_path / 'subject-02-depth.npy').is_file()


def test_overlapping_subjects_fail_before_second_inference(tmp_path):
    from experiments.subject_relief.run import compose
    from experiments.subject_relief.segmentation import Region

    mask = np.ones((8, 8), dtype=bool)
    with pytest.raises(ValueError, match='overlap'):
        compose(np.zeros((8, 8, 3), dtype=np.uint8), [Region('a', mask), Region('b', mask)],
                lambda rgb: (np.ones(rgb.shape[:2]), np.ones(rgb.shape[:2], dtype=bool)), tmp_path)
