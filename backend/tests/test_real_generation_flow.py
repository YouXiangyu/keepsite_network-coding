"""Opt-in full CPU inference flow using a test-owned image and isolated stores."""
import io
import math
import os

import pytest
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.backups import restore_account_backup
from backend.object_storage import LocalObjectStore
from backend.relief_engine import engine_status
from account_fixtures import FakeAuthFactory, ORIGIN, WRITE_HEADERS, signed_in
from test_api import build_app, create_collection, create_payload, edit_payload, get_item, state, upload


def scene_image():
    with Image.new("RGB", (256, 256)) as image:
        pixels = image.load()
        for y in range(256):
            for x in range(256):
                if y < 140:
                    pixels[x, y] = (100 + y // 3, 150 + y // 5, 220)
                else:
                    shade = 45 * ((x // 25 + y // 20) % 2)
                    pixels[x, y] = (130 + shade, 110 + shade, 90 + shade)
        draw = ImageDraw.Draw(image)
        draw.polygon([(30, 95), (100, 70), (110, 175), (30, 195)], fill=(160, 70, 40))
        draw.polygon([(100, 70), (140, 95), (145, 185), (110, 175)], fill=(110, 40, 20))
        draw.ellipse((140, 175, 240, 212), fill=(65, 65, 60))
        for radius in range(46, 0, -1):
            light = int(75 + 150 * math.sqrt(1 - (radius / 47) ** 2))
            draw.ellipse((190 - radius, 150 - radius, 190 + radius, 150 + radius), fill=(light, light, 70))
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()


def test_real_upload_generate_restart_export_restore_and_two_account_isolation(tmp_path):
    if os.environ.get("MEMOVERSE_RUN_REAL_GENERATION") != "1":
        pytest.skip("Set MEMOVERSE_RUN_REAL_GENERATION=1 to run the installed CPU model")
    assert engine_status()["available"], engine_status()["reason"]
    app, _ = build_app(tmp_path)
    raw = scene_image()
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        asset = upload(client, raw)
        item = create_collection(client, create_payload(asset["assetId"], title="真实推理与恢复"))
        assert app.state.worker.run_one()
        ready = get_item(state(client.get("/api/state")), item["id"])
        assert ready["generation"] == "ready", ready.get("generationError")
        original = client.get(ready["customImage"]).content
        glb = client.get(ready["modelUrl"]).content
        assert original == raw and glb[:4] == b"glTF" and len(glb) > 1024
        response = client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                                json=edit_payload(ready, note="生成后保存的文字"))
        changed = get_item(state(response), item["id"])
        assert changed["modelUrl"] == ready["modelUrl"]
        assert client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                            json=edit_payload(ready, note="过时版本不能覆盖")).status_code == 409
        archive = tmp_path / "real-account.zip"
        backup = client.get("/api/backup")
        assert backup.status_code == 200
        archive.write_bytes(backup.content)
        signed_in(client, "bob")
        state(client.get("/api/state"))
        for url in (ready["customImage"], ready["modelUrl"], f"/api/jobs/{ready['generationJobId']}"):
            assert client.get(url).status_code == 404
    # A new app and session exercise persisted state, rather than a JS snapshot.
    restarted, _ = build_app(tmp_path)
    with TestClient(restarted, base_url=ORIGIN) as client:
        signed_in(client)
        persisted = get_item(state(client.get("/api/state")), item["id"])
        assert persisted == changed
        assert client.get(persisted["modelUrl"]).content == glb
    destination = tmp_path / "restored-real"
    result = restore_account_backup(archive, destination, user_id="user-alice")
    assert result["verified"]
    restored = create_app(destination, run_worker=False, auth_provider=FakeAuthFactory(),
                          object_store=LocalObjectStore(destination / "objects"), template_dir=tmp_path / "template")
    with TestClient(restored, base_url=ORIGIN) as client:
        signed_in(client)
        after = get_item(state(client.get("/api/state")), item["id"])
        assert after == changed
        assert client.get(after["customImage"]).content == original
        assert client.get(after["modelUrl"]).content == glb
        assert client.get(f"/api/jobs/{after['generationJobId']}").json()["status"] == "ready"
