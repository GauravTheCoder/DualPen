import pytest
from sqlalchemy import select

from server.app.db import AsyncSessionLocal
from server.app.models import User
from server.cli import create_admin


async def test_create_admin_creates_admin_user():
    await create_admin("clibootstrap", "CLI Bootstrap", "clipass1234")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == "clibootstrap"))
        user = result.scalar_one()
        assert user.is_admin is True
        assert user.display_name == "CLI Bootstrap"


async def test_create_admin_refuses_duplicate_username():
    await create_admin("dupeadmin", "Dupe Admin", "pass1234567")

    with pytest.raises(SystemExit):
        await create_admin("dupeadmin", "Dupe Admin 2", "pass7654321")


def _run_cli(monkeypatch, argv, stdin=None):
    import io
    import sys

    from server import cli

    monkeypatch.setattr(sys, "argv", ["server.cli", *argv])
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    cli.main()


async def test_cli_noninteractive_flags_stdin(monkeypatch):
    # main() uses asyncio.run, so run it off the test's event loop.
    import asyncio

    await asyncio.to_thread(
        _run_cli, monkeypatch, ["create-admin", "--username", "flagadmin", "--password-stdin"], "flagpass1234\n"
    )
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "flagadmin"))).scalar_one()
        assert user.is_admin is True


async def test_cli_noninteractive_env(monkeypatch):
    import asyncio

    monkeypatch.setenv("COLLAB_EDITOR_ADMIN_USERNAME", "envadmin")
    monkeypatch.setenv("COLLAB_EDITOR_ADMIN_PASSWORD", "envpass12345")
    await asyncio.to_thread(_run_cli, monkeypatch, ["create-admin"])
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(User).where(User.username == "envadmin"))).scalar_one()


def test_cli_noninteractive_rejects_empty_password(monkeypatch):
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, ["create-admin", "--username", "x", "--password-stdin"], "\n")


def test_cli_noninteractive_rejects_missing_password(monkeypatch):
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, ["create-admin", "--username", "x"])
