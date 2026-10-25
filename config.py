import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

# ID проверяющих через запятую в переменной REVIEWERS.
# Если переменная не задана — прежнее значение, чтобы прод работал как раньше.
REVIEWERS = [
    int(x) for x in os.getenv("REVIEWERS", "1828733770").replace(" ", "").split(",") if x
]
