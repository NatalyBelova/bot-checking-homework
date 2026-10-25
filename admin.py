"""Отслеживание ДЗ: сводка, очередь на проверку, ученики, история ДЗ, выгрузка."""
from datetime import datetime

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import db
import export
from common import (
    BTN_EXPORT, BTN_PENDING, BTN_STATS, BTN_STUDENTS, STATUS_LABELS,
    clip, fmt_ago, fmt_dt, homework_title, is_admin, is_reviewer, is_staff, log,
    review_keyboard, send_files, student_label,
)

router = Router()

STUDENTS_PAGE = 15


def _admin(event):
    return is_admin(event.from_user.id)


def _staff(event):
    return is_staff(event.from_user.id)


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _reviewer_label(reviewed_by):
    if not reviewed_by:
        return "проверяющий"
    if reviewed_by == "ai":
        return "🤖 ИИ"
    return "проверяющий"


# ---------------------------------------------------------------------------
# Сводка
# ---------------------------------------------------------------------------

@router.message(F.text == BTN_STATS, _admin)
@router.message(Command("stats"), _admin)
async def stats(message: types.Message):
    o = await db.stats_overview()
    by_task = await db.stats_by_assignment()

    if not o["total"]:
        await message.answer("📊 Пока не отправлено ни одного ДЗ")
        return

    pending = f"🕐 На проверке: {o['pending']}"
    if o["oldest_pending"]:
        pending += f" (самое давнее ждёт {fmt_ago(o['oldest_pending'])})"

    lines = [
        "📊 Сводка\n",
        f"Всего ДЗ: {o['total']} · учеников: {o['students']}",
        pending,
        f"🔄 На доработке: {o['revision']}",
        f"✅ Принято: {o['accepted']}",
        "",
        f"За 7 дней: прислали {o['new_week']}, принято {o['accepted_week']}",
    ]
    if o["avg_attempts"]:
        lines.append(f"В среднем попыток до принятия: {o['avg_attempts']}")

    if by_task:
        lines += ["", "По заданиям (✅ принято · 🔄 доработка · 🕐 проверка):"]
        for t in by_task:
            lines.append(f"• {t['title']} — ✅{t['accepted']} · 🔄{t['revision']} · 🕐{t['pending']}")

    await message.answer(clip("\n".join(lines)))


# ---------------------------------------------------------------------------
# Очередь на проверку
# ---------------------------------------------------------------------------

@router.message(F.text == BTN_PENDING, _staff)
async def pending(message: types.Message):
    rows = await db.list_pending(limit=30)
    if not rows:
        await message.answer("🎉 Все ДЗ проверены, очередь пуста")
        return

    buttons = []
    for hw in rows:
        parts = [f"#{hw['id']}"]
        if hw["assignment_title"]:
            parts.append(hw["assignment_title"][:25])
        parts.append(student_label(hw["student_id"], hw["student_name"]))
        if hw["current_version"] > 1:
            parts.append(f"попытка {hw['current_version']}")
        parts.append(fmt_ago(hw["updated_at"]))
        buttons.append([_btn(" · ".join(parts), f"hw_{hw['id']}")])

    text = f"🕐 Ждут проверки: {len(rows)}" + (" (показаны первые 30)" if len(rows) == 30 else "")
    text += "\nСверху — те, что ждут дольше всех. Нажми, чтобы открыть."
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ---------------------------------------------------------------------------
# Ученики
# ---------------------------------------------------------------------------

async def _students_page(offset):
    rows, total = await db.list_students(offset=offset, limit=STUDENTS_PAGE)
    if not total:
        return "👥 Учеников пока нет", None

    pages = (total + STUDENTS_PAGE - 1) // STUDENTS_PAGE
    page = offset // STUDENTS_PAGE + 1
    buttons = []
    for s in rows:
        label = (f"{student_label(s['student_id'], s['student_name'])} — "
                 f"✅{s['accepted']} 🔄{s['revision']} 🕐{s['pending']}")
        buttons.append([_btn(label, f"st_{s['student_id']}")])

    nav = []
    if offset > 0:
        nav.append(_btn("◀️", f"stp_{offset - STUDENTS_PAGE}"))
    if offset + STUDENTS_PAGE < total:
        nav.append(_btn("▶️", f"stp_{offset + STUDENTS_PAGE}"))
    if nav:
        buttons.append(nav)

    text = (f"👥 Ученики: {total}" + (f" · стр. {page}/{pages}" if pages > 1 else "")
            + "\n✅ принято · 🔄 на доработке · 🕐 на проверке\nСверху — недавно активные.")
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(F.text == BTN_STUDENTS, _admin)
async def students(message: types.Message):
    text, keyboard = await _students_page(0)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("stp_"), _admin)
async def students_page(callback: types.CallbackQuery):
    offset = max(int(callback.data.split("_")[1]), 0)
    text, keyboard = await _students_page(offset)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("st_"), _staff)
async def student_card(callback: types.CallbackQuery):
    student_id = int(callback.data.split("_")[1])
    homeworks = await db.get_homeworks_by_student(student_id)
    name = student_label(student_id, await db.get_student_name(student_id))

    if not homeworks:
        await callback.answer("У ученика нет ДЗ", show_alert=True)
        return

    accepted = sum(1 for h in homeworks if h["status"] == "accepted")
    lines = [f"👤 {name} (id {student_id})", f"ДЗ: {len(homeworks)}, принято: {accepted}", ""]
    buttons = []
    for hw in homeworks:
        status = STATUS_LABELS.get(hw["status"], hw["status"])
        attempts = f" · попыток {hw['current_version']}" if hw["current_version"] > 1 else ""
        lines.append(f"{homework_title(hw)} — {status}{attempts} · {fmt_dt(hw['created_at'], False)}")
        if len(buttons) < 30:
            buttons.append([_btn(f"Открыть {homework_title(hw)}"[:60], f"hw_{hw['id']}")])

    await callback.message.answer(clip("\n".join(lines)),
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ---------------------------------------------------------------------------
# История одного ДЗ
# ---------------------------------------------------------------------------

async def _send_homework_card(chat_id, homework_id, viewer_id, bot_message):
    hw = await db.get_homework(homework_id)
    if not hw:
        await bot_message.answer(f"ДЗ #{homework_id} не найдено")
        return

    versions = await db.get_versions(homework_id)
    student = student_label(hw["student_id"], hw["student_name"] or await db.get_student_name(hw["student_id"]))
    status = STATUS_LABELS.get(hw["status"], hw["status"])

    lines = [
        f"📄 {homework_title(hw)} — {status}",
        f"Ученик: {student}",
        f"Отправлено: {fmt_dt(hw['created_at'])} · попыток: {len(versions)}",
    ]
    buttons = []
    for v in versions:
        n = v["version_num"] or "?"
        files = f" · 📎 {len(v['files'])}" if v["files"] else ""
        lines.append(f"\n— Попытка {n} · {fmt_dt(v['created_at'])}{files}")
        if v["text"]:
            lines.append(clip(v["text"], 300))
        if v["comment"]:
            lines.append(f"💬 {_reviewer_label(v['reviewed_by'])}: {clip(v['comment'], 400)}")
        if v["files"]:
            buttons.append([_btn(f"📎 Файлы попытки {n} ({len(v['files'])})", f"vf_{v['id']}")])

    if hw["status"] == "new" and is_reviewer(viewer_id):
        buttons += review_keyboard(homework_id).inline_keyboard
    buttons.append([_btn("👤 Все ДЗ ученика", f"st_{hw['student_id']}")])

    await bot_message.answer(clip("\n".join(lines)),
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("hw_"), _staff)
async def homework_card(callback: types.CallbackQuery):
    homework_id = int(callback.data.split("_")[1])
    await _send_homework_card(callback.message.chat.id, homework_id, callback.from_user.id, callback.message)
    await callback.answer()


@router.message(Command("hw"), _staff)
async def homework_command(message: types.Message, command: CommandObject):
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Напиши номер ДЗ: /hw 12")
        return
    await _send_homework_card(message.chat.id, int(arg), message.from_user.id, message)


@router.callback_query(F.data.startswith("vf_"), _staff)
async def version_files(callback: types.CallbackQuery):
    version_id = int(callback.data.split("_")[1])
    files = await db.get_version_files(version_id)
    if not files:
        await callback.answer("Файлов нет", show_alert=True)
        return
    await callback.answer()
    await send_files(callback.message.chat.id, files)


# ---------------------------------------------------------------------------
# Выгрузка
# ---------------------------------------------------------------------------

@router.message(F.text == BTN_EXPORT, _admin)
@router.message(Command("export"), _admin)
async def export_excel(message: types.Message):
    await message.answer("Готовлю файл… ⏳")
    try:
        data = await db.export_data()
        content = export.build_workbook(*data)
    except Exception:
        log.exception("Ошибка выгрузки")
        await message.answer("Не получилось сделать выгрузку 😔 Попробуй ещё раз позже")
        return

    filename = f"homeworks_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.xlsx"
    await message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption="📥 Листы: «Прогресс» (ученики × задания), «Все ДЗ», «Попытки»",
    )
