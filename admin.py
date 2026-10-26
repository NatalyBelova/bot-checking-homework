"""Отслеживание ДЗ: сводка, очередь на проверку, ученики, история ДЗ, выгрузка."""
from datetime import datetime

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import db
import export
import sheets
from config import AI_MODE
from common import (
    BTN_EXPORT, BTN_PENDING, BTN_STATS, BTN_STUDENTS, REFERENCE, STATUS_LABELS,
    _user_locks, bot, clip, decode_text, download_attachment, extract_content, fmt_ago, fmt_dt, homework_title,
    is_admin, is_reviewer, is_staff, log, merge_text, review_keyboard, send_files, student_label,
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
    if reviewed_by == "ai":
        return "🤖 ИИ"
    if reviewed_by == "system":
        return "⚙️ бот (таблица не открылась)"
    if (reviewed_by or "").startswith("ai+human"):
        return "🤖 ИИ (подтвердил проверяющий)"
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

    ai = await db.ai_stats()
    if AI_MODE != "off" or ai["total"]:
        mode = {"off": "выключена", "suggest": "ИИ предлагает, человек подтверждает",
                "auto": "ИИ решает сам, спорное — человеку"}[AI_MODE]
        lines += ["", f"🤖 ИИ-проверка: {mode}",
                  f"Проверок ИИ: {ai['total']} · ИИ решил сам: {ai['auto_applied']} · "
                  f"подтверждено человеком: {ai['confirmed']}"
                  + (f" · ошибок: {ai['errors']}" if ai["errors"] else "")]

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


# ---------------------------------------------------------------------------
# Задания и эталоны
# ---------------------------------------------------------------------------

KIND_LABELS = {"screenshots": "скриншоты", "checklist": "чек-лист (таблица)", "jira": "Jira"}


def _has_reference(a):
    return bool(a["reference"]) or bool(a.get("ref_files"))


@router.message(Command("tasks"), _staff)
async def list_tasks(message: types.Message):
    assignments = await db.list_assignments(active_only=False)
    if not assignments:
        await message.answer("Заданий пока нет. Добавь: /addtask Название")
        return

    buttons = []
    for a in assignments:
        marks = ("✅" if _has_reference(a) else "⚠️") + ("" if a["is_active"] else " 🙈")
        buttons.append([_btn(f"{marks} #{a['id']} {a['title']}"[:60], f"tk_{a['id']}")])
    await message.answer(
        "📚 Задания. ✅ — эталон задан, ⚠️ — нет эталона (ИИ такое не проверяет), 🙈 — скрыто от учеников.\n"
        "Нажми на задание, чтобы задать эталон.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(Command("addtask"), _staff)
async def add_task(message: types.Message, command: CommandObject):
    title = (command.args or "").strip()
    if not title:
        await message.answer("Напиши название после команды: /addtask Урок 1. Чек-лист")
        return
    assignment_id = await db.add_assignment(title)
    await message.answer(
        f"Задание #{assignment_id} «{title}» добавлено ✅",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            _btn("✏️ Задать эталон", f"tkref_{assignment_id}")]]),
    )


async def _task_card(assignment_id):
    a = await db.get_assignment(assignment_id)
    if not a:
        return None, None
    lines = [
        f"📚 #{a['id']} {a['title']}",
        f"Тип: {KIND_LABELS.get(a['kind'], a['kind'])} · "
        + ("видно ученикам" if a["is_active"] else "🙈 скрыто от учеников"),
        "",
    ]
    if _has_reference(a):
        lines.append("Эталон и критерии:")
        lines.append(clip(a["reference"], 2500) if a["reference"] else "(только картинки)")
        if a["ref_files"]:
            lines.append(f"\n🖼 Картинок эталона: {a['ref_files']}")
    else:
        lines.append("⚠️ Эталон не задан — ИИ не будет проверять это задание, работы уйдут проверяющему.")

    buttons = [[_btn("✏️ Задать эталон заново" if _has_reference(a) else "✏️ Задать эталон",
                     f"tkref_{a['id']}")]]
    if a["ref_files"]:
        buttons.append([_btn("🖼 Показать картинки эталона", f"tkimg_{a['id']}")])
    if a["reference"] and len(a["reference"]) > 2500:
        buttons.append([_btn("📄 Эталон целиком файлом", f"tktxt_{a['id']}")])
    buttons.append([_btn("👁 Показать ученикам" if not a["is_active"] else "🙈 Скрыть от учеников",
                         f"tktog_{a['id']}")])
    buttons.append([_btn(f"🔁 Тип: {KIND_LABELS.get(a['kind'], a['kind'])} — сменить", f"tkkind_{a['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("tk_"), _staff)
async def task_card(callback: types.CallbackQuery):
    text, keyboard = await _task_card(int(callback.data.split("_")[1]))
    if not text:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tktog_"), _staff)
async def task_toggle(callback: types.CallbackQuery):
    assignment_id = int(callback.data.split("_")[1])
    a = await db.get_assignment(assignment_id)
    if not a:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await db.set_assignment_active(assignment_id, not a["is_active"])
    text, keyboard = await _task_card(assignment_id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer("Скрыто" if a["is_active"] else "Снова видно ученикам")


@router.callback_query(F.data.startswith("tkkind_"), _staff)
async def task_kind(callback: types.CallbackQuery):
    assignment_id = int(callback.data.split("_")[1])
    a = await db.get_assignment(assignment_id)
    if not a:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    kinds = list(KIND_LABELS)
    new_kind = kinds[(kinds.index(a["kind"]) + 1) % len(kinds)] if a["kind"] in kinds else kinds[0]
    await db.set_assignment_kind(assignment_id, new_kind)
    text, keyboard = await _task_card(assignment_id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(f"Тип: {KIND_LABELS[new_kind]}")


@router.callback_query(F.data.startswith("tkimg_"), _staff)
async def task_images(callback: types.CallbackQuery):
    images = await db.get_reference_files(int(callback.data.split("_")[1]))
    await callback.answer()
    for i in range(0, len(images), 10):
        chunk = images[i:i + 10]
        media = [types.InputMediaPhoto(media=BufferedInputFile(data, filename=f"ref{i + j}.jpg"))
                 for j, (_, data) in enumerate(chunk)]
        if len(media) == 1:
            await callback.message.answer_photo(media[0].media)
        else:
            await callback.message.answer_media_group(media)


@router.callback_query(F.data.startswith("tkref_"), _staff)
async def task_reference_start(callback: types.CallbackQuery):
    assignment_id = int(callback.data.split("_")[1])
    a = await db.get_assignment(assignment_id)
    if not a:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await db.set_state(callback.from_user.id, REFERENCE,
                       {"assignment_id": assignment_id, "text": "", "files": []})
    await callback.message.answer(
        f"✏️ Эталон для «{a['title']}»\n\n"
        "Пришли критерии приёмки текстом: что должно быть в работе, что считается ошибкой, "
        "за что точно возвращать на доработку. Можно добавить эталонные скриншоты "
        "(как выглядит правильный результат), а для чек-листа — эталонную таблицу "
        "файлом .xlsx или ссылкой на Google Таблицу. Длинные критерии удобно прислать файлом .txt.\n\n"
        "Можно несколькими сообщениями. Старый эталон заменится, когда нажмёшь «Сохранить»."
    )
    await callback.answer()


async def add_to_reference(messages):
    """Вызывается из общего обработчика, когда админ в состоянии REFERENCE."""
    user_id = messages[0].from_user.id
    text, files = extract_content(messages)
    async with _user_locks[user_id]:
        state, data = await db.get_state(user_id)
        if state != REFERENCE:
            return
        data["text"] = merge_text(data.get("text", ""), text)
        data["files"] = data.get("files", []) + files
        await db.set_state(user_id, REFERENCE, data)

    await bot.send_message(
        user_id,
        f"В эталоне: текст {len(data['text'])} символов, файлов {len(data['files'])}. "
        "Можно прислать ещё 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            _btn("💾 Сохранить эталон", "refsave"), _btn("❌ Отмена", "refcancel")]]),
    )


@router.callback_query(F.data == "refsave", _staff)
async def reference_save(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)
    async with _user_locks[user_id]:
        state, data = await db.get_state(user_id)
        if state != REFERENCE or not (data.get("text") or data.get("files")):
            await callback.answer("Нечего сохранять", show_alert=True)
            return
        await db.clear_state(user_id)
    await callback.answer()

    images, documents, texts, skipped = [], [], [], []
    for file_type, file_id in data["files"]:
        kind, meta, content = await download_attachment(file_type, file_id)
        if kind == "image":
            images.append((meta, content))
        elif kind == "table":
            documents.append((meta, content))
        elif kind == "text":
            texts.append(decode_text(content))
        else:
            skipped.append(meta)

    # эталонная таблица (файл или ссылка) сохраняется текстом — так её проще сравнивать
    # критерии можно прислать и файлом .txt — длинный текст не влезает в одно сообщение Telegram
    reference = "\n\n".join(x for x in [data.get("text", "")] + texts if x)
    tables, problems = await sheets.collect_tables(reference, documents)
    if tables:
        reference += "\n\n" + sheets.describe(tables, [], "ЭТАЛОННАЯ ТАБЛИЦА")

    await db.set_reference(data["assignment_id"], reference, images)
    text, keyboard = await _task_card(data["assignment_id"])
    notes = []
    if texts:
        notes.append(f"📄 Текстовых файлов в эталоне: {len(texts)}")
    if tables:
        notes.append(f"📊 Таблиц в эталоне: {len(tables)} (сохранены текстом)")
    if problems:
        notes.append("⚠️ Не открылись таблицы: " + "; ".join(problems))
    if skipped:
        notes.append("⚠️ Не сохранено (ИИ понимает картинки, таблицы и текстовые файлы): " + ", ".join(skipped))
    note = ("\n\n" + "\n".join(notes)) if notes else ""
    await callback.message.answer(clip(f"💾 Эталон сохранён{note}\n\n{text}"), reply_markup=keyboard)


@router.callback_query(F.data == "refcancel", _staff)
async def reference_cancel(callback: types.CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.clear_state(callback.from_user.id)
    await callback.message.answer("Отменено, эталон не изменён")
    await callback.answer()


# ---------------------------------------------------------------------------
# Все задания со всей информацией
# ---------------------------------------------------------------------------

REF_PREVIEW = 3000


@router.message(Command("alltasks"), _staff)
async def all_tasks(message: types.Message):
    assignments = await db.list_assignments(active_only=False)
    if not assignments:
        await message.answer("Заданий пока нет. Добавь: /addtask Название")
        return

    stats = {s["id"]: s for s in await db.stats_by_assignment()}
    with_ref = sum(1 for a in assignments if _has_reference(a))
    visible = sum(1 for a in assignments if a["is_active"])
    ai_mode = {"off": "выключена", "suggest": "ИИ предлагает, человек подтверждает",
               "auto": "ИИ решает сам, спорное — человеку"}[AI_MODE]
    await message.answer(
        f"📚 Заданий: {len(assignments)} · с эталоном: {with_ref} · видно ученикам: {visible}\n"
        f"🤖 ИИ-проверка: {ai_mode}"
    )

    for a in assignments:
        st = stats.get(a["id"]) or {"total": 0, "accepted": 0, "revision": 0, "pending": 0}
        if AI_MODE == "off":
            ai = "🤖 ИИ выключен — проверяет человек"
        elif _has_reference(a):
            ai = "🤖 ИИ проверяет по эталону"
        else:
            ai = "⚠️ Нет эталона — ИИ не проверяет, только человек"

        lines = [
            f"📚 #{a['id']} {a['title']}",
            f"Тип: {KIND_LABELS.get(a['kind'], a['kind'])} · "
            + ("👁 видно ученикам" if a["is_active"] else "🙈 скрыто от учеников"),
            ai,
            f"ДЗ: всего {st['total']} · ✅ {st['accepted']} · 🔄 {st['revision']} · 🕐 {st['pending']}",
        ]
        if a["reference"]:
            lines += ["", "Эталон и критерии:", clip(a["reference"], REF_PREVIEW)]
        if a["ref_files"]:
            lines.append(f"\n🖼 Картинок эталона: {a['ref_files']}")

        buttons = [[_btn("✏️ Изменить эталон" if _has_reference(a) else "✏️ Задать эталон",
                         f"tkref_{a['id']}")]]
        extra = []
        if a["ref_files"]:
            extra.append(_btn("🖼 Картинки", f"tkimg_{a['id']}"))
        if a["reference"] and len(a["reference"]) > REF_PREVIEW:
            extra.append(_btn("📄 Эталон целиком", f"tktxt_{a['id']}"))
        if extra:
            buttons.append(extra)
        buttons.append([_btn("⚙️ Тип / видимость", f"tk_{a['id']}")])

        await message.answer(clip("\n".join(lines)),
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("tktxt_"), _staff)
async def task_reference_file(callback: types.CallbackQuery):
    a = await db.get_assignment(int(callback.data.split("_")[1]))
    if not a or not a["reference"]:
        await callback.answer("Эталона нет", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer_document(
        BufferedInputFile(a["reference"].encode("utf-8"), filename=f"etalon_{a['id']}.txt"),
        caption=f"📄 Эталон «{a['title']}» целиком",
    )
