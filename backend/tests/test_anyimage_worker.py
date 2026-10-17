"""Real image preparation checks; the optional ORT test loads the pinned model."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps, JpegImagePlugin
import pytest

from backend import anyimage_worker as worker


COLORS = {
    'red': (240, 10, 10), 'green': (10, 220, 10),
    'blue': (10, 10, 240), 'yellow': (230, 220, 10),
}


@pytest.mark.parametrize(('orientation', 'corners'), [
    (1, ('red', 'green', 'blue', 'yellow')),
    (2, ('green', 'red', 'yellow', 'blue')),
    (3, ('yellow', 'blue', 'green', 'red')),
    (4, ('blue', 'yellow', 'red', 'green')),
    (5, ('red', 'blue', 'green', 'yellow')),
    (6, ('blue', 'red', 'yellow', 'green')),
    (7, ('yellow', 'green', 'blue', 'red')),
    (8, ('green', 'yellow', 'red', 'blue')),
])
def test_exif_orientation_is_applied_once_and_small_images_are_not_enlarged(tmp_path, orientation, corners):
    source = tmp_path / f'orientation-{orientation}.jpg'
    with Image.new('RGB', (120, 80)) as image:
        draw = ImageDraw.Draw(image)
        for color, box in zip(('red', 'green', 'blue', 'yellow'),
                              ((0, 0, 59, 39), (60, 0, 119, 39), (0, 40, 59, 79), (60, 40, 119, 79))):
            draw.rectangle(box, fill=COLORS[color])
        exif = Image.Exif()
        exif[274] = orientation
        image.save(source, quality=100, subsampling=0, exif=exif)
    with worker.prepare_texture(source) as texture:
        assert texture.mode == 'RGBA'
        assert texture.size == ((80, 120) if orientation >= 5 else (120, 80))
        assert texture.getexif().get(274) in (None, 1)
        coordinates = [(texture.width // 4, texture.height // 4),
                       (texture.width * 3 // 4, texture.height // 4),
                       (texture.width // 4, texture.height * 3 // 4),
                       (texture.width * 3 // 4, texture.height * 3 // 4)]
        for coordinate, color in zip(coordinates, corners):
            actual = texture.getpixel(coordinate)
            assert max(abs(a - b) for a, b in zip(actual[:3], COLORS[color])) <= 3
            assert actual[3] == 255
    # Preparation closes the source file, including on Windows.
    source.rename(source.with_suffix('.closed.jpg'))


def test_large_image_is_bounded_before_exif_copy_and_rgba_conversion(tmp_path, monkeypatch):
    source = tmp_path / 'large.png'
    with Image.new('RGB', (6000, 4000), 'red') as image:
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)
    transpose, convert = ImageOps.exif_transpose, Image.Image.convert
    observed = []

    def bounded_transpose(image, *args, **kwargs):
        observed.append(('exif', image.size))
        assert max(image.size) <= 1536
        return transpose(image, *args, **kwargs)

    def bounded_convert(image, mode=None, *args, **kwargs):
        if mode == 'RGBA':
            observed.append(('rgba', image.size))
            assert max(image.size) <= 1536
        return convert(image, mode, *args, **kwargs)

    monkeypatch.setattr(ImageOps, 'exif_transpose', bounded_transpose)
    monkeypatch.setattr(Image.Image, 'convert', bounded_convert)
    with worker.prepare_texture(source) as texture:
        assert texture.size == (1024, 1536)
    assert ('exif', (1536, 1024)) in observed
    assert ('rgba', (1024, 1536)) in observed


def test_large_jpeg_uses_decoder_downsampling_before_loading_texture(tmp_path, monkeypatch):
    source = tmp_path / 'large.jpg'
    with Image.new('RGB', (8000, 5000), 'navy') as image:
        image.save(source)
    draft = JpegImagePlugin.JpegImageFile.draft
    reductions = []

    def track_draft(image, *args, **kwargs):
        before = image.size
        result = draft(image, *args, **kwargs)
        reductions.append((before, image.size))
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, 'draft', track_draft)
    with worker.prepare_texture(source) as texture:
        assert texture.size == (1536, 960)
    assert any(after[0] < before[0] and after[1] < before[1] for before, after in reductions)


def test_transparent_rgba_png_retains_transparent_semitransparent_and_opaque_regions(tmp_path):
    source = tmp_path / 'alpha.png'
    with Image.new('RGBA', (2048, 1024), (255, 20, 20, 0)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((700, 0, 1350, 1023), fill=(20, 255, 20, 128))
        draw.rectangle((1351, 0, 2047, 1023), fill=(20, 20, 255, 255))
        image.save(source)
    with worker.prepare_texture(source) as texture:
        assert texture.size == (1536, 768)
        assert texture.getpixel((100, 200))[3] == 0
        assert texture.getpixel((750, 200))[3] == 128
        assert texture.getpixel((1400, 200))[3] == 255


def test_palette_transparency_is_preserved_when_conversion_follows_thumbnail(tmp_path):
    source = tmp_path / 'palette.png'
    with Image.new('P', (2048, 1024), 0) as image:
        image.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * (768 - 9))
        draw = ImageDraw.Draw(image)
        draw.rectangle((700, 0, 1350, 1023), fill=1)
        draw.rectangle((1351, 0, 2047, 1023), fill=2)
        image.save(source, transparency=bytes((0, 128, 255)))
    with worker.prepare_texture(source) as texture:
        assert texture.mode == 'RGBA' and texture.size == (1536, 768)
        assert texture.getpixel((100, 200)) == (255, 0, 0, 0)
        assert texture.getpixel((750, 200)) == (0, 255, 0, 128)
        assert texture.getpixel((1400, 200)) == (0, 0, 255, 255)


def test_pinned_cpu_session_uses_explicit_threads_and_no_spinning(monkeypatch):
    model_dir = worker.ROOT / '.cache/models/moge-2-vits-normal-onnx'
    if not (model_dir / 'model.onnx').is_file() or importlib.util.find_spec('onnxruntime') is None:
        pytest.skip('Pinned local ONNX runtime/model is not installed')
    import onnxruntime

    monkeypatch.delenv('MEMOVERSE_ORT_THREADS', raising=False)
    session = worker.create_cpu_session(model_dir)
    options = session.get_session_options()
    assert session.get_providers() == ['CPUExecutionProvider']
    assert options.intra_op_num_threads == options.inter_op_num_threads == 1
    assert options.execution_mode == onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    assert options.get_session_config_entry('session.intra_op.allow_spinning') == '0'
    assert options.get_session_config_entry('session.inter_op.allow_spinning') == '0'


@pytest.mark.parametrize('threads', ['0', '-1', '33', 'invalid'])
def test_invalid_thread_counts_fail_before_model_loading(monkeypatch, threads):
    monkeypatch.setenv('MEMOVERSE_ORT_THREADS', threads)
    with pytest.raises(ValueError):
        worker.create_cpu_session(Path('missing-model-directory'))
