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
а не открывать дом — это отдельный этап.

## Важно знать
- Клиент панели писан под Remnawave 2.x (в `06-panel-home.sh` тег `remnawave/backend:2`). При другой версии правьте только `app/remnawave.py`.
- Таблицы создаются автоматически (`create_all`). Для изменений схемы в будущем подключите Alembic.
- Блокировки на пользователя работают внутри одного процесса: запускайте один экземпляр бота.
- Возвраты Stars: `telegram_payment_charge_id` хранится в `payments.external_id`.
- Режим direct зависит от VPN на роутере (включите kill-switch); режим egress — от доступности FIN. Без выхода к Telegram бот недоступен. Платежи не теряются: Stars-оплата проходит на стороне
  Telegram, а недовыданные подписки видны в `/pending`.
- База бота — `/opt/vpnbot/data/vpnbot.db`. Включите в бэкапы (этап 9): `sqlite3 vpnbot.db ".backup 'копия.db'"`, не копированием файла на лету.
# remna_vpm_bot
