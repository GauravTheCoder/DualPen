import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from server.app import auth
from server.app.db import AsyncSessionLocal
from server.app.models import GuestGrant, Node, Session, ShareLink, User
from server.app.user_service import create_user


async def _make_guest(db, owner):
    doc = Node(name="d", kind="document")
    db.add(doc)
    await db.commit()
    db.add(ShareLink(token="tok", doc_id=doc.id, read_only=True, created_by=owner.id))
    guest = await create_user(db, "guest-1", "Guest", "x" * 20)
    db.add(GuestGrant(user_id=guest.id, doc_id=doc.id, read_only=True, link_id="tok"))
    await db.commit()
    return doc, guest


async def test_grant_lookup_and_require_member(normal_user):
    async with AsyncSessionLocal() as db:
        _, guest = await _make_guest(db, normal_user)
        grant = await auth.get_guest_grant(db, guest)
        assert grant.read_only and grant.link_id == "tok"
        assert await auth.get_guest_grant(db, normal_user) is None
        with pytest.raises(HTTPException) as exc:
            await auth.require_member(guest, db)
        assert exc.value.status_code == 403
        assert await auth.require_member(normal_user, db) is normal_user


async def test_session_lifetime(normal_user):
    async with AsyncSessionLocal() as db:
        default = await auth.create_session(db, normal_user)
        guest = await auth.create_session(db, normal_user, auth.GUEST_SESSION_LIFETIME)
        assert guest.expires_at < default.expires_at
        delta = guest.expires_at - datetime.datetime.now(datetime.timezone.utc)
        assert datetime.timedelta(hours=23) < delta <= datetime.timedelta(hours=24)


async def test_delete_link_cleans_guests(normal_user):
    async with AsyncSessionLocal() as db:
        _, guest = await _make_guest(db, normal_user)
        await auth.create_session(db, guest, auth.GUEST_SESSION_LIFETIME)
        await auth.delete_share_link(db, "tok")
        for model in (ShareLink, GuestGrant, Session):
            assert (await db.execute(select(model))).first() is None
        assert (await db.execute(select(User).where(User.id == guest.id))).first() is None
        assert (await db.execute(select(User).where(User.id == normal_user.id))).first() is not None
