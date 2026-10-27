"""QQ full-message delivery must preserve mention-only intake (#63765, #132713)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter


def make_adapter(**extra):
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={
        "app_id": "123456", "client_secret": "test-only", "dm_policy": "allowlist", "allow_from": ["dm-user"],
        "group_policy": "open", "bot_member_openid": "SELF_OPENID", **extra,
    }))
    adapter.handle_message = AsyncMock()
    return adapter


def group_message(**changes):
    return {
        "id": "message-1", "group_openid": "group-1",
        "author": {"member_openid": "sender-1"},
        "content": "<@SELF_OPENID> hello <@other-member>",
        "mentions": [{"member_openid": "SELF_OPENID", "bot": True}],
        **changes,
    }


async def deliver(adapter, *events):
    tasks = []

    def spawn(coro):
        task = asyncio.get_running_loop().create_task(coro)
        tasks.append(task)
        return task

    with patch("gateway.platforms.qqbot.adapter.asyncio.create_task", side_effect=spawn):
        for seq, (event_type, message) in enumerate(events, 1):
            adapter._dispatch_payload({"op": 0, "t": event_type, "s": seq, "d": message})
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,mentions,accepted", [
    ("<@SELF_OPENID> hello <@other-member>", None, True),
    ("hello <@!SELF_OPENID>", [], True),
    ("@Alice hello <@SELF_OPENID>", [], True),
    ("hello", [{"member_openid": "SELF_OPENID", "bot": True}], True),
    ("hello", [{"id": "SELF_OPENID", "bot": True}], True),
    ("hello", None, False),
    ("<@all> hello", [], False),
    ("<@OTHER_BOT> hello", [{"member_openid": "OTHER_BOT", "bot": True}], False),
    ("<@123456> hello", [{"id": "123456", "bot": True}], False),
    ("hello", [{"is_you": True, "bot": True}], False),
    ("<@SELF_OPENID-extra> hello", [], False),
    ("hello", [None, "SELF_OPENID", {"bot": True}], False),
    ("hello", {"member_openid": "SELF_OPENID"}, False),
])
async def test_full_group_messages_require_this_bot(content, mentions, accepted):
    adapter = make_adapter()
    await deliver(adapter, ("GROUP_MESSAGE_CREATE", group_message(content=content, mentions=mentions)))
    assert adapter.handle_message.await_count == int(accepted)
    if accepted:
        event = adapter.handle_message.call_args.args[0]
        assert event.source.chat_id == "group-1"
        assert event.source.user_id == "sender-1"
        assert event.source.chat_type == "group"
        assert "SELF_OPENID" not in event.text
        if content.startswith("@Alice"):
            assert event.text.startswith("@Alice hello")
        assert ("<@other-member>" in event.text) == ("<@other-member>" in content)


@pytest.mark.asyncio
async def test_full_events_fail_closed_without_confirmed_identity():
    adapter = make_adapter(bot_member_openid="")
    await deliver(adapter, ("GROUP_MESSAGE_CREATE", group_message()))
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,allowed", [
    ("open", True), ("allowlist", True), ("disabled", False), ("pairing", False),
])
@pytest.mark.parametrize("event_type", ["GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"])
async def test_both_group_events_obey_existing_acl(policy, allowed, event_type):
    adapter = make_adapter(group_policy=policy, group_allow_from=["group-1"])
    await deliver(adapter, (event_type, group_message()))
    assert adapter.handle_message.await_count == int(allowed)


@pytest.mark.asyncio
async def test_full_event_checks_acl_before_mention_identity():
    adapter = make_adapter(group_policy="allowlist", group_allow_from=["another-group"])
    with patch.object(adapter, "_mentions_this_bot", side_effect=AssertionError("ACL must run first")):
        await deliver(adapter, ("GROUP_MESSAGE_CREATE", group_message()))
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("full_first", [True, False])
@pytest.mark.parametrize("full_mentioned", [True, False])
async def test_dual_delivery_handles_once_in_either_order(full_first, full_mentioned):
    adapter = make_adapter()
    full = ("GROUP_MESSAGE_CREATE", group_message() if full_mentioned else group_message(content="hello", mentions=[]))
    old = ("GROUP_AT_MESSAGE_CREATE", group_message(content="hello", mentions=[]))
    await deliver(adapter, *([full, old] if full_first else [old, full]))
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_group_semantics_and_private_messages_do_not_require_identity():
    adapter = make_adapter(bot_member_openid="")
    await deliver(adapter,
        ("GROUP_AT_MESSAGE_CREATE", group_message(content="@Bot hello", mentions=[])),
        ("C2C_MESSAGE_CREATE", {"id": "private-1", "author": {"user_openid": "dm-user"}, "content": "hi"}),
    )
    assert [c.args[0].text for c in adapter.handle_message.call_args_list] == ["hello", "hi"]
    assert adapter.handle_message.call_args.args[0].source.chat_type == "dm"


@pytest.mark.asyncio
async def test_full_group_attachment_uses_existing_ingest():
    adapter = make_adapter()
    message = group_message(content="<@SELF_OPENID>", attachments=[{"content_type": "image/png", "url": "https://example.com/image.png"}])
    with patch.object(adapter, "_process_attachments", new=AsyncMock(return_value={
        "voice_transcripts": [], "attachment_info": "", "image_urls": ["https://example.com/image.png"],
        "image_media_types": ["image"],
    })):
        await deliver(adapter, ("GROUP_MESSAGE_CREATE", message))
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.call_args.args[0].media_urls == ["https://example.com/image.png"]
