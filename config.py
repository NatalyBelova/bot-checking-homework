import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

# DEV_MODE=1 включает /switch — проверяющий может временно стать учеником (только для тестов)
DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")


def _ids(value):
    return [int(x) for x in (value or "").replace(" ", "").split(",") if x]


# ID проверяющих через запятую в переменной REVIEWERS.
# Если переменная не задана — прежнее значение, чтобы прод работал как раньше.
REVIEWERS = _ids(os.getenv("REVIEWERS", "1828733770"))

# Админы видят статистику и выгрузку. Если не заданы — админы = проверяющие.
ADMINS = _ids(os.getenv("ADMINS")) or list(REVIEWERS)

# Часовой пояс для дат в отчётах (в базе время хранится в UTC)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# --- ИИ-проверка ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
# Глубина рассуждения для моделей gpt-5 / o-серии: minimal | low | medium | high
OPENAI_REASONING = os.getenv("OPENAI_REASONING", "medium").lower()

# off     — ИИ не используется
# suggest — ИИ предлагает решение, проверяющий подтверждает (по умолчанию)
# auto    — ИИ сам принимает/возвращает, спорные работы уходят человеку
AI_MODE = os.getenv("AI_MODE", "suggest").lower() if OPENAI_API_KEY else "off"
if AI_MODE not in ("off", "suggest", "auto"):
    AI_MODE = "suggest"

# --- Jira (для заданий типа «Jira»): https://<домен>.atlassian.net, email и API-токен аккаунта ---
JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")

# В режиме auto: начиная с этой попытки работу смотрит человек (чтобы не крутить ученика бесконечно)
AI_MAX_ATTEMPTS = int(os.getenv("AI_MAX_ATTEMPTS", "3"))
