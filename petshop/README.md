# petshop — питомцы и донат в Telegram

Telegram-бот с мини-аппом. Человек покупает питомца за крипту, а питомец каждый день приносит бонусы.
Бонусы тратятся на донат в играх (Robux, UC, гемы и т. п.) в этом же магазине.

Пример: питомец за 1 000 ₽ за 30 дней приносит 1 200 ₽ бонусами, это 40 ₽ в день.

## Правила, заложенные в код

- **Питомца покупают только за крипту** через @CryptoBot. За бонусы питомца купить нельзя, иначе бонусы росли бы сами из себя.
- **Бонусы нельзя вывести или передать другому человеку.** Их можно только потратить на товары магазина.
- **Заказ оплачивается** бонусами, криптой или частично тем и другим.
- **Условия питомца фиксируются в момент покупки.** Правки в админке действуют только на новые покупки.
- **Отказ по заказу** возвращает всю его сумму на бонусный баланс покупателя.

### Экономика: проверь до запуска

1 000 ₽ → 1 200 ₽ бонусами — это скидка ~17 % на донат. **Если твоя наценка меньше, каждый питомец в минус.**
Деньги за питомцев — это аванс: их ещё предстоит отдать донатом.
В админке («Статистика → Ещё должен выдать донатом») видно, сколько именно ты сейчас должен.

## Как это выглядит

- **🐾 Питомцы.** Копилка тикает в реальном времени, кнопка «Собрать» переносит накопленное на бонусный баланс. Здесь же покупка новых питомцев.
- **🛒 Магазин.** Донат по играм. При покупке человек вводит ник или ID, можно списать бонусы, остаток оплачивается криптой.
- **👤 Профиль.** Баланс, заказы со статусами, неоплаченные счета, история бонусов.
- **🛠 Админ** (видят только `ADMIN_IDS`): заказы с кнопками «Выполнено» и «Отказ», статистика, товары, питомцы, ручная корректировка бонусов.

Оплаченный заказ приходит админу и в бот, кнопки «Выполнено» и «Отказ» есть прямо в сообщении.
Покупатель получает уведомление, когда заказ выполнен.

## Что нужно

1. **Бот** от [@BotFather](https://t.me/BotFather) — токен.
2. **Crypto Pay.** В [@CryptoBot](https://t.me/CryptoBot) открой «Crypto Pay» → «Create App» и скопируй API Token.
   Чтобы сначала проверить всё в тестовой сети, создай приложение в [@CryptoTestnetBot](https://t.me/CryptoTestnetBot) и поставь `CRYPTOPAY_TESTNET=true`.
3. **Домен с https.** Telegram открывает мини-аппы только по https. Подойдёт бесплатный поддомен:
   зайди на <https://www.duckdns.org>, войди, создай поддомен (например `petshop`) и укажи в поле **current ip** IP своего VPS.
4. **VPS на Ubuntu** с открытыми портами 80 и 443.

## Установка на VPS

Код лежит в папке `petshop/` репозитория `autoprsrnft`, в ветке `claude/pet-shop-bot`.

```bash
# 1. Код (если репозиторий уже склонирован в /opt/autoprsrnft — только fetch + checkout)
cd /opt/autoprsrnft
sudo -u autoprsrnft git fetch origin
sudo -u autoprsrnft git checkout claude/pet-shop-bot
cd petshop

# 2. Зависимости
sudo -u autoprsrnft python3 -m venv .venv
sudo -u autoprsrnft .venv/bin/pip install --no-cache-dir -r requirements.txt

# 3. Настройки
sudo -u autoprsrnft cp .env.example .env
sudo -u autoprsrnft nano .env      # BOT_TOKEN, ADMIN_IDS, WEBAPP_URL, CRYPTOPAY_TOKEN
```

Если репозитория на сервере ещё нет, сначала:
```bash
sudo useradd -r -s /usr/sbin/nologin autoprsrnft
sudo git clone https://github.com/eskton/autoprsrnft /opt/autoprsrnft
sudo chown -R autoprsrnft: /opt/autoprsrnft
```

### HTTPS через Caddy

Caddy сам получает и продлевает сертификат:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

sudo cp /opt/autoprsrnft/petshop/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile         # впиши свой домен
sudo systemctl reload caddy
sudo ufw allow 80,443/tcp 2>/dev/null  # если включён файрвол
```

### Запуск как сервис

```bash
sudo cp /opt/autoprsrnft/petshop/deploy/petshop.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now petshop
journalctl -u petshop -f              # логи
```

Открой бота и нажми `/start`, затем «🐾 Открыть магазин». Кнопка «Магазин» у поля ввода появится сама.
При первом запуске создаются примеры питомцев и товаров с условными ценами. **Поменяй их в админке.**

Обновление: `cd /opt/autoprsrnft && sudo -u autoprsrnft git pull && sudo systemctl restart petshop`.

## Разработка

```bash
cd petshop
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

```
shop/
  main.py       запуск: бот + веб-сервер мини-аппа + проверка оплат
  service.py    вся логика: питомцы, начисления, заказы, возвраты, админка
  db.py         SQLite (data/shop.db), суммы в копейках
  web.py        JSON API для мини-аппа, проверка подписи Telegram (initData)
  cryptopay.py  клиент Crypto Pay API (@CryptoBot)
  payments.py   фоновая проверка счетов: оплачен → выдать, истёк → отменить
  bot.py        /start, уведомления, кнопки «Выполнено» и «Отказ» для админа
  seed.py       примеры питомцев и товаров при первом запуске
webapp/         мини-апп (HTML/CSS/JS без сборки, тема Telegram)
```

База — один файл `data/shop.db`. Безопасный бэкап на ходу (можно повесить на cron раз в день):

```bash
cd /opt/autoprsrnft/petshop && sudo -u autoprsrnft .venv/bin/python -c \
  "import sqlite3; sqlite3.connect('data/shop.db').backup(sqlite3.connect('data/backup.db'))"
```
