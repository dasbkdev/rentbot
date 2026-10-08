"""Telegram conversations. Every draft/step is persisted in Store, not MemoryStorage."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import weakref
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from .publishing import DeliveryUncertain

log = logging.getLogger(__name__)


def keyboard(*rows):
    """Rows contain (label, callback) pairs or ready-made URL buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        item if isinstance(item, InlineKeyboardButton)
        else InlineKeyboardButton(text=item[0], callback_data=item[1])
        for item in row
    ] for row in rows])


def phone_number(text: str) -> str | None:
    value = text.strip()
    if not re.fullmatch(r"\+?[0-9 ()-]{7,30}", value):
        return None
    digits = re.sub(r"\D", "", value)
    return ("+" if value.startswith("+") else "") + digits if 7 <= len(digits) <= 15 else None


class UserSerialMiddleware(BaseMiddleware):
    """Serialize simultaneous album messages/clicks; locks contain no durable data."""
    def __init__(self):
        self.locks = weakref.WeakValueDictionary()

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        lock = self.locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            return await handler(event, data)


def build_router(config, store, publisher) -> Router:
    router = Router(name="rentbot_conversations")
    serial = UserSerialMiddleware()
    router.message.middleware(serial)
    router.callback_query.middleware(serial)

    def private(message):
        return message.chat.type == "private"

    def admin(uid):
        return uid in config.admin_ids

    def link(payload):
        return f"https://t.me/{config.bot_username}?start={payload}"

    def main_keys():
        return keyboard(
            [InlineKeyboardButton(text="🏠 Каталог квартир", url=config.channel_url)],
            [("💳 Подписка", "menu:subscription"), ("🔎 Ищу квартиру", "menu:request")],
            [("👥 Приглашения", "menu:referrals"), ("Помощь", "menu:help")],
            [InlineKeyboardButton(text="Техподдержка", url=config.support_url)],
        )

    def read_state(uid):
        saved = store.get_state(uid)
        return (saved["state"], dict(saved.get("data") or {})) if saved else ("idle", {})

    def state(uid, name, data=None):
        store.set_state(uid, name, read_state(uid)[1] if data is None else data)

    def register(user, referrer=None):
        try:
            return store.register_user(user.id, user.username or "", user.first_name or "", referrer)
        except ValueError:
            # Previously known users cannot rewrite their first-touch referrer.
            return store.register_user(user.id, user.username or "", user.first_name or "")

    async def only_private(message, payload=""):
        if private(message):
            return True
        await message.answer("Продолжите в личном чате с ботом.", reply_markup=keyboard([
            InlineKeyboardButton(text="Открыть бота", url=link(payload or "menu"))
        ]))
        return False

    async def admin_private(message):
        if not admin(message.from_user.id):
            await message.answer("Команда доступна администратору.")
            return False
        return await only_private(message)

    async def main(message):
        await message.answer(
            "Здесь можно выбрать квартиру в каталоге и получить номер владельца.\n"
            f"Подписка: <b>{config.subscription_price} сом на {config.subscription_days} дней</b>.\n"
            f"Для публикации заявки «Ищу квартиру» нужны {config.referral_goal} подтверждённых приглашений.",
            reply_markup=main_keys(), parse_mode="HTML",
        )

    async def help_text(message):
        await message.answer(
            "1. Откройте каталог и выберите «Получить номер» под объявлением.\n"
            "2. Оплатите подписку и отправьте чек фото или PDF в этот личный чат. "
            "Администратор вручную проверит перевод.\n"
            "3. После подтверждения откроется доступ к номерам.\n\n"
            "Для заявки «Ищу квартиру» заполните комнаты, район и телефон, затем пригласите "
            f"{config.referral_goal} новых пользователей по своей ссылке. "
            "Каждый должен зайти в бот впервые по вашей ссылке и подтвердить вступление в канал/группу.\n\n"
            "/start — меню; /receiptstatus — состояние чека и повтор доставки; /cancel — отменить ввод.",
            reply_markup=keyboard([InlineKeyboardButton(text="Техподдержка", url=config.support_url)]),
        )

    async def subscription(message):
        uid = message.from_user.id
        state(uid, "waiting_receipt")
        until = store.subscription_until(uid)
        suffix = ""
        if store.has_access(uid):
            date = datetime.fromtimestamp(until, ZoneInfo("Asia/Bishkek")).strftime("%d.%m.%Y %H:%M")
            suffix = f"\nДоступ действует до {date} (Бишкек). Новый подтверждённый платёж продлит его."
        await message.answer(
            f"<b>Подписка: {config.subscription_price} сом / {config.subscription_days} дней</b>\n"
            f"{escape(config.payment_details)}{suffix}\n\n"
            "После оплаты отправьте сюда фото или PDF чека. Проверка ручная: доступ появится после подтверждения администратора.",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
        if config.payment_qr_file_id:
            try:
                await message.answer_photo(config.payment_qr_file_id, caption="QR-код для оплаты подписки")
            except Exception:
                log.warning("Could not deliver payment QR to user %s", uid)
                await message.answer("QR-код сейчас не загрузился. Используйте реквизиты выше или обратитесь в поддержку.")

    async def show_number(message, uid, listing_id):
        # This guard also covers callback messages created in public chats.
        if not private(message):
            await only_private(message, f"l_{listing_id}")
            return
        listing = store.get_listing(listing_id)
        if not listing or not listing["active"]:
            await message.answer("Это объявление недоступно. Выберите другое в каталоге.", reply_markup=main_keys())
            return
        store.set_last_listing(uid, listing_id)
        if not store.has_access(uid):
            await message.answer("Для получения номера нужна действующая подписка.")
            await subscription_for(message, uid)
            return
        await message.answer(
            f"Номер владельца по объявлению <b>№{listing_id}</b>:\n<code>{escape(listing['phone'])}</code>",
            parse_mode="HTML", reply_markup=main_keys(),
        )

    async def subscription_for(message, uid):
        # Callback.message.from_user is the bot, not the clicking human.
        state(uid, "waiting_receipt")
        text = (f"<b>{config.subscription_price} сом / {config.subscription_days} дней</b>\n"
                f"{escape(config.payment_details)}\n\nПосле оплаты отправьте фото или PDF чека в этот личный чат. "
                "Доступ откроется после ручной проверки перевода.")
        await message.answer(text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        if config.payment_qr_file_id:
            try:
                await message.answer_photo(config.payment_qr_file_id, caption="QR-код для оплаты подписки")
            except Exception:
                log.warning("Could not deliver payment QR to user %s", uid)
                await message.answer("QR-код не загрузился. Используйте реквизиты или свяжитесь с поддержкой.")

    async def send_receipt(bot, payment):
        user = store.get_user(payment["user_id"]) or {}
        name = ("@" + user["username"]) if user.get("username") else user.get("first_name", "Пользователь")
        caption = (f"<b>Чек №{payment['id']}</b>\nПользователь: {escape(name)}\n"
                   f"ID: <code>{payment['user_id']}</code>\nСумма: {payment['amount']} сом\n"
                   "Сверьте фактическое поступление перевода перед подтверждением.")
        markup = keyboard([("✅ Принять", f"pay:approve:{payment['id']}"), ("❌ Отклонить", f"pay:reject:{payment['id']}")])
        try:
            method = bot.send_photo if payment["receipt_kind"] == "photo" else bot.send_document
            kind = "photo" if payment["receipt_kind"] == "photo" else "document"
            await method(chat_id=config.admin_chat_id, **{kind: payment["receipt_file_id"]},
                         caption=caption, reply_markup=markup, parse_mode="HTML")
            return True
        except Exception:
            log.warning("Admin delivery failed for stored payment %s", payment["id"])
            return False

    async def receipt_status(message):
        if not await only_private(message):
            return
        payments = store.list_payments(limit=1, pending_only=False, user_id=message.from_user.id)
        if not payments:
            await message.answer("Вы ещё не отправляли чек.")
            return
        payment = payments[0]
        if payment["status"] == "pending":
            sent = await send_receipt(message.bot, payment)
            await message.answer(f"Чек №{payment['id']} ожидает ручной проверки. " + (
                "Повторно доставлен администраторам." if sent else
                "Он сохранён, но доставка администраторам пока не удалась. Можно повторить /receiptstatus позже."
            ))
        else:
            label = "подтверждён" if payment["status"] == "approved" else "отклонён"
            await message.answer(f"Чек №{payment['id']} {label}.")

    async def referrals(message, uid):
        stats = store.referral_stats(uid)
        await message.answer(
            f"Ваша ссылка: {link(f'ref_{uid}')}\n\n"
            f"Подтверждено: {stats['total']}; использовано: {stats['used']}; доступно: {stats['available']}.\n"
            f"Одна заявка стоит {config.referral_goal} приглашений. Приглашённый должен впервые открыть бот "
            "по вашей ссылке, вступить в указанный канал/группу и нажать «Проверить приглашение». "
            "Повторные входы и приглашение самого себя не учитываются.",
            reply_markup=keyboard(
                [InlineKeyboardButton(text="Открыть канал / группу", url=config.required_chat_url or config.channel_url)],
                [("Проверить приглашение", "ref:verify")],
                [("Продолжить заявку", "menu:request")],
            ), disable_web_page_preview=True,
        )

    async def begin_request(message, uid):
        data = read_state(uid)[1]
        draft = data.get("request_draft")
        pending = store.get_pending_request(uid)
        if pending and (not draft or draft.get("request_id") != pending["id"]):
            draft = {key: pending[key] for key in ("rooms", "district", "phone")}
            draft.update(token=secrets.token_hex(4), request_id=pending["id"])
            data["request_draft"] = draft
        if draft and all(key in draft for key in ("rooms", "district", "phone")):
            state(uid, "request_confirm", data)
            await request_preview(message, uid, draft)
            return
        token = secrets.token_hex(4)
        data["request_draft"] = {"token": token}
        state(uid, "request_rooms", data)
        await message.answer("Сколько комнат ищете?", reply_markup=keyboard([
            ("5+" if n == 5 else str(n), f"req:rooms:{token}:{n}") for n in range(1, 6)
        ]))

    async def request_preview(message, uid, draft):
        stats = store.referral_stats(uid)
        await message.answer(
            f"<b>Заявка «Ищу квартиру»</b>\nКомнат: {'5+' if draft['rooms'] == 5 else draft['rooms']}\n"
            f"Район: {escape(draft['district'])}\nТелефон: {escape(draft['phone'])}\n\n"
            "При публикации район и телефон будут видны участникам канала/группы заявок.\n"
            f"Приглашений доступно: {stats['available']} / {config.referral_goal}.",
            parse_mode="HTML", reply_markup=keyboard(
                [("Опубликовать заявку", f"req:publish:{draft['token']}")],
                [("Приглашения", "menu:referrals"), ("Заполнить заново", "req:restart")],
            ),
        )

    async def listing_preview(message, draft):
        photos = draft["photos"]
        if len(photos) == 1:
            await message.answer_photo(photos[0])
        else:
            await message.answer_media_group([InputMediaPhoto(media=item) for item in photos])
        await message.answer(
            f"<b>Предпросмотр объявления</b>\n{escape(draft['description'])}\n\n"
            f"Номер (будет скрыт в канале): <code>{escape(draft['phone'])}</code>",
            parse_mode="HTML", reply_markup=keyboard([
                ("Опубликовать", f"listing:confirm:{draft['token']}"), ("Отмена", "flow:cancel")
            ]),
        )

    async def listing_detail(message, item):
        flag = "активно" if item["active"] else "отключено"
        await message.answer(
            f"<b>№{item['id']} — {flag}</b>\n{escape(item['description'])}\n"
            f"Телефон: <code>{escape(item['phone'])}</code>", parse_mode="HTML",
            reply_markup=keyboard([
                ("Отключить" if item["active"] else "Включить",
                 f"listing:{'deactivate' if item['active'] else 'activate'}:{item['id']}"),
                ("Опубликовать / повторить", f"listing:publish:{item['id']}"),
            ]),
        )

    async def list_admin(message, page=0, uid=None):
        uid = uid if uid is not None else message.from_user.id
        if not admin(uid) or not private(message):
            await message.answer("Управление объявлениями доступно администратору в личном чате.")
            return
        if not 0 <= page <= 1_000_000:
            await message.answer("Некорректный номер страницы.")
            return
        listings = store.list_listings(limit=21, include_inactive=True, offset=page * 20)
        if not listings:
            await message.answer("На этой странице объявлений нет. Начало: /listings. Создать: /newlisting")
            return
        lines, rows = [], []
        for item in listings[:20]:
            flag = "✅" if item["active"] else "⏸"
            title = " ".join(item["description"].split())[:100]
            lines.append(f"{flag} <b>№{item['id']}</b> — {escape(title)}")
            rows.append([(f"Открыть №{item['id']}", f"listings:show:{item['id']}")])
        nav = []
        if page:
            nav.append(("← Новее", f"listings:page:{page - 1}"))
        if len(listings) > 20:
            nav.append(("Старее →", f"listings:page:{page + 1}"))
        if nav:
            rows.append(nav)
        await message.answer(f"<b>Объявления · страница {page + 1}</b>\n" + "\n".join(lines)
                             + "\n\nОткрыть по номеру: /listing ID", parse_mode="HTML", reply_markup=keyboard(*rows))

    async def command_start(message: Message):
        if not message.from_user or message.from_user.is_bot:
            return
        payload = (message.text or "").partition(" ")[2].strip()
        ref = int(payload[4:]) if re.fullmatch(r"ref_\d+", payload) else None
        register(message.from_user, ref)
        if not await only_private(message, payload):
            return
        state(message.from_user.id, "idle")
        if re.fullmatch(r"l_\d+", payload):
            await show_number(message, message.from_user.id, int(payload[2:]))
        elif payload == "request":
            await begin_request(message, message.from_user.id)
        elif ref is not None:
            await message.answer("Добро пожаловать! Вступите в канал/группу и подтвердите приглашение кнопкой ниже.")
            await referrals(message, message.from_user.id)
        else:
            await main(message)

    async def command_handler(message: Message):
        if not message.from_user or message.from_user.is_bot:
            return
        uid = message.from_user.id
        register(message.from_user)
        cmd = (message.text or "").split()[0].split("@")[0].lower()
        if cmd == "/myid":
            await message.answer(f"Ваш ID: {uid}\nID этого чата: {message.chat.id}")
            return
        if cmd == "/payments":
            if not admin(uid) or (not private(message) and message.chat.id != config.admin_chat_id):
                await message.answer("Команда доступна администраторам в личном чате или чате проверки.")
                return
            payments = store.list_payments(limit=20, pending_only=True)
            if not payments:
                await message.answer("Ожидающих чеков нет.")
            else:
                delivered = sum([await send_receipt(message.bot, payment) for payment in payments])
                await message.answer(f"Ожидающих чеков в этой выборке: {len(payments)}; доставлено в чат проверки: {delivered}.")
            return
        if not await only_private(message):
            return
        if cmd == "/help":
            await help_text(message)
        elif cmd == "/cancel":
            await cancel(message, uid)
        elif cmd == "/receiptstatus":
            await receipt_status(message)
        elif cmd == "/subscribe":
            await subscription(message)
        elif cmd == "/admin":
            if await admin_private(message):
                await message.answer("Управление:\n/newlisting — создать объявление\n/listings — список объявлений по страницам\n"
                                     "/listing ID — открыть объявление по номеру\n"
                                     "/payments — повторить доставку ожидающих чеков в чат проверки\n"
                                     "/fileid — ID фото QR-кода (командой в ответ на фото)\n"
                                     "/cancel — отменить текущий ввод\n/myid — ID пользователя и чата")
        elif cmd in {"/fileid", "/qr"}:
            if await admin_private(message):
                source = message.reply_to_message
                if source and source.photo:
                    await message.answer(f"Для настройки PAYMENT_QR_FILE_ID:\n<code>{escape(source.photo[-1].file_id)}</code>", parse_mode="HTML")
                elif source and source.document:
                    await message.answer(f"ID документа: <code>{escape(source.document.file_id)}</code>\n"
                                         "Для PAYMENT_QR_FILE_ID отправьте QR как фото и ответьте на него /fileid.", parse_mode="HTML")
                else:
                    await message.answer("Отправьте QR-код как фото в этот чат, затем ответьте на фото командой /fileid. "
                                         "Полученное значение нужно поместить в PAYMENT_QR_FILE_ID.")
        elif cmd == "/newlisting":
            if await admin_private(message):
                data = read_state(uid)[1]
                data["listing_draft"] = {"photos": [], "token": secrets.token_hex(4)}
                state(uid, "listing_photos", data)
                await message.answer("Пришлите 1–10 фото квартиры (можно альбомом), затем нажмите «Готово».",
                                     reply_markup=keyboard([("Готово", "listing:photos_done")]))
        elif cmd == "/listings":
            parts = (message.text or "").split()
            if len(parts) > 1 and (not parts[1].isdigit() or int(parts[1]) < 1):
                await message.answer("Используйте /listings или /listings 2.")
                return
            await list_admin(message, int(parts[1]) - 1 if len(parts) > 1 else 0)
        elif cmd == "/listing":
            if await admin_private(message):
                parts = (message.text or "").split()
                item = store.get_listing(int(parts[1])) if len(parts) == 2 and parts[1].isdigit() else None
                if item:
                    await listing_detail(message, item)
                else:
                    await message.answer("Объявление не найдено. Используйте /listing ID, например /listing 1.")

    async def cancel(message, uid):
        current, data = read_state(uid)
        if current.startswith("listing_"):
            data.pop("listing_draft", None)
        if current.startswith("request_"):
            data.pop("request_draft", None)
        state(uid, "idle", data)
        await message.answer("Ввод отменён.", reply_markup=ReplyKeyboardRemove())
        await main(message)

    async def decide(query, action, payment_id):
        if not admin(query.from_user.id) or query.message.chat.id != config.admin_chat_id:
            await query.answer("Подтверждать чеки можно только администратору в чате проверки.", show_alert=True)
            return
        result = store.decide_payment(query.from_user.id, payment_id, action == "approve")
        payment = result["payment"]
        label = "подтверждён" if payment["status"] == "approved" else "отклонён"
        await query.answer(f"Чек {label}." if result["changed"] else f"Чек уже {label}.")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            log.info("Payment keyboard already removed or inaccessible for %s", payment_id)
        if not result["changed"]:
            return
        text = (f"✅ Чек №{payment_id} подтверждён. Доступ к номерам открыт на {config.subscription_days} дней."
                if action == "approve" else f"Чек №{payment_id} отклонён. Проверьте реквизиты и сумму или обратитесь в поддержку.")
        try:
            await query.bot.send_message(payment["user_id"], text, reply_markup=main_keys())
            if action == "approve":
                listing_id = store.get_last_listing(payment["user_id"])
                item = store.get_listing(listing_id) if listing_id else None
                if item and item["active"]:
                    await query.bot.send_message(payment["user_id"],
                        f"Номер владельца по объявлению №{item['id']}:\n<code>{escape(item['phone'])}</code>", parse_mode="HTML")
        except Exception:
            log.warning("Decision saved but user notification failed for payment %s", payment_id)
            await query.message.answer(f"Решение по чеку №{payment_id} сохранено, но уведомление пользователю не доставлено.")

    async def callback(query: CallbackQuery):
        if not isinstance(query.message, Message):
            await query.answer("Откройте бот заново через /start.")
            return
        uid, value, message = query.from_user.id, query.data or "", query.message
        register(query.from_user)
        try:
            if re.fullmatch(r"pay:(approve|reject):\d+", value):
                _, action, item_id = value.split(":")
                await decide(query, action, int(item_id))
                return
            if not private(message):
                await query.answer("Продолжите в личном чате с ботом.", show_alert=True)
                return
            await query.answer()
            if value == "menu:main":
                await main(message)
            elif value == "menu:subscription":
                await subscription_for(message, uid)
            elif value == "menu:help":
                await help_text(message)
            elif value == "menu:referrals":
                await referrals(message, uid)
            elif value == "menu:request":
                await begin_request(message, uid)
            elif value == "flow:cancel":
                await cancel(message, uid)
            elif value == "ref:verify":
                try:
                    member = await query.bot.get_chat_member(config.required_chat_id, uid)
                    joined = member.status in {"member", "administrator", "creator"} or (
                        member.status == "restricted" and bool(getattr(member, "is_member", False)))
                except Exception:
                    log.warning("Membership verification unavailable for user %s", uid)
                    await message.answer("Не удалось проверить вступление. Приглашение пока не засчитано. Повторите позже.")
                    return
                if not joined:
                    await message.answer("Вступление пока не подтверждено. Вступите в канал/группу и повторите проверку.")
                    return
                credited = store.verify_referral(uid)
                await message.answer("Вступление подтверждено. Приглашение засчитано." if credited else
                                     "Вступление подтверждено. Повторно приглашения не начисляются; засчитываются только новые пользователи по реферальной ссылке.")
            elif value == "req:restart":
                data = read_state(uid)[1]
                data.pop("request_draft", None)
                state(uid, "idle", data)
                await begin_request(message, uid)
            elif value.startswith("req:rooms:"):
                parts = value.split(":")
                current, data = read_state(uid)
                draft = data.get("request_draft", {})
                if len(parts) != 4 or draft.get("token") != parts[2] or current != "request_rooms":
                    await message.answer("Эта кнопка устарела. Откройте «Ищу квартиру» в меню.")
                    return
                rooms = int(parts[3])
                if not 1 <= rooms <= 5:
                    return
                draft["rooms"] = rooms
                state(uid, "request_district", data)
                await message.answer("Напишите желаемый район (до 200 символов).")
            elif value.startswith("req:publish:"):
                await publish_request(message, uid, value.split(":")[-1])
            elif value.startswith("listing:"):
                if not admin(uid):
                    await message.answer("Доступно администратору.")
                    return
                await listing_callback(message, uid, value)
            elif re.fullmatch(r"listings:(page|show):\d+", value):
                if not admin(uid):
                    await message.answer("Доступно администратору.")
                    return
                _, action, item_id = value.split(":")
                if action == "page":
                    await list_admin(message, int(item_id), uid)
                else:
                    item = store.get_listing(int(item_id))
                    if item:
                        await listing_detail(message, item)
                    else:
                        await message.answer("Объявление не найдено.")
        except (ValueError, PermissionError) as exc:
            log.info("Rejected action %s (%s)", value.split(":")[0], type(exc).__name__)
            await message.answer("Действие недоступно или данные устарели. Откройте меню заново через /start.")

    async def publish_request(message, uid, token):
        data = read_state(uid)[1]
        draft = data.get("request_draft", {})
        if draft.get("token") != token or not all(key in draft for key in ("rooms", "district", "phone")):
            await message.answer("Это старая кнопка. Откройте текущую заявку через «Ищу квартиру».")
            return
        if draft.get("request_id"):
            item = store.get_request(draft["request_id"])
        else:
            pending = store.get_pending_request(uid)
            if pending:
                draft.update({key: pending[key] for key in ("rooms", "district", "phone")})
                draft["request_id"] = pending["id"]
                state(uid, "request_confirm", data)
                await message.answer("Найдена сохранённая заявка, за которую приглашения уже списаны. Проверьте её перед повторной публикацией.")
                await request_preview(message, uid, draft)
                return
            stats = store.referral_stats(uid)
            if stats["available"] < config.referral_goal:
                await message.answer(f"Черновик сохранён. Не хватает {config.referral_goal - stats['available']} подтверждённых приглашений.")
                await referrals(message, uid)
                return
            item = store.create_request(uid, draft["rooms"], draft["district"], draft["phone"])
            draft["request_id"] = item["id"]
            state(uid, "request_confirm", data)
        if item.get("published_at") or item.get("channel_message_id"):
            await message.answer(f"Заявка №{item['id']} уже опубликована. Повторного списания приглашений нет.")
            return
        try:
            await publisher.publish_request(item)
        except DeliveryUncertain:
            await message.answer(f"Заявка №{item['id']} сохранена. Результат отправки пока неизвестен; "
                                 "администратору нужно проверить публикацию. Повторное списание приглашений не производится.",
                                 reply_markup=keyboard([InlineKeyboardButton(text="Техподдержка", url=config.support_url)]))
            return
        except Exception:
            log.warning("Request %s retained after publication failure", item["id"])
            await message.answer("Заявка сохранена, но отправить её пока не удалось. Нажмите «Опубликовать заявку» ещё раз: повторного списания приглашений не будет.")
            return
        state(uid, "request_done", data)
        await message.answer(f"✅ Заявка №{item['id']} опубликована.", reply_markup=keyboard(
            [("Создать новую заявку", "req:restart")], [("Меню", "menu:main")]))

    async def listing_callback(message, uid, value):
        current, data = read_state(uid)
        draft = data.get("listing_draft", {})
        if value == "listing:photos_done":
            if current != "listing_photos" or not draft.get("photos"):
                await message.answer("Сначала пришлите хотя бы одно фото. Для нового объявления: /newlisting")
                return
            state(uid, "listing_description", data)
            await message.answer("Пришлите описание (до 3500 символов). Номер телефона укажите отдельным следующим сообщением.")
        elif value.startswith("listing:confirm:"):
            if draft.get("token") != value.split(":")[-1] or not all(key in draft for key in ("description", "phone", "photos")):
                await message.answer("Предпросмотр устарел. Используйте /newlisting или /listings.")
                return
            if draft.get("listing_id"):
                item = store.get_listing(draft["listing_id"])
            else:
                item = store.create_listing(uid, draft["description"], draft["phone"], draft["photos"])
                draft["listing_id"] = item["id"]
                state(uid, "listing_confirm", data)
            await publish_admin_listing(message, uid, item)
        elif re.fullmatch(r"listing:(activate|deactivate|publish):\d+", value):
            _, action, item_id = value.split(":")
            item = store.get_listing(int(item_id))
            if not item:
                await message.answer("Объявление не найдено.")
                return
            if action in {"activate", "deactivate"}:
                item = store.set_listing_status(uid, item["id"], action == "activate")
                await message.answer(f"Объявление №{item['id']} " + ("включено." if item["active"] else "отключено. Номер больше не выдаётся."))
            else:
                await publish_admin_listing(message, uid, item)

    async def publish_admin_listing(message, uid, item):
        if not item["active"]:
            await message.answer("Сначала включите объявление через /listings.")
            return
        if item.get("published_at") or item.get("channel_message_id"):
            await message.answer(f"Объявление №{item['id']} уже опубликовано.")
            return
        try:
            await publisher.publish_listing(item)
        except DeliveryUncertain as exc:
            await message.answer(escape(str(exc)), parse_mode="HTML")
            return
        except Exception:
            log.warning("Listing %s retained after publication failure", item["id"])
            await message.answer(f"Объявление №{item['id']} сохранено. Публикация пока не удалась; повторите из /listings.")
            return
        state(uid, "idle")
        await message.answer(f"✅ Объявление №{item['id']} опубликовано. Номер скрыт от пользователей без подписки.")

    async def incoming(message: Message):
        if not message.from_user or message.from_user.is_bot or not private(message):
            return
        uid = message.from_user.id
        register(message.from_user)
        current, data = read_state(uid)
        text = (message.text or "").strip()
        if current.startswith("listing_") and not admin(uid):
            state(uid, "idle")
            return
        if current == "waiting_receipt":
            kind = "photo" if message.photo else "document"
            file_id = message.photo[-1].file_id if message.photo else None
            if message.document and message.document.mime_type == "application/pdf":
                file_id = message.document.file_id
            if not file_id:
                await message.answer("Отправьте чек как фото или PDF-документ. /cancel — отменить.")
                return
            try:
                payment = store.create_payment(uid, file_id, kind, config.subscription_price)
            except ValueError:
                await message.answer("Этот чек уже использован или не может быть принят. Проверить свои чеки: /receiptstatus. Для уточнения обратитесь в поддержку.")
                return
            if payment["status"] != "pending":
                label = "подтверждён" if payment["status"] == "approved" else "отклонён"
                await message.answer(f"Этот чек №{payment['id']} уже {label}. Для нового платежа отправьте новый чек.")
                return
            delivered = await send_receipt(message.bot, payment)
            state(uid, "idle", data)
            await message.answer(f"Чек №{payment['id']} сохранён и ожидает проверки. " + (
                "Администратор получил заявку." if delivered else
                "Доставка администратору пока не удалась. Чек не потерян; повторить доставку: /receiptstatus."
            ))
        elif current == "listing_photos":
            if not message.photo:
                await message.answer("Нужно фото (не файл). Когда все фото отправлены, нажмите «Готово».")
                return
            photos = data["listing_draft"]["photos"]
            if len(photos) >= 10:
                await message.answer("Уже сохранено 10 фото. Нажмите «Готово».")
                return
            photos.append(message.photo[-1].file_id)
            state(uid, current, data)
            if not message.media_group_id:
                await message.answer(f"Фото сохранено ({len(photos)}/10).", reply_markup=keyboard([("Готово", "listing:photos_done")]))
        elif current == "listing_description":
            if not text or len(text) > 3500:
                await message.answer("Пришлите текст описания от 1 до 3500 символов.")
                return
            if any(sum(ch.isdecimal() for ch in run) >= 7 for run in re.findall(r"[\d\s+()./–—-]+", text)):
                await message.answer("В описании найден текст, похожий на телефон. Уберите номер из описания: он вводится отдельно и должен быть скрыт.")
                return
            data["listing_draft"]["description"] = text
            state(uid, "listing_phone", data)
            await message.answer("Теперь пришлите телефон владельца (7–15 цифр).")
        elif current == "listing_phone":
            phone = phone_number(text)
            if not phone:
                await message.answer("Номер должен содержать 7–15 цифр; допустимы +, пробелы, скобки и дефисы.")
                return
            data["listing_draft"]["phone"] = phone
            state(uid, "listing_confirm", data)
            await listing_preview(message, data["listing_draft"])
        elif current == "request_district":
            if not text or len(text) > 200:
                await message.answer("Пришлите район текстом: от 1 до 200 символов.")
                return
            if any(sum(ch.isdecimal() for ch in run) >= 7 for run in re.findall(r"[\d\s+()./–—-]+", text)):
                await message.answer("Укажите только район. Телефон будет запрошен следующим шагом.")
                return
            data["request_draft"]["district"] = text
            state(uid, "request_phone", data)
            await message.answer("Пришлите телефон для связи. Он будет опубликован вместе с заявкой.",
                reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
                                                resize_keyboard=True, one_time_keyboard=True))
        elif current == "request_phone":
            if message.contact and message.contact.user_id != uid:
                await message.answer("Кнопкой можно отправить только свой контакт. Либо напишите номер текстом.")
                return
            phone = phone_number(message.contact.phone_number if message.contact else text)
            if not phone:
                await message.answer("Нужен номер из 7–15 цифр. Отправьте свой контакт кнопкой или напишите номер.")
                return
            data["request_draft"]["phone"] = phone
            state(uid, "request_confirm", data)
            await message.answer("Проверьте заявку перед публикацией.", reply_markup=ReplyKeyboardRemove())
            await request_preview(message, uid, data["request_draft"])
        elif current in {"request_confirm", "request_done"} and data.get("request_draft"):
            await request_preview(message, uid, data["request_draft"])
        elif current == "request_rooms":
            await message.answer("Выберите количество комнат кнопкой выше или /cancel.")
        elif current == "listing_confirm":
            await message.answer("Проверьте предпросмотр выше и нажмите «Опубликовать» или /cancel.")
        else:
            await main(message)

    router.message.register(command_start, CommandStart())
    router.message.register(command_handler, Command("help", "cancel", "subscribe", "receiptstatus", "admin", "newlisting", "listing", "listings", "payments", "myid", "fileid", "qr"))
    router.callback_query.register(callback)
    router.message.register(incoming)
    return router
