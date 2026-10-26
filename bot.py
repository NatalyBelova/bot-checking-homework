import asyncio

from aiogram import Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import admin
import db
import decisions
from common import (
    ALBUM_DELAY, BTN_MY, BTN_NEW, REFERENCE, REVIEW, STATUS_LABELS, SUBMIT,
    _acting_as_student, _albums, _idle_albums, _user_locks,
    bot, clip, deny_callback, extract_content, gather_album, get_user_display,
    homework_title, submit_hint, is_admin, is_reviewer, is_staff, log, merge_text, review_confirm_keyboard,
    review_keyboard, staff_keyboard, student_keyboard, submit_keyboard,
)
from config import ADMINS, AI_MODE, DEV_MODE, OPENAI_MODEL, REVIEWERS

dp = Dispatcher()
fallback_router = Router()  # подключается последним: ловит всё, что не поймали другие


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    if is_staff(user_id):
        lines = []
        if is_admin(user_id):
            lines.append("Ты админ 📊 — кнопки внизу: сводка, ученики, выгрузка.")
        if is_reviewer(user_id):
            lines.append("Ты проверяешь ДЗ ✏️ — новые работы приходят сюда.")
        lines += [
            "",
            "/tasks — список заданий",
            "/alltasks — все задания подробно: тип, эталон, статистика",
            "/seed — загрузить задания и эталоны из репозитория (админ)",
            "/addtask Название — добавить задание",
            "/hw номер — история ДЗ",
            "/cancel — сбросить текущее действие",
        ]
        if DEV_MODE and user_id in REVIEWERS + ADMINS:
            lines.append("/switch — стать учеником (тестовый режим)")
        await message.answer("\n".join(lines), reply_markup=staff_keyboard(user_id))
        return
    await message.answer("Выбери действие 👇", reply_markup=student_keyboard())


@dp.message(Command("switch"))
async def switch_role(message: types.Message):
    """Только для тестов: проверяющий переключается между ролями."""
    user_id = message.from_user.id
    if not DEV_MODE or user_id not in REVIEWERS + ADMINS:
        await handle_message(message)
        return

    await db.clear_state(user_id)
    if user_id in _acting_as_student:
        _acting_as_student.discard(user_id)
        await message.answer("🔁 Ты снова в своей роли", reply_markup=staff_keyboard(user_id))
    else:
        _acting_as_student.add(user_id)
        await message.answer(
            "🔁 Теперь ты ученик. Карточки ДЗ всё равно будут приходить сюда — "
            "чтобы нажать «Принять»/«Доработка», вернись через /switch",
            reply_markup=student_keyboard(),
        )


@dp.message(Command("cancel"))
async def cancel(message: types.Message):
    await db.clear_state(message.from_user.id)
    if is_staff(message.from_user.id):
        await message.answer("Действие сброшено", reply_markup=staff_keyboard(message.from_user.id))
    else:
        await message.answer("Действие сброшено", reply_markup=student_keyboard())


# ---------------------------------------------------------------------------
# Ученик: выбор задания и отправка
# ---------------------------------------------------------------------------

async def start_new_homework(message: types.Message):
    user_id = message.from_user.id
    if is_staff(user_id):
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
        f"{submit_hint(assignment['kind'])}\n"
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
        f"Пришли исправления по {homework_title(hw)} ✏️\n{submit_hint(hw['assignment_kind'])}\n"
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
        decisions.start_processing(homework_id, f"{header}\n\n{text}".strip(), files)
        await callback.message.answer(f"Доработка по ДЗ #{homework_id} отправлена ✅")
    else:
        homework_id = await db.create_homework(
            user.id, data.get("assignment_id"), text, files, student_name=user_display)
        hw = await db.get_homework(homework_id)
        header = f"🆕 Новое {homework_title(hw)} от {user_display}"
        decisions.start_processing(homework_id, f"{header}\n\n{text}".strip(), files)
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
        await deny_callback(callback)
        return
    homework_id = int(callback.data.split("_")[1])
    hw = await _pending_homework_or_alert(callback, homework_id)
    if not hw:
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await decisions.apply_decision(hw, "accept", None, [], reviewed_by=f"human:{callback.from_user.id}")
    await callback.message.answer(f"ДЗ #{homework_id} принято ✅")
    await callback.answer()


async def _start_review(callback, action):
    if not is_reviewer(callback.from_user.id):
        await deny_callback(callback)
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
    reviewed_by = data.get("reviewed_by") or f"human:{reviewer_id}"
    await decisions.apply_decision(hw, data["action"], text, files, reviewed_by=reviewed_by)
    if data.get("ai_review_id"):
        await db.mark_ai_review_applied(data["ai_review_id"], reviewed_by)

    if data["action"] == "accept":
        await callback.message.answer(f"ДЗ #{homework_id} принято ✅")
    else:
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
# Предложение ИИ
# ---------------------------------------------------------------------------

async def _ai_review_or_alert(callback):
    if not is_reviewer(callback.from_user.id):
        await deny_callback(callback)
        return None, None
    review = await db.get_ai_review(int(callback.data.split("_")[1]))
    if not review or review["verdict"] not in ("accept", "revise"):
        await callback.answer("Предложение ИИ не найдено", show_alert=True)
        return None, None
    hw = await _pending_homework_or_alert(callback, review["homework_id"])
    if not hw:
        return None, None
    if review["version_id"] != await db.get_latest_version_id(hw["id"]):
        await callback.answer("Ученик уже прислал новую попытку — это предложение устарело", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return None, None
    return review, hw


@dp.callback_query(F.data.startswith("aiok_"))
async def ai_accept_suggestion(callback: types.CallbackQuery):
    review, hw = await _ai_review_or_alert(callback)
    if not review:
        return
    reviewed_by = f"ai+human:{callback.from_user.id}"
    await callback.message.edit_reply_markup(reply_markup=None)
    await decisions.apply_decision(hw, review["verdict"], review["comment"], [], reviewed_by=reviewed_by)
    await db.mark_ai_review_applied(review["id"], reviewed_by)
    result = "принято ✅" if review["verdict"] == "accept" else "отправлено на доработку 🔄"
    await callback.message.answer(f"ДЗ #{hw['id']} {result} — с комментарием ИИ")
    await callback.answer()


@dp.callback_query(F.data.startswith("aiedit_"))
async def ai_edit_suggestion(callback: types.CallbackQuery):
    review, hw = await _ai_review_or_alert(callback)
    if not review:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.set_state(callback.from_user.id, REVIEW, {
        "homework_id": hw["id"], "action": review["verdict"], "text": "", "files": [],
        "ai_review_id": review["id"], "reviewed_by": f"ai+human:{callback.from_user.id}",
    })
    action = "принятия" if review["verdict"] == "accept" else "доработки"
    await callback.message.answer(
        f"Пришли свой вариант комментария для {action} ДЗ #{hw['id']}. "
        "Текст ИИ ниже — его удобно скопировать и поправить 👇")
    await callback.message.answer(review["comment"] or "(ИИ не написал комментарий)")
    await callback.answer()


# ---------------------------------------------------------------------------
# Все остальные сообщения
# ---------------------------------------------------------------------------

@fallback_router.message()
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
    if state == REFERENCE:
        await gather_album(message, admin.add_to_reference)
        return

    # альбом приходит пачкой — отвечаем один раз
    if message.media_group_id:
        if message.media_group_id in _idle_albums:
            return
        _idle_albums.add(message.media_group_id)
        asyncio.get_running_loop().call_later(30, _idle_albums.discard, message.media_group_id)

    if is_staff(user_id):
        await message.answer("Ожидай новое ДЗ от ученика ✏️", reply_markup=staff_keyboard(user_id))
    else:
        await message.answer("Нажми кнопку «📤 Отправить новое ДЗ», чтобы начать 👇",
                             reply_markup=student_keyboard())


@fallback_router.callback_query()
async def handle_unknown_callback(callback: types.CallbackQuery):
    """Кнопка, которую никто не обработал (например, админская кнопка в роли ученика)."""
    await deny_callback(callback)


# порядок важен: сначала кнопки админа, потом «всё остальное»
dp.include_router(admin.router)
dp.include_router(fallback_router)


async def main():
    db.init_db()
    log.info("Проверяющие: %s, админы: %s", REVIEWERS, ADMINS)
    log.info("ИИ-проверка: %s%s", AI_MODE, f" ({OPENAI_MODEL})" if AI_MODE != "off" else "")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
