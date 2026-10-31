"""Решение по ДЗ (принять / на доработку) и маршрут новой отправки через ИИ."""
import asyncio

import ai_review
import db
import jira
from common import (
    SUBMIT, fix_keyboard, homework_title, is_reviewer, log, merge_text, notify_reviewers, send_files, send_long,
)
from config import REVIEWERS
import settings

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
        await send_long(student_id, caption)
        await send_files(student_id, files)
        return

    await db.update_status(homework_id, "revision")
    caption = f"🔄 {homework_title(hw)}: нужно доработать"
    if text:
        caption += f"\n\n{text}"
    await send_files(student_id, files)
    await send_long(student_id, caption, reply_markup=fix_keyboard(homework_id))

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
    version_id = await db.get_latest_version_id(homework_id)
    try:
        await process_submission(homework_id, card_text, files, version_id)
    except Exception:
        if await _replaced(homework_id, version_id):
            return
        log.exception("Ошибка обработки ДЗ #%s — отправляю проверяющему как есть", homework_id)
        await notify_reviewers(card_text, files, homework_id)


async def _replaced(homework_id, version_id):
    """Пока шла проверка, ученик заменил работу — результат по старой версии никому не нужен."""
    if version_id and version_id != await db.get_latest_version_id(homework_id):
        log.info("ДЗ #%s: версия %s заменена учеником — результат проверки отбрасываю", homework_id, version_id)
        return True
    return False


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


async def process_submission(homework_id, card_text, files, version_id=None):
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
            if await _replaced(homework_id, version_id):
                return
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
            if await _replaced(homework_id, version_id):
                return
            reason = "; ".join(material.problems)
            problem = (f"Не получилось открыть твою таблицу: {reason}." if reason
                       else "В отправке нет ссылки на таблицу или файла .xlsx.")
            await apply_decision(hw, "revise", CHECKLIST_HINT.format(problem=problem), [],
                                 reviewed_by="system")
            log.info("ДЗ #%s: таблица не открылась, вернул ученику (%s)", homework_id, reason)
            return

    if hw and hw["assignment_kind"] == "screenshots":
        assignment = await db.get_assignment(hw["assignment_id"])
        if CHECKLIST_SPLIT_MARKER in ((assignment or {}).get("reference") or "").lower():
            # в папке блока 3–4 лежит и чек-лист — проверяем его как отдельное ДЗ;
            # в других блоках таблицы — часть самого блока (например, практика API в блоке 1–2)
            versions = await db.get_versions(homework_id)
            material = await ai_review.gather_material(versions[-1], "screenshots")
            if await _replaced(homework_id, version_id):
                return
            if material.tables and await split_checklist(hw, versions[-1]):
                material.tables = []

    mode = settings.ai_mode()
    if mode == "off":
        await notify_reviewers(card_text, files, homework_id)
        return

    result = await ai_review.check_homework(homework_id, material)
    if result is None:  # у задания нет эталона — только человек
        await notify_reviewers(card_text, files, homework_id)
        return

    hw = await db.get_homework(homework_id)
    if not hw or hw["status"] != "new":
        return  # пока ИИ думал, ДЗ уже проверили вручную
    if await _replaced(homework_id, version_id):
        return

    if result.error:
        await notify_reviewers(
            f"{card_text}\n\n⚠️ ИИ не смог проверить эту работу, нужна ручная проверка.\n"
            f"Причина: {result.error[:300]}",
            files, homework_id)
        return

    # в блоке 5 ученик сдаёт баги порциями — много попыток там нормально
    too_many_attempts = (result.verdict == "revise" and hw["current_version"] >= settings.max_attempts()
                         and not result.details)
    if mode == "auto" and result.verdict in ("accept", "revise") and not too_many_attempts:
        await apply_decision(hw, result.verdict, result.comment, [], reviewed_by="ai")
        await db.mark_ai_review_applied(result.review_id, "ai")
        review = await db.get_ai_review(result.review_id)
        await apply_jira_effects(review, hw, confirmed_by=None)
        return

    # suggest-режим или спорный случай в auto — решает человек, ИИ подсказывает
    lines = [card_text, "", f"🤖 ИИ {VERDICT_LABELS.get(result.verdict, result.verdict)}"
             + (f" · оценка {result.score}/10" if result.score else "")]
    if mode == "auto" and too_many_attempts:
        lines.append(f"Это уже попытка №{hw['current_version']} — дальше решает человек.")
    if result.details:
        # блок 5: сначала сводка по багам, потом что удалится и ответ ученику
        lines += ["", result.note]
        to_delete = [b["key"] for b in result.details["bugs"] if b["status"] in ("counted", "rejected")]
        if to_delete and result.verdict in ("accept", "revise"):
            lines += ["", "🗑 После «Согласен» удалю из Jira: " + ", ".join(to_delete)]
        if result.comment:
            lines += ["", "Ответ ученику:", result.comment]
    else:
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


# ---------------------------------------------------------------------------
# Повторная отправка по заданию, где уже есть непринятая работа
# ---------------------------------------------------------------------------

async def add_to_existing(hw, text, files, who, source=""):
    """Кладёт отправку ученика в уже открытое ДЗ вместо нового.

    На доработке — это следующая попытка. Ещё не проверено — новая версия заменяет старую и попыткой
    не считается (в блоке 5 баги сдают порциями, поэтому там прошлую порцию дописываем, а не теряем).
    Возвращает текст для ученика или None, если ДЗ тем временем уже проверили.
    """
    title = homework_title(hw)
    if hw["status"] == "revision":
        num = await db.add_version(hw["id"], text, files)
        if num is None:
            return None
        header = f"🔄 Доработка (попытка {num}) по {title} от {who}{source}"
        student_text = f"добавила к {title} как попытку {num}"
    elif hw["status"] == "new":
        had_card = settings.ai_mode() == "off" or await db.latest_version_has_ai_review(hw["id"])
        if hw["assignment_kind"] == "jira":
            versions = await db.get_versions(hw["id"])
            if versions:
                text = merge_text(versions[-1]["text"] or "", text)
                files = list(versions[-1]["files"]) + list(files)
        replaced = await db.replace_pending_version(hw["id"], text, files)
        if replaced is None:
            return None
        num = replaced[0]
        header = f"♻️ {title} от {who} — новая версия вместо непроверенной (попытка {num}){source}"
        if had_card:
            await _send_reviewers(f"♻️ {title}: ученик прислал новую версию, пока прошлую не проверили. "
                                  "Прошлая карточка устарела — новая придёт отдельно.")
        student_text = (f"дописала к {title}, проверим всё вместе" if hw["assignment_kind"] == "jira"
                        else f"заменила работу в {title}: прошлую версию ещё не проверили, проверим новую")
    else:
        return None
    log.info("ДЗ #%s: отправка ученика добавлена к открытому ДЗ (статус был %s)", hw["id"], hw["status"])
    start_processing(hw["id"], f"{header}\n\n{text}".strip(), files)
    return student_text


RECOVERY_DELAY = 180  # сек после старта: старый контейнер при деплое успевает доделать свои проверки


async def recover_unfinished(delay=RECOVERY_DELAY):
    """Повторяет ИИ-проверки, прерванные перезапуском бота (деплой, рестарт)."""
    await asyncio.sleep(delay)
    if settings.ai_mode() == "off":
        return
    try:
        homework_ids = await db.unfinished_ai_checks()
    except Exception:
        log.exception("Не удалось найти прерванные проверки")
        return
    for homework_id in homework_ids:
        hw = await db.get_homework(homework_id)
        versions = await db.get_versions(homework_id)
        if not hw or not versions:
            continue
        v = versions[-1]
        who = hw["student_name"] or f"id:{hw['student_id']}"
        header = (f"♻️ {homework_title(hw)} от {who} (попытка {v['version_num']}) — "
                  "проверка прервалась при перезапуске бота, проверяю заново")
        log.info("Восстанавливаю проверку ДЗ #%s", homework_id)
        start_processing(homework_id, f"{header}\n\n{v['text'] or ''}".strip(), v["files"])


# ---------------------------------------------------------------------------
# Блок 5 (Jira): что происходит после того, как ученик получил разбор багов
# ---------------------------------------------------------------------------

async def _send_reviewers(text, keyboard=None, only=None):
    for reviewer in ([only] if only else REVIEWERS):
        try:
            await send_long(reviewer, text, reply_markup=keyboard)
        except Exception:
            log.exception("Не удалось отправить сообщение проверяющему %s", reviewer)


async def delete_issues_report(keys):
    """Удаляет задачи из Jira и возвращает текст отчёта."""
    done, failed = await jira.delete_issues(keys)
    if done:
        await db.mark_issues_deleted(done)
    lines = []
    if done:
        lines.append("🗑 Удалено из Jira: " + ", ".join(done))
    if failed:
        lines.append("⚠️ Не удалось удалить: " + "; ".join(f"{k} — {v}" for k, v in failed.items()))
    return "\n".join(lines) or "Удалять нечего"


async def apply_jira_effects(review, hw, confirmed_by):
    """Записывает итог по багам, удаляет засчитанные/незасчитанные задачи и предлагает финал.

    confirmed_by — id проверяющего, который нажал «Согласен» (удаляем сразу).
    None — ИИ применил решение сам (режим auto): удаление ждёт кнопки проверяющего.
    """
    details = (review or {}).get("details")
    if not details:
        return
    homework_id = hw["id"]
    await db.save_jira_bugs(homework_id, review["id"], details["bugs"])
    owner = (await db.get_homework(homework_id) or {}).get("jira_owner")
    if owner:
        await db.link_jira_users_to_student([owner], hw["student_id"])

    keys = await db.issues_to_delete(homework_id, review["id"])
    if keys:
        if confirmed_by and is_reviewer(confirmed_by):
            await _send_reviewers(await delete_issues_report(keys), only=confirmed_by)
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"🗑 Удалить из Jira ({len(keys)})", callback_data=f"jdel_{homework_id}")]])
            await _send_reviewers(
                f"ИИ разобрал баги по {homework_title(hw)} и отправил ученику.\n"
                f"Можно удалить из Jira: {', '.join(keys)}", keyboard)

    if review["verdict"] == "accept":
        await offer_final_cleanup(hw)


async def final_cleanup_plan(homework_id):
    """Кого удалить из Jira, когда ученик нашёл все баги: (добавленные через бота, неизвестные авторы).

    Удаляем только тех, кого добавили через бота (/jira) — так бот не удалит преподавателя или
    кого-то ещё, чьи задачи оказались в отправке.
    """
    known, unknown = [], []
    owner = (await db.get_homework(homework_id) or {}).get("jira_owner")
    if owner:  # только сам ученик, чужие авторы — никогда
        row = await db.find_jira_user(account_id=owner)
        (known if row else unknown).append((owner, row))
    return known, unknown


async def offer_final_cleanup(hw):
    homework_id = hw["id"]
    known, unknown = await final_cleanup_plan(homework_id)
    who = hw["student_name"] or f"id:{hw['student_id']}"
    lines = [f"🎉 {who} нашёл все баги — {homework_title(hw)} принято."]
    keyboard = None
    if known:
        lines.append("Удалить из Jira ученика (" + ", ".join(row["email"] for _, row in known)
                     + ") и все его оставшиеся задачи?")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🗑 Удалить ученика и его задачи", callback_data=f"jfin_{homework_id}")]])
    if unknown or not known:
        lines.append("Автора задач не добавляли в Jira через бота — его бот не удаляет. "
                     "Удали ученика из Jira вручную.")
    await _send_reviewers("\n".join(lines), keyboard)


# ---------------------------------------------------------------------------
# Чек-лист внутри сдачи скриншотов
# ---------------------------------------------------------------------------

CHECKLIST_SPLIT_MARKER = "чек-лист в папке проверяется отдельно"


async def split_checklist(hw, version):
    """Таблица в сдаче блока → отдельное ДЗ по заданию-чек-листу. True, если таблицу забрали туда."""
    checklists = [a for a in await db.list_assignments(active_only=True) if a["kind"] == "checklist"]
    if not checklists:
        return False
    checklist = checklists[0]
    student_id = hw["student_id"]
    who = hw["student_name"] or f"id:{student_id}"
    existing = await db.latest_homework_for_assignment(student_id, checklist["id"])
    text, files = version["text"] or "", version["files"]
    source = f" — таблица из сдачи {homework_title(hw)}"
    if existing and existing["status"] in ("new", "revision"):
        child = await db.get_homework(existing["id"])
        done = await add_to_existing(child, text, files, who, source)
        if done:
            try:
                await send_long(student_id, f"📊 В твоей отправке по {homework_title(hw)} есть таблица — {done}. "
                                            "Результат придёт отдельно.")
            except Exception:
                log.exception("Не удалось написать ученику %s про чек-лист", student_id)
            return True
        existing = await db.latest_homework_for_assignment(student_id, checklist["id"])  # уже проверили
    if existing and existing["status"] == "accepted":
        # уже принят — второй раз не заводим, но говорим об этом
        child = await db.get_homework(existing["id"])
        log.info("ДЗ #%s: таблица есть, но чек-лист уже принят (ДЗ #%s)", hw["id"], existing["id"])
        try:
            await send_long(student_id, f"📊 В твоей отправке по {homework_title(hw)} есть таблица, но "
                                        f"{homework_title(child)} уже принят — повторно не отправляю.")
        except Exception:
            log.exception("Не удалось написать ученику %s про чек-лист", student_id)
        return True

    homework_id = await db.create_homework(student_id, checklist["id"], text, files, student_name=hw["student_name"])
    child = await db.get_homework(homework_id)
    header = f"🆕 Новое ДЗ #{homework_id} «{checklist['title']}» от {who}{source}"
    log.info("ДЗ #%s: найдена таблица — проверяю её как чек-лист в ДЗ #%s", hw["id"], homework_id)
    try:
        await send_long(student_id, f"📊 В твоей отправке по {homework_title(hw)} есть таблица — отправила её на "
                                    f"проверку как {homework_title(child)}. Результат придёт отдельно.")
    except Exception:
        log.exception("Не удалось написать ученику %s про чек-лист", student_id)
    start_processing(homework_id, f"{header}\n\n{text}".strip(), files)
    return True
