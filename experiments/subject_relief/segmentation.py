"""Local semantic proposals and conservative masks for the relief experiment.

This is semantic connected-component segmentation, not instance segmentation:
touching objects of the same class can share a region. GraphCut only removes
pixels from a narrow inner boundary, so a predicted floor cannot become raised
foreground during refinement. Manual boxes or painted masks are the correction
path when the semantic model misses or misclassifies an object.
"""

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import unicodedata

import cv2
import numpy as np
from PIL import Image


@dataclass
class Region:
    name: str
    mask: np.ndarray
    score: float | None = None
    source: str = "manual"


class NoSubjectsError(RuntimeError):
    """No usable subjects were found; diagnostics remain available to callers."""

    def __init__(self, message: str, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


class RefinementError(RuntimeError):
    """GraphCut failed; the caller must explicitly choose another mask method."""


# Deliberately select known objects rather than using brightness as depth.
# ADE20K has neither a dedicated cup class nor an air-conditioner class.
DEFAULT_SUBJECT_LABELS = frozenset(
    "animal|apparel|armchair|ashcan|bag|ball|barrel|basket|bathtub|bed|bench|"
    "bicycle|blanket|boat|book|bookcase|bottle|box|buffet|bus|cabinet|car|case|"
    "chair|chandelier|chest of drawers|clock|coffee table|computer|cradle|"
    "crt screen|cushion|desk|dishwasher|fan|fireplace|flower|food|glass|hood|"
    "kitchen island|lamp|microwave|minibike|monitor|ottoman|oven|person|"
    "pillow|plant|plate|plaything|pool table|pot|radiator|refrigerator|"
    "sculpture|seat|shelf|ship|shower|sink|sofa|stool|stove|swivel chair|"
    "table|tank|television receiver|toilet|towel|traffic light|tray|truck|"
    "van|vase|wardrobe|washer".split("|")
)

# Even an explicit automatic-label request cannot promote structural ground
# into a subject. An intentional override must use a manual box or mask.
PROTECTED_BACKGROUND_LABELS = frozenset(
    "wall|building|sky|floor|ceiling|road|grass|sidewalk|earth|mountain|"
    "water|house|sea|rug|field|fence|sand|skyscraper|path|stairs|runway|"
    "stairway|river|bridge|hill|land|dirt track|stage|swimming pool|"
    "waterfall|lake|step|pier".split("|")
)


def _slug(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Region names must be non-empty strings.")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:64]
    if not slug:
        slug = "subject-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    reserved = {"con", "prn", "aux", "nul"}
    reserved.update(f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10))
    return "subject-" + slug if slug in reserved else slug


def _validate_image(image_rgb: np.ndarray) -> None:
    if not isinstance(image_rgb, np.ndarray):
        raise ValueError("image_rgb must be a numpy uint8 RGB array.")
    if image_rgb.dtype != np.uint8 or image_rgb.ndim != 3 or image_rgb.shape[2] != 3:
        raise ValueError("image_rgb must have dtype uint8 and shape (height, width, 3).")
    if min(image_rgb.shape[:2]) < 2:
        raise ValueError("image_rgb must be at least 2 by 2 pixels.")


def _aliases(label: str) -> set[str]:
    return {part.strip().lower() for part in label.split(",") if part.strip()}


def _canonical_label(label: str) -> str:
    return label.split(",", 1)[0].strip().lower()


def _infer_semantics(image_rgb: np.ndarray, model_dir: Path):
    """Return full-size IDs/confidence, label names and local-model metadata.

    Keep this adapter separate so tests can inject predictions without loading
    weights or contacting a model registry. Class logits are never expanded to
    the original multi-megapixel image size.
    """
    model_dir = Path(model_dir).resolve()
    required = ("config.json", "preprocessor_config.json", "model.safetensors")
    missing = [name for name in required if not (model_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Local SegFormer files missing in {model_dir}: {', '.join(missing)}. "
            "Download the pinned model explicitly before running; automatic downloads are disabled."
        )

    import torch
    from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor

    processor = SegformerImageProcessor.from_pretrained(str(model_dir), local_files_only=True)
    model = SegformerForSemanticSegmentation.from_pretrained(
        str(model_dir), local_files_only=True, use_safetensors=True
    ).to("cpu")
    model.eval()
    height, width = image_rgb.shape[:2]
    scale = min(1.0, 512 / max(height, width))
    working_size = (max(1, round(height * scale)), max(1, round(width * scale)))
    with torch.inference_mode():
        # PIL-backed arrays may be read-only; torch.from_numpy needs writable storage.
        inputs = processor(images=np.array(image_rgb, copy=True, order="C"), return_tensors="pt")
        inputs = {key: value.to("cpu") for key, value in inputs.items()}
        logits = model(**inputs).logits
        logits = torch.nn.functional.interpolate(
            logits, size=working_size, mode="bilinear", align_corners=False
        )
        confidence, semantic = logits.softmax(dim=1).max(dim=1)
        semantic_small = semantic[0].numpy().astype(np.int32)
        confidence_small = confidence[0].numpy().astype(np.float32)

    semantic_map = cv2.resize(semantic_small, (width, height), interpolation=cv2.INTER_NEAREST)
    confidence_map = cv2.resize(confidence_small, (width, height), interpolation=cv2.INTER_LINEAR)
    labels_by_id = {int(key): str(value) for key, value in model.config.id2label.items()}
    metadata = {
        "model": "SegFormer semantic segmentation",
        "model_dir": str(model_dir),
        "device": "cpu",
        "local_files_only": True,
        "use_safetensors": True,
        "working_size": [working_size[1], working_size[0]],
    }
    return semantic_map, confidence_map, labels_by_id, metadata


def _validate_prediction(semantic_map, confidence_map, labels_by_id, image_shape):
    if (
        not isinstance(semantic_map, np.ndarray)
        or semantic_map.shape != image_shape
        or not np.issubdtype(semantic_map.dtype, np.integer)
    ):
        raise ValueError("Semantic adapter returned an invalid class map.")
    if (
        not isinstance(confidence_map, np.ndarray)
        or confidence_map.shape != image_shape
        or not np.isfinite(confidence_map).all()
        or np.any((confidence_map < 0) | (confidence_map > 1))
    ):
        raise ValueError("Semantic adapter returned invalid confidence values.")
    if not labels_by_id or any(not isinstance(value, str) or not value.strip() for value in labels_by_id.values()):
        raise ValueError("Semantic adapter returned invalid class names.")
    unknown_ids = set(np.unique(semantic_map).tolist()) - set(labels_by_id)
    if unknown_ids:
        raise ValueError(f"Semantic adapter returned IDs without labels: {sorted(unknown_ids)}.")


def _refine_mask(image_rgb: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, dict]:
    """Shrink only the uncertain inner rim; preserve core, holes and exclusion."""
    height, width = mask.shape
    radius = max(1, round(min(height, width) / 256))
    rows, columns = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if not len(rows):
        raise RefinementError("Cannot refine an empty subject mask.")
    padding = radius + 2
    y0, y1 = max(0, rows[0] - padding), min(height, rows[-1] + padding + 1)
    x0, x1 = max(0, columns[0] - padding), min(width, columns[-1] + padding + 1)
    roi_mask = mask[y0:y1, x0:x1]
    roi_image = image_rgb[y0:y1, x0:x1]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    original_core = cv2.erode(
        roi_mask.astype(np.uint8), kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0
    ).astype(bool)
    scale = min(1.0, 768 / max(roi_mask.shape))
    working_size = (max(2, round(roi_mask.shape[1] * scale)), max(2, round(roi_mask.shape[0] * scale)))
    work_image = cv2.resize(roi_image, working_size, interpolation=cv2.INTER_AREA)
    work_mask = cv2.resize(roi_mask.astype(np.uint8), working_size, interpolation=cv2.INTER_NEAREST).astype(bool)
    work_core = cv2.resize(original_core.astype(np.uint8), working_size, interpolation=cv2.INTER_NEAREST).astype(bool)
    if int(work_mask.sum()) < 5 or int((~work_mask).sum()) < 5:
        raise RefinementError(
            "Subject has fewer than five foreground or background samples for GraphCut; "
            "use refine=False explicitly or provide a manual mask."
        )
    if not work_core.any():
        distance = cv2.distanceTransform(work_mask.astype(np.uint8), cv2.DIST_L2, 5)
        work_core = work_mask & (distance >= distance.max() * 0.75)
    graph_labels = np.full(work_mask.shape, cv2.GC_BGD, dtype=np.uint8)
    graph_labels[work_mask] = cv2.GC_PR_FGD
    graph_labels[work_core] = cv2.GC_FGD
    try:
        cv2.grabCut(
            np.ascontiguousarray(work_image), graph_labels, None,
            np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64),
            3, cv2.GC_INIT_WITH_MASK,
        )
    except cv2.error as exc:
        raise RefinementError(f"GraphCut boundary refinement failed: {exc}") from exc
    selected = (graph_labels == cv2.GC_FGD) | (graph_labels == cv2.GC_PR_FGD)
    selected = cv2.resize(
        selected.astype(np.uint8), (roi_mask.shape[1], roi_mask.shape[0]),
        interpolation=cv2.INTER_NEAREST,
    ).astype(bool)
    # Reapply the exact, full-resolution constraints after the working resize.
    selected = original_core | (roi_mask & selected)
    if not selected.any():
        raise RefinementError("GraphCut removed the whole subject; provide a manual mask.")
    refined = np.zeros_like(mask)
    refined[y0:y1, x0:x1] = selected
    return refined, {
        "method": "graphcut-inner-boundary",
        "radius_pixels": radius,
        "working_size": list(working_size),
        "removed_pixels": int(mask.sum() - refined.sum()),
        "added_pixels": 0,
    }


def automatic_regions(
    image_rgb: np.ndarray,
    model_dir: Path,
    *,
    labels: list[str] | None = None,
    min_area_fraction: float = 0.003,
    max_regions: int = 12,
    refine: bool = True,
) -> tuple[list[Region], dict]:
    """Propose disjoint object regions; fail explicitly if none are usable.

    ``labels`` matches model class names or their comma-separated aliases. It
    does not accept invented labels: ADE20K cannot identify every object type.
    Confidence is semantic class confidence, not a correctness guarantee.
    """
    _validate_image(image_rgb)
    if (
        isinstance(min_area_fraction, bool)
        or not isinstance(min_area_fraction, (int, float))
        or not math.isfinite(min_area_fraction)
        or not 0 < min_area_fraction <= 1
    ):
        raise ValueError("min_area_fraction must be finite and in (0, 1].")
    if isinstance(max_regions, bool) or not isinstance(max_regions, int) or max_regions < 1:
        raise ValueError("max_regions must be a positive integer.")
    if not isinstance(refine, bool):
        raise ValueError("refine must be a boolean.")
    if labels is not None and (
        not isinstance(labels, list) or not labels
        or any(not isinstance(label, str) or not label.strip() for label in labels)
    ):
        raise ValueError("labels must be a non-empty list of model class names.")

    semantic_map, confidence_map, labels_by_id, metadata = _infer_semantics(image_rgb, Path(model_dir))
    _validate_prediction(semantic_map, confidence_map, labels_by_id, image_rgb.shape[:2])
    aliases_by_id = {class_id: _aliases(label) for class_id, label in labels_by_id.items()}
    if labels is None:
        selected_ids = {
            class_id for class_id, aliases in aliases_by_id.items()
            if aliases & DEFAULT_SUBJECT_LABELS and not aliases & PROTECTED_BACKGROUND_LABELS
        }
    else:
        requested = {label.strip().lower() for label in labels}
        available = set().union(*aliases_by_id.values())
        unknown = requested - available
        if unknown:
            raise ValueError(f"Labels are absent from this model: {', '.join(sorted(unknown))}.")
        selected_ids = {class_id for class_id, aliases in aliases_by_id.items() if aliases & requested}
        protected = [
            labels_by_id[class_id] for class_id in selected_ids
            if aliases_by_id[class_id] & PROTECTED_BACKGROUND_LABELS
        ]
        if protected:
            raise ValueError(
                f"Scene planes cannot be automatic subjects: {', '.join(protected)}. "
                "Use a manual mask for an intentional override."
            )

    total_pixels = semantic_map.size
    minimum_pixels = max(1, math.ceil(total_pixels * min_area_fraction))
    diagnostics = {
        "semantic_map": semantic_map,
        "model": metadata,
        "detected_classes": [],
        "filtered": [],
        "regions": [],
        "limitations": [
            "Semantic connected components are not instances; touching objects of one class can merge.",
            "Object category selection is not physical foreground/depth estimation; check masks visually.",
            "ADE20K has no dedicated cup or air-conditioner label; "
            "omitted or incorrect subjects need manual correction.",
            "GraphCut only shrinks the inner boundary; it cannot recover an object the semantic model omitted.",
        ],
    }
    candidates = []
    class_ids, pixel_counts = np.unique(semantic_map, return_counts=True)
    for class_id, pixel_count in zip(class_ids.tolist(), pixel_counts.tolist()):
        class_name = _canonical_label(labels_by_id[class_id])
        class_mask = semantic_map == class_id
        diagnostics["detected_classes"].append({
            "id": class_id, "name": class_name, "pixels": pixel_count,
            "fraction": pixel_count / total_pixels,
            "mean_confidence": float(confidence_map[class_mask].mean()),
            "selected": class_id in selected_ids,
        })
        if class_id not in selected_ids:
            diagnostics["filtered"].append({
                "class": class_name, "pixels": pixel_count,
                "reason": (
                    "protected-background" if aliases_by_id[class_id] & PROTECTED_BACKGROUND_LABELS
                    else "class-not-selected"
                ),
            })
            continue
        component_count, component_map, stats, _ = cv2.connectedComponentsWithStats(
            class_mask.astype(np.uint8), connectivity=8
        )
        for component_id in range(1, component_count):
            x, y, width, height, pixels = stats[component_id].tolist()
            record = {"class": class_name, "class_id": class_id, "component": component_id, "pixels": pixels}
            if pixels < minimum_pixels:
                diagnostics["filtered"].append({**record, "reason": "below-minimum-area"})
                continue
            candidates.append({
                **record, "box": (x, y, x + width, y + height),
                "mask": component_map[y:y + height, x:x + width] == component_id,
            })

    candidates.sort(key=lambda item: (-item["pixels"], item["class_id"], item["component"]))
    for candidate in candidates[max_regions:]:
        diagnostics["filtered"].append({
            key: value for key, value in candidate.items() if key not in ("mask", "box")
        } | {"reason": "max-regions"})
    regions = []
    for candidate in candidates[:max_regions]:
        mask = np.zeros(image_rgb.shape[:2], dtype=bool)
        x0, y0, x1, y1 = candidate["box"]
        mask[y0:y1, x0:x1] = candidate["mask"]
        name = f"{_slug(candidate['class'])}-{candidate['component']:02d}"
        refinement = {"method": "disabled-explicitly", "added_pixels": 0, "removed_pixels": 0}
        if refine:
            try:
                mask, refinement = _refine_mask(image_rgb, mask)
            except RefinementError as exc:
                raise RefinementError(f"Region {name}: {exc}") from exc
        score = float(confidence_map[mask].mean())
        regions.append(Region(name=name, mask=mask, score=score, source="semantic-graphcut" if refine else "semantic"))
        diagnostics["regions"].append({
            "name": name, "class": candidate["class"], "class_id": candidate["class_id"],
            "component": candidate["component"], "pixels": int(mask.sum()),
            "original_pixels": candidate["pixels"], "score": score,
            "refinement": refinement,
        })
    if not regions:
        raise NoSubjectsError(
            "No usable object regions were found. Inspect the semantic diagnostics, "
            "select supported labels, or supply manual boxes/masks.", diagnostics
        )
    return regions, diagnostics


def automatic_foreground(
    image_rgb: np.ndarray,
    model_dir: Path,
    *,
    min_component_fraction: float = 0.02,
    preserve_mask: np.ndarray | None = None,
    refine: bool = True,
) -> tuple[np.ndarray, dict]:
    """Make one foreground mask for a shared, whole-image depth prediction.

    Semantic classes are united before component filtering, so a cushion and
    the adjoining sofa can survive together. Manual pixels are exempt from
    area filtering and restored after refinement. No holes are filled, and no
    automatic operation adds pixels outside the original object-class union.
    """
    _validate_image(image_rgb)
    if (
        isinstance(min_component_fraction, bool)
        or not isinstance(min_component_fraction, (int, float))
        or not math.isfinite(min_component_fraction)
        or not 0 < min_component_fraction <= 1
    ):
        raise ValueError("min_component_fraction must be finite and in (0, 1].")
    if not isinstance(refine, bool):
        raise ValueError("refine must be a boolean.")
    shape = image_rgb.shape[:2]
    if preserve_mask is not None and (
        not isinstance(preserve_mask, np.ndarray)
        or preserve_mask.shape != shape
        or preserve_mask.dtype != bool
    ):
        raise ValueError("preserve_mask must be a boolean array matching the image height and width.")
    manual = np.zeros(shape, dtype=bool) if preserve_mask is None else preserve_mask.copy()

    semantic_map, confidence_map, labels_by_id, metadata = _infer_semantics(image_rgb, Path(model_dir))
    _validate_prediction(semantic_map, confidence_map, labels_by_id, shape)
    selected_ids = {
        class_id for class_id, label in labels_by_id.items()
        if _aliases(label) & DEFAULT_SUBJECT_LABELS and not _aliases(label) & PROTECTED_BACKGROUND_LABELS
    }
    semantic_union = np.isin(semantic_map, list(selected_ids))
    raw_union = semantic_union | manual
    minimum_pixels = max(1, math.ceil(raw_union.size * min_component_fraction))
    diagnostics = {
        "semantic_map": semantic_map,
        "model": metadata,
        "threshold": {
            "fraction": float(min_component_fraction),
            "pixels": minimum_pixels,
            "image_pixels": raw_union.size,
            "connectivity": 8,
        },
        "semantic_union_pixels": int(semantic_union.sum()),
        "raw_union_pixels": int(raw_union.sum()),
        "kept_pixels_before_refinement": 0,
        "kept_pixels": 0,
        "detected_classes": [],
        "components_before": [],
        "components_after": [],
        "removed_regions": [],
        "manual": {
            "supplied": preserve_mask is not None,
            "pixels": int(manual.sum()),
            "added_to_semantic_union_pixels": int((manual & ~semantic_union).sum()),
            "retained_pixels": 0,
        },
        "hole_filling": {"enabled": False, "added_pixels": 0},
        "refinement": {"method": "disabled-explicitly", "added_pixels": 0, "removed_pixels": 0},
        "limitations": [
            "The foreground union is not an instance count; touching furniture can share one component.",
            "The area threshold measures image pixels, not physical object size; small real objects may be removed.",
            "A small misclassified fragment attached to a large component can survive area filtering.",
            "Holes are not filled; excluded floor and wall pixels remain background unless explicitly preserved.",
            "Semantic classes can be incorrect or missing; ADE20K has no dedicated cup or air-conditioner class.",
            "Use this single mask with a shared depth prediction; do not normalize each semantic class independently.",
        ],
    }
    class_ids, pixel_counts = np.unique(semantic_map, return_counts=True)
    for class_id, pixels in zip(class_ids.tolist(), pixel_counts.tolist()):
        diagnostics["detected_classes"].append({
            "id": class_id, "name": _canonical_label(labels_by_id[class_id]),
            "pixels": pixels, "selected": class_id in selected_ids,
        })

    def filter_components(mask, stage):
        count, component_map, stats, _ = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=8
        )
        kept = np.zeros(shape, dtype=bool)
        records = []
        for component_id in range(1, count):
            x, y, width, height, pixels = stats[component_id].tolist()
            component = component_map[y:y + height, x:x + width] == component_id
            manual_pixels = int((component & manual[y:y + height, x:x + width]).sum())
            keep = pixels >= minimum_pixels or manual_pixels > 0
            record = {
                "component": component_id, "pixels": pixels,
                "fraction": pixels / raw_union.size,
                "box": [x, y, x + width, y + height],
                "manual_pixels": manual_pixels,
                "manual_exemption": bool(manual_pixels and pixels < minimum_pixels),
                "kept": keep,
            }
            records.append(record)
            if keep:
                kept[y:y + height, x:x + width] |= component
            else:
                diagnostics["removed_regions"].append({
                    **record, "stage": stage, "reason": "below-minimum-component-area",
                })
        return kept, records

    kept, diagnostics["components_before"] = filter_components(raw_union, "before-refinement")
    diagnostics["kept_pixels_before_refinement"] = int(kept.sum())
    if not kept.any():
        raise NoSubjectsError(
            "No foreground components meet the area threshold; inspect diagnostics, "
            "lower the threshold explicitly, or preserve a manual mask.", diagnostics
        )
    if refine and np.any(kept & ~manual):
        kept, diagnostics["refinement"] = _refine_mask(image_rgb, kept)
    elif refine:
        diagnostics["refinement"]["method"] = "manual-only-no-automatic-refinement"
    kept |= manual
    # Refinement can sever a thin connection; recheck the final component sizes.
    kept, final_records = filter_components(kept, "after-refinement")
    diagnostics["components_after"] = [record for record in final_records if record["kept"]]
    diagnostics["kept_pixels"] = int(kept.sum())
    diagnostics["manual"]["retained_pixels"] = int((kept & manual).sum())
    if not kept.any():
        raise NoSubjectsError(
            "No foreground components meet the area threshold after boundary refinement; "
            "inspect diagnostics or preserve a manual mask.", diagnostics
        )
    return kept, diagnostics


def grabcut_region(image_rgb: np.ndarray, box: tuple[int, int, int, int], name: str) -> Region:
    """Segment a manual rectangle, in half-open pixels (x0, y0, x1, y1).

    Include some background inside the rectangle. Pixels outside it are always
    background; colors alone cannot reliably split an object from an identical
    background, for which a painted mask is the explicit correction path.
    """
    _validate_image(image_rgb)
    name = _slug(name)
    if (
        not isinstance(box, (tuple, list)) or len(box) != 4
        or any(isinstance(value, bool) or not isinstance(value, (int, np.integer)) for value in box)
    ):
        raise ValueError("box must contain four integer pixel coordinates (x0, y0, x1, y1).")
    x0, y0, x1, y1 = map(int, box)
    height, width = image_rgb.shape[:2]
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("box must be a non-empty, half-open rectangle within the image.")
    box_pixels = (x1 - x0) * (y1 - y0)
    if box_pixels < 5 or height * width - box_pixels < 5:
        raise ValueError("GraphCut requires at least five pixels inside and outside the box.")
    graph_labels = np.zeros((height, width), dtype=np.uint8)
    try:
        cv2.grabCut(
            np.ascontiguousarray(image_rgb), graph_labels, (x0, y0, x1 - x0, y1 - y0),
            np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64),
            5, cv2.GC_INIT_WITH_RECT,
        )
    except cv2.error as exc:
        raise RefinementError(f"Manual GraphCut for {name} failed: {exc}") from exc
    mask = (graph_labels == cv2.GC_FGD) | (graph_labels == cv2.GC_PR_FGD)
    if not mask.any():
        raise NoSubjectsError(f"Manual GraphCut found no foreground for {name}; supply a painted mask.", {})
    return Region(name=name, mask=mask, source="manual-grabcut")


def load_mask(path: Path, size: tuple[int, int]) -> np.ndarray:
    """Read an exact-size painted mask, with size=(width, height).

    Luminance >= 128 is foreground. If alpha exists, it must also be >= 128.
    This function deliberately refuses implicit resizing and empty masks.
    """
    if (
        not isinstance(size, (tuple, list)) or len(size) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value <= 0
            for value in size
        )
    ):
        raise ValueError("size must be (width, height), both positive integers.")
    with Image.open(path) as image:
        if image.size != tuple(size):
            raise ValueError(f"Mask size {image.size} does not match image size {tuple(size)}; resize explicitly.")
        mask = np.asarray(image.convert("L")) >= 128
        if "A" in image.getbands() or "transparency" in image.info:
            mask &= np.asarray(image.convert("RGBA").getchannel("A")) >= 128
    if not mask.any():
        raise ValueError("Mask contains no foreground pixels.")
    return mask
