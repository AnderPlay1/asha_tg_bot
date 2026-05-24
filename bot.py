import asyncio
import logging
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

from config import Config
import db

config = Config.from_env()

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    force=True,
)
logger = logging.getLogger(__name__)
logging.getLogger('aiogram').setLevel(logging.INFO)

bot = Bot(
    token=config.bot_token,
    default=DefaultBotProperties(parse_mode='HTML'),
)
dp = Dispatcher(storage=MemoryStorage())


tasks: list['TaskItem'] = []


@dataclass
class TaskItem:
    text: str
    sample_image: str | None = None
    sample_video: str | None = None


class TaskStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_confirmation = State()
    waiting_for_review = State()
    adding_task_text = State()
    adding_task_media_optional = State()


def is_video_file(path: str) -> bool:
    if path.startswith('tg_file_id:'):
        return True  # videos stored as Telegram file_id references
    video_extensions = ('.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv')
    return path.lower().endswith(video_extensions)


async def load_tasks() -> list[TaskItem]:
    task_path = Path(config.task_file)
    if not task_path.exists():
        task_path.touch()
        return []

    tasks_list: list[TaskItem] = []
    with task_path.open('r', encoding='utf-8') as source:
        for raw_line in source:
            line = raw_line.strip()
            if not line:
                continue

            image_path = None
            video_path = None

            if '||' in line:
                parts = [part.strip() for part in line.split('||')]
                text = parts[0]

                # Support multiple media files
                for media_path in parts[1:]:
                    if media_path:
                        if is_video_file(media_path):
                            video_path = media_path
                        else:
                            image_path = media_path
            else:
                text = line

            if not text:
                continue

            tasks_list.append(TaskItem(
                text=text,
                sample_image=image_path or None,
                sample_video=video_path or None
            ))

    return tasks_list


async def save_task_to_file(task_text: str, media_paths: list[str] | None = None) -> None:
    task_path = Path(config.task_file)

    if media_paths:
        line = f"{task_text}||" + "||" .join(media_paths)
    else:
        line = task_text

    with task_path.open('a', encoding='utf-8') as f:
        f.write(line + '\n')

    # Reload tasks in memory
    global tasks
    tasks = await load_tasks()


async def delete_task(task_index: int) -> bool:
    task_path = Path(config.task_file)

    if not task_path.exists():
        return False

    with task_path.open('r', encoding='utf-8') as f:
        lines = f.readlines()

    if task_index < 0 or task_index >= len(lines):
        return False

    del lines[task_index]

    with task_path.open('w', encoding='utf-8') as f:
        f.writelines(lines)

    global tasks
    tasks = await load_tasks()

    return True


async def initialize_database() -> None:
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    await db.init_db(config.db_path)

    today = date.today().isoformat()
    async with db.aiosqlite.connect(config.db_path) as conn:
        event_date, ended = await db.get_or_create_event_state(conn)
        if event_date != today:
            await db.set_event_state(conn, today, False)


async def schedule_event_end() -> None:
    while True:
        now = datetime.now()
        end_time = now.replace(
            hour=config.end_hour, minute=config.end_minute, second=0, microsecond=0)
        if now >= end_time:
            end_time += timedelta(days=1)

        await asyncio.sleep((end_time - now).total_seconds())
        today = date.today().isoformat()

        async with db.aiosqlite.connect(config.db_path) as conn:
            await db.set_event_state(conn, today, True)

        async with db.aiosqlite.connect(config.db_path) as conn:
            conn.row_factory = db.aiosqlite.Row
            cursor = await conn.execute('SELECT tg_id FROM users WHERE active = 1')
            active_users = await cursor.fetchall()

        for user in active_users:
            try:
                await bot.send_message(
                    user['tg_id'],
                    'Время 22:00. Фотоквест завершён, ответы больше не принимаются. Спасибо за участие!'
                )
            except Exception:
                continue


async def is_event_ended() -> bool:
    today = date.today().isoformat()
    async with db.aiosqlite.connect(config.db_path) as conn:
        event_date, ended = await db.get_or_create_event_state(conn)
    return ended and event_date == today


async def ensure_user(message: Message) -> db.aiosqlite.Row:
    async with db.aiosqlite.connect(config.db_path) as conn:
        user = await db.get_user(conn, message.from_user.id)
        is_admin = message.from_user.id == config.admin_id
        if user is None:
            user = await db.create_user(
                conn,
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                message.from_user.last_name,
                is_admin=is_admin,
            )
        elif is_admin and not user['is_admin']:
            await db.update_user_admin_status(conn, user['id'], True)
            user = await db.get_user(conn, message.from_user.id)
    return user


async def get_next_task(user_id: int) -> tuple[int, TaskItem] | None:
    async with db.aiosqlite.connect(config.db_path) as conn:
        completed = await db.get_completed_task_indexes(conn, user_id)

    remaining = [idx for idx in range(len(tasks)) if idx not in completed]
    if not remaining:
        return None
    choice = random.choice(remaining)
    return choice, tasks[choice]


def create_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text='Участники', callback_data='admin:participants')],
        [InlineKeyboardButton(text='Ожидают проверки',
                              callback_data='admin:pending')],
        [InlineKeyboardButton(
            text='Рейтинг', callback_data='admin:leaderboard')],
        [InlineKeyboardButton(text='Статистика', callback_data='admin:stats')],
        [InlineKeyboardButton(text='Добавить задание',
                              callback_data='admin:add_task')],
        [InlineKeyboardButton(text='Задания',
                              callback_data='admin:tasks')],
        [InlineKeyboardButton(text='🛑 Завершить квест',
                              callback_data='admin:end_quest')],
    ])


def create_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Да, это финальный ответ',
                              callback_data='confirm:yes')],
        [InlineKeyboardButton(text='Нет, отправлю заново',
                              callback_data='confirm:no')],
    ])


async def send_next_task(message: Message, state: FSMContext, user_id: int) -> None:
    next_task = await get_next_task(user_id)
    if not next_task:
        await state.clear()
        if not tasks:
            await message.answer('⚠️ Задания ещё не добавлены. Подожди — скоро начнём!')
        else:
            await message.answer('🎉 Все задания выполнены — спасибо за участие!')
        return

    task_index, task_item = next_task
    await state.update_data(
        task_index=task_index,
        task_text=task_item.text,
        task_image=task_item.sample_image,
        task_video=task_item.sample_video,
    )
    await state.set_state(TaskStates.waiting_for_photo)

    caption = f"Новое задание:\n\n{task_item.text}\n\nОтправь фото или видео ответ, когда будешь готов."

    # Send video if available
    if task_item.sample_video:
        if task_item.sample_video.startswith('tg_file_id:'):
            video_source = task_item.sample_video[len('tg_file_id:'):]
        else:
            sample_path = Path(task_item.sample_video)
            video_source = FSInputFile(
                sample_path) if sample_path.exists() else task_item.sample_video

        await message.answer_video(
            video=video_source,
            caption=caption,
        )
        return

    # Send photo if available
    if task_item.sample_image:
        sample_path = Path(task_item.sample_image)
        if sample_path.exists():
            photo_source = FSInputFile(sample_path)
        else:
            photo_source = task_item.sample_image

        await message.answer_photo(
            photo=photo_source,
            caption=caption,
        )
        return

    await message.answer(caption)


async def start_handler(message: Message, state: FSMContext) -> None:
    if await is_event_ended():
        await message.answer('Фотоквест завершён, ответы больше не принимаются.')
        return

    user = await ensure_user(message)
    await message.answer('Привет, СтСник. Пока мясо готовится, а в волейбол играть еще не начали, можешь пройти квест за призы от перваков 🎁\n\nЗа первое место споем персональную переделанную песню, за второе — сделаем эдит, за третье — упоминание в посте.\n\nЭтот бот будет выдавать тебе задания. Ответом на каждое задание должна быть фотка или видео. Чем больше заданий выполнишь, тем выше шанс победить 💪')
    await send_next_task(message, state, user['id'])


async def photo_handler(message: Message, state: FSMContext) -> None:
    if await is_event_ended():
        await message.answer('Фотоквест завершён, ответы больше не принимаются.')
        return

    if await state.get_state() != TaskStates.waiting_for_photo:
        await message.answer('Пожалуйста, начни с команды /start, чтобы получить задание.')
        return

    if not message.photo:
        await message.answer('Отправь, пожалуйста, фотографию или видео.')
        return

    photo = message.photo[-1]
    await state.update_data(file_id=photo.file_id, caption=message.caption or '', media_type='photo')
    await state.set_state(TaskStates.waiting_for_confirmation)
    await message.answer('Это финальный ответ?', reply_markup=create_confirmation_keyboard())


async def confirm_callback(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    if query.data == 'confirm:no':
        await state.set_state(TaskStates.waiting_for_photo)
        await query.message.answer('Хорошо, отправь новое фото или видео, когда будешь готов.')
        return

    data = await state.get_data()
    task_index = data.get('task_index')
    task_text = data.get('task_text')
    file_id = data.get('file_id')
    caption = data.get('caption')

    if task_index is None or file_id is None:
        await query.message.answer('Не удалось сохранить ответ. Начни заново командой /start.')
        await state.clear()
        return

    async with db.aiosqlite.connect(config.db_path) as conn:
        user = await db.get_user(conn, query.from_user.id)
        media_type = data.get('media_type', 'photo')
        await db.insert_submission(conn, user['id'], task_index, task_text, file_id, caption, media_type)

    await query.message.answer(
        '✅ Ответ отправлен на проверку.'
    )

    await send_next_task(
        query.message,
        state,
        user['id']
    )


async def has_admin_rights(user_id: int) -> bool:
    # Главный админ из конфига
    if user_id == config.admin_id:
        return True

    # Дополнительные админы из БД
    async with db.aiosqlite.connect(config.db_path) as conn:
        user = await db.get_user(conn, user_id)

    return bool(user and user['is_admin'])


async def admin_command(message: Message) -> None:
    if not await has_admin_rights(message.from_user.id):
        await message.answer('У вас нет прав администратора.')
        return
    await message.answer('Админ-меню:', reply_markup=create_admin_menu())


async def grant_admin(message: Message) -> None:
    if not await has_admin_rights(message.from_user.id):
        await message.answer('У вас нет прав администратора.')
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer('Использование: /grantadmin <telegram_id>')
        return

    target_tg_id = int(parts[1])
    async with db.aiosqlite.connect(config.db_path) as conn:
        target_user = await db.get_user(conn, target_tg_id)
        if not target_user:
            await message.answer('Пользователь не найден. Он должен сначала написать боту.')
            return
        await db.update_user_admin_status(conn, target_user['id'], True)

    await message.answer(f'Пользователь {target_tg_id} получил права администратора.')


def build_review_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text='Принять', callback_data=f'review:{submission_id}:accept'),
            InlineKeyboardButton(
                text='Отклонить', callback_data=f'review:{submission_id}:reject'),
        ]
    ])


def create_skip_photo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Задание без медиа',
                              callback_data='add_task:skip_photo')],
    ])


def create_done_media_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Готово',
                              callback_data='add_task:done_media')],
    ])


async def admin_callback(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    if not await has_admin_rights(query.from_user.id):
        await query.message.answer('У вас нет прав администратора.')
        return

    if query.data == 'admin:participants':
        async with db.aiosqlite.connect(config.db_path) as conn:
            participants = await db.list_participants(conn)

        if not participants:
            await query.message.answer('Пока нет участников.')
            return

        lines = []
        for participant in participants:
            name = participant['username'] or participant['first_name'] or str(
                participant['tg_id'])
            role = 'Админ' if participant['is_admin'] else 'Участник'
            lines.append(f"{name} ({participant['tg_id']}) — {role}")

        await query.message.answer('Список участников:\n' + '\n'.join(lines))
        return

    if query.data == 'admin:pending':
        async with db.aiosqlite.connect(config.db_path) as conn:
            pending = await db.list_pending_submissions(conn)

        if not pending:
            await query.message.answer('Нет заявок на проверку.')
            return

        for submission in pending:
            author = submission['username'] or submission['first_name'] or str(
                submission['tg_id'])
            caption = submission['caption'] or 'Без подписи'
            text = (
                f"ID: {submission['id']}\n"
                f"Участник: {author} ({submission['tg_id']})\n"
                f"Задание: {submission['task_text']}\n"
                f"Время: {submission['submitted_at']}\n"
                f"Комментарий: {caption}"
            )
            try:
                media_type = submission['media_type'] if submission['media_type'] else 'photo'
                if media_type == 'video':
                    await bot.send_video(query.from_user.id, video=submission['file_id'], caption=text, reply_markup=build_review_keyboard(submission['id']))
                else:
                    await bot.send_photo(query.from_user.id, photo=submission['file_id'], caption=text, reply_markup=build_review_keyboard(submission['id']))
            except Exception as e:
                await query.message.answer(f'{text}\n\n⚠️ Файл не удалось отправить: {e}', reply_markup=build_review_keyboard(submission['id']))
    if query.data == 'admin:tasks':
        if not tasks:
            await query.message.answer('Заданий пока нет.')
            return

        text = ['📋 Список заданий:\n']

        for idx, task in enumerate(tasks, start=1):
            text.append(f'{idx}. {task.text}')

        await query.message.answer(
            '\n'.join(text),
            reply_markup=build_tasks_keyboard()
        )
        return

    if query.data == 'admin:leaderboard':
        async with db.aiosqlite.connect(config.db_path) as conn:
            leaderboard = await db.get_leaderboard(conn, limit=10)

        if not leaderboard:
            await query.message.answer('🏆 Пока никто не выполнил ни одного задания с оценкой «принято».')
            return

        lines = ['🏆 <b>Рейтинг участников</b>\n']
        for idx, user in enumerate(leaderboard, 1):
            if user['username']:
                name = f"<a href='tg://user?id={user['tg_id']}'>@{user['username']}</a>"
            else:
                name = user['first_name'] or str(user['tg_id'])
            accepted = user['accepted_count']
            total = user['total_submissions']
            medal = '🥇' if idx == 1 else '🥈' if idx == 2 else '🥉' if idx == 3 else f'{idx}.'
            lines.append(
                f'{medal} {name}: <b>{accepted}</b> ✅ ({total} всего попыток)')

        await query.message.answer('\n'.join(lines), disable_web_page_preview=True)
        return

    if query.data == 'admin:stats':
        async with db.aiosqlite.connect(config.db_path) as conn:
            stats = await db.get_global_stats(conn)

        message_text = (
            f'📊 <b>Статистика фотоквеста</b>\n\n'
            f'👥 Участников: <b>{stats["participants"]}</b>\n'
            f'📸 Всего заданий выполнено: <b>{stats["total_submissions"]}</b>\n'
            f'✅ Одобрено: <b>{stats["accepted"]}</b>\n'
            f'❌ Отклонено: <b>{stats["rejected"]}</b>\n'
            f'⏳ В ожидании: <b>{stats["pending"]}</b>'
        )
        await query.message.answer(message_text)
        return

    if query.data == 'admin:add_task':
        await state.set_state(TaskStates.adding_task_text)
        await query.message.answer('Напиши текст нового задания:')
        return

    if query.data == 'admin:end_quest':
        today = date.today().isoformat()
        async with db.aiosqlite.connect(config.db_path) as conn:
            await db.set_event_state(conn, today, True)

        await query.message.answer('⏹️ Квест завершён! Участники больше не смогут отправлять ответы.')

        # Notify all active users
        async with db.aiosqlite.connect(config.db_path) as conn:
            conn.row_factory = db.aiosqlite.Row
            cursor = await conn.execute('SELECT tg_id FROM users WHERE active = 1')
            active_users = await cursor.fetchall()

        for user in active_users:
            try:
                await bot.send_message(
                    user['tg_id'],
                    '⏹️ Фотоквест завершён. Спасибо за участие!'
                )
            except Exception:
                continue
        return

    if query.data.startswith('review:'):
        _, submission_id, action = query.data.split(':', maxsplit=2)
        submission_id = int(submission_id)
        status = 'accepted' if action == 'accept' else 'rejected'

        async with db.aiosqlite.connect(config.db_path) as conn:
            await db.update_submission_status(conn, submission_id, status, query.from_user.id)
            submission = await db.get_submission(conn, submission_id)

        await query.message.answer(f'Заявка #{submission_id} отмечена как {status}.')
        user_tg_id = submission['tg_id']
        if status == 'accepted':
            await bot.send_message(
                user_tg_id,
                f"✅ Задание \"{submission['task_text']}\" принято!"
            )
        else:
            await bot.send_message(
                user_tg_id,
                f"❌ Задание \"{submission['task_text']}\" отклонено."
            )


@dp.message(Command('start'))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await start_handler(message, state)


@dp.message(Command('admin'))
async def cmd_admin(message: Message) -> None:
    await admin_command(message)


@dp.message(Command('grantadmin'))
async def cmd_grantadmin(message: Message) -> None:
    await grant_admin(message)


@dp.message(Command('cancel'))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer('Текущее действие отменено.')


@dp.message(F.text)
async def cmd_text(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()

    # Handle task text input during task addition
    if current_state == TaskStates.adding_task_text:
        task_text = message.text
        await state.update_data(task_text=task_text)
        await state.set_state(TaskStates.adding_task_media_optional)
        await message.answer(
            'Отправь фото или видео для этого задания, или выбери "Задание без медиа":',
            reply_markup=create_skip_photo_keyboard()
        )
        return

    # Default response for other text messages
    await message.answer('Для взаимодействия с ботом используй команду /start или /admin')


@dp.message(F.photo)
async def cmd_photo(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()

    # Handle photo during task addition
    if current_state == TaskStates.adding_task_media_optional:
        state_data = await state.get_data()
        task_text = state_data.get('task_text')
        media_paths = state_data.get('media_paths', [])

        if not message.photo:
            await message.answer('Отправь фотографию.')
            return

        # Save photo to photos directory
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_filename = f'photos/task_{datetime.now().timestamp()}.jpg'
        photo_path = Path(photo_filename)

        # Ensure photos directory exists
        photo_path.parent.mkdir(parents=True, exist_ok=True)

        # Download and save photo
        await bot.download_file(file_info.file_path, str(photo_path))

        media_paths.append(photo_filename)
        await state.update_data(media_paths=media_paths)

        await message.answer('✅ Фото добавлено! Отправь видео (опционально) или нажми "Готово":', reply_markup=create_done_media_keyboard())
        return

    await photo_handler(message, state)


@dp.message(F.video)
async def cmd_video(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()

    # Handle video during task addition
    if current_state == TaskStates.adding_task_media_optional:
        state_data = await state.get_data()
        task_text = state_data.get('task_text')
        media_paths = state_data.get('media_paths', [])

        if not message.video:
            await message.answer('Отправь видео.')
            return

        # For videos, store file_id directly (Bot API limits downloads to 20MB).
        # Telegram keeps the file and we can resend it by file_id at any time.
        video = message.video
        file_id_ref = f'tg_file_id:{video.file_id}'

        media_paths.append(file_id_ref)
        await state.update_data(media_paths=media_paths)

        await message.answer('✅ Видео добавлено!', reply_markup=create_done_media_keyboard())
        return

    if current_state != TaskStates.waiting_for_photo:
        await message.answer('Пожалуйста, начни с команды /start, чтобы получить задание.')
        return

    if await is_event_ended():
        await message.answer('Фотоквест завершён, ответы больше не принимаются.')
        return

    if not message.video:
        await message.answer('Отправь видео.')
        return

    video = message.video
    await state.update_data(file_id=video.file_id, caption=message.caption or '', media_type='video')
    await state.set_state(TaskStates.waiting_for_confirmation)
    await message.answer('Это финальный ответ?', reply_markup=create_confirmation_keyboard())


@dp.callback_query()
async def cmd_callback(query: CallbackQuery, state: FSMContext) -> None:
    data = query.data or ''
    if data.startswith('confirm:'):
        await confirm_callback(query, state)
        return

    if data == 'add_task:skip_photo':
        state_data = await state.get_data()
        task_text = state_data.get('task_text')
        media_paths = state_data.get('media_paths', []) or []
        if task_text:
            await save_task_to_file(task_text, media_paths if media_paths else None)
            media_label = 'медиа' if media_paths else 'фото'
            await query.message.answer(f'✅ Задание добавлено (без {media_label})!')
            await query.message.answer('Админ-меню:', reply_markup=create_admin_menu())
            await state.clear()
        return

    if data == 'add_task:done_media':
        state_data = await state.get_data()
        task_text = state_data.get('task_text')
        media_paths = state_data.get('media_paths', []) or []
        if task_text:
            await save_task_to_file(task_text, media_paths if media_paths else None)
            media_label = 'с медиа' if media_paths else 'без медиа'
            await query.message.answer(f'✅ Задание добавлено {media_label}!')
            await query.message.answer('Админ-меню:', reply_markup=create_admin_menu())
            await state.clear()
        return

    if data.startswith('task_delete:'):
        task_index = int(data.split(':')[1])

        if await delete_task(task_index):
            await query.answer('Удалено')
            await query.message.answer(
                '✅ Задание удалено',
                reply_markup=create_admin_menu()
            )
        else:
            await query.answer('Ошибка')

        return

    if data.startswith('admin:') or data.startswith('review:'):
        await admin_callback(query, state)
        return


def build_tasks_keyboard() -> InlineKeyboardMarkup:
    keyboard = []

    for idx, task in enumerate(tasks):
        keyboard.append([
            InlineKeyboardButton(
                text=f'Удалить #{idx + 1}',
                callback_data=f'task_delete:{idx}'
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text='⬅ Назад',
            callback_data='admin:back'
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def on_startup() -> None:
    logger.info('Initializing database and loading tasks...')
    await initialize_database()
    global tasks
    tasks = await load_tasks()
    logger.info('Loaded %d task(s) from %s', len(tasks), config.task_file)
    asyncio.create_task(schedule_event_end())


if __name__ == '__main__':
    dp.startup.register(on_startup)
    logger.info('Starting Telegram bot polling...')
    dp.run_polling(bot, skip_updates=True)
