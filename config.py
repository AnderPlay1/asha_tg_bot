import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str
    admin_id: int
    db_path: str
    task_file: str
    end_hour: int
    end_minute: int

    @classmethod
    def from_env(cls):
        bot_token = os.getenv('BOT_TOKEN')
        admin_id = os.getenv('ADMIN_ID')
        if not bot_token or not admin_id:
            raise ValueError(
                'BOT_TOKEN and ADMIN_ID must be set in environment')

        return cls(
            bot_token=bot_token.strip(),
            admin_id=int(admin_id.strip()),
            db_path=os.getenv('DB_PATH', 'data/bot.db'),
            task_file=os.getenv('TASK_FILE', 'tasks.txt'),
            end_hour=int(os.getenv('EVENT_END_HOUR', '22')),
            end_minute=int(os.getenv('EVENT_END_MINUTE', '0')),
        )
