# autoprsrnft

Telegram-бот, который ищет арбитраж NFT-подарков между **Portals**, **Tonnel** и **MRKT**.

- Каждые ~1.5 с бот смотрит ленту новых лотов на всех трёх биржах.
- Каждый новый лот сравнивается с флором той же коллекции и модели на остальных биржах.
- Если купить здесь и продать там выгодно с учётом комиссий, бот присылает уведомление.
- Можно вручную проверить любой подарок: напиши боту `Plush Pepe | Cozy Galaxy`.

Пример уведомления:

```
⚡️ Арбитраж
🎁 Plush Pepe — Cozy Galaxy #7
Onyx, Moon

🛒 Купить: Tonnel — 110.00 TON
💸 Продать: Portals — флор 150.00, на руки 142.50 TON
🟢 Профит: +32.50 TON (+29.5%)
```

## Как считается профит

| Биржа   | Цена покупки                         | «На руки» при продаже                                  |
|---------|--------------------------------------|--------------------------------------------------------|
| Portals | цена лота                            | цена × (1 − `PORTALS_SELL_FEE`)                        |
| Tonnel  | цена из API × (1 + `TONNEL_BUY_FEE`) | цена из API × (1 − `TONNEL_SELL_FEE`)                  |
| MRKT    | `salePrice`                          | `salePriceWithoutFee` из API (иначе `MRKT_SELL_FEE`)   |

Цена продажи — это текущий флор на бирже, то есть бот предполагает, что ты выставишься по флору.
Комиссии площадки меняют: сверь их с маркетами и при необходимости поправь в `.env`.

## Что нужно

1. **Бот**: создай его у [@BotFather](https://t.me/BotFather) и возьми токен.
2. **User-аккаунт Telegram**: у бирж нет публичного API. Бот заходит в их мини-приложения от имени твоего аккаунта, как если бы ты открыл их сам. Получи `api_id` и `api_hash` на <https://my.telegram.org> → API development tools.
   Лучше завести отдельный аккаунт и один раз открыть в нём @portals, @mrkt и @Tonnel_Network_bot.
3. **VPS на Ubuntu** с Python 3.10+.

## Установка на VPS (Ubuntu)

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd -r -s /usr/sbin/nologin autoprsrnft
sudo git clone https://github.com/eskton/autoprsrnft /opt/autoprsrnft
sudo chown -R autoprsrnft: /opt/autoprsrnft
cd /opt/autoprsrnft
sudo -u autoprsrnft python3 -m venv .venv
sudo -u autoprsrnft .venv/bin/pip install --no-cache-dir -r requirements.txt

sudo -u autoprsrnft cp .env.example .env
sudo -u autoprsrnft nano .env          # заполни BOT_TOKEN, ALLOWED_USERS, API_ID, API_HASH

# Один раз войти в user-аккаунт (спросит телефон и код)
sudo -u autoprsrnft .venv/bin/python login.py

# Запуск как сервис (автостарт и перезапуск при падении)
sudo cp deploy/autoprsrnft.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autoprsrnft
journalctl -u autoprsrnft -f            # логи
```

Обновление: `cd /opt/autoprsrnft && sudo -u autoprsrnft git pull && sudo systemctl restart autoprsrnft`.

## Команды бота

| Команда                         | Что делает                                          |
|---------------------------------|-----------------------------------------------------|
| `Plush Pepe`                    | флоры коллекции на всех биржах и лучшая связка      |
| `Plush Pepe \| Cozy Galaxy`     | то же для конкретной модели                         |
| `/alerts`                       | вкл/выкл автоуведомления                            |
| `/minprofit 2`, `/minpct 7`     | минимальный профит в TON и в %                      |
| `/maxbuy 100`                   | не присылать лоты дороже 100 TON (0 = без лимита)   |
| `/settings`, `/status`          | текущие настройки и состояние бирж                  |

## Скорость

- `POLL_INTERVAL` задаёт паузу между опросами ленты каждой биржи. Если ловишь `429` в `/status` или в логах, увеличь паузу или пропиши `PROXY`.
- Флоры кешируются на `FLOOR_TTL` секунд. Перед отправкой уведомления флор перепроверяется на свежих данных, чтобы не слать ложных сигналов.
- Хороший VPS в Европе (например, в Германии или Нидерландах) даёт меньший пинг до API бирж.

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Структура:

```
app/
  main.py          запуск: бот + сканер
  scanner.py       опрос лент новых лотов, поиск сделок
  arbitrage.py     сравнение цен между биржами, кеш флоров
  markets/         клиенты Portals / Tonnel / MRKT
  tg_auth.py       initData мини-приложений через Telethon
  handlers.py      команды бота (aiogram 3)
  formatting.py    тексты сообщений
  storage.py       настройки пользователей (data/users.json)
login.py           одноразовый вход в user-аккаунт
```

API бирж неофициальные: эндпоинты взяты из открытых клиентов (portalsmp, tonnelmp, amrkt) и могут меняться.
Если какая-то биржа перестала отвечать, смотри `/status` и логи.
