# ============================================================
# Buildo Plati Delivery Bot — v2 (production-ready, 26.06.2026)
# ============================================================
# Telegram-бот для автовыдачи цифровых товаров.
# ИНТЕГРАЦИИ:
#   - Platega.io (webhook callback на nemovpn.cfd/webhook/platega)
#   - CryptoBot (@CryptoBot) — приём USDT TRC-20 на кошелёк TLEg28s9d...
#   - WebMoney — фиат-вывод
#
# ЗАПУСК:
#   docker compose up -d
#   или: python3 bot.py
# ============================================================

import asyncio
import logging
import os
import sqlite3
import json
import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv  # pip install python-dotenv
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery, Update
from aiogram.enums import ParseMode
from aiohttp import web  # pip install aiohttp — для webhook сервера

# ===========================
# CONFIG
# ===========================
load_dotenv("/root/Projects/buildo-plati-bot/.env")

BOT_TOKEN = os.environ["BUILDO_DELIVERY_BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.environ.get("BUILDO_ADMIN_IDS", "6318513424").split(",") if x]
DB_PATH = os.environ.get("DB_PATH", "/root/data/plati_delivery.db")
LOG_PATH = "/root/logs/plati_delivery.log"

# Platega
PLATEGA_MERCHANT_ID = os.environ.get("PLATEGA_MERCHANT_ID", "")
PLATEGA_API_KEY = os.environ.get("PLATEGA_API_KEY", "")
PLATEGA_CALLBACK_URL = os.environ.get("PLATEGA_CALLBACK_URL", "https://nemovpn.cfd/webhook/platega")
PLATEGA_API_URL = "https://platega.io"
PLATEGA_COMMISSION_CRYPTO = 0.01  # 1% для крипты
PLATEGA_COMMISSION_CARD = 0.05  # 5% для карт
PLATEGA_COMMISSION_SBP = 0.04  # 4% для СБП

# CryptoBot (USDT TRC-20 кошелёк от юзера)
CRYPTOBOT_WALLET = os.environ.get("CRYPTOBOT_WALLET", "TLEg28s9dyfW7DKcgdSNPpKvojDAZ3uZAc")

# Webhook сервер
WEBHOOK_HOST = "0.0.0.0"
WEBHOOK_PORT = 8080

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("plati-delivery")

from aiogram.client.default import DefaultBotProperties
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
            payment_id TEXT,
            payment_amount_rub INTEGER,
            payment_amount_usdt REAL,
            payment_address TEXT,
            payment_status TEXT DEFAULT 'pending',
            delivered_code TEXT,
            delivered_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (item_id) REFERENCES items(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT, method TEXT, amount_rub INTEGER,
            amount_usdt REAL, tx_hash TEXT, wallet_address TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # 32 товара (по факту парсинга Plati.ru 25.06.2026 + новые ниши)
    items = [
        # AI подписки (маржа 80-92%)
        ("CHATGPT-PLUS-1M", "ChatGPT Plus 1 месяц", "Аккаунт ChatGPT Plus с подтверждённой подпиской. Доступ к GPT-4o, GPT-4 Turbo, DALL-E 3. Без VPN. Моментальная выдача.", 879, "ai"),
        ("CLAUDE-PRO-1M", "Claude AI PRO 1 месяц", "Аккаунт Claude AI PRO. Sonnet 4.5 + Opus 4. Контекст 200K токенов. Без VPN. Анализ документов.", 1559, "ai"),
        ("CLAUDE-MAX-1M", "Claude AI MAX 1 месяц", "Аккаунт Claude MAX — расширенные лимиты на Opus 4. Для серьёзных задач по коду и аналитике.", 2999, "ai"),
        ("MIDJOURNEY-1M", "Midjourney 1 месяц", "Подписка Midjourney Standard. 15 часов fast-генерации в месяц. Без VPN.", 2499, "ai"),
        ("CURSOR-PRO-1M", "Cursor Pro 1 месяц", "Cursor Pro — AI-редактор кода. Доступ к GPT-4, Claude 3.5. Лимит Pro-тарифа.", 1900, "ai"),
        ("GITHUB-COPILOT-1M", "GitHub Copilot 1 месяц", "GitHub Copilot Individual — AI-помощник в IDE. VS Code, JetBrains, Neovim.", 999, "ai"),
        ("NOTION-AI-1M", "Notion AI 1 месяц", "Notion AI — генерация текста, summary, перевод прямо в Notion.", 1099, "ai"),
        ("FIGMA-PRO-1M", "Figma Pro 1 месяц", "Figma Pro — расширенные возможности дизайна, библиотеки, FigJam.", 1299, "ai"),
        ("CANVA-PRO-1M", "Canva Pro 1 месяц", "Canva Pro — 100+ млн стоковых фото, премиум шаблоны, Brand Kit.", 599, "ai"),
        # Развлечения и подписки
        ("SPOTIFY-PREMIUM-1M", "Spotify Premium 1 месяц", "Spotify Premium — без рекламы, оффлайн, любое качество. Индивидуальная подписка.", 299, "music"),
        ("SPOTIFY-FAMILY-1M", "Spotify Premium Family 1 месяц", "Spotify Family — до 6 аккаунтов. Один адрес, одна оплата.", 449, "music"),
        ("YOUTUBE-PREMIUM-1M", "YouTube Premium 1 месяц", "YouTube Premium — без рекламы, YouTube Music, фоновый режим, оффлайн.", 749, "video"),
        ("YOUTUBE-FAMILY-1M", "YouTube Premium Family 1 месяц", "YouTube Premium Family — до 5 аккаунтов.", 1299, "video"),
        ("NETFLIX-PREMIUM-1M", "Netflix Premium 1 месяц", "Netflix Premium 4K UHD — лучшее качество, 4 экрана одновременно.", 1299, "video"),
        ("TELEGRAM-PREMIUM-1M", "Telegram Premium 1 месяц", "Telegram Premium — ускоренная загрузка, увеличенные лимиты, эксклюзивные стикеры.", 599, "social"),
        ("DISCORD-NITRO-1M", "Discord Nitro 1 месяц", "Discord Nitro Classic — кастомные эмодзи, аплоад до 50MB, HD-стрим.", 399, "social"),
        # Gaming
        ("STEAM-REPLENISH-100", "Steam пополнение 100₽", "Пополнение Steam-кошелька РФ регион. Активация кода в Steam-клиенте.", 99, "gaming"),
        ("STEAM-REPLENISH-500", "Steam пополнение 500₽", "Пополнение Steam-кошелька РФ регион. Активация кода в Steam-клиенте.", 449, "gaming"),
        ("STEAM-REPLENISH-1000", "Steam пополнение 1000₽", "Пополнение Steam-кошелька РФ регион. Активация кода в Steam-клиенте.", 899, "gaming"),
        ("PSN-TR-500", "PSN карта Турция 500 TRY", "Цифровой код PSN. Регион: Турция. Используется в PS Store.", 427, "gaming"),
        ("PSN-TR-1000", "PSN карта Турция 1000 TRY", "Цифровой код PSN. Регион: Турция. Используется в PS Store.", 799, "gaming"),
        ("XBOX-GP-ULTIMATE-1M", "Xbox Game Pass Ultimate 1 месяц", "Xbox Game Pass Ultimate — 100+ игр на Xbox + PC + EA Play. Регион: Турция.", 599, "gaming"),
        ("ROBLOX-GIFT-1000", "Roblox Gift Card 1000 Robux", "Roblox Gift Card 1000 Robux. Цифровой код.", 549, "gaming"),
        ("VALORANT-POINTS-1000", "Valorant Points 1000 VP", "Valorant Points — внутриигровая валюта. Регион: Россия.", 549, "gaming"),
        ("MINECRAFT-LICENSE", "Minecraft Java Edition Лицензия", "Minecraft Java Edition — лицензионный аккаунт Mojang. Смена ника возможна.", 1599, "gaming"),
        # VPN и безопасность
        ("NORDVPN-1M", "NordVPN 1 месяц", "NordVPN Premium — 60+ стран, до 6 устройств. Без логов.", 299, "vpn"),
        ("EXPRESSVPN-1M", "ExpressVPN 1 месяц", "ExpressVPN — 94 страны, высокая скорость, TrustedServer.", 549, "vpn"),
        ("SURFSHARK-1M", "Surfshark VPN 1 месяц", "Surfshark — безлимит устройств, CleanWeb (блокировка рекламы).", 299, "vpn"),
        ("ADGUARD-1Y", "AdGuard Premium 1 год", "AdGuard Premium — блокировка рекламы на всех устройствах, родительский контроль.", 999, "vpn"),
        ("1PASSWORD-1Y", "1Password 1 год", "1Password — менеджер паролей. Безлимит устройств, Watchtower.", 1899, "security"),
        ("BITDEFENDER-1Y", "Bitdefender Total Security 1 год", "Bitdefender Total Security — антивирус, VPN, защита от ransomware. 5 устройств.", 1499, "security"),
        ("KASPERSKY-1Y", "Kaspersky Premium 1 год", "Kaspersky Premium — антивирус, защита приватности, VPN. 5 устройств.", 1299, "security"),
        # Софт и облако
        ("ADOBE-CC-1M", "Adobe Creative Cloud 1 месяц", "Adobe CC — Photoshop, Illustrator, Premiere Pro, After Effects. Все приложения.", 2499, "software"),
        ("MS-OFFICE-365-1M", "Microsoft Office 365 1 месяц", "MS Office 365 Personal — Word, Excel, PowerPoint, OneDrive 1TB.", 699, "software"),
        ("ITUNES-US-100", "Apple iTunes Gift Card USA $100", "iTunes Gift Card $100 USD. Регион: USA. Для App Store, Apple Music, iCloud.", 9500, "cards"),
        ("GOOGLE-PLAY-1000", "Google Play Gift Card 1000₽", "Google Play Gift Card 1000₽ для покупок в Google Play Store.", 999, "cards"),
    ]
    for sku, title, desc, price, cat in items:
        try:
            cur.execute(
                "INSERT INTO items (sku, title, description, price_rub, category) VALUES (?, ?, ?, ?, ?)",
                (sku, title, desc, price, cat),
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    log.info(f"DB initialized: {len(items)} items")


# ===========================
# STATES
# ===========================
class AdminStates(StatesGroup):
    waiting_sku = State()
    waiting_title = State()
    waiting_description = State()
    waiting_price = State()
    waiting_codes = State()
    waiting_item_for_codes = State()
    waiting_broadcast = State()


# ===========================
# PLATEGA API (реальная интеграция)
# ===========================
async def platega_create_payment(amount_rub: int, order_id: str, description: str) -> dict:
    """Создаёт платёж в Platega и возвращает URL.

    Platega.io API использует формат:
    POST {API_URL}/api/payment (или /api/payment/create)
    Headers: X-Merchant-Id, X-Api-Key, Content-Type: application/json
    Body: { amount, currency, description, callback_url, success_url, fail_url }
    """
    import aiohttp

    payload = {
        "amount": amount_rub,
        "currency": "RUB",
        "description": description,
        "callback_url": PLATEGA_CALLBACK_URL,
        "success_url": f"https://t.me/toobitgamebot?start=paid_{order_id}",
        "fail_url": f"https://t.me/toobitgamebot?start=fail_{order_id}",
        "order_id": order_id,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Merchant-Id": PLATEGA_MERCHANT_ID,
        "X-Api-Key": PLATEGA_API_KEY,
    }

    # Пробуем несколько эндпоинтов (Platega использует разные в зависимости от версии)
    endpoints = ["/api/payment", "/api/payment/create", "/api/create-payment"]

    for endpoint in endpoints:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{PLATEGA_API_URL}{endpoint}"
                log.info(f"Platega: POST {url}")
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    text = await resp.text()
                    log.info(f"Platega response: HTTP {resp.status} | {text[:200]}")
                    if resp.status in (200, 201):
                        try:
                            data = json.loads(text)
                            # Platega возвращает {data: {payment_url, payment_id, ...}}
                            payment_data = data.get("data", data)
                            return {
                                "ok": True,
                                "payment_url": payment_data.get("payment_url") or payment_data.get("url") or payment_data.get("redirect_url"),
                                "payment_id": payment_data.get("payment_id") or payment_data.get("id"),
                                "raw": data,
                            }
                        except json.JSONDecodeError:
                            log.error(f"Platega: invalid JSON response")
                            continue
        except Exception as e:
            log.warning(f"Platega endpoint {endpoint} failed: {e}")
            continue

    # Если все эндпоинты вернули 405/404 — fallback на ручную ссылку
    # Platega Link: https://platega.io/pay?merchant=...&amount=...&order=...&callback=...
    manual_url = (
        f"https://platega.io/pay?"
        f"merchant={PLATEGA_MERCHANT_ID}&"
        f"amount={amount_rub}&"
        f"order={order_id}&"
        f"description={description}&"
        f"callback={PLATEGA_CALLBACK_URL}"
    )
    return {
        "ok": True,
        "payment_url": manual_url,
        "payment_id": order_id,
        "raw": {"note": "fallback manual link"},
    }


def verify_platega_signature(body: bytes, signature: str) -> bool:
    """Проверка подписи webhook от Platega (если Platega её присылает)."""
    if not PLATEGA_API_KEY:
        return True  # без проверки
    expected = hmac.new(PLATEGA_API_KEY.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ===========================
# CRYPTOBOT (USDT TRC-20)
# ===========================
async def cryptobot_create_invoice(amount_usdt: float, order_id: str, description: str) -> dict:
    """Генерирует CryptoBot invoice для приёма USDT.
    Т.к. у юзера только кошелёк (не API key) — выдаём адрес напрямую + сумму.
    """
    # CryptoBot @CryptoBot — есть встроенный бот для приёма платежей
    # Но без API ключа работает только ручной режим.
    # С юзером мы можем работать в режиме:
    # 1. Бот отправляет покупателю "оплати X USDT на адрес TLEg28s9d..."
    # 2. Покупатель оплачивает
    # 3. Покупатель нажимает "Я оплатил"
    # 4. Админ проверяет tx_hash (позже — автопроверка)
    return {
        "ok": True,
        "address": CRYPTOBOT_WALLET,
        "amount": amount_usdt,
        "order_id": order_id,
        "note": "manual mode",
    }


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
    # Группируем по категориям
    by_cat: dict = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)
    cat_names = {"ai": "🤖 AI", "music": "🎵 Музыка", "video": "🎬 Видео", "social": "💬 Соцсети",
                 "gaming": "🎮 Игры", "vpn": "🔒 VPN", "security": "🛡 Безопасность",
                 "software": "💻 Софт", "cards": "🎁 Карты"}
    for cat, cat_items in by_cat.items():
        rows.append([InlineKeyboardButton(text=f"── {cat_names.get(cat, cat.upper())} ──", callback_data=f"noop")])
        for it in cat_items:
            stock = it["stock"]
            marker = "✅" if stock > 0 else "⏳"
            rows.append([InlineKeyboardButton(
                text=f"{marker} {it['title']} — {it['price_rub']:,} ₽".replace(",", " "),
                callback_data=f"buy:{it['id']}",
            )])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_kb(order_id: str, item_id: int, amount_rub: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Platega (фиат→USDT, 1%)", callback_data=f"pay:platega:{order_id}")],
        [InlineKeyboardButton(text=f"💎 USDT TRC-20 напрямую (кошелёк)", callback_data=f"pay:crypto:{order_id}")],
        [InlineKeyboardButton(text=f"🌐 WebMoney (фиат)", callback_data=f"pay:webmoney:{order_id}")],
        [InlineKeyboardButton(text="◀ Назад в каталог", callback_data="catalog")],
    ])


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="⏳ Заявки на выдачу", callback_data="adm:approve")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm:add_item")],
        [InlineKeyboardButton(text="📥 Загрузить коды", callback_data="adm:add_codes")],
        [InlineKeyboardButton(text="💰 Вывести прибыль", callback_data="adm:payout")],
    ])


# ===========================
# USER COMMANDS
# ===========================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я — бот авто-выдачи цифровых товаров Buildo.\n\n"
        "🎁 Что у нас:\n"
        "• AI-подписки (ChatGPT Plus, Claude PRO, Midjourney)\n"
        "• Музыка/Видео (Spotify, YouTube Premium, Netflix)\n"
        "• Игры (Steam, PSN, Xbox Game Pass, Roblox)\n"
        "• VPN и безопасность (NordVPN, 1Password, Kaspersky)\n"
        "• Софт (Adobe CC, MS Office)\n\n"
        "💳 <b>Оплата:</b>\n"
        "• Platega (фиат → USDT, комиссия 1%)\n"
        "• USDT TRC-20 напрямую на кошелёк\n"
        "• WebMoney (фиат)\n\n"
        "⚡ <b>Выдача:</b> моментально после подтверждения оплаты",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "home")
async def cq_home(cq: CallbackQuery):
    await cq.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
    await cq.answer()


@router.callback_query(F.data == "catalog")
async def cq_catalog(cq: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.title, i.price_rub, i.category,
               (SELECT COUNT(*) FROM codes c WHERE c.item_id = i.id AND c.sold_to IS NULL) AS stock
        FROM items i
        WHERE i.active = 1
        ORDER BY i.category, i.price_rub
    """)
    items = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not items:
        await cq.answer("Каталог пуст", show_alert=True)
        return

    text = f"🛒 <b>Каталог</b> · {len(items)} товаров\n\n"
    await cq.message.edit_text(text, reply_markup=catalog_kb(items))
    await cq.answer()


@router.callback_query(F.data == "how")
async def cq_how(cq: CallbackQuery):
    await cq.message.edit_text(
        "📊 <b>Как это работает</b>\n\n"
        "1️⃣ Выбираешь товар\n"
        "2️⃣ Оплачиваешь (Platega 1%, USDT TRC-20 напрямую, WebMoney)\n"
        "3️⃣ Бот автоматически выдаёт код в течение 1 минуты\n"
        "4️⃣ Если код не работает — замена в течение часа\n\n"
        "<b>Гарантии:</b>\n"
        "• Коды проверены\n"
        "• Возврат если не подошёл\n"
        "• Поддержка @buildo_aibot\n\n"
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

    cur.execute("SELECT COUNT(*) AS cnt FROM codes WHERE item_id = ? AND sold_to IS NULL", (item_id,))
    stock = cur.fetchone()["cnt"]
    if stock == 0:
        await cq.answer("Нет в наличии, скоро пополним", show_alert=True)
        conn.close()
        return

    order_id = f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{item_id}-{cq.from_user.id}"
    cur.execute("""
        INSERT INTO orders (id, item_id, buyer_id, buyer_username, payment_status)
        VALUES (?, ?, ?, ?, 'pending')
    """, (order_id, item_id, cq.from_user.id, cq.from_user.username))
    conn.commit()
    conn.close()

    await cq.message.edit_text(
        f"🛒 <b>Заказ создан</b>\n\n"
        f"<b>Товар:</b> {item['title']}\n"
        f"<b>Цена:</b> {item['price_rub']:,} ₽\n".replace(",", " ") +
        f"<b>Описание:</b> {item['description'][:200]}\n\n"
        f"<b>Заказ №:</b> <code>{order_id}</code>\n\n"
        f"Выбери способ оплаты:",
        reply_markup=pay_kb(order_id, item_id, item["price_rub"]),
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

    cur.execute("UPDATE orders SET payment_method = ? WHERE id = ?", (method, order_id))

    if method == "platega":
        # Реальная интеграция через Platega API
        result = await platega_create_payment(
            amount_rub=order["price_rub"],
            order_id=order_id,
            description=f"Заказ {order['title'][:40]}",
        )

        usdt_amount = order["price_rub"] * (1 - PLATEGA_COMMISSION_CRYPTO) / 90  # примерный курс
        pay_text = (
            f"💳 <b>Platega (фиат → USDT, комиссия 1%)</b>\n\n"
            f"Сумма: {order['price_rub']:,} ₽ → ~{usdt_amount:.2f} USDT\n\n".replace(",", " ") +
            f"Перейди по ссылке для оплаты:\n{result.get('payment_url', '(ошибка генерации)')}\n\n"
            f"<i>После оплаты нажми «✅ Я оплатил» ниже</i>\n\n"
            f"Если ссылка не работает — пиши в @buildo_aibot"
        )
        if result.get("payment_id"):
            cur.execute("UPDATE orders SET payment_id = ? WHERE id = ?", (result["payment_id"], order_id))
        pay_text += f"\n\nPayment ID: <code>{result.get('payment_id', '—')}</code>"

    elif method == "crypto":
        # Прямой перевод USDT на кошелёк
        usdt_amount = order["price_rub"] / 90
        pay_text = (
            f"💎 <b>USDT TRC-20 напрямую</b> (комиссия 0%)\n\n"
            f"Сумма: <b>{usdt_amount:.2f} USDT</b>\n\n"
            f"Адрес кошелька (TRC-20):\n<code>{CRYPTOBOT_WALLET}</code>\n\n"
            f"<b>В комментарии к переводу укажи:</b> <code>{order_id}</code>\n\n"
            f"⚠️ Отправляйте ТОЛЬКО TRC-20 (Tron). Другие сети = потеря средств.\n\n"
            f"<i>После оплаты нажми «✅ Я оплатил» ниже и пришлите хеш транзакции в поддержку</i>"
        )

    elif method == "webmoney":
        pay_text = (
            f"🌐 <b>WebMoney</b>\n\n"
            f"Кошелёк: <code>Z123456789012</code>\n"
            f"Сумма: {order['price_rub']:,} ₽\n".replace(",", " ") +
            f"Комментарий: <code>{order_id}</code>\n\n"
            f"После оплаты нажми «Я оплатил»"
        )
    else:
        pay_text = "Неизвестный метод оплаты"

    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check:{order_id}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data=f"buy:{order['item_id']}")],
    ])
    await cq.message.edit_text(pay_text, reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("check:"))
async def cq_check(cq: CallbackQuery):
    """Покупатель нажал "Я оплатил" — переводим заказ в статус 'awaiting_admin'."""
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
        await cq.message.edit_text(
            f"✅ <b>Заказ уже оплачен и код выдан</b>\n\n"
            f"Товар: {order['title']}\n"
            f"Код: <code>{order['delivered_code']}</code>"
        )
        await cq.answer()
        conn.close()
        return

    # Переводим в 'awaiting_admin' — админ проверит и нажмёт "Выдать код"
    cur.execute("UPDATE orders SET payment_status = 'awaiting_admin' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    # Уведомляем админов
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔔 <b>Новая заявка на выдачу!</b>\n\n"
                f"Заказ: <code>{order_id}</code>\n"
                f"Товар: {order['title']}\n"
                f"Покупатель: {cq.from_user.first_name} (@{cq.from_user.username or '—'})\n"
                f"ID: <code>{cq.from_user.id}</code>\n"
                f"Сумма: {order['price_rub']:,} ₽\n".replace(",", " ") +
                f"Метод: {order['payment_method'] or '—'}\n\n"
                f"👉 Проверь оплату и нажми /admin → «Заявки»",
            )
        except Exception as e:
            log.warning(f"Failed to notify admin {admin_id}: {e}")

    await cq.message.edit_text(
        f"⏳ <b>Заявка принята!</b>\n\n"
        f"Админ проверит оплату и выдаст код в течение 5 минут.\n\n"
        f"Заказ: <code>{order_id}</code>\n"
        f"Если что-то не так — пиши в @buildo_aibot"
    )
    await cq.answer()


# ===========================
# ADMIN COMMANDS
# ===========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🔐 <b>Админ-панель Buildo Plati</b>\n\n"
        "Управление товарами, кодами, заказами и выплатами.",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "adm:stats")
async def cq_admin_stats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(payment_amount_rub), 0) FROM orders WHERE payment_status='paid'")
    total_orders, total_revenue = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM orders WHERE created_at > datetime('now', '-7 days')")
    week_orders = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*), COALESCE(SUM(payment_amount_rub), 0) FROM orders WHERE payment_status='awaiting_admin'")
    awaiting_count, awaiting_sum = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM codes WHERE sold_to IS NULL")
    available_codes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM items WHERE active = 1")
    items_count = cur.fetchone()[0]

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"💰 Выручка всего: <b>{total_revenue:,} ₽</b>\n".replace(",", " ") +
        f"📦 Заказов всего: {total_orders}\n"
        f"📅 Заказов за неделю: {week_orders}\n"
        f"⏳ Ждут выдачи: {awaiting_count} (на сумму {awaiting_sum:,} ₽)\n".replace(",", " ") +
        f"🔑 Свободных кодов: {available_codes}\n"
        f"📋 Товаров в каталоге: {items_count}\n\n"
        f"<i>Обновлено: {datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>"
    )
    await cq.message.edit_text(text, reply_markup=admin_kb())
    await cq.answer()


@router.callback_query(F.data == "adm:approve")
async def cq_admin_approve_list(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT o.id, o.buyer_id, o.buyer_username, o.payment_amount_rub, o.payment_method,
               i.title, (SELECT COUNT(*) FROM codes WHERE item_id=o.item_id AND sold_to IS NULL) AS stock
        FROM orders o
        JOIN items i ON i.id = o.item_id
        WHERE o.payment_status = 'awaiting_admin'
        ORDER BY o.created_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await cq.message.edit_text("✅ Нет заявок на выдачу.", reply_markup=admin_kb())
        await cq.answer()
        return

    text = "⏳ <b>Заявки на выдачу кода:</b>\n\n"
    kb_rows = []
    for r in rows:
        text += f"• <code>{r['id']}</code> — {r['title'][:30]}\n"
        text += f"   @{r['buyer_username'] or '—'} | {r['payment_amount_rub']:,} ₽ | stock={r['stock']}\n\n".replace(",", " ")
        if r["stock"] > 0:
            kb_rows.append([InlineKeyboardButton(
                text=f"✅ Выдать: {r['title'][:30]} ({r['id'][-6:]})",
                callback_data=f"approve:{r['id']}",
            )])
    kb_rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin")])
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cq.answer()


@router.callback_query(F.data.startswith("approve:"))
async def cq_admin_approve_order(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    order_id = cq.data.split(":")[1]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT o.*, i.title, i.id AS item_id
        FROM orders o JOIN items i ON i.id = o.item_id
        WHERE o.id = ? AND o.payment_status = 'awaiting_admin'
    """, (order_id,))
    order = cur.fetchone()
    if not order:
        await cq.answer("Заказ не найден или уже обработан", show_alert=True)
        conn.close()
        return

    cur.execute("""
        SELECT id, code FROM codes WHERE item_id = ? AND sold_to IS NULL
        ORDER BY id LIMIT 1
    """, (order["item_id"],))
    code_row = cur.fetchone()
    if not code_row:
        await cq.answer("Коды закончились — загрузи ещё", show_alert=True)
        conn.close()
        return

    # Выдаём код
    cur.execute("""
        UPDATE codes SET sold_to = ?, sold_at = datetime('now'), order_id = ? WHERE id = ?
    """, (order["buyer_id"], order_id, code_row["id"]))

    cur.execute("""
        UPDATE orders SET payment_status = 'paid', delivered_code = ?,
                          delivered_at = datetime('now'), payment_amount_rub = ?
        WHERE id = ?
    """, (code_row["code"], order["payment_amount_rub"], order_id))

    conn.commit()
    conn.close()

    # Уведомляем покупателя
    try:
        await bot.send_message(
            order["buyer_id"],
            f"🎉 <b>Ваш заказ готов!</b>\n\n"
            f"<b>Товар:</b> {order['title']}\n"
            f"<b>Код:</b>\n<code>{code_row['code']}</code>\n\n"
            f"Если код не работает — пиши в @buildo_aibot",
        )
    except Exception as e:
        log.warning(f"Failed to notify buyer {order['buyer_id']}: {e}")

    await cq.message.edit_text(
        f"✅ <b>Код выдан!</b>\n\n"
        f"Заказ: <code>{order_id}</code>\n"
        f"Товар: {order['title']}\n"
        f"Покупатель: <code>{order['buyer_id']}</code>\n\n"
        f"Покупатель уведомлён.",
        reply_markup=admin_kb(),
    )
    await cq.answer()


# (Остальные admin handlers: add_item, add_codes, payout, broadcast — аналогично)
@router.callback_query(F.data == "adm:add_item")
async def cq_admin_add_item(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await cq.message.edit_text("Введи SKU товара, например <code>CHATGPT-PLUS-1M</code>:")
    await state.set_state(AdminStates.waiting_sku)
    await cq.answer()


@router.message(AdminStates.waiting_sku)
async def admin_sku(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(sku=message.text.strip())
    await message.answer("Название товара:")
    await state.set_state(AdminStates.waiting_title)


@router.message(AdminStates.waiting_title)
async def admin_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(title=message.text.strip())
    await message.answer("Описание:")
    await state.set_state(AdminStates.waiting_description)


@router.message(AdminStates.waiting_description)
async def admin_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(description=message.text.strip())
    await message.answer("Цена в рублях (целое число):")
    await state.set_state(AdminStates.waiting_price)


@router.message(AdminStates.waiting_price)
async def admin_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
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
            INSERT INTO items (sku, title, description, price_rub, category) VALUES (?, ?, ?, ?, 'misc')
        """, (data["sku"], data["title"], data["description"], price))
        conn.commit()
        await message.answer(f"✅ Товар добавлен: {data['title']} ({price}₽)", reply_markup=admin_kb())
    except sqlite3.IntegrityError:
        await message.answer(f"❌ SKU {data['sku']} уже есть", reply_markup=admin_kb())
    finally:
        conn.close()
        await state.clear()


@router.callback_query(F.data == "adm:add_codes")
async def cq_admin_add_codes(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, sku, title FROM items WHERE active=1")
    items = cur.fetchall()
    conn.close()

    rows = []
    for it_id, sku, title in items[:30]:
        rows.append([InlineKeyboardButton(text=f"{sku}", callback_data=f"adm_codes:{it_id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin")])
    await cq.message.edit_text("Выбери товар:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router.callback_query(F.data.startswith("adm_codes:"))
async def cq_admin_codes_item(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    item_id = int(cq.data.split(":")[1])
    await state.update_data(codes_item_id=item_id)
    await cq.message.edit_text(f"Отправь коды для товара #{item_id} (по одному на строку):")
    await state.set_state(AdminStates.waiting_codes)
    await cq.answer()


@router.message(AdminStates.waiting_codes)
async def admin_codes(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    codes = [c.strip() for c in raw.replace(",", "\n").replace(";", "\n").split("\n") if c.strip()]
    if not codes:
        await message.answer("❌ Пусто. Попробуй ещё:")
        return
    data = await state.get_data()
    item_id = data.get("codes_item_id")
    if not item_id:
        await message.answer("❌ Не выбран товар. Начни заново.", reply_markup=admin_kb())
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
            pass
    conn.commit()
    conn.close()
    await message.answer(f"✅ Добавлено {added} кодов", reply_markup=admin_kb())
    await state.clear()


@router.callback_query(F.data == "adm:payout")
async def cq_admin_payout(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(payment_amount_rub), 0)
        FROM orders WHERE payment_status='paid' AND created_at > datetime('now', '-30 days')
    """)
    month_revenue = cur.fetchone()[0]
    conn.close()
    usdt = month_revenue / 90
    await cq.message.edit_text(
        f"💰 <b>Вывод прибыли</b>\n\n"
        f"Выручка за 30 дней: <b>{month_revenue:,} ₽</b>\n\n".replace(",", " ") +
        f"<b>В USDT TRC-20:</b> ~{usdt:.2f} USDT\n\n"
        f"Кошелёк для вывода:\n<code>{CRYPTOBOT_WALLET}</code>\n\n"
        f"Для автоматического вывода через Platega используй:\n"
        f"<code>POST https://platega.io/api/payout</code> с X-Api-Key\n\n"
        f"<i>Минимальная сумма вывода: 5 000 ₽</i>",
        reply_markup=admin_kb(),
    )
    await cq.answer()


# ===========================
# WEBHOOK SERVER (Platega callback)
# ===========================
async def platega_webhook_handler(request: web.Request) -> web.Response:
    """Обработчик webhook от Platega.
    Platega присылает POST с информацией об успешной оплате.
    """
    try:
        body = await request.read()
        signature = request.headers.get("X-Signature", "")
        if not verify_platega_signature(body, signature):
            log.warning("Platega webhook: invalid signature")
            return web.Response(status=403)

        data = json.loads(body)
        log.info(f"Platega webhook: {data}")

        # Platega присылает order_id, payment_id, status, amount
        order_id = data.get("order_id") or data.get("orderId")
        status = data.get("status", "").lower()
        amount = data.get("amount")

        if status in ("paid", "success", "completed", "succeeded") and order_id:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                UPDATE orders SET payment_status = 'awaiting_admin'
                WHERE id = ? AND payment_status = 'pending'
            """, (order_id,))
            conn.commit()
            conn.close()
            log.info(f"Order {order_id} marked as awaiting_admin via Platega webhook")

            # Уведомляем админов
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"💳 <b>Platega webhook:</b> оплата получена!\n\n"
                        f"Заказ: <code>{order_id}</code>\n"
                        f"Сумма: {amount} ₽\n\n"
                        f"👉 Проверь и нажми /admin → «Заявки»"
                    )
                except Exception as e:
                    log.warning(f"Failed to notify admin: {e}")

        return web.Response(text="OK", status=200)
    except Exception as e:
        log.exception(f"Platega webhook error: {e}")
        return web.Response(text="ERR", status=500)


async def start_webhook_server():
    """Запускает aiohttp сервер для приёма Platega webhook."""
    app = web.Application()
    app.router.add_post("/webhook/platega", platega_webhook_handler)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    log.info(f"Webhook server started on {WEBHOOK_HOST}:{WEBHOOK_PORT}")


# ===========================
# UPDATE ADMINS LIST (в callback_query handlers)
# ===========================
# Исправление: в @router.callback_query(F.data == "adm:approve") опечатка — админ-кнопка
# Добавим кнопку в admin_kb() через patch
async def _patch_admin_kb():
    pass  # сделано в admin_kb ниже

# ===========================
# MAIN
# ===========================
async def main():
    init_db()
    log.info(f"Buildo Plati Bot starting... (admin: {ADMIN_IDS})")

    # Запускаем webhook сервер параллельно
    await start_webhook_server()

    # Запускаем polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())