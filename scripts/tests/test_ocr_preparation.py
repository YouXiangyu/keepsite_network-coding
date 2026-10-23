import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("prepare_ocr", ROOT / "scripts/prepare-ocr.py")
prepare_ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_ocr)


def test_default_plan_does_not_create_directories(tmp_path):
    destination = tmp_path / "not-created"
    prepare_ocr.prepare(destination)
    assert not destination.exists()


def test_bad_download_does_not_publish_or_leave_partial_file(tmp_path, monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            return BytesIO(b"wrong model")

    monkeypatch.setattr(prepare_ocr.urllib.request, "build_opener", lambda *args: Opener())
    destination = tmp_path / "models"
    with pytest.raises(ValueError, match="SHA-256"):
        prepare_ocr.prepare(destination, True)
    assert list(destination.iterdir()) == []


def test_existing_different_model_is_preserved(tmp_path, monkeypatch):
    manifest = json.loads(prepare_ocr.MANIFEST.read_text())
    model = next(iter(manifest["models"].values()))
    existing = tmp_path / model["filename"]
    existing.write_bytes(b"an earlier independent artifact")
    expected = hashlib.sha256(existing.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="not overwritten"):
        prepare_ocr.prepare(tmp_path, True)
    assert hashlib.sha256(existing.read_bytes()).hexdigest() == expected


def test_https_redirect_cannot_downgrade():
    redirect = prepare_ocr.HTTPSRedirect()
    with pytest.raises(ValueError, match="HTTPS"):
        redirect.redirect_request(None, None, 302, "redirect", {}, "http://example.test/model")


def test_dictionary_write_failure_never_publishes_partial_file(tmp_path, monkeypatch):
    destination = tmp_path / "characters.txt"

    def fail_sync(_):
        raise OSError("simulated flush failure")

    monkeypatch.setattr(prepare_ocr.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="flush failure"):
        prepare_ocr.publish_dictionary(destination, b"verified dictionary")
    assert list(tmp_path.iterdir()) == []


def test_dictionary_concurrent_destination_is_not_replaced(tmp_path, monkeypatch):
    destination = tmp_path / "characters.txt"

    def concurrent_publication(source, target):
        Path(target).write_bytes(b"another preparation")
        raise FileExistsError("destination appeared")

    monkeypatch.setattr(prepare_ocr.os, "link", concurrent_publication)
    with pytest.raises(FileExistsError):
        prepare_ocr.publish_dictionary(destination, b"verified dictionary")
    assert destination.read_bytes() == b"another preparation"
    assert list(tmp_path.iterdir()) == [destination]
