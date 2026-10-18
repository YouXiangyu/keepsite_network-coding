"""Subject-local relief heights and disjoint textured patches on a flat plane.

These heights express an artistic relief, not metric scene depth. No production
generation, storage, or API code imports this experimental module.
"""
from __future__ import annotations

import numpy as np


def whole_image_relief(depth, valid, foreground, *, amplitude=0.32, transition_pixels=2.0):
    """Keep globally normalized scene geometry, flattening only the background.

    Heights are artistic offsets from the original far plane, not metric depth.
    The foreground union only controls coverage; it never changes normalization.
    """
    from scipy.ndimage import distance_transform_edt

    depth = np.asarray(depth, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    foreground = np.asarray(foreground)
    if (depth.ndim != 2 or not depth.size or valid.shape != depth.shape
            or foreground.shape != depth.shape or foreground.dtype != bool):
        raise ValueError('depth, valid and boolean foreground must share a nonempty 2D shape')
    if (not np.isfinite([amplitude, transition_pixels]).all()
            or amplitude <= 0 or transition_pixels < 0):
        raise ValueError('amplitude must be positive and transition_pixels nonnegative and finite')
    selected = valid & np.isfinite(depth) & (depth > 0)
    if selected.sum() < 16:
        raise ValueError('whole-image relief requires at least sixteen valid depth samples')
    inverse = np.zeros_like(depth)
    with np.errstate(over='ignore'):
        inverse[selected] = 1.0 / depth[selected]
    if not np.isfinite(inverse[selected]).all():
        raise ValueError('whole-image inverse depth exceeds the supported numeric range')
    low, high = np.percentile(inverse[selected], [2, 98])
    if high - low < 1e-8:
        raise ValueError('whole-image depth is effectively flat; no relief range is available')
    original = np.clip((inverse - low) / (high - low), 0, 1) * amplitude
    original[~selected] = 0
    distance = distance_transform_edt(np.pad(foreground, 1))[1:-1, 1:-1]
    weights = np.ones(depth.shape) if transition_pixels == 0 else np.minimum(distance / transition_pixels, 1)
    heights = np.where(foreground & selected, original * weights, 0)
    stable = foreground & selected & (weights == 1)
    transition = foreground & selected & (weights < 1)
    return heights, {
        'normalization': 'whole-image valid inverse depth, percentiles 2 and 98',
        'inverse_depth_percentiles': [float(low), float(high)],
        'original_height_range': [float(original[selected].min()), float(original[selected].max())],
        'amplitude': float(amplitude), 'transition_pixels': float(transition_pixels),
        'transition_count': int(transition.sum()), 'stable_foreground_count': int(stable.sum()),
        'stable_foreground_max_error': float(np.abs(heights[stable] - original[stable]).max()) if stable.any() else 0.0,
        'background_max_abs_height': float(np.abs(heights[~foreground]).max()) if (~foreground).any() else 0.0,
        'valid_fraction': float(selected.mean()), 'invalid_foreground_count': int((foreground & ~selected).sum()),
    }


def export_heightfield(heights, texture, target, *, z_offset=0):
    """Export one continuous grid with the production worker's XY/UV topology."""
    import trimesh

    heights = np.asarray(heights)
    if (heights.ndim != 2 or min(heights.shape, default=0) < 2
            or not np.isfinite(heights).all() or np.any(heights < 0)
            or not np.isfinite(z_offset)):
        raise ValueError('heightfield must be finite, nonnegative and at least 2 by 2; offset must be finite')
    if texture.mode != 'RGBA':
        raise ValueError('heightfield texture must be RGBA')
    height, width = heights.shape
    aspect = width / height
    x, y = np.meshgrid(np.linspace(-aspect, aspect, width), np.linspace(1, -1, height))
    vertices = np.stack((x, y, heights + z_offset), axis=-1).reshape(-1, 3)
    ids = np.arange(width * height).reshape(height, width)
    a, b, c, d = ids[:-1, :-1], ids[:-1, 1:], ids[1:, :-1], ids[1:, 1:]
    faces = np.concatenate((np.stack((a, c, b), -1).reshape(-1, 3), np.stack((b, c, d), -1).reshape(-1, 3)))
    uv = np.stack(((x / aspect + 1) / 2, (y + 1) / 2), -1).reshape(-1, 2)
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=texture, metallicFactor=0.0, roughnessFactor=1.0,
        doubleSided=True, alphaMode='BLEND' if texture.getextrema()[3][0] < 255 else 'OPAQUE',
    )
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False,
                           visual=trimesh.visual.texture.TextureVisuals(uv=uv, material=material))
    mesh.export(str(target), file_type='glb', include_normals=True)
    return {
        'topology': 'single continuous regular grid, production XY/UV layout',
        'vertices': len(vertices), 'triangles': len(faces), 'aspect': aspect,
        'z_offset': float(z_offset), 'height_range': [float(heights.min()), float(heights.max())],
        'z_range': [float(heights.min() + z_offset), float(heights.max() + z_offset)],
        'coordinates': {'front': '+Z', 'up': '+Y', 'uv': 'bottom-left origin'},
    }


def subject_heights(depth, valid, mask, *, lift=0.04, relief=0.18):
    """Normalize inverse depth using this subject only; keep it above z=0.

    Invalid pixels inside a subject borrow its nearest valid prediction. An
    entirely invalid subject fails explicitly. A flat prediction becomes a
    raised flat patch rather than a fabricated curved object.
    """
    from scipy.ndimage import distance_transform_edt

    depth = np.asarray(depth, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    mask = np.asarray(mask, dtype=bool)
    if depth.ndim != 2 or valid.shape != depth.shape or mask.shape != depth.shape:
        raise ValueError('depth, valid, and mask must be equal-sized 2D arrays')
    if not np.isfinite([lift, relief]).all() or lift <= 0 or relief < 0:
        raise ValueError('lift must be positive and relief must be nonnegative')
    selected = mask & valid & np.isfinite(depth) & (depth > 0)
    if not mask.any() or selected.sum() < 4:
        raise ValueError('subject has fewer than four valid depth samples')
    inverse = np.zeros(depth.shape, dtype=np.float64)
    inverse[selected] = 1.0 / depth[selected].astype(np.float64)
    low, high = np.percentile(inverse[selected], [2, 98])
    normalized = np.zeros_like(depth)
    flat = bool(high - low < 1e-8)
    if not flat:
        normalized[selected] = np.clip(
            (inverse[selected] - low) / (high - low), 0, 1,
        )
    missing = mask & ~selected
    if missing.any():
        nearest = distance_transform_edt(~selected, return_distances=False, return_indices=True)
        normalized[missing] = normalized[tuple(nearest[:, missing])]
    heights = np.zeros_like(depth)
    heights[mask] = lift + relief * normalized[mask]
    return heights, {
        'valid_fraction': float(selected.sum() / mask.sum()),
        'filled_invalid_pixels': int(missing.sum()),
        'flat_prediction': flat,
        'inverse_depth_percentiles': [float(low), float(high)],
    }


def soften_subject_edges(heights, mask, *, lift, bevel_pixels):
    """Smooth only within one subject and taper its rim toward the base lift.

    A normalized masked filter avoids borrowing background/adjacent-object
    heights. The short bevel reduces tall pixel stair-steps without expanding
    the selected silhouette or pushing any selected pixel below ``lift``.
    """
    from scipy.ndimage import distance_transform_edt, gaussian_filter

    heights = np.asarray(heights, dtype=np.float32)
    mask = np.asarray(mask, dtype=bool)
    if heights.ndim != 2 or mask.shape != heights.shape or not mask.any():
        raise ValueError('edge softening requires a nonempty matching 2D mask')
    if not np.isfinite(heights[mask]).all() or not np.isfinite([lift, bevel_pixels]).all() or lift <= 0 or bevel_pixels <= 0:
        raise ValueError('edge softening parameters and selected heights must be finite and positive')
    sigma = max(0.5, bevel_pixels / 3)
    weights = gaussian_filter(mask.astype(np.float32), sigma, mode='constant')
    weighted = gaussian_filter(np.where(mask, heights, 0), sigma, mode='constant')
    smooth = np.divide(weighted, weights, out=np.zeros_like(weighted), where=weights > 1e-8)
    distance = distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    rim = np.minimum(distance / bevel_pixels, 1)
    result = np.zeros_like(heights)
    result[mask] = lift + np.maximum(smooth[mask] - lift, 0) * rim[mask]
    return result


def cell_patch_arrays(heights, owners, owner, *, aspect):
    """Build one patch, with no top triangle crossing a subject boundary.

    Each label owns complete grid cells. Adjacent labels duplicate their seam
    vertices, so a raised object cannot pull floor vertices upwards. Subject
    boundary walls connect to z=0; the background itself stays exactly flat.
    """
    heights = np.asarray(heights, dtype=np.float32)
    owners = np.asarray(owners)
    if heights.ndim != 2 or owners.shape != heights.shape:
        raise ValueError('height and owner grids must have matching 2D shapes')
    if not np.isfinite(heights).all() or not np.isfinite(aspect) or aspect <= 0:
        raise ValueError('geometry must be finite and aspect must be positive')
    cells = owners == owner
    if not cells.any():
        raise ValueError('patch has no cells')
    rows, cols = cells.shape
    if min(rows, cols) < 2:
        raise ValueError('geometry grid must be at least 2 by 2')
    if owner and np.any(heights[cells] <= 0):
        raise ValueError('subject cells must be above the background')

    # Average only cells belonging to this patch; another object's depth has
    # no influence on this object's edge vertices.
    count = np.zeros((rows + 1, cols + 1), dtype=np.float32)
    total = np.zeros_like(count)
    for dy, dx in ((0, 0), (0, 1), (1, 0), (1, 1)):
        count[dy:dy + rows, dx:dx + cols] += cells
        total[dy:dy + rows, dx:dx + cols] += np.where(cells, heights, 0)
    z = np.divide(total, count, out=np.zeros_like(total), where=count > 0)
    if owner == 0:
        z.fill(0)
    x, y = np.meshgrid(np.linspace(-aspect, aspect, cols + 1), np.linspace(1, -1, rows + 1))
    all_vertices = np.stack((x, y, z), axis=-1).reshape(-1, 3)
    all_uv = np.stack(((x / aspect + 1) / 2, (y + 1) / 2), axis=-1).reshape(-1, 2)
    ids = np.arange((rows + 1) * (cols + 1)).reshape(rows + 1, cols + 1)
    a, b, c, d = ids[:-1, :-1][cells], ids[:-1, 1:][cells], ids[1:, :-1][cells], ids[1:, 1:][cells]
    top = np.concatenate((np.stack((a, c, b), -1), np.stack((b, c, d), -1)))
    used, remapped = np.unique(top, return_inverse=True)
    vertices, uv = all_vertices[used], all_uv[used]
    faces = remapped.reshape(-1, 3)
    top_faces = len(faces)
    boundary_edges = 0

    if owner:
        padded = np.pad(cells, 1)
        edges = []
        for neighbor, start, end in (
            (padded[:-2, 1:-1], ids[:-1, :-1], ids[:-1, 1:]),
            (padded[1:-1, 2:], ids[:-1, 1:], ids[1:, 1:]),
            (padded[2:, 1:-1], ids[1:, 1:], ids[1:, :-1]),
            (padded[1:-1, :-2], ids[1:, :-1], ids[:-1, :-1]),
        ):
            boundary = cells & ~neighbor
            edges.append(np.stack((start[boundary], end[boundary]), axis=-1))
        edges = np.concatenate(edges)
        boundary_edges = len(edges)
        # Separate wall vertices preserve a sharp edge and independent normals.
        wall_top = all_vertices[edges]
        wall_bottom = wall_top.copy()
        wall_bottom[..., 2] = 0
        walls = np.stack((wall_top[:, 0], wall_top[:, 1], wall_bottom[:, 0], wall_bottom[:, 1]), axis=1)
        wall_uv = all_uv[edges][:, [0, 1, 0, 1]]
        wall_ids = np.arange(len(vertices), len(vertices) + 4 * len(edges)).reshape(-1, 4)
        wa, wb, wc, wd = wall_ids.T
        wall_faces = np.concatenate((np.stack((wa, wb, wc), -1), np.stack((wb, wd, wc), -1)))
        vertices = np.concatenate((vertices, walls.reshape(-1, 3)))
        uv = np.concatenate((uv, wall_uv.reshape(-1, 2)))
        faces = np.concatenate((faces, wall_faces))
    return vertices, faces, uv, {
        'owner': int(owner), 'cells': int(cells.sum()),
        'top_triangles': top_faces, 'boundary_edges': boundary_edges,
        'wall_triangles': boundary_edges * 2,
    }


def export_scene(heights, owners, texture, target, names):
    import trimesh

    scene = trimesh.Scene()
    patches = []
    aspect = texture.width / texture.height
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=texture, metallicFactor=0.0, roughnessFactor=1.0,
        doubleSided=True, alphaMode='BLEND' if texture.getextrema()[3][0] < 255 else 'OPAQUE',
    )
    for owner in np.unique(owners):
        vertices, faces, uv, metadata = cell_patch_arrays(heights, owners, int(owner), aspect=aspect)
        mesh = trimesh.Trimesh(
            vertices=vertices, faces=faces, process=False,
            visual=trimesh.visual.texture.TextureVisuals(uv=uv, material=material),
        )
        scene.add_geometry(mesh, node_name=names[int(owner)], geom_name=names[int(owner)])
        metadata.update(vertices=len(vertices), triangles=len(faces))
        patches.append(metadata)
    scene.export(str(target), file_type='glb', include_normals=True)
    return patches
