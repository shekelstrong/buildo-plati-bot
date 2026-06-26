# Buildo Plati Bot

Telegram-бот для автоматической выдачи цифровых товаров (подписки ChatGPT, Claude, Spotify, Steam, VPN и т.д.) с приёмом оплаты через Platega.io и USDT TRC-20.

## Архитектура

- **bot.py** — aiogram 3.13 Telegram-бот с FSM, SQLite, Platega webhook
- **parser_pw.py** — Playwright парсер Plati.ru (32 ниши)
- **Dockerfile + docker-compose.yml** — контейнеризация с cron hourly

## CI/CD

GitHub Actions автоматически:
1. Запускает тесты при каждом push
2. Деплоит на production сервер через SSH
3. Запускает парсер каждый час

## Secrets (заполняются через GitHub Secrets + SSH .env)

- `BUILDO_DELIVERY_BOT_TOKEN` — Telegram bot token от @BotFather
- `BUILDO_ADMIN_IDS` — ID админов через запятую
- `PLATEGA_MERCHANT_ID` — Platega merchant ID
- `PLATEGA_API_KEY` — Platega API key
- `PLATEGA_CALLBACK_URL` — webhook для уведомлений об оплате
- `CRYPTOBOT_WALLET` — USDT TRC-20 кошелёк для прямого приёма

## Деплой на сервер

```bash
# На 108.165.164.85
ssh root@108.165.164.85
cd ~/Projects/buildo-plati-bot
git clone https://github.com/shekelstrong/buildo-plati-bot.git . 2>/dev/null || git pull origin main
# Создать .env с секретами
nano .env
docker compose up -d --build
docker compose logs -f
```

## Локальный запуск

```bash
pip install -r requirements.txt
playwright install chromium
python3 bot.py          # запуск бота + webhook сервера
python3 parser_pw.py    # одноразовый парсинг
python3 parser_pw.py --watch --interval 3600  # режим мониторинга
```# Trigger CI/CD deploy test Fri Jun 26 04:53:16 AM MSK 2026
# CI/CD trigger with correct SSH key Fri Jun 26 05:00:16 AM MSK 2026
