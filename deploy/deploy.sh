#!/bin/bash
# Buildo Plati Bot — production deploy script
# Использование:
#   1. Скопируйте этот скрипт на сервер 108.165.164.85
#   2. Создайте .env с реальными секретами (см. .env.example)
#   3. Запустите: ./deploy.sh
#
# После этого:
#   - Бот работает как Docker-контейнер
#   - Webhook сервер слушает на :8080 (нужен nginx для :80/443)
#   - Парсер запускается по cron каждый час
#
# Требования:
#   - Docker + docker-compose
#   - nginx (для проксирования :80/443 → :8080)
#   - certbot (Let's Encrypt для HTTPS)
#   - systemd (для автозапуска)

set -e

echo "=== Buildo Plati Bot Deploy ==="

cd "$(dirname "$0")"

# 1. Проверяем .env
if [ ! -f .env ]; then
  echo "ERROR: .env not found!"
  echo "Создайте .env по шаблону .env.example с реальными значениями:"
  echo "  BUILDO_DELIVERY_BOT_TOKEN=..."
  echo "  PLATEGA_MERCHANT_ID=..."
  echo "  PLATEGA_API_KEY=..."
  echo "  PLATEGA_CALLBACK_URL=https://nemovpn.cfd/webhook/platega"
  echo "  CRYPTOBOT_WALLET=T..."
  echo "  BUILDO_ADMIN_IDS=6318513424"
  exit 1
fi

# 2. Создаём директории
mkdir -p data logs

# 3. Останавливаем старое
echo "Stopping old container..."
docker compose down 2>/dev/null || true

# 4. Собираем образ
echo "Building image..."
docker compose build --no-cache

# 5. Запускаем
echo "Starting container..."
docker compose up -d

# 6. Health check
sleep 10
echo "Health check:"
curl -sf http://localhost:8080/health && echo " ← OK"

# 7. Устанавливаем systemd service
if [ -d /etc/systemd/system ]; then
  cp deploy/buildo-bot.service /etc/systemd/system/buildo-bot.service
  systemctl daemon-reload
  systemctl enable buildo-bot.service
  echo "systemd service installed and enabled"
fi

# 8. Cron для парсера (если не через GitHub Actions)
echo "Setting up hourly parser cron..."
CRON_LINE="0 * * * * cd $(pwd) && /usr/bin/docker compose run --rm bot python3 parser_pw.py --max-pages 2 >> /root/logs/parser_cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v "parser_pw.py"; echo "$CRON_LINE") | crontab -
echo "Cron installed"

echo ""
echo "=== Deploy complete ==="
echo "Проверить: docker compose ps && docker compose logs -f --tail 50"
echo "Health: curl http://localhost:8080/health"
echo "Bot username: $(grep BUILDO_DELIVERY_BOT_TOKEN .env | head -1 | cut -d= -f2 | head -c 12)..."