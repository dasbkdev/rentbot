"""Public messages never use the listing owner's phone field."""
from datetime import datetime, timezone
from html import escape
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def listing_text(listing: dict) -> str:
    return f"🏠 <b>Объявление №{int(listing['id'])}</b>\n\n{escape(listing['description'])}"


def listing_keyboard(listing_id: int, username: str, support_url: str) -> InlineKeyboardMarkup:
    root = f"https://t.me/{username}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Получить номер", url=f"{root}?start=l_{listing_id}")],
        [InlineKeyboardButton(text="🔎 Ищу квартиру", url=f"{root}?start=request")],
        [InlineKeyboardButton(text="💬 Техподдержка", url=support_url)],
    ])


def request_text(request: dict) -> str:
    rooms = "5 и более" if request['rooms'] == 5 else str(request['rooms'])
    return (f"🔎 <b>Ищу квартиру · заявка №{int(request['id'])}</b>\n\n"
            f"Комнат: {rooms}\nРайон: {escape(request['district'])}\n"
            f"Телефон для связи: {escape(request['phone'])}")


def utc_date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
