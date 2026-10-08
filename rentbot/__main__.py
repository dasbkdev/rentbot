"""CLI entry point. Network is used only by explicit run/ids commands."""
import argparse
import asyncio
from dataclasses import replace
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, Message
from .config import Config, ConfigurationError


async def identify(env_file, chat=None):
    from dotenv import load_dotenv
    load_dotenv(env_file)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigurationError("Заполните BOT_TOKEN в .env")
    router = Router()

    @router.message(Command("myid"))
    @router.channel_post(Command("myid"))
    async def myid(message: Message):
        await message.answer(
            f"Ваш Telegram ID: {message.from_user.id if message.from_user else 'нет'}\n"
            f"ID этого чата: {message.chat.id}\n"
            "Для канала можно временно добавить бота и указать CHANNEL_ID через ссылку/данные администратора.")
    bot = Bot(token)
    if chat:
        try:
            target = await bot.get_chat(int(chat) if chat.lstrip('-').isdigit() else chat)
            print(f"ID чата: {target.id}; тип: {target.type}")
        finally:
            await bot.session.close()
        return
    dp = Dispatcher()
    dp.include_router(router)
    print("Режим определения ID. Отправьте /myid боту лично и в админ-группе. Ctrl+C — завершить.")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "channel_post"])
    finally:
        await bot.session.close()


async def run(config):
    from .store import Store
    from .publishing import Publisher
    from .handlers import build_router
    bot = Bot(config.bot_token)
    store = None
    try:
        me = await bot.get_me()
        if not me.username:
            raise ConfigurationError("У бота отсутствует username")
        config = replace(config, bot_username=me.username)
        member = await bot.get_chat_member(config.channel_id, me.id)
        if member.status not in {"administrator", "creator"}:
            raise ConfigurationError("Добавьте бота администратором канала")
        if member.status == "administrator" and getattr(member, "can_post_messages", None) is False:
            raise ConfigurationError("У бота нет права публикации в канале")
        if config.required_chat_id != config.channel_id:
            required_member = await bot.get_chat_member(config.required_chat_id, me.id)
            if required_member.status not in {"administrator", "creator"}:
                raise ConfigurationError("Бот должен быть администратором чата проверки приглашений")
        await bot.get_chat(config.admin_chat_id)
        await bot.get_chat(config.request_chat_id)
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(config.db_path, admin_ids=set(config.admin_ids),
                      subscription_days=config.subscription_days, referral_goal=config.referral_goal)
        publisher = Publisher(bot, config, store)
        dp = Dispatcher()
        dp.include_router(build_router(config, store, publisher))
        await bot.set_my_commands([
            BotCommand(command="start",description="Открыть меню"),
            BotCommand(command="help",description="Помощь"),
            BotCommand(command="cancel",description="Отменить текущий ввод"),
            BotCommand(command="receiptstatus",description="Статус отправленного чека"),
            BotCommand(command="myid",description="Мой ID и ID чата"),
        ])
        print(f"@{me.username} запущен. Каталог: {config.channel_url}. Ctrl+C — завершить.")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()
        await bot.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Telegram-бот объявлений об аренде")
    parser.add_argument("--env", default=".env", help="Путь к локальному .env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Проверить .env без подключения к Telegram")
    sub.add_parser("run", help="Запустить polling")
    ids = sub.add_parser("ids", help="Временный бот /myid; нужен только BOT_TOKEN")
    ids.add_argument("--chat", help="Получить ID публичного чата по @username без polling")
    sub.add_parser("deliveries", help="Показать незавершённые публикации")
    rec = sub.add_parser("reconcile", help="Восстановить отправку после ручной проверки канала")
    rec.add_argument("kind", choices=["listing","request"])
    rec.add_argument("id", type=int)
    choice = rec.add_mutually_exclusive_group(required=True)
    choice.add_argument("--message-id", type=int, help="ID уже отправленной карточки")
    choice.add_argument("--retry", action="store_true", help="Подтверждаю: спорное сообщение не было отправлено")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "ids":
            asyncio.run(identify(args.env, args.chat))
            return 0
        config = Config.from_env(args.env)
        if args.command == "check":
            print("Конфигурация заполнена. Сеть не использовалась; токен и права в Telegram ещё не проверены.")
            return 0
        if args.command == "run":
            asyncio.run(run(config))
            return 0
        from .store import Store
        from .publishing import Publisher
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(config.db_path,admin_ids=set(config.admin_ids),
                      subscription_days=config.subscription_days,referral_goal=config.referral_goal)
        publisher = Publisher(None,config,store)
        if args.command == "deliveries":
            for item in publisher.unresolved():
                print(f"{item['kind']} #{item['id']}: {item['stage']}")
        else:
            publisher.reconcile(args.kind,args.id,message_id=args.message_id,retry=args.retry)
            print("Результат проверки сохранён. При --retry повторите публикацию из меню администратора.")
        if hasattr(store,"close"):
            store.close()
        return 0
    except (ConfigurationError,ValueError) as exc:
        print(f"Ошибка: {exc}")
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Do not print request URLs or Bot API credentials in diagnostics.
        print(f"Ошибка запуска: {type(exc).__name__}. Проверьте сеть, токен и права бота.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

