"""Общее для всех обработчиков: экземпляр бота, роли, клавиатуры, помощники."""
import asyncio
import logging
import os
from collections import defaultdict

from aiogram import Bot, types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from datetime import timezone
from zoneinfo import ZoneInfo

from config import ADMINS, REVIEWERS, TIMEZONE, TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("homework_bot")

bot = Bot(token=TOKEN)

BTN_NEW = "📤 Отправить новое ДЗ"
BTN_MY = "📋 Мои ДЗ"

# Состояния (хранятся в таблице user_states и переживают перезапуск бота)
SUBMIT = "submit"  # ученик собирает отправку: data = {mode, assignment_id|homework_id, text, files}
REVIEW = "review"  # проверяющий пишет комментарий: data = {homework_id, action, text, files}
REFERENCE = "reference"  # админ задаёт эталон задания: data = {assignment_id, text, files}

STATUS_LABELS = {
    "new": "🕐 На проверке",
    "revision": "🔄 На доработке",
    "accepted": "✅ Принято",
}

ALBUM_DELAY = 0.8  # сек: Telegram присылает альбом отдельными сообщениями
MAX_TEXT = 3500    # лимит сообщения Telegram — 4096 символов

_albums = defaultdict(list)
_album_tasks = {}
_idle_albums = set()
_user_locks = defaultdict(asyncio.Lock)
_acting_as_student = set()  # DEV_MODE: проверяющие, переключившиеся в роль ученика


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def is_reviewer(user_id):
    return user_id in REVIEWERS and user_id not in _acting_as_student


async def deny_callback(callback):
    """Отвечает на нажатие кнопки, которая этому пользователю недоступна."""
    if callback.from_user.id in _acting_as_student:
        await callback.answer("Сейчас ты в роли ученика. Вернись через /switch и нажми ещё раз",
                              show_alert=True)
    else:
        await callback.answer("Эта кнопка тебе недоступна", show_alert=True)


def is_admin(user_id):
    return user_id in ADMINS and user_id not in _acting_as_student


def is_staff(user_id):
    return is_reviewer(user_id) or is_admin(user_id)


_TZ = ZoneInfo(TIMEZONE)


def fmt_dt(dt, with_time=True):
    """Время из БД (UTC без зоны) → местное время для отчётов."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TZ).strftime("%d.%m %H:%M" if with_time else "%d.%m")


def fmt_ago(dt):
    """«3 ч», «2 дн» — сколько прошло с момента dt."""
    if dt is None:
        return ""
    from datetime import datetime
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    minutes = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} мин"
    if minutes < 48 * 60:
        return f"{minutes // 60} ч"
    return f"{minutes // (24 * 60)} дн"


def student_label(student_id, name):
    return name or f"id:{student_id}"


SUBMIT_HINTS = {
    "screenshots": "Отправь скриншоты (можно альбомом) и, если нужно, текст 📚",
    "checklist": ("Пришли ссылку на таблицу с чек-листом 📊\n"
                  "Google Таблицы, Яндекс.Диск или OneDrive — с доступом «Все, у кого есть ссылка». "
                  "Можно вместо ссылки прислать файл .xlsx."),
    "jira": ("Пришли ссылки на свои задачи в Jira 🐞\n"
             "Можно номером со ссылкой («353 сделал»), адресом задачи (…/browse/KAN-353) или ключом: KAN-353."),
}


def submit_hint(kind):
    return SUBMIT_HINTS.get(kind or "screenshots", SUBMIT_HINTS["screenshots"])


BTN_STATS = "📊 Сводка"
BTN_PENDING = "🕐 Ждут проверки"
BTN_STUDENTS = "👥 Ученики"
BTN_EXPORT = "📥 Выгрузка в Excel"
BTN_SETTINGS = "⚙️ Настройки ИИ"


def staff_keyboard(user_id):
    """Клавиатура админа/проверяющего. None — если кнопок не положено."""
    if is_admin(user_id):
        rows = [
            [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_PENDING)],
            [KeyboardButton(text=BTN_STUDENTS), KeyboardButton(text=BTN_EXPORT)],
            [KeyboardButton(text=BTN_SETTINGS)],
        ]
    elif is_reviewer(user_id):
        rows = [[KeyboardButton(text=BTN_PENDING)]]
    else:
        return None
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_user_display(user: types.User):
    if user.username:
        return f"@{user.username}"
    if user.full_name:
        return user.full_name
    return f"id:{user.id}"


def clip(text, limit=MAX_TEXT):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def split_text(text, limit=MAX_TEXT, max_parts=6):
    """Длинный текст → несколько сообщений, по границам абзацев/строк."""
    text = text or ""
    parts = []
    while len(text) > limit and len(parts) < max_parts - 1:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(clip(text, limit))
    return parts


async def send_long(chat_id, text, reply_markup=None):
    """Отправляет длинный текст частями; кнопки — у последней части."""
    parts = split_text(text)
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part, reply_markup=reply_markup if i == len(parts) - 1 else None)


def student_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_NEW)], [KeyboardButton(text=BTN_MY)]],
        resize_keyboard=True,
    )


def review_keyboard(homework_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Принять", callback_data=f"accept_{homework_id}"),
            InlineKeyboardButton(text="💬 Принять с комментарием", callback_data=f"commentaccept_{homework_id}"),
        ],
        [InlineKeyboardButton(text="Доработка", callback_data=f"revise_{homework_id}")],
    ])


def submit_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_send"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_send"),
    ]])


def review_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_review"),
        InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_review"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_review"),
    ]])


def fix_keyboard(homework_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✏️ Отправить доработку по ДЗ #{homework_id}",
                             callback_data=f"fix_{homework_id}"),
    ]])


def homework_title(hw):
    title = f"ДЗ #{hw['id']}"
    if hw.get("assignment_title"):
        title += f" «{hw['assignment_title']}»"
    return title


def extract_content(messages):
    """Текст и файлы из одного сообщения или альбома.

    Ссылки, спрятанные под текстом (например «353» → задача в Jira),
    дописываются в конец, иначе они теряются при пересылке текста.
    """
    texts, files, links = [], [], []
    for msg in messages:
        text = msg.text or msg.caption
        if text:
            texts.append(text)
        for entity in (msg.entities or msg.caption_entities or []):
            if entity.type == "text_link" and entity.url not in links:
                links.append(entity.url)

        if msg.photo:
            files.append(["photo", msg.photo[-1].file_id])
        elif msg.document:
            files.append(["document", msg.document.file_id])
        elif msg.video:
            files.append(["video", msg.video.file_id])

    text = "\n".join(texts)
    if links:
        text += ("\n\n" if text else "") + "Ссылки:\n" + "\n".join(links)
    return text, files


def merge_text(old, new):
    if old and new:
        return old + "\n" + new
    return old or new


async def send_files(chat_id, files):
    photos = [file_id for file_type, file_id in files if file_type == "photo"]
    for i in range(0, len(photos), 10):  # в альбоме максимум 10 файлов
        chunk = photos[i:i + 10]
        if len(chunk) == 1:
            await bot.send_photo(chat_id, chunk[0])
        else:
            await bot.send_media_group(chat_id, [InputMediaPhoto(media=f) for f in chunk])

    for file_type, file_id in files:
        if file_type == "document":
            await bot.send_document(chat_id, file_id)
        elif file_type == "video":
            await bot.send_video(chat_id, file_id)


async def notify_reviewers(text, files, homework_id, extra_rows=None):
    keyboard = review_keyboard(homework_id)
    if extra_rows:
        keyboard = InlineKeyboardMarkup(inline_keyboard=list(extra_rows) + keyboard.inline_keyboard)
    for reviewer in REVIEWERS:
        try:
            await send_files(reviewer, files)
            await send_long(reviewer, text, reply_markup=keyboard)
        except Exception:
            log.exception("Не удалось отправить ДЗ #%s проверяющему %s", homework_id, reviewer)


async def gather_album(message, handler):
    """Собирает альбом в один вызов handler(messages)."""
    group_id = message.media_group_id
    if not group_id:
        await handler([message])
        return

    _albums[group_id].append(message)
    task = _album_tasks.get(group_id)
    if task:
        task.cancel()
    _album_tasks[group_id] = asyncio.create_task(_flush_album(group_id, handler))


async def _flush_album(group_id, handler):
    try:
        await asyncio.sleep(ALBUM_DELAY)
    except asyncio.CancelledError:
        return
    messages = _albums.pop(group_id, [])
    _album_tasks.pop(group_id, None)
    if messages:
        try:
            await handler(messages)
        except Exception:
            log.exception("Ошибка обработки альбома %s", group_id)




async def read_limited(response, limit):
    """Читает тело ответа aiohttp целиком, но не больше limit+1 байт.

    Важно: response.content.read(n) возвращает только уже пришедший кусок, а не n байт —
    из-за этого большие файлы раньше обрезались.
    """
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            break
    return b"".join(chunks)


def sniff_image(data):
    """Настоящий MIME картинки по первым байтам или None, если это не картинка (например, HTML)."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


IMAGE_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp", ".gif": "image/gif"}


TABLE_EXT = (".xlsx", ".xlsm", ".xls", ".csv")
TEXT_EXT = (".txt", ".md", ".log", ".json", ".sql", ".yml", ".yaml", ".xml")
MAX_TEXT_FILE = 20_000  # символов из одного текстового файла


def decode_text(data):
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").strip()
    return text if len(text) <= MAX_TEXT_FILE else text[:MAX_TEXT_FILE] + "\n… файл обрезан"


async def download_attachment(file_type, file_id):
    """Скачивает вложение из Telegram и определяет, что это.

    ('image', mime, bytes)      — картинка, её можно показать ИИ
    ('table', filename, bytes)  — Excel/CSV, превращается в текст
    ('text', filename, bytes)   — .txt/.md/.log/… — читается как текст
    ('other', описание, None)   — видео, PDF и прочее, ИИ не видит
    """
    if file_type == "video":
        return "other", "видео", None
    try:
        tg_file = await bot.get_file(file_id)
    except Exception as e:
        log.warning("Не удалось получить файл %s: %s", file_id, e)
        return "other", "файл не удалось скачать (возможно, больше 20 МБ)", None

    path = tg_file.file_path or ""
    ext = os.path.splitext(path)[1].lower()
    if file_type == "photo" or ext in IMAGE_EXT:
        data = (await bot.download_file(path)).read()
        mime = sniff_image(data)
        if not mime:
            return "other", f"картинка {ext or ''} в неподдерживаемом формате", None
        return "image", mime, data
    if ext in TABLE_EXT:
        buffer = await bot.download_file(path)
        return "table", os.path.basename(path), buffer.read()
    if ext in TEXT_EXT:
        buffer = await bot.download_file(path)
        return "text", os.path.basename(path), buffer.read()
    return "other", f"файл {ext or 'без расширения'}", None


async def download_image(file_type, file_id):
    """(mime, bytes) для картинок и (None, описание) для остального."""
    kind, meta, data = await download_attachment(file_type, file_id)
    if kind == "image":
        return meta, data
    return None, (f"{'таблица' if kind == 'table' else 'текстовый файл'} {meta}") if kind in ("table", "text") else meta
