import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
import uvicorn
import websockets
from pycrdt import Doc, Text, YMessageType, create_sync_message, create_update_message, handle_sync_message

from server.app.db import AsyncSessionLocal
from server.app.main import app
from server.app.routers.sync import CLOSE_FORBIDDEN, TEXT_KEY
from server.app.user_service import create_user

SHARE_PORT = 8768


async def _make_doc(client, name="shared.txt") -> str:
    resp = await client.post("/api/documents", json={"name": name, "parent_id": None})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _make_link(client, doc_id, read_only=False) -> dict:
    resp = await client.post(f"/api/documents/{doc_id}/share-links", json={"read_only": read_only})
    assert resp.status_code == 201
    return resp.json()


@asynccontextmanager
async def _guest_client(token, name="Guest"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as guest:
        resp = await guest.post(f"/api/share/{token}/join", json={"display_name": name})
        assert resp.status_code == 200, resp.text
        yield guest


# --- REST: link management, join, guest restrictions ---


async def test_create_list_revoke(user_client):
    doc_id = await _make_doc(user_client)
    link = await _make_link(user_client, doc_id, read_only=True)
    assert link["read_only"] is True and link["doc_id"] == doc_id and len(link["token"]) >= 32

    listed = (await user_client.get(f"/api/documents/{doc_id}/share-links")).json()
    assert [x["token"] for x in listed] == [link["token"]]

    resp = await user_client.delete(f"/api/documents/{doc_id}/share-links/{link['token']}")
    assert resp.status_code == 204
    assert (await user_client.get(f"/api/documents/{doc_id}/share-links")).json() == []
    assert (await user_client.delete(f"/api/documents/{doc_id}/share-links/{link['token']}")).status_code == 404


async def test_create_link_unknown_doc_404(user_client):
    resp = await user_client.post("/api/documents/nope/share-links", json={})
    assert resp.status_code == 404


async def test_join_sets_guest_session_and_me(user_client):
    doc_id = await _make_doc(user_client)
    link = await _make_link(user_client, doc_id, read_only=True)
    async with _guest_client(link["token"], name="  Gina  ") as guest:
        me = (await guest.get("/api/me")).json()
        assert me["is_guest"] is True
        assert me["guest_doc_id"] == doc_id
        assert me["guest_read_only"] is True
        assert me["display_name"] == "Gina"
        assert me["username"].startswith("guest-")
        assert me["is_admin"] is False


async def test_member_can_resolve_link_but_guest_cannot(user_client):
    doc_id = await _make_doc(user_client, "named.txt")
    link = await _make_link(user_client, doc_id)
    resp = await user_client.get(f"/api/share/{link['token']}")
    assert resp.status_code == 200 and resp.json()["doc_id"] == doc_id
    assert (await user_client.get("/api/share/nope")).status_code == 404
    async with _guest_client(link["token"]) as guest:
        assert (await guest.get(f"/api/share/{link['token']}")).status_code == 403
        assert (await guest.get("/api/me")).json()["guest_doc_name"] == "named.txt"


async def test_join_bad_token_and_bad_name(client):
    resp = await client.post("/api/share/nope/join", json={"display_name": "x"})
    assert resp.status_code == 404


async def test_join_validates_display_name(user_client):
    doc_id = await _make_doc(user_client)
    token = (await _make_link(user_client, doc_id))["token"]
    for bad in ("", "   ", "x" * 65):
        resp = await user_client.post(f"/api/share/{token}/join", json={"display_name": bad})
        assert resp.status_code == 422


async def test_join_rate_limited(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_JOIN_RATE_LIMIT", "2")
    doc_id = await _make_doc(user_client)
    token = (await _make_link(user_client, doc_id))["token"]
    codes = [(await user_client.post(f"/api/share/{token}/join", json={"display_name": "g"})).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_revoked_link_cannot_join_and_guest_session_dies(user_client):
    doc_id = await _make_doc(user_client)
    link = await _make_link(user_client, doc_id)
    async with _guest_client(link["token"]) as guest:
        assert (await guest.get("/api/me")).status_code == 200
        await user_client.delete(f"/api/documents/{doc_id}/share-links/{link['token']}")
        assert (await guest.get("/api/me")).status_code == 401
    resp = await user_client.post(f"/api/share/{link['token']}/join", json={"display_name": "late"})
    assert resp.status_code == 404


async def test_guest_blocked_on_member_routes(user_client):
    doc_id = await _make_doc(user_client)
    other_id = await _make_doc(user_client, "other.txt")
    link = await _make_link(user_client, doc_id)
    async with _guest_client(link["token"]) as guest:
        blocked = [
            ("GET", "/api/tree", None),
            ("POST", "/api/folders", {"name": "f", "parent_id": None}),
            ("POST", "/api/documents", {"name": "d", "parent_id": None}),
            ("GET", f"/api/documents/{doc_id}/content", None),
            ("PUT", f"/api/documents/{doc_id}/content", {"content": "x"}),
            ("PATCH", f"/api/nodes/{doc_id}", {"name": "z"}),
            ("DELETE", f"/api/nodes/{doc_id}", None),
            ("GET", "/api/export-zip", None),
            ("GET", "/api/presence", None),
            ("GET", f"/api/documents/{other_id}/chat", None),
            ("GET", f"/api/documents/{doc_id}/share-links", None),
            ("POST", f"/api/documents/{doc_id}/share-links", {}),
            ("DELETE", f"/api/documents/{doc_id}/share-links/{link['token']}", None),
            ("GET", "/api/admin/users", None),
        ]
        for method, url, body in blocked:
            resp = await guest.request(method, url, json=body)
            assert resp.status_code in (403,), (method, url, resp.status_code)
        resp = await guest.post("/api/import-zip", files={"file": ("a.zip", b"x")})
        assert resp.status_code == 403
        # Allowed: chat history for their own doc.
        assert (await guest.get(f"/api/documents/{doc_id}/chat")).status_code == 200


async def test_admin_user_list_hides_guests(admin_client):
    doc_id = await _make_doc(admin_client)
    link = await _make_link(admin_client, doc_id)
    async with _guest_client(link["token"]):
        pass
    users = (await admin_client.get("/api/admin/users")).json()
    assert [u["username"] for u in users] == ["admin"]


# --- WebSocket enforcement (live server, same pattern as test_chat.py) ---


@pytest_asyncio.fixture
async def live_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=SHARE_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield server
    server.should_exit = True
    await task


BASE = f"http://127.0.0.1:{SHARE_PORT}"


@asynccontextmanager
async def _member():
    async with AsyncSessionLocal() as db:
        await create_user(db, "alice", "Alice", "alicepass123")
    async with httpx.AsyncClient(base_url=BASE) as client:
        resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
        assert resp.status_code == 200
        yield client


async def _join(token) -> str:
    async with httpx.AsyncClient(base_url=BASE) as c:
        resp = await c.post(f"/api/share/{token}/join", json={"display_name": "Gina"})
        assert resp.status_code == 200
        return resp.cookies["session_token"]


async def _connect(cookie, doc_id):
    doc = Doc()
    text = doc.get(TEXT_KEY, type=Text)
    ws = await websockets.connect(
        f"ws://127.0.0.1:{SHARE_PORT}/ws/doc/{doc_id}", additional_headers={"Cookie": f"session_token={cookie}"}
    )
    await ws.send(create_sync_message(doc))
    for _ in range(5):
        frame = await asyncio.wait_for(ws.recv(), timeout=5)
        if frame[0] == YMessageType.SYNC:
            handle_sync_message(frame[1:], doc)
            if frame[1] == 1:
                break
    return ws, doc, text


async def _send_edit(ws, doc, text, suffix):
    captured = []
    sub = doc.observe(lambda e: captured.append(e.update))
    with doc.transaction():
        text += suffix
    sub.drop()
    await ws.send(create_update_message(captured[0]))


async def _wait_text(ws, doc, text, expected, timeout=3) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while str(text) != expected:
                frame = await ws.recv()
                if frame[0] == YMessageType.SYNC:
                    handle_sync_message(frame[1:], doc)
    except TimeoutError:
        return False
    return True


async def test_guest_wrong_doc_ws_closed(live_server):
    async with _member() as member:
        doc_id = await _make_doc(member)
        other_id = await _make_doc(member, "other.txt")
        cookie = await _join((await _make_link(member, doc_id))["token"])
        ws = await websockets.connect(
            f"ws://127.0.0.1:{SHARE_PORT}/ws/doc/{other_id}", additional_headers={"Cookie": f"session_token={cookie}"}
        )
        with pytest.raises(websockets.ConnectionClosed) as exc:
            await asyncio.wait_for(ws.recv(), timeout=5)
        assert exc.value.rcvd.code == CLOSE_FORBIDDEN


async def test_edit_guest_syncs_to_member_and_view_guest_cannot_edit(live_server):
    async with _member() as member:
        doc_id = await _make_doc(member)
        edit_cookie = await _join((await _make_link(member, doc_id))["token"])
        view_cookie = await _join((await _make_link(member, doc_id, read_only=True))["token"])
        member_cookie = member.cookies["session_token"]

        m_ws, m_doc, m_text = await _connect(member_cookie, doc_id)
        e_ws, e_doc, e_text = await _connect(edit_cookie, doc_id)
        v_ws, v_doc, v_text = await _connect(view_cookie, doc_id)

        await _send_edit(v_ws, v_doc, v_text, "VIEWER")
        assert not await _wait_text(m_ws, m_doc, m_text, "VIEWER", timeout=1.5)
        assert str(m_text) == ""

        await _send_edit(e_ws, e_doc, e_text, "EDITOR")
        assert await _wait_text(m_ws, m_doc, m_text, "EDITOR")

        for ws in (m_ws, e_ws, v_ws):
            await ws.close()


async def test_revoke_closes_live_guest_socket(live_server):
    async with _member() as member:
        doc_id = await _make_doc(member)
        link = await _make_link(member, doc_id)
        cookie = await _join(link["token"])
        ws, _doc, _text = await _connect(cookie, doc_id)

        resp = await member.delete(f"/api/documents/{doc_id}/share-links/{link['token']}")
        assert resp.status_code == 204
        with pytest.raises(websockets.ConnectionClosed) as exc:
            async with asyncio.timeout(5):
                while True:
                    await ws.recv()
        assert exc.value.rcvd.code == CLOSE_FORBIDDEN
