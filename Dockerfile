# ============================================================
# Buildo Plati Bot — запуск через Docker
# ============================================================
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Парсер по крону — каждый час
RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

# Crontab: парсинг каждый час, очистка логов каждый день
RUN echo "0 * * * * cd /app && python3 parser.py >> /var/log/cron-parser.log 2>&1" > /etc/cron.d/plati-parser \
    && echo "0 4 * * * find /root/logs -name '*.log' -mtime +7 -delete" >> /etc/cron.d/plati-parser \
    && chmod 0644 /etc/cron.d/plati-parser \
    && crontab /etc/cron.d/plati-parser

# Запускаем cron + бот
CMD cron && python3 bot.py