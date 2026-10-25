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
        _pool = ThreadedConnectionPool(1, 10, DATABASE_URL)
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
            SELECT id, title, kind, reference, is_active, sort_order
            FROM assignments
            {"WHERE is_active" if active_only else ""}
            ORDER BY sort_order, id
            """
        )
        return cursor.fetchall()


@_async
def get_assignment(assignment_id):
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            "SELECT id, title, kind, reference, is_active FROM assignments WHERE id=%s",
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
                   h.created_at, h.updated_at
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
