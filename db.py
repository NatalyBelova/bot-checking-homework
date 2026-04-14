import psycopg2
import os

DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()


def init_db():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS homeworks (
        id SERIAL PRIMARY KEY,
        student_id BIGINT,
        status TEXT,
        current_version INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS versions (
        id SERIAL PRIMARY KEY,
        homework_id INTEGER,
        text TEXT,
        comment TEXT,
        file_id TEXT,
        file_type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()


def create_homework(student_id, text=None, file_id=None, file_type=None):
    cursor.execute(
        "INSERT INTO homeworks (student_id, status) VALUES (%s, %s) RETURNING id",
        (student_id, "new")
    )
    homework_id = cursor.fetchone()[0]

    cursor.execute(
        "INSERT INTO versions (homework_id, text, file_id, file_type) VALUES (%s, %s, %s, %s)",
        (homework_id, text, file_id, file_type)
    )

    conn.commit()
    return homework_id


def update_status(homework_id, status):
    cursor.execute(
        "UPDATE homeworks SET status=%s WHERE id=%s",
        (status, homework_id)
    )
    conn.commit()


def add_comment(homework_id, comment):
    cursor.execute("""
    UPDATE versions
    SET comment=%s
    WHERE id = (
        SELECT id FROM versions
        WHERE homework_id=%s
        ORDER BY id DESC
        LIMIT 1
    )
    """, (comment, homework_id))

    conn.commit()


def get_student_id(homework_id):
    cursor.execute(
        "SELECT student_id FROM homeworks WHERE id=%s",
        (homework_id,)
    )
    result = cursor.fetchone()
    return result[0] if result else None


def get_active_homework(student_id):
    cursor.execute("""
    SELECT id FROM homeworks
    WHERE student_id=%s AND status IN ('revision', 'new')
    ORDER BY id DESC
    LIMIT 1
    """, (student_id,))

    result = cursor.fetchone()
    return result[0] if result else None


def add_version(homework_id, text=None, file_id=None, file_type=None):
    cursor.execute(
        "SELECT current_version FROM homeworks WHERE id=%s",
        (homework_id,)
    )
    version = cursor.fetchone()[0] + 1

    cursor.execute(
        "UPDATE homeworks SET current_version=%s WHERE id=%s",
        (version, homework_id)
    )

    cursor.execute("""
    INSERT INTO versions (homework_id, text, file_id, file_type)
    VALUES (%s, %s, %s, %s)
    """, (homework_id, text, file_id, file_type))

    conn.commit()


def get_current_version(homework_id):
    cursor.execute(
        "SELECT current_version FROM homeworks WHERE id=%s",
        (homework_id,)
    )
    return cursor.fetchone()[0]


def get_homework_status(homework_id):
    cursor.execute(
        "SELECT status FROM homeworks WHERE id=%s",
        (homework_id,)
    )
    return cursor.fetchone()[0]