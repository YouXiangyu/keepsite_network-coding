"""Real BFF and schema-3 integration with signed offline Cognito responses."""
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend import accounts
from backend.auth import AuthConfig, hashed, install_auth
from backend.object_storage import LocalObjectStore
from backend.shared_state import RedisSharedState
from account_fixtures import account_snapshot
from account_fixtures import image_bytes
from test_auth import FakeProvider

PUBLIC = "https://memoverse.test"
HEADERS = {"Origin": PUBLIC, "X-MemoVerse-Client": "web"}


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def integrated(tmp_path, signing_key):
    config = AuthConfig("ap-northeast-1", "ap-northeast-1_fixture", "testclient123",
                        "https://fixture.auth.ap-northeast-1.amazoncognito.com", PUBLIC,
                        Fernet.generate_key().decode(), google_enabled=True)
    clock = lambda: 1800000000.0
    provider = FakeProvider(config, clock, signing_key)
    def signed_cognito_test_factory(app, data_root, *, on_authenticated):
        return install_auth(app, data_root, on_authenticated=on_authenticated,
                            config=config, provider=provider, clock=clock)
    app = create_app(tmp_path / "account-data", auth_provider=signed_cognito_test_factory, run_recovery=False, shared_state=RedisSharedState(),
                     object_store=LocalObjectStore(tmp_path / "objects"))
    with TestClient(app, base_url=PUBLIC) as client:
        yield client, app, provider, config


def login(client, provider, name):
    provider.subject = f"subject-{name}"
    provider.email = f"{name}@example.test"
    provider.id_changes = {"name": name.title()}
    response = client.post("/api/auth/login", headers=HEADERS,
                           json={"email": provider.email, "password": "StrongPassword!123"})
    assert response.status_code == 200, response.text
    assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
    assert "__Host-memoverse_session=" in response.headers["set-cookie"]
    return response.json()


def test_real_login_csrf_account_switch_and_logout_protect_old_urls(integrated):
    client, app, provider, config = integrated
    assert client.get("/api/sync/snapshot").status_code == 401
    alice = login(client, provider, "alice")
    alice_sid = client.cookies.get(app.state.auth.cookie_name)
    a = account_snapshot(client)
    assert a["userId"] == alice["user"]["id"] == hashed(config.issuer + "\0subject-alice")
    assert a["tickets"] == []
    alice_headers = {**HEADERS, "X-CSRF-Token": alice["csrfToken"]}
    upload = client.post("/api/media", headers=alice_headers,
                         data={"uploadId": "alice-photo"},
                         files={"file": ("private.png", image_bytes(), "image/png")})
    assert upload.status_code == 200, upload.text
    asset = upload.json()
    assert client.get(asset["url"]).status_code == 404
    payload = {"id": "alice-owned", "operationId": "create-alice", "originalAssetId": asset["assetId"], "fields": {"title": "Alice private"}}
    assert client.post("/api/tickets", headers=HEADERS, json=payload).status_code == 403
    created = client.post("/api/tickets", headers=alice_headers, json=payload)
    assert created.status_code == 200, created.text
    assert client.get(asset["url"]).status_code == 200
    bob = login(client, provider, "bob")
    b = account_snapshot(client)
    assert b["userId"] == bob["user"]["id"] != a["userId"]
    assert b["tickets"] == []
    assert client.get(asset["url"]).status_code == 404
    bob_headers = {**HEADERS, "X-CSRF-Token": bob["csrfToken"]}
    assert client.post("/api/tickets", headers=bob_headers, json=payload).status_code == 404
    assert client.post("/api/tickets", headers=alice_headers, json=payload).status_code == 403
    assert app.state.auth.session(alice_sid) is None
    response = client.post("/api/auth/logout", headers=bob_headers)
    assert response.status_code == 200
    assert client.get("/api/auth/session").json()["authenticated"] is False
    for path in ("/api/sync/snapshot", asset["url"], "/api/backup"):
        assert client.get(path).status_code == 401


def test_real_unverified_identity_never_initializes_business_account(integrated):
    client, app, provider, _ = integrated
    provider.id_changes = {"email_verified": False}
    response = client.post("/api/auth/login", headers=HEADERS,
                           json={"email": provider.email, "password": "StrongPassword!123"})
    assert response.status_code == 403 and response.json()["code"] == "email_unverified"
    assert client.get("/api/sync/snapshot").status_code == 401
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


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
