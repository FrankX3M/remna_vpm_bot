from __future__ import annotations

import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Settings
from ..formatting import account_text, config_text
from ..payments.base import PaymentProvider
from ..services import VPNService, apply_discount
from ..settings_store import PLATFORMS, SettingsStore
from ..utils import fmt_dt

router = Router(name="user")


class PromoInput(StatesGroup):
    waiting_code = State()


# ---------------------------------------------------------------- вспомогательное

def active_providers(providers: dict[str, PaymentProvider], store: SettingsStore) -> dict[str, PaymentProvider]:
    """Провайдеры, включённые в настройках (pay_<name>_enabled)."""
    return {n: p for n, p in providers.items() if store.provider_enabled(n)}


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def _answer(msg: Message, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await msg.answer(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "parse entities" not in str(e):
            raise
        await msg.answer(_strip_tags(text), reply_markup=kb, parse_mode=None)  # админ сломал разметку в тексте


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            msg = str(e)
            if "message is not modified" in msg:
                pass  # повторное нажатие: текст тот же — просто гасим, чтобы не плодить дубли
            elif "parse entities" in msg:
                await target.message.edit_text(_strip_tags(text), reply_markup=kb, parse_mode=None)
            else:
                await _answer(target.message, text, kb)  # сообщение нельзя редактировать (счёт, фото)
        await target.answer()
    else:
        await _answer(target, text, kb)


def back_kb(text: str = "⬅️ Меню", data: str = "menu") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=data)
    return b.as_markup()


def menu_kb(*, trial_available: bool, show_promo: bool, show_ref: bool, has_support: bool,
            is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    sizes: list[int] = []
    if trial_available:
        b.button(text="🎁 Пробный период", callback_data="trial")
        sizes.append(1)
    b.button(text="🛒 Купить / продлить", callback_data="buy")
    sizes.append(1)
    b.button(text="👤 Мой аккаунт", callback_data="account")
    b.button(text="📲 Подключиться", callback_data="connect")
    sizes.append(2)
    extra = 0
    if show_promo:
        b.button(text="🎟 Промокод", callback_data="promo")
        extra += 1
    if show_ref:
        b.button(text="👥 Друзья", callback_data="ref")
        extra += 1
    if extra:
        sizes.append(extra)
    if has_support:
        b.button(text="💬 Поддержка", callback_data="support")
        sizes.append(1)
    if is_admin:
        b.button(text="🛠 Веб-админка", callback_data="admin_web")
        sizes.append(1)
    b.adjust(*sizes)
    return b.as_markup()


async def show_menu(target: Message | CallbackQuery, svc: VPNService, settings: Settings, store: SettingsStore) -> None:
    uid = target.from_user.id
    user = await svc.get_user(uid)
    text = store.get("welcome_text")
    disc = await svc.get_discount(uid)
    if disc:
        text += (
            f"\n\n💸 <b>Активна скидка {disc.percent}%</b> (<code>{escape(disc.code)}</code>) "
            "— цены в «Купить» уже пересчитаны."
        )
    kb = menu_kb(
        trial_available=bool(user and not user.trial_used and store.get("trial_enabled")),
        show_promo=bool(store.get("promo_enabled") and store.get("menu_show_promo")),
        show_ref=bool(store.get("referral_enabled") and store.get("menu_show_referral")),
        has_support=bool(store.get("support_url")),
        is_admin=uid in settings.admin_id_set,   # обычному пользователю кнопка админки не показывается вообще
    )
    await show(target, text, kb)


# ---------------------------------------------------------------- старт и меню

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, svc: VPNService, settings: Settings,
                    store: SettingsStore, state: FSMContext) -> None:
    await state.clear()
    ref_id = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        ref_id = int(command.args[4:])
    await svc.register(message.from_user.id, message.from_user.username, ref_id)
    await show_menu(message, svc, settings, store)


@router.message(Command("menu"))
async def cmd_menu(message: Message, svc: VPNService, settings: Settings, store: SettingsStore,
                   state: FSMContext) -> None:
    await state.clear()
    await show_menu(message, svc, settings, store)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, svc: VPNService, settings: Settings, store: SettingsStore,
                  state: FSMContext) -> None:
    await state.clear()
    await show_menu(cb, svc, settings, store)


# ---------------------------------------------------------------- пробный период

@router.callback_query(F.data == "trial")
async def cb_trial(cb: CallbackQuery, svc: VPNService, store: SettingsStore) -> None:
    expire = await svc.claim_trial(cb.from_user.id)
    acc = await svc.get_account(cb.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="📲 Как подключиться", callback_data="connect")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    text = (
        f"🎁 Пробный период на {store.get_int('trial_days')} дн. активирован!\n"
        f"Действует до <b>{fmt_dt(expire)}</b>.\n\n" + config_text(svc.sub_url(acc.remna))
    )
    await show(cb, text, b.as_markup())


# ---------------------------------------------------------------- покупка

def _price_label(plan, providers: dict[str, PaymentProvider], percent: int = 0) -> str:
    parts = []
    for p in providers.values():
        amount = plan.price(p.currency)
        if amount:
            parts.append(p.format_price(apply_discount(amount, percent) if percent else amount))
    return " / ".join(parts)


@router.callback_query(F.data == "buy")
async def cb_buy(cb: CallbackQuery, svc: VPNService, store: SettingsStore,
                 providers: dict[str, PaymentProvider]) -> None:
    providers = active_providers(providers, store)
    disc = await svc.get_discount(cb.from_user.id)
    percent = disc.percent if disc else 0
    b = InlineKeyboardBuilder()
    shown = 0
    for plan in await svc.list_plans():
        label = _price_label(plan, providers, percent)
        if label:
            b.button(text=f"{plan.title} — {label}", callback_data=f"plan:{plan.id}")
            shown += 1
    if store.get("promo_enabled"):
        b.button(text="🎟 Ввести промокод", callback_data="promo")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    text = "🛒 <b>Выберите тариф</b>" if shown else "Тарифы пока не настроены. Загляните позже."
    if disc and shown:
        text += (
            f"\n\n💸 Применена скидка <b>{disc.percent}%</b> по промокоду "
            f"<code>{escape(disc.code)}</code> — цены уже пересчитаны."
        )
    await show(cb, text, b.as_markup())


async def _start_payment(cb: CallbackQuery, bot: Bot, svc: VPNService, store: SettingsStore,
                         provider: PaymentProvider, plan, amount: int) -> None:
    desc = store.get("invoice_description").replace("{days}", str(plan.days))
    disc = await svc.get_discount(cb.from_user.id)
    if disc:
        await provider.start(
            bot, cb.message.chat.id, plan, apply_discount(amount, disc.percent),
            redemption_id=disc.redemption_id, discount=disc.percent, description=desc,
        )
    else:
        await provider.start(bot, cb.message.chat.id, plan, amount, description=desc)


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(cb: CallbackQuery, bot: Bot, svc: VPNService, store: SettingsStore,
                  providers: dict[str, PaymentProvider]) -> None:
    providers = active_providers(providers, store)
    plan = await svc.get_plan(int(cb.data.split(":")[1]))
    if not plan or not plan.is_active:
        await cb.answer("Тариф недоступен", show_alert=True)
        return
    options = [(p, plan.price(p.currency)) for p in providers.values() if plan.price(p.currency)]
    if not options:
        await cb.answer("Для этого тарифа нет способов оплаты", show_alert=True)
        return
    if len(options) == 1:  # один способ оплаты — сразу счёт
        await cb.answer()
        provider, amount = options[0]
        await _start_payment(cb, bot, svc, store, provider, plan, amount)
        return
    disc = await svc.get_discount(cb.from_user.id)
    b = InlineKeyboardBuilder()
    for provider, amount in options:
        shown_amount = apply_discount(amount, disc.percent) if disc else amount
        b.button(text=f"{provider.title} — {provider.format_price(shown_amount)}", callback_data=f"pay:{plan.id}:{provider.name}")
    b.button(text="⬅️ Назад", callback_data="buy")
    b.adjust(1)
    await show(cb, f"Способ оплаты для «{escape(plan.title)}»:", b.as_markup())


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery, bot: Bot, svc: VPNService, store: SettingsStore,
                 providers: dict[str, PaymentProvider]) -> None:
    providers = active_providers(providers, store)
    _, plan_id, provider_name = cb.data.split(":")
    plan = await svc.get_plan(int(plan_id))
    provider = providers.get(provider_name)
    amount = plan.price(provider.currency) if plan and provider else None
    if not plan or not plan.is_active or not provider or not amount:
        await cb.answer("Недоступно", show_alert=True)
        return
    await cb.answer()
    await _start_payment(cb, bot, svc, store, provider, plan, amount)


# ---------------------------------------------------------------- аккаунт (статус + ссылка + действия)

@router.callback_query(F.data.in_({"account", "config"}))   # «config» — кнопка из старых сообщений
async def cb_account(cb: CallbackQuery, svc: VPNService, store: SettingsStore) -> None:
    acc = await svc.get_account(cb.from_user.id)
    url = svc.sub_url(acc.remna)
    text = account_text(svc, acc)
    if url:
        text += "\n\n" + config_text(url)
    b = InlineKeyboardBuilder()
    sizes: list[int] = []
    if not acc.remna and not acc.user.trial_used and store.get("trial_enabled"):
        b.button(text="🎁 Пробный период", callback_data="trial")
        sizes.append(1)
    b.button(text="🛒 Продлить" if acc.remna else "🛒 Купить", callback_data="buy")
    b.button(text="📲 Подключиться", callback_data="connect")
    sizes.append(2)
    if acc.remna:
        b.button(text="🔄 Обновить ссылку", callback_data="revoke")
        sizes.append(1)
    b.button(text="⬅️ Меню", callback_data="menu")
    sizes.append(1)
    b.adjust(*sizes)
    await show(cb, text, b.as_markup())


@router.callback_query(F.data == "revoke")
async def cb_revoke(cb: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, обновить", callback_data="revoke_yes")
    b.button(text="Отмена", callback_data="account")
    b.adjust(2)
    await show(
        cb,
        "🔄 <b>Обновить ссылку подписки?</b>\n\nСтарая ссылка перестанет работать, "
        "и её нужно будет заново добавить во всех ваших устройствах. Срок подписки не изменится.",
        b.as_markup(),
    )


@router.callback_query(F.data == "revoke_yes")
async def cb_revoke_yes(cb: CallbackQuery, svc: VPNService) -> None:
    url = await svc.revoke_link(cb.from_user.id)
    await show(cb, "✅ Ссылка обновлена.\n\n" + config_text(url), back_kb("👤 К аккаунту", "account"))


# ---------------------------------------------------------------- подключение по платформам

@router.callback_query(F.data == "connect")
async def cb_connect(cb: CallbackQuery, store: SettingsStore) -> None:
    b = InlineKeyboardBuilder()
    for key, title in PLATFORMS:
        b.button(text=title, callback_data=f"connect:{key}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2, 2, 1, 1)
    await show(cb, store.get("connect_intro"), b.as_markup())


@router.callback_query(F.data.startswith("connect:"))
async def cb_connect_platform(cb: CallbackQuery, svc: VPNService, store: SettingsStore) -> None:
    platform = cb.data.split(":", 1)[1]
    if platform not in dict(PLATFORMS):
        await cb.answer("Неизвестная платформа", show_alert=True)
        return
    acc = await svc.get_account(cb.from_user.id)
    url = svc.sub_url(acc.remna)
    guide = store.get(f"guide_{platform}").replace("{sub_url}", escape(url) if url else "появится после получения подписки")
    b = InlineKeyboardBuilder()
    sizes: list[int] = []
    for title, link in store.apps(platform):
        b.button(text=title, url=link)
        sizes.append(1)
    if url:
        # страница подписки Remnawave сама предлагает «Добавить в приложение» под платформу пользователя
        b.button(text="🔗 Открыть страницу подписки", url=url)
        sizes.append(1)
    else:
        if not acc.user.trial_used and store.get("trial_enabled"):
            b.button(text="🎁 Пробный период", callback_data="trial")
            sizes.append(1)
        b.button(text="🛒 Купить", callback_data="buy")
        sizes.append(1)
    b.button(text="⬅️ К устройствам", callback_data="connect")
    sizes.append(1)
    b.adjust(*sizes)
    await show(cb, guide, b.as_markup())


# ---------------------------------------------------------------- поддержка

def _support_kb(store: SettingsStore) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    url = store.get("support_url")
    if url:
        b.button(text="✉️ Написать в поддержку", url=url)
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, store: SettingsStore) -> None:
    if not store.get("support_url"):
        await cb.answer("Поддержка пока не настроена", show_alert=True)
        return
    await show(cb, store.get("support_text"), _support_kb(store))


@router.message(Command("support"))
async def cmd_support(message: Message, store: SettingsStore) -> None:
    if not store.get("support_url"):
        await message.answer("Поддержка пока не настроена.")
        return
    await _answer(message, store.get("support_text"), _support_kb(store))


# ---------------------------------------------------------------- рефералы

@router.callback_query(F.data == "ref")
async def cb_ref(cb: CallbackQuery, bot: Bot, svc: VPNService, store: SettingsStore) -> None:
    if not store.get("referral_enabled"):
        await cb.answer("Реферальная программа сейчас недоступна", show_alert=True)
        return
    acc = await svc.get_account(cb.from_user.id)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{acc.user.id}"
    every = store.get("referral_every_purchase")
    invited_bonus = store.get_int("referral_invited_bonus_days")
    text = (
        "👥 <b>Приглашайте друзей</b>\n\n"
        f"За каждого друга, который совершит <b>{'покупку' if every else 'первую покупку'}</b>, "
        f"вы получите +{store.get_int('referral_bonus_days')} дн. подписки."
        + (f"\nИ друг тоже получит +{invited_bonus} дн. на своей первой покупке." if invited_bonus else "")
        + f"\n\nВаша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {acc.invited}\n"
        f"Совершили покупку (бонус начислен): {acc.rewarded}"
    )
    await show(cb, text, back_kb())


# ---------------------------------------------------------------- промокоды

def _promo_kb(has_discount: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_discount:
        b.button(text="🛒 Купить со скидкой", callback_data="buy")
        b.button(text="❌ Отменить скидку", callback_data="promo_cancel")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "promo")
async def cb_promo(cb: CallbackQuery, svc: VPNService, store: SettingsStore, state: FSMContext) -> None:
    if not store.get("promo_enabled"):
        await cb.answer("Промокоды сейчас недоступны", show_alert=True)
        return
    disc = await svc.get_discount(cb.from_user.id)
    text = store.get("promo_help_text")
    if disc:
        text += f"\n\n💸 <b>Сейчас активна скидка {disc.percent}%</b> (<code>{escape(disc.code)}</code>)."
    await state.set_state(PromoInput.waiting_code)
    await show(cb, text + "\n\n✍️ <b>Отправьте промокод:</b>", _promo_kb(bool(disc)))


@router.callback_query(F.data == "promo_cancel")
async def cb_promo_cancel(cb: CallbackQuery, svc: VPNService, state: FSMContext) -> None:
    await state.clear()
    done = await svc.cancel_discount(cb.from_user.id)
    await show(cb, "Скидка отменена. Промокод можно будет ввести снова." if done else "Активной скидки нет.", back_kb())


async def _redeem_and_reply(message: Message, svc: VPNService, state: FSMContext, raw: str) -> None:
    # при ошибке (VPNError → errors.py покажет текст) остаёмся в режиме ввода: можно сразу исправить опечатку
    res = await svc.redeem_promo(message.from_user.id, raw)
    await state.clear()
    if res.kind == "discount":
        b = InlineKeyboardBuilder()
        b.button(text="🛒 Купить со скидкой", callback_data="buy")
        b.button(text="⬅️ Меню", callback_data="menu")
        b.adjust(1)
        await message.answer(
            f"💸 Промокод принят: скидка <b>{res.value}%</b>.\n"
            "Она применится к следующей покупке — откройте «Купить / продлить», цены уже пересчитаны.",
            reply_markup=b.as_markup(),
        )
        return
    acc = await svc.get_account(message.from_user.id)
    head = "🚀 Пробный период активирован" if res.kind == "trial" else "🎁 Промокод активирован"
    b = InlineKeyboardBuilder()
    b.button(text="📲 Как подключиться", callback_data="connect")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await message.answer(
        f"{head}: <b>+{res.value} дн.</b>\nПодписка действует до <b>{fmt_dt(res.expire_at)}</b>.\n\n"
        + config_text(svc.sub_url(acc.remna)),
        reply_markup=b.as_markup(),
    )


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject, svc: VPNService, store: SettingsStore,
                    state: FSMContext) -> None:
    if not store.get("promo_enabled"):
        await message.answer("Промокоды сейчас недоступны.")
        return
    if not command.args:
        await state.set_state(PromoInput.waiting_code)
        await _answer(message, store.get("promo_help_text") + "\n\n✍️ <b>Отправьте промокод:</b>", _promo_kb(False))
        return
    await _redeem_and_reply(message, svc, state, command.args.split()[0])


@router.message(PromoInput.waiting_code, F.text, ~F.text.startswith("/"))
async def msg_promo_code(message: Message, svc: VPNService, state: FSMContext) -> None:
    await _redeem_and_reply(message, svc, state, message.text)
