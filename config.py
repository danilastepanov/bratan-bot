import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
CHAT_IDS: list[int] = [int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x]
TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

# Участники чата для похвалы
MEMBERS: list[str] = [
    "@Richard_Starodubov",
    "@StepanoffDanila",
    "@scary_artemis",
]