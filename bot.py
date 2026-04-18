import asyncio
import db
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from config import TOKEN, REVIEWERS
from collections import defaultdict
from aiogram.types import InputMediaPhoto



# Инициализация бота и диспетчера
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализация базы данных (создание таблиц)
db.init_db()

# Состояние: проверяющий написал "доработка" и бот ждёт комментарий
review_state = {}

# Временное хранение ДЗ до подтверждения отправки
pending_homeworks = {}
pending_reviews = {}

media_groups = defaultdict(list)
media_tasks = {}

review_media_groups = defaultdict(list)
review_media_tasks = {}

waiting_for_homework = set()


# Команда /start
@dp.message(Command("start"))
async def start(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📤 Отправить новое ДЗ")],
            [types.KeyboardButton(text="📋 Мои ДЗ")]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Выбери действие 👇",
        reply_markup=keyboard
    )

async def process_media_group(group_id, user_id):
    await asyncio.sleep(0.8)  # ждем все сообщения альбома

    messages = media_groups.pop(group_id, [])
    media_tasks.pop(group_id, None)

    if not messages:
        return

    files = []
    text = ""

    for msg in messages:
        if msg.caption and not text:
            text = msg.caption

        if msg.photo:
            files.append(("photo", msg.photo[-1].file_id))

        elif msg.document:
            files.append(("document", msg.document.file_id))

    pending_homeworks[user_id] = {
        "text": text,
        "files": files,
        "file_type": "photo_group"
    }

    await send_confirm(user_id, messages[-1])

async def process_single_message(message, user_id):
    file_id = None
    file_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"

    pending_homeworks[user_id] = {
        "text": message.text or message.caption or "",
        "file_id": file_id,
        "file_type": file_type
    }

    await send_confirm(user_id, message)

async def send_confirm(user_id, message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_send"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_send")
        ]
    ])

    await message.answer("📤 Отправить домашнее задание?", reply_markup=keyboard)

async def collect_review_single(message, user_id):
    homework_id = review_state[user_id]

    text = message.text or message.caption or ""

    file = None

    if message.document:
        file = ("document", message.document.file_id)
    elif message.photo:
        file = ("photo", message.photo[-1].file_id)
    elif message.video:
        file = ("video", message.video.file_id)

    # если уже есть данные — дополняем
    review = pending_reviews.get(user_id, {"text": "", "files": []})

    if text:
        if review["text"]:
            review["text"] += "\n" + text
        else:
            review["text"] = text

    if file:
        review["files"].append(file)

    pending_reviews[user_id] = review

    # кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_review"),
            InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_review"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_review")
        ]
    ])

    await message.answer("Отправить доработку?", reply_markup=keyboard)

async def collect_review_media_group(group_id, user_id):
    await asyncio.sleep(0.8)

    messages = review_media_groups.pop(group_id, [])
    review_media_tasks.pop(group_id, None)

    if not messages:
        return

    review = pending_reviews.get(user_id, {"text": "", "files": []})

    for msg in messages:
        if msg.caption:
            if review["text"]:
                review["text"] += "\n" + msg.caption
            else:
                review["text"] = msg.caption

        if msg.photo:
            review["files"].append(("photo", msg.photo[-1].file_id))
        elif msg.document:
            review["files"].append(("document", msg.document.file_id))

    pending_reviews[user_id] = review

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_review"),
            InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_review"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_review")
        ]
    ])

    await bot.send_message(user_id, "Отправить доработку?", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "confirm_review")
async def confirm_review(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    await callback.message.edit_reply_markup(reply_markup=None)

    review = pending_reviews.get(user_id)

    if not review:
        await callback.answer("Нет данных")
        return

    homework_id = review_state[user_id]
    student_id = db.get_student_id(homework_id)

    text = review.get("text", "")
    files = review.get("files", [])

    # обновляем БД
    db.update_status(homework_id, "revision")
    db.add_comment(homework_id, text)

    # текст
    caption = "Нужно доработать"
    if text:
        caption += f":\n{text}"

    await bot.send_message(student_id, caption)

    # файлы
    photos = [f for f in files if f[0] == "photo"]
    docs = [f for f in files if f[0] == "document"]
    videos = [f for f in files if f[0] == "video"]

    if photos:
        media = [InputMediaPhoto(media=file_id) for _, file_id in photos]
        await bot.send_media_group(student_id, media)

    for _, file_id in docs:
        await bot.send_document(student_id, file_id)

    for _, file_id in videos:
        await bot.send_video(student_id, file_id)

    await bot.send_message(user_id, "Комментарий отправлен ученику ✏️")

    # очистка
    pending_reviews.pop(user_id, None)
    del review_state[user_id]

    await callback.answer()

@dp.callback_query(lambda c: c.data == "add_review")
async def add_review(callback: types.CallbackQuery):
    await callback.answer("Можешь отправить ещё текст или файлы 👍")

@dp.callback_query(lambda c: c.data == "cancel_review")
async def cancel_review(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    await callback.message.edit_reply_markup(reply_markup=None)

    pending_reviews.pop(user_id, None)

    await callback.message.answer(
        "Отправка отменена ❌\n\nНапиши комментарий для доработки"
    )

    await callback.answer()


# Основной обработчик сообщений (ученик + комментарии)
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id

    # 📋 мои ДЗ
    if message.text == "📋 Мои ДЗ":
        homeworks = db.get_homeworks_by_student(user_id)

        if not homeworks:
            await message.answer("У тебя пока нет отправленных ДЗ 📭")
            return

        status_map = {
            "new": "🆕 Новое",
            "revision": "🔄 На доработке",
            "accepted": "✅ Принято"
        }

        text = "📋 Твои домашние задания:\n\n"

        for hw_id, status, created_at in homeworks:
            pretty_status = status_map.get(status, status)
            date = created_at.strftime("%d.%m")

            text += f"📌 ДЗ #{hw_id} — {pretty_status} ({date})\n"

        await message.answer(text)
        return

    # новое ДЗ
    if message.text == "📤 Отправить новое ДЗ":
        waiting_for_homework.add(user_id)
        await message.answer("Отправь домашнее задание 📚")
        return

    # блокируем всё, кроме разрешённых действий
    if (
        user_id not in waiting_for_homework
        and user_id not in review_state
        and message.text != "📋 Мои ДЗ"
    ):
        if message.text:
            await message.answer("Нажми '📤 Отправить новое ДЗ', чтобы начать 👇")
        return

    # --- 1. комментарий от валидатора ---
    if user_id in review_state:

        if message.media_group_id:
            group_id = message.media_group_id

            review_media_groups[group_id].append(message)

            if group_id in review_media_tasks:
                review_media_tasks[group_id].cancel()

            review_media_tasks[group_id] = asyncio.create_task(
                collect_review_media_group(group_id, user_id)
            )

            return

        await collect_review_single(message, user_id)
        return

    # --- 2. если альбом (ДЗ) ---
    if message.media_group_id:
        group_id = message.media_group_id

        media_groups[group_id].append(message)

        if group_id in media_tasks:
            media_tasks[group_id].cancel()

        media_tasks[group_id] = asyncio.create_task(
            process_media_group(group_id, user_id)
        )

        return

    # --- 3. обычное ДЗ ---
    file_id = None
    file_type = None

    if message.document:
        file_id = message.document.file_id
        file_type = "document"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"

    text = message.text or message.caption or ""

    pending_homeworks[user_id] = {
        "text": text,
        "file_id": file_id,
        "file_type": file_type,
    }

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_send"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_send")
        ]
    ])

    await message.answer("📤 Отправить домашнее задание?", reply_markup=keyboard)


# Принять ДЗ
@dp.callback_query(lambda c: c.data.startswith("accept"))
async def accept(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])

    # убираем кнопки
    await callback.message.edit_reply_markup(reply_markup=None)

    # меняем статус
    db.update_status(homework_id, "accepted")

    # уведомляем ученика
    student_id = db.get_student_id(homework_id)

    # 🔥 чистим pending
    pending_homeworks.pop(student_id, None)

    await bot.send_message(
        student_id,
        f"ДЗ #{homework_id} принято ✅"
    )

    # сообщение валидатору в чат
    await callback.message.answer("ДЗ принято ✅")

    await callback.answer()


# Отправить на доработку
@dp.callback_query(lambda c: c.data.startswith("revise"))
async def revise(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])

    # убираем кнопки
    await callback.message.edit_reply_markup(reply_markup=None)

    # сохраняем, что ждём комментарий от этого проверяющего
    review_state[callback.from_user.id] = homework_id

    await callback.message.answer("Напиши комментарий для доработки")
    await callback.answer()


# Подтверждение отправки ДЗ
@dp.callback_query(lambda c: c.data == "confirm_send")
async def confirm_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    # убираем кнопки
    await callback.message.edit_reply_markup(reply_markup=None)

    # достаём сохранённое ДЗ
    data = pending_homeworks.get(user_id)

    if not data:
        await callback.answer("Нет данных")
        return

    text = data.get("text", "")

    # ВСЕГДА создаём новое ДЗ
    homework_id = db.create_homework(
        user_id,
        text,
        None,
        data.get("file_type")
    )

    title = f"Новое ДЗ #{homework_id}"
    caption = f"{title}\n{text or ''}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Принять", callback_data=f"accept_{homework_id}"),
            InlineKeyboardButton(text="Доработка", callback_data=f"revise_{homework_id}")
        ]
    ])

    # --- отправка ревьюерам ---
    for reviewer in REVIEWERS:

        # если альбом (несколько файлов)
        if data.get("file_type") == "photo_group":
            files = data.get("files", [])

            # делим на фото и документы
            photos = [f for f in files if f[0] == "photo"]
            docs = [f for f in files if f[0] == "document"]

            # --- фото альбомом ---
            if photos:
                media = [InputMediaPhoto(media=file_id) for _, file_id in photos]
                await bot.send_media_group(reviewer, media)

            # --- документы по одному ---
            for _, file_id in docs:
                await bot.send_document(reviewer, file_id)

            # --- текст + кнопки ---
            await bot.send_message(
                reviewer,
                caption,
                reply_markup=keyboard
            )

        # --- одиночный файл ---
        else:
            file_id = data.get("file_id")
            file_type = data.get("file_type")

            if file_id:
                if file_type == "photo":
                    await bot.send_photo(reviewer, file_id, caption=caption, reply_markup=keyboard)
                elif file_type == "video":
                    await bot.send_video(reviewer, file_id, caption=caption, reply_markup=keyboard)
                else:
                    await bot.send_document(reviewer, file_id, caption=caption, reply_markup=keyboard)
            else:
                await bot.send_message(reviewer, caption, reply_markup=keyboard)

    await callback.message.answer(
        f"ДЗ #{homework_id} отправлено на проверку ✅\n\n"
        f"Можешь отправить ещё одно 👇"
    )
    await callback.answer()

    pending_homeworks.pop(user_id, None)

    waiting_for_homework.discard(user_id)


# Отмена отправки
@dp.callback_query(lambda c: c.data == "cancel_send")
async def cancel_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    # убираем кнопки
    await callback.message.edit_reply_markup(reply_markup=None)

    # удаляем из временного хранилища
    pending_homeworks.pop(user_id, None)

    await callback.message.answer("Отправка отменена ❌")
    await callback.answer()


# Запуск бота
async def main():
    print(f"REWIEVERS: {REVIEWERS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())