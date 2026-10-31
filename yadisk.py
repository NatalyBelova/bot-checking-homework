"""Публичные ссылки Яндекс.Диска: папка со скриншотами или отдельный файл.

Ученики часто сдают скриншоты блока одной ссылкой на папку (disk.yandex.ru/d/…). Целиком папку
Яндекс отдаёт архивом и на больших папках отвечает ошибкой 500, поэтому берём список файлов через
API и скачиваем каждый файл отдельно: картинки — как скриншоты, таблицы — как таблицы, .txt — как текст.
"""
import io
import re
from dataclasses import dataclass, field

import aiohttp

from common import log, read_limited, sniff_image

TITLE = "Яндекс.Диск"
API = "https://cloud-api.yandex.net/v1/disk/public/resources"
MAX_FILES = 200                   # сколько файлов смотрим в папке (весь блок — это 30–50 скриншотов + файлы)
PAGE = 100                        # столько Яндекс отдаёт за один запрос списка
MAX_DEPTH = 2                     # папки внутри папки
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_SIDE = 2000             # большие скриншоты уменьшаем — так быстрее и дешевле для ИИ

TABLE_EXT = (".xlsx", ".xlsm", ".xls", ".csv")
TEXT_EXT = (".txt", ".md", ".log", ".json", ".sql", ".yml", ".yaml")
LINK_EXT = (".url", ".webloc")    # ярлыки-ссылки (Windows / macOS): в них ссылка на Google Таблицу, Jira и т.п.
DOC_EXT = (".docx",)              # Word: текст и ссылки из него

URL_RE = re.compile(r"https?://(?:disk\.yandex\.(?:ru|com|by|kz)|yadi\.sk|[\w.-]+\.disk\.yandex\.ru)/\S+", re.I)


@dataclass
class DiskFiles:
    images: list = field(default_factory=list)     # [(mime, bytes, имя)]
    tables: list = field(default_factory=list)     # [(имя, bytes)]
    texts: list = field(default_factory=list)      # [(имя, bytes)]
    unseen: list = field(default_factory=list)     # имена файлов, которые ИИ не увидит
    problems: list = field(default_factory=list)   # ссылки, которые не открылись
    links: list = field(default_factory=list)      # ссылки из ярлыков .url и документов .docx
    urls: list = field(default_factory=list)       # какие ссылки обработаны здесь


def find_urls(text):
    urls = []
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,;:!?)»")
        if url not in urls:
            urls.append(url)
    return urls


URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+")


def shortcut_links(data):
    """Ссылки из ярлыка: Windows .url (URL=…) или macOS .webloc (plist)."""
    text = data.decode("utf-8", "ignore")
    return [u.rstrip(".,;)") for u in URL_IN_TEXT_RE.findall(text)]


def docx_text(data):
    """(текст, ссылки) документа Word. Без сторонних библиотек: .docx — это zip с XML."""
    import html
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
            rels = z.read("word/_rels/document.xml.rels").decode("utf-8", "ignore") \
                if "word/_rels/document.xml.rels" in z.namelist() else ""
    except (zipfile.BadZipFile, KeyError):
        return "", []
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()
    links = re.findall(r'Target="(https?://[^"]+)"[^>]*TargetMode="External"', rels)
    links += re.findall(r'TargetMode="External"[^>]*Target="(https?://[^"]+)"', rels)
    links += URL_IN_TEXT_RE.findall(text)
    seen, unique = set(), []
    for u in (html.unescape(x).rstrip(".,;)") for x in links):
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return text, unique


def add_link_file(result, name, data):
    """Ярлык или .docx: ссылки — в result.links, а текст — ИИ, чтобы он видел, что ученик прислал."""
    lower = name.lower()
    if lower.endswith(LINK_EXT):
        links = shortcut_links(data)
        text = "Файл-ссылка, ведёт на: " + (", ".join(links) or "(ссылки нет)")
    else:
        text, links = docx_text(data)
        if links:
            text += "\n\nСсылки в документе: " + ", ".join(links)
    result.links += [u for u in links if u not in result.links]
    if text:
        result.texts.append((name, text.encode("utf-8")))


def _natural_key(name):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def shrink_image(data):
    """Уменьшает большую картинку до MAX_IMAGE_SIDE по длинной стороне. Возвращает (mime, bytes)."""
    mime = sniff_image(data)
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if max(img.size) <= MAX_IMAGE_SIDE and len(data) <= 4 * 1024 * 1024:
            return mime, data
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return "image/jpeg", out.getvalue()
    except Exception as e:  # Pillow нет или картинка необычная — отдаём как есть
        log.info("Яндекс.Диск: картинку не уменьшить (%s)", e)
        return mime, data


async def _meta(session, public_key, path=None, offset=0):
    params = {"public_key": public_key, "limit": str(PAGE), "offset": str(offset), "sort": "name"}
    if path:
        params["path"] = path
    async with session.get(API, params=params) as response:
        if response.status == 404:
            raise ValueError("на Яндекс.Диске не найдено или доступ закрыт")
        if response.status >= 400:
            raise ValueError(f"Яндекс.Диск ответил ошибкой {response.status}")
        return await response.json()


async def _list_files(session, public_key, path=None, depth=0):
    """Все файлы по ссылке (папка — рекурсивно, не глубже MAX_DEPTH, постранично)."""
    meta = await _meta(session, public_key, path)
    if meta.get("type") == "file":
        return [meta]
    files, offset = [], 0
    while True:
        embedded = meta.get("_embedded") or {}
        items = embedded.get("items") or []
        for item in items:
            if item.get("type") == "dir":
                if depth < MAX_DEPTH:
                    files += await _list_files(session, public_key, item.get("path"), depth + 1)
            else:
                files.append(item)
        offset += len(items)
        total = embedded.get("total") or 0
        if not items or offset >= total or len(files) >= MAX_FILES:
            break
        meta = await _meta(session, public_key, path, offset)
    return files[:MAX_FILES]


async def _download(session, item):
    href = item.get("file")
    if not href:
        raise ValueError("нет ссылки на скачивание")
    async with session.get(href, allow_redirects=True) as response:
        if response.status >= 400:
            raise ValueError(f"ошибка {response.status}")
        data = await read_limited(response, MAX_FILE_BYTES)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("файл больше 20 МБ")
        return data


async def collect(text, max_images):
    """Скачивает файлы по всем ссылкам Яндекс.Диска из текста ученика."""
    result = DiskFiles()
    urls = find_urls(text)
    if not urls:
        return result
    timeout = aiohttp.ClientTimeout(total=180)
    headers = {"User-Agent": "Mozilla/5.0 (homework-bot)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for url in urls:
            result.urls.append(url)
            try:
                files = await _list_files(session, url)
            except (ValueError, aiohttp.ClientError, TimeoutError) as e:
                result.problems.append(f"{url} — {e}")
                continue
            files.sort(key=lambda f: _natural_key(f.get("name") or ""))
            for item in files:
                name = item.get("name") or "файл"
                mime = (item.get("mime_type") or "").lower()
                lower = name.lower()
                if (item.get("size") or 0) > MAX_FILE_BYTES:
                    result.unseen.append(f"{name} (больше 20 МБ)")
                    continue
                if mime.startswith("image/"):
                    if len(result.images) >= max_images:
                        result.unseen.append(f"{name} (скриншотов больше {max_images})")
                        continue
                    kind = "image"
                elif lower.endswith(TABLE_EXT):
                    kind = "table"
                elif lower.endswith(LINK_EXT + DOC_EXT):
                    kind = "links"
                elif lower.endswith(TEXT_EXT) or mime.startswith("text/"):
                    kind = "text"
                else:
                    result.unseen.append(name)
                    continue
                try:
                    data = await _download(session, item)
                except (ValueError, aiohttp.ClientError, TimeoutError) as e:
                    result.problems.append(f"{name} — не скачался ({e})")
                    continue
                if kind == "image":
                    if not sniff_image(data):
                        result.unseen.append(f"{name} (не картинка)")
                        continue
                    shown_mime, shown = shrink_image(data)
                    result.images.append((shown_mime, shown, name))
                elif kind == "table":
                    result.tables.append((name, data))
                elif kind == "links":
                    add_link_file(result, name, data)
                else:
                    result.texts.append((name, data))
    log.info("Яндекс.Диск: картинок %s, таблиц %s, текстов %s, ссылок %s, не показано %s, проблем %s",
             len(result.images), len(result.tables), len(result.texts), len(result.links), len(result.unseen),
             len(result.problems))
    return result
