# autoprsrnft

Telegram-бот, который ищет выгодные NFT-подарки на маркетах (сейчас — **Portals**).

- Каждые ~1.5 с бот смотрит ленту новых лотов. Каждый новый лот проверяется двумя способами:
  - **по фону.** Лот дешевле флора того же фона в той же коллекции на N % (по умолчанию 10 %).
    Пример: флор Toy Bear на Onyx Black — 57 TON, кто-то выставил за 51, это −11 %.
  - **по модели.** Лот можно перепродать дороже по флору той же модели.
- Флор — самый дешёвый другой такой же лот, сам найденный лот не учитывается.
- Можно вручную проверить любой подарок: `Plush Pepe | Cozy Galaxy` или `Toy Bear фон Onyx Black`.

## Биржи

Сейчас стабильно работает только **Portals**, поэтому по умолчанию бот подключается только к нему (`MARKETS=portals`).
Клиенты Tonnel и MRKT остались в коде. Когда их авторизацию починим, они включаются одной строкой в `.env`:
`MARKETS=portals,tonnel,mrkt`. Тогда флоры считаются по всем включённым биржам сразу,
и бот ищет ещё и связки «купить на одной бирже — продать на другой».

Примеры уведомлений:

```
🎯 Ниже флора фона на 11%
🎁 Toy Bear — Sheriff #4521
Onyx Black, Star

🛒 Купить: Portals — 51.00 TON
📉 Флор фона Onyx Black: 57.00 TON на Portals — лот дешевле на 10.5%
💸 Продать: Portals — флор 57.00, на руки 54.15 TON
🟢 Профит: +3.15 TON (+6.2%)
```

```
⚡️ Арбитраж
🎁 Plush Pepe — Cozy Galaxy #7
Onyx, Moon

🛒 Купить: Portals — 110.00 TON
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
   Лучше завести отдельный аккаунт и один раз открыть в нём @portals (и @mrkt, @Tonnel_Network_bot, если включишь эти биржи).
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
| `Toy Bear фон Onyx Black`       | коллекция на конкретном фоне, флор фона и скидка    |
| `Toy Bear \| Wizard \| Onyx Black` | конкретная модель на конкретном фоне            |
| `/mode фон`, `модель`, `оба`    | что искать в автопоиске (по умолчанию оба)          |
| `/discount 10`                  | на сколько % лот должен быть дешевле флора фона     |
| `/alerts`                       | вкл/выкл автоуведомления                            |
| `/minprofit 2`, `/minpct 7`     | минимальный профит в TON и в % (поиск по модели)    |
| `/maxbuy 100`                   | не присылать лоты дороже 100 TON (0 = без лимита)   |
| `/settings`, `/status`          | текущие настройки и состояние бирж                  |

## Скорость

- `POLL_INTERVAL` задаёт паузу между опросами ленты каждой биржи. Если ловишь `429` в `/status` или в логах, увеличь паузу или пропиши `PROXY`.
- В режиме «оба» на каждый новый лот уходит до 6 запросов к биржам (по 3 на фон и на модель). Если биржи режут запросы, оставь один режим, например `/mode фон`.
- Если на фоне мало лотов (меньше трёх), бот предупреждает: флор может быть случайным, кто-то мог выставить по завышенной цене.
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
