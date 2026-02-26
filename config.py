import os
from dotenv import load_dotenv

load_dotenv()

# Токен от @BotFather
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# ID групповых чатов (можно несколько)
# Как получить: добавь бота в чат, напиши /start, смотри логи или используй @userinfobot
CHAT_IDS: list[int] = [
    int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x.strip()
]

# Часовой пояс
TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

# Вероятность что бот напишет в конкретный день (0.0 - 1.0)
# 0.7 = примерно 5 раз в неделю
CALL_PROBABILITY: float = float(os.getenv("CALL_PROBABILITY", "0.7"))
