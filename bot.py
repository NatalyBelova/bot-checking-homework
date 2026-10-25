import asyncio
import logging
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import db
from config import REVIEWERS, TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("homework_bot")

bot = Bot(token=TOKEN)
dp = Dispatcher()

BTN_NEW = "📤 Отправить новое ДЗ"
BTN_MY = "📋 Мои ДЗ"

# Состояния (хранятся в таблице user_states и переживают перезапуск бота)
SUBMIT = "submit"  # ученик собирает отправку: data = {mode, assignment_id|homework_id, text, files}
REVIEW = "review"  # проверяющий пишет комментарий: data = {homework_id, action, text, files}

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


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def is_reviewer(user_id):
    return user_id in REVIEWERS


def get_user_display(user: types.User):
    if user.username:
        return f"@{user.username}"
    if user.full_name:
        return user.full_name
    return f"id:{user.id}"


def clip(text, limit=MAX_TEXT):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


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


async def notify_reviewers(text, files, homework_id):
    for reviewer in REVIEWERS:
        try:
            await send_files(reviewer, files)
            await bot.send_message(reviewer, clip(text), reply_markup=review_keyboard(homework_id))
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


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def start(message: types.Message):
    if is_reviewer(message.from_user.id):
        await message.answer(
            "Ты в режиме проверки ДЗ ✏️\n\n"
            "/tasks — список заданий\n"
            "/addtask Название — добавить задание\n"
            "/cancel — сбросить текущее действие",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await message.answer("Выбери действие 👇", reply_markup=student_keyboard())


@dp.message(Command("cancel"))
async def cancel(message: types.Message):
    await db.clear_state(message.from_user.id)
    if is_reviewer(message.from_user.id):
        await message.answer("Действие сброшено")
    else:
        await message.answer("Действие сброшено", reply_markup=student_keyboard())


@dp.message(Command("tasks"))
async def list_tasks(message: types.Message):
    if not is_reviewer(message.from_user.id):
        return
    assignments = await db.list_assignments(active_only=False)
    if not assignments:
        await message.answer("Заданий пока нет. Добавь: /addtask Название")
        return
    lines = ["📚 Задания:\n"]
    for a in assignments:
        flags = []
        if not a["is_active"]:
            flags.append("скрыто")
        if not a["reference"]:
            flags.append("нет эталона")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"#{a['id']} {a['title']} [{a['kind']}]{suffix}")
    await message.answer("\n".join(lines))


@dp.message(Command("addtask"))
async def add_task(message: types.Message, command: CommandObject):
    if not is_reviewer(message.from_user.id):
        return
    title = (command.args or "").strip()
    if not title:
        await message.answer("Напиши название после команды: /addtask Урок 1. Чек-лист")
        return
    assignment_id = await db.add_assignment(title)
    await message.answer(f"Задание #{assignment_id} «{title}» добавлено ✅")


# ---------------------------------------------------------------------------
# Ученик: выбор задания и отправка
# ---------------------------------------------------------------------------

async def start_new_homework(message: types.Message):
    user_id = message.from_user.id
    if is_reviewer(user_id):
        await message.answer("Ты не можешь отправлять ДЗ ❌")
        return

    assignments = await db.list_assignments()
    if not assignments:
        # задания ещё не заведены — работаем как раньше, без выбора
        await db.set_state(user_id, SUBMIT, {"mode": "new", "assignment_id": None, "text": "", "files": []})
        await message.answer("Отправь домашнее задание 📚")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a["title"], callback_data=f"task_{a['id']}")]
        for a in assignments
    ])
    await message.answer("Выбери задание 👇", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("task_"))
async def choose_task(callback: types.CallbackQuery):
    assignment_id = int(callback.data.split("_")[1])
    assignment = await db.get_assignment(assignment_id)
    if not assignment or not assignment["is_active"]:
        await callback.answer("Задание недоступно", show_alert=True)
        return

    await db.set_state(callback.from_user.id, SUBMIT,
                       {"mode": "new", "assignment_id": assignment_id, "text": "", "files": []})
    await callback.message.edit_text(
        f"Задание: «{assignment['title']}»\n\n"
        "Отправь скриншоты, файлы или ссылку 📚\n"
        "Можно несколькими сообщениями — потом нажми «Отправить»."
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("fix_"))
async def start_fix(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])
    hw = await db.get_homework(homework_id)
    if not hw or hw["student_id"] != callback.from_user.id:
        await callback.answer("ДЗ не найдено", show_alert=True)
        return
    if hw["status"] != "revision":
        await callback.answer("Это ДЗ сейчас не на доработке", show_alert=True)
        return

    await db.set_state(callback.from_user.id, SUBMIT,
                       {"mode": "revision", "homework_id": homework_id, "text": "", "files": []})
    await callback.message.answer(
        f"Пришли исправления по {homework_title(hw)} — текстом, скриншотами или файлами ✏️\n"
        "Можно несколькими сообщениями — потом нажми «Отправить»."
    )
    await callback.answer()


async def show_my_homeworks(message: types.Message):
    homeworks = await db.get_homeworks_by_student(message.from_user.id)
    if not homeworks:
        await message.answer("У тебя пока нет отправленных ДЗ 📭")
        return

    lines = ["📋 Твои домашние задания:\n"]
    buttons = []
    for hw in homeworks:
        status = STATUS_LABELS.get(hw["status"], hw["status"])
        date = hw["created_at"].strftime("%d.%m")
        lines.append(f"📌 {homework_title(hw)} — {status} ({date})")
        if hw["status"] == "revision":
            buttons.append([InlineKeyboardButton(
                text=f"✏️ Доработка по ДЗ #{hw['id']}", callback_data=f"fix_{hw['id']}")])

    await message.answer(
        clip("\n".join(lines)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


async def add_to_submission(messages):
    """Добавляет сообщение (или альбом) ученика к текущей отправке."""
    user_id = messages[0].from_user.id
    text, files = extract_content(messages)

    async with _user_locks[user_id]:
        state, data = await db.get_state(user_id)
        if state != SUBMIT:
            return
        data["text"] = merge_text(data.get("text", ""), text)
        data["files"] = data.get("files", []) + files
        await db.set_state(user_id, SUBMIT, data)

    count = len(data["files"])
    summary = f"Файлов в отправке: {count}. " if count else ""
    await bot.send_message(
        user_id,
        f"{summary}Можешь прислать ещё или отправить 👇",
        reply_markup=submit_keyboard(),
    )


@dp.callback_query(F.data == "confirm_send")
async def confirm_send(callback: types.CallbackQuery):
    user = callback.from_user
    await callback.message.edit_reply_markup(reply_markup=None)

    async with _user_locks[user.id]:
        state, data = await db.get_state(user.id)
        if state != SUBMIT or not (data.get("text") or data.get("files")):
            await callback.answer("Нечего отправлять", show_alert=True)
            return
        await db.clear_state(user.id)

    text, files = data.get("text", ""), data.get("files", [])
    user_display = get_user_display(user)

    if data["mode"] == "revision":
        homework_id = data["homework_id"]
        hw = await db.get_homework(homework_id)
        if not hw or hw["student_id"] != user.id or hw["status"] != "revision":
            await callback.answer("Это ДЗ сейчас не на доработке", show_alert=True)
            return
        version = await db.add_version(homework_id, text, files)
        header = f"🔄 Доработка (попытка {version}) по {homework_title(hw)} от {user_display}"
        await notify_reviewers(f"{header}\n\n{text}".strip(), files, homework_id)
        await callback.message.answer(f"Доработка по ДЗ #{homework_id} отправлена ✅")
    else:
        homework_id = await db.create_homework(
            user.id, data.get("assignment_id"), text, files, student_name=user_display)
        hw = await db.get_homework(homework_id)
        header = f"🆕 Новое {homework_title(hw)} от {user_display}"
        await notify_reviewers(f"{header}\n\n{text}".strip(), files, homework_id)
        await callback.message.answer(
            f"ДЗ #{homework_id} отправлено на проверку ✅\n\nМожешь отправить ещё одно 👇",
            reply_markup=student_keyboard(),
        )

    await callback.answer()


@dp.callback_query(F.data == "cancel_send")
async def cancel_send(callback: types.CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.clear_state(callback.from_user.id)
    await callback.message.answer("Отправка отменена ❌", reply_markup=student_keyboard())
    await callback.answer()


# ---------------------------------------------------------------------------
# Проверяющий
# ---------------------------------------------------------------------------

async def _pending_homework_or_alert(callback, homework_id):
    hw = await db.get_homework(homework_id)
    if not hw:
        await callback.answer("ДЗ не найдено", show_alert=True)
        return None
    if hw["status"] != "new":
        await callback.answer(f"ДЗ #{homework_id} уже проверено: {STATUS_LABELS.get(hw['status'], hw['status'])}",
                              show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return None
    return hw


@dp.callback_query(F.data.startswith("accept_"))
async def accept(callback: types.CallbackQuery):
    if not is_reviewer(callback.from_user.id):
        await callback.answer()
        return
    homework_id = int(callback.data.split("_")[1])
    hw = await _pending_homework_or_alert(callback, homework_id)
    if not hw:
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await db.update_status(homework_id, "accepted")
    await db.add_comment(homework_id, None, reviewed_by=f"human:{callback.from_user.id}")
    await bot.send_message(hw["student_id"], f"✅ {homework_title(hw)} принято!")
    await callback.message.answer(f"ДЗ #{homework_id} принято ✅")
    await callback.answer()


async def _start_review(callback, action):
    if not is_reviewer(callback.from_user.id):
        await callback.answer()
        return
    homework_id = int(callback.data.split("_")[1])
    hw = await _pending_homework_or_alert(callback, homework_id)
    if not hw:
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await db.set_state(callback.from_user.id, REVIEW,
                       {"homework_id": homework_id, "action": action, "text": "", "files": []})
    if action == "accept":
        await callback.message.answer(f"Напиши комментарий к принятому ДЗ #{homework_id}")
    else:
        await callback.message.answer(f"Напиши комментарий для доработки по ДЗ #{homework_id}")
    await callback.answer()


@dp.callback_query(F.data.startswith("commentaccept_"))
async def accept_with_comment(callback: types.CallbackQuery):
    await _start_review(callback, "accept")


@dp.callback_query(F.data.startswith("revise_"))
async def revise(callback: types.CallbackQuery):
    await _start_review(callback, "revise")


async def add_to_review(messages):
    user_id = messages[0].from_user.id
    text, files = extract_content(messages)

    async with _user_locks[user_id]:
        state, data = await db.get_state(user_id)
        if state != REVIEW:
            return
        data["text"] = merge_text(data.get("text", ""), text)
        data["files"] = data.get("files", []) + files
        await db.set_state(user_id, REVIEW, data)

    prompt = "Отправить принятие ДЗ?" if data["action"] == "accept" else "Отправить доработку?"
    await bot.send_message(user_id, prompt, reply_markup=review_confirm_keyboard())


@dp.callback_query(F.data == "confirm_review")
async def confirm_review(callback: types.CallbackQuery):
    reviewer_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)

    async with _user_locks[reviewer_id]:
        state, data = await db.get_state(reviewer_id)
        if state != REVIEW:
            await callback.answer("Нет данных", show_alert=True)
            return
        await db.clear_state(reviewer_id)

    homework_id = data["homework_id"]
    hw = await db.get_homework(homework_id)
    if not hw or hw["status"] != "new":
        await callback.message.answer(f"ДЗ #{homework_id} уже проверено, комментарий не отправлен")
        await callback.answer()
        return

    text, files = data.get("text", ""), data.get("files", [])
    student_id = hw["student_id"]
    await db.add_comment(homework_id, text, reviewed_by=f"human:{reviewer_id}")

    if data["action"] == "accept":
        await db.update_status(homework_id, "accepted")
        caption = f"✅ {homework_title(hw)} принято!"
        if text:
            caption += f"\n\n{text}"
        await bot.send_message(student_id, clip(caption))
        await send_files(student_id, files)
        await callback.message.answer(f"ДЗ #{homework_id} принято ✅")
    else:
        await db.update_status(homework_id, "revision")
        caption = f"🔄 {homework_title(hw)}: нужно доработать"
        if text:
            caption += f"\n\n{text}"
        await send_files(student_id, files)
        await bot.send_message(student_id, clip(caption), reply_markup=fix_keyboard(homework_id))

        # если ученик ничего не делает в боте — сразу ждём от него исправления
        student_state, _ = await db.get_state(student_id)
        if student_state is None:
            await db.set_state(student_id, SUBMIT,
                               {"mode": "revision", "homework_id": homework_id, "text": "", "files": []})
        await callback.message.answer(f"Комментарий по ДЗ #{homework_id} отправлен ученику ✏️")

    await callback.answer()


@dp.callback_query(F.data == "add_review")
async def add_review(callback: types.CallbackQuery):
    await callback.message.answer("Можешь отправить ещё текст или файлы 👍")
    await callback.answer()


@dp.callback_query(F.data == "cancel_review")
async def cancel_review(callback: types.CallbackQuery):
    reviewer_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)
    _, data = await db.get_state(reviewer_id)
    await db.clear_state(reviewer_id)

    homework_id = data.get("homework_id")
    if homework_id:
        await callback.message.answer(
            f"Отменено ❌ Что сделать с ДЗ #{homework_id}?",
            reply_markup=review_keyboard(homework_id),
        )
    else:
        await callback.message.answer("Отменено ❌")
    await callback.answer()


# ---------------------------------------------------------------------------
# Все остальные сообщения
# ---------------------------------------------------------------------------

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id

    if message.text == BTN_MY:
        await show_my_homeworks(message)
        return
    if message.text == BTN_NEW:
        await start_new_homework(message)
        return

    state, _ = await db.get_state(user_id)

    if state == REVIEW:
        await gather_album(message, add_to_review)
        return
    if state == SUBMIT:
        await gather_album(message, add_to_submission)
        return

    # альбом приходит пачкой — отвечаем один раз
    if message.media_group_id:
        if message.media_group_id in _idle_albums:
            return
        _idle_albums.add(message.media_group_id)
        asyncio.get_running_loop().call_later(30, _idle_albums.discard, message.media_group_id)

    if is_reviewer(user_id):
        await message.answer("Ожидай новое ДЗ от ученика ✏️")
    else:
        await message.answer("Нажми кнопку «📤 Отправить новое ДЗ», чтобы начать 👇",
                             reply_markup=student_keyboard())


async def main():
    db.init_db()
    log.info("Проверяющие: %s", REVIEWERS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
