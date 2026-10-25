import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

# ID проверяющих через запятую в переменной REVIEWERS.
# Если переменная не задана — прежнее значение, чтобы прод работал как раньше.
# DEV_MODE=1 включает /switch — проверяющий может временно стать учеником (только для тестов)
DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")

def _ids(value):
    return [int(x) for x in (value or "").replace(" ", "").split(",") if x]


REVIEWERS = _ids(os.getenv("REVIEWERS", "1828733770"))

# Админы видят статистику и выгрузку. Если не заданы — админы = проверяющие.
ADMINS = _ids(os.getenv("ADMINS")) or list(REVIEWERS)

# Часовой пояс для дат в отчётах (в базе время хранится в UTC)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
