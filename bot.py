import asyncio
import os
import random
import sqlite3
import time
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)


BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 5134277438
OWNER_USERNAME = "@emptinessdurka"

# True - тестовый бот доступен всем, но реальные функции работают только у владельца.
# False - бот работает для всех пользователей.
TEST_MODE = True

DB_FILE = "/app/data/bot.db"
DAILY_COOLDOWN = 24 * 60 * 60

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Установите переменную окружения BOT_TOKEN."
    )


db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row

db.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL DEFAULT '',
        points INTEGER NOT NULL DEFAULT 0,
        last_claim INTEGER NOT NULL DEFAULT 0,
        banned INTEGER NOT NULL DEFAULT 0
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS tags (
        tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag TEXT NOT NULL UNIQUE,
        price INTEGER NOT NULL DEFAULT 0
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS user_tags (
        user_id INTEGER NOT NULL,
        tag_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, tag_id),
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
    """
)

# Новая колонка для выбранного игроком тега.
try:
    db.execute("ALTER TABLE users ADD COLUMN selected_tag_id INTEGER")
except sqlite3.OperationalError:
    pass

db.commit()

# Базовые теги магазина. INSERT OR IGNORE не дублирует их при перезапуске.
DEFAULT_TAGS = [
    ("🤡", 20),
    ("👻", 20),
    ("🥸", 20),
    ("🥵", 50),
    ("💀", 50),
    ("👍", 50),
    ("👎", 50),
    ("💯", 100),
    ("💪", 100),
    ("💎", 100),
]

db.executemany(
    "INSERT OR IGNORE INTO tags (tag, price) VALUES (?, ?)",
    DEFAULT_TAGS,
)
db.commit()

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Состояния админ-панели. В боте только один владелец.
admin_states: dict[int, str] = {}
admin_temp: dict[int, dict] = {}


def ensure_user(user_id: int, username: str | None) -> None:
    username = username or ""

    db.execute(
        """
        INSERT INTO users (user_id, username)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET username = excluded.username
        """,
        (user_id, username),
    )
    db.commit()


def get_user(user_id: int):
    return db.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()


def find_user(username: str):
    username = username.strip().lstrip("@").lower()

    if not username:
        return None

    return db.execute(
        """
        SELECT * FROM users
        WHERE LOWER(username) = ?
        LIMIT 1
        """,
        (username,),
    ).fetchone()


def username_text(user) -> str:
    if user["user_id"] == OWNER_ID:
        return f"{escape(OWNER_USERNAME)} 😎"

    if user["username"]:
        name = f"@{escape(user['username'].lstrip('@'))}"
    else:
        name = f"ID {user['user_id']}"

    if user["banned"]:
        return name + " 🔒"

    tag = get_selected_tag(user["user_id"])
    if tag:
        return f"{name} {escape(tag)}"

    return name


def get_selected_tag(user_id: int) -> str | None:
    user = get_user(user_id)
    if not user or user["user_id"] == OWNER_ID or user["banned"]:
        return None

    selected_id = user["selected_tag_id"]
    if selected_id is None:
        return None

    # -1 означает, что игрок выбрал тег недели.
    if selected_id == -1:
        weekly = get_weekly_tag()
        return weekly["tag"] if weekly else None

    row = db.execute(
        "SELECT tag FROM tags WHERE tag_id = ?",
        (selected_id,),
    ).fetchone()

    return row["tag"] if row else None


def get_weekly_tag():
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'weekly_tag_id'"
    ).fetchone()

    if not row or not row["value"]:
        return None

    try:
        tag_id = int(row["value"])
    except ValueError:
        return None

    return db.execute(
        "SELECT * FROM tags WHERE tag_id = ?",
        (tag_id,),
    ).fetchone()


def set_weekly_tag(tag_id: int | None) -> None:
    if tag_id is None:
        db.execute("DELETE FROM settings WHERE key = 'weekly_tag_id'")
    else:
        db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('weekly_tag_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(tag_id),),
        )
    db.commit()


def tag_owned(user_id: int, tag_id: int) -> bool:
    return bool(
        db.execute(
            """
            SELECT 1 FROM user_tags
            WHERE user_id = ? AND tag_id = ?
            """,
            (user_id, tag_id),
        ).fetchone()
    )


def user_tag_rows(user_id: int):
    return db.execute(
        """
        SELECT t.*
        FROM tags t
        INNER JOIN user_tags ut ON ut.tag_id = t.tag_id
        WHERE ut.user_id = ?
        ORDER BY t.price ASC, t.tag_id ASC
        """,
        (user_id,),
    ).fetchall()


def get_remaining(last_claim: int) -> int:
    return max(0, DAILY_COOLDOWN - (int(time.time()) - last_claim))


def format_remaining(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds %= 60

    if hours:
        return f"{hours} ч. {minutes} мин."
    if minutes:
        return f"{minutes} мин. {seconds} сек."
    return f"{seconds} сек."


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🎁 Получить очки")],
        [
            KeyboardButton(text="👤 Профиль"),
            KeyboardButton(text="🏆 Лидеры"),
        ],
        [KeyboardButton(text="🛒 Магазин")],
    ]

    if user_id == OWNER_ID:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🚫 Забанить"),
                KeyboardButton(text="♻️ Чёрный список"),
            ],
            [KeyboardButton(text="🧹 Очистить игрока")],
            [KeyboardButton(text="👥 Пользователи")],
            [KeyboardButton(text="🏷️ Управление тегами")],
            [KeyboardButton(text="💥 Очистить всё")],
            [KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def shop_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💎 Кристаллы", callback_data="shop_crystals")],
        [InlineKeyboardButton(text="🏷️ Магазин тегов", callback_data="shop_tags")],
    ]

    weekly = get_weekly_tag()
    if weekly:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🆓 Тег недели: {weekly['tag']}",
                    callback_data=f"weekly_tag:{weekly['tag_id']}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="shop_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tags_shop_keyboard() -> InlineKeyboardMarkup:
    rows = []

    weekly = get_weekly_tag()
    if weekly:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🆓 {weekly['tag']} - бесплатно",
                    callback_data=f"weekly_tag:{weekly['tag_id']}",
                )
            ]
        )

    tags = db.execute(
        "SELECT * FROM tags ORDER BY price ASC, tag_id ASC"
    ).fetchall()

    for tag in tags:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{tag['tag']} - {tag['price']} 💎",
                    callback_data=f"buy_tag:{tag['tag_id']}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="shop_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tag_confirm_keyboard(tag_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Купить",
                    callback_data=f"confirm_buy:{tag_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="shop_tags",
                ),
            ]
        ]
    )


def profile_tags_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for tag in user_tag_rows(user_id):
        current = get_selected_tag(user_id) == tag["tag"]
        prefix = "✅ " if current else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{tag['tag']}",
                    callback_data=f"select_tag:{tag['tag_id']}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="profile_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_tags_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить тег", callback_data="admin_add_tag")],
            [InlineKeyboardButton(text="🗑️ Удалить тег", callback_data="admin_delete_tag")],
            [InlineKeyboardButton(text="💰 Изменить цену", callback_data="admin_price_tag")],
            [InlineKeyboardButton(text="🆓 Тег недели", callback_data="admin_weekly_tag")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_tags_back")],
        ]
    )


def admin_tag_list_keyboard(action: str) -> InlineKeyboardMarkup:
    rows = []
    for tag in db.execute(
        "SELECT * FROM tags ORDER BY price ASC, tag_id ASC"
    ).fetchall():
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{tag['tag']} - {tag['price']} 💎",
                    callback_data=f"{action}:{tag['tag_id']}",
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_tags")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def check_access(message: Message) -> bool:
    user = message.from_user
    if user is None:
        return False

    ensure_user(user.id, user.username)
    row = get_user(user.id)

    if row is None:
        return False

    if TEST_MODE and user.id != OWNER_ID:
        await message.answer(
            "🧪 <b>Тестовый режим</b>\n"
            "Сейчас функции бота доступны только владельцу."
        )
        return False

    if row["banned"] and user.id != OWNER_ID:
        await message.answer(
            "🚫 <b>Ваша учётная запись была заблокирована в боте!</b>\n"
            "Подать апелляцию - @emptinessdurka"
        )
        return False

    return True


def is_owner(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == OWNER_ID


def is_owner_callback(callback: CallbackQuery) -> bool:
    return callback.from_user.id == OWNER_ID


@dp.message(CommandStart())
async def start(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    ensure_user(user.id, user.username)
    row = get_user(user.id)

    if row is None:
        await message.answer("Не удалось создать профиль. Попробуйте ещё раз.")
        return

    if row["banned"] and user.id != OWNER_ID:
        await message.answer(
            "🚫 <b>Ваша учётная запись была заблокирована в боте!</b>\n"
            "Подать апелляцию - @emptinessdurka"
        )
        return

    if TEST_MODE and user.id != OWNER_ID:
        await message.answer(
            "🧪 <b>Бот находится в тестовом режиме.</b>\n"
            "Сейчас функции доступны только владельцу."
        )
        return

    await message.answer(
        "🎉 <b>Добро пожаловать в самого бесполезного бота в вашей жизни!</b> 🤡\n"
        "💎 Получай кристаллы раз в 24 часа и попадай в лидеры 🏆\n"
        "🏷️ Собирай редкие теги в магазине\n"
        "😎 Автор: @emptinessdurka",
        reply_markup=main_keyboard(user.id),
    )


@dp.message(F.text == "🎁 Получить очки")
async def claim(message: Message) -> None:
    if not await check_access(message):
        return

    user_id = message.from_user.id
    now = int(time.time())
    reward = random.randint(1, 5)

    cursor = db.execute(
        """
        UPDATE users
        SET points = points + ?,
            last_claim = ?
        WHERE user_id = ?
          AND last_claim <= ?
          AND banned = 0
        """,
        (reward, now, user_id, now - DAILY_COOLDOWN),
    )
    db.commit()

    if cursor.rowcount == 0:
        row = get_user(user_id)
        remaining = get_remaining(row["last_claim"]) if row else DAILY_COOLDOWN

        await message.answer(
            "⏳ Подарок пока недоступен!\n"
            f"Приходите через {format_remaining(remaining)} 🕐",
            reply_markup=main_keyboard(user_id),
        )
        return

    await message.answer(
        f"🎁 <b>Вам выпало {reward} кристаллов!</b>\n"
        "Приходите через 24 часа за новым подарком 💎",
        reply_markup=main_keyboard(user_id),
    )


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    if not await check_access(message):
        return

    row = get_user(message.from_user.id)
    if row is None:
        return

    await message.answer(
        "👤 <b>Ваш профиль:</b>\n"
        f"Юзернейм - {username_text(row)}\n"
        f"Кристаллы - {row['points']} 💎\n"
        "🏷️ Тег - "
        + ("😎" if row["user_id"] == OWNER_ID else (get_selected_tag(row["user_id"]) or "нет")),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🏷️ Теги",
                        callback_data="profile_tags",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Главное меню",
                        callback_data="profile_back",
                    )
                ],
            ]
        ),
    )


@dp.message(F.text == "🏆 Лидеры")
async def leaders(message: Message) -> None:
    if not await check_access(message):
        return

    rows = db.execute(
        """
        SELECT * FROM users
        ORDER BY points DESC, user_id ASC
        LIMIT 5
        """
    ).fetchall()

    text = "🏆 <b>Лидеры</b>\n\n"
    places = ["👑", "2 место", "3 место", "4 место", "5 место"]

    if rows:
        for index, row in enumerate(rows):
            text += (
                f"{places[index]}: {username_text(row)} - "
                f"{row['points']} кристаллов\n"
            )
    else:
        text += "Пока здесь никого нет 😴\n"

    await message.answer(
        text,
        reply_markup=main_keyboard(message.from_user.id),
    )


@dp.message(F.text == "🛒 Магазин")
async def shop(message: Message) -> None:
    if not await check_access(message):
        return

    await message.answer(
        "🏪 <b>Магазин тегов</b>\n\n"
        "💎 Здесь можно получить кристаллы и приобрести теги.\n"
        "🆓 Тег недели доступен бесплатно.",
        reply_markup=shop_keyboard(),
    )


@dp.callback_query(F.data == "shop_crystals")
async def shop_crystals(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "💎 <b>Кристаллы</b>\n\n"
        "🎁 Ежедневный подарок: от 1 до 5 кристаллов.\n"
        "⏰ Забрать его можно один раз в 24 часа.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎁 Забрать подарок",
                        callback_data="claim_inline",
                    )
                ],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="shop_back")],
            ]
        ),
    )


@dp.callback_query(F.data == "claim_inline")
async def claim_inline(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return

    user_id = callback.from_user.id
    ensure_user(user_id, callback.from_user.username)
    row = get_user(user_id)

    if row["banned"] and user_id != OWNER_ID:
        await callback.answer("🚫 Вы заблокированы.", show_alert=True)
        return

    now = int(time.time())
    remaining = get_remaining(row["last_claim"])

    if remaining > 0:
        await callback.answer(
            f"⏳ Следующий подарок через {format_remaining(remaining)}",
            show_alert=True,
        )
        return

    reward = random.randint(1, 5)
    db.execute(
        """
        UPDATE users
        SET points = points + ?, last_claim = ?
        WHERE user_id = ?
        """,
        (reward, now, user_id),
    )
    db.commit()

    await callback.answer(f"🎁 Вам выпало {reward} кристаллов!", show_alert=True)


@dp.callback_query(F.data == "shop_tags")
async def shop_tags(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "🏷️ <b>Магазин тегов</b>\n\n"
        "Выберите тег, чтобы посмотреть его цену и купить его.",
        reply_markup=tags_shop_keyboard(),
    )


@dp.callback_query(F.data.startswith("buy_tag:"))
async def buy_tag(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return

    tag_id = int(callback.data.split(":")[1])
    tag = db.execute(
        "SELECT * FROM tags WHERE tag_id = ?",
        (tag_id,),
    ).fetchone()

    if tag is None:
        await callback.answer("❌ Тег больше не существует.", show_alert=True)
        return

    weekly = get_weekly_tag()
    if weekly and weekly["tag_id"] == tag_id:
        await select_tag_for_user(callback, tag_id, weekly=True)
        return

    if tag_owned(callback.from_user.id, tag_id):
        await callback.answer("ℹ️ Этот тег уже куплен.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        f"🏷️ <b>Покупка тега {escape(tag['tag'])}</b>\n\n"
        f"Цена: <b>{tag['price']} 💎</b>\n\n"
        "Купить этот тег?",
        reply_markup=tag_confirm_keyboard(tag_id),
    )


@dp.callback_query(F.data.startswith("confirm_buy:"))
async def confirm_buy(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return

    tag_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    tag = db.execute(
        "SELECT * FROM tags WHERE tag_id = ?",
        (tag_id,),
    ).fetchone()

    if tag is None:
        await callback.answer("❌ Тег больше не существует.", show_alert=True)
        return

    if tag_owned(user_id, tag_id):
        await callback.answer("ℹ️ Этот тег уже куплен.", show_alert=True)
        return

    user = get_user(user_id)
    if user is None:
        ensure_user(user_id, callback.from_user.username)
        user = get_user(user_id)

    if user["points"] < tag["price"]:
        await callback.answer(
            f"❌ Недостаточно кристаллов. Нужно {tag['price']} 💎, у вас {user['points']} 💎.",
            show_alert=True,
        )
        return

    db.execute(
        "UPDATE users SET points = points - ? WHERE user_id = ?",
        (tag["price"], user_id),
    )
    db.execute(
        "INSERT OR IGNORE INTO user_tags (user_id, tag_id) VALUES (?, ?)",
        (user_id, tag_id),
    )
    db.commit()

    await callback.answer("✅ Тег куплен!", show_alert=True)
    await callback.message.edit_text(
        f"✅ <b>Тег {escape(tag['tag'])} успешно куплен!</b>\n\n"
        "Открыть его можно в профиле → 🏷️ Теги.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏷️ Мои теги", callback_data="profile_tags")],
                [InlineKeyboardButton(text="🛒 Магазин", callback_data="shop_tags")],
            ]
        ),
    )


async def select_tag_for_user(
    callback: CallbackQuery,
    tag_id: int,
    weekly: bool = False,
) -> None:
    user_id = callback.from_user.id
    if user_id != OWNER_ID:
        user = get_user(user_id)
        if user and user["banned"]:
            await callback.answer("🚫 Заблокированный пользователь не может менять тег.", show_alert=True)
            return

    if not weekly and not tag_owned(user_id, tag_id):
        await callback.answer("❌ Этот тег ещё не куплен.", show_alert=True)
        return

    selected_value = -1 if weekly else tag_id
    db.execute(
        "UPDATE users SET selected_tag_id = ? WHERE user_id = ?",
        (selected_value, user_id),
    )
    db.commit()

    tag = get_weekly_tag() if weekly else db.execute(
        "SELECT tag FROM tags WHERE tag_id = ?",
        (tag_id,),
    ).fetchone()

    await callback.answer(f"✅ Выбран тег {tag['tag']}!")
    await callback.message.edit_text(
        f"🏷️ <b>Текущий тег:</b> {escape(tag['tag'])}",
        reply_markup=profile_tags_keyboard(user_id),
    )


@dp.callback_query(F.data.startswith("select_tag:"))
async def select_tag(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return
    await select_tag_for_user(callback, int(callback.data.split(":")[1]))


@dp.callback_query(F.data.startswith("weekly_tag:"))
async def weekly_tag(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return

    tag_id = int(callback.data.split(":")[1])
    weekly = get_weekly_tag()
    if not weekly or weekly["tag_id"] != tag_id:
        await callback.answer(
            "❌ Этот тег больше не является тегом недели.",
            show_alert=True,
        )
        return

    await select_tag_for_user(callback, tag_id, weekly=True)


@dp.callback_query(F.data == "profile_tags")
async def profile_tags(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback) and TEST_MODE:
        await callback.answer("🧪 Тестовый режим: доступ только владельцу.", show_alert=True)
        return

    user_id = callback.from_user.id
    user = get_user(user_id)
    if user and user["banned"] and user_id != OWNER_ID:
        await callback.answer("🚫 Заблокированный пользователь не может менять тег.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "🏷️ <b>Мои теги</b>\n\n"
        "Выберите купленный тег или бесплатный тег недели.",
        reply_markup=profile_tags_keyboard(user_id),
    )


@dp.callback_query(F.data == "profile_back")
async def profile_back(callback: CallbackQuery) -> None:
    await callback.answer()
    user = get_user(callback.from_user.id)
    if user:
        await callback.message.edit_text(
            "👤 <b>Профиль</b>\n"
            f"Кристаллы: {user['points']} 💎\n"
            f"Текущий тег: {'😎' if user['user_id'] == OWNER_ID else (get_selected_tag(user['user_id']) or 'нет')}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏷️ Теги", callback_data="profile_tags")],
                    [InlineKeyboardButton(text="🔙 Главное меню", callback_data="close_inline")],
                ]
            ),
        )


@dp.callback_query(F.data == "close_inline")
async def close_inline(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.delete()


@dp.callback_query(F.data == "shop_back")
async def shop_back(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "🏪 <b>Магазин тегов</b>\n\n"
        "💎 Здесь можно получить кристаллы и приобрести теги.\n"
        "🆓 Тег недели доступен бесплатно.",
        reply_markup=shop_keyboard(),
    )


@dp.callback_query(F.data == "shop_close")
async def shop_close(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.delete()


@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message) -> None:
    if not is_owner(message):
        return

    admin_states.pop(OWNER_ID, None)
    admin_temp.pop(OWNER_ID, None)

    await message.answer(
        "⚙️ <b>Админ-панель</b>",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "🏷️ Управление тегами")
async def admin_tags(message: Message) -> None:
    if not is_owner(message):
        return

    admin_states.pop(OWNER_ID, None)
    admin_temp.pop(OWNER_ID, None)

    await message.answer(
        "🏷️ <b>Управление тегами</b>\n\n"
        "Здесь можно добавлять, удалять, менять цены и выбирать тег недели.",
        reply_markup=admin_tags_keyboard(),
    )


@dp.callback_query(F.data == "admin_tags")
async def admin_tags_callback(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "🏷️ <b>Управление тегами</b>",
        reply_markup=admin_tags_keyboard(),
    )


@dp.callback_query(F.data == "admin_add_tag")
async def admin_add_tag(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    admin_states[OWNER_ID] = "tag_add_name"
    admin_temp[OWNER_ID] = {}
    await callback.answer()
    await callback.message.answer(
        "➕ Введите новый тег:",
        reply_markup=cancel_keyboard(),
    )


@dp.callback_query(F.data == "admin_delete_tag")
async def admin_delete_tag(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "🗑️ <b>Выберите тег для удаления:</b>",
        reply_markup=admin_tag_list_keyboard("admin_del"),
    )


@dp.callback_query(F.data == "admin_price_tag")
async def admin_price_tag(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "💰 <b>Выберите тег для изменения цены:</b>",
        reply_markup=admin_tag_list_keyboard("admin_price"),
    )


@dp.callback_query(F.data == "admin_weekly_tag")
async def admin_weekly_tag(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "🆓 <b>Выберите тег недели:</b>\n\n"
        "Он будет бесплатным для всех пользователей.",
        reply_markup=admin_tag_list_keyboard("admin_weekly"),
    )


@dp.callback_query(F.data == "admin_tags_back")
async def admin_tags_back(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        "⚙️ <b>Админ-панель</b>",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data.startswith("admin_del:"))
async def admin_del(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    tag_id = int(callback.data.split(":")[1])
    tag = db.execute("SELECT * FROM tags WHERE tag_id = ?", (tag_id,)).fetchone()

    if not tag:
        await callback.answer("❌ Тег не найден.", show_alert=True)
        return

    weekly = get_weekly_tag()
    if weekly and weekly["tag_id"] == tag_id:
        set_weekly_tag(None)

    db.execute("DELETE FROM user_tags WHERE tag_id = ?", (tag_id,))
    db.execute("UPDATE users SET selected_tag_id = NULL WHERE selected_tag_id = ?", (tag_id,))
    db.execute("DELETE FROM tags WHERE tag_id = ?", (tag_id,))
    db.commit()

    await callback.answer("🗑️ Тег удалён.", show_alert=True)
    await callback.message.edit_text(
        "🏷️ <b>Управление тегами</b>",
        reply_markup=admin_tags_keyboard(),
    )


@dp.callback_query(F.data.startswith("admin_price:"))
async def admin_price(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    tag_id = int(callback.data.split(":")[1])
    tag = db.execute("SELECT * FROM tags WHERE tag_id = ?", (tag_id,)).fetchone()

    if not tag:
        await callback.answer("❌ Тег не найден.", show_alert=True)
        return

    admin_states[OWNER_ID] = "tag_price"
    admin_temp[OWNER_ID] = {"tag_id": tag_id}

    await callback.answer()
    await callback.message.answer(
        f"💰 Введите новую цену для тега {escape(tag['tag'])} в кристаллах:",
        reply_markup=cancel_keyboard(),
    )


@dp.callback_query(F.data.startswith("admin_weekly:"))
async def admin_weekly(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    tag_id = int(callback.data.split(":")[1])
    tag = db.execute("SELECT * FROM tags WHERE tag_id = ?", (tag_id,)).fetchone()

    if not tag:
        await callback.answer("❌ Тег не найден.", show_alert=True)
        return

    set_weekly_tag(tag_id)

    await callback.answer(f"🆓 {tag['tag']} теперь тег недели!", show_alert=True)
    await callback.message.edit_text(
        f"🆓 <b>Тег недели:</b> {escape(tag['tag'])}",
        reply_markup=admin_tags_keyboard(),
    )


@dp.message(F.text == "🚫 Забанить")
async def ban_start(message: Message) -> None:
    if not is_owner(message):
        return

    admin_states[OWNER_ID] = "ban"

    await message.answer(
        "🚫 Введите юзернейм:",
        reply_markup=cancel_keyboard(),
    )


@dp.message(F.text == "♻️ Чёрный список")
async def blacklist(message: Message) -> None:
    if not is_owner(message):
        return

    rows = db.execute(
        "SELECT * FROM users WHERE banned = 1 ORDER BY user_id"
    ).fetchall()

    text = "♻️ <b>Чёрный список</b>\n\n"

    if rows:
        for row in rows:
            text += f"🚫 {username_text(row)}\n"

        text += "\nВведите имя пользователя для разблокировки:"
        admin_states[OWNER_ID] = "unban"
        markup = cancel_keyboard()
    else:
        text += "Список пуст."
        markup = admin_keyboard()

    await message.answer(text, reply_markup=markup)


@dp.message(F.text == "🧹 Очистить игрока")
async def clear_player_start(message: Message) -> None:
    if not is_owner(message):
        return

    admin_states[OWNER_ID] = "clear_user"

    await message.answer(
        "🧹 Введите юзернейм:",
        reply_markup=cancel_keyboard(),
    )


@dp.message(F.text == "👥 Пользователи")
async def users_count(message: Message) -> None:
    if not is_owner(message):
        return

    row = db.execute(
        "SELECT COUNT(*) AS count FROM users"
    ).fetchone()

    count = row["count"] if row else 0

    await message.answer(
        f"👥 Число пользователей в боте: {count}",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "💥 Очистить всё")
async def clear_all_start(message: Message) -> None:
    if not is_owner(message):
        return

    admin_states[OWNER_ID] = "wipe_first"

    await message.answer(
        "⚠️ Вы действительно хотите полностью очистить бота?\n\n"
        "Напишите ДА для продолжения.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(F.text == "❌ Отмена")
async def cancel(message: Message) -> None:
    if not is_owner(message):
        return

    admin_states.pop(OWNER_ID, None)
    admin_temp.pop(OWNER_ID, None)

    await message.answer(
        "❌ Действие отменено.",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "🔙 Главное меню")
async def back_to_menu(message: Message) -> None:
    if not is_owner(message):
        return

    admin_states.pop(OWNER_ID, None)
    admin_temp.pop(OWNER_ID, None)

    await message.answer(
        "🏠 Главное меню",
        reply_markup=main_keyboard(OWNER_ID),
    )


@dp.message()
async def admin_input(message: Message) -> None:
    if not is_owner(message):
        return

    state = admin_states.get(OWNER_ID)
    if not state:
        return

    text = (message.text or "").strip()

    if state == "tag_add_name":
        if not text:
            await message.answer(
                "❌ Тег не может быть пустым. Введите тег ещё раз.",
                reply_markup=cancel_keyboard(),
            )
            return

        existing = db.execute(
            "SELECT 1 FROM tags WHERE tag = ?",
            (text,),
        ).fetchone()

        if existing:
            await message.answer(
                "❌ Такой тег уже существует.",
                reply_markup=cancel_keyboard(),
            )
            return

        admin_temp[OWNER_ID] = {"tag": text}
        admin_states[OWNER_ID] = "tag_add_price"

        await message.answer(
            f"🏷️ Тег: {escape(text)}\n\n"
            "💰 Теперь введите цену в кристаллах:",
            reply_markup=cancel_keyboard(),
        )
        return

    if state == "tag_add_price":
        try:
            price = int(text)
        except ValueError:
            await message.answer(
                "❌ Цена должна быть целым числом.",
                reply_markup=cancel_keyboard(),
            )
            return

        if price < 0:
            await message.answer(
                "❌ Цена не может быть отрицательной.",
                reply_markup=cancel_keyboard(),
            )
            return

        tag = admin_temp.get(OWNER_ID, {}).get("tag")
        if not tag:
            admin_states.pop(OWNER_ID, None)
            admin_temp.pop(OWNER_ID, None)
            await message.answer("❌ Данные добавления потеряны. Начните заново.", reply_markup=admin_keyboard())
            return

        try:
            db.execute(
                "INSERT INTO tags (tag, price) VALUES (?, ?)",
                (tag, price),
            )
            db.commit()
        except sqlite3.IntegrityError:
            await message.answer(
                "❌ Такой тег уже существует.",
                reply_markup=cancel_keyboard(),
            )
            return

        admin_states.pop(OWNER_ID, None)
        admin_temp.pop(OWNER_ID, None)

        await message.answer(
            f"✅ Тег {escape(tag)} добавлен за {price} 💎.",
            reply_markup=admin_tags_keyboard(),
        )
        return

    if state == "tag_price":
        try:
            price = int(text)
        except ValueError:
            await message.answer(
                "❌ Цена должна быть целым числом.",
                reply_markup=cancel_keyboard(),
            )
            return

        if price < 0:
            await message.answer(
                "❌ Цена не может быть отрицательной.",
                reply_markup=cancel_keyboard(),
            )
            return

        tag_id = admin_temp.get(OWNER_ID, {}).get("tag_id")
        tag = db.execute(
            "SELECT * FROM tags WHERE tag_id = ?",
            (tag_id,),
        ).fetchone()

        if not tag:
            admin_states.pop(OWNER_ID, None)
            admin_temp.pop(OWNER_ID, None)
            await message.answer("❌ Тег не найден.", reply_markup=admin_keyboard())
            return

        db.execute(
            "UPDATE tags SET price = ? WHERE tag_id = ?",
            (price, tag_id),
        )
        db.commit()

        admin_states.pop(OWNER_ID, None)
        admin_temp.pop(OWNER_ID, None)

        await message.answer(
            f"✅ Цена тега {escape(tag['tag'])} изменена на {price} 💎.",
            reply_markup=admin_tags_keyboard(),
        )
        return

    if state == "ban":
        row = find_user(text)

        if row is None:
            await message.answer(
                "❌ Пользователь не найден.",
                reply_markup=cancel_keyboard(),
            )
            return

        if row["user_id"] == OWNER_ID:
            await message.answer(
                "❌ Нельзя заблокировать владельца бота.",
                reply_markup=cancel_keyboard(),
            )
            return

        if row["banned"]:
            await message.answer(
                "🚫 Пользователь уже заблокирован.",
                reply_markup=cancel_keyboard(),
            )
            return

        db.execute(
            "UPDATE users SET banned = 1 WHERE user_id = ?",
            (row["user_id"],),
        )
        db.commit()
        admin_states.pop(OWNER_ID, None)

        try:
            await bot.send_message(
                row["user_id"],
                "🚫 Ваша учётная запись была заблокирована в боте!\n"
                "Подать апелляцию - @emptinessdurka",
            )
        except Exception:
            pass

        await message.answer(
            f"🚫 {username_text(row)} заблокирован.",
            reply_markup=admin_keyboard(),
        )
        return

    if state == "unban":
        row = find_user(text)

        if row is None:
            await message.answer(
                "❌ Пользователь не найден.",
                reply_markup=cancel_keyboard(),
            )
            return

        if not row["banned"]:
            await message.answer(
                "ℹ️ Пользователь не находится в чёрном списке.",
                reply_markup=cancel_keyboard(),
            )
            return

        db.execute(
            "UPDATE users SET banned = 0 WHERE user_id = ?",
            (row["user_id"],),
        )
        db.commit()
        admin_states.pop(OWNER_ID, None)

        try:
            await bot.send_message(
                row["user_id"],
                "♻️ Ваша учётная запись снова доступна в боте!",
            )
        except Exception:
            pass

        await message.answer(
            f"♻️ {username_text(row)} снова доступен.",
            reply_markup=admin_keyboard(),
        )
        return

    if state == "clear_user":
        row = find_user(text)

        if row is None:
            await message.answer(
                "❌ Пользователь не найден.",
                reply_markup=cancel_keyboard(),
            )
            return

        if row["user_id"] == OWNER_ID:
            await message.answer(
                "❌ Нельзя очистить профиль владельца этим действием.",
                reply_markup=cancel_keyboard(),
            )
            return

        db.execute(
            """
            UPDATE users
            SET points = 0,
                last_claim = 0,
                selected_tag_id = NULL
            WHERE user_id = ?
            """,
            (row["user_id"],),
        )
        db.execute(
            "DELETE FROM user_tags WHERE user_id = ?",
            (row["user_id"],),
        )
        db.commit()
        admin_states.pop(OWNER_ID, None)

        await message.answer(
            f"🧹 Данные игрока {username_text(row)} очищены.",
            reply_markup=admin_keyboard(),
        )
        return

    if state == "wipe_first":
        if text.upper() != "ДА":
            await message.answer(
                "❌ Напишите ДА для продолжения.",
                reply_markup=cancel_keyboard(),
            )
            return

        admin_states[OWNER_ID] = "wipe_second"

        await message.answer(
            "⚠️ Последнее подтверждение.\n\n"
            "Напишите УДАЛИТЬ для полной очистки.",
            reply_markup=cancel_keyboard(),
        )
        return

    if state == "wipe_second":
        if text.upper() != "УДАЛИТЬ":
            await message.answer(
                "❌ Напишите УДАЛИТЬ для подтверждения.",
                reply_markup=cancel_keyboard(),
            )
            return

        db.execute("DELETE FROM user_tags")
        db.execute("DELETE FROM users")
        db.execute("DELETE FROM settings")
        db.commit()
        admin_states.pop(OWNER_ID, None)

        await message.answer(
            "💥 Бот полностью очищен.",
            reply_markup=admin_keyboard(),
        )


async def main() -> None:
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
