"""Local API contract tests. Each test owns a temporary database and media store."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app


WRITE_HEADERS = {"X-MemoVerse-Client": "local"}


def image_bytes(color="red", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (9, 7), color).save(output, format=format)
    return output.getvalue()


def snapshot():
    return {
        "collections": [], "shares": [], "community": [],
        "profile": {"name": "测试用户", "bio": "保留我的收藏"},
        "likedCommunityIds": [],
    }


def state(response):
    assert response.status_code in (200, 201), response.text
    value = response.json()
    assert value["initialized"] is True
    assert isinstance(value["serverId"], str) and value["serverId"]
    assert isinstance(value["revision"], int)
    assert isinstance(value["data"], dict)
    return value


def error(response, status):
    assert response.status_code == status, response.text
    assert isinstance(response.json().get("detail"), str), response.text


def bootstrap(client, data=None, migration_id="test-migration"):
    return client.post("/api/bootstrap", headers=WRITE_HEADERS, json={
        "migrationId": migration_id, "data": data if data is not None else snapshot(),
    })


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, run_worker=False)) as connection:
        state(bootstrap(connection))
        yield connection


def upload(client, content=None, mime="image/png"):
    content = image_bytes() if content is None else content
    response = client.post("/api/media", headers=WRITE_HEADERS,
                           files={"file": ("photo.png", content, mime)})
    assert response.status_code in (200, 201), response.text
    return response.json()


def create_payload(asset_id, title="我的照片"):
    return {
        "id": str(uuid.uuid4()), "title": title, "category": "daily",
        "tags": ["日常"], "note": "这张原图应完整保存", "date": "2026-10-15",
        "art": "cafe", "assetId": asset_id,
    }


def create_collection(client, payload=None):
    payload = payload or create_payload(upload(client)["assetId"])
    result = state(client.post("/api/collections", headers=WRITE_HEADERS, json=payload))
    return next(item for item in result["data"]["collections"] if item["id"] == payload["id"])


def edit_payload(item, **changes):
    result = {key: item[key] for key in ("version", "title", "category", "tags", "note", "date", "art")}
    result.update(changes)
    return result


def get_item(result, item_id):
    return next(item for item in result["data"]["collections"] if item["id"] == item_id)


def test_database_and_original_media_survive_app_reopen(tmp_path):
    original = image_bytes(format="JPEG")
    with TestClient(create_app(data_dir=tmp_path, run_worker=False)) as first:
        initial = state(bootstrap(first))
        asset = upload(first, original, "image/jpeg")
        item = create_collection(first, create_payload(asset["assetId"]))
        saved = state(first.patch("/api/profile", headers=WRITE_HEADERS,
                                  json={"name": "持久化昵称", "bio": "重启后保留"}))
        assert saved["revision"] > initial["revision"]
    with TestClient(create_app(data_dir=tmp_path, run_worker=False)) as second:
        restored = state(second.get("/api/state"))
        assert restored["serverId"] == saved["serverId"]
        assert restored["revision"] >= saved["revision"]
        assert restored["data"]["profile"]["name"] == "持久化昵称"
        assert get_item(restored, item["id"])["assetId"] == asset["assetId"]
        assert second.get(f"/api/media/{asset['assetId']}").content == original


def test_media_preserves_original_hash_and_deduplicates(client):
    original = image_bytes()
    first = upload(client, original)
    second = upload(client, original)
    assert first["assetId"] == second["assetId"]
    assert first["sha256"] == hashlib.sha256(original).hexdigest()
    assert first["size"] == len(original)
    assert (first["width"], first["height"]) == (9, 7)
    assert first["mimeType"] == "image/png"
    response = client.get(f"/api/media/{first['assetId']}")
    assert response.status_code == 200
    assert response.content == original


def test_bootstrap_retry_does_not_overwrite_and_other_id_conflicts(client):
    saved = state(client.patch("/api/profile", headers=WRITE_HEADERS,
                               json={"name": "之后修改的昵称", "bio": "不能被迁移覆盖"}))
    retried = state(bootstrap(client))
    assert retried["data"] == saved["data"]
    assert retried["revision"] == saved["revision"]
    error(bootstrap(client, migration_id="different-migration"), 409)
    assert client.get("/api/state").json()["data"] == saved["data"]


def legacy_item(original):
    return {
        "id": "legacy-image", "title": "旧收藏", "category": "daily",
        "tags": ["照片"], "note": "原有备注", "date": "2026-09-01",
        "addedAt": 1788220800000, "source": "mine", "art": "cafe",
        "customImage": "data:image/png;base64," + base64.b64encode(original).decode(),
        "generation": "pending",
    }


def test_bootstrap_migrates_data_url_without_losing_metadata(tmp_path):
    original = image_bytes("blue")
    data = snapshot()
    old = legacy_item(original)
    data["collections"] = [old]
    with TestClient(create_app(data_dir=tmp_path, run_worker=False)) as connection:
        imported = state(bootstrap(connection, data))
        item = get_item(imported, old["id"])
        for key in ("id", "title", "tags", "date", "addedAt", "note", "source"):
            assert item[key] == old[key]
        assert not item["customImage"].startswith("data:")
        assert connection.get(f"/api/media/{item['assetId']}").content == original
        assert state(bootstrap(connection, data))["data"] == imported["data"]


def test_create_is_idempotent_and_different_payload_conflicts(client):
    payload = create_payload(upload(client)["assetId"])
    first = state(client.post("/api/collections", headers=WRITE_HEADERS, json=payload))
    again = state(client.post("/api/collections", headers=WRITE_HEADERS, json=payload))
    assert len(again["data"]["collections"]) == 1
    assert again["revision"] == first["revision"]
    error(client.post("/api/collections", headers=WRITE_HEADERS,
                      json={**payload, "title": "同 ID 不同输入"}), 409)
    assert client.get("/api/state").json()["data"] == first["data"]


def test_edit_and_delete_reject_stale_versions(client):
    item = create_collection(client)
    updated = state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                                 json=edit_payload(item, title="新标题")))
    current = get_item(updated, item["id"])
    assert current["version"] > item["version"]
    error(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                       json=edit_payload(item, note="过期修改")), 409)
    error(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                         json={"version": item["version"]}), 409)
    assert get_item(client.get("/api/state").json(), item["id"])["title"] == "新标题"
    deleted = state(client.request("DELETE", f"/api/collections/{item['id']}",
                                   headers=WRITE_HEADERS, json={"version": current["version"]}))
    assert not deleted["data"]["collections"]


def test_metadata_edit_reuses_job_but_image_change_creates_new_input(client):
    item = create_collection(client)
    job_id = item["generationJobId"]
    job_response = client.get(f"/api/jobs/{job_id}")
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["collectionId"] == item["id"]
    assert job["status"] != "ready"
    edited = state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                                json=edit_payload(item, title="仅改文字", note="新的备注")))
    current = get_item(edited, item["id"])
    assert current["assetId"] == item["assetId"]
    assert current["generationJobId"] == job_id
    assert client.get(f"/api/jobs/{job_id}").json()["inputRevision"] == job["inputRevision"]
    replacement = upload(client, image_bytes("green"))
    changed = state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                                 json=edit_payload(current, assetId=replacement["assetId"])))
    new_item = get_item(changed, item["id"])
    assert new_item["assetId"] == replacement["assetId"]
    assert new_item["generationJobId"] != job_id
    new_job = client.get(f"/api/jobs/{new_item['generationJobId']}").json()
    assert new_job["inputRevision"] != job["inputRevision"]
    assert new_job["status"] != "ready"


def test_unknown_asset_and_missing_asset_rejected(client):
    error(client.post("/api/collections", headers=WRITE_HEADERS,
                      json=create_payload(str(uuid.uuid4()))), 404)
    payload = create_payload("unused")
    del payload["assetId"]
    error(client.post("/api/collections", headers=WRITE_HEADERS, json=payload), 422)
    assert client.get("/api/state").json()["data"]["collections"] == []


@pytest.mark.parametrize("content,mime", [(b"not a picture", "image/png"),
                                           (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")])
def test_invalid_media_rejected(client, content, mime):
    response = client.post("/api/media", headers=WRITE_HEADERS,
                           files={"file": ("bad.png", content, mime)})
    error(response, 422)


def test_oversized_media_rejected(client):
    content = image_bytes() + b"\0" * (12 * 1024 * 1024)
    error(client.post("/api/media", headers=WRITE_HEADERS,
                      files={"file": ("oversized.png", content, "image/png")}), 413)


def test_mutation_requires_client_header(client):
    before = client.get("/api/state").json()
    error(client.patch("/api/profile", json={"name": "禁止改写", "bio": ""}), 403)
    assert client.get("/api/state").json() == before


def test_untrusted_origin_cannot_mutate(client):
    before = client.get("/api/state").json()
    response = client.patch("/api/profile", headers={**WRITE_HEADERS, "Origin": "https://evil.example"},
                            json={"name": "跨站修改", "bio": ""})
    error(response, 403)
    assert client.get("/api/state").json() == before


def test_backup_zip_includes_original_image_and_metadata(client):
    original = image_bytes("purple")
    item = create_collection(client, create_payload(upload(client, original)["assetId"]))
    response = client.get("/api/backup")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        files = [archive.read(name) for name in archive.namelist() if not name.endswith("/")]
        assert original in files
        metadata = []
        for content in files:
            try:
                metadata.append(json.loads(content))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        assert any(item["id"] in json.dumps(value, ensure_ascii=False) for value in metadata)


def test_received_independent_copy_survives_source_deletion(tmp_path):
    original = image_bytes("orange")
    data = snapshot()
    own = legacy_item(original)
    received = {**own, "id": "friend-independent-copy", "source": "friend", "sender": "小林"}
    data["collections"] = [own]
    data["shares"] = [{"id": "share-1", "item": received, "sender": "小林",
                       "receivedAt": "2026-10-15", "unread": True}]
    with TestClient(create_app(data_dir=tmp_path, run_worker=False)) as connection:
        initialized = state(bootstrap(connection, data))
        source = get_item(initialized, own["id"])
        collected = state(connection.post("/api/shares/share-1/collect", headers=WRITE_HEADERS))
        repeated = state(connection.post("/api/shares/share-1/collect", headers=WRITE_HEADERS))
        assert len(repeated["data"]["collections"]) == len(collected["data"]["collections"]) == 2
        independent = next(item for item in collected["data"]["collections"] if item["id"] != source["id"])
        deleted = state(connection.request("DELETE", f"/api/collections/{source['id']}",
                                           headers=WRITE_HEADERS, json={"version": source["version"]}))
        kept = get_item(deleted, independent["id"])
        assert kept["title"] == received["title"]
        assert connection.get(f"/api/media/{kept['assetId']}").content == original
        assert deleted["data"]["shares"][0]["item"]["title"] == received["title"]


def test_community_like_and_collect_are_idempotent(tmp_path):
    data = snapshot()
    data["community"] = [{
        "id": "community-example", "title": "示例社区条目", "category": "daily",
        "art": "cafe", "author": "示例作者", "description": "示例内容",
        "exposures": 10, "saves": 3, "likes": 4,
    }]
    with TestClient(create_app(data_dir=tmp_path, run_worker=False)) as connection:
        state(bootstrap(connection, data))
        path = "/api/community/community-example"
        for _ in range(2):
            liked = state(connection.put(path + "/like", headers=WRITE_HEADERS, json={"liked": True}))
        assert liked["data"]["community"][0]["likes"] == 5
        assert liked["data"]["likedCommunityIds"] == ["community-example"]
        for _ in range(2):
            collected = state(connection.post(path + "/collect", headers=WRITE_HEADERS))
        assert len(collected["data"]["collections"]) == 1
        assert collected["data"]["community"][0]["saves"] == 4
        assert collected["data"]["collections"][0]["source"] == "friend"


def test_invalid_edit_asset_does_not_damage_saved_collection(client):
    item = create_collection(client)
    before = client.get("/api/state").json()
    error(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                       json=edit_payload(item, assetId=str(uuid.uuid4()))), 404)
    assert client.get("/api/state").json() == before
    assert client.get(f"/api/media/{item['assetId']}").status_code == 200


def test_visual_template_change_creates_new_generation_job(client):
    item = create_collection(client)
    updated = state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                                 json=edit_payload(item, art="sea")))
    changed = get_item(updated, item["id"])
    assert changed["assetId"] == item["assetId"]
    assert changed["generationJobId"] != item["generationJobId"]
