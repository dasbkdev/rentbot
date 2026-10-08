"""Durable publication checkpoints; ambiguous network outcomes require reconciliation."""
import asyncio
import json
import sqlite3
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto
from .formatting import listing_keyboard, listing_text, request_text


class DeliveryUncertain(ValueError):
    """Telegram may have received the request. Do not blindly send it again."""


class Publisher:
    def __init__(self, bot, config, store):
        self.bot, self.config, self.store = bot, config, store
        self._lock = asyncio.Lock()
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS delivery_journal(
                kind TEXT NOT NULL, object_id INTEGER NOT NULL,
                stage TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind,object_id))""")

    def _db(self):
        return sqlite3.connect(self.config.db_path, timeout=15)

    def checkpoint(self, kind, object_id):
        with self._db() as con:
            row = con.execute(
                "SELECT stage,data FROM delivery_journal WHERE kind=? AND object_id=?",
                (kind, object_id)).fetchone()
        return {"stage": row[0], "data": json.loads(row[1])} if row else {"stage": "new", "data": {}}

    def _save(self, kind, object_id, stage, data):
        with self._db() as con:
            con.execute("""INSERT INTO delivery_journal VALUES(?,?,?,?)
                ON CONFLICT(kind,object_id) DO UPDATE SET stage=excluded.stage,data=excluded.data""",
                (kind, object_id, stage, json.dumps(data)))

    def unresolved(self):
        with self._db() as con:
            return [{"kind": k, "id": i, "stage": s}
                    for k, i, s in con.execute(
                        "SELECT kind,object_id,stage FROM delivery_journal WHERE stage!='done'")]

    def reconcile(self, kind, object_id, *, message_id=None, retry=False):
        """Operator has inspected the destination before calling this method."""
        if kind not in {"listing", "request"}:
            raise ValueError("Тип должен быть listing или request")
        state = self.checkpoint(kind, object_id)
        if state["stage"] == "done":
            return
        data = state["data"]
        if message_id is not None:
            if message_id <= 0:
                raise ValueError("message_id должен быть положительным")
            if kind == "listing" and state["stage"] not in {"sending_card", "media_sent"}:
                raise ValueError("Для альбома нельзя считать карточку опубликованной без проверки всех этапов")
            data["message_id"] = message_id
            self._save(kind, object_id, "done", data)
            self._mark_done(kind, object_id, message_id)
        elif retry:
            if state["stage"] == "sending_card":
                self._save(kind, object_id, "media_sent", data)
            elif state["stage"] in {"sending_media", "sending_request"}:
                self._save(kind, object_id, "new", {})
            # A known rejection leaves a safe stage already; no change required.
        else:
            raise ValueError("Укажите message_id либо явно разрешите повтор")

    def _mark_done(self, kind, object_id, message_id):
        if kind == "listing":
            self.store.set_listing_published(next(iter(self.config.admin_ids)), object_id, message_id)
        else:
            self.store.mark_request_published(object_id, message_id)

    async def _send(self, kind, object_id, stage, previous, data, action):
        self._save(kind, object_id, stage, data)
        try:
            return await action()
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
            # The API explicitly rejected the request; a deliberate retry is safe.
            self._save(kind, object_id, previous, data)
            raise
        except Exception as exc:
            raise DeliveryUncertain(
                f"Результат отправки {kind} №{object_id} неизвестен. "
                "Администратору нужно проверить канал и выполнить reconcile; "
                "автоматический повтор остановлен, чтобы не создать дубликат."
            ) from exc

    async def publish_listing(self, listing):
        async with self._lock:
            item = self.store.get_listing(listing["id"])
            if not item or not item["active"]:
                raise ValueError("Объявление отсутствует или снято")
            object_id = item["id"]
            state = self.checkpoint("listing", object_id)
            if state["stage"].startswith("sending_"):
                raise DeliveryUncertain(f"Проверьте предыдущую отправку объявления №{object_id} (reconcile)")
            data = state["data"]
            if state["stage"] == "done":
                self._mark_done("listing", object_id, data["message_id"])
                return data.get("media_ids", []) + [data["message_id"]]
            if item.get("channel_message_id"):
                return [item["channel_message_id"]]
            if state["stage"] == "new":
                photos = item["photos"]
                if len(photos) == 1:
                    result = await self._send("listing", object_id, "sending_media", "new", {},
                        lambda: self.bot.send_photo(chat_id=self.config.channel_id, photo=photos[0]))
                    media_ids = [result.message_id]
                else:
                    result = await self._send("listing", object_id, "sending_media", "new", {},
                        lambda: self.bot.send_media_group(chat_id=self.config.channel_id,
                            media=[InputMediaPhoto(media=x) for x in photos]))
                    media_ids = [x.message_id for x in result]
                data = {"media_ids": media_ids}
                self._save("listing", object_id, "media_sent", data)
            result = await self._send("listing", object_id, "sending_card", "media_sent", data,
                lambda: self.bot.send_message(
                    chat_id=self.config.channel_id, text=listing_text(item), parse_mode="HTML",
                    reply_markup=listing_keyboard(object_id,self.config.bot_username,self.config.support_url),
                    disable_web_page_preview=True))
            data["message_id"] = result.message_id
            self._save("listing", object_id, "done", data)
            self._mark_done("listing", object_id, result.message_id)
            return data.get("media_ids", []) + [result.message_id]

    async def publish_request(self, request):
        async with self._lock:
            item = self.store.get_request(request["id"])
            if not item:
                raise ValueError("Заявка не найдена")
            object_id = item["id"]
            state = self.checkpoint("request", object_id)
            if state["stage"] == "done":
                self._mark_done("request", object_id, state["data"]["message_id"])
                return state["data"]["message_id"]
            if item.get("channel_message_id"):
                return item["channel_message_id"]
            if state["stage"].startswith("sending_"):
                raise DeliveryUncertain(f"Проверьте предыдущую отправку заявки №{object_id} (reconcile)")
            message = await self._send("request", object_id, "sending_request", "new", {},
                lambda: self.bot.send_message(
                    chat_id=self.config.request_chat_id, text=request_text(item),
                    parse_mode="HTML", disable_web_page_preview=True))
            self._save("request", object_id, "done", {"message_id": message.message_id})
            self._mark_done("request", object_id, message.message_id)
            return message.message_id

