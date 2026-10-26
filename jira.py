"""Чтение баг-репортов из Jira Cloud для проверки ДЗ.

Ученик присылает номера задач со ссылками («353 сделал» с гиперссылкой на задачу) — бот находит
ключи задач (KAN-353), забирает их через REST API и превращает в текст для ИИ:
поля, описание, комментарии, последние изменения и картинки-вложения.

Нужны переменные: JIRA_URL (https://<домен>.atlassian.net), JIRA_EMAIL, JIRA_TOKEN.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

from common import log, read_limited, sniff_image
from config import JIRA_EMAIL, JIRA_TOKEN, JIRA_URL

MAX_ISSUES = 10
MAX_IMAGES_PER_ISSUE = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DESCRIPTION = 6000
MAX_COMMENTS = 5
MAX_CHANGES = 15

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,19}-\d{1,7})\b")
BROWSE_RE = re.compile(r"/browse/([A-Z][A-Z0-9_]{1,19}-\d{1,7})")

# поля, которые выводим отдельно или которые ИИ не нужны
_SKIP_FIELDS = {
    "summary", "description", "issuetype", "priority", "status", "environment", "labels",
    "attachment", "comment", "reporter", "creator", "assignee", "created", "updated", "project",
    "watches", "votes", "worklog", "timetracking", "aggregateprogress", "progress", "lastViewed",
    "statuscategorychangedate", "issuelinks", "subtasks", "issuerestriction", "thumbnail",
    "statusCategory", "workratio", "aggregatetimeestimate", "aggregatetimeoriginalestimate",
    "aggregatetimespent", "timeestimate", "timeoriginalestimate", "timespent", "versions",
    "fixVersions", "components", "resolution", "resolutiondate", "duedate", "security", "parent",
}


class JiraError(Exception):
    """kind: 'student' — проблема в ссылке/задаче (вернуть ученику); 'config' — проблема с доступом бота."""

    def __init__(self, message, kind="student"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Issue:
    key: str
    url: str
    text: str
    images: List[tuple] = field(default_factory=list)  # [(mime, bytes, filename)]
    image_errors: List[str] = field(default_factory=list)  # вложения-картинки, которые не скачались


def enabled():
    return bool(JIRA_URL and JIRA_EMAIL and JIRA_TOKEN)


def _base():
    return (JIRA_URL or "").rstrip("/")


def find_issue_keys(text):
    """Ключи задач из ссылок на нашу Jira и из текста («KAN-353»). Порядок сохраняется."""
    keys = []
    host = urlparse(_base()).netloc.lower()

    def add(key):
        if key not in keys:
            keys.append(key)

    for url in re.findall(r"https?://\S+", text or ""):
        parsed = urlparse(url.rstrip(".,;:!?)»"))
        if host and parsed.netloc.lower() != host:
            continue
        match = BROWSE_RE.search(parsed.path)
        if match:
            add(match.group(1))
            continue
        # ссылки с доски: …/boards/1?selectedIssue=KAN-353
        for value in parse_qs(parsed.query).get("selectedIssue", []):
            if KEY_RE.fullmatch(value):
                add(value)
    # явные ключи в тексте, но не части чужих ссылок
    without_urls = re.sub(r"https?://\S+", " ", text or "")
    for key in KEY_RE.findall(without_urls):
        add(key)
    return keys[:MAX_ISSUES]


def _plain(value):
    """Значение произвольного поля → короткий текст (или '' если пусто)."""
    if value is None or value == [] or value == "":
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for k in ("value", "name", "displayName", "key"):
            if value.get(k):
                child = value.get("child")
                return value[k] + (f" / {_plain(child)}" if child else "")
        return ""
    if isinstance(value, list):
        return ", ".join(x for x in (_plain(v) for v in value) if x)
    return ""


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit] + "…"


def issue_to_text(data):
    """JSON задачи (fields=*all, expand=names,changelog) → текст для ИИ."""
    f = data.get("fields", {})
    names = data.get("names", {})
    # В проекте могут быть свои поля «Приоритет»/«Серьёзность» — тогда системный Priority
    # обычно не заполняют (стоит Medium по умолчанию) и показывать его — только путать ИИ.
    custom_names = {names.get(fid, "").strip().lower() for fid in f if fid.startswith("customfield_")}
    has_custom_priority = bool(custom_names & {"приоритет", "priority"})
    head = [f"Тип: {_plain(f.get('issuetype')) or '—'}"]
    if not has_custom_priority:
        head.append(f"Приоритет: {_plain(f.get('priority')) or '—'}")
    head.append(f"Статус: {_plain(f.get('status')) or '—'}")
    lines = [
        f"Задача {data.get('key')}: {f.get('summary') or '(без заголовка)'}",
        " · ".join(head),
    ]
    if _plain(f.get("labels")):
        lines.append(f"Метки: {_plain(f.get('labels'))}")
    if f.get("environment"):
        lines.append(f"Окружение: {_clip(str(f['environment']).strip(), 1000)}")

    # кастомные поля (например, «Серьёзность», «Шаги воспроизведения»)
    for field_id, value in f.items():
        if field_id in _SKIP_FIELDS:
            continue
        text = _plain(value)
        if text and text != "{}" and field_id.startswith("customfield_"):
            name = names.get(field_id, field_id)
            if name.strip().lower() in ("rank", "ранжирование", "рейтинг", "sprint", "спринт", "development",
                                        "разработка", "team", "команда", "start date", "дата начала",
                                        "flagged", "помечено"):
                continue
            lines.append(f"{name}: {_clip(text, 1000)}")

    description = (f.get("description") or "").strip()
    lines += ["", "Описание:", _clip(description, MAX_DESCRIPTION) if description else "(пусто)"]

    attachments = f.get("attachment") or []
    if attachments:
        lines += ["", "Вложения: " + ", ".join(
            f"{a.get('filename')} ({a.get('mimeType')})" for a in attachments)]

    comments = (f.get("comment") or {}).get("comments") or []
    if comments:
        lines += ["", "Комментарии (последние):"]
        for c in comments[-MAX_COMMENTS:]:
            author = _plain(c.get("author")) or "?"
            lines.append(f"— {author} ({(c.get('created') or '')[:16]}): {_clip((c.get('body') or '').strip(), 800)}")

    histories = (data.get("changelog") or {}).get("histories") or []
    changes = []
    for h in histories:
        for item in h.get("items", []):
            if item.get("field") in ("Rank", "rank"):
                continue
            changes.append(f"{(h.get('created') or '')[:16]} {item.get('field')}: "
                           f"«{_clip(item.get('fromString') or '', 150)}» → «{_clip(item.get('toString') or '', 150)}»")
    if changes:
        lines += ["", "Последние изменения задачи (что ученик правил):"] + changes[-MAX_CHANGES:]
    return "\n".join(lines)


async def _get_json(session, url, params=None):
    async with session.get(url, params=params) as response:
        if response.status == 401:
            raise JiraError("бот не смог войти в Jira — проверь JIRA_EMAIL и JIRA_TOKEN", kind="config")
        if response.status == 403:
            raise JiraError("у аккаунта бота нет доступа к этому проекту Jira", kind="config")
        if response.status == 404:
            raise JiraError("задача не найдена — проверь ссылку")
        if response.status >= 400:
            raise JiraError(f"Jira ответила ошибкой {response.status}", kind="config")
        return await response.json()


def _direct_attachment_url(url):
    """Jira по умолчанию отвечает редиректом на api.media.atlassian.com, а при переходе на другой
    домен авторизация теряется и вместо файла приходит страница. redirect=false отдаёт файл сразу."""
    if "/attachment/content/" in url and "redirect=" not in url:
        return url + ("&" if "?" in url else "?") + "redirect=false"
    return url


async def _download(session, url):
    url = _direct_attachment_url(url)
    async with session.get(url) as response:
        if response.status >= 400:
            log.warning("Jira: вложение %s не скачалось, статус %s", url, response.status)
            return None
        data = await read_limited(response, MAX_IMAGE_BYTES)
        return data if len(data) <= MAX_IMAGE_BYTES else None


def _session():
    auth = aiohttp.BasicAuth(JIRA_EMAIL or "", JIRA_TOKEN or "")
    return aiohttp.ClientSession(auth=auth, timeout=aiohttp.ClientTimeout(total=40),
                                 headers={"Accept": "application/json"})


async def fetch_issue(session, key):
    data = await _get_json(session, f"{_base()}/rest/api/2/issue/{key}",
                           params={"fields": "*all", "expand": "names,changelog"})
    issue = Issue(key=data.get("key", key), url=f"{_base()}/browse/{data.get('key', key)}",
                  text=issue_to_text(data))
    for a in (data.get("fields", {}).get("attachment") or []):
        if len(issue.images) >= MAX_IMAGES_PER_ISSUE:
            break
        mime = a.get("mimeType") or ""
        if mime.startswith("image/") and a.get("content") and (a.get("size") or 0) <= MAX_IMAGE_BYTES:
            content = await _download(session, a["content"])
            real_mime = sniff_image(content)
            if real_mime:
                issue.images.append((real_mime, content, a.get("filename") or "image"))
            else:
                issue.image_errors.append(a.get("filename") or "image")
    return issue


async def collect_issues(text):
    """Возвращает (issues, problems, config_problem).

    problems — по каким задачам ученика не получилось (ученик может исправить);
    config_problem — проблема с доступом бота (ученик не виноват), иначе None.
    """
    keys = find_issue_keys(text)
    if not keys:
        return [], [], None
    if not enabled():
        return [], [], "Jira не подключена к боту (нет JIRA_URL / JIRA_EMAIL / JIRA_TOKEN)"

    issues: List[Issue] = []
    problems: List[str] = []
    config_problem: Optional[str] = None
    async with _session() as session:
        for key in keys:
            try:
                issues.append(await fetch_issue(session, key))
            except JiraError as e:
                if e.kind == "config":
                    config_problem = str(e)
                    break
                problems.append(f"{key} — {e}")
            except (aiohttp.ClientError, TimeoutError) as e:
                config_problem = f"не удалось связаться с Jira ({type(e).__name__})"
                break
    return issues, problems, config_problem
