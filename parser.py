# ============================================================
# Buildo Plati.ru Parser
# ============================================================
# Парсер актуальных цен и конкурентов на Plati.ru.
# Запускать по крону каждый час для мониторинга рынка.
#
# Использование:
#   python3 parser.py                       # один проход
#   python3 parser.py --watch --interval 3600  # режим наблюдения
#   python3 parser.py --sku CHATGPT-PLUS-1M    # конкретный товар
# ============================================================

import asyncio
import aiohttp
import json
import sqlite3
import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
import statistics

import os
_default_data = os.environ.get("PARSER_DATA_DIR") or ("/tmp/plati_parser" if os.geteuid() != 0 or not os.path.isdir("/root") else "/root/data")
DB_PATH = os.path.join(_default_data, "plati_parser.db")
LOG_PATH = os.path.join(_default_data, "plati_parser.log")

Path(_default_data).mkdir(parents=True, exist_ok=True)


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} | {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            avg_price REAL,
            min_price REAL,
            max_price REAL,
            median_price REAL,
            sellers_count INTEGER,
            total_sold INTEGER,
            scraped_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            seller_name TEXT,
            seller_rating REAL,
            seller_reviews INTEGER,
            price_rub REAL,
            total_sold INTEGER,
            listing_url TEXT,
            title TEXT,
            scraped_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            avg_market_price REAL,
            our_target_price REAL,
            margin_pct REAL,
            recommendation TEXT,
            detected_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


# Каталог товаров для мониторинга (расширяй по мере добавления)
DEFAULT_QUERIES = [
    "ChatGPT Plus 1 месяц",
    "Claude AI PRO 1 месяц",
    "Claude AI MAX 1 месяц",
    "Midjourney подписка",
    "Spotify Premium",
    "YouTube Premium",
    "Netflix Premium",
    "PSN карта Турция",
    "Xbox Game Pass",
    "Apple iTunes USA",
    "Steam пополнение",
    "NordVPN подписка",
    "Cursor Pro подписка",
    "GitHub Copilot",
    "Adobe Creative Cloud",
    "Notion AI",
    "Zoom Pro",
    "Canva Pro",
]

# Стоимость закупки (наша) — для расчёта маржи. Обновлять вручную после исследования.
COST_OF_GOODS = {
    "ChatGPT Plus 1 месяц": 130,
    "Claude AI PRO 1 месяц": 400,
    "Claude AI MAX 1 месяц": 500,
    "Midjourney подписка": 200,
    "Spotify Premium": 80,
    "YouTube Premium": 100,
    "Netflix Premium": 150,
    "PSN карта Турция": 250,
    "Xbox Game Pass": 200,
    "Apple iTunes USA": 80,
    "Steam пополнение": 20,
    "NordVPN подписка": 100,
    "Cursor Pro подписка": 800,
    "GitHub Copilot": 200,
    "Adobe Creative Cloud": 300,
    "Notion AI": 250,
    "Zoom Pro": 100,
    "Canva Pro": 150,
}


# ================================================================
# Plati.ru — два метода парсинга:
# 1. HTML (search) — основной, простой, без API
# 2. JSON-API (если найдём endpoints) — запасной
# ================================================================

PLATI_SEARCH_URL = "https://plati.market/search"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


async def fetch_search(session: aiohttp.ClientSession, query: str) -> str | None:
    """Поиск товаров на Plati.ru."""
    headers = {
        "User-Agent": USER_AGENTS[0],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    params = {"q": query}

    try:
        async with session.get(
            PLATI_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                log(f"Search '{query}': HTTP {resp.status}")
                return None
            return await resp.text()
    except Exception as e:
        log(f"Search '{query}' failed: {e}")
        return None


def parse_search_results(html: str) -> list[dict]:
    """Парсим HTML результатов поиска."""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Ищем карточки товаров. У Plati структура может меняться,
    # используем несколько селекторов для надёжности.

    # Вариант 1: catalog-item (типичная структура)
    for item in soup.select(".catalog-item, .product-item, .goods-item, [class*='item-card']"):
        try:
            title_el = item.select_one(".catalog-item-title, .product-title, .item-title, h3, h4")
            price_el = item.select_one(".catalog-item-price, .product-price, .item-price, [class*='price']")
            seller_el = item.select_one(".catalog-item-seller, .seller-name, [class*='seller']")
            sold_el = item.select_one(".catalog-item-sold, .sold-count, [class*='sold']")
            rating_el = item.select_one(".catalog-item-rating, .rating, [class*='rating']")
            link_el = item.select_one("a")

            title = title_el.get_text(strip=True) if title_el else ""
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_text)

            if price == 0 or not title:
                continue

            seller = seller_el.get_text(strip=True) if seller_el else "Unknown"
            sold_text = sold_el.get_text(strip=True) if sold_el else "0"
            sold = parse_sold(sold_text)
            rating = parse_rating(rating_el.get_text(strip=True) if rating_el else "0")
            url = ""
            if link_el and link_el.get("href"):
                href = link_el["href"]
                url = href if href.startswith("http") else f"https://plati.market{href}"

            results.append({
                "title": title,
                "price_rub": price,
                "seller_name": seller,
                "seller_rating": rating,
                "total_sold": sold,
                "listing_url": url,
            })
        except Exception as e:
            continue

    # Вариант 2: если первый не сработал — таблица/список
    if not results:
        # Plati иногда выдаёт в виде ссылок
        for link in soup.find_all("a", href=re.compile(r"/item/|/product/|/goods/")):
            href = link.get("href", "")
            text = link.get_text(" ", strip=True)
            price = parse_price(text)
            if price > 0 and len(text) > 5:
                results.append({
                    "title": text[:120],
                    "price_rub": price,
                    "seller_name": "Unknown",
                    "seller_rating": 0,
                    "total_sold": 0,
                    "listing_url": f"https://plati.market{href}" if not href.startswith("http") else href,
                })

    return results


def parse_price(text: str) -> float:
    """Извлекаем цену в рублях из строки вида '879 ₽' или '1 559 RUB'."""
    if not text:
        return 0.0
    text = text.replace("\xa0", " ").replace(",", ".")
    # Ищем число с возможным разделителем
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:₽|руб|RUB|р\.|р\b)", text)
    if m:
        return float(m.group(1))
    # Если цена без символа — последнее число в строке
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if nums:
        return float(nums[-1])
    return 0.0


def parse_sold(text: str) -> int:
    """Извлекаем количество продаж."""
    if not text:
        return 0
    m = re.search(r"(\d+(?:\s?\d+)*)", text.replace("\xa0", ""))
    if m:
        return int(m.group(1).replace(" ", ""))
    return 0


def parse_rating(text: str) -> float:
    """Извлекаем рейтинг продавца."""
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    return 0.0


def compute_stats(results: list[dict]) -> dict:
    """Считаем статистику по списку объявлений."""
    if not results:
        return {
            "avg": 0, "min": 0, "max": 0, "median": 0,
            "sellers_count": 0, "total_sold": 0,
        }

    prices = [r["price_rub"] for r in results if r["price_rub"] > 0]
    if not prices:
        return {
            "avg": 0, "min": 0, "max": 0, "median": 0,
            "sellers_count": 0, "total_sold": 0,
        }

    sellers = set(r["seller_name"] for r in results if r["seller_name"] != "Unknown")
    total_sold = sum(r["total_sold"] for r in results)

    return {
        "avg": round(statistics.mean(prices), 2),
        "min": min(prices),
        "max": max(prices),
        "median": round(statistics.median(prices), 2),
        "sellers_count": len(sellers),
        "total_sold": total_sold,
    }


def analyze_opportunity(query: str, stats: dict) -> dict | None:
    """Анализируем — стоит ли продавать этот товар."""
    cost = COST_OF_GOODS.get(query)
    if not cost or stats["avg"] == 0:
        return None

    target_price = round(stats["avg"] * 0.95)  # чуть ниже среднего для старта
    margin = ((target_price - cost) / target_price) * 100

    if margin < 50:
        rec = f"⚠️ Низкая маржа ({margin:.0f}%). Пропускаем."
    elif margin < 70:
        rec = f"🟡 Средняя маржа ({margin:.0f}%). Осторожно."
    elif stats["total_sold"] < 100:
        rec = f"🟢 Хорошая маржа ({margin:.0f}%), но низкий спрос ({stats['total_sold']} продаж)."
    else:
        rec = f"🚀 ОТЛИЧНАЯ ВОЗМОЖНОСТЬ! Маржа {margin:.0f}%, продаж {stats['total_sold']}, продавцов {stats['sellers_count']}. ВХОДИТЬ!"

    return {
        "avg": stats["avg"],
        "target": target_price,
        "margin_pct": round(margin, 1),
        "recommendation": rec,
    }


async def process_query(session: aiohttp.ClientSession, query: str):
    """Парсим один поисковый запрос."""
    html = await fetch_search(session, query)
    if not html:
        return

    results = parse_search_results(html)
    if not results:
        log(f"⚠️ '{query}': результаты не найдены (возможно, изменилась вёрстка)")
        return

    stats = compute_stats(results)
    opportunity = analyze_opportunity(query, stats)

    # Сохраняем в БД
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO snapshots (query, avg_price, min_price, max_price, median_price, sellers_count, total_sold)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (query, stats["avg"], stats["min"], stats["max"], stats["median"],
          stats["sellers_count"], stats["total_sold"]))

    for r in results[:30]:  # top-30 для анализа
        cur.execute("""
            INSERT INTO listings (query, seller_name, seller_rating, seller_reviews, price_rub, total_sold, listing_url, title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (query, r["seller_name"], r["seller_rating"], 0,
              r["price_rub"], r["total_sold"], r["listing_url"], r["title"]))

    if opportunity:
        cur.execute("""
            INSERT INTO opportunities (query, avg_market_price, our_target_price, margin_pct, recommendation)
            VALUES (?, ?, ?, ?, ?)
        """, (query, opportunity["avg"], opportunity["target"],
              opportunity["margin_pct"], opportunity["recommendation"]))

    conn.commit()
    conn.close()

    log(f"✅ '{query}': {len(results)} объявлений | "
        f"avg={stats['avg']}₽ min={stats['min']}₽ max={stats['max']}₽ "
        f"sellers={stats['sellers_count']} sold={stats['total_sold']} | "
        f"{opportunity['recommendation'] if opportunity else 'no recommendation'}")


async def run_once(queries: list[str]):
    """Один проход парсинга."""
    async with aiohttp.ClientSession() as session:
        # 5 параллельных запросов
        sem = asyncio.Semaphore(5)

        async def bounded(q):
            async with sem:
                await process_query(session, q)
                await asyncio.sleep(1.5)  # пауза между запросами

        tasks = [bounded(q) for q in queries]
        await asyncio.gather(*tasks)


def get_top_opportunities(limit: int = 10) -> list[dict]:
    """Топ рекомендаций из БД."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT query, avg_market_price, our_target_price, margin_pct, recommendation, detected_at
        FROM opportunities
        ORDER BY detected_at DESC, margin_pct DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def main():
    p = argparse.ArgumentParser(description="Plati.ru парсер для Buildo")
    p.add_argument("--watch", action="store_true", help="Режим постоянного мониторинга")
    p.add_argument("--interval", type=int, default=3600, help="Интервал в секундах (по умолчанию 1 час)")
    p.add_argument("--query", type=str, help="Парсить один конкретный запрос")
    p.add_argument("--top", type=int, default=5, help="Показать топ-N возможностей")
    p.add_argument("--list", action="store_true", help="Только список топ-возможностей (без парсинга)")
    args = p.parse_args()

    init_db()

    if args.list:
        print("\n🔥 ТОП ВОЗМОЖНОСТЕЙ ДЛЯ ПРОДАЖИ:")
        print("=" * 80)
        for op in get_top_opportunities(args.top):
            print(f"\n{op['recommendation']}")
            print(f"   Запрос: {op['query']}")
            print(f"   Средняя цена: {op['avg_market_price']}₽ | Твоя цена: {op['our_target_price']}₽ | Маржа: {op['margin_pct']}%")
            print(f"   Обновлено: {op['detected_at']}")
        return

    queries = [args.query] if args.query else DEFAULT_QUERIES

    if args.watch:
        log(f"Watch mode: {len(queries)} queries, interval {args.interval}s")
        while True:
            try:
                asyncio.run(run_once(queries))
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"Loop error: {e}")
            import time
            time.sleep(args.interval)
    else:
        log(f"Starting one-pass parse: {len(queries)} queries")
        asyncio.run(run_once(queries))
        log("Done.")

        # Покажем топ возможности
        print("\n🔥 ТОП ВОЗМОЖНОСТЕЙ ПОСЛЕ ПАРСИНГА:")
        print("=" * 80)
        for op in get_top_opportunities(args.top):
            print(f"\n{op['recommendation']}")
            print(f"   Запрос: {op['query']}")
            print(f"   Средняя цена: {op['avg_market_price']}₽ | Твоя цена: {op['our_target_price']}₽ | Маржа: {op['margin_pct']}%")


if __name__ == "__main__":
    main()