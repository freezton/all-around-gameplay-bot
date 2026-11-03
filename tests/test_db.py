from __future__ import annotations

from datetime import UTC, datetime

from all_around_bot.db import MessageStore, StoredMessage


def _message(message_id: int, *, author_id: int = 7, content: str = "hello") -> StoredMessage:
    return StoredMessage(
        message_id=message_id,
        guild_id=1,
        channel_id=2,
        author_id=author_id,
        author_name="tester",
        content=content,
        created_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )


async def test_insert_and_deduplicate(tmp_path) -> None:
    store = MessageStore(tmp_path / "bot.db")
    await store.open()
    try:
        assert await store.add_many([_message(1), _message(2)]) == 2
        assert await store.add_many([_message(1), _message(2)]) == 0
        assert await store.count(7) == 2
    finally:
        await store.close()


async def test_count_filters_by_guild(tmp_path) -> None:
    store = MessageStore(tmp_path / "bot.db")
    await store.open()
    try:
        await store.add_many([_message(1), _message(2)])
        assert await store.count(7, guild_id=1) == 2
        assert await store.count(7, guild_id=999) == 0
    finally:
        await store.close()


async def test_random_for_returns_none_when_empty(tmp_path) -> None:
    store = MessageStore(tmp_path / "bot.db")
    await store.open()
    try:
        assert await store.random_for(7) is None
    finally:
        await store.close()


async def test_random_for_returns_stored_message(tmp_path) -> None:
    store = MessageStore(tmp_path / "bot.db")
    await store.open()
    try:
        await store.add_many([_message(1, content="first"), _message(2, content="second")])
        picked = await store.random_for(7)
        assert picked is not None
        assert picked.content in {"first", "second"}
        assert picked.created_at == datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    finally:
        await store.close()


async def test_count_and_pick_across_all_authors(tmp_path) -> None:
    store = MessageStore(tmp_path / "bot.db")
    await store.open()
    try:
        await store.add_many([_message(1, author_id=7), _message(2, author_id=8)])

        assert await store.count() == 2
        assert await store.count(None, guild_id=1) == 2
        assert await store.count(7) == 1

        picked = await store.random_for()
        assert picked is not None
        assert picked.author_id in {7, 8}

        assert await store.author_count() == 2
        assert await store.author_count(guild_id=999) == 0
    finally:
        await store.close()


def test_jump_url_points_at_the_original_message() -> None:
    message = _message(123)
    assert message.jump_url == "https://discord.com/channels/1/2/123"
