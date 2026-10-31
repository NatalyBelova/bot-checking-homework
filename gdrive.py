"""Публичные ссылки Google Drive: папка со скриншотами или отдельный файл.

Список файлов папки берём из встраиваемого представления drive.google.com/embeddedfolderview
(работает без ключа API для папок с доступом «Все, у кого есть ссылка»), каждый файл скачиваем
отдельно. Google Таблицы (docs.google.com/spreadsheets) обрабатывает sheets.py.
"""
import html
import re

import aiohttp

from common import log, read_limited, sniff_image
from yadisk import MAX_FILE_BYTES, MAX_FILES, TABLE_EXT, TEXT_EXT, DiskFiles, _natural_key, shrink_image

TITLE = "Google Drive"
FOLDER_VIEW = "https://drive.google.com/embeddedfolderview"
DOWNLOAD = "https://drive.usercontent.google.com/download"
MAX_DEPTH = 2

FOLDER_RE = re.compile(r"https?://drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|open\?id=)([\w-]{10,})", re.I)
FILE_RE = re.compile(r"https?://drive\.google\.com/(?:file/d/|uc\?(?:[^ ]*&)?id=)([\w-]{10,})", re.I)
ENTRY_RE = re.compile(
    r'id="entry-([\w-]+)".*?href="([^"]+)".*?class="flip-entry-title"[^>]*>(.*?)</div>', re.S)

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def find_links(text):
    """[(вид, id, исходная ссылка)] — вид 'folder' или 'file'."""
    links, seen = [], set()
    for url in re.findall(r"https?://\S+", text or ""):
        url = url.rstrip(".,;:!?)»")
        for kind, rx in (("folder", FOLDER_RE), ("file", FILE_RE)):
            m = rx.match(url)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                links.append((kind, m.group(1), url))
                break
    return links


async def _list_folder(session, folder_id, depth=0):
    """[(id, имя)] файлов папки (вложенные папки — рекурсивно)."""
    async with session.get(FOLDER_VIEW, params={"id": folder_id}) as response:
        if response.status in (401, 403, 404):
            raise ValueError("папка закрыта или не найдена — нужен доступ «Все, у кого есть ссылка»")
        if response.status >= 400:
            raise ValueError(f"Google Drive ответил ошибкой {response.status}")
        page = await response.text()
    if "flip-entry" not in page and ("ServiceLogin" in page or "accounts.google.com" in page):
        raise ValueError("папка закрыта — нужен доступ «Все, у кого есть ссылка»")
    files = []
    for entry_id, href, title in ENTRY_RE.findall(page):
        name = html.unescape(re.sub(r"<[^>]+>", "", title)).strip() or entry_id
        if "/folders/" in href:
            if depth < MAX_DEPTH:
                files += await _list_folder(session, entry_id, depth + 1)
        else:
            files.append((entry_id, name))
        if len(files) >= MAX_FILES:
            break
    return files


async def _download(session, file_id):
    async with session.get(DOWNLOAD, params={"id": file_id, "export": "download"},
                           allow_redirects=True) as response:
        if response.status in (401, 403, 404):
            raise ValueError("файл закрыт или не найден")
        if response.status >= 400:
            raise ValueError(f"ошибка {response.status}")
        data = await read_limited(response, MAX_FILE_BYTES)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("файл больше 20 МБ")
        return data, response.headers.get("Content-Disposition", "")


def _name_from_disposition(value, fallback):
    m = re.search(r"filename\*=UTF-8''([^;]+)", value) or re.search(r'filename="([^"]+)"', value)
    if not m:
        return fallback
    from urllib.parse import unquote
    return unquote(m.group(1))


async def collect(text, max_images):
    result = DiskFiles()
    links = find_links(text)
    if not links:
        return result
    timeout = aiohttp.ClientTimeout(total=180)
    headers = {"User-Agent": "Mozilla/5.0 (homework-bot)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for kind, item_id, url in links:
            result.urls.append(url)
            try:
                files = await _list_folder(session, item_id) if kind == "folder" else [(item_id, None)]
            except (ValueError, aiohttp.ClientError, TimeoutError) as e:
                result.problems.append(f"{url} — {e}")
                continue
            files.sort(key=lambda f: _natural_key(f[1] or ""))
            for file_id, name in files:
                lower = (name or "").lower()
                if name and not lower.endswith(IMAGE_EXT + TABLE_EXT + TEXT_EXT):
                    result.unseen.append(name)
                    continue
                if name and lower.endswith(IMAGE_EXT) and len(result.images) >= max_images:
                    result.unseen.append(f"{name} (скриншотов больше {max_images})")
                    continue
                try:
                    data, disposition = await _download(session, file_id)
                except (ValueError, aiohttp.ClientError, TimeoutError) as e:
                    result.problems.append(f"{name or url} — не скачался ({e})")
                    continue
                name = name or _name_from_disposition(disposition, f"файл {file_id[:6]}")
                lower = name.lower()
                if sniff_image(data):
                    if len(result.images) >= max_images:
                        result.unseen.append(f"{name} (скриншотов больше {max_images})")
                        continue
                    mime, shown = shrink_image(data)
                    result.images.append((mime, shown, name))
                elif lower.endswith(TABLE_EXT) or data[:2] == b"PK":
                    result.tables.append((name if lower.endswith(TABLE_EXT) else name + ".xlsx", data))
                elif lower.endswith(TEXT_EXT):
                    result.texts.append((name, data))
                elif data.lstrip()[:1] == b"<":
                    result.problems.append(f"{name} — закрыт доступ, нужен «Все, у кого есть ссылка»")
                else:
                    result.unseen.append(name)
    log.info("Google Drive: картинок %s, таблиц %s, текстов %s, не показано %s, проблем %s",
             len(result.images), len(result.tables), len(result.texts), len(result.unseen), len(result.problems))
    return result
