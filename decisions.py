"""Решение по ДЗ (принять / на доработку) и маршрут новой отправки через ИИ."""
import asyncio

import ai_review
import db
from common import (
    SUBMIT, bot, clip, fix_keyboard, homework_title, log, notify_reviewers, send_files,
)
from config import AI_MAX_ATTEMPTS, AI_MODE

from aiogram.types import InlineKeyboardButton

# ссылки на фоновые задачи, чтобы их не собрал сборщик мусора (и чтобы тесты могли их дождаться)
background_tasks = set()

VERDICT_LABELS = {
    "accept": "✅ предлагает ПРИНЯТЬ",
    "revise": "🔄 предлагает вернуть НА ДОРАБОТКУ",
    "unsure": "🤔 не уверен — нужно твоё решение",
}


async def apply_decision(hw, action, text, files, reviewed_by):
    """Принимает ДЗ или отправляет на доработку и сообщает ученику.

    reviewed_by: 'human:<id>' | 'ai' | 'ai+human:<id>'
    """
    homework_id, student_id = hw["id"], hw["student_id"]
    await db.add_comment(homework_id, text or None, reviewed_by=reviewed_by)

    if action == "accept":
        await db.update_status(homework_id, "accepted")
        caption = f"✅ {homework_title(hw)} принято!"
        if text:
            caption += f"\n\n{text}"
        await bot.send_message(student_id, clip(caption))
        await send_files(student_id, files)
        return

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


def start_processing(homework_id, card_text, files):
    """Запускает проверку в фоне: ученик сразу получает «отправлено», ИИ думает 10–60 секунд."""
    task = asyncio.create_task(_process_submission(homework_id, card_text, files))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _process_submission(homework_id, card_text, files):
    try:
        await process_submission(homework_id, card_text, files)
    except Exception:
        log.exception("Ошибка обработки ДЗ #%s — отправляю проверяющему как есть", homework_id)
        await notify_reviewers(card_text, files, homework_id)


CHECKLIST_HINT = (
    "{problem}\n\n"
    "Пришли ссылку на таблицу с доступом «Все, у кого есть ссылка» (в Google Таблице: «Настройки доступа» → "
    "«Все, у кого есть ссылка» → «Читатель») или просто пришли сюда файл .xlsx."
)


JIRA_HINT = (
    "{problem}\n\n"
    "Пришли ссылки на задачи в Jira — скопируй адрес открытой задачи (вида …/browse/KAN-353) "
    "или напиши ключи задач, например: KAN-353, KAN-354."
)


async def process_submission(homework_id, card_text, files):
    material = None
    hw = await db.get_homework(homework_id)
    if hw and hw["assignment_kind"] == "jira":
        versions = await db.get_versions(homework_id)
        material = await ai_review.gather_material(versions[-1], "jira")
        if material.jira_config_problem:
            # ученик не виноват — разбирается человек
            await notify_reviewers(
                f"{card_text}\n\n⚠️ Не удалось прочитать задачи из Jira: {material.jira_config_problem}. "
                "Проверь вручную.", files, homework_id)
            return
        if not material.jira_issues and not material.images:
            reason = "; ".join(material.jira_problems)
            problem = (f"Не получилось открыть твои баг-репорты: {reason}." if reason
                       else "В отправке нет ссылок на задачи Jira.")
            await apply_decision(hw, "revise", JIRA_HINT.format(problem=problem), [], reviewed_by="system")
            log.info("ДЗ #%s: задачи Jira не найдены, вернул ученику (%s)", homework_id, reason)
            return

    if hw and hw["assignment_kind"] == "checklist":
        # чек-лист без доступной таблицы проверять бессмысленно — сразу просим ученика поправить
        versions = await db.get_versions(homework_id)
        material = await ai_review.gather_material(versions[-1])
        if not material.tables and not material.images:
            reason = "; ".join(material.problems)
            problem = (f"Не получилось открыть твою таблицу: {reason}." if reason
                       else "В отправке нет ссылки на таблицу или файла .xlsx.")
            await apply_decision(hw, "revise", CHECKLIST_HINT.format(problem=problem), [],
                                 reviewed_by="system")
            log.info("ДЗ #%s: таблица не открылась, вернул ученику (%s)", homework_id, reason)
            return

    if AI_MODE == "off":
        await notify_reviewers(card_text, files, homework_id)
        return

    result = await ai_review.check_homework(homework_id, material)
    if result is None:  # у задания нет эталона — только человек
        await notify_reviewers(card_text, files, homework_id)
        return

    hw = await db.get_homework(homework_id)
    if not hw or hw["status"] != "new":
        return  # пока ИИ думал, ДЗ уже проверили вручную

    if result.error:
        await notify_reviewers(
            f"{card_text}\n\n⚠️ ИИ не смог проверить эту работу, нужна ручная проверка.\n"
            f"Причина: {result.error[:300]}",
            files, homework_id)
        return

    too_many_attempts = result.verdict == "revise" and hw["current_version"] >= AI_MAX_ATTEMPTS
    if AI_MODE == "auto" and result.verdict in ("accept", "revise") and not too_many_attempts:
        await apply_decision(hw, result.verdict, result.comment, [], reviewed_by="ai")
        await db.mark_ai_review_applied(result.review_id, "ai")
        return

    # suggest-режим или спорный случай в auto — решает человек, ИИ подсказывает
    lines = [card_text, "", f"🤖 ИИ {VERDICT_LABELS.get(result.verdict, result.verdict)}"]
    if AI_MODE == "auto" and too_many_attempts:
        lines.append(f"Это уже попытка №{hw['current_version']} — дальше решает человек.")
    if result.verdict == "revise" and result.issues:
        lines += ["", "Нарушения критериев:"] + [f"• {i}" for i in result.issues[:7]]
    if result.comment:
        lines += ["", "Комментарий для ученика:", result.comment]
    if result.note:
        lines += ["", f"📝 Для тебя: {result.note}"]

    extra = []
    if result.verdict in ("accept", "revise"):
        extra = [
            [InlineKeyboardButton(text="👍 Согласен — отправить ученику",
                                  callback_data=f"aiok_{result.review_id}")],
            [InlineKeyboardButton(text="✏️ Поправить комментарий ИИ",
                                  callback_data=f"aiedit_{result.review_id}")],
        ]
    await notify_reviewers("\n".join(lines), files, homework_id, extra_rows=extra)
