"""Explicit configuration. Importing this module never contacts Telegram."""
from dataclasses import dataclass
from pathlib import Path
import os
import re
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    bot_username: str
    admin_ids: frozenset[int]
    admin_chat_id: int
    channel_id: int
    channel_url: str
    request_chat_id: int
    required_chat_id: int
    support_url: str
    payment_details: str
    db_path: Path
    subscription_price: int = 400
    subscription_days: int = 7
    referral_goal: int = 10
    payment_qr_file_id: str | None = None
    required_chat_url: str = ""

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Config":
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)
        def required(key: str) -> str:
            value = os.environ.get(key, "").strip()
            if not value or "CHANGE_ME" in value:
                raise ConfigurationError(f"Заполните {key} в .env")
            return value
        def integer(key: str, default=None, minimum=None) -> int:
            raw = os.environ.get(key, str(default) if default is not None else "").strip()
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ConfigurationError(f"{key}: требуется целое число") from None
            if value == 0 or (minimum is not None and value < minimum):
                raise ConfigurationError(f"{key}: недопустимое значение")
            return value
        def telegram_url(key: str) -> str:
            value = required(key)
            parsed = urlparse(value)
            if parsed.scheme != "https" or parsed.netloc != "t.me" or not parsed.path.strip("/"):
                raise ConfigurationError(f"{key}: нужна ссылка https://t.me/...")
            return value
        token = required("BOT_TOKEN")
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ConfigurationError("BOT_TOKEN: неверный формат токена BotFather")
        try:
            admins = frozenset(int(x.strip()) for x in required("ADMIN_IDS").split(","))
        except ValueError:
            raise ConfigurationError("ADMIN_IDS: числовые ID через запятую") from None
        if not admins or any(x <= 0 for x in admins):
            raise ConfigurationError("ADMIN_IDS: требуются положительные ID администраторов")
        channel_id = integer("CHANNEL_ID")
        username = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
        if username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ConfigurationError("BOT_USERNAME: неверное имя")
        return cls(
            bot_token=token, bot_username=username, admin_ids=admins,
            admin_chat_id=integer("ADMIN_CHAT_ID"), channel_id=channel_id,
            channel_url=telegram_url("CHANNEL_URL"),
            required_chat_url=telegram_url("REQUIRED_CHAT_URL") if os.environ.get("REQUIRED_CHAT_URL", "").strip() else telegram_url("CHANNEL_URL"),
            request_chat_id=integer("REQUEST_CHAT_ID", channel_id),
            required_chat_id=integer("REQUIRED_CHAT_ID", channel_id),
            support_url=telegram_url("SUPPORT_URL"),
            payment_details=required("PAYMENT_DETAILS").replace("\\n", "\n"),
            payment_qr_file_id=os.environ.get("PAYMENT_QR_FILE_ID", "").strip() or None,
            db_path=Path(os.environ.get("DB_PATH", "data/rentbot.sqlite3")).resolve(),
            subscription_price=integer("SUBSCRIPTION_PRICE", 400, 1),
            subscription_days=integer("SUBSCRIPTION_DAYS", 7, 1),
            referral_goal=integer("REFERRAL_GOAL", 10, 1),
        )
