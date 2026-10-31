"""Работа с PostgreSQL.

Все публичные функции асинхронные: синхронный psycopg2 выполняется в отдельном
потоке (asyncio.to_thread), поэтому долгие запросы не блокируют бота.
"""
import asyncio
import functools
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL")

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        # время в базе всегда в UTC — в местное переводим при показе
        _pool = ThreadedConnectionPool(1, 10, DATABASE_URL, options="-c timezone=UTC")
    return _pool


@contextmanager
def _cursor(dict_rows=False):
    """Берёт соединение из пула, коммитит при успехе, откатывает при ошибке."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cursor_factory = RealDictCursor if dict_rows else None
        with conn.cursor(cursor_factory=cursor_factory) as cursor:
            yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _async(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    wrapper.sync = fn  # синхронная версия, если нужна вне event loop
    return wrapper


# ---------------------------------------------------------------------------
# Схема и миграции
# ---------------------------------------------------------------------------
# Все шаги идемпотентны: их можно запускать на пустой базе и на боевой базе
# старой версии бота — существующие ДЗ и версии сохраняются.

MIGRATIONS = [
    # --- исходные таблицы (как в старой версии) ---
    """
    CREATE TABLE IF NOT EXISTS homeworks (
        id SERIAL PRIMARY KEY,
        student_id BIGINT,
        status TEXT,
        current_version INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS versions (
        id SERIAL PRIMARY KEY,
        homework_id INTEGER,
        text TEXT,
        comment TEXT,
        file_id TEXT,
        file_type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # --- задания и эталоны ---
    """
    CREATE TABLE IF NOT EXISTS assignments (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'screenshots',  -- screenshots | checklist | jira
        reference TEXT,                             -- эталон и критерии приёмки
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS assignment_id INTEGER REFERENCES assignments(id)",
    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS student_name TEXT",
    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    # --- номер версии и роль автора комментария ---
    "ALTER TABLE versions ADD COLUMN IF NOT EXISTS version_num INTEGER",
    "ALTER TABLE versions ADD COLUMN IF NOT EXISTS reviewed_by TEXT",  # 'human:<id>' | 'ai'
    """
    UPDATE versions v SET version_num = sub.rn
    FROM (
        SELECT id, ROW_NUMBER() OVER (PARTITION BY homework_id ORDER BY id) AS rn
        FROM versions
    ) sub
    WHERE v.id = sub.id AND v.version_num IS NULL
    """,
    # --- все файлы версии (альбом = одна версия с несколькими файлами) ---
    """
    CREATE TABLE IF NOT EXISTS version_files (
        id SERIAL PRIMARY KEY,
        version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
        file_id TEXT NOT NULL,
        file_type TEXT NOT NULL
    )
    """,
    # перенос файлов старых версий, у которых был один file_id
    """
    INSERT INTO version_files (version_id, file_id, file_type)
    SELECT v.id, v.file_id, COALESCE(v.file_type, 'document')
    FROM versions v
    WHERE v.file_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM version_files f WHERE f.version_id = v.id)
    """,
    # --- состояние диалога (раньше жило в памяти и терялось при перезапуске) ---
    """
    CREATE TABLE IF NOT EXISTS user_states (
        user_id BIGINT PRIMARY KEY,
        state TEXT NOT NULL,
        data JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # --- картинки эталона (храним сами файлы: file_id тестового и боевого ботов разные) ---
    """
    CREATE TABLE IF NOT EXISTS assignment_files (
        id SERIAL PRIMARY KEY,
        assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
        mime TEXT NOT NULL DEFAULT 'image/jpeg',
        data BYTEA NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # --- результаты ИИ-проверки ---
    """
    CREATE TABLE IF NOT EXISTS ai_reviews (
        id SERIAL PRIMARY KEY,
        homework_id INTEGER NOT NULL REFERENCES homeworks(id) ON DELETE CASCADE,
        version_id INTEGER REFERENCES versions(id) ON DELETE CASCADE,
        verdict TEXT,               -- accept | revise | unsure | NULL при ошибке
        comment TEXT,               -- текст для ученика
        note TEXT,                  -- заметка для проверяющего
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        error TEXT,
        applied_by TEXT,            -- 'ai' | 'ai+human:<id>' | NULL (не применено)
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_by TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # --- блок 5 (Jira): разбор каждого бага и люди, добавленные в Jira через бота ---
    "ALTER TABLE ai_reviews ADD COLUMN IF NOT EXISTS details JSONB",
    "ALTER TABLE ai_reviews ADD COLUMN IF NOT EXISTS score SMALLINT",  # оценка ИИ 1–10 для статистики
    """
    CREATE TABLE IF NOT EXISTS jira_bugs (
        id SERIAL PRIMARY KEY,
        homework_id INTEGER REFERENCES homeworks(id) ON DELETE CASCADE,
        review_id INTEGER,
        issue_key TEXT NOT NULL,
        bug_number INTEGER,              -- номер бага из списка преподавателя (NULL — не из списка)
        status TEXT NOT NULL,            -- counted | fix | rejected
        reporter_id TEXT,                -- accountId автора задачи в Jira
        delete_requested BOOLEAN DEFAULT FALSE,
        deleted_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jira_bugs_homework ON jira_bugs(homework_id)",
    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS jira_owner TEXT",  # accountId ученика в Jira
    """
    CREATE TABLE IF NOT EXISTS jira_users (
        id SERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        account_id TEXT,
        display_name TEXT,
        student_id BIGINT,
        added_by TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        removed_at TIMESTAMP,
        removed_by TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_homeworks_student ON homeworks(student_id)",
    "CREATE INDEX IF NOT EXISTS idx_versions_homework ON versions(homework_id)",
    "CREATE INDEX IF NOT EXISTS idx_version_files_version ON version_files(version_id)",
]


def init_db():
    with _cursor() as cursor:
        for statement in MIGRATIONS:
            cursor.execute(statement)


# ---------------------------------------------------------------------------
# Задания
# ---------------------------------------------------------------------------

@_async
def list_assignments(active_only=True):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            f"""
            SELECT a.id, a.title, a.kind, a.reference, a.is_active, a.sort_order,
                   (SELECT COUNT(*) FROM assignment_files f WHERE f.assignment_id = a.id) AS ref_files
            FROM assignments a
            {"WHERE a.is_active" if active_only else ""}
            ORDER BY a.sort_order, a.id
            """
        )
        return cursor.fetchall()


@_async
def get_assignment(assignment_id):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT a.id, a.title, a.kind, a.reference, a.is_active,
                   (SELECT COUNT(*) FROM assignment_files f WHERE f.assignment_id = a.id) AS ref_files
            FROM assignments a WHERE a.id=%s
            """,
            (assignment_id,),
        )
        return cursor.fetchone()


@_async
def add_assignment(title, kind="screenshots", reference=None):
    with _cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO assignments (title, kind, reference, sort_order)
            VALUES (%s, %s, %s, COALESCE((SELECT MAX(sort_order) FROM assignments), 0) + 1)
            RETURNING id
            """,
            (title, kind, reference),
        )
        return cursor.fetchone()[0]


@_async
def set_assignment_reference(assignment_id, reference):
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE assignments SET reference=%s WHERE id=%s", (reference, assignment_id)
        )
        return cursor.rowcount > 0


@_async
def count_homeworks_for_assignment(assignment_id):
    with _cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM homeworks WHERE assignment_id=%s", (assignment_id,))
        return cursor.fetchone()[0]


@_async
def delete_assignment(assignment_id, with_homeworks=False):
    """Удаляет задание. Без with_homeworks — только если по нему нет ДЗ.

    С with_homeworks удаляет и все ДЗ задания: попытки, файлы, проверки ИИ.
    """
    with _cursor() as cursor:
        if with_homeworks:
            cursor.execute("SELECT id FROM homeworks WHERE assignment_id=%s", (assignment_id,))
            ids = [row[0] for row in cursor.fetchall()]
            if ids:
                # version_files удаляются каскадом вместе с versions, ai_reviews — вместе с homeworks
                cursor.execute("DELETE FROM versions WHERE homework_id = ANY(%s)", (ids,))
                cursor.execute("DELETE FROM homeworks WHERE id = ANY(%s)", (ids,))
        cursor.execute(
            """
            DELETE FROM assignments a
            WHERE a.id=%s AND NOT EXISTS (SELECT 1 FROM homeworks h WHERE h.assignment_id = a.id)
            """,
            (assignment_id,),
        )
        return cursor.rowcount > 0


@_async
def homework_authors_for_assignment(assignment_id):
    """[(student_id, имя, сколько ДЗ)] по заданию."""
    with _cursor() as cursor:
        cursor.execute(
            """
            SELECT student_id, MAX(student_name), COUNT(*)
            FROM homeworks WHERE assignment_id=%s
            GROUP BY student_id ORDER BY COUNT(*) DESC
            """,
            (assignment_id,),
        )
        return cursor.fetchall()


@_async
def get_assignment_by_title(title):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            "SELECT id, title, kind, reference, is_active FROM assignments WHERE title=%s ORDER BY id LIMIT 1",
            (title,),
        )
        return cursor.fetchone()


@_async
def set_assignment_kind(assignment_id, kind):
    with _cursor() as cursor:
        cursor.execute("UPDATE assignments SET kind=%s WHERE id=%s", (kind, assignment_id))
        return cursor.rowcount > 0


@_async
def set_assignment_active(assignment_id, is_active):
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE assignments SET is_active=%s WHERE id=%s", (is_active, assignment_id)
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# ДЗ и версии
# ---------------------------------------------------------------------------

def _insert_version(cursor, homework_id, version_num, text, files):
    cursor.execute(
        """
        INSERT INTO versions (homework_id, version_num, text)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (homework_id, version_num, text),
    )
    version_id = cursor.fetchone()[0]
    for file_type, file_id in files:
        cursor.execute(
            "INSERT INTO version_files (version_id, file_id, file_type) VALUES (%s, %s, %s)",
            (version_id, file_id, file_type),
        )
    return version_id


@_async
def create_homework(student_id, assignment_id, text, files, student_name=None):
    """files — список кортежей (file_type, file_id)."""
    with _cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO homeworks (student_id, assignment_id, student_name, status)
            VALUES (%s, %s, %s, 'new')
            RETURNING id
            """,
            (student_id, assignment_id, student_name),
        )
        homework_id = cursor.fetchone()[0]
        _insert_version(cursor, homework_id, 1, text, files)
        return homework_id


@_async
def add_version(homework_id, text, files):
    """Новая попытка ученика: одна версия на всю отправку (включая альбом)."""
    with _cursor() as cursor:
        cursor.execute(
            """
            UPDATE homeworks
            SET current_version = current_version + 1,
                status = 'new',
                updated_at = CURRENT_TIMESTAMP
            WHERE id=%s
            RETURNING current_version
            """,
            (homework_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        _insert_version(cursor, homework_id, row[0], text, files)
        return row[0]


@_async
def replace_pending_version(homework_id, text, files):
    """Ученик прислал работу заново, пока прошлая ещё ждёт проверки: старую версию убираем, новая
    встаёт на её место (номер попытки тот же — повторная отправка до проверки попыткой не считается).

    У новой версии новый id — поэтому карточки и проверка ИИ по старой версии становятся устаревшими.
    Возвращает (номер попытки, id старой версии) или None, если ДЗ уже не ждёт проверки.
    """
    with _cursor() as cursor:
        cursor.execute("SELECT status FROM homeworks WHERE id=%s FOR UPDATE", (homework_id,))
        row = cursor.fetchone()
        if not row or row[0] != "new":
            return None
        cursor.execute(
            "SELECT id, version_num FROM versions WHERE homework_id=%s ORDER BY id DESC LIMIT 1", (homework_id,))
        old = cursor.fetchone()
        if not old:
            return None
        old_id, num = old
        cursor.execute("DELETE FROM versions WHERE id=%s", (old_id,))
        _insert_version(cursor, homework_id, num, text, files)
        cursor.execute("UPDATE homeworks SET updated_at=CURRENT_TIMESTAMP WHERE id=%s", (homework_id,))
        return num, old_id


@_async
def update_status(homework_id, status):
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE homeworks SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (status, homework_id),
        )


@_async
def add_comment(homework_id, comment, reviewed_by=None):
    """Комментарий проверяющего к последней версии ДЗ."""
    with _cursor() as cursor:
        cursor.execute(
            """
            UPDATE versions
            SET comment=%s, reviewed_by=%s
            WHERE id = (
                SELECT id FROM versions
                WHERE homework_id=%s
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (comment, reviewed_by, homework_id),
        )


@_async
def get_homework(homework_id):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT h.id, h.student_id, h.student_name, h.status, h.current_version,
                   h.assignment_id, a.title AS assignment_title, a.kind AS assignment_kind,
                   h.created_at, h.updated_at, h.jira_owner
            FROM homeworks h
            LEFT JOIN assignments a ON a.id = h.assignment_id
            WHERE h.id=%s
            """,
            (homework_id,),
        )
        return cursor.fetchone()


@_async
def get_student_id(homework_id):
    with _cursor() as cursor:
        cursor.execute("SELECT student_id FROM homeworks WHERE id=%s", (homework_id,))
        row = cursor.fetchone()
        return row[0] if row else None


@_async
def get_homeworks_by_student(student_id):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT h.id, h.status, h.created_at, h.current_version,
                   a.title AS assignment_title
            FROM homeworks h
            LEFT JOIN assignments a ON a.id = h.assignment_id
            WHERE h.student_id=%s
            ORDER BY h.id DESC
            """,
            (student_id,),
        )
        return cursor.fetchall()


@_async
def get_versions(homework_id):
    """Все попытки ДЗ с файлами и комментариями — пригодится для ИИ-проверки."""
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT v.id, v.version_num, v.text, v.comment, v.reviewed_by, v.created_at,
                   COALESCE(
                       json_agg(json_build_array(f.file_type, f.file_id) ORDER BY f.id)
                       FILTER (WHERE f.id IS NOT NULL),
                       '[]'
                   ) AS files
            FROM versions v
            LEFT JOIN version_files f ON f.version_id = v.id
            WHERE v.homework_id=%s
            GROUP BY v.id
            ORDER BY v.id
            """,
            (homework_id,),
        )
        rows = cursor.fetchall()
        for row in rows:
            row["files"] = [tuple(f) for f in row["files"]]
        return rows


# ---------------------------------------------------------------------------
# Состояние диалога
# ---------------------------------------------------------------------------

@_async
def get_state(user_id):
    """Возвращает (state, data) или (None, {})."""
    with _cursor() as cursor:
        cursor.execute("SELECT state, data FROM user_states WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, {})


@_async
def set_state(user_id, state, data=None):
    with _cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO user_states (user_id, state, data, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
            SET state=EXCLUDED.state, data=EXCLUDED.data, updated_at=CURRENT_TIMESTAMP
            """,
            (user_id, state, Json(data or {})),
        )


@_async
def clear_state(user_id):
    with _cursor() as cursor:
        cursor.execute("DELETE FROM user_states WHERE user_id=%s", (user_id,))


# ---------------------------------------------------------------------------
# Отслеживание и статистика (для админа)
# ---------------------------------------------------------------------------

# Имя ученика берём из последнего ДЗ, где оно сохранено (у старых ДЗ его нет)
_STUDENT_NAME = """
    (SELECT h2.student_name FROM homeworks h2
     WHERE h2.student_id = {alias}.student_id AND h2.student_name IS NOT NULL
     ORDER BY h2.id DESC LIMIT 1)
"""


@_async
def stats_overview():
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status='new') AS pending,
                COUNT(*) FILTER (WHERE status='revision') AS revision,
                COUNT(*) FILTER (WHERE status='accepted') AS accepted,
                COUNT(DISTINCT student_id) AS students,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') AS new_week,
                COUNT(*) FILTER (WHERE status='accepted'
                                 AND updated_at >= NOW() - INTERVAL '7 days') AS accepted_week,
                ROUND(AVG(current_version) FILTER (WHERE status='accepted'), 1) AS avg_attempts,
                MIN(updated_at) FILTER (WHERE status='new') AS oldest_pending
            FROM homeworks
            """
        )
        return cursor.fetchone()


@_async
def stats_by_assignment():
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT a.id, a.title,
                   COUNT(h.id) AS total,
                   COUNT(h.id) FILTER (WHERE h.status='accepted') AS accepted,
                   COUNT(h.id) FILTER (WHERE h.status='revision') AS revision,
                   COUNT(h.id) FILTER (WHERE h.status='new') AS pending
            FROM assignments a
            LEFT JOIN homeworks h ON h.assignment_id = a.id
            GROUP BY a.id
            ORDER BY a.sort_order, a.id
            """
        )
        rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status='accepted') AS accepted,
                   COUNT(*) FILTER (WHERE status='revision') AS revision,
                   COUNT(*) FILTER (WHERE status='new') AS pending
            FROM homeworks WHERE assignment_id IS NULL
            """
        )
        no_task = cursor.fetchone()
        if no_task["total"]:
            rows.append({"id": None, "title": "Без задания", **no_task})
        return rows


@_async
def list_pending(limit=30):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            f"""
            SELECT h.id, h.student_id, h.current_version, h.updated_at,
                   a.title AS assignment_title,
                   {_STUDENT_NAME.format(alias="h")} AS student_name
            FROM homeworks h
            LEFT JOIN assignments a ON a.id = h.assignment_id
            WHERE h.status='new'
            ORDER BY h.updated_at, h.id
            LIMIT %s
            """,
            (limit,),
        )
        return cursor.fetchall()


@_async
def list_students(offset=0, limit=15):
    """Ученики с прогрессом, последние активные сверху. Возвращает (rows, total)."""
    with _cursor(dict_rows=True) as cursor:
        cursor.execute("SELECT COUNT(DISTINCT student_id) AS n FROM homeworks")
        total = cursor.fetchone()["n"]
        cursor.execute(
            f"""
            SELECT s.student_id, s.total, s.accepted, s.revision, s.pending, s.last_activity,
                   {_STUDENT_NAME.format(alias="s")} AS student_name
            FROM (
                SELECT student_id,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status='accepted') AS accepted,
                       COUNT(*) FILTER (WHERE status='revision') AS revision,
                       COUNT(*) FILTER (WHERE status='new') AS pending,
                       MAX(COALESCE(updated_at, created_at)) AS last_activity
                FROM homeworks
                GROUP BY student_id
            ) s
            ORDER BY s.last_activity DESC NULLS LAST
            OFFSET %s LIMIT %s
            """,
            (offset, limit),
        )
        return cursor.fetchall(), total


@_async
def get_student_name(student_id):
    with _cursor() as cursor:
        cursor.execute(
            """
            SELECT student_name FROM homeworks
            WHERE student_id=%s AND student_name IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (student_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


@_async
def get_version_files(version_id):
    with _cursor() as cursor:
        cursor.execute(
            "SELECT file_type, file_id FROM version_files WHERE version_id=%s ORDER BY id",
            (version_id,),
        )
        return cursor.fetchall()


@_async
def export_data():
    """Всё для выгрузки в Excel: задания, ДЗ, попытки."""
    with _cursor(dict_rows=True) as cursor:
        cursor.execute("SELECT id, title FROM assignments ORDER BY sort_order, id")
        assignments = cursor.fetchall()

        cursor.execute(
            f"""
            SELECT h.id, h.student_id, h.status, h.current_version, h.created_at,
                   COALESCE(h.updated_at, h.created_at) AS updated_at,
                   h.assignment_id, a.title AS assignment_title,
                   {_STUDENT_NAME.format(alias="h")} AS student_name,
                   (SELECT v.comment FROM versions v
                    WHERE v.homework_id = h.id AND v.comment IS NOT NULL
                    ORDER BY v.id DESC LIMIT 1) AS last_comment
            FROM homeworks h
            LEFT JOIN assignments a ON a.id = h.assignment_id
            ORDER BY h.id
            """
        )
        homeworks = cursor.fetchall()

        cursor.execute(
            """
            SELECT v.homework_id, COALESCE(v.version_num, 0) AS version_num, v.created_at,
                   v.text, v.comment, v.reviewed_by,
                   (SELECT COUNT(*) FROM version_files f WHERE f.version_id = v.id) AS files,
                   (SELECT r.score FROM ai_reviews r WHERE r.version_id = v.id AND r.score IS NOT NULL
                    ORDER BY r.id DESC LIMIT 1) AS ai_score
            FROM versions v
            ORDER BY v.homework_id, v.id
            """
        )
        versions = cursor.fetchall()
        return assignments, homeworks, versions


# ---------------------------------------------------------------------------
# Эталоны и ИИ-проверка
# ---------------------------------------------------------------------------

@_async
def set_reference(assignment_id, text, images):
    """Заменяет эталон задания. images — список (mime, bytes)."""
    with _cursor() as cursor:
        cursor.execute("UPDATE assignments SET reference=%s WHERE id=%s", (text or None, assignment_id))
        cursor.execute("DELETE FROM assignment_files WHERE assignment_id=%s", (assignment_id,))
        for mime, data in images:
            cursor.execute(
                "INSERT INTO assignment_files (assignment_id, mime, data) VALUES (%s, %s, %s)",
                (assignment_id, mime, psycopg2.Binary(data)),
            )


@_async
def get_reference_files(assignment_id):
    """[(mime, bytes)] картинок эталона."""
    with _cursor() as cursor:
        cursor.execute(
            "SELECT mime, data FROM assignment_files WHERE assignment_id=%s ORDER BY id",
            (assignment_id,),
        )
        return [(mime, bytes(data)) for mime, data in cursor.fetchall()]


@_async
def count_reference_files(assignment_id):
    with _cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM assignment_files WHERE assignment_id=%s", (assignment_id,))
        return cursor.fetchone()[0]


@_async
def add_ai_review(homework_id, version_id, verdict=None, comment=None, note=None,
                  model=None, input_tokens=None, output_tokens=None, error=None, details=None, score=None):
    with _cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ai_reviews (homework_id, version_id, verdict, comment, note, model,
                                    input_tokens, output_tokens, error, details, score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (homework_id, version_id, verdict, comment, note, model, input_tokens, output_tokens, error,
             Json(details) if details is not None else None, score),
        )
        return cursor.fetchone()[0]


@_async
def get_ai_review(review_id):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute("SELECT * FROM ai_reviews WHERE id=%s", (review_id,))
        return cursor.fetchone()


@_async
def mark_ai_review_applied(review_id, applied_by):
    with _cursor() as cursor:
        cursor.execute("UPDATE ai_reviews SET applied_by=%s WHERE id=%s", (applied_by, review_id))


@_async
def get_latest_version_id(homework_id):
    with _cursor() as cursor:
        cursor.execute(
            "SELECT id FROM versions WHERE homework_id=%s ORDER BY id DESC LIMIT 1", (homework_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else None


@_async
def ai_stats():
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE error IS NOT NULL) AS errors,
                   COUNT(*) FILTER (WHERE applied_by = 'ai') AS auto_applied,
                   COUNT(*) FILTER (WHERE applied_by LIKE 'ai+human:%%') AS confirmed,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   ROUND(AVG(score)::numeric, 1) AS avg_score
            FROM ai_reviews
            """
        )
        return cursor.fetchone()


@_async
def unfinished_ai_checks(min_age_minutes=2, max_age_hours=24):
    """ДЗ на проверке, у последней попытки которых нет результата ИИ (проверка прервалась перезапуском).

    Только задания с эталоном — без эталона ИИ не вызывается, и карточка уходит сразу.
    """
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT h.id AS homework_id
            FROM homeworks h
            JOIN assignments a ON a.id = h.assignment_id
            JOIN LATERAL (
                SELECT v.id, v.created_at FROM versions v
                WHERE v.homework_id = h.id ORDER BY v.id DESC LIMIT 1
            ) lv ON TRUE
            WHERE h.status = 'new'
              AND (a.reference IS NOT NULL OR EXISTS (SELECT 1 FROM assignment_files f WHERE f.assignment_id = a.id))
              AND lv.created_at < NOW() - make_interval(mins => %s)
              AND lv.created_at > NOW() - make_interval(hours => %s)
              AND NOT EXISTS (SELECT 1 FROM ai_reviews r WHERE r.version_id = lv.id)
            ORDER BY h.id
            """,
            (min_age_minutes, max_age_hours),
        )
        return [row["homework_id"] for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Настройки, которые админ меняет из бота
# ---------------------------------------------------------------------------

def get_settings():
    with _cursor() as cursor:
        cursor.execute("SELECT key, value FROM settings")
        return dict(cursor.fetchall())


@_async
def set_setting(key, value, updated_by=None):
    with _cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO settings (key, value, updated_by, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = CURRENT_TIMESTAMP
            """,
            (key, str(value), updated_by),
        )


# ---------------------------------------------------------------------------
# Блок 5: баги ученика в Jira и люди в Jira
# ---------------------------------------------------------------------------

@_async
def save_jira_bugs(homework_id, review_id, bugs):
    """bugs: [{key, bug_number, status, reporter_id}] — итог разбора, который увидел ученик.

    Засчитанные и незасчитанные баги помечаются к удалению из Jira.
    """
    with _cursor() as cursor:
        for b in bugs:
            cursor.execute(
                """
                INSERT INTO jira_bugs (homework_id, review_id, issue_key, bug_number, status, reporter_id,
                                       delete_requested)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (homework_id, review_id, b["key"], b.get("bug_number"), b["status"], b.get("reporter_id"),
                 b["status"] in ("counted", "rejected")),
            )


@_async
def counted_bugs(homework_id):
    """{номер бага: ключ задачи} — баги, которые уже засчитаны по этому ДЗ."""
    with _cursor() as cursor:
        cursor.execute(
            """
            SELECT bug_number, MIN(issue_key) FROM jira_bugs
            WHERE homework_id=%s AND status='counted' AND bug_number IS NOT NULL
            GROUP BY bug_number
            """,
            (homework_id,),
        )
        return {row[0]: row[1] for row in cursor.fetchall()}


@_async
def deleted_issue_keys(homework_id):
    with _cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT issue_key FROM jira_bugs WHERE homework_id=%s AND deleted_at IS NOT NULL",
            (homework_id,),
        )
        return {row[0] for row in cursor.fetchall()}


@_async
def issues_to_delete(homework_id, review_id=None):
    """Задачи, помеченные к удалению и ещё не удалённые (по одной проверке или по всему ДЗ)."""
    with _cursor() as cursor:
        sql = """
            SELECT DISTINCT issue_key FROM jira_bugs b
            WHERE b.homework_id=%s AND b.delete_requested AND b.deleted_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM jira_bugs d WHERE d.issue_key=b.issue_key AND d.deleted_at IS NOT NULL)
        """
        params = [homework_id]
        if review_id is not None:
            sql += " AND b.review_id=%s"
            params.append(review_id)
        cursor.execute(sql + " ORDER BY issue_key", params)
        return [row[0] for row in cursor.fetchall()]


@_async
def mark_issues_deleted(keys):
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE jira_bugs SET deleted_at=CURRENT_TIMESTAMP WHERE issue_key = ANY(%s) AND deleted_at IS NULL",
            (list(keys),),
        )


@_async
def reporters_for_homework(homework_id):
    with _cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT reporter_id FROM jira_bugs WHERE homework_id=%s AND reporter_id IS NOT NULL",
            (homework_id,),
        )
        return [row[0] for row in cursor.fetchall()]


@_async
def add_jira_user(email, account_id, display_name, added_by):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            INSERT INTO jira_users (email, account_id, display_name, added_by)
            VALUES (%s, %s, %s, %s) RETURNING *
            """,
            (email.lower(), account_id, display_name, added_by),
        )
        return cursor.fetchone()


@_async
def active_jira_users():
    with _cursor(dict_rows=True) as cursor:
        cursor.execute("SELECT * FROM jira_users WHERE removed_at IS NULL ORDER BY added_at")
        return cursor.fetchall()


@_async
def get_jira_user(user_row_id):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute("SELECT * FROM jira_users WHERE id=%s", (user_row_id,))
        return cursor.fetchone()


@_async
def find_jira_user(email=None, account_id=None):
    """Активная запись по почте или accountId."""
    with _cursor(dict_rows=True) as cursor:
        if email:
            cursor.execute("SELECT * FROM jira_users WHERE email=%s AND removed_at IS NULL "
                           "ORDER BY id DESC LIMIT 1", (email.lower(),))
        else:
            cursor.execute("SELECT * FROM jira_users WHERE account_id=%s AND removed_at IS NULL "
                           "ORDER BY id DESC LIMIT 1", (account_id,))
        return cursor.fetchone()


@_async
def link_jira_users_to_student(account_ids, student_id):
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE jira_users SET student_id=%s WHERE account_id = ANY(%s) AND removed_at IS NULL",
            (student_id, list(account_ids)),
        )


@_async
def mark_jira_user_removed(account_id, removed_by):
    with _cursor() as cursor:
        cursor.execute(
            "UPDATE jira_users SET removed_at=CURRENT_TIMESTAMP, removed_by=%s "
            "WHERE account_id=%s AND removed_at IS NULL",
            (removed_by, account_id),
        )


@_async
def jira_bug_keys(homework_id):
    with _cursor() as cursor:
        cursor.execute("SELECT DISTINCT issue_key FROM jira_bugs WHERE homework_id=%s", (homework_id,))
        return [row[0] for row in cursor.fetchall()]


@_async
def set_jira_owner(homework_id, account_id):
    with _cursor() as cursor:
        cursor.execute("UPDATE homeworks SET jira_owner=%s WHERE id=%s AND jira_owner IS NULL",
                       (account_id, homework_id))


@_async
def jira_accounts_of_student(student_id):
    """accountId, которые уже связаны с этим учеником (через /jira или прошлые ДЗ блока 5)."""
    with _cursor() as cursor:
        cursor.execute(
            """
            SELECT account_id FROM jira_users WHERE student_id=%s AND removed_at IS NULL AND account_id IS NOT NULL
            UNION
            SELECT jira_owner FROM homeworks WHERE student_id=%s AND jira_owner IS NOT NULL
            """,
            (student_id, student_id),
        )
        return [row[0] for row in cursor.fetchall()]


@_async
def latest_homework_for_assignment(student_id, assignment_id):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            "SELECT id, status, current_version FROM homeworks WHERE student_id=%s AND assignment_id=%s "
            "ORDER BY id DESC LIMIT 1",
            (student_id, assignment_id),
        )
        return cursor.fetchone()


@_async
def latest_version_has_ai_review(homework_id):
    with _cursor() as cursor:
        cursor.execute(
            """
            SELECT EXISTS (SELECT 1 FROM ai_reviews r WHERE r.version_id = (
                SELECT id FROM versions WHERE homework_id=%s ORDER BY id DESC LIMIT 1))
            """,
            (homework_id,),
        )
        return cursor.fetchone()[0]
