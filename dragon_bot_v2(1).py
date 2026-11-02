"""
🐉 Dragon Tapper v2 — кликер с редкостями драконов и донатом Telegram Stars.

Запуск:
    pip install aiogram
    export BOT_TOKEN="токен_от_BotFather"
    export ADMIN_ID="ваш_telegram_id"      # нужен для /refund (необязательно)
    python dragon_bot_v2.py
"""
import asyncio
import html
import logging
import os
import random
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

TOKEN = os.getenv("BOT_TOKEN", "PASTE_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_CONTACT = "@your_username"  # куда писать по вопросам оплаты
DB_FILE = os.getenv("DB_FILE", "dragon.db")  # на хостинге укажите путь на постоянном диске, напр. /data/dragon.db

CRIT_CHANCE = 0.08
CRIT_MULT = 5
ENERGY_REGEN = 1          # энергии в секунду
OFFLINE_CAP = 8 * 3600    # макс. накопление офлайн
DUPLICATE_REFUND = 0.30   # доля цены яйца, возвращаемая за дубликат

# ---------------------------------------------------------------- ДРАКОНЫ
# mult — множитель ко ВСЕМ доходам, weight — шанс выпадения из яйца (%)
RARITIES = {
    "common":    {"label": "Обычный",       "icon": "⚪", "mult": 1.0,  "weight": 60},
    "rare":      {"label": "Редкий",        "icon": "🔵", "mult": 1.5,  "weight": 25},
    "epic":      {"label": "Эпический",     "icon": "🟣", "mult": 2.5,  "weight": 10},
    "legendary": {"label": "Легендарный",   "icon": "🟠", "mult": 5.0,  "weight": 4.5},
    "mythic":    {"label": "Таинственный",  "icon": "🔮", "mult": 15.0, "weight": 0.5},
}
RARITY_ORDER = list(RARITIES)

DRAGONS = {
    "c1": ("🦎", "Пепельная ящерка", "common"),
    "c2": ("🐊", "Болотный змей", "common"),
    "c3": ("🦕", "Долинный дракончик", "common"),
    "r1": ("🐲", "Лесной дракон", "rare"),
    "r2": ("🌊", "Морской змей", "rare"),
    "r3": ("❄️", "Ледяной вирм", "rare"),
    "e1": ("🔥", "Огненный дракон", "epic"),
    "e2": ("⚡", "Грозовой дракон", "epic"),
    "e3": ("☠️", "Костяной дракон", "epic"),
    "l1": ("🌟", "Солнечный дракон", "legendary"),
    "l2": ("🌙", "Лунный дракон", "legendary"),
    "m1": ("🌌", "Дракон Бездны", "mythic"),
    "m2": ("🪐", "Первородный Хаос", "mythic"),
}

# ---------------------------------------------------------------- ДОНАТ (Telegram Stars, валюта XTR)
STAR_ITEMS = {
    "starter": {
        "title": "🎁 Стартовый набор", "stars": 30,
        "desc": "5000 монет + буст x2 на 24 часа + полная энергия. Покупается один раз.",
    },
    "refill": {
        "title": "🔋 Полная энергия", "stars": 15,
        "desc": "Мгновенно заполнить энергию до максимума.",
    },
    "boost": {
        "title": "🚀 Буст x2 на 24 часа", "stars": 50,
        "desc": "Все доходы (тапы и авто) удваиваются на сутки.",
    },
    "vip": {
        "title": "👑 VIP навсегда", "stars": 250,
        "desc": "Все доходы x2 навсегда и +100 к максимуму энергии.",
    },
    "legend": {
        "title": "🟠 Легендарный дракон", "stars": 150,
        "desc": "Гарантированный легендарный дракон: все доходы x5.",
    },
    "mythic": {
        "title": "🔮 Таинственный дракон", "stars": 500,
        "desc": "Гарантированный таинственный дракон: все доходы x15!",
    },
}

# ---------------------------------------------------------------- БАЗА ДАННЫХ
db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row
db.execute(
    """CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY, name TEXT, coins REAL DEFAULT 0,
        taps INTEGER DEFAULT 0, power INTEGER DEFAULT 1, auto INTEGER DEFAULT 0,
        battery INTEGER DEFAULT 0, energy REAL DEFAULT 100, ts REAL
    )"""
)
# миграция со старой версии
for col, ddl in [
    ("dragons", "TEXT DEFAULT 'c1'"),
    ("vip", "INTEGER DEFAULT 0"),
    ("boost_until", "REAL DEFAULT 0"),
    ("hatches", "INTEGER DEFAULT 0"),
    ("starter", "INTEGER DEFAULT 0"),
]:
    try:
        db.execute(f"ALTER TABLE players ADD COLUMN {col} {ddl}")
    except sqlite3.OperationalError:
        pass
db.execute(
    """CREATE TABLE IF NOT EXISTS payments (
        charge_id TEXT PRIMARY KEY, user_id INTEGER, item TEXT, stars INTEGER, ts REAL
    )"""
)
db.commit()


# ---------------------------------------------------------------- ЛОГИКА
def owned(p) -> set:
    return set(filter(None, (p["dragons"] or "c1").split(",")))


def best_dragon(p) -> str:
    ids = owned(p) or {"c1"}
    return max(ids, key=lambda d: RARITY_ORDER.index(DRAGONS[d][2]))


def boost_active(p) -> bool:
    return p["boost_until"] > time.time()


def mult(p) -> float:
    m = RARITIES[DRAGONS[best_dragon(p)][2]]["mult"]
    if p["vip"]:
        m *= 2
    if boost_active(p):
        m *= 2
    return m


def max_energy(p) -> int:
    return 100 + 50 * p["battery"] + (100 if p["vip"] else 0)


def costs(p) -> dict:
    return {
        "power": int(50 * 1.6 ** (p["power"] - 1)),
        "auto": int(100 * 1.7 ** p["auto"]),
        "battery": int(80 * 1.6 ** p["battery"]),
    }


def egg_cost(p) -> int:
    return int(500 * 1.35 ** p["hatches"])


def load(uid: int, name: str) -> dict:
    now = time.time()
    row = db.execute("SELECT * FROM players WHERE id=?", (uid,)).fetchone()
    if row is None:
        db.execute("INSERT INTO players (id, name, ts) VALUES (?,?,?)", (uid, name, now))
        db.commit()
        row = db.execute("SELECT * FROM players WHERE id=?", (uid,)).fetchone()
    p = dict(row)
    dt = max(0, now - p["ts"])
    p["energy"] = min(max_energy(p), p["energy"] + dt * ENERGY_REGEN)
    p["coins"] += min(dt, OFFLINE_CAP) * p["auto"] * mult(p)
    p["ts"] = now
    p["name"] = name
    return p


def save(p: dict) -> None:
    db.execute(
        """UPDATE players SET name=?, coins=?, taps=?, power=?, auto=?, battery=?,
           energy=?, ts=?, dragons=?, vip=?, boost_until=?, hatches=?, starter=?
           WHERE id=?""",
        (p["name"], p["coins"], p["taps"], p["power"], p["auto"], p["battery"],
         p["energy"], p["ts"], p["dragons"], p["vip"], p["boost_until"],
         p["hatches"], p["starter"], p["id"]),
    )
    db.commit()


def roll_rarity() -> str:
    keys = RARITY_ORDER
    return random.choices(keys, weights=[RARITIES[k]["weight"] for k in keys])[0]


def grant_dragon(p: dict, rarity: str):
    """Выдаёт случайного дракона нужной редкости. Возвращает (id, новый_ли)."""
    pool = [d for d, v in DRAGONS.items() if v[2] == rarity]
    fresh = [d for d in pool if d not in owned(p)]
    d = random.choice(fresh or pool)
    is_new = d in fresh
    if is_new:
        p["dragons"] = ",".join(sorted(owned(p) | {d}))
    return d, is_new


def dragon_line(d: str) -> str:
    emoji, name, rk = DRAGONS[d]
    r = RARITIES[rk]
    return f"{emoji} <b>{name}</b> — {r['icon']} {r['label']} (x{r['mult']:g})"


def apply_purchase(p: dict, key: str) -> str:
    now = time.time()
    if key == "refill":
        p["energy"] = max_energy(p)
        return "Энергия заполнена! ⚡"
    if key == "boost":
        p["boost_until"] = max(now, p["boost_until"]) + 86400
        return "Буст x2 активен на 24 часа! 🚀"
    if key == "vip":
        p["vip"] = 1
        return "Теперь вы VIP! Все доходы x2 навсегда 👑"
    if key == "starter":
        p["starter"] = 1
        p["coins"] += 5000
        p["energy"] = max_energy(p)
        p["boost_until"] = max(now, p["boost_until"]) + 86400
        return "Стартовый набор получен: +5000 монет, буст x2 и полная энергия! 🎁"
    if key in ("legend", "mythic"):
        rarity = "legendary" if key == "legend" else "mythic"
        d, is_new = grant_dragon(p, rarity)
        tail = "" if is_new else "\n(Он у вас уже был, но доходы всё равно x" + f"{RARITIES[rarity]['mult']:g}.)"
        return "Вам достался:\n" + dragon_line(d) + tail
    return ""


# ---------------------------------------------------------------- ЭКРАНЫ
def bar(value: float, total: int, size: int = 10) -> str:
    filled = round(size * value / total) if total else 0
    return "🟩" * filled + "⬜" * (size - filled)


def main_screen(p: dict, note: str = ""):
    d = best_dragon(p)
    emoji, name, rk = DRAGONS[d]
    r = RARITIES[rk]
    m = mult(p)
    me = max_energy(p)

    status = []
    if p["vip"]:
        status.append("👑 VIP")
    if boost_active(p):
        left = int(p["boost_until"] - time.time())
        status.append(f"🚀 x2 ещё {left // 3600}ч {left % 3600 // 60}м")

    text = (
        f"{emoji} <b>{name}</b>  {r['icon']} {r['label']}\n"
        f"Множитель дохода: <b>x{m:g}</b>"
        + (f"  ({' · '.join(status)})" if status else "")
        + f"\n\n💰 Монеты: <b>{int(p['coins'])}</b>\n"
        f"👆 За тап: <b>{p['power'] * m:g}</b>   ⚙️ Авто: <b>{p['auto'] * m:g}/сек</b>\n"
        f"⚡ Энергия: {int(p['energy'])}/{me}\n{bar(p['energy'], me)}"
    )
    if note:
        text += f"\n\n{note}"

    rows = [
        [InlineKeyboardButton(text=f"{emoji}  ТАП!  {emoji}", callback_data="tap")],
        [
            InlineKeyboardButton(text="🛒 Улучшения", callback_data="shop"),
            InlineKeyboardButton(text="🥚 Яйца", callback_data="egg"),
        ],
        [
            InlineKeyboardButton(text="🐉 Коллекция", callback_data="dragons"),
            InlineKeyboardButton(text="💎 Звёзды", callback_data="stars"),
        ],
        [InlineKeyboardButton(text="🏆 Топ", callback_data="top")],
    ]
    if p["energy"] < 1:
        rows.insert(1, [InlineKeyboardButton(
            text=f"🔋 Заправить за {STAR_ITEMS['refill']['stars']}⭐", callback_data="pay:refill")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def shop_screen(p: dict, note: str = ""):
    c = costs(p)
    text = (
        f"🛒 <b>Улучшения</b>\n💰 У вас: <b>{int(p['coins'])}</b>\n\n"
        f"💪 <b>Сила тапа</b> (ур. {p['power']}): +1 к базе за тап\n"
        f"⚙️ <b>Автодобытчик</b> (ур. {p['auto']}): +1 монета/сек, даже офлайн\n"
        f"🔋 <b>Батарея</b> (ур. {p['battery']}): +50 к максимуму энергии"
    )
    if note:
        text += f"\n\n{note}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💪 Сила — {c['power']}💰", callback_data="buy:power")],
        [InlineKeyboardButton(text=f"⚙️ Авто — {c['auto']}💰", callback_data="buy:auto")],
        [InlineKeyboardButton(text=f"🔋 Батарея — {c['battery']}💰", callback_data="buy:battery")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])
    return text, kb


def egg_screen(p: dict, note: str = ""):
    odds = "\n".join(
        f"{r['icon']} {r['label']}: {r['weight']:g}%  (x{r['mult']:g})" for r in RARITIES.values()
    )
    text = (
        f"🥚 <b>Драконье яйцо</b>\n💰 У вас: <b>{int(p['coins'])}</b>\n\n"
        f"Шансы выпадения:\n{odds}\n\n"
        f"Дубликат вернёт {int(DUPLICATE_REFUND * 100)}% стоимости яйца.\n"
        f"Лучший дракон автоматически становится вашим основным."
    )
    if note:
        text += f"\n\n{note}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🥚 Вылупить — {egg_cost(p)}💰", callback_data="hatch")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])
    return text, kb


def dragons_screen(p: dict):
    have = owned(p)
    lines = []
    for rk in RARITY_ORDER:
        r = RARITIES[rk]
        lines.append(f"\n{r['icon']} <b>{r['label']}</b> (x{r['mult']:g})")
        for d, (emoji, name, k) in DRAGONS.items():
            if k != rk:
                continue
            lines.append(f"  {emoji} {name}" if d in have else "  ❔ ???")
    text = f"🐉 <b>Коллекция: {len(have)}/{len(DRAGONS)}</b>\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]])
    return text, kb


def stars_screen(p: dict):
    lines, rows = [], []
    for key, it in STAR_ITEMS.items():
        if key == "starter" and p["starter"]:
            continue
        if key == "vip" and p["vip"]:
            continue
        lines.append(f"{it['title']} — <b>{it['stars']}⭐</b>\n{it['desc']}")
        rows.append([InlineKeyboardButton(text=f"{it['title']} · {it['stars']}⭐", callback_data=f"pay:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
    text = "💎 <b>Магазин звёзд</b>\nПоддержите игру и получите огромное преимущество!\n\n" + "\n\n".join(lines)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def top_screen():
    rows = db.execute(
        "SELECT name, taps, dragons FROM players ORDER BY taps DESC LIMIT 10").fetchall()
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [
        f"{medals[i]} {DRAGONS[best_dragon(r)][0]} {html.escape(r['name'] or '???')} — {r['taps']} тапов"
        for i, r in enumerate(rows)
    ]
    text = "🏆 <b>Топ-10 тапперов</b>\n\n" + ("\n".join(lines) or "Пока никого нет")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]])
    return text, kb


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass  # "message is not modified"


# ---------------------------------------------------------------- ХЕНДЛЕРЫ
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    p = load(m.from_user.id, m.from_user.first_name)
    save(p)
    await m.answer(
        "🐉 Добро пожаловать в Dragon Tapper!\n"
        "Тапайте, копите монеты, вылупляйте яйца и собирайте драконов "
        "всех редкостей. Самый редкий — Таинственный. 🔮"
    )
    text, kb = main_screen(p)
    await m.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("game"))
async def game(m: Message):
    p = load(m.from_user.id, m.from_user.first_name)
    save(p)
    text, kb = main_screen(p)
    await m.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer(f"По вопросам оплаты и возвратов пишите: {SUPPORT_CONTACT}")


@dp.callback_query(F.data == "tap")
async def on_tap(cb: CallbackQuery):
    p = load(cb.from_user.id, cb.from_user.first_name)

    if p["energy"] < 1:
        save(p)
        text, kb = main_screen(p, "⚡ <b>Энергия закончилась!</b> Подождите или заправьтесь.")
        await show(cb, text, kb)
        await cb.answer()
        return

    gain = p["power"] * mult(p)
    note = ""
    if random.random() < CRIT_CHANCE:
        gain *= CRIT_MULT
        note = f"💥 <b>КРИТ!</b> +{gain:g} монет"

    p["energy"] -= 1
    p["coins"] += gain
    p["taps"] += 1
    save(p)

    text, kb = main_screen(p, note)
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "shop")
async def on_shop(cb: CallbackQuery):
    p = load(cb.from_user.id, cb.from_user.first_name)
    save(p)
    text, kb = shop_screen(p)
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def on_buy(cb: CallbackQuery):
    item = cb.data.split(":")[1]
    p = load(cb.from_user.id, cb.from_user.first_name)
    price = costs(p).get(item)
    if price is None:
        await cb.answer()
        return
    if p["coins"] < price:
        save(p)
        await cb.answer("Не хватает монет 😢")
        return
    p["coins"] -= price
    p[item] += 1
    if item == "battery":
        p["energy"] = max_energy(p)
    save(p)
    text, kb = shop_screen(p, "✅ Куплено!")
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "egg")
async def on_egg(cb: CallbackQuery):
    p = load(cb.from_user.id, cb.from_user.first_name)
    save(p)
    text, kb = egg_screen(p)
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "hatch")
async def on_hatch(cb: CallbackQuery):
    p = load(cb.from_user.id, cb.from_user.first_name)
    cost = egg_cost(p)
    if p["coins"] < cost:
        save(p)
        await cb.answer("Не хватает монет 😢")
        return

    p["coins"] -= cost
    p["hatches"] += 1
    rarity = roll_rarity()
    d, is_new = grant_dragon(p, rarity)
    if is_new:
        note = "🎉 <b>Новый дракон!</b>\n" + dragon_line(d)
    else:
        back = int(cost * DUPLICATE_REFUND)
        p["coins"] += back
        note = f"♻️ Дубликат: {dragon_line(d)}\nВозврат: +{back}💰"
    save(p)

    text, kb = egg_screen(p, note)
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "dragons")
async def on_dragons(cb: CallbackQuery):
    p = load(cb.from_user.id, cb.from_user.first_name)
    save(p)
    text, kb = dragons_screen(p)
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "stars")
async def on_stars(cb: CallbackQuery):
    p = load(cb.from_user.id, cb.from_user.first_name)
    save(p)
    text, kb = stars_screen(p)
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "top")
async def on_top(cb: CallbackQuery):
    text, kb = top_screen()
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "home")
async def on_home(cb: CallbackQuery):
    p = load(cb.from_user.id, cb.from_user.first_name)
    save(p)
    text, kb = main_screen(p)
    await show(cb, text, kb)
    await cb.answer()


# ---------------------------------------------------------------- ПЛАТЕЖИ (Telegram Stars)
@dp.callback_query(F.data.startswith("pay:"))
async def on_pay(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    item = STAR_ITEMS.get(key)
    if not item:
        await cb.answer()
        return
    p = load(cb.from_user.id, cb.from_user.first_name)
    if (key == "starter" and p["starter"]) or (key == "vip" and p["vip"]):
        await cb.answer("Это уже куплено ✅", show_alert=True)
        return
    await cb.message.answer_invoice(
        title=item["title"],
        description=item["desc"],
        payload=key,
        currency="XTR",  # Telegram Stars; provider_token не нужен
        prices=[LabeledPrice(label=item["title"], amount=item["stars"])],
    )
    await cb.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    item = STAR_ITEMS.get(q.invoice_payload)
    if not item or q.currency != "XTR" or q.total_amount != item["stars"]:
        await q.answer(ok=False, error_message="Товар недоступен")
        return
    p = load(q.from_user.id, q.from_user.first_name)
    if (q.invoice_payload == "starter" and p["starter"]) or (q.invoice_payload == "vip" and p["vip"]):
        await q.answer(ok=False, error_message="Это уже куплено")
        return
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    sp = m.successful_payment
    if db.execute("SELECT 1 FROM payments WHERE charge_id=?",
                  (sp.telegram_payment_charge_id,)).fetchone():
        return  # уже обработан
    db.execute(
        "INSERT INTO payments (charge_id, user_id, item, stars, ts) VALUES (?,?,?,?,?)",
        (sp.telegram_payment_charge_id, m.from_user.id, sp.invoice_payload,
         sp.total_amount, time.time()),
    )
    p = load(m.from_user.id, m.from_user.first_name)
    result = apply_purchase(p, sp.invoice_payload)
    save(p)  # внутри commit: платёж и выдача товара фиксируются вместе
    await m.answer(f"✅ Спасибо за поддержку!\n{result}", parse_mode="HTML")
    text, kb = main_screen(p)
    await m.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("refund"))
async def refund(m: Message, command: CommandObject, bot: Bot):
    """Только для админа: /refund <charge_id> — вернуть звёзды игроку."""
    if m.from_user.id != ADMIN_ID:
        return
    charge = (command.args or "").strip()
    row = db.execute("SELECT * FROM payments WHERE charge_id=?", (charge,)).fetchone()
    if not row:
        await m.answer("Платёж не найден")
        return
    await bot.refund_star_payment(user_id=row["user_id"], telegram_payment_charge_id=charge)
    await m.answer("Возврат выполнен (покупку в игре откатите вручную, если нужно).")


# ---------------------------------------------------------------- ЗАПУСК
async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
