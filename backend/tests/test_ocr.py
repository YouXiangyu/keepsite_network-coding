import os
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont

from backend import ocr


def test_only_reliable_movie_fields_are_returned():
    values = ocr.parse_fields([("片名：流浪地球", .99), ("影院：星光影城", .98),
                               ("3号厅", .99), ("08排012座", .97),
                               ("放映时间：2026年10月22日 19:30", .99),
                               ("会员电话: 13812345678", .99)])
    assert values == {"title": "流浪地球", "cinema": "星光影城", "hall": "3号厅",
                      "seat": "8排12座", "showtime": "2026-10-22T19:30"}


def test_ambiguous_low_confidence_and_non_show_dates_stay_empty():
    assert ocr.parse_fields([("片名: 甲片", .99), ("片名: 乙片", .99),
                             ("影院: 星光影城", .84), ("购买时间: 2026-10-22 18:00", .99),
                             ("放映: 2026-02-30 19:30", .99)]) == {}


def test_english_and_missing_year():
    assert ocr.parse_fields([("Film: Metropolis", .99), ("Cinema: Aurora Cinema", .99),
                             ("Screen: 2", .99), ("Seat: G12", .99),
                             ("Show: 10-22 19:30", .99)]) == {
        "title": "Metropolis", "cinema": "Aurora Cinema", "hall": "2", "seat": "G12"}


def test_availability_never_downloads_or_loads_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEPSITE_OCR_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(ocr, "_load_engine", lambda *args: (_ for _ in ()).throw(AssertionError("engine loaded")))
    assert ocr.availability()["available"] is False


def test_wrong_hash_is_rejected(monkeypatch, tmp_path):
    for model in [*ocr.MANIFEST["models"].values(), ocr.MANIFEST["dictionary"]]:
        (tmp_path / model["filename"]).write_bytes(b"not the fixed model")
    monkeypatch.setenv("KEEPSITE_OCR_MODEL_DIR", str(tmp_path))
    assert ocr.availability()["available"] is False


def test_labelled_seat_normalization_is_not_an_ambiguity():
    assert ocr.parse_fields([("座位: 08排012座", .99)]) == {"seat": "8排12座"}


@pytest.mark.parametrize("lines", [
    [("购买时间", .99), ("2026-10-22 18:00", .99)],
    [("PRINT", .99), ("2026-10-22 18:00", .99)],
    [("2026-10-22 18:00", .99)],
    [("放映时间", .99), ("2026-10-22 18:00", .99)],
    [("Showtime: 2026-10-22 18:00 / 2026-10-23 19:00", .99)],
])
def test_bare_split_or_ambiguous_dates_are_not_showtime(lines):
    assert ocr.parse_fields(lines) == {}


def test_real_pinned_model_without_any_runtime_download(monkeypatch):
    directory = os.environ.get("KEEPSITE_TEST_OCR_MODEL_DIR")
    if not directory:
        if os.environ.get("CI"):
            pytest.fail("CI must prepare and test the pinned OCR models")
        pytest.skip("Real OCR model directory not configured")
    monkeypatch.setenv("KEEPSITE_OCR_MODEL_DIR", directory)
    from rapidocr.utils.download_file import DownloadFile

    def no_download(*args, **kwargs):
        raise AssertionError("Runtime tried to download an OCR resource")

    monkeypatch.setattr(DownloadFile, "run", no_download)
    monkeypatch.setattr(ocr, "_engine", None)
    picture = Image.new("RGB", (850, 400), "white")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.load_default(size=32)
    draw.multiline_text((30, 30), "Movie: Metropolis\nCinema: Aurora Cinema\nScreen: 3\nSeat: G12\nShowtime: 2026-10-22 19:30",
                        font=font, fill="black", spacing=20)
    output = BytesIO()
    picture.save(output, format="PNG")
    picture.close()
    assert ocr.recognize(output.getvalue()) == {
        "title": "Metropolis", "cinema": "Aurora Cinema", "hall": "3", "seat": "G12", "showtime": "2026-10-22T19:30"}
