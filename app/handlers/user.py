from __future__ import annotations

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
from ..utils import fmt_dt

router = Router(name="user")

WELCOME = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "Быстрый и стабильный VPN. Получите пробный период, оформите подписку "
    "и приглашайте друзей — за каждого купившего друга вы получаете бонусные дни."
)


class PromoInput(StatesGroup):
    waiting_code = State()


def menu_kb(trial_available: bool, is_admin: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    sizes: list[int] = []
    if trial_available:
        b.button(text="🎁 Пробный период", callback_data="trial")
        sizes.append(1)
    b.button(text="🛒 Купить / продлить", callback_data="buy")
    sizes.append(1)
    b.button(text="👤 Мой аккаунт", callback_data="account")
    b.button(text="🔗 Моя подписка", callback_data="config")
    b.button(text="🔄 Обновить ссылку", callback_data="revoke")
    b.button(text="👥 Пригласить друзей", callback_data="ref")
    b.button(text="🎟 Промокод", callback_data="promo")
    sizes += [2, 2, 1]
    if is_admin:
        b.button(text="🛠 Промокоды: инструкция для админа", callback_data="admin_promo_help")
        sizes.append(1)
    b.adjust(*sizes)
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Меню", callback_data="menu")
    return b.as_markup()


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            # «not modified» — текст тот же (повторное нажатие): просто гасим, чтобы не плодить дубли
            if "message is not modified" not in str(e):
                await target.message.answer(text, reply_markup=kb)  # сообщение нельзя редактировать (счёт, фото)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


async def show_menu(target: Message | CallbackQuery, svc: VPNService, settings: Settings) -> None:
    user = await svc.get_user(target.from_user.id)
    is_admin = target.from_user.id in settings.admin_id_set
    await show(target, WELCOME, menu_kb(trial_available=bool(user and not user.trial_used), is_admin=is_admin))


# ---------------------------------------------------------------- старт и меню

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, svc: VPNService, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    ref_id = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        ref_id = int(command.args[4:])
    await svc.register(message.from_user.id, message.from_user.username, ref_id)
    await show_menu(message, svc, settings)


@router.message(Command("menu"))
async def cmd_menu(message: Message, svc: VPNService, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await show_menu(message, svc, settings)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, svc: VPNService, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await show_menu(cb, svc, settings)


# ---------------------------------------------------------------- пробный период

@router.callback_query(F.data == "trial")
async def cb_trial(cb: CallbackQuery, svc: VPNService, settings: Settings) -> None:
    expire = await svc.claim_trial(cb.from_user.id)
    acc = await svc.get_account(cb.from_user.id)
    text = (
        f"🎁 Пробный период на {settings.trial_days} дн. активирован!\n"
        f"Действует до <b>{fmt_dt(expire)}</b>.\n\n" + config_text(svc.sub_url(acc.remna))
    )
    await show(cb, text, back_kb())


# ---------------------------------------------------------------- покупка

def _price_label(plan, providers: dict[str, PaymentProvider], percent: int = 0) -> str:
    parts = []
    for p in providers.values():
        amount = plan.price(p.currency)
        if amount:
            parts.append(p.format_price(apply_discount(amount, percent) if percent else amount))
    return " / ".join(parts)


@router.callback_query(F.data == "buy")
async def cb_buy(cb: CallbackQuery, svc: VPNService, providers: dict[str, PaymentProvider]) -> None:
    disc = await svc.get_discount(cb.from_user.id)
    percent = disc.percent if disc else 0
    b = InlineKeyboardBuilder()
    shown = 0
    for plan in await svc.list_plans():
        label = _price_label(plan, providers, percent)
        if label:
            b.button(text=f"{plan.title} — {label}", callback_data=f"plan:{plan.id}")
            shown += 1
    b.button(text="🎟 Ввести промокод", callback_data="promo")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    text = "🛒 <b>Выберите тариф</b>" if shown else "Тарифы пока не настроены. Загляните позже."
    if disc and shown:
        text += f"\n\n💸 Применена скидка <b>{disc.percent}%</b> по промокоду <code>{escape(disc.code)}</code> — цены уже пересчитаны."
    await show(cb, text, b.as_markup())


async def _start_payment(cb: CallbackQuery, bot: Bot, svc: VPNService, provider: PaymentProvider, plan, amount: int) -> None:
    disc = await svc.get_discount(cb.from_user.id)
    if disc:
        await provider.start(
            bot, cb.message.chat.id, plan, apply_discount(amount, disc.percent),
            redemption_id=disc.redemption_id, discount=disc.percent,
        )
    else:
        await provider.start(bot, cb.message.chat.id, plan, amount)


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(cb: CallbackQuery, bot: Bot, svc: VPNService, providers: dict[str, PaymentProvider]) -> None:
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
        await _start_payment(cb, bot, svc, provider, plan, amount)
        return
    disc = await svc.get_discount(cb.from_user.id)
    b = InlineKeyboardBuilder()
    for provider, amount in options:
        shown_amount = apply_discount(amount, disc.percent) if disc else amount
        b.button(text=f"{provider.title} — {provider.format_price(shown_amount)}", callback_data=f"pay:{plan.id}:{provider.name}")
    b.button(text="⬅️ Назад", callback_data="buy")
    b.adjust(1)
    await show(cb, f"Способ оплаты для «{plan.title}»:", b.as_markup())


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery, bot: Bot, svc: VPNService, providers: dict[str, PaymentProvider]) -> None:
    _, plan_id, provider_name = cb.data.split(":")
    plan = await svc.get_plan(int(plan_id))
    provider = providers.get(provider_name)
    amount = plan.price(provider.currency) if plan and provider else None
    if not plan or not plan.is_active or not provider or not amount:
        await cb.answer("Недоступно", show_alert=True)
        return
    await cb.answer()
    await _start_payment(cb, bot, svc, provider, plan, amount)


# ---------------------------------------------------------------- промокоды

PROMO_INFO = (
    "🎟 <b>Промокод</b>\n\n"
    "Отправьте промокод <b>одним сообщением</b> — он сработает сразу. "
    "Регистр букв не важен. Можно и командой: <code>/promo КОД</code>.\n\n"
    "<b>Что может дать промокод</b>\n"
    "🎁 <b>Бесплатные дни</b> — подписка продлевается на указанное число дней (от конца текущей, если она активна).\n"
    "💸 <b>Скидка</b> — на ближайшую покупку: откройте «Купить / продлить», цены уже пересчитаны. "
    "Скидка одна за раз: новый промокод заменяет прежнюю неиспользованную. "
    "Её можно отменить кнопкой ниже.\n"
    "🚀 <b>Пробный период</b> — N дней бесплатно для тех, кто ещё не брал пробный период и не покупал подписку. "
    "Заменяет обычный пробный период.\n\n"
    "<b>Правила</b>\n"
    "• У промокода может быть лимит общего числа активаций и срок действия — если они вышли, код не сработает.\n"
    "• Обычно один код можно применить один раз на аккаунт.\n"
    "• Некоторые коды — только для новичков (ещё не покупали) или только для постоянных клиентов (уже покупали).\n"
    "• После нескольких неверных вводов подряд ввод ненадолго блокируется."
)


def _promo_kb(has_discount: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_discount:
        b.button(text="🛒 Купить со скидкой", callback_data="buy")
        b.button(text="❌ Отменить скидку", callback_data="promo_cancel")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "promo")
async def cb_promo(cb: CallbackQuery, svc: VPNService, state: FSMContext) -> None:
    disc = await svc.get_discount(cb.from_user.id)
    text = PROMO_INFO
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
    await message.answer(
        f"{head}: <b>+{res.value} дн.</b>\nПодписка действует до <b>{fmt_dt(res.expire_at)}</b>.\n\n"
        + config_text(svc.sub_url(acc.remna)),
        reply_markup=back_kb(),
    )


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject, svc: VPNService, state: FSMContext) -> None:
    if not command.args:
        await state.set_state(PromoInput.waiting_code)
        await message.answer(PROMO_INFO + "\n\n✍️ <b>Отправьте промокод:</b>", reply_markup=_promo_kb(False))
        return
    await _redeem_and_reply(message, svc, state, command.args.split()[0])


@router.message(PromoInput.waiting_code, F.text, ~F.text.startswith("/"))
async def msg_promo_code(message: Message, svc: VPNService, state: FSMContext) -> None:
    await _redeem_and_reply(message, svc, state, message.text)


# ---------------------------------------------------------------- аккаунт и конфиги

@router.callback_query(F.data == "account")
async def cb_account(cb: CallbackQuery, svc: VPNService) -> None:
    acc = await svc.get_account(cb.from_user.id)
    await show(cb, account_text(svc, acc), back_kb())


@router.callback_query(F.data == "config")
async def cb_config(cb: CallbackQuery, svc: VPNService) -> None:
    acc = await svc.get_account(cb.from_user.id)
    if not acc.remna:
        await show(cb, "У вас ещё нет подписки. Получите пробный период или купите тариф.", back_kb())
        return
    await show(cb, config_text(svc.sub_url(acc.remna)), back_kb())


@router.callback_query(F.data == "revoke")
async def cb_revoke(cb: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, обновить", callback_data="revoke_yes")
    b.button(text="Отмена", callback_data="menu")
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
    await show(cb, "✅ Ссылка обновлена.\n\n" + config_text(url), back_kb())


# ---------------------------------------------------------------- рефералы

@router.callback_query(F.data == "ref")
async def cb_ref(cb: CallbackQuery, bot: Bot, svc: VPNService, settings: Settings) -> None:
    acc = await svc.get_account(cb.from_user.id)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{acc.user.id}"
    text = (
        "👥 <b>Приглашайте друзей</b>\n\n"
        f"За каждого друга, который совершит <b>первую покупку</b>, вы получите +{settings.referral_bonus_days} дн. подписки.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {acc.invited}\n"
        f"Совершили покупку (бонус начислен): {acc.rewarded}"
    )
    await show(cb, text, back_kb())
