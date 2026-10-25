import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

# ID проверяющих через запятую в переменной REVIEWERS.
# Если переменная не задана — прежнее значение, чтобы прод работал как раньше.
# DEV_MODE=1 включает /switch — проверяющий может временно стать учеником (только для тестов)
DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")

REVIEWERS = [
    int(x) for x in os.getenv("REVIEWERS", "1828733770").replace(" ", "").split(",") if x
]
