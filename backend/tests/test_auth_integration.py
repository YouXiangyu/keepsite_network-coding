"""Real BFF + schema-2 integration with a signed, offline Cognito fixture."""
from __future__ import annotations

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend import accounts
from backend.auth import AuthConfig, hashed, install_auth
from backend.object_storage import LocalObjectStore
from account_fixtures import image_bytes, template_fixture
from test_auth import FakeProvider


PUBLIC = "https://memoverse.test"
HEADERS = {"Origin": PUBLIC, "X-MemoVerse-Client": "web"}


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def integrated(tmp_path, signing_key):
    template_dir = tmp_path / "template"
    template_fixture(template_dir)
    config = AuthConfig("ap-northeast-1", "ap-northeast-1_fixture", "testclient123",
                        "https://fixture.auth.ap-northeast-1.amazoncognito.com", PUBLIC,
                        Fernet.generate_key().decode(), google_enabled=True)
    clock = lambda: 1800000000.0
    provider = FakeProvider(config, clock, signing_key)

    def signed_cognito_test_factory(app, data_root, *, on_authenticated):
        # This is the real install_auth middleware/router, not the business
        # tests' cookie stub. Only the external identity provider is replaced.
        return install_auth(app, data_root, on_authenticated=on_authenticated,
                            config=config, provider=provider, clock=clock)

    app = create_app(data_dir=tmp_path / "account-data", run_worker=False,
                     auth_provider=signed_cognito_test_factory,
                     object_store=LocalObjectStore(tmp_path / "private-objects"),
                     template_dir=template_dir)
    with TestClient(app, base_url=PUBLIC) as client:
        yield client, app, provider, config


def login(client, provider, name):
    provider.subject = f"subject-{name}"
    provider.email = f"{name}@example.test"
    provider.id_changes = {"name": name.title()}
    response = client.post("/api/auth/login", headers=HEADERS,
                           json={"email": provider.email, "password": "StrongPassword!123"})
    assert response.status_code == 200, response.text
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "__Host-memoverse_session=" in response.headers["set-cookie"]
    return response.json()


def test_real_login_csrf_account_switch_and_logout_protect_old_urls(integrated):
    client, app, provider, config = integrated
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/health").json() == {"status": "ok"}
    alice = login(client, provider, "alice")
    alice_sid = client.cookies.get(app.state.auth.cookie_name)
    a = client.get("/api/state").json()
    assert a["userId"] == alice["user"]["id"] == hashed(config.issuer + "\0subject-alice")
    assert a["data"]["profile"]["name"] == "Alice"
    assert len(a["data"]["collections"]) == len(a["data"]["shares"]) == len(a["data"]["community"]) == 1
    initial = a["data"]["collections"][0]
    assert initial["generation"] == "ready"
    assert client.get(initial["customImage"]).status_code == 200
    assert client.get(initial["modelUrl"]).status_code == 200

    profile = {"name": "Alice only", "bio": "私有资料"}
    assert client.patch("/api/profile", headers=HEADERS, json=profile).status_code == 403
    alice_headers = {**HEADERS, "X-CSRF-Token": alice["csrfToken"]}
    saved = client.patch("/api/profile", headers=alice_headers, json=profile)
    assert saved.status_code == 200 and saved.json()["data"]["profile"] == profile
    private_image = image_bytes("red")
    upload = client.post("/api/media", headers=alice_headers,
                         files={"file": ("private.png", private_image, "image/png")})
    assert upload.status_code == 200, upload.text
    asset = upload.json()
    payload = {"id": "alice-owned", "title": "Alice private", "category": "daily", "tags": [], "note": "private",
               "date": "2026-10-18", "art": "cafe", "assetId": asset["assetId"]}
    created = client.post("/api/collections", headers=alice_headers, json=payload)
    assert created.status_code == 200, created.text
    item = next(row for row in created.json()["data"]["collections"] if row["id"] == payload["id"])

    bob = login(client, provider, "bob")
    b = client.get("/api/state").json()
    assert b["userId"] == bob["user"]["id"] != a["userId"]
    assert b["data"]["profile"]["name"] == "Bob"
    assert not any(row["id"] == item["id"] for row in b["data"]["collections"])
    assert client.get(item["customImage"]).status_code == 404
    assert client.get(f"/api/jobs/{item['generationJobId']}").status_code == 404
    bob_headers = {**HEADERS, "X-CSRF-Token": bob["csrfToken"]}
    assert client.post("/api/collections", headers=bob_headers, json=payload).status_code == 404
    assert client.patch("/api/profile", headers=alice_headers, json=profile).status_code == 403
    # Switching through a successful login rotates the real server-side session.
    assert app.state.auth.session(alice_sid) is None
    # Immutable template objects are intentionally granted to both accounts.
    assert client.get(initial["modelUrl"]).status_code == 200

    response = client.post("/api/auth/logout", headers=bob_headers)
    assert response.status_code == 200, response.text
    assert client.get("/api/auth/session").json()["authenticated"] is False
    for path in ("/api/state", initial["customImage"], initial["modelUrl"], item["customImage"], "/api/backup"):
        assert client.get(path).status_code == 401


def test_real_unverified_identity_never_initializes_business_account(integrated):
    client, app, provider, _ = integrated
    provider.id_changes = {"email_verified": False}
    response = client.post("/api/auth/login", headers=HEADERS,
                           json={"email": provider.email, "password": "StrongPassword!123"})
    assert response.status_code == 403 and response.json()["code"] == "email_unverified"
    assert client.get("/api/state").status_code == 401
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0


def test_failed_workspace_initialization_does_not_leave_authenticated_session(integrated, monkeypatch):
    client, app, provider, _ = integrated
    def unavailable(*args):
        raise RuntimeError("fixture workspace unavailable")
    monkeypatch.setattr(accounts, "ensure_account", unavailable)
    response = client.post("/api/auth/login", headers=HEADERS,
                           json={"email": provider.email, "password": "StrongPassword!123"})
    assert response.status_code == 503 and response.json()["code"] == "workspace_unavailable"
    assert "fixture" not in response.text
    assert client.get("/api/auth/session").json()["authenticated"] is False
    with app.state.auth.db() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
