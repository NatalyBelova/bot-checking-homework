"""Поддержка: ученик пишет вопрос в бота, проверяющие отвечают через бота.

Ответ приходит ученику от имени бота — личный аккаунт проверяющего ученик не видит.
"""
from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
from common import (
    STATUS_LABELS, SUPPORT, SUPPORT_REPLY, bot, clip, deny_callback, extract_content, get_user_display,
    homework_title, is_reviewer, is_staff, log, send_files, send_long, student_keyboard,
    staff_keyboard,
)
from config import REVIEWERS

router = Router()

CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="supno")]])


def _ticket_keyboard(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"supr_{ticket_id}")],
        [InlineKeyboardButton(text="✅ Закрыть без ответа", callback_data=f"supc_{ticket_id}")],
    ])


async def start_support(message: types.Message):
    if is_staff(message.from_user.id):
        await message.answer("Поддержка — для учеников. Обращения учеников приходят сюда ✏️")
        return
    await db.set_state(message.from_user.id, SUPPORT, {})
    await message.answer(
        "🆘 Напиши свой вопрос одним сообщением — можно со скриншотом.\n"
        "Если вопрос про конкретное ДЗ, укажи его номер (например, ДЗ #12).",
        reply_markup=CANCEL_KB)


@router.callback_query(F.data == "supno")
async def cancel_support(callback: types.CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.clear_state(callback.from_user.id)
    await callback.message.answer("Отменено", reply_markup=student_keyboard(callback.from_user.id))
    await callback.answer()


async def _student_context(student_id):
    homeworks = await db.get_homeworks_by_student(student_id)
    if not homeworks:
        return "ДЗ ещё не сдавал"
    lines = [f"{homework_title(h)} — {STATUS_LABELS.get(h['status'], h['status'])}" for h in homeworks[:3]]
    return "Последние ДЗ:\n" + "\n".join(lines)


async def receive_question(messages):
    """Ученик прислал вопрос (одно сообщение или альбом) — пересылаем проверяющим."""
    user = messages[0].from_user
    state, _ = await db.get_state(user.id)
    if state != SUPPORT:
        return
    await db.clear_state(user.id)
    text, files = extract_content(messages)
    if not (text or files):
        await bot.send_message(user.id, "Пустое сообщение — нажми «🆘 Поддержка» ещё раз и напиши вопрос.",
                               reply_markup=student_keyboard(user.id))
        return
    who = get_user_display(user)
    ticket_id = await db.add_ticket(user.id, who, text, files)
    log.info("Поддержка: обращение #%s от %s", ticket_id, user.id)
    card = (f"🆘 Обращение #{ticket_id} от {who} (id {user.id})\n\n{text or '(без текста)'}\n\n"
            f"{await _student_context(user.id)}")
    for reviewer in REVIEWERS:
        try:
            await send_files(reviewer, files)
            await send_long(reviewer, card, reply_markup=_ticket_keyboard(ticket_id))
        except Exception:
            log.exception("Не удалось отправить обращение #%s проверяющему %s", ticket_id, reviewer)
    await bot.send_message(user.id, "✅ Вопрос отправлен. Ответ придёт сюда, в этот чат.",
                           reply_markup=student_keyboard(user.id))


@router.callback_query(F.data.startswith("supr_"))
async def start_reply(callback: types.CallbackQuery):
    if not is_reviewer(callback.from_user.id):
        await deny_callback(callback)
        return
    ticket = await db.get_ticket(int(callback.data.split("_")[1]))
    if not ticket or ticket["status"] != "open":
        await callback.answer("На это обращение уже ответили", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await db.set_state(callback.from_user.id, SUPPORT_REPLY, {"ticket_id": ticket["id"], "text": "", "files": []})
    await callback.message.answer(
        f"Напиши ответ на обращение #{ticket['id']} — можно со скриншотом. Ученик получит его от имени бота.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="suprno")]]))
    await callback.answer()


@router.callback_query(F.data == "suprno")
async def cancel_reply(callback: types.CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    _, data = await db.get_state(callback.from_user.id)
    await db.clear_state(callback.from_user.id)
    ticket_id = (data or {}).get("ticket_id")
    await callback.message.answer(f"Ответ отменён. Обращение #{ticket_id} ждёт ответа.",
                                  reply_markup=_ticket_keyboard(ticket_id) if ticket_id else None)
    await callback.answer()


async def receive_reply(messages):
    """Проверяющий прислал ответ — отправляем ученику и закрываем обращение."""
    reviewer = messages[0].from_user
    state, data = await db.get_state(reviewer.id)
    if state != SUPPORT_REPLY:
        return
    await db.clear_state(reviewer.id)
    text, files = extract_content(messages)
    ticket = await db.get_ticket(data["ticket_id"])
    if not ticket:
        await bot.send_message(reviewer.id, "Обращение не найдено")
        return
    if not await db.close_ticket(ticket["id"], reviewer.id, answer=text or "(файл)"):
        await bot.send_message(reviewer.id, f"На обращение #{ticket['id']} уже ответили — твой ответ не отправлен.",
                               reply_markup=staff_keyboard(reviewer.id))
        return
    question = clip(ticket["text"] or "", 300)
    try:
        await send_long(ticket["student_id"], f"💬 Ответ поддержки на твой вопрос:\n«{question}»\n\n{text}".strip())
        await send_files(ticket["student_id"], files)
    except Exception:
        log.exception("Не удалось отправить ответ на обращение #%s", ticket["id"])
        await bot.send_message(reviewer.id, "⚠️ Не получилось отправить ответ ученику — возможно, он заблокировал бота.")
        return
    log.info("Поддержка: на обращение #%s ответил %s", ticket["id"], reviewer.id)
    await bot.send_message(reviewer.id, f"✅ Ответ на обращение #{ticket['id']} отправлен ученику",
                           reply_markup=staff_keyboard(reviewer.id))
    others = [r for r in REVIEWERS if r != reviewer.id]
    for other in others:
        try:
            await bot.send_message(other, f"Обращение #{ticket['id']}: ответил {get_user_display(reviewer)}")
        except Exception:
            log.exception("Не удалось сообщить проверяющему %s", other)


@router.callback_query(F.data.startswith("supc_"))
async def close_without_answer(callback: types.CallbackQuery):
    if not is_reviewer(callback.from_user.id):
        await deny_callback(callback)
        return
    ticket_id = int(callback.data.split("_")[1])
    await callback.message.edit_reply_markup(reply_markup=None)
    if await db.close_ticket(ticket_id, callback.from_user.id):
        await callback.message.answer(f"Обращение #{ticket_id} закрыто без ответа")
    else:
        await callback.message.answer(f"Обращение #{ticket_id} уже закрыто")
    await callback.answer()


async def open_tickets_text():
    tickets = await db.open_tickets()
    if not tickets:
        return None, None
    lines = [f"🆘 Обращения без ответа: {len(tickets)}"]
    rows = []
    for t in tickets[:20]:
        lines.append(f"#{t['id']} {t['student_name'] or t['student_id']}: {clip((t['text'] or '(файл)').replace(chr(10), ' '), 80)}")
        rows.append([InlineKeyboardButton(text=f"✉️ Ответить на #{t['id']}", callback_data=f"supr_{t['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
