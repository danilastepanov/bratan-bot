import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
CHAT_IDS: list[int] = [int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x.strip()]
TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

# Участники: берём из .env (MEMBERS=@user1,@user2) или fallback на хардкод
_members_env = os.getenv("MEMBERS", "")
MEMBERS: list[str] = (
    [x.strip() for x in _members_env.split(",") if x.strip()]
    if _members_env
    else []
)

# Валидация при старте
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not CHAT_IDS:
    raise ValueError("CHAT_IDS не задан в .env")
if not MEMBERS:
    raise ValueError("MEMBERS пустой — задайте MEMBERS=@user1,@user2 в .env")