import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command

from config import TOKEN, REVIEWERS
import db

# Инициализация бота и диспетчера
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализация базы данных (создание таблиц)
db.init_db()

# Состояние: проверяющий написал "доработка" и бот ждёт комментарий
review_state = {}

# Временное хранение ДЗ до подтверждения отправки
pending_homeworks = {}


# Команда /start
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Отправь домашку текстом")


# Основной обработчик сообщений (ученик + комментарии)
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id

    # Получаем текст (или подпись к фото)
    text = message.text or message.caption

    # Определяем файл (если есть)
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

    # комментарий от проверяющего
    if user_id in review_state:
        homework_id = review_state[user_id]

        # меняем статус и сохраняем комментарий
        db.update_status(homework_id, "revision")
        db.add_comment(homework_id, text)

        # отправляем комментарий ученику
        student_id = db.get_student_id(homework_id)

        await bot.send_message(
            student_id,
            f"Нужно доработать:\n{text}"
        )

        # очищаем состояние
        del review_state[user_id]
        return

    # получаем активное ДЗ
    existing_hw = db.get_active_homework(user_id)

    is_revision = False

    if existing_hw:
        status = db.get_homework_status(existing_hw)
        is_revision = (status == "revision")

    # сохраняем данные
    pending_homeworks[user_id] = {
        "text": text,
        "file_id": file_id,
        "file_type": file_type,
    }

    # Кнопки подтверждения
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_send"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_send")
        ]
    ])

    question = "🔄 Отправить доработку?" if is_revision else "📤 Отправить домашнее задание?"

    await message.answer(question, reply_markup=keyboard)


# Принять ДЗ
@dp.callback_query(lambda c: c.data.startswith("accept"))
async def accept(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])

    # меняем статус
    db.update_status(homework_id, "accepted")

    # уведомляем ученика
    student_id = db.get_student_id(homework_id)

    # 🔥 чистим pending
    pending_homeworks.pop(student_id, None)

    await bot.send_message(student_id, "ДЗ принято ✅")

    await callback.answer("Принято")


# Отправить на доработку
@dp.callback_query(lambda c: c.data.startswith("revise"))
async def revise(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])

    # сохраняем, что ждём комментарий от этого проверяющего
    review_state[callback.from_user.id] = homework_id

    await callback.message.answer("Напиши комментарий для доработки")
    await callback.answer()


# Подтверждение отправки ДЗ
@dp.callback_query(lambda c: c.data == "confirm_send")
async def confirm_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    # достаём сохранённое ДЗ
    data = pending_homeworks.get(user_id)

    if not data:
        await callback.answer("Нет данных")
        return

    text = data["text"]
    file_id = data["file_id"]
    file_type = data["file_type"]

    # проверяем: новое или доработка
    existing_hw = db.get_active_homework(user_id)

    if existing_hw:
        status = db.get_homework_status(existing_hw)

        if status == "revision":
            # это доработка
            db.add_version(existing_hw, text, file_id, file_type)
            db.update_status(existing_hw, "new")

            homework_id = existing_hw
            title = f"ДЗ #{homework_id} (доработка)"

        else:
            # новое ДЗ после accepted
            homework_id = db.create_homework(user_id, text, file_id, file_type)
            title = f"Новое ДЗ #{homework_id}"

    else:
        # вообще первое ДЗ
        homework_id = db.create_homework(user_id, text, file_id, file_type)
        title = f"Новое ДЗ #{homework_id}"

    caption = f"{title}\n{text or ''}"

    # Кнопки для проверяющего
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Принять", callback_data=f"accept_{homework_id}"),
            InlineKeyboardButton(text="Доработка", callback_data=f"revise_{homework_id}")
        ]
    ])

    # Отправка проверяющим (с учётом типа файла)
    for reviewer in REVIEWERS:
        if file_id:
            if file_type == "photo":
                await bot.send_photo(reviewer, file_id, caption=caption, reply_markup=keyboard)
            elif file_type == "video":
                await bot.send_video(reviewer, file_id, caption=caption, reply_markup=keyboard)
            else:
                await bot.send_document(reviewer, file_id, caption=caption, reply_markup=keyboard)
        else:
            await bot.send_message(reviewer, caption, reply_markup=keyboard)

    await callback.message.answer("ДЗ отправлено на проверку ✅")
    await callback.answer()

    # очищаем состояние
    pending_homeworks.pop(user_id, None)


# Отмена отправки
@dp.callback_query(lambda c: c.data == "cancel_send")
async def cancel_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id

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