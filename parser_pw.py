# ============================================================
# Buildo Plati.ru Parser — v2 (catalog scraping)
# ============================================================
# Парсит цены и продавцов через навигацию по категориям /games/{slug}/.
# Использует Playwright для обхода DDoS-Guard, BeautifulSoup для парсинга.
#
# Запуск:
#   python3 parser_pw.py --query "ChatGPT Plus"   # одна категория
#   python3 parser_pw.py                          # все категории
#   python3 parser_pw.py --watch --interval 3600  # режим мониторинга
#   python3 parser_pw.py --top 5                  # только топ-возможности
# ============================================================

import asyncio
import json
import sqlite3
import argparse
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

DB_PATH = "/root/data/plati_parser.db"
LOG_PATH = "/root/logs/plati_parser.log"

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)


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
            avg_price REAL, min_price REAL, max_price REAL, median_price REAL,
            sellers_count INTEGER, total_sold INTEGER, listings_count INTEGER,
            scraped_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            seller_name TEXT, seller_rating REAL, price_rub REAL,
            total_sold INTEGER, listing_url TEXT, title TEXT,
            scraped_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT, avg_market_price REAL, our_target_price REAL,
            margin_pct REAL, recommendation TEXT,
            detected_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


# Маппинг: человеческое имя → поисковый запрос на Plati.ru
# Поиск идёт через /search/{query} — универсальный endpoint
CATEGORIES = {
    "ChatGPT Plus": "ChatGPT Plus",
    "Claude AI": "Claude AI",
    "Claude Pro": "Claude Pro",
    "Midjourney": "Midjourney",
    "Spotify Premium": "Spotify Premium",
    "YouTube Premium": "YouTube Premium",
    "PSN карта": "PSN карта",
    "Xbox Game Pass": "Xbox Game Pass",
    "Steam пополнение": "Steam пополнение",
    "Adobe Creative Cloud": "Adobe Creative Cloud",
    "Telegram Premium": "Telegram Premium",
    "NordVPN": "NordVPN",
    "Cursor Pro": "Cursor Pro",
    "GitHub Copilot": "GitHub Copilot",
    "Apple iTunes": "iTunes",
    "Notion AI": "Notion AI",
    # Доп. ниши (06-26)
    "Discord Nitro": "Discord Nitro",
    "Microsoft Office": "Microsoft Office",
    "Roblox": "Roblox",
    "Minecraft": "Minecraft",
    "Riot Points": "Riot Points",
    "Valorant Points": "Valorant Points",
    "Google Play": "Google Play",
    "iTunes Gift Card": "iTunes Gift Card",
    "Figma Pro": "Figma Pro",
    "Canva Pro": "Canva Pro",
    "ExpressVPN": "ExpressVPN",
    "Surfshark": "Surfshark",
    "AdGuard": "AdGuard",
    "1Password": "1Password",
    "Bitdefender": "Bitdefender",
    "Kaspersky": "Kaspersky",
}

# Себестоимость для расчёта маржи
COST_OF_GOODS = {
    "ChatGPT Plus": 130,
    "Claude AI": 400,
    "Midjourney": 200,
    "Spotify Premium": 80,
    "YouTube Premium": 100,
    "PSN карта": 250,
    "Xbox Game Pass": 200,
    "Steam пополнение": 20,
    "Adobe Creative Cloud": 300,
    "Telegram Premium": 150,
    "NordVPN": 100,
    "Cursor Pro": 800,
    "GitHub Copilot": 200,
    "Apple iTunes": 80,
    "Notion AI": 250,
}


class PlatiScraper:
    def __init__(self, browser):
        self.browser = browser

    async def __aenter__(self):
        self.context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800},
            locale='ru-RU',
            timezone_id='Europe/Moscow',
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.context.close()

    async def fetch_category(self, name: str, slug: str, max_pages: int = 3) -> list[dict]:
        """Парсим через /search/{query} — работает для всех ключевых слов.
        slug здесь — поисковый запрос (не обязательно slug категории).
        """
        all_results = []
        query = slug  # теперь slug = поисковый запрос

        for page_num in range(1, max_pages + 1):
            # Поиск работает через /search/{q}?page=N
            encoded = query.replace(' ', '%20')
            url = f"https://plati.market/search/{encoded}"
            if page_num > 1:
                url += f"?page={page_num}"
            page = await self.context.new_page()
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(2500)

                html = await page.content()
                results = self._parse_page(name, html)
                all_results.extend(results)
                log(f"  📄 {name} '{query}' стр.{page_num}: +{len(results)}")

                if len(results) < 5:
                    break

            except Exception as e:
                log(f"  ❌ {name} '{query}' стр.{page_num}: {e}")
            finally:
                await page.close()

            await asyncio.sleep(1.5)

        return all_results

    def _parse_page(self, query_name: str, html: str) -> list[dict]:
        """Парсим HTML одной страницы каталога."""
        soup = BeautifulSoup(html, 'html.parser')
        results = []

        # Plati.ru: карточка = ссылка /itm/{id} + цена внутри или рядом
        for a in soup.select('a[href*="/itm/"]'):
            href = a.get('href', '')
            if not re.search(r'/itm/\d+', href):
                continue

            full_url = href if href.startswith('http') else f"https://plati.market{href}"

            # Название — текст внутри ссылки
            title = a.get_text(' ', strip=True)
            title = re.sub(r'\s+', ' ', title)
            if len(title) < 5 or len(title) > 250:
                continue

            # Ищем цену в ближайшем родителе с .title-bold
            price_el = None
            parent = a.parent
            for _ in range(5):
                if parent is None:
                    break
                p = parent.select_one('.title-bold')
                if p and '₽' in p.get_text():
                    price_el = p
                    break
                parent = parent.parent

            if not price_el:
                continue

            price_text = price_el.get_text(strip=True).replace('\xa0', ' ').replace(' ', '')
            price_match = re.search(r'(\d+)', price_text)
            if not price_match:
                continue
            price = float(price_match.group(1))
            if price <= 0 or price > 100000:
                continue

            # Продавец — в карточке, обычно с префиксом "продавец:" или иконкой
            seller = "Unknown"
            card = a.find_parent(['div', 'article', 'li']) or a
            seller_el = card.select_one('[class*="seller"], [class*="vendor"], [class*="author"]')
            if seller_el:
                seller = seller_el.get_text(strip=True)[:60]

            # Кол-во продаж — обычно "продано N раз" или "1234 продаж"
            sold = 0
            sold_match = re.search(r'(\d[\d\s]*)\s*(?:продаж|продано|sold)', title, re.IGNORECASE)
            if sold_match:
                sold = int(sold_match.group(1).replace(' ', '').replace('\xa0', ''))

            # Дубль-фильтр (некоторые карточки повторяются)
            if any(r["listing_url"] == full_url for r in results):
                continue

            results.append({
                "title": title[:150],
                "price_rub": price,
                "seller_name": seller,
                "seller_rating": 0,
                "total_sold": sold,
                "listing_url": full_url,
            })

        return results


def compute_stats(results):
    if not results:
        return {"avg": 0, "min": 0, "max": 0, "median": 0, "sellers_count": 0, "total_sold": 0, "listings_count": 0}
    prices = [r["price_rub"] for r in results if r["price_rub"] > 0]
    sellers = set(r["seller_name"] for r in results if r["seller_name"] != "Unknown")
    return {
        "avg": round(statistics.mean(prices), 2) if prices else 0,
        "min": min(prices) if prices else 0,
        "max": max(prices) if prices else 0,
        "median": round(statistics.median(prices), 2) if prices else 0,
        "sellers_count": len(sellers),
        "total_sold": sum(r["total_sold"] for r in results),
        "listings_count": len(results),
    }


def analyze_opportunity(query, stats):
    cost = COST_OF_GOODS.get(query)
    if not cost or stats["avg"] == 0:
        return None
    target = round(stats["avg"] * 0.92)  # чуть ниже среднего для старта
    margin = ((target - cost) / target) * 100

    if margin < 40:
        rec = f"⚠️ Низкая маржа ({margin:.0f}%). Пропускаем."
    elif margin < 60:
        rec = f"🟡 Средняя маржа ({margin:.0f}%). Осторожно."
    elif stats["listings_count"] < 30:
        rec = f"🟢 Хорошая маржа ({margin:.0f}%), низкая конкуренция ({stats['listings_count']} объявлений)."
    elif stats["listings_count"] > 200:
        rec = f"🟢 Маржа {margin:.0f}%, ВЫСОКИЙ СПРОС ({stats['listings_count']} объявлений!)."
    else:
        rec = f"🚀 ОТЛИЧНО! Маржа {margin:.0f}%, объявлений {stats['listings_count']}, продавцов {stats['sellers_count']}."
    return {"avg": stats["avg"], "target": target, "margin_pct": round(margin, 1), "recommendation": rec}


def save_results(query, results, stats, opp):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO snapshots (query, avg_price, min_price, max_price, median_price, sellers_count, total_sold, listings_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (query, stats["avg"], stats["min"], stats["max"], stats["median"],
          stats["sellers_count"], stats["total_sold"], stats["listings_count"]))
    for r in results[:50]:
        cur.execute("""
            INSERT INTO listings (query, seller_name, seller_rating, price_rub, total_sold, listing_url, title)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (query, r["seller_name"], r["seller_rating"], r["price_rub"],
              r["total_sold"], r["listing_url"], r["title"]))
    if opp:
        cur.execute("""
            INSERT INTO opportunities (query, avg_market_price, our_target_price, margin_pct, recommendation)
            VALUES (?, ?, ?, ?, ?)
        """, (query, opp["avg"], opp["target"], opp["margin_pct"], opp["recommendation"]))
    conn.commit()
    conn.close()


def get_top_opportunities(limit=10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT query, avg_market_price, our_target_price, margin_pct, recommendation, detected_at
        FROM opportunities
        WHERE detected_at > datetime('now', '-1 day')
        ORDER BY margin_pct DESC, detected_at DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


async def run_once(queries, max_pages=2):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage'],
        )
        async with PlatiScraper(browser) as scraper:
            sem = asyncio.Semaphore(2)

            async def bounded(name):
                async with sem:
                    query_str = CATEGORIES.get(name, name)
                    results = await scraper.fetch_category(name, query_str, max_pages=max_pages)
                    stats = compute_stats(results)
                    opp = analyze_opportunity(name, stats)
                    save_results(name, results, stats, opp)
                    log(f"✅ '{name}': {stats['listings_count']} listings | "
                        f"avg={stats['avg']}₽ min={stats['min']}₽ max={stats['max']}₽ "
                        f"sellers={stats['sellers_count']} | "
                        f"{opp['recommendation'] if opp else '—'}")
                    await asyncio.sleep(2)

            await asyncio.gather(*[bounded(q) for q in queries])

        await browser.close()


def main():
    p = argparse.ArgumentParser(description="Plati.ru парсер (Playwright)")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=int, default=3600)
    p.add_argument("--query", type=str, help="Имя категории или slug")
    p.add_argument("--max-pages", type=int, default=2, help="Страниц на категорию")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--list", action="store_true")
    args = p.parse_args()

    init_db()

    if args.list:
        print("\n🔥 ТОП ВОЗМОЖНОСТЕЙ (за последние сутки):")
        print("=" * 80)
        for op in get_top_opportunities(args.top):
            print(f"\n{op['recommendation']}")
            print(f"   {op['query']} | avg {op['avg_market_price']}₽ → наша {op['our_target_price']}₽ ({op['margin_pct']}% маржа)")
            print(f"   {op['detected_at']}")
        return

    queries = [args.query] if args.query else list(CATEGORIES.keys())

    if args.watch:
        log(f"Watch mode: {len(queries)} categories, {args.interval}s")
        while True:
            try:
                asyncio.run(run_once(queries, args.max_pages))
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"Loop error: {e}")
            import time
            time.sleep(args.interval)
    else:
        log(f"One-pass: {len(queries)} categories, max {args.max_pages} pages each")
        asyncio.run(run_once(queries, args.max_pages))
        log("Done.")
        print("\n🔥 ТОП ВОЗМОЖНОСТЕЙ ПОСЛЕ ПАРСИНГА:")
        print("=" * 80)
        for op in get_top_opportunities(args.top):
            print(f"\n{op['recommendation']}")
            print(f"   {op['query']} | avg {op['avg_market_price']}₽ → наша {op['our_target_price']}₽ ({op['margin_pct']}% маржа)")


if __name__ == "__main__":
    main()