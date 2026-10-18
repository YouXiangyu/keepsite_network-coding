"""Segmentation contract tests; model inference is local/injected, never online."""

from pathlib import Path
import sys
import types

import cv2
import numpy as np
from PIL import Image
import pytest

from experiments.subject_relief import segmentation as subject


def inject_semantics(monkeypatch, semantic, labels=None, confidence=None):
    labels = labels or {0: "floor, flooring", 1: "bed", 2: "pillow", 3: "cabinet"}
    confidence = confidence if confidence is not None else np.full(semantic.shape, 0.9, np.float32)
    monkeypatch.setattr(
        subject, "_infer_semantics", lambda image, path: (semantic, confidence, labels, {"test": True})
    )


def test_white_bed_is_a_subject_and_bright_floor_never_is(monkeypatch):
    image = np.full((64, 80, 3), 245, np.uint8)
    image[12:50, 14:62] = 255
    semantic = np.zeros(image.shape[:2], np.int32)
    semantic[12:50, 14:62] = 1
    inject_semantics(monkeypatch, semantic)
    regions, diagnostics = subject.automatic_regions(image, Path("unused"))
    assert [region.name for region in regions] == ["bed-01"]
    assert regions[0].mask[20:40, 25:50].all()
    assert not regions[0].mask[semantic == 0].any()
    assert regions[0].score == pytest.approx(0.9)
    assert diagnostics["semantic_map"] is semantic
    assert any(record["reason"] == "protected-background" for record in diagnostics["filtered"])


def test_graphcut_removes_wrong_color_rim_but_preserves_core_and_hole():
    image = np.zeros((96, 96, 3), np.uint8)
    mask = np.zeros((96, 96), bool)
    mask[15:80, 15:80] = True
    mask[35:50, 35:50] = False
    image[16:79, 16:79] = (245, 245, 245)
    image[35:50, 35:50] = 0
    refined, report = subject._refine_mask(image, mask)
    assert not (refined & ~mask).any()
    assert refined[20:30, 20:30].all()
    assert not refined[35:50, 35:50].any()
    assert not refined[15, 20:70].any()
    assert report["removed_pixels"] > 0
    assert report["added_pixels"] == 0


def test_adjacent_classes_disconnected_subjects_and_holes_stay_disjoint(monkeypatch):
    image = np.full((80, 100, 3), 150, np.uint8)
    semantic = np.zeros(image.shape[:2], np.int32)
    semantic[10:50, 10:60] = 1
    semantic[20:35, 20:35] = 2
    semantic[10:50, 60:80] = 3
    semantic[60:70, 10:25] = 1
    inject_semantics(monkeypatch, semantic)
    regions, _ = subject.automatic_regions(image, Path("unused"), refine=False)
    assert len(regions) == 4
    assert {region.name for region in regions} == {"bed-01", "bed-02", "pillow-01", "cabinet-01"}
    coverage = np.sum([region.mask for region in regions], axis=0)
    assert coverage.max() == 1
    assert np.array_equal(coverage > 0, semantic > 0)
    bed = next(region for region in regions if region.name == "bed-01")
    assert not bed.mask[semantic == 2].any()


def test_touching_same_class_is_reported_as_one_component(monkeypatch):
    image = np.zeros((40, 40, 3), np.uint8)
    semantic = np.zeros((40, 40), np.int32)
    semantic[5:20, 5:20] = 1
    semantic[10:30, 20:30] = 1
    inject_semantics(monkeypatch, semantic)
    regions, diagnostics = subject.automatic_regions(image, Path("unused"), refine=False)
    assert len(regions) == 1
    assert any("not instances" in limitation for limitation in diagnostics["limitations"])


def test_filters_have_reasons_and_largest_regions_are_retained(monkeypatch):
    image = np.zeros((100, 100, 3), np.uint8)
    semantic = np.zeros((100, 100), np.int32)
    semantic[5:25, 5:25] = 1
    semantic[30:40, 30:40] = 1
    semantic[60:62, 60:62] = 1
    inject_semantics(monkeypatch, semantic)
    regions, diagnostics = subject.automatic_regions(image, Path("unused"), max_regions=1, refine=False)
    assert len(regions) == 1
    assert regions[0].mask.sum() == 400
    assert {record["reason"] for record in diagnostics["filtered"]} >= {"max-regions", "below-minimum-area"}


def test_no_subject_is_a_visible_failure_with_diagnostics(monkeypatch):
    image = np.full((40, 40, 3), 255, np.uint8)
    inject_semantics(monkeypatch, np.zeros((40, 40), np.int32))
    with pytest.raises(subject.NoSubjectsError) as error:
        subject.automatic_regions(image, Path("unused"))
    assert error.value.diagnostics["detected_classes"][0]["name"] == "floor"
    assert error.value.diagnostics["regions"] == []


@pytest.mark.parametrize("labels", [["floor"], ["flooring"], ["invented-object"]])
def test_explicit_automatic_labels_cannot_raise_floor_or_invent_classes(monkeypatch, labels):
    image = np.zeros((40, 40, 3), np.uint8)
    inject_semantics(monkeypatch, np.zeros((40, 40), np.int32))
    with pytest.raises(ValueError):
        subject.automatic_regions(image, Path("unused"), labels=labels)


def test_graphcut_failure_is_not_silently_replaced_by_semantic_mask(monkeypatch):
    image = np.zeros((40, 40, 3), np.uint8)
    semantic = np.zeros((40, 40), np.int32)
    semantic[5:30, 5:30] = 1
    inject_semantics(monkeypatch, semantic)

    def fail(*args, **kwargs):
        raise cv2.error("controlled failure")

    monkeypatch.setattr(cv2, "grabCut", fail)
    with pytest.raises(subject.RefinementError, match="Region bed-01.*controlled failure"):
        subject.automatic_regions(image, Path("unused"))


@pytest.mark.parametrize("image", [
    np.zeros((0, 3, 3), np.uint8), np.zeros((3, 3), np.uint8),
    np.zeros((3, 3, 4), np.uint8), np.zeros((3, 3, 3), float),
])
def test_bad_images_are_rejected_before_model_loading(monkeypatch, image):
    monkeypatch.setattr(subject, "_infer_semantics", lambda *args: pytest.fail("model must not load"))
    with pytest.raises(ValueError):
        subject.automatic_regions(image, Path("unused"))


@pytest.mark.parametrize("arguments", [
    {"min_area_fraction": 0}, {"min_area_fraction": float("nan")},
    {"max_regions": 0}, {"max_regions": True}, {"labels": []}, {"labels": "bed"}, {"refine": "yes"},
])
def test_bad_options_are_rejected_before_model_loading(monkeypatch, arguments):
    monkeypatch.setattr(subject, "_infer_semantics", lambda *args: pytest.fail("model must not load"))
    with pytest.raises(ValueError):
        subject.automatic_regions(np.zeros((3, 3, 3), np.uint8), Path("unused"), **arguments)


def test_invalid_adapter_output_is_rejected(monkeypatch):
    semantic = np.ones((20, 20), np.int32)
    confidence = np.ones((20, 20), np.float32)
    confidence[0, 0] = np.nan
    inject_semantics(monkeypatch, semantic, confidence=confidence)
    with pytest.raises(ValueError, match="confidence"):
        subject.automatic_regions(np.zeros((20, 20, 3), np.uint8), Path("unused"))


def test_local_adapter_requires_explicit_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="automatic downloads are disabled"):
        subject._infer_semantics(np.zeros((20, 30, 3), np.uint8), tmp_path)


def test_local_adapter_pins_offline_safe_weights_and_cpu(monkeypatch, tmp_path):
    import torch

    for name in ("config.json", "preprocessor_config.json", "model.safetensors"):
        (tmp_path / name).write_bytes(b"test-only-placeholder")
    calls = {}

    class Processor:
        @classmethod
        def from_pretrained(cls, directory, **options):
            calls["processor"] = (directory, options)
            return cls()

        def __call__(self, **options):
            assert options["images"].flags.writeable
            assert options["images"].flags.c_contiguous
            return {"pixel_values": torch.zeros((1, 3, 16, 16))}

    class Model:
        config = types.SimpleNamespace(id2label={0: "floor", 1: "bed"})

        @classmethod
        def from_pretrained(cls, directory, **options):
            calls["model"] = (directory, options)
            return cls()

        def to(self, device):
            calls["device"] = device
            return self

        def eval(self):
            calls["eval"] = True

        def __call__(self, **inputs):
            logits = torch.zeros((1, 2, 4, 4))
            logits[:, 1] = 4
            return types.SimpleNamespace(logits=logits)

    transformer_stub = types.ModuleType("transformers")
    transformer_stub.SegformerImageProcessor = Processor
    transformer_stub.SegformerForSemanticSegmentation = Model
    monkeypatch.setitem(sys.modules, "transformers", transformer_stub)
    readonly_image = np.asarray(Image.fromarray(np.zeros((48, 64, 3), np.uint8)))
    assert not readonly_image.flags.writeable
    semantic, confidence, labels, metadata = subject._infer_semantics(readonly_image, tmp_path)
    assert calls["processor"][1] == {"local_files_only": True}
    assert calls["model"][1] == {"local_files_only": True, "use_safetensors": True}
    assert calls["device"] == "cpu"
    assert calls["eval"] is True
    assert semantic.shape == confidence.shape == (48, 64)
    assert (semantic == 1).all()
    assert labels[1] == "bed"
    assert metadata["working_size"] == [64, 48]


def test_manual_box_selects_object_and_cannot_leak_outside():
    image = np.full((80, 100, 3), (20, 35, 80), np.uint8)
    image[25:55, 30:70] = (250, 240, 230)
    region = subject.grabcut_region(image, (20, 15, 80, 65), "Bed / manual")
    assert region.name == "bed-manual"
    assert region.mask[30:50, 40:60].all()
    assert not region.mask[:15].any()
    assert not region.mask[:, 80:].any()
    assert region.source == "manual-grabcut"


@pytest.mark.parametrize("box", [
    (-1, 1, 10, 10), (1, 1, 30, 10), (5, 5, 5, 7),
    (0, 0, 20, 20), (1.5, 1, 10, 10), (False, 1, 10, 10),
])
def test_manual_box_invalid_boundaries(box):
    with pytest.raises(ValueError):
        subject.grabcut_region(np.zeros((20, 20, 3), np.uint8), box, "bed")


def test_manual_mask_requires_exact_size_and_respects_transparency(tmp_path):
    path = tmp_path / "mask.png"
    mask = np.full((10, 12, 4), 255, np.uint8)
    mask[:3, :, 3] = 0
    mask[:, :4, :3] = 127
    Image.fromarray(mask).save(path)
    loaded = subject.load_mask(path, (12, 10))
    assert loaded.dtype == bool
    assert loaded[3:, 4:].all()
    assert not loaded[:3].any()
    assert not loaded[:, :4].any()
    with pytest.raises(ValueError, match="does not match"):
        subject.load_mask(path, (10, 12))


def test_empty_manual_mask_is_rejected(tmp_path):
    path = tmp_path / "empty.png"
    Image.fromarray(np.zeros((10, 12), np.uint8)).save(path)
    with pytest.raises(ValueError, match="no foreground"):
        subject.load_mask(path, (12, 10))


def test_foreground_unites_classes_before_component_area_filter(monkeypatch):
    image = np.zeros((100, 100, 3), np.uint8)
    semantic = np.zeros((100, 100), np.int32)
    semantic[20:30, 20:35] = 1
    semantic[20:30, 35:50] = 2
    inject_semantics(monkeypatch, semantic)
    mask, diagnostics = subject.automatic_foreground(image, Path("unused"), refine=False)
    # Each semantic part is only 1.5%; their joined 3% component survives 2%.
    assert np.array_equal(mask, semantic > 0)
    assert diagnostics["threshold"]["pixels"] == 200
    assert len(diagnostics["components_before"]) == len(diagnostics["components_after"]) == 1
    assert diagnostics["raw_union_pixels"] == diagnostics["kept_pixels"] == 300


def test_foreground_removes_isolated_small_island_without_expanding_into_floor(monkeypatch):
    image = np.zeros((100, 100, 3), np.uint8)
    semantic = np.zeros((100, 100), np.int32)
    semantic[10:40, 10:40] = 1
    semantic[70:75, 70:75] = 2
    inject_semantics(monkeypatch, semantic)
    mask, diagnostics = subject.automatic_foreground(image, Path("unused"), refine=False)
    assert mask.sum() == 900
    assert not mask[70:75, 70:75].any()
    assert not mask[semantic == 0].any()
    assert diagnostics["removed_regions"][0]["pixels"] == 25
    assert diagnostics["removed_regions"][0]["manual_exemption"] is False


def test_foreground_preserves_tiny_manual_subject_even_without_automatic_subject(monkeypatch):
    image = np.zeros((100, 100, 3), np.uint8)
    inject_semantics(monkeypatch, np.zeros((100, 100), np.int32))
    manual = np.zeros((100, 100), bool)
    manual[50, 50] = True
    original = manual.copy()
    monkeypatch.setattr(subject, "_refine_mask", lambda *args: pytest.fail("manual-only pixels need no refinement"))
    mask, diagnostics = subject.automatic_foreground(image, Path("unused"), preserve_mask=manual)
    assert np.array_equal(mask, original)
    assert np.array_equal(manual, original)
    assert diagnostics["components_after"][0]["manual_exemption"] is True
    assert diagnostics["manual"]["retained_pixels"] == 1
    assert diagnostics["refinement"]["method"] == "manual-only-no-automatic-refinement"


def test_foreground_refines_union_once_without_internal_semantic_seam(monkeypatch):
    image = np.full((80, 100, 3), 10, np.uint8)
    semantic = np.zeros((80, 100), np.int32)
    semantic[15:65, 15:50] = 1
    semantic[15:65, 50:85] = 2
    image[semantic > 0] = 240
    inject_semantics(monkeypatch, semantic)
    original_refine = subject._refine_mask
    calls = []

    def tracked_refine(rgb, mask):
        calls.append(mask.copy())
        return original_refine(rgb, mask)

    monkeypatch.setattr(subject, "_refine_mask", tracked_refine)
    mask, diagnostics = subject.automatic_foreground(image, Path("unused"))
    assert len(calls) == 1
    assert np.array_equal(calls[0], semantic > 0)
    assert mask[20:60, 48:52].all()
    assert diagnostics["refinement"]["method"] == "graphcut-inner-boundary"


def test_foreground_does_not_fill_floor_holes_or_raise_wall(monkeypatch):
    image = np.zeros((80, 100, 3), np.uint8)
    semantic = np.zeros((80, 100), np.int32)
    semantic[15:65, 15:85] = 1
    semantic[35:40, 40:45] = 0
    semantic[:8] = 2
    image[semantic == 1] = 240
    inject_semantics(monkeypatch, semantic, labels={0: "floor", 1: "bed", 2: "wall"})
    mask, diagnostics = subject.automatic_foreground(image, Path("unused"))
    assert not mask[35:40, 40:45].any()
    assert not mask[semantic != 1].any()
    assert diagnostics["hole_filling"] == {"enabled": False, "added_pixels": 0}


def test_foreground_restores_manual_pixels_after_one_refinement(monkeypatch):
    image = np.zeros((100, 100, 3), np.uint8)
    semantic = np.zeros((100, 100), np.int32)
    semantic[10:40, 10:40] = 1
    manual = np.zeros((100, 100), bool)
    manual[70:72, 70:72] = True
    inject_semantics(monkeypatch, semantic)
    calls = []

    def remove_manual(rgb, mask):
        calls.append(1)
        return mask & ~manual, {"method": "test-refinement", "removed_pixels": 4, "added_pixels": 0}

    monkeypatch.setattr(subject, "_refine_mask", remove_manual)
    mask, diagnostics = subject.automatic_foreground(image, Path("unused"), preserve_mask=manual)
    assert len(calls) == 1
    assert mask[manual].all()
    assert diagnostics["manual"]["retained_pixels"] == 4
    assert len(diagnostics["components_after"]) == 2


def test_foreground_rechecks_small_components_severed_by_refinement(monkeypatch):
    image = np.zeros((100, 100, 3), np.uint8)
    semantic = np.zeros((100, 100), np.int32)
    semantic[10:40, 10:40] = 1
    semantic[35:45, 35:45] = 1
    inject_semantics(monkeypatch, semantic)

    def sever_small_piece(rgb, mask):
        refined = mask.copy()
        refined[35:40, 35:40] = False
        refined[35:40, 40:45] = False
        refined[39, 34:45] = False
        return refined, {"method": "test-refinement", "removed_pixels": 51, "added_pixels": 0}

    monkeypatch.setattr(subject, "_refine_mask", sever_small_piece)
    mask, diagnostics = subject.automatic_foreground(image, Path("unused"))
    assert not mask[40:45, 35:45].any()
    assert any(item["stage"] == "after-refinement" for item in diagnostics["removed_regions"])


def test_foreground_no_subject_is_explicit_with_json_diagnostics(monkeypatch):
    import json

    image = np.zeros((40, 40, 3), np.uint8)
    inject_semantics(monkeypatch, np.zeros((40, 40), np.int32))
    with pytest.raises(subject.NoSubjectsError) as error:
        subject.automatic_foreground(image, Path("unused"))
    diagnostics = error.value.diagnostics
    assert diagnostics["raw_union_pixels"] == diagnostics["kept_pixels"] == 0
    json.dumps({key: value for key, value in diagnostics.items() if key != "semantic_map"})


@pytest.mark.parametrize("arguments", [
    {"min_component_fraction": 0}, {"min_component_fraction": 1.1},
    {"min_component_fraction": float("nan")}, {"min_component_fraction": True},
    {"refine": 1}, {"preserve_mask": np.zeros((4, 4), bool)},
    {"preserve_mask": np.zeros((40, 40), np.uint8)},
])
def test_foreground_bad_options_fail_before_inference(monkeypatch, arguments):
    monkeypatch.setattr(subject, "_infer_semantics", lambda *args: pytest.fail("model must not load"))
    with pytest.raises(ValueError):
        subject.automatic_foreground(np.zeros((40, 40, 3), np.uint8), Path("unused"), **arguments)


def test_foreground_empty_image_fails_before_inference(monkeypatch):
    monkeypatch.setattr(subject, "_infer_semantics", lambda *args: pytest.fail("model must not load"))
    with pytest.raises(ValueError):
        subject.automatic_foreground(np.zeros((0, 40, 3), np.uint8), Path("unused"))
