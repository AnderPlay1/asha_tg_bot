import aiosqlite
from datetime import datetime

CREATE_USERS = '''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
'''

CREATE_SUBMISSIONS = '''
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    task_index INTEGER NOT NULL,
    task_text TEXT NOT NULL,
    file_id TEXT NOT NULL,
    caption TEXT,
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_at TEXT,
    reviewer_id INTEGER,
    review_comment TEXT,
    media_type TEXT NOT NULL DEFAULT 'photo',
    FOREIGN KEY(user_id) REFERENCES users(id)
);
'''

CREATE_SETTINGS = '''
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
'''


async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(CREATE_USERS)
        await db.execute(CREATE_SUBMISSIONS)
        await db.execute(CREATE_SETTINGS)
        await db.commit()
        # Migration: add media_type if missing
        try:
            await db.execute(
                "ALTER TABLE submissions ADD COLUMN media_type TEXT NOT NULL DEFAULT 'photo'"
            )
            await db.commit()
        except Exception:
            pass  # column already exists


async def get_user(db: aiosqlite.Connection, tg_id: int):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute('SELECT * FROM users WHERE tg_id = ?', (tg_id,))
    return await cursor.fetchone()


async def create_user(db: aiosqlite.Connection, tg_id: int, username: str | None, first_name: str | None, last_name: str | None, is_admin: bool = False):
    started_at = datetime.utcnow().isoformat()
    await db.execute(
        'INSERT OR IGNORE INTO users (tg_id, username, first_name, last_name, is_admin, started_at, active) VALUES (?, ?, ?, ?, ?, ?, 1)',
        (tg_id, username, first_name, last_name, int(is_admin), started_at),
    )
    await db.commit()
    return await get_user(db, tg_id)


async def update_user_admin_status(db: aiosqlite.Connection, user_id: int, is_admin: bool):
    await db.execute('UPDATE users SET is_admin = ? WHERE id = ?', (int(is_admin), user_id))
    await db.commit()


async def list_participants(db: aiosqlite.Connection):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute('SELECT id, tg_id, username, first_name, last_name, is_admin FROM users ORDER BY started_at')
    return await cursor.fetchall()


async def list_user_submissions(db: aiosqlite.Connection, user_id: int):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute('SELECT * FROM submissions WHERE user_id = ? ORDER BY submitted_at', (user_id,))
    return await cursor.fetchall()


async def list_pending_submissions(db: aiosqlite.Connection):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute('SELECT s.*, u.tg_id, u.username, u.first_name, u.last_name FROM submissions s JOIN users u ON s.user_id = u.id WHERE s.status = ? ORDER BY s.submitted_at', ('pending',))
    return await cursor.fetchall()


async def get_submission(db: aiosqlite.Connection, submission_id: int):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute('SELECT s.*, u.tg_id, u.username, u.first_name, u.last_name FROM submissions s JOIN users u ON s.user_id = u.id WHERE s.id = ?', (submission_id,))
    return await cursor.fetchone()


async def insert_submission(db: aiosqlite.Connection, user_id: int, task_index: int, task_text: str, file_id: str, caption: str | None, media_type: str = 'photo'):
    submitted_at = datetime.utcnow().isoformat()
    cursor = await db.execute(
        'INSERT INTO submissions (user_id, task_index, task_text, file_id, caption, submitted_at, status, media_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (user_id, task_index, task_text, file_id,
         caption, submitted_at, 'pending', media_type),
    )
    await db.commit()
    return cursor.lastrowid


async def get_completed_task_indexes(db: aiosqlite.Connection, user_id: int):
    cursor = await db.execute('SELECT task_index FROM submissions WHERE user_id = ?', (user_id,))
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def update_submission_status(db: aiosqlite.Connection, submission_id: int, status: str, reviewer_id: int | None, review_comment: str | None = None):
    reviewed_at = datetime.utcnow().isoformat()
    await db.execute('UPDATE submissions SET status = ?, reviewer_id = ?, reviewed_at = ?, review_comment = ? WHERE id = ?', (status, reviewer_id, reviewed_at, review_comment, submission_id))
    await db.commit()


async def get_setting(db: aiosqlite.Connection, key: str):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = await cursor.fetchone()
    return row['value'] if row else None


async def set_setting(db: aiosqlite.Connection, key: str, value: str):
    await db.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    await db.commit()


async def get_or_create_event_state(db: aiosqlite.Connection):
    date_value = await get_setting(db, 'event_date')
    ended = await get_setting(db, 'event_ended')
    return date_value, ended == '1'


async def set_event_state(db: aiosqlite.Connection, date_value: str, ended: bool):
    await set_setting(db, 'event_date', date_value)
    await set_setting(db, 'event_ended', '1' if ended else '0')


async def get_leaderboard(db: aiosqlite.Connection, limit: int = 10):
    """Получить топ участников по количеству принятых заданий."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute('''
        SELECT 
            u.tg_id,
            u.username,
            u.first_name,
            u.last_name,
            COUNT(CASE WHEN s.status = 'accepted' THEN 1 END) as accepted_count,
            COUNT(s.id) as total_submissions
        FROM users u
        LEFT JOIN submissions s ON u.id = s.user_id
        GROUP BY u.id
        HAVING accepted_count > 0
        ORDER BY accepted_count DESC, total_submissions ASC
        LIMIT ?
    ''', (limit,))
    return await cursor.fetchall()


async def get_global_stats(db: aiosqlite.Connection):
    """Получить глобальную статистику по всему фотоквесту."""
    db.row_factory = aiosqlite.Row

    # Общее количество участников
    cursor = await db.execute('SELECT COUNT(*) as count FROM users WHERE is_admin = 0')
    participants = (await cursor.fetchone())['count']

    # Количество отправленных заданий
    cursor = await db.execute('SELECT COUNT(*) as count FROM submissions')
    total_submissions = (await cursor.fetchone())['count']

    # Количество принятых заданий
    cursor = await db.execute("SELECT COUNT(*) as count FROM submissions WHERE status = 'accepted'")
    accepted = (await cursor.fetchone())['count']

    # Количество отклонённых заданий
    cursor = await db.execute("SELECT COUNT(*) as count FROM submissions WHERE status = 'rejected'")
    rejected = (await cursor.fetchone())['count']

    # Количество ожидающих проверки
    cursor = await db.execute("SELECT COUNT(*) as count FROM submissions WHERE status = 'pending'")
    pending = (await cursor.fetchone())['count']

    return {
        'participants': participants,
        'total_submissions': total_submissions,
        'accepted': accepted,
        'rejected': rejected,
        'pending': pending,
    }
