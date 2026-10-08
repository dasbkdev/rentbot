from dataclasses import replace
from types import SimpleNamespace
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from rentbot.config import Config
from rentbot.formatting import listing_text, listing_keyboard
from rentbot.publishing import Publisher, DeliveryUncertain
from rentbot.store import Store


class FakeBot:
    def __init__(self):
        self.calls = []
        self.next_error = None
    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        return SimpleNamespace(message_id=len(self.calls))
    async def send_media_group(self, **kw):
        self.calls.append(("album", kw))
        return [SimpleNamespace(message_id=10+i) for i in range(len(kw["media"]))]
    async def send_message(self, **kw):
        self.calls.append(("message", kw))
        if self.next_error is not None:
            error,self.next_error = self.next_error,None
            raise error
        return SimpleNamespace(message_id=100+len(self.calls))


@pytest.fixture
def env(tmp_path):
    config = Config(
        bot_token="123456:FAKE_TEST_TOKEN_NEVER_SENT",bot_username="rental_test_bot",
        admin_ids=frozenset({1}),admin_chat_id=-10,channel_id=-20,
        channel_url="https://t.me/test_channel",request_chat_id=-30,required_chat_id=-20,
        support_url="https://t.me/test_support",payment_details="Тестовые реквизиты",
        db_path=tmp_path/"db.sqlite3")
    store = Store(config.db_path,{1},clock=lambda:1000000)
    store.register_user(1)
    store.register_user(2)
    yield config,store
    store.close()


def make_listing(store, photos=None):
    return store.create_listing(1,"<Джал> & 2 комнаты, 35 000 сом","+996555123456",photos or ["photo_1","photo_2"])


def test_public_card_has_no_owner_contact(env):
    config,store=env
    listing=make_listing(store)
    assert listing["phone"] not in listing_text(listing)
    assert "&lt;Джал&gt; &amp;" in listing_text(listing)
    serialized=listing_keyboard(listing["id"],config.bot_username,config.support_url).model_dump_json()
    assert listing["phone"] not in serialized
    assert "start=l_" in serialized
    assert "актуальность" not in serialized


async def test_publication_survives_process_restart_without_duplicate(env):
    config,store=env
    listing=make_listing(store)
    bot=FakeBot()
    ids=await Publisher(bot,config,store).publish_listing(listing)
    assert len(ids)==3
    assert [c[0] for c in bot.calls]==["album","message"]
    again=FakeBot()
    assert await Publisher(again,config,store).publish_listing(listing)==ids
    assert again.calls==[]
    assert store.get_listing(listing["id"])["channel_message_id"]==ids[-1]


async def test_known_card_rejection_does_not_resend_album(env):
    config,store=env
    listing=make_listing(store)
    bot=FakeBot()
    bot.next_error=TelegramBadRequest(method=SendMessage(chat_id=-20,text="test"),message="Bad Request")
    with pytest.raises(TelegramBadRequest):
        await Publisher(bot,config,store).publish_listing(listing)
    assert Publisher(bot,config,store).checkpoint("listing",listing["id"])["stage"]=="media_sent"
    await Publisher(bot,config,store).publish_listing(listing)
    assert [c[0] for c in bot.calls]==["album","message","message"]


async def test_unknown_result_is_blocked_until_manual_reconciliation(env):
    config,store=env
    listing=make_listing(store,["photo_1"])
    bot=FakeBot()
    bot.next_error=TimeoutError("connection lost after send")
    publisher=Publisher(bot,config,store)
    with pytest.raises(DeliveryUncertain):
        await publisher.publish_listing(listing)
    count=len(bot.calls)
    with pytest.raises(DeliveryUncertain):
        await Publisher(bot,config,store).publish_listing(listing)
    assert len(bot.calls)==count
    publisher.reconcile("listing",listing["id"],message_id=999)
    assert store.get_listing(listing["id"])["channel_message_id"]==999
    assert await publisher.publish_listing(listing)==[1,999]
    assert len(bot.calls)==count


async def test_explicit_retry_reuses_confirmed_media(env):
    config,store=env
    listing=make_listing(store)
    bot=FakeBot()
    bot.next_error=OSError("network")
    publisher=Publisher(bot,config,store)
    with pytest.raises(DeliveryUncertain):
        await publisher.publish_listing(listing)
    publisher.reconcile("listing",listing["id"],retry=True)
    await publisher.publish_listing(listing)
    assert [c[0] for c in bot.calls]==["album","message","message"]


async def test_request_is_published_once_and_referral_cost_is_not_repeated(env):
    config,store=env
    for uid in range(100,110):
        store.register_user(uid,referrer_id=2)
        store.verify_referral(uid)
    request=store.create_request(2,2,"Джал","+996700123456")
    bot=FakeBot()
    message_id=await Publisher(bot,config,store).publish_request(request)
    assert await Publisher(bot,config,store).publish_request(request)==message_id
    assert len(bot.calls)==1
    assert store.referral_stats(2)["used"]==10
    assert store.referral_stats(2)["available"]==0
    assert request["phone"] in bot.calls[0][1]["text"]


async def test_inactive_listing_cannot_be_published(env):
    config,store=env
    listing=make_listing(store)
    store.set_listing_status(1,listing["id"],False)
    bot=FakeBot()
    with pytest.raises(ValueError):
        await Publisher(bot,config,store).publish_listing(listing)
    assert not bot.calls


def test_bishkek_timezone_available_on_windows():
    from zoneinfo import ZoneInfo
    from datetime import datetime,timedelta
    assert datetime(2026,10,7,tzinfo=ZoneInfo("Asia/Bishkek")).utcoffset()==timedelta(hours=6)

