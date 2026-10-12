# VPN-бот (aiogram 3 + Remnawave) — развёртывание на домашнем сервере

## Где запускать и почему
Бот ставится на **домашний сервер рядом с панелью** (каталог `/opt/vpnbot`, скрипт `07-bot-home.sh`).

- К панели бот ходит по docker-сети `remnawave-network` напрямую (`http://remnawave:3000`): новые правила ACL
  и туннель не нужны, API-токен с правами админа никогда не покидает дом.
- Бот работает через long polling — только исходящие соединения; порты на роутере по-прежнему не пробрасываются.
- VPS RU не нагружается (708 МиБ RAM уже заняты хабом, Caddy, subscription-page и нодой) и не становится ещё одной
  точкой, где лежат токен панели и база платежей.
- Если дом выключен, бот всё равно бесполезен (выдавать подписки некому), так что отдельного резервирования не требует.

**Telegram в РФ заблокирован**, нужен западный выход. Два режима, `07-bot-home.sh` выбирает сам:

- **direct (основной)** — домашний сервер подключён к сети/Wi-Fi роутера, на котором настроен VPN. Бот ходит в Telegram напрямую.
  Туннель AmneziaWG не мешает: у дома `AllowedIPs = 10.66.0.0/24`, остальной трафик идёт мимо него, а к панели бот обращается по
  docker-сети, не через туннель. Исключать бота из туннеля и делать для него второй AWG-конфиг не нужно.
- **egress (запасной)** — контейнер `tg-egress`: SOCKS5 → VLESS Reality → нода FIN. Включается флагом `--vless 'vless://…'`
  (ссылка прямого входа на FIN, лучше от отдельного пользователя `bot-egress`).

## Быстрый старт
```bash
# на домашнем сервере, от root, после 06-panel-home.sh; сервер уже в сети роутера с VPN
./07-bot-home.sh
# запасной вариант без VPN на роутере (нужна нода FIN с inbound Reality):
./07-bot-home.sh --vless 'vless://UUID@fin-xxx.1rtp.ru:443?type=tcp&security=reality&pbk=...&sid=...&sni=...&flow=xtls-rprx-vision#bot-egress'
# скрипт спросит BOT_TOKEN, ADMIN_IDS, токен панели и SQUAD_UUIDS (или впишите их в /opt/vpnbot/.env)
```
Токен панели: Settings → API Tokens (отдельный от токена subscription-page). `SQUAD_UUIDS` — внутренние сквады с вашими
нодами, иначе у новых пользователей не будет доступа.

## Обновление: PostgreSQL, веб-админка, настройки в БД

- **БД — PostgreSQL 17** (контейнер `vpnbot-postgres`, схема ведётся Alembic-миграциями, применяются при старте). Старый `data/vpnbot.db` `07-bot-home.sh` переносит сам (с копией и транзакцией), затем переименовывает файл.
- **Веб-админка** — только через туннель, как панель: Caddy панели получает сайт `bot.1rtp.ru` на `10.66.0.2:443`. На устройстве админа: строка в hosts `10.66.0.2 bot.1rtp.ru`; вход — команда `/web` в боте (одноразовая ссылка на 5 минут, сессия 12 часов). Разделы: обзор и аналитика (новички/старички, источники: органика/реферал/промокод, выручка, скидки на руках), пользователи, платежи (повторная выдача), промокоды, тарифы, **настройки**, журнал действий.
- **Настройки** (пробный период, рефералы, промокоды, оплата, кнопки меню, тексты, инструкции по платформам и ссылки приложений, квоты, сквады) хранятся в БД и применяются сразу; значения из `.env` — только стартовые.
- Меню бота: «Мой аккаунт» объединяет подписку; «Подключиться» — инструкции для iOS/Android/Windows/macOS/Linux с кнопкой открытия приложения; «Поддержка» (ссылка в настройках); активная скидка видна в меню; админ-кнопка видна только админам (команды Telegram для обычных пользователей — только `/start`, `/promo`, `/support`).
- Бэкап: `docker exec vpnbot-postgres pg_dump -U vpnbot -Fc vpnbot > vpnbot-$(date +%F).dump` + копия `.env`.
- Тесты: `python smoke_test.py`, `python test_web.py`, `python test_bot.py`; на PostgreSQL — `TEST_DATABASE_URL=postgresql+asyncpg://… python smoke_test.py` (БД будет очищена!).

## Проверки
```bash
python smoke_test.py        # бизнес-логика без Telegram и панели
python test_egress.py       # конвертер vless->xray и работа бота через SOCKS5
docker compose run --rm vpnbot python -m app.selfcheck   # Telegram через прокси + панель + токен
docker compose ps           # vpnbot должен быть healthy (маркер обновляется, только если Telegram отвечает)
```

## Структура
```
app/
  services.py        вся бизнес-логика (без aiogram) — её же будет вызывать веб-версия
  remnawave.py       клиент REST API панели (единственное место, зависящее от версии панели)
  db.py              модели: users, plans, plan_prices, payments, promo_codes, promo_redemptions
  payments/          провайдеры оплаты (base.py — интерфейс, stars.py — Telegram Stars)
  handlers/          user.py (меню), admin.py (команды админа), errors.py
  main.py            запуск: прокси к Telegram, ожидание связи, heartbeat
  selfcheck.py       проверка перед запуском
tools/vless2xray.py  vless://-ссылка -> egress/config.json (только для режима egress)
docker-compose.yml   vpnbot (+ tg-egress по профилю egress)
```

## Админ-команды
`/admin` — список. Тарифы: `/addplan 30 150 1 месяц`, `/setprice`, `/toggleplan`, `/plans`.
Пользователи: `/user`, `/adduser`, `/extend`, `/deluser`. Платежи: `/pending`, `/regrant`, `/stats`.
Промокоды: `/addpromo`, `/promos`, `/promoinfo`, `/setpromo`, `/togglepromo`, `/delpromo`, `/promohelp`.

## Промокоды
Три типа: `days` (бесплатные дни), `discount` (скидка %, на ближайшую покупку), `trial` (пробный период на N дней).
Настройки каждого кода: `limit` (общий лимит активаций), `per_user` (раз на человека), `for=all|new|old`
(новички — ещё нет успешной оплаты / старички — уже покупали), `until=ДД.ММ.ГГГГ` (до конца дня по UTC).
Админ: `/addpromo`, `/promos`, `/promoinfo`, `/setpromo`, `/togglepromo`, `/delpromo`; инструкция — `/promohelp`
и кнопка «🛠 Промокоды: инструкция для админа» в меню админа. Пользователь: «🎟 Промокод» в меню / `/promo КОД`.
Новые таблицы `promo_codes`, `promo_redemptions` создаются сами при старте — существующую БД трогать не нужно.
Скидка хранится как активация в статусе `pending` и расходуется, когда подписка по оплате выдана
(код активации лежит в payload счёта: `plan:ID:rd:RID`). Новый провайдер оплаты обязан: брать сумму со скидкой из
`start(..., amount)`, класть `redemption_id` в payload и передавать его в `svc.process_payment(redemption_id=...)`.

## Как добавить способ оплаты
См. docstring в `app/payments/base.py`. Коротко: класс-провайдер → регистрация в `main.py` →
по подтверждению оплаты вызвать `svc.process_payment(provider=..., external_id=...)`.
Цены в новой валюте админ задаёт через `/setprice ID ЦЕНА ВАЛЮТА`.
Вебхуки платёжек (карты/крипта) потребуют публичного адреса: его нужно вешать на VPS RU (Caddy → туннель → дом),
а не открывать дом — так сделано для Trybit (см. ниже).

## Оплата криптовалютой: Trybit
Код: `app/payments/trybit_api.py` (клиент API v2, проверка JWT, логика POSTBACK; без зависимостей от aiogram/БД) и
`app/payments/trybit.py` (провайдер, таблица заказов `trybit_orders`, кнопка «Проверить оплату», сверка раз в минуту).
Документация Trybit: https://docs.trybit.com/ru.

**Как включить.**
1. В кабинете Trybit создайте проект «На своём сайте» → интеграция **API**. Ключи API KEY / SHOP ID / SECRET KEY — справа в настройках проекта.
2. В `/opt/vpnbot/.env`: `TRYBIT_API_KEY`, `TRYBIT_SHOP_ID`, `TRYBIT_SECRET_KEY` (+ при желании `TRYBIT_CURRENCIES=USDT_TRC20,USDT_TON,TON`, `TRYBIT_INVOICE_MINUTES`). Затем `./07-bot-home.sh` (миграция `0002` создаётся автоматически).
3. Цены в рублях: `/setprice ID СУММА RUB` (или веб-админка → Тарифы). Trybit пересчитает в крипту сам. Включение/выключение способа — «Настройки → Оплата».
4. Поля формы Trybit «Интеграция»: URL успешной оплаты `https://t.me/<бот>?start=paid`, неудачной `https://t.me/<бот>?start=pay_failed`,
   URL уведомлений `https://sub.1rtp.ru/pay/trybit/postback` (Caddy на VPS RU, фрагмент — `deploy/ru-caddy-trybit.snippet`).
5. Тест: в проекте Trybit включён тестовый режим → в `.env` `TRYBIT_ALLOW_TEST=true` → купить тариф **через бота** → в кабинете «Платежи → Тестовые» →
   «Подтвердить счёт без оплаты» → подписка выдана, в «Истории постбэков» ответ 200. После одобрения проекта: `TRYBIT_ALLOW_TEST=false`.

**Как это устроено / безопасность.** POSTBACK (`/trybit/postback` на веб-порту бота, без сессии) принимается только с валидным JWT (HS256, SECRET KEY,
обязателен `exp`); после этого статус и сумма **перезапрашиваются у API Trybit** — телу запроса не верим. Счёт привязан к заказу, сумма и валюта
сверяются с заказом, выдача идемпотентна (захват `UPDATE ... WHERE status='pending'` + уникальность `payments(provider, external_id)`).
`paid`/`overpaid` выдают подписку, `partial` — только уведомление админу и пользователю, `canceled` закрывает заказ. Если панель лежит —
платёж остаётся `granted=False` (`/pending`, `/regrant`). Тестовые счета игнорируются, пока `TRYBIT_ALLOW_TEST=false`.
Для приёма POSTBACK должна быть включена веб-часть (`WEB_ENABLED=true`); без неё платежи подхватит только сверка (раз в минуту).
**Выбор оплаты в боте.** «Купить» → способ оплаты (показываются только включённые в «Настройки → Оплата» и имеющие цену хотя бы у одного тарифа;
один способ — шаг пропускается) → тариф → для крипты выбор монеты/сети из `TRYBIT_CURRENCIES` (дешёвые сети первыми; монета уходит в
Trybit как `add_fields.cryptocurrency`). Новый способ (карты) подключается классом `PaymentProvider` + регистрацией в `main.py` — в меню он появится сам,
когда включён и у тарифов есть цена в его валюте (`options()` у провайдера — необязательный подвыбор вроде монеты).
Тесты: `python test_trybit_core.py` (подпись и логика), `python test_pay_menu.py` (меню выбора оплаты), `python test_trybit.py` (всё вместе на БД; `TEST_DATABASE_URL` — PostgreSQL).

## Важно знать
- Клиент панели писан под Remnawave 2.x (в `06-panel-home.sh` тег `remnawave/backend:2`). При другой версии правьте только `app/remnawave.py`.
- Схема БД: PostgreSQL — миграции Alembic при старте (`app/migrations`, сейчас 0001, 0002 — Trybit, 0003 — монета заказа); SQLite (тесты) — `create_all`.
- Блокировки на пользователя работают внутри одного процесса: запускайте один экземпляр бота.
- Возвраты Stars: `telegram_payment_charge_id` хранится в `payments.external_id`.
- Режим direct зависит от VPN на роутере (включите kill-switch); режим egress — от доступности FIN. Без выхода к Telegram бот недоступен. Платежи не теряются: Stars-оплата проходит на стороне
  Telegram, а недовыданные подписки видны в `/pending`.
- База бота — `/opt/vpnbot/data/vpnbot.db`. Включите в бэкапы (этап 9): `sqlite3 vpnbot.db ".backup 'копия.db'"`, не копированием файла на лету.
