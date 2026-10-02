import io
import zipfile

import pytest

from server.app import import_export_service


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --- cookie Secure flag ---

async def test_cookie_not_secure_when_disabled(client, normal_user):
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert "secure" not in resp.headers["set-cookie"].lower()


@pytest.mark.parametrize("value", [None, "1", "true"])
async def test_cookie_secure_by_default(client, normal_user, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("COLLAB_EDITOR_COOKIE_SECURE")
    else:
        monkeypatch.setenv("COLLAB_EDITOR_COOKIE_SECURE", value)
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert "secure" in resp.headers["set-cookie"].lower()


@pytest.mark.parametrize("value", ["0", "false", "No"])
async def test_cookie_secure_disabled_values(client, normal_user, monkeypatch, value):
    monkeypatch.setenv("COLLAB_EDITOR_COOKIE_SECURE", value)
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert "secure" not in resp.headers["set-cookie"].lower()


# --- body size cap ---

async def test_body_over_content_length_cap_413(client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "100")
    resp = await client.post("/api/login", content=b"x" * 500, headers={"content-type": "application/json"})
    assert resp.status_code == 413


async def test_streamed_body_over_cap_413(client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "100")

    async def gen():
        for _ in range(10):
            yield b"x" * 50

    resp = await client.post("/api/login", content=gen(), headers={"content-type": "application/json"})
    assert resp.status_code == 413


async def test_body_under_cap_ok(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "1000")
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert resp.status_code == 200


# --- import limits ---

async def test_import_too_many_entries_413(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_IMPORT_MAX_ENTRIES", "3")
    data = _zip({f"f{i}.txt": "x" for i in range(4)})
    resp = await user_client.post("/api/import-zip", files={"file": ("big.zip", data, "application/zip")})
    assert resp.status_code == 413


async def test_import_declared_uncompressed_too_large_413(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_IMPORT_MAX_UNCOMPRESSED_BYTES", "1000")
    data = _zip({"a.txt": "a" * 5000})
    resp = await user_client.post("/api/import-zip", files={"file": ("bomb.zip", data, "application/zip")})
    assert resp.status_code == 413


async def test_import_within_limits_ok(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_IMPORT_MAX_ENTRIES", "3")
    data = _zip({"a.txt": "a", "b.txt": "b"})
    resp = await user_client.post("/api/import-zip", files={"file": ("ok.zip", data, "application/zip")})
    assert resp.status_code == 201


def test_import_limit_error_is_exception_type():
    assert issubclass(import_export_service.ImportTooLargeError, Exception)


# --- rate limiting ---

async def test_login_rate_limited_429_with_retry_after(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "3")
    for _ in range(3):
        r = await client.post("/api/login", json={"username": "alice", "password": "bad"})
        assert r.status_code == 401
    r = await client.post("/api/login", json={"username": "alice", "password": "bad"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


async def test_admin_rate_limited(admin_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_ADMIN_RATE_LIMIT", "2")
    assert (await admin_client.get("/api/admin/users")).status_code == 200
    assert (await admin_client.get("/api/admin/users")).status_code == 200
    r = await admin_client.get("/api/admin/users")
    assert r.status_code == 429
    assert "retry-after" in r.headers


async def test_rate_limit_disabled(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "1")
    monkeypatch.setenv("COLLAB_EDITOR_RATE_LIMIT_ENABLED", "0")
    for _ in range(3):
        r = await client.post("/api/login", json={"username": "alice", "password": "bad"})
        assert r.status_code == 401
