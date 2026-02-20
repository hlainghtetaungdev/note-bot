import csv
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Dict, Optional, Tuple

import telebot
from telebot.types import KeyboardButton, ReplyKeyboardMarkup

ENV_PATH = Path(__file__).with_name('.env')
DEFAULT_CATEGORY = 'other'
BROADCAST_TTL_MINUTES = 10
WIPE_TTL_MINUTES = 10
MENU_OPTIONS = [
    '💰 လက်ကျန်ငွေ',
    '📝 ဘတ်ဂျက် သတ်မှတ်မယ်',
    '📅 ၇ ရက်စာ',
    '📊 ဒီလစာရင်း',
    '⏪ အရင်လစာရင်း',
    '🗂 အုပ်စုစာရင်း',
]
CATEGORY_TAG_PATTERN = re.compile(r'#([A-Za-z0-9_]{1,24})')
PERIOD_PRESETS = {
    '7d': {
        'filter': "date >= datetime('now', '-7 days')",
        'title': '📅 ပြီးခဲ့သော ၇ ရက်စာ အသုံးစရိတ်',
        'label': '7d',
    },
    'month': {
        'filter': "strftime('%Y-%m', date) = strftime('%Y-%m', 'now')",
        'title': '📊 ယခုလ အသုံးစရိတ်',
        'label': 'month',
    },
    'last_month': {
        'filter': "strftime('%Y-%m', date) = strftime('%Y-%m', 'now', '-1 month')",
        'title': '⏪ ယခင်လ အသုံးစရိတ်',
        'label': 'last_month',
    },
    'all': {
        'filter': '1=1',
        'title': '🧾 အသုံးစရိတ်အားလုံး',
        'label': 'all',
    },
}
BUTTON_PERIODS = {
    '📅 ၇ ရက်စာ': '7d',
    '📊 ဒီလစာရင်း': 'month',
    '⏪ အရင်လစာရင်း': 'last_month',
}

pending_broadcasts: Dict[int, Dict[str, object]] = {}
pending_wipes: Dict[int, datetime] = {}


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ENV_PATH)

TOKEN = os.getenv('BOT_TOKEN', '').strip()
DB_PATH = os.getenv('DB_PATH', 'expense_tracker.db')
ADMIN_IDS = {
    int(raw_id.strip())
    for raw_id in os.getenv('ADMIN_IDS', '').split(',')
    if raw_id.strip().isdigit()
}

if not TOKEN or TOKEN == 'PASTE_NEW_TOKEN_FROM_BOTFATHER':
    raise RuntimeError('BOT_TOKEN is missing. Put a NEW token in .env file.')

bot = telebot.TeleBot(TOKEN)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = Lock()


def db_execute(
    query,
    params=(),
    *,
    fetchone=False,
    fetchall=False,
    commit=False,
    lastrowid=False,
):
    with db_lock:
        cursor = conn.cursor()
        cursor.execute(query, params)

        one_row = cursor.fetchone() if fetchone else None
        all_rows = cursor.fetchall() if fetchall else None
        row_id = cursor.lastrowid if lastrowid else None

        if commit:
            conn.commit()

    if fetchone:
        return one_row
    if fetchall:
        return all_rows
    if lastrowid:
        return row_id
    return None


def column_exists(table_name: str, column_name: str) -> bool:
    rows = db_execute(f'PRAGMA table_info({table_name})', fetchall=True)
    return any(row['name'] == column_name for row in rows)


def ensure_column_exists(table_name: str, column_definition: str) -> None:
    column_name = column_definition.split()[0]
    if column_exists(table_name, column_name):
        return

    db_execute(f'ALTER TABLE {table_name} ADD COLUMN {column_definition}', commit=True)


def init_db() -> None:
    db_execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            budget REAL DEFAULT 0,
            initial_budget REAL DEFAULT 0,
            alert_80_sent INTEGER DEFAULT 0,
            alert_100_sent INTEGER DEFAULT 0
        )
        ''',
        commit=True,
    )

    db_execute(
        '''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            description TEXT,
            category TEXT DEFAULT 'other',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''',
        commit=True,
    )

    ensure_column_exists('users', 'initial_budget REAL DEFAULT 0')
    ensure_column_exists('users', 'alert_80_sent INTEGER DEFAULT 0')
    ensure_column_exists('users', 'alert_100_sent INTEGER DEFAULT 0')
    ensure_column_exists('expenses', "category TEXT DEFAULT 'other'")

    db_execute(
        'CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)',
        commit=True,
    )

    db_execute(
        '''
        UPDATE users
        SET initial_budget = budget
        WHERE initial_budget IS NULL OR initial_budget <= 0
        ''',
        commit=True,
    )
    db_execute(
        '''
        UPDATE users
        SET alert_80_sent = COALESCE(alert_80_sent, 0),
            alert_100_sent = COALESCE(alert_100_sent, 0)
        ''',
        commit=True,
    )
    db_execute(
        '''
        UPDATE expenses
        SET category = ?
        WHERE category IS NULL OR TRIM(category) = ''
        ''',
        (DEFAULT_CATEGORY,),
        commit=True,
    )


init_db()

def send_text(chat_id: int, text: str, **kwargs):
    return bot.send_message(chat_id, text, **kwargs)


def normalize_category(raw_category: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9_\-]', '', raw_category.strip().lower())
    if not cleaned:
        return DEFAULT_CATEGORY
    return cleaned[:24]


def split_description_and_category(raw_description: str) -> Tuple[str, str]:
    category_match = CATEGORY_TAG_PATTERN.search(raw_description)
    if not category_match:
        return raw_description.strip(), DEFAULT_CATEGORY

    category = normalize_category(category_match.group(1))
    description = CATEGORY_TAG_PATTERN.sub('', raw_description).strip()
    if not description:
        description = 'အသုံးစရိတ်'

    return description, category


def parse_expense_text(raw_text: str) -> Optional[Tuple[str, float, str]]:
    text = (raw_text or '').strip()
    match = re.match(r'(.+?)\s+([\d,]+(?:\.\d+)?)$', text)
    if not match:
        return None

    description_part = match.group(1).strip()
    amount_str = match.group(2).replace(',', '')

    try:
        amount = float(amount_str)
    except ValueError:
        return None

    if amount <= 0:
        return None

    description, category = split_description_and_category(description_part)
    if not description:
        description = 'အသုံးစရိတ်'

    return description, amount, category


def parse_period_arg(raw_arg: Optional[str], allow_all: bool = True) -> Optional[str]:
    if not raw_arg:
        return 'month'

    normalized = raw_arg.strip().lower().replace('-', '_')
    alias_map = {
        '7d': '7d',
        '7days': '7d',
        'week': '7d',
        'month': 'month',
        'this_month': 'month',
        'current_month': 'month',
        'last_month': 'last_month',
        'prev_month': 'last_month',
        'previous_month': 'last_month',
        'all': 'all',
    }

    parsed = alias_map.get(normalized)
    if parsed == 'all' and not allow_all:
        return None

    return parsed


def get_main_menu() -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton('💰 လက်ကျန်ငွေ'),
        KeyboardButton('📝 ဘတ်ဂျက် သတ်မှတ်မယ်'),
    )
    markup.add(
        KeyboardButton('📅 ၇ ရက်စာ'),
        KeyboardButton('📊 ဒီလစာရင်း'),
        KeyboardButton('⏪ အရင်လစာရင်း'),
    )
    markup.add(KeyboardButton('🗂 အုပ်စုစာရင်း'))
    return markup


def ensure_user_exists(user_id: int) -> None:
    db_execute(
        '''
        INSERT OR IGNORE INTO users (
            user_id,
            budget,
            initial_budget,
            alert_80_sent,
            alert_100_sent
        )
        VALUES (?, 0, 0, 0, 0)
        ''',
        (user_id,),
        commit=True,
    )


def get_user_profile(user_id: int):
    return db_execute(
        '''
        SELECT budget, initial_budget, alert_80_sent, alert_100_sent
        FROM users
        WHERE user_id = ?
        ''',
        (user_id,),
        fetchone=True,
    )


def get_user_budget(user_id: int) -> float:
    row = get_user_profile(user_id)
    return float(row['budget']) if row else 0.0


def get_user_initial_budget(user_id: int) -> float:
    row = get_user_profile(user_id)
    return float(row['initial_budget']) if row else 0.0


def set_user_budget(user_id: int, amount: float) -> None:
    db_execute(
        '''
        INSERT INTO users (user_id, budget, initial_budget, alert_80_sent, alert_100_sent)
        VALUES (?, ?, ?, 0, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            budget = excluded.budget,
            initial_budget = excluded.initial_budget,
            alert_80_sent = 0,
            alert_100_sent = 0
        ''',
        (user_id, amount, amount),
        commit=True,
    )


def update_user_remaining_budget(user_id: int, amount: float) -> None:
    db_execute(
        'UPDATE users SET budget = ? WHERE user_id = ?',
        (amount, user_id),
        commit=True,
    )


def update_alert_flags(user_id: int, alert_80_sent: int, alert_100_sent: int) -> None:
    db_execute(
        '''
        UPDATE users
        SET alert_80_sent = ?, alert_100_sent = ?
        WHERE user_id = ?
        ''',
        (alert_80_sent, alert_100_sent, user_id),
        commit=True,
    )


def deduct_budget(user_id: int, amount: float) -> float:
    current_budget = get_user_budget(user_id)
    new_budget = current_budget - amount
    update_user_remaining_budget(user_id, new_budget)
    return new_budget


def refund_budget(user_id: int, amount: float) -> float:
    current_budget = get_user_budget(user_id)
    new_budget = current_budget + amount
    update_user_remaining_budget(user_id, new_budget)
    return new_budget


def maybe_send_budget_alert(user_id: int, chat_id: int) -> None:
    profile = get_user_profile(user_id)
    if not profile:
        return

    initial_budget = float(profile['initial_budget'] or 0)
    remaining_budget = float(profile['budget'] or 0)
    alert_80_sent = int(profile['alert_80_sent'] or 0)
    alert_100_sent = int(profile['alert_100_sent'] or 0)

    if initial_budget <= 0:
        return

    spent_ratio = (initial_budget - remaining_budget) / initial_budget

    if spent_ratio >= 1:
        if not alert_100_sent:
            send_text(
                chat_id,
                (
                    f'🚨 Budget 100% ကျော်သွားပါပြီ။\n'
                    f'သတ်မှတ်: {initial_budget:,.0f} ကျပ်\n'
                    f'လက်ကျန်: {remaining_budget:,.0f} ကျပ်'
                ),
            )
        update_alert_flags(user_id, 1, 1)
        return

    if spent_ratio >= 0.8:
        if not alert_80_sent:
            send_text(
                chat_id,
                (
                    f'⚠️ Budget 80% သုံးပြီးပါပြီ။\n'
                    f'သတ်မှတ်: {initial_budget:,.0f} ကျပ်\n'
                    f'လက်ကျန်: {remaining_budget:,.0f} ကျပ်'
                ),
            )
        update_alert_flags(user_id, 1, 0)
        return

    if alert_80_sent or alert_100_sent:
        update_alert_flags(user_id, 0, 0)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_all_user_ids() -> list:
    rows = db_execute(
        '''
        SELECT DISTINCT user_id
        FROM (
            SELECT user_id FROM users
            UNION ALL
            SELECT user_id FROM expenses
        )
        WHERE user_id IS NOT NULL
        ''',
        fetchall=True,
    )
    return [int(row['user_id']) for row in rows]


def format_db_datetime(raw_value: str) -> str:
    if not raw_value:
        return '-'

    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(raw_value, fmt).strftime('%d-%m %H:%M')
        except ValueError:
            continue

    return str(raw_value)


def get_period_preset(period_key: str):
    return PERIOD_PRESETS.get(period_key, PERIOD_PRESETS['month'])


def send_period_report(chat_id: int, user_id: int, period_key: str) -> None:
    preset = get_period_preset(period_key)
    time_filter = preset['filter']
    title = preset['title']

    total_row = db_execute(
        f'SELECT COALESCE(SUM(amount), 0) AS total_spent FROM expenses WHERE user_id = ? AND {time_filter}',
        (user_id,),
        fetchone=True,
    )
    records = db_execute(
        f'''
        SELECT id, amount, description, category, date
        FROM expenses
        WHERE user_id = ? AND {time_filter}
        ORDER BY date DESC
        LIMIT 10
        ''',
        (user_id,),
        fetchall=True,
    )

    lines = [
        title,
        '',
        f"စုစုပေါင်းသုံးစွဲငွေ: {float(total_row['total_spent']):,.0f} ကျပ်",
        '',
        '-- နောက်ဆုံးမှတ်တမ်းများ --',
    ]

    if not records:
        lines.append('မှတ်တမ်းမရှိသေးပါ။')
    else:
        for row in records:
            date_str = format_db_datetime(row['date'])
            lines.append(
                f"#{row['id']} | {date_str} | {float(row['amount']):,.0f} | {row['category']} | {row['description']}"
            )

    send_text(chat_id, '\n'.join(lines))


def send_category_report(chat_id: int, user_id: int, period_key: str) -> None:
    preset = get_period_preset(period_key)
    time_filter = preset['filter']

    rows = db_execute(
        f'''
        SELECT category, COUNT(*) AS tx_count, COALESCE(SUM(amount), 0) AS total_spent
        FROM expenses
        WHERE user_id = ? AND {time_filter}
        GROUP BY category
        ORDER BY total_spent DESC
        ''',
        (user_id,),
        fetchall=True,
    )

    if not rows:
        send_text(chat_id, 'ဒီ period အတွင်း category မှတ်တမ်း မရှိသေးပါ။')
        return

    lines = [f"🗂 Category Report ({preset['label']})", '']
    for row in rows:
        lines.append(
            f"• {row['category']}: {float(row['total_spent']):,.0f} ကျပ် ({int(row['tx_count'])} ခု)"
        )

    send_text(chat_id, '\n'.join(lines))


def get_last_expense(user_id: int):
    return db_execute(
        '''
        SELECT id, amount, description, category, date
        FROM expenses
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        ''',
        (user_id,),
        fetchone=True,
    )


def get_expense_by_id(user_id: int, expense_id: int):
    return db_execute(
        '''
        SELECT id, amount, description, category, date
        FROM expenses
        WHERE user_id = ? AND id = ?
        ''',
        (user_id, expense_id),
        fetchone=True,
    )


def insert_expense(user_id: int, amount: float, description: str, category: str) -> int:
    return int(
        db_execute(
            'INSERT INTO expenses (user_id, amount, description, category) VALUES (?, ?, ?, ?)',
            (user_id, amount, description, category),
            commit=True,
            lastrowid=True,
        )
    )


def delete_expense(user_id: int, expense_id: int) -> None:
    db_execute(
        'DELETE FROM expenses WHERE user_id = ? AND id = ?',
        (user_id, expense_id),
        commit=True,
    )


def update_expense(user_id: int, expense_id: int, amount: float, description: str, category: str) -> None:
    db_execute(
        '''
        UPDATE expenses
        SET amount = ?, description = ?, category = ?
        WHERE user_id = ? AND id = ?
        ''',
        (amount, description, category, user_id, expense_id),
        commit=True,
    )


def send_history(chat_id: int, user_id: int, limit: int = 10) -> None:
    limit = max(1, min(limit, 30))
    rows = db_execute(
        '''
        SELECT id, amount, description, category, date
        FROM expenses
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        ''',
        (user_id, limit),
        fetchall=True,
    )

    if not rows:
        send_text(chat_id, 'မှတ်တမ်းမရှိသေးပါ။')
        return

    lines = [f'🧾 နောက်ဆုံး {len(rows)} ခု']
    for row in rows:
        lines.append(
            f"#{row['id']} | {format_db_datetime(row['date'])} | {float(row['amount']):,.0f} | {row['category']} | {row['description']}"
        )

    send_text(chat_id, '\n'.join(lines))


def send_user_profile(chat_id: int, user_id: int) -> None:
    ensure_user_exists(user_id)
    profile = get_user_profile(user_id)
    stats = db_execute(
        '''
        SELECT COUNT(*) AS tx_count, COALESCE(SUM(amount), 0) AS total_spent
        FROM expenses
        WHERE user_id = ?
        ''',
        (user_id,),
        fetchone=True,
    )

    budget = float(profile['budget'] or 0)
    initial_budget = float(profile['initial_budget'] or 0)
    spent = max(0.0, initial_budget - budget)
    usage = (spent / initial_budget * 100) if initial_budget > 0 else 0

    text = (
        '👤 My Profile\n\n'
        f'ID: {user_id}\n'
        f'Current Budget: {budget:,.0f} ကျပ်\n'
        f'Monthly Budget: {initial_budget:,.0f} ကျပ်\n'
        f'Usage: {usage:.1f}%\n'
        f"Transactions: {int(stats['tx_count'])}\n"
        f"Total Spent: {float(stats['total_spent']):,.0f} ကျပ်"
    )
    send_text(chat_id, text)


def parse_export_args(message_text: str) -> Optional[str]:
    _, _, arg = message_text.partition(' ')
    arg = arg.strip()
    if not arg:
        return 'month'

    return parse_period_arg(arg, allow_all=True)


def export_expenses_csv(chat_id: int, user_id: int, period_key: str) -> None:
    preset = get_period_preset(period_key)
    time_filter = preset['filter']

    rows = db_execute(
        f'''
        SELECT id, date, amount, description, category
        FROM expenses
        WHERE user_id = ? AND {time_filter}
        ORDER BY date DESC
        ''',
        (user_id,),
        fetchall=True,
    )

    if not rows:
        send_text(chat_id, 'Export လုပ်ရန် data မရှိသေးပါ။')
        return

    csv_buffer = io.StringIO(newline='')
    writer = csv.writer(csv_buffer)
    writer.writerow(['id', 'date', 'amount', 'category', 'description'])
    for row in rows:
        writer.writerow(
            [
                int(row['id']),
                row['date'],
                float(row['amount']),
                row['category'],
                row['description'],
            ]
        )

    payload = io.BytesIO(csv_buffer.getvalue().encode('utf-8-sig'))
    payload.name = f"expenses_{period_key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    payload.seek(0)

    bot.send_document(chat_id, payload, caption=f"Export complete ({period_key})")

def build_help_text(is_admin_user: bool) -> str:
    lines = [
        'အသုံးပြုပုံများ',
        '',
        '1) အသုံးစရိတ်မှတ်ရန်: မုန့်ဖိုး #food 5000',
        '2) Budget သတ်မှတ်: 📝 ဘတ်ဂျက် သတ်မှတ်မယ်',
        '3) Report: 📅 / 📊 / ⏪ buttons',
        '',
        'User Commands',
        '/menu - main menu',
        '/history [10] - နောက်ဆုံးမှတ်တမ်း',
        '/undo - နောက်ဆုံး expense ဖျက်',
        '/delete <id> - id နဲ့ဖျက်',
        '/edit <id> <description amount> - ပြင်',
        '/category_report [7d|month|last_month|all] - category summary',
        '/export [7d|month|last_month|all] - CSV export',
        '/me - ကိုယ့် data summary',
        '/wipe_my_data - ကိုယ့် data ဖျက်',
        '/cancel - pending step/confirm ရပ်',
        '/myid - telegram user id',
    ]

    if is_admin_user:
        lines.extend(
            [
                '',
                'Admin Commands',
                '/stats - overall bot stats',
                '/broadcast <message> - prepare broadcast',
                '/broadcast_confirm - broadcast send confirm',
                '/broadcast_cancel - broadcast cancel',
            ]
        )

    return '\n'.join(lines)


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)
    text = build_help_text(is_admin(user_id))
    send_text(message.chat.id, text, reply_markup=get_main_menu())


@bot.message_handler(commands=['menu'])
def show_menu(message):
    send_text(message.chat.id, 'Menu ကို ပြန်ဖွင့်လိုက်ပါပြီ။', reply_markup=get_main_menu())


@bot.message_handler(commands=['myid'])
def show_my_id(message):
    send_text(message.chat.id, f'Your Telegram ID: {message.from_user.id}')


@bot.message_handler(commands=['cancel'])
def cancel_step(message):
    user_id = message.from_user.id
    bot.clear_step_handler_by_chat_id(message.chat.id)
    pending_broadcasts.pop(user_id, None)
    pending_wipes.pop(user_id, None)
    send_text(message.chat.id, 'လုပ်ဆောင်နေသော step/confirm ကို ရပ်လိုက်ပါပြီ။', reply_markup=get_main_menu())


@bot.message_handler(commands=['stats'])
def admin_stats(message):
    if not is_admin(message.from_user.id):
        send_text(message.chat.id, '⛔ ဒီ command ကို admin သာ သုံးနိုင်ပါတယ်။')
        return

    users_row = db_execute(
        '''
        SELECT COUNT(*) AS total_users
        FROM (
            SELECT user_id FROM users
            UNION
            SELECT user_id FROM expenses
        )
        ''',
        fetchone=True,
    )
    expenses_row = db_execute(
        'SELECT COUNT(*) AS total_records, COALESCE(SUM(amount), 0) AS total_spent FROM expenses',
        fetchone=True,
    )
    today_row = db_execute(
        '''
        SELECT COUNT(*) AS today_records, COALESCE(SUM(amount), 0) AS today_spent
        FROM expenses
        WHERE date(date, 'localtime') = date('now', 'localtime')
        ''',
        fetchone=True,
    )

    text = (
        'Admin Stats\n\n'
        f"👥 Users: {int(users_row['total_users'])}\n"
        f"🧾 Records: {int(expenses_row['total_records'])}\n"
        f"💸 Total Spent: {float(expenses_row['total_spent']):,.0f} ကျပ်\n"
        f"📅 Today Records: {int(today_row['today_records'])}\n"
        f"📌 Today Spent: {float(today_row['today_spent']):,.0f} ကျပ်"
    )
    send_text(message.chat.id, text)


@bot.message_handler(commands=['broadcast'])
def admin_broadcast(message):
    if not is_admin(message.from_user.id):
        send_text(message.chat.id, '⛔ ဒီ command ကို admin သာ သုံးနိုင်ပါတယ်။')
        return

    _, _, payload = message.text.partition(' ')
    payload = payload.strip()

    if not payload:
        send_text(message.chat.id, 'အသုံးပြုပုံ: /broadcast announce message')
        return

    recipients = get_all_user_ids()
    if not recipients:
        send_text(message.chat.id, 'Broadcast ပို့ရန် user မရှိသေးပါ။')
        return

    pending_broadcasts[message.from_user.id] = {
        'payload': payload,
        'expires_at': datetime.utcnow() + timedelta(minutes=BROADCAST_TTL_MINUTES),
    }

    send_text(
        message.chat.id,
        (
            'Broadcast draft save လုပ်ပြီးပါပြီ။\n'
            f'Target users: {len(recipients)}\n\n'
            f'Message:\n{payload}\n\n'
            'Confirm: /broadcast_confirm\n'
            'Cancel: /broadcast_cancel\n'
            f'({BROADCAST_TTL_MINUTES} minutes အတွင်း confirm လုပ်ပါ)'
        ),
    )


@bot.message_handler(commands=['broadcast_confirm'])
def admin_broadcast_confirm(message):
    if not is_admin(message.from_user.id):
        send_text(message.chat.id, '⛔ ဒီ command ကို admin သာ သုံးနိုင်ပါတယ်။')
        return

    pending = pending_broadcasts.get(message.from_user.id)
    if not pending:
        send_text(message.chat.id, 'Confirm လုပ်ရန် pending broadcast မရှိပါ။')
        return

    expires_at = pending['expires_at']
    if datetime.utcnow() > expires_at:
        pending_broadcasts.pop(message.from_user.id, None)
        send_text(message.chat.id, 'Pending broadcast သက်တမ်းကုန်သွားပါပြီ။ /broadcast ဖြင့် ပြန်စပါ။')
        return

    payload = str(pending['payload'])
    recipients = get_all_user_ids()

    sent = 0
    failed = 0
    for user_id in recipients:
        try:
            send_text(user_id, f'📢 {payload}')
            sent += 1
        except Exception:
            failed += 1

    pending_broadcasts.pop(message.from_user.id, None)
    send_text(
        message.chat.id,
        f'Broadcast complete. Sent: {sent}, Failed: {failed}, Total: {len(recipients)}',
    )


@bot.message_handler(commands=['broadcast_cancel'])
def admin_broadcast_cancel(message):
    if not is_admin(message.from_user.id):
        send_text(message.chat.id, '⛔ ဒီ command ကို admin သာ သုံးနိုင်ပါတယ်။')
        return

    if pending_broadcasts.pop(message.from_user.id, None):
        send_text(message.chat.id, 'Pending broadcast ကို cancel လုပ်ပြီးပါပြီ။')
    else:
        send_text(message.chat.id, 'Pending broadcast မရှိပါ။')


@bot.message_handler(commands=['history'])
def command_history(message):
    ensure_user_exists(message.from_user.id)

    _, _, arg = message.text.partition(' ')
    arg = arg.strip()
    limit = 10

    if arg:
        if not arg.isdigit():
            send_text(message.chat.id, 'အသုံးပြုပုံ: /history 10')
            return
        limit = int(arg)

    send_history(message.chat.id, message.from_user.id, limit)

@bot.message_handler(commands=['undo', 'delete_last'])
def command_undo(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)

    last_expense = get_last_expense(user_id)
    if not last_expense:
        send_text(message.chat.id, 'ဖျက်ရန် နောက်ဆုံး expense မရှိသေးပါ။')
        return

    expense_id = int(last_expense['id'])
    amount = float(last_expense['amount'])
    delete_expense(user_id, expense_id)
    remaining = refund_budget(user_id, amount)
    maybe_send_budget_alert(user_id, message.chat.id)

    send_text(
        message.chat.id,
        (
            '✅ Undo complete\n'
            f'ဖျက်လိုက်သော record: #{expense_id}\n'
            f'Amount: {amount:,.0f} ကျပ်\n'
            f'လက်ကျန်ငွေ: {remaining:,.0f} ကျပ်'
        ),
    )


@bot.message_handler(commands=['delete'])
def command_delete(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)

    _, _, arg = message.text.partition(' ')
    arg = arg.strip()

    if not arg or not arg.isdigit():
        send_text(message.chat.id, 'အသုံးပြုပုံ: /delete <id>')
        return

    expense_id = int(arg)
    record = get_expense_by_id(user_id, expense_id)
    if not record:
        send_text(message.chat.id, 'အဆိုပါ id မတွေ့ပါ။ /history ကိုစစ်ပါ။')
        return

    amount = float(record['amount'])
    delete_expense(user_id, expense_id)
    remaining = refund_budget(user_id, amount)
    maybe_send_budget_alert(user_id, message.chat.id)

    send_text(
        message.chat.id,
        (
            f'✅ Record #{expense_id} ဖျက်ပြီးပါပြီ။\n'
            f'လက်ကျန်ငွေ: {remaining:,.0f} ကျပ်'
        ),
    )


@bot.message_handler(commands=['edit'])
def command_edit(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)

    match = re.match(r'^/edit\s+(\d+)\s+(.+)$', message.text.strip())
    if not match:
        send_text(message.chat.id, 'အသုံးပြုပုံ: /edit <id> <description amount>\nဥပမာ: /edit 12 Grab #transport 4500')
        return

    expense_id = int(match.group(1))
    new_payload = match.group(2).strip()

    record = get_expense_by_id(user_id, expense_id)
    if not record:
        send_text(message.chat.id, 'အဆိုပါ id မတွေ့ပါ။ /history ကိုစစ်ပါ။')
        return

    parsed = parse_expense_text(new_payload)
    if not parsed:
        send_text(message.chat.id, "format မှားနေပါတယ်။ ဥပမာ: /edit 12 မုန့်ဖိုး #food 5000")
        return

    new_description, new_amount, new_category = parsed
    old_amount = float(record['amount'])

    update_expense(user_id, expense_id, new_amount, new_description, new_category)

    if new_amount > old_amount:
        remaining = deduct_budget(user_id, new_amount - old_amount)
    elif new_amount < old_amount:
        remaining = refund_budget(user_id, old_amount - new_amount)
    else:
        remaining = get_user_budget(user_id)

    maybe_send_budget_alert(user_id, message.chat.id)

    send_text(
        message.chat.id,
        (
            f'✅ Record #{expense_id} ပြင်ပြီးပါပြီ။\n'
            f'Old: {old_amount:,.0f} ကျပ်\n'
            f'New: {new_amount:,.0f} ကျပ် ({new_category})\n'
            f'လက်ကျန်ငွေ: {remaining:,.0f} ကျပ်'
        ),
    )


@bot.message_handler(commands=['category_report'])
def command_category_report(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)

    _, _, arg = message.text.partition(' ')
    period_key = parse_period_arg(arg.strip() if arg else None, allow_all=True)
    if not period_key:
        send_text(message.chat.id, 'အသုံးပြုပုံ: /category_report [7d|month|last_month|all]')
        return

    send_category_report(message.chat.id, user_id, period_key)


@bot.message_handler(commands=['export'])
def command_export(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)

    period_key = parse_export_args(message.text)
    if not period_key:
        send_text(message.chat.id, 'အသုံးပြုပုံ: /export [7d|month|last_month|all]')
        return

    export_expenses_csv(message.chat.id, user_id, period_key)


@bot.message_handler(commands=['me'])
def command_me(message):
    send_user_profile(message.chat.id, message.from_user.id)


@bot.message_handler(commands=['wipe_my_data'])
def command_wipe_my_data(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)

    pending_wipes[user_id] = datetime.utcnow() + timedelta(minutes=WIPE_TTL_MINUTES)
    send_text(
        message.chat.id,
        (
            '⚠️ ဒီလုပ်ဆောင်ချက်က expense history + budget data ကို ဖျက်ပါမယ်။\n'
            'Confirm လုပ်ရန်: /confirm_wipe\n'
            'Cancel လုပ်ရန်: /cancel_wipe\n'
            f'({WIPE_TTL_MINUTES} minutes အတွင်း confirm လုပ်ပါ)'
        ),
    )


@bot.message_handler(commands=['confirm_wipe'])
def command_confirm_wipe(message):
    user_id = message.from_user.id
    expiry = pending_wipes.get(user_id)

    if not expiry:
        send_text(message.chat.id, 'Confirm လုပ်ရန် pending wipe request မရှိပါ။')
        return

    if datetime.utcnow() > expiry:
        pending_wipes.pop(user_id, None)
        send_text(message.chat.id, 'Wipe request သက်တမ်းကုန်သွားပါပြီ။ /wipe_my_data ကိုပြန်စပါ။')
        return

    db_execute('DELETE FROM expenses WHERE user_id = ?', (user_id,), commit=True)
    db_execute('DELETE FROM users WHERE user_id = ?', (user_id,), commit=True)
    pending_wipes.pop(user_id, None)
    bot.clear_step_handler_by_chat_id(message.chat.id)

    send_text(message.chat.id, '✅ သင့် data ကို ဖျက်ပြီးပါပြီ။ ပြန်စရန် /start ကိုနှိပ်ပါ။')


@bot.message_handler(commands=['cancel_wipe'])
def command_cancel_wipe(message):
    user_id = message.from_user.id
    if pending_wipes.pop(user_id, None):
        send_text(message.chat.id, 'Pending wipe request ကို cancel လုပ်ပြီးပါပြီ။')
    else:
        send_text(message.chat.id, 'Pending wipe request မရှိပါ။')


@bot.message_handler(func=lambda message: message.text in MENU_OPTIONS, content_types=['text'])
def handle_buttons(message):
    user_id = message.from_user.id
    ensure_user_exists(user_id)
    text = message.text

    if text == '💰 လက်ကျန်ငွေ':
        balance = get_user_budget(user_id)
        initial = get_user_initial_budget(user_id)
        send_text(
            message.chat.id,
            (
                f'💰 လက်ကျန်ငွေ: {balance:,.0f} ကျပ်\n'
                f'🧾 သတ်မှတ်ထားသော လစဉ် Budget: {initial:,.0f} ကျပ်'
            ),
        )
        return

    if text == '📝 ဘတ်ဂျက် သတ်မှတ်မယ်':
        prompt = send_text(
            message.chat.id,
            'လစဉ်သုံးမည့် ဘတ်ဂျက်ငွေပမာဏကို ဂဏန်းဖြင့် ရိုက်ထည့်ပါ (ဥပမာ - 300000):',
        )
        bot.register_next_step_handler(prompt, process_budget_step)
        return

    if text == '🗂 အုပ်စုစာရင်း':
        send_category_report(message.chat.id, user_id, 'month')
        return

    period_key = BUTTON_PERIODS.get(text)
    if period_key:
        send_period_report(message.chat.id, user_id, period_key)

def process_budget_step(message):
    text = (message.text or '').strip().replace(',', '')

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError

        user_id = message.from_user.id
        set_user_budget(user_id, amount)

        send_text(
            message.chat.id,
            f'✅ လစဉ်ဘတ်ဂျက် {amount:,.0f} ကျပ် သတ်မှတ်ပြီးပါပြီ။',
            reply_markup=get_main_menu(),
        )
    except ValueError:
        send_text(
            message.chat.id,
            '⚠️ ကျေးဇူးပြု၍ 0 ထက်ကြီးသော ဂဏန်းသာ ရိုက်ထည့်ပါ။ (ဥပမာ - 300000)',
            reply_markup=get_main_menu(),
        )


@bot.message_handler(content_types=['text'], func=lambda message: True)
def handle_expense_input(message):
    text = (message.text or '').strip()
    if not text:
        return

    if text.startswith('/'):
        send_text(message.chat.id, '⚠️ မသိသော command ပါ။ /help ကိုသုံးပါ။')
        return

    parsed = parse_expense_text(text)
    if not parsed:
        send_text(
            message.chat.id,
            "⚠️ နားမလည်ပါ။ ဥပမာ: 'မုန့်ဖိုး #food 5000' ပုံစံဖြင့် ရိုက်ထည့်ပါ။",
            reply_markup=get_main_menu(),
        )
        return

    description, amount, category = parsed
    user_id = message.from_user.id
    ensure_user_exists(user_id)

    expense_id = insert_expense(user_id, amount, description, category)
    remaining_balance = deduct_budget(user_id, amount)

    send_text(
        message.chat.id,
        (
            '💸 မှတ်တမ်းသွင်းပြီးပါပြီ။\n'
            f'ID: #{expense_id}\n'
            f'Description: {description}\n'
            f'Category: {category}\n'
            f'Amount: {amount:,.0f} ကျပ်\n'
            f'လက်ကျန်ငွေ: {remaining_balance:,.0f} ကျပ်'
        ),
    )

    maybe_send_budget_alert(user_id, message.chat.id)


if __name__ == '__main__':
    print('Bot is running with .env configuration...')
    bot.infinity_polling(skip_pending=True)
