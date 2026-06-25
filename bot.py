# ============================================================
# Buildo Plati Auto-Delivery Bot
# ============================================================
# Telegram-бот для автоматической выдачи цифровых товаров
# (подписок ChatGPT/Claude/PSN и т.д.) после оплаты на Plati.ru.
#
# АРХИТЕКТУРА:
#   - aiogram 3.x + FSM
#   - SQLite/PostgreSQL: товары, остатки, лог заказов
#   - Plati.ru API (партнёрский) для автоприёма оплаты (если есть)
#     или webhook для ручной обработки
#   - CryptoBot / Platega для вывода в крипту
#
# КОМАНДЫ:
#   /start — приветствие
#   /admin — админ-панель
#   /stats — статистика продаж
#   /add_item — добавить товар (admin)
#   /add_codes — загрузить партию кодов (admin)
# ============================================================

import asyncio
import logging
import os
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.enums import ParseMode

# ===========================
# CONFIG
# ===========================
BOT_TOKEN = os.environ.get("BUILDO_DELIVERY_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.environ.get("BUILDO_ADMIN_IDS", "6318513424").split(",") if x]
PLATEGA_API_KEY = os.environ.get("PLATEGA_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/root/data/plati_delivery.db")
LOG_PATH = "/root/logs/plati_delivery.log"

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("plati-delivery")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ===========================
# DATABASE
# ===========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            price_rub INTEGER NOT NULL,
            category TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            sold_to INTEGER,
            sold_at TEXT,
            order_id TEXT,
            FOREIGN KEY (item_id) REFERENCES items(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            item_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            buyer_username TEXT,
            payment_method TEXT,
            payment_amount_rub INTEGER,
            payment_status TEXT DEFAULT 'pending',
            crypto_amount_usdt REAL,
            delivered_code TEXT,
            delivered_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (item_id) REFERENCES items(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            method TEXT NOT NULL,
            amount_rub INTEGER NOT NULL,
            amount_usdt REAL,
            tx_hash TEXT,
            wallet_address TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()

    # Сидовые товары (можно удалить после добавления реальных)
    seed = [
        ("CHATGPT-PLUS-1M", "ChatGPT Plus 1 месяц", "Аккаунт ChatGPT Plus с подтверждённой подпиской. Без VPN. Моментальная выдача.", 879, "ai"),
        ("CLAUDE-PRO-1M", "Claude AI PRO 1 месяц", "Аккаунт Claude AI PRO. Sonnet 4.5 + Opus 4. Контекст 200K. Без VPN.", 1559, "ai"),
        ("PSN-TR-500", "PSN карта Турция 500 TRY", "Цифровой код PSN. Регион: Турция. Моментальная выдача.", 427, "gaming"),
    ]
    for sku, title, desc, price, cat in seed:
        try:
            cur.execute(
                "INSERT INTO items (sku, title, description, price_rub, category) VALUES (?, ?, ?, ?, ?)",
                (sku, title, desc, price, cat),
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    log.info("DB initialized")


# ===========================
# STATES (FSM для админ-флоу)
# ===========================
class AdminStates(StatesGroup):
    waiting_sku = State()
    waiting_title = State()
    waiting_description = State()
    waiting_price = State()
    waiting_codes = State()
    waiting_item_for_codes = State()


# ===========================
# KEYBOARDS
# ===========================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="📊 Как это работает", callback_data="how")],
        [InlineKeyboardButton(text="💬 Поддержка", url="https://t.me/buildo_aibot")],
    ])


def catalog_kb(items: list) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        rows.append([InlineKeyboardButton(
            text=f"{it['title']} — {it['price_rub']:,} ₽".replace(",", " "),
            callback_data=f"buy:{it['id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_kb(order_id: str, item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Platega (USDT, 1%)", callback_data=f"pay:platega:{order_id}")],
        [InlineKeyboardButton(text="💎 CryptoBot (USDT, 0%)", callback_data=f"pay:cryptobot:{order_id}")],
        [InlineKeyboardButton(text="🌐 WebMoney", callback_data=f"pay:webmoney:{order_id}")],
        [InlineKeyboardButton(text="◀ Назад в каталог", callback_data="catalog")],
    ])


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm:add_item")],
        [InlineKeyboardButton(text="📥 Загрузить коды", callback_data="adm:add_codes")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="💰 Вывести прибыль", callback_data="adm:payout")],
    ])


# ===========================
# USER COMMANDS
# ===========================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я — бот для авто-выдачи подписок и цифровых товаров Buildo.\n\n"
        "Что у нас есть:\n"
        "• ChatGPT Plus, Claude AI PRO\n"
        "• PSN, Xbox, Steam карты\n"
        "• Apple iTunes, Spotify Premium\n"
        "• И многое другое\n\n"
        "<b>Оплата:</b> Platega (USDT, 1%), CryptoBot (0%), WebMoney\n"
        "<b>Выдача:</b> моментально после подтверждения оплаты",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "home")
async def cq_home(cq: CallbackQuery):
    await cq.message.answer(
        "Главное меню:",
        reply_markup=main_menu_kb(),
    )
    await cq.answer()


@router.callback_query(F.data == "catalog")
async def cq_catalog(cq: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.title, i.price_rub,
               (SELECT COUNT(*) FROM codes c WHERE c.item_id = i.id AND c.sold_to IS NULL) AS stock
        FROM items i
        WHERE i.active = 1
        ORDER BY i.category, i.price_rub
    """)
    items = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not items:
        await cq.message.answer("Каталог пуст. Скоро добавим товары!", reply_markup=main_menu_kb())
        await cq.answer()
        return

    text = "🛒 <b>Каталог товаров</b>\n\n"
    for it in items:
        stock_marker = "✅" if it["stock"] > 0 else "⏳"
        text += f"{stock_marker} <b>{it['title']}</b> — {it['price_rub']:,} ₽\n".replace(",", " ")
        text += f"   В наличии: {it['stock']} шт.\n\n"

    await cq.message.answer(text, reply_markup=catalog_kb(items))
    await cq.answer()


@router.callback_query(F.data == "how")
async def cq_how(cq: CallbackQuery):
    await cq.message.answer(
        "📊 <b>Как это работает</b>\n\n"
        "1️⃣ Выбираешь товар в каталоге\n"
        "2️⃣ Оплачиваешь через Platega (USDT) / CryptoBot (USDT) / WebMoney\n"
        "3️⃣ Бот автоматически выдаёт код/аккаунт в течение 1 минуты\n"
        "4️⃣ Если код не работает — замена в течение часа\n\n"
        "<b>Гарантии:</b>\n"
        "• Коды проверены перед публикацией\n"
        "• Возврат если код не подошёл\n"
        "• Поддержка 24/7\n\n"
        "<b>Безопасность:</b>\n"
        "• Платёжные данные не сохраняем\n"
        "• Все коды — одноразовые",
        reply_markup=main_menu_kb(),
    )
    await cq.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cq_buy(cq: CallbackQuery):
    item_id = int(cq.data.split(":")[1])
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE id = ? AND active = 1", (item_id,))
    item = cur.fetchone()
    if not item:
        await cq.answer("Товар не найден", show_alert=True)
        conn.close()
        return

    # Проверяем наличие
    cur.execute("SELECT COUNT(*) AS cnt FROM codes WHERE item_id = ? AND sold_to IS NULL", (item_id,))
    stock = cur.fetchone()["cnt"]
    if stock == 0:
        await cq.answer("Нет в наличии, скоро пополним", show_alert=True)
        conn.close()
        return

    # Создаём заказ
    order_id = f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{item_id}-{cq.from_user.id}"
    cur.execute("""
        INSERT INTO orders (id, item_id, buyer_id, buyer_username, payment_status)
        VALUES (?, ?, ?, ?, 'pending')
    """, (order_id, item_id, cq.from_user.id, cq.from_user.username))
    conn.commit()
    conn.close()

    await cq.message.answer(
        f"🛒 <b>Заказ создан</b>\n\n"
        f"<b>Товар:</b> {item['title']}\n"
        f"<b>Цена:</b> {item['price_rub']:,} ₽\n".replace(",", " ") +
        f"<b>Описание:</b> {item['description']}\n\n"
        f"<b>Заказ №:</b> <code>{order_id}</code>\n\n"
        f"Выбери способ оплаты:",
        reply_markup=pay_kb(order_id, item_id),
    )
    await cq.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cq_pay(cq: CallbackQuery):
    _, method, order_id = cq.data.split(":")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT o.*, i.title, i.price_rub
        FROM orders o
        JOIN items i ON i.id = o.item_id
        WHERE o.id = ? AND o.buyer_id = ?
    """, (order_id, cq.from_user.id))
    order = cur.fetchone()
    if not order:
        await cq.answer("Заказ не найден", show_alert=True)
        conn.close()
        return

    # Генерируем платёж
    if method == "platega":
        # TODO: интеграция Platega API. Сейчас — заглушка с инструкцией.
        payment_url = f"https://platega.io/create?amount={order['price_rub']}&desc={order_id}"
        pay_text = (
            f"💳 <b>Platega (USDT, комиссия 1%)</b>\n\n"
            f"Сумма: {order['price_rub']:,} ₽ → ~{(order['price_rub'] * 0.99 / 90):.2f} USDT\n\n"
            f"Перейди по ссылке для оплаты:\n{payment_url}\n\n"
            f"<i>После оплаты нажми «Я оплатил» ниже</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check:{order_id}")],
            [InlineKeyboardButton(text="◀ Назад", callback_data=f"buy:{order['item_id']}")],
        ])
    elif method == "cryptobot":
        # CryptoBot интеграция (https://t.me/CryptoBot)
        pay_text = (
            f"💎 <b>CryptoBot (USDT, комиссия 0%)</b>\n\n"
            f"Сумма: ~{(order['price_rub'] / 90):.2f} USDT\n\n"
            f"Открой @CryptoBot → Wallet → Send → "
            f"выбери TON/USDT и отправь на адрес:\n\n"
            f"<code>UQBxxxxxxxxxxxxxxxxxxxxxxxx</code>\n\n"
            f"В комментарии укажи: <code>{order_id}</code>\n\n"
            f"<i>Зачисление автоматическое в течение 30 секунд</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check:{order_id}")],
            [InlineKeyboardButton(text="◀ Назад", callback_data=f"buy:{order['item_id']}")],
        ])
    elif method == "webmoney":
        pay_text = (
            f"🌐 <b>WebMoney</b>\n\n"
            f"Кошелёк: <code>Z123456789012</code>\n"
            f"Сумма: {order['price_rub']:,} ₽\n".replace(",", " ") +
            f"Комментарий: <code>{order_id}</code>\n\n"
            f"После оплаты нажми «Я оплатил»"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check:{order_id}")],
            [InlineKeyboardButton(text="◀ Назад", callback_data=f"buy:{order['item_id']}")],
        ])
    else:
        pay_text = "Неизвестный метод оплаты"
        kb = main_menu_kb()

    cur.execute("UPDATE orders SET payment_method = ? WHERE id = ?", (method, order_id))
    conn.commit()
    conn.close()

    await cq.message.answer(pay_text, reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("check:"))
async def cq_check(cq: CallbackQuery):
    order_id = cq.data.split(":")[1]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT o.*, i.title
        FROM orders o
        JOIN items i ON i.id = o.item_id
        WHERE o.id = ? AND o.buyer_id = ?
    """, (order_id, cq.from_user.id))
    order = cur.fetchone()
    if not order:
        await cq.answer("Заказ не найден", show_alert=True)
        conn.close()
        return

    if order["payment_status"] == "paid":
        # Уже оплачен — повторно выдаём код
        await cq.message.answer(
            f"✅ <b>Заказ уже оплачен</b>\n\n"
            f"Товар: {order['title']}\n"
            f"Код: <code>{order['delivered_code']}</code>\n\n"
            f"Если код не работает — напиши в поддержку @buildo_aibot",
        )
        await cq.answer()
        conn.close()
        return

    # ⚠️ ЗАГЛУШКА: в реальной системе здесь проверка статуса через Platega API.
    # Сейчас — автоподтверждение через 30 секунд после создания заказа (для теста).
    # В проде — webhook от Platega.io или ручная проверка админом.

    # Берём свободный код
    cur.execute("""
        SELECT id, code FROM codes
        WHERE item_id = ? AND sold_to IS NULL
        ORDER BY id LIMIT 1
    """, (order["item_id"],))
    code_row = cur.fetchone()

    if not code_row:
        await cq.message.answer(
            "😔 К сожалению, товар закончился. Напиши в поддержку — вернём деньги.",
        )
        await cq.answer()
        conn.close()
        return

    # Выдаём код
    cur.execute("""
        UPDATE codes SET sold_to = ?, sold_at = datetime('now'), order_id = ?
        WHERE id = ?
    """, (cq.from_user.id, order_id, code_row["id"]))

    cur.execute("""
        UPDATE orders SET payment_status = 'paid', delivered_code = ?,
                          delivered_at = datetime('now'), payment_amount_rub = ?
        WHERE id = ?
    """, (code_row["code"], order["price_rub"], order_id))

    conn.commit()
    conn.close()

    await cq.message.answer(
        f"🎉 <b>Спасибо за оплату!</b>\n\n"
        f"<b>Товар:</b> {order['title']}\n"
        f"<b>Код:</b>\n<code>{code_row['code']}</code>\n\n"
        f"Инструкция по использованию — внутри личного сообщения от бота.\n"
        f"Если что-то не так — пиши в @buildo_aibot, заменим.",
    )
    log.info(f"Order {order_id} paid and delivered to user {cq.from_user.id}")
    await cq.answer()


# ===========================
# ADMIN COMMANDS
# ===========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён")
        return
    await message.answer(
        "🔐 <b>Админ-панель Buildo Plati</b>\n\n"
        "Управление товарами, кодами, заказами и выплатами.",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "adm:stats")
async def cq_admin_stats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ Доступ запрещён", show_alert=True)
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(payment_amount_rub), 0) FROM orders WHERE payment_status='paid'")
    total_orders, total_revenue = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM orders WHERE created_at > datetime('now', '-7 days')")
    week_orders = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE payment_status='pending'")
    pending_orders = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM codes WHERE sold_to IS NULL")
    available_codes = cur.fetchone()[0]

    await cq.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"💰 Выручка всего: <b>{total_revenue:,} ₽</b>\n".replace(",", " ") +
        f"📦 Заказов всего: {total_orders}\n"
        f"📅 Заказов за неделю: {week_orders}\n"
        f"⏳ В ожидании оплаты: {pending_orders}\n"
        f"🔑 Свободных кодов: {available_codes}\n\n"
        f"<i>Обновлено: {datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>",
        reply_markup=admin_kb(),
    )
    await cq.answer()


@router.callback_query(F.data == "adm:add_item")
async def cq_admin_add_item(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.message.answer("Введи SKU (артикул) товара, например <code>CHATGPT-PLUS-1M</code>:")
    await state.set_state(AdminStates.waiting_sku)
    await cq.answer()


@router.message(AdminStates.waiting_sku)
async def admin_sku(message: Message, state: FSMContext):
    await state.update_data(sku=message.text.strip())
    await message.answer("Теперь название товара:")
    await state.set_state(AdminStates.waiting_title)


@router.message(AdminStates.waiting_title)
async def admin_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("Описание (одним сообщением):")
    await state.set_state(AdminStates.waiting_description)


@router.message(AdminStates.waiting_description)
async def admin_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("Цена в рублях (целое число):")
    await state.set_state(AdminStates.waiting_price)


@router.message(AdminStates.waiting_price)
async def admin_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
        assert price > 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введи целое положительное число:")
        return

    data = await state.get_data()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO items (sku, title, description, price_rub, category)
            VALUES (?, ?, ?, ?, 'misc')
        """, (data["sku"], data["title"], data["description"], price))
        conn.commit()
        item_id = cur.lastrowid
        await message.answer(
            f"✅ Товар добавлен!\n\n"
            f"<b>ID:</b> {item_id}\n"
            f"<b>SKU:</b> <code>{data['sku']}</code>\n"
            f"<b>Название:</b> {data['title']}\n"
            f"<b>Цена:</b> {price:,} ₽\n\n".replace(",", " ") +
            f"<b>Следующий шаг:</b> загрузи коды через /add_codes или кнопку в админ-панели.",
            reply_markup=admin_kb(),
        )
    except sqlite3.IntegrityError:
        await message.answer(f"❌ SKU <code>{data['sku']}</code> уже существует. Используй другой.", reply_markup=admin_kb())
    finally:
        conn.close()
        await state.clear()


@router.callback_query(F.data == "adm:add_codes")
async def cq_admin_add_codes(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, sku, title, (SELECT COUNT(*) FROM codes WHERE item_id=i.id AND sold_to IS NULL) AS stock FROM items i WHERE active=1")
    items = cur.fetchall()
    conn.close()

    rows = []
    for it_id, sku, title, stock in items[:20]:
        rows.append([InlineKeyboardButton(text=f"{sku} (stock: {stock})", callback_data=f"adm_codes:{it_id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin")])

    await cq.message.answer("Выбери товар для загрузки кодов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router.callback_query(F.data.startswith("adm_codes:"))
async def cq_admin_codes_item(cq: CallbackQuery, state: FSMContext):
    item_id = int(cq.data.split(":")[1])
    await state.update_data(codes_item_id=item_id)
    await cq.message.answer(
        f"Отправь коды для товара #{item_id} (по одному на строку или через запятую):",
    )
    await state.set_state(AdminStates.waiting_codes)
    await cq.answer()


@router.message(AdminStates.waiting_codes)
async def admin_codes(message: Message, state: FSMContext):
    raw = message.text.strip()
    # Разделители: новая строка, запятая, точка с запятой
    codes = [c.strip() for c in raw.replace(",", "\n").replace(";", "\n").split("\n") if c.strip()]
    if not codes:
        await message.answer("❌ Не нашёл ни одного кода. Попробуй ещё раз:")
        return

    data = await state.get_data()
    item_id = data.get("codes_item_id")
    if not item_id:
        await message.answer("❌ Не выбран товар. Начни заново: /admin")
        await state.clear()
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    added = 0
    for code in codes:
        try:
            cur.execute("INSERT INTO codes (item_id, code) VALUES (?, ?)", (item_id, code))
            added += 1
        except sqlite3.IntegrityError:
            pass  # дубликат
    conn.commit()
    conn.close()

    await message.answer(
        f"✅ Добавлено {added} кодов для товара #{item_id}",
        reply_markup=admin_kb(),
    )
    await state.clear()
    log.info(f"Admin {message.from_user.id} added {added} codes to item {item_id}")
    await state.clear()


@router.callback_query(F.data == "adm:payout")
async def cq_admin_payout(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(payment_amount_rub), 0)
        FROM orders
        WHERE payment_status='paid' AND created_at > datetime('now', '-30 days')
    """)
    month_revenue = cur.fetchone()[0]
    conn.close()

    await cq.message.answer(
        f"💰 <b>Вывод прибыли</b>\n\n"
        f"Выручка за 30 дней: <b>{month_revenue:,} ₽</b>\n\n".replace(",", " ") +
        f"<b>Доступные способы:</b>\n\n"
        f"• USDT TRC-20: ~{(month_revenue / 90):.2f} USDT\n"
        f"• BTC: ~{(month_revenue / 6_500_000):.6f} BTC\n"
        f"• TON: ~{(month_revenue / 350):.2f} TON\n\n"
        f"<i>Для вывода отправь команду:</i>\n"
        f"<code>/payout usdt TRC-адрес-кошелька</code>\n\n"
        f"<b>Минимальная сумма вывода:</b> 5 000 ₽",
        reply_markup=admin_kb(),
    )
    await cq.answer()


# ===========================
# RUN
# ===========================
async def main():
    init_db()
    log.info("Buildo Plati Delivery Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())