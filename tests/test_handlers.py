"""Offline tests through aiogram's actual update dispatcher and mocked transport."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import ChatMemberMember, Message, Update, User

from rentbot.handlers import build_router
from rentbot.store import Store


class Transport(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.fail_admin = False
        self.fail_membership = False

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        name = type(method).__name__
        if self.fail_admin and getattr(method, "chat_id", None) == -900:
            raise RuntimeError("offline admin destination")
        if name == "GetChatMember":
            if self.fail_membership:
                raise RuntimeError("offline membership failure")
            return ChatMemberMember(status="member", user=User(id=method.user_id, is_bot=False, first_name="Test"))
        if name == "AnswerCallbackQuery":
            return True
        chat_id = getattr(method, "chat_id", 1)
        result = Message(message_id=len(self.sent), date=datetime.now(timezone.utc),
                         chat={"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                         text=getattr(method, "text", None))
        return [result] if name == "SendMediaGroup" else result


class FakePublisher:
    def __init__(self, store):
        self.store = store
        self.listing_calls = []
        self.request_calls = []

    async def publish_listing(self, listing):
        self.listing_calls.append(listing["id"])
        self.store.set_listing_published(99, listing["id"], 700)
        return [700]

    async def publish_request(self, request):
        self.request_calls.append(request["id"])
        self.store.mark_request_published(request["id"], 701)
        return 701


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "test.db", {99}, clock=lambda: 1_800_000_000)
    cfg = SimpleNamespace(bot_username="test_bot", admin_ids=frozenset({99}), admin_chat_id=-900,
        channel_id=-901, channel_url="https://t.me/test_catalog", request_chat_id=-902,
        required_chat_id=-903, required_chat_url="https://t.me/test_required", support_url="https://t.me/test_support",
        subscription_price=400, subscription_days=7, referral_goal=10, payment_details="Оплата по реквизитам",
        payment_qr_file_id=None)
    transport = Transport()
    bot = Bot("123456:FAKE_TEST_TOKEN_DO_NOT_USE", session=transport)
    publisher = FakePublisher(store)
    dp = Dispatcher()
    dp.include_router(build_router(cfg, store, publisher))
    value = SimpleNamespace(store=store, cfg=cfg, bot=bot, dp=dp, transport=transport, publisher=publisher, seq=0)
    yield value
    store.close()


def user(uid):
    return {"id": uid, "is_bot": False, "first_name": "Test", "username": f"test{uid}"}


async def msg(env, uid, text=None, chat_id=None, **extra):
    env.seq += 1
    target = chat_id or uid
    data = {"message_id": env.seq, "date": 1_800_000_000,
            "chat": {"id": target, "type": "private" if target > 0 else "supergroup"},
            "from": user(uid), **extra}
    if text is not None:
        data["text"] = text
    update = Update.model_validate({"update_id": env.seq, "message": data})
    await env.dp.feed_update(env.bot, update)


async def click(env, uid, value, chat_id=None):
    env.seq += 1
    target = chat_id or uid
    message = {"message_id": env.seq, "date": 1_800_000_000,
               "chat": {"id": target, "type": "private" if target > 0 else "supergroup"}, "text": "buttons"}
    update = Update.model_validate({"update_id": env.seq, "callback_query": {
        "id": str(env.seq), "from": user(uid), "chat_instance": "offline", "data": value, "message": message}})
    await env.dp.feed_update(env.bot, update)


def texts(env):
    return "\n".join((getattr(method, "text", None) or getattr(method, "caption", None) or "") for method in env.transport.sent)


def credit(env, uid, count=10):
    env.store.register_user(uid)
    for i in range(count):
        child = uid * 1000 + 100 + i
        env.store.register_user(child, referrer_id=uid)
        env.store.verify_referral(child)


@pytest.mark.asyncio
async def test_phone_requires_access_and_is_never_sent_to_public_chat(env):
    listing = env.store.create_listing(99, "Квартира в центре", "+996700123456", ["photo"])
    await msg(env, 1, f"/start l_{listing['id']}")
    assert "+996700123456" not in texts(env)
    assert env.store.get_last_listing(1) == listing["id"]
    assert env.store.get_state(1)["state"] == "waiting_receipt"
    payment = env.store.create_payment(1, "receipt", "photo")
    env.store.decide_payment(99, payment["id"], True)
    env.transport.sent.clear()
    await msg(env, 1, f"/start l_{listing['id']}", chat_id=-500)
    assert "+996700123456" not in texts(env)
    await msg(env, 1, f"/start l_{listing['id']}")
    assert "+996700123456" in texts(env)


@pytest.mark.asyncio
async def test_payment_requires_admin_identity_and_exact_review_chat_and_is_idempotent(env):
    env.store.register_user(1)
    p = env.store.create_payment(1, "receipt", "photo")
    await click(env, 2, f"pay:approve:{p['id']}", chat_id=-900)
    await click(env, 99, f"pay:approve:{p['id']}", chat_id=-901)
    assert not env.store.has_access(1)
    await click(env, 99, f"pay:approve:{p['id']}", chat_id=-900)
    end = env.store.subscription_until(1)
    assert env.store.has_access(1)
    await click(env, 99, f"pay:approve:{p['id']}", chat_id=-900)
    assert env.store.subscription_until(1) == end


@pytest.mark.asyncio
async def test_receipt_survives_admin_delivery_error_and_retry(env):
    await msg(env, 1, "/subscribe")
    env.transport.fail_admin = True
    await msg(env, 1, photo=[{"file_id": "proof", "file_unique_id": "proof_u", "width": 10, "height": 10}])
    assert env.store.list_payments(user_id=1)[0]["status"] == "pending"
    assert "Чек не потерян" in texts(env)
    env.transport.fail_admin = False
    await msg(env, 1, "/receiptstatus")
    assert "Повторно доставлен" in texts(env)


@pytest.mark.asyncio
async def test_reused_approved_receipt_is_not_forwarded_as_pending(env):
    env.store.register_user(1)
    p = env.store.create_payment(1, "proof", "photo")
    env.store.decide_payment(99, p["id"], True)
    env.store.set_state(1, "waiting_receipt")
    await msg(env, 1, photo=[{"file_id": "proof", "file_unique_id": "proof_u", "width": 10, "height": 10}])
    assert "уже подтверждён" in texts(env)
    assert not any(getattr(m, "chat_id", None) == -900 for m in env.transport.sent)


@pytest.mark.asyncio
async def test_membership_error_fails_closed_and_success_only_credits_once(env):
    env.store.register_user(1)
    await msg(env, 2, "/start ref_1")
    env.transport.fail_membership = True
    await click(env, 2, "ref:verify")
    assert env.store.referral_stats(1)["total"] == 0
    env.transport.fail_membership = False
    await click(env, 2, "ref:verify")
    await click(env, 2, "ref:verify")
    assert env.store.referral_stats(1)["total"] == 1


@pytest.mark.asyncio
async def test_existing_users_cannot_be_referred_later(env):
    await msg(env, 1, "/start")
    await msg(env, 2, "/start")
    await msg(env, 2, "/start ref_1")
    await click(env, 2, "ref:verify")
    assert env.store.get_user(2)["referrer_id"] is None
    assert env.store.referral_stats(1)["total"] == 0


@pytest.mark.asyncio
async def test_request_draft_persists_and_publication_charges_once(env):
    credit(env, 1)
    await click(env, 1, "menu:request")
    token = env.store.get_state(1)["data"]["request_draft"]["token"]
    await click(env, 1, f"req:rooms:{token}:2")
    await msg(env, 1, "Джал")
    # Reconstruct the router to simulate a process restart between input steps.
    env.dp = Dispatcher()
    env.dp.include_router(build_router(env.cfg, env.store, env.publisher))
    await msg(env, 1, "+996555123456")
    await click(env, 1, f"req:publish:{token}")
    await click(env, 1, f"req:publish:{token}")
    assert len(env.publisher.request_calls) == 1
    assert env.store.referral_stats(1)["used"] == 10


@pytest.mark.asyncio
async def test_unpublished_request_resumes_after_lost_fsm_checkpoint(env):
    credit(env, 1)
    pending = env.store.create_request(1, 3, "Центр", "+996555123456")
    env.store.set_state(1, "idle", {})
    await click(env, 1, "menu:request")
    draft = env.store.get_state(1)["data"]["request_draft"]
    assert draft["request_id"] == pending["id"]
    assert draft["rooms"] == 3
    await click(env, 1, f"req:publish:{draft['token']}")
    assert env.publisher.request_calls == [pending["id"]]
    assert env.store.referral_stats(1)["used"] == 10


@pytest.mark.asyncio
async def test_foreign_contact_not_accepted_as_own(env):
    await msg(env, 1, "/start")
    env.store.set_state(1, "request_phone", {"request_draft": {"token": "draft", "rooms": 2, "district": "Джал"}})
    await msg(env, 1, contact={"phone_number": "+996555123456", "first_name": "Other", "user_id": 2})
    assert env.store.get_state(1)["state"] == "request_phone"
    assert "только свой контакт" in texts(env)


@pytest.mark.asyncio
async def test_non_admin_cannot_create_listings(env):
    await msg(env, 1, "/newlisting")
    assert env.store.get_state(1) is None
    assert "администратору" in texts(env)


@pytest.mark.asyncio
async def test_group_receipt_is_not_saved(env):
    await msg(env, 1, "/subscribe")
    await msg(env, 1, chat_id=-900, photo=[{"file_id": "proof", "file_unique_id": "proof_u", "width": 10, "height": 10}])
    assert env.store.list_payments(user_id=1) == []


@pytest.mark.asyncio
async def test_listing_phones_are_rejected_in_public_description(env):
    await msg(env, 99, "/newlisting")
    await msg(env, 99, photo=[{"file_id": "house", "file_unique_id": "house_u", "width": 10, "height": 10}])
    await click(env, 99, "listing:photos_done")
    await msg(env, 99, "Номер +996 700 123456")
    assert env.store.get_state(99)["state"] == "listing_description"
    await msg(env, 99, "Уютная квартира")
    await msg(env, 99, "+996700123456")
    token = env.store.get_state(99)["data"]["listing_draft"]["token"]
    await click(env, 99, f"listing:confirm:{token}")
    await click(env, 99, f"listing:confirm:{token}")
    assert len(env.publisher.listing_calls) == 1


@pytest.mark.asyncio
async def test_admin_pagination_reaches_old_listings_and_detail_is_private(env):
    for n in range(25):
        env.store.create_listing(99, f"Квартира вариант {n}", "+996700123456", ["photo"])
    await msg(env, 99, "/listings")
    assert "№25" in texts(env) and "№1</b>" not in texts(env)
    env.transport.sent.clear()
    await click(env, 99, "listings:page:1")
    assert "№1</b>" in texts(env)
    assert "+996700123456" not in texts(env)
    await click(env, 99, "listings:show:1", chat_id=-900)
    assert "+996700123456" not in texts(env)
    await msg(env, 99, "/listing 1")
    assert "+996700123456" in texts(env)


@pytest.mark.asyncio
async def test_deactivate_callback_replay_does_not_reactivate_listing(env):
    item = env.store.create_listing(99, "Квартира", "+996700123456", ["photo"])
    await click(env, 99, f"listing:deactivate:{item['id']}")
    await click(env, 99, f"listing:deactivate:{item['id']}")
    assert not env.store.get_listing(item["id"])["active"]


@pytest.mark.asyncio
async def test_anonymous_sender_command_ignored(env):
    event = Update.model_validate({"update_id": 999, "message": {"message_id": 1, "date": 1_800_000_000,
        "chat": {"id": -900, "type": "supergroup"}, "text": "/start"}})
    await env.dp.feed_update(env.bot, event)
    assert env.transport.sent == []
