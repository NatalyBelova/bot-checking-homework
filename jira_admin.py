"""Блок 5: люди в Jira и удаление задач.

Админ присылает почту ученика — бот приглашает его в Jira. Когда ученик нашёл все баги,
проверяющий одной кнопкой удаляет его из Jira вместе с оставшимися задачами.
"""
import re

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
import decisions
import jira
from common import clip, deny_callback, fmt_dt, is_admin, is_reviewer, log, student_label

router = Router()

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAX_CALLBACK_EMAIL = 58  # callback_data в Telegram — до 64 байт


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _admin(event):
    return is_admin(event.from_user.id)


async def _users_text():
    users = await db.active_jira_users()
    if not users:
        return "В Jira через бота пока никого не добавляли.", None
    lines = [f"👥 В Jira через бота добавлено: {len(users)}", ""]
    rows = []
    for u in users:
        student = ""
        if u["student_id"]:
            student = f" · ученик {student_label(u['student_id'], await db.get_student_name(u['student_id']))}"
        lines.append(f"• {u['email']} — с {fmt_dt(u['added_at'])}{student}")
        rows.append([_btn(f"🗑 Удалить {u['email']}", f"jrm_{u['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("jira"), _admin)
async def jira_command(message: types.Message, command: CommandObject):
    email = (command.args or "").strip()
    if email:
        if not EMAIL_RE.fullmatch(email):
            await message.answer("Это не похоже на почту. Пример: /jira ivan@gmail.com")
            return
        await add_user(message, email.lower(), message.from_user.id)
        return
    text, keyboard = await _users_text()
    await message.answer(
        f"{text}\n\nЧтобы добавить ученика в Jira, пришли его почту: /jira ivan@gmail.com "
        "(или просто почту отдельным сообщением).", reply_markup=keyboard)


async def offer_add(message: types.Message, email):
    """Админ прислал просто почту — уточняем, что добавить её в Jira."""
    email = email.lower()
    if len(f"jadd:{email}".encode()) > 64:
        await message.answer(f"Почта длинная — добавь командой: /jira {email}")
        return
    await message.answer(f"Добавить {email} в Jira?", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[_btn("➕ Добавить в Jira", f"jadd:{email}")]]))


@router.callback_query(F.data.startswith("jadd:"))
async def add_user_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await deny_callback(callback)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await add_user(callback.message, callback.data.split(":", 1)[1], callback.from_user.id)


async def add_user(message, email, admin_id):
    existing = await db.find_jira_user(email=email)
    if existing:
        await message.answer(f"{email} уже добавлен в Jira ({fmt_dt(existing['added_at'])}). Список — /jira")
        return
    try:
        account_id, name = await jira.invite_user(email)
    except jira.JiraError as e:
        log.warning("Jira: не удалось добавить %s: %s", email, e)
        hint = ""
        if "seat" in str(e).lower() or "limit" in str(e).lower() or "лимит" in str(e).lower():
            hint = "\nПохоже, закончились места в Jira — удали кого-нибудь: /jira"
        await message.answer(f"⚠️ Не получилось добавить {email} в Jira: {e}{hint}")
        return
    except Exception as e:
        log.exception("Jira: ошибка при добавлении %s", email)
        await message.answer(f"⚠️ Не получилось добавить {email} в Jira: {type(e).__name__}")
        return
    await db.add_jira_user(email, account_id, name, f"human:{admin_id}")
    log.info("Jira: добавлен %s (%s) админом %s", email, account_id, admin_id)
    await message.answer(
        f"✅ {email} добавлен в Jira — Atlassian пришлёт ему письмо-приглашение.\n"
        "Когда ученик найдёт все баги, бот предложит удалить его из Jira. Список — /jira")


@router.callback_query(F.data.startswith("jrm_"))
async def remove_user_ask(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await deny_callback(callback)
        return
    row = await db.get_jira_user(int(callback.data.split("_")[1]))
    if not row or row["removed_at"]:
        await callback.answer("Уже удалён", show_alert=True)
        return
    await callback.message.answer(
        f"Удалить {row['email']} из Jira? Его задачи останутся, освободится место.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            _btn("🗑 Да, удалить", f"jrmok_{row['id']}"), _btn("Отмена", "jrmno")]]))
    await callback.answer()


@router.callback_query(F.data == "jrmno")
async def remove_user_cancel(callback: types.CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("jrmok_"))
async def remove_user_confirm(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await deny_callback(callback)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    row = await db.get_jira_user(int(callback.data.split("_")[1]))
    if not row or row["removed_at"]:
        await callback.message.answer("Уже удалён")
        return
    await callback.message.answer(await _remove_account(row["account_id"], callback.from_user.id, row["email"]))


async def _remove_account(account_id, by, label):
    try:
        await jira.remove_user(account_id)
    except jira.JiraError as e:
        log.warning("Jira: не удалось удалить %s: %s", label, e)
        return f"⚠️ Не получилось удалить {label} из Jira: {e}"
    await db.mark_jira_user_removed(account_id, f"human:{by}")
    log.info("Jira: удалён %s (%s), подтвердил %s", label, account_id, by)
    return f"✅ {label} удалён из Jira"


# ---------------------------------------------------------------------------
# Удаление задач по кнопке проверяющего
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("jdel_"))
async def delete_issues_button(callback: types.CallbackQuery):
    if not is_reviewer(callback.from_user.id):
        await deny_callback(callback)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    homework_id = int(callback.data.split("_")[1])
    keys = await db.issues_to_delete(homework_id)
    await callback.message.answer(await decisions.delete_issues_report(keys))


@router.callback_query(F.data.startswith("jfin_"))
async def final_cleanup(callback: types.CallbackQuery):
    if not is_reviewer(callback.from_user.id):
        await deny_callback(callback)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    homework_id = int(callback.data.split("_")[1])

    lines = []
    projects = {k.split("-")[0] for k in await db.jira_bug_keys(homework_id)}
    keys = list(await db.issues_to_delete(homework_id))
    accounts, _ = await decisions.final_cleanup_plan(homework_id)
    for account_id, _ in accounts:
        try:
            for key in await jira.issues_by_reporter(account_id, projects):
                if key not in keys:
                    keys.append(key)
        except jira.JiraError as e:
            lines.append(f"⚠️ Не получилось найти задачи ученика: {e}")
    lines.append(await decisions.delete_issues_report(keys))
    for account_id, row in accounts:
        lines.append(await _remove_account(account_id, callback.from_user.id, row["email"]))
    await callback.message.answer(clip("\n".join(lines)))
