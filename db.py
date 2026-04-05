import sqlite3

conn = sqlite3.connect("bot.db")
cursor = conn.cursor()

def init_db():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS homeworks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        status TEXT,
        current_version INTEGER DEFAULT 1
    )
    """)

    # 👇 ВОТ ЭТО ДОБАВЬ
    try:
        cursor.execute("ALTER TABLE homeworks ADD COLUMN current_version INTEGER DEFAULT 1")
    except:
        pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        homework_id INTEGER,
        text TEXT,
        comment TEXT,
        file_id TEXT
    )
    """)
    # 👇 ВОТ ЭТО ДОБАВЬ
    try:
        cursor.execute("ALTER TABLE versions ADD COLUMN file_id TEXT")
    except:
        pass

    try:
        cursor.execute("ALTER TABLE versions ADD COLUMN file_type TEXT")
    except:
        pass

    conn.commit()



def create_homework(student_id, text=None, file_id=None, file_type=None):
    cursor.execute(
        "INSERT INTO homeworks (student_id, status) VALUES (?, ?)",
        (student_id, "new")
    )
    homework_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO versions (homework_id, text, file_id, file_type) VALUES (?, ?, ?, ?)",
        (homework_id, text, file_id, file_type)
    )

    conn.commit()
    return homework_id


def update_status(homework_id, status):
    cursor.execute(
        "UPDATE homeworks SET status=? WHERE id=?",
        (status, homework_id)
    )
    conn.commit()


def add_comment(homework_id, comment):
    cursor.execute(
        "UPDATE versions SET comment=? WHERE homework_id=?",
        (comment, homework_id)
    )
    conn.commit()


def get_student_id(homework_id):
    cursor.execute(
        "SELECT student_id FROM homeworks WHERE id=?",
        (homework_id,)
    )
    return cursor.fetchone()[0]


def get_active_homework(student_id):
    cursor.execute("""
    SELECT id FROM homeworks
    WHERE student_id=? AND status='revision'
    ORDER BY id DESC LIMIT 1
    """, (student_id,))

    result = cursor.fetchone()
    return result[0] if result else None


def add_version(homework_id, text=None, file_id=None, file_type=None):
    cursor.execute("""
    SELECT current_version FROM homeworks WHERE id=?
    """, (homework_id,))

    version = cursor.fetchone()[0] + 1

    cursor.execute("""
    UPDATE homeworks SET current_version=? WHERE id=?
    """, (version, homework_id))

    cursor.execute("""
    INSERT INTO versions (homework_id, text, file_id, file_type)
    VALUES (?, ?, ?, ?)
    """, (homework_id, text, file_id, file_type))

    conn.commit()


def get_current_version(homework_id):
    cursor.execute("""
    SELECT current_version FROM homeworks WHERE id=?
    """, (homework_id,))

    return cursor.fetchone()[0]