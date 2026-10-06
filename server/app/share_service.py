from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.app import node_service
from server.app.models import GuestGrant, Node, Session, ShareLink, User
from server.app.routers.sync import kick_users

# Same name the client uses for its auto-created root "Trash" folder.
TRASH_FOLDER_NAME = "Trash"


async def delete_share_link(db: AsyncSession, token: str) -> list[int]:
    """Remove a link and cut off the guests it created: their sessions are deleted and
    the users deactivated. User and grant rows are kept so ids are never reused (chat
    messages reference them) and the guests stay hidden from the admin list.
    Returns the guest user ids so callers can close live connections."""
    guest_ids = list((await db.execute(select(GuestGrant.user_id).where(GuestGrant.link_id == token))).scalars())
    if guest_ids:
        await db.execute(delete(Session).where(Session.user_id.in_(guest_ids)))
        await db.execute(update(User).where(User.id.in_(guest_ids)).values(is_active=False))
    await db.execute(delete(ShareLink).where(ShareLink.token == token))
    await db.commit()
    return guest_ids


async def end_sharing_if_trashed(db: AsyncSession, node: Node) -> None:
    """Revoke every link on the documents under `node` once it sits inside the Trash folder."""
    path = await node_service.get_ancestor_path(db, node)
    if not path or path[0] != TRASH_FOLDER_NAME:
        return
    doc_ids: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.kind == "document":
            doc_ids.append(current.id)
        else:
            children = await db.execute(select(Node).where(Node.parent_id == current.id))
            stack.extend(children.scalars())
    if not doc_ids:
        return
    tokens = (await db.execute(select(ShareLink.token).where(ShareLink.doc_id.in_(doc_ids)))).scalars().all()
    guest_ids: list[int] = []
    for token in tokens:
        guest_ids += await delete_share_link(db, token)
    await kick_users(guest_ids)
