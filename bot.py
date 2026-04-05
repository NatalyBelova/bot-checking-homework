import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command

from config import TOKEN, REVIEWERS
import db

bot = Bot(token=TOKEN)
dp = Dispatcher()

db.init_db()

# 👉 хранение состояния "ждём комментарий"
review_state = {}

pending_homeworks = {}


# 🟢 START
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Отправь домашку текстом")


# 📨 Ученик отправляет ДЗ
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id

    text = message.text or message.caption
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

    # 🟡 комментарий от проверяющего
    if user_id in review_state:
        homework_id = review_state[user_id]

        db.update_status(homework_id, "revision")
        db.add_comment(homework_id, text)

        student_id = db.get_student_id(homework_id)

        await bot.send_message(
            student_id,
            f"Нужно доработать:\n{text}"
        )

        del review_state[user_id]
        return

    # 🟢 сохраняем как pending
    pending_homeworks[user_id] = {
        "text": text,
        "file_id": file_id,
        "file_type": file_type
    }

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_send"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_send")
        ]
    ])

    await message.answer("Отправить домашнее задание?", reply_markup=keyboard)



# ✅ Принять
@dp.callback_query(lambda c: c.data.startswith("accept"))
async def accept(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])

    db.update_status(homework_id, "accepted")

    student_id = db.get_student_id(homework_id)

    await bot.send_message(student_id, "ДЗ принято ✅")
    await callback.answer("Принято")


# ✏️ На доработку
@dp.callback_query(lambda c: c.data.startswith("revise"))
async def revise(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])

    review_state[callback.from_user.id] = homework_id

    await callback.message.answer("Напиши комментарий для доработки")
    await callback.answer()



@dp.callback_query(lambda c: c.data == "confirm_send")
async def confirm_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    data = pending_homeworks.get(user_id)

    if not data:
        await callback.answer("Нет данных")
        return

    text = data["text"]
    file_id = data["file_id"]
    file_type = data["file_type"]

    existing_hw = db.get_active_homework(user_id)

    if existing_hw:
        db.add_version(existing_hw, text, file_id, file_type)
        homework_id = existing_hw
        title = f"ДЗ #{homework_id} (доработка)"
    else:
        homework_id = db.create_homework(user_id, text, file_id, file_type)
        title = f"Новое ДЗ #{homework_id}"

    caption = f"{title}\n{text or ''}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Принять", callback_data=f"accept_{homework_id}"),
            InlineKeyboardButton(text="Доработка", callback_data=f"revise_{homework_id}")
        ]
    ])

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

    del pending_homeworks[user_id]

@dp.callback_query(lambda c: c.data == "cancel_send")
async def cancel_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    pending_homeworks.pop(user_id, None)

    await callback.message.answer("Отправка отменена ❌")
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())