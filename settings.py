"""Настройки ИИ, которые админ меняет прямо в боте (/settings).

Значения из Railway (AI_MODE, AI_MAX_ATTEMPTS) — только начальные: как только админ
выбрал режим в боте, он хранится в базе и переживает перезапуски.
"""
import db
from config import AI_MAX_ATTEMPTS, AI_MODE, OPENAI_API_KEY

MODES = ("suggest", "auto", "off")
MODE_LABELS = {
    "off": "выключена — проверяет только человек",
    "suggest": "ИИ предлагает, человек подтверждает",
    "auto": "ИИ решает сам, спорное — человеку",
}
ATTEMPT_CHOICES = (2, 3, 5)

_values = {}


def load():
    """Читает настройки из базы при старте (синхронно, до запуска бота)."""
    _values.clear()
    _values.update(db.get_settings())


def ai_available():
    return bool(OPENAI_API_KEY)


def ai_mode():
    if not ai_available():
        return "off"
    mode = _values.get("ai_mode", AI_MODE)
    return mode if mode in MODES else "suggest"


def max_attempts():
    try:
        return max(1, int(_values.get("ai_max_attempts", AI_MAX_ATTEMPTS)))
    except ValueError:
        return AI_MAX_ATTEMPTS


async def set_ai_mode(mode, updated_by):
    if mode not in MODES:
        raise ValueError(mode)
    await db.set_setting("ai_mode", mode, updated_by)
    _values["ai_mode"] = mode


async def set_max_attempts(value, updated_by):
    value = int(value)
    await db.set_setting("ai_max_attempts", value, updated_by)
    _values["ai_max_attempts"] = str(value)
