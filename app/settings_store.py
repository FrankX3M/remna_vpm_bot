"""
Настройки бота, которые админ меняет в веб-панели (хранятся в БД, таблица settings).

Принципы:
- В .env остаются только секреты и инфраструктура (токены, адреса, пароль БД). Всё «бизнес-поведение» — здесь.
- Нет строки в БД → действует значение по умолчанию. Для старых параметров (TRIAL_DAYS, REFERRAL_BONUS_DAYS,
  TRAFFIC_LIMIT_GB, HWID_LIMIT, SQUAD_UUIDS) значением по умолчанию служит то, что стоит в .env, — обновление
  не меняет поведение существующей установки.
- Чтение (`get`) синхронное и из памяти, запись — через `update`, которая валидирует и сохраняет в БД.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .config import Settings
from .db import SettingRow, utcnow

log = logging.getLogger(__name__)

GROUPS: list[tuple[str, str]] = [
    ("subscription", "Подписка и пробный период"),
    ("referral", "Рефералы"),
    ("promo", "Промокоды"),
    ("payments", "Оплата"),
    ("menu", "Меню и кнопки бота"),
    ("texts", "Тексты бота"),
    ("guides", "Инструкции подключения по платформам"),
    ("system", "Служебное"),
]

PLATFORMS: list[tuple[str, str]] = [
    ("ios", "📱 iPhone / iPad"),
    ("android", "🤖 Android"),
    ("windows", "🖥 Windows"),
    ("macos", "🍎 macOS"),
    ("linux", "🐧 Linux"),
]

WELCOME_DEFAULT = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "Быстрый и стабильный VPN. Получите пробный период, оформите подписку "
    "и приглашайте друзей — за каждого купившего друга вы получаете бонусные дни."
)

PROMO_HELP_DEFAULT = (
    "🎟 <b>Промокод</b>\n\n"
    "Отправьте промокод <b>одним сообщением</b> — он сработает сразу. "
    "Регистр букв не важен. Можно и командой: <code>/promo КОД</code>.\n\n"
    "<b>Что может дать промокод</b>\n"
    "🎁 <b>Бесплатные дни</b> — подписка продлевается на указанное число дней (от конца текущей, если она активна).\n"
    "💸 <b>Скидка</b> — на ближайшую покупку: откройте «Купить / продлить», цены уже пересчитаны. "
    "Скидка одна за раз: новый промокод заменяет прежнюю неиспользованную. Её можно отменить кнопкой ниже.\n"
    "🚀 <b>Пробный период</b> — бесплатные дни для тех, кто ещё не брал пробный период и не покупал подписку. "
    "Заменяет обычный пробный период.\n\n"
    "<b>Правила</b>\n"
    "• У промокода может быть лимит общего числа активаций и срок действия — если они вышли, код не сработает.\n"
    "• Обычно один код можно применить один раз на аккаунт.\n"
    "• Некоторые коды — только для новичков (ещё не покупали) или только для постоянных клиентов (уже покупали).\n"
    "• После нескольких неверных вводов подряд ввод ненадолго блокируется."
)

CONNECT_INTRO_DEFAULT = (
    "📲 <b>Как подключиться</b>\n\n"
    "Выберите своё устройство — покажу, какое приложение поставить и как добавить подписку. "
    "Весь процесс занимает пару минут."
)

SUPPORT_TEXT_DEFAULT = "💬 <b>Поддержка</b>\n\nЕсли что-то не работает или есть вопрос — напишите нам, ответим как можно скорее."

_GUIDE_TAIL = (
    "\n\n<b>Ваша ссылка подписки</b> (нажмите, чтобы скопировать):\n<code>{sub_url}</code>"
)

GUIDE_DEFAULTS: dict[str, str] = {
    "ios": (
        "📱 <b>iPhone / iPad</b>\n\n"
        "1. Установите приложение <b>Happ</b> из App Store (кнопка ниже).\n"
        "2. Нажмите «Открыть страницу подписки» ниже — на ней есть кнопка «Добавить в приложение». "
        "Либо скопируйте ссылку ниже и в Happ нажмите «+» → «Вставить из буфера».\n"
        "3. Выберите сервер и включите VPN." + _GUIDE_TAIL
    ),
    "android": (
        "🤖 <b>Android</b>\n\n"
        "1. Установите приложение <b>Happ</b> из Google Play (кнопка ниже).\n"
        "2. Нажмите «Открыть страницу подписки» ниже и добавьте подписку в приложение. "
        "Либо скопируйте ссылку ниже и в Happ нажмите «+» → «Вставить из буфера».\n"
        "3. Выберите сервер и включите VPN." + _GUIDE_TAIL
    ),
    "windows": (
        "🖥 <b>Windows</b>\n\n"
        "1. Скачайте и установите <b>Happ</b> для Windows (кнопка ниже).\n"
        "2. Откройте страницу подписки (кнопка ниже) и нажмите «Добавить в приложение». "
        "Либо в Happ нажмите «+» и вставьте ссылку ниже.\n"
        "3. Выберите сервер и включите VPN." + _GUIDE_TAIL
    ),
    "macos": (
        "🍎 <b>macOS</b>\n\n"
        "1. Установите <b>Happ</b> (кнопка ниже).\n"
        "2. Откройте страницу подписки (кнопка ниже) и нажмите «Добавить в приложение». "
        "Либо в Happ нажмите «+» и вставьте ссылку ниже.\n"
        "3. Выберите сервер и включите VPN." + _GUIDE_TAIL
    ),
    "linux": (
        "🐧 <b>Linux</b>\n\n"
        "1. Скачайте <b>Happ</b> для Linux со страницы загрузки (кнопка ниже).\n"
        "2. В приложении нажмите «+» и вставьте ссылку подписки ниже.\n"
        "3. Выберите сервер и включите VPN." + _GUIDE_TAIL
    ),
}

APPS_DEFAULTS: dict[str, str] = {
    "ios": "⬇️ Happ в App Store | https://apps.apple.com/app/happ-proxy-utility/id6504287215",
    "android": "⬇️ Happ в Google Play | https://play.google.com/store/apps/details?id=com.happproxy",
    "windows": "⬇️ Скачать Happ | https://www.happ.su/main",
    "macos": "⬇️ Happ в App Store | https://apps.apple.com/app/happ-proxy-utility/id6504287215",
    "linux": "⬇️ Скачать Happ | https://www.happ.su/main",
}

INVOICE_DESC_DEFAULT = "Подписка VPN на {days} дн."


@dataclass(frozen=True)
class Def:
    key: str
    group: str
    label: str
    kind: str                     # int | bool | str | text | csv
    default: Any
    help: str = ""
    min: int | None = None
    max: int | None = None
    env: str | None = None        # атрибут Settings (.env), который служит значением по умолчанию
    html: bool = False            # текст идёт в Telegram с parse_mode=HTML — проверяем разметку
    apps: bool = False            # формат «Название | https://url» по строкам
    placeholders: str = ""        # подсказка про {переменные} в тексте


REGISTRY: list[Def] = [
    # --- подписка и пробный период
    Def("trial_enabled", "subscription", "Пробный период включён", "bool", True,
        "Если выключить, кнопка «Пробный период» пропадёт, а промокоды типа «пробный» продолжат работать."),
    Def("trial_days", "subscription", "Длительность пробного периода, дней", "int", 30, min=1, max=365, env="trial_days"),
    Def("trial_only_before_purchase", "subscription", "Пробный — только до первой покупки", "bool", True,
        "Если выключить, пробный можно взять и после оплаты (один раз на аккаунт)."),
    Def("traffic_limit_gb", "subscription", "Лимит трафика, ГБ в месяц (0 = без лимита)", "int", 0, min=0, max=100000,
        env="traffic_limit_gb", help="Применяется к НОВЫМ пользователям панели; существующим меняется в самой панели."),
    Def("hwid_limit", "subscription", "Лимит устройств (0 = не задавать)", "int", 0, min=0, max=100, env="hwid_limit",
        help="Применяется к новым пользователям панели."),
    Def("squad_uuids", "subscription", "UUID внутренних сквадов (через запятую)", "csv", "", env="squad_uuids",
        help="Какие ноды получают новые пользователи. Без сквадов у новых пользователей не будет доступа к нодам."),
    # --- рефералы
    Def("referral_enabled", "referral", "Реферальная программа включена", "bool", True,
        "Выключение скрывает кнопку «Пригласить друзей» и прекращает начисление бонусов."),
    Def("referral_bonus_days", "referral", "Бонус пригласившему, дней", "int", 30, min=0, max=3650, env="referral_bonus_days"),
    Def("referral_invited_bonus_days", "referral", "Бонус приглашённому, дней (на его первой покупке)", "int", 0,
        min=0, max=3650, help="0 = приглашённый ничего не получает."),
    Def("referral_every_purchase", "referral", "Начислять за каждую покупку приглашённого", "bool", False,
        "По умолчанию бонус только за ПЕРВУЮ покупку."),
    # --- промокоды
    Def("promo_enabled", "promo", "Промокоды включены", "bool", True, "Выключение скрывает кнопку и команду /promo."),
    Def("promo_max_fails", "promo", "Неверных вводов до блокировки", "int", 8, min=1, max=100,
        help="Защита от перебора кодов."),
    Def("promo_fail_window_min", "promo", "Окно блокировки, минут", "int", 10, min=1, max=1440),
    # --- оплата
    Def("pay_stars_enabled", "payments", "Оплата Telegram Stars включена", "bool", True),
    Def("invoice_description", "payments", "Описание в счёте", "str", INVOICE_DESC_DEFAULT,
        "Telegram ограничивает описание 255 символами.", placeholders="{days} — срок тарифа в днях"),
    # --- меню
    Def("menu_show_promo", "menu", "Кнопка «Промокод» в меню", "bool", True),
    Def("menu_show_referral", "menu", "Кнопка «Друзья» в меню", "bool", True),
    Def("support_url", "menu", "Ссылка поддержки", "str", "",
        "Пусто = кнопки «Поддержка» нет. Принимаются: https://t.me/name, t.me/name, @name, https://… ."),
    # --- тексты
    Def("welcome_text", "texts", "Приветствие в главном меню", "text", WELCOME_DEFAULT, html=True),
    Def("promo_help_text", "texts", "Инструкция на экране «Промокод»", "text", PROMO_HELP_DEFAULT, html=True),
    Def("connect_intro", "texts", "Заголовок экрана «Подключиться»", "text", CONNECT_INTRO_DEFAULT, html=True),
    Def("support_text", "texts", "Текст экрана «Поддержка»", "text", SUPPORT_TEXT_DEFAULT, html=True),
    # --- служебное
    Def("sync_interval_min", "system", "Синхронизация сроков подписок с панелью, минут (0 = выкл.)", "int", 30,
        min=0, max=1440, help="Нужна для аналитики «активные сейчас / истекают»."),
]
for _p, _title in PLATFORMS:
    REGISTRY.append(Def(f"guide_{_p}", "guides", f"Инструкция: {_title}", "text", GUIDE_DEFAULTS[_p], html=True,
                        placeholders="{sub_url} — ссылка подписки пользователя"))
    REGISTRY.append(Def(f"apps_{_p}", "guides", f"Кнопки приложений: {_title}", "text", APPS_DEFAULTS[_p], apps=True,
                        help="По одной на строку: «Название кнопки | https://ссылка». Пусто = без кнопок."))

BY_KEY: dict[str, Def] = {d.key: d for d in REGISTRY}


# ---------------------------------------------------------------------------------------------- проверки

_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler", "blockquote"}
_VOID = {"br"}


class _TagChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.error: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        if tag not in _ALLOWED_TAGS:
            self.error = f"тег <{tag}> не поддерживается Telegram"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.error = self.error or f"лишний или незакрытый тег </{tag}>"
            return
        self.stack.pop()


def check_tg_html(text: str) -> str | None:
    """None — разметка допустима для Telegram parse_mode=HTML; иначе текст ошибки."""
    c = _TagChecker()
    c.feed(text)
    c.close()
    if c.error:
        return c.error
    if c.stack:
        return f"тег <{c.stack[-1]}> не закрыт"
    # одиночные < и & без экранирования Telegram тоже не принимает
    stripped = re.sub(r"</?[a-zA-Z-]+(?:\s[^>]*)?>", "", text)
    if "<" in stripped or ">" in stripped:
        return "символы < и > в тексте нужно писать как &lt; и &gt;"
    if re.search(r"&(?!(?:amp|lt|gt|quot|#\d+);)", stripped):
        return "символ & нужно писать как &amp;"
    return None


def parse_apps(text: str) -> list[tuple[str, str]]:
    """«Название | https://url» построчно → [(название, url)]. Некорректные строки вызывают ValueError."""
    out: list[tuple[str, str]] = []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        title, sep, url = (x.strip() for x in line.partition("|"))
        if not sep or not title or not re.match(r"^https?://\S+$", url):
            raise ValueError(f"строка {n}: нужен формат «Название | https://ссылка»")
        out.append((title[:64], url))
    return out


def normalize_support_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    if raw.startswith("t.me/"):
        return "https://" + raw
    return raw


def coerce(d: Def, raw: Any) -> Any:
    """Приводит значение из формы/JSON к типу настройки; ValueError с понятным текстом."""
    if d.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes", "да")
    if d.kind == "int":
        try:
            v = int(str(raw).strip())
        except ValueError:
            raise ValueError("нужно целое число") from None
        if d.min is not None and v < d.min:
            raise ValueError(f"не меньше {d.min}")
        if d.max is not None and v > d.max:
            raise ValueError(f"не больше {d.max}")
        return v
    s = str(raw if raw is not None else "").replace("\r\n", "\n").strip()
    if d.kind == "csv":
        items = [x for x in re.split(r"[\s,;]+", s) if x]
        for it in items:
            if not re.match(r"^[0-9a-fA-F-]{8,64}$", it):
                raise ValueError(f"«{it}» не похоже на UUID")
        return ",".join(items)
    if d.key == "support_url":
        s = normalize_support_url(s)
        if s and not re.match(r"^https?://\S+$", s):
            raise ValueError("нужна ссылка вида https://t.me/name или @name")
        return s
    if d.apps:
        parse_apps(s)
        return s
    if d.key == "invoice_description":
        if not s:
            raise ValueError("не может быть пустым")
        if len(s) > 255:
            raise ValueError("не длиннее 255 символов")
        return s
    if d.kind == "text" and not s:
        raise ValueError("не может быть пустым (чтобы вернуть стандартный текст, нажмите «Сбросить»)")
    if len(s) > 3500:
        raise ValueError("слишком длинно (максимум 3500 символов)")
    if d.html:
        err = check_tg_html(s)
        if err:
            raise ValueError(f"ошибка разметки: {err}")
    return s


# ---------------------------------------------------------------------------------------------- хранилище

class SettingsStore:
    def __init__(self, sessionmaker: async_sessionmaker, env: Settings):
        self._sm = sessionmaker
        self._env = env
        self._db: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # --- чтение (синхронное, из памяти)
    def default(self, key: str) -> Any:
        d = BY_KEY[key]
        if d.env:
            v = getattr(self._env, d.env, None)
            if v not in (None, ""):
                return v
        return d.default

    def get(self, key: str) -> Any:
        if key in self._db:
            return self._db[key]
        return self.default(key)

    def is_custom(self, key: str) -> bool:
        return key in self._db

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def provider_enabled(self, name: str) -> bool:
        key = f"pay_{name}_enabled"
        return bool(self.get(key)) if key in BY_KEY else True

    def squads(self) -> list[str]:
        return [x for x in str(self.get("squad_uuids")).split(",") if x]

    def apps(self, platform: str) -> list[tuple[str, str]]:
        try:
            return parse_apps(self.get(f"apps_{platform}"))
        except ValueError:
            log.warning("apps_%s: некорректный формат в БД, кнопки пропущены", platform)
            return []

    # --- загрузка/запись
    async def load(self) -> None:
        async with self._sm() as s:
            rows = (await s.execute(select(SettingRow))).scalars().all()
        fresh: dict[str, Any] = {}
        for r in rows:
            d = BY_KEY.get(r.key)
            if d is None:
                continue  # настройка удалена в новой версии кода
            try:
                fresh[r.key] = coerce(d, r.value)
            except ValueError as e:
                log.warning("Настройка %s в БД некорректна (%s) — использую значение по умолчанию", r.key, e)
        self._db = fresh

    async def update(self, changes: dict[str, Any], actor: str) -> dict[str, str]:
        """Проверяет ВСЕ значения; если есть ошибки — не сохраняет ничего и возвращает {ключ: ошибка}."""
        errors: dict[str, str] = {}
        clean: dict[str, Any] = {}
        for key, raw in changes.items():
            d = BY_KEY.get(key)
            if d is None:
                errors[key] = "неизвестная настройка"
                continue
            try:
                clean[key] = coerce(d, raw)
            except ValueError as e:
                errors[key] = str(e)
        if errors:
            return errors
        async with self._lock:
            async with self._sm() as s:
                for key, val in clean.items():
                    row = await s.get(SettingRow, key)
                    if val == self.default(key):
                        # равно умолчанию — не держим строку, чтобы смена .env/кода подхватывалась
                        if row is not None:
                            await s.delete(row)
                        self._db.pop(key, None)
                        continue
                    if row is None:
                        s.add(SettingRow(key=key, value=val, updated_by=actor[:32], updated_at=utcnow()))
                    else:
                        row.value, row.updated_by, row.updated_at = val, actor[:32], utcnow()
                    self._db[key] = val
                await s.commit()
        return {}

    async def reset(self, key: str) -> None:
        if key not in BY_KEY:
            raise KeyError(key)
        async with self._lock:
            async with self._sm() as s:
                row = await s.get(SettingRow, key)
                if row is not None:
                    await s.delete(row)
                    await s.commit()
            self._db.pop(key, None)

    async def refresh_forever(self, every: float = 30.0) -> None:
        """Подхватывает изменения, сделанные другим процессом (если веб когда-нибудь вынесут отдельно)."""
        while True:
            await asyncio.sleep(every)
            try:
                await self.load()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось обновить настройки из БД")
