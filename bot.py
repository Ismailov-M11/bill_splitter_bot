# bot.py
import os
import re
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================== CONFIG & LOGGING ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8388611917:AAEL-NwaqhEBlQFT_waK5iwy3ehiydBZgbU")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bill-splitter-bot.netlify.app/")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bill_splitter")

UZS = "UZS"
Q3 = Decimal("0.001")  # округление до тысячных
Q2 = Decimal("0.01")

# ================== DATA MODELS ==================
@dataclass
class Dish:
    name: str
    qty_total: Decimal            # всего штук в позиции
    line_total: Decimal           # сумма за всю позицию
    assigned: List[Decimal] = field(default_factory=list)  # по людям, сколько штук назначили

    @property
    def unit_price(self) -> Decimal:
        if self.qty_total == 0:
            return Decimal(0)
        return (self.line_total / self.qty_total).quantize(Q3, rounding=ROUND_HALF_UP)

    def remaining(self) -> Decimal:
        return (self.qty_total - sum(self.assigned)).quantize(Q3)

@dataclass
class Bill:
    people: List[str] = field(default_factory=list)
    dishes: List[Dish] = field(default_factory=list)
    service_pct: Decimal = Decimal("0")  # по умолчанию 0%

    def ensure_assign_matrix(self):
        for d in self.dishes:
            need = len(self.people) - len(d.assigned)
            if need > 0:
                d.assigned.extend([Decimal(0)] * need)

# чат -> состояние (для режима «меню» бота)
STATE: Dict[int, Bill] = {}

# ================== HELPERS ==================
def fmt_money(n: int | Decimal) -> str:
    n = int(n)
    return f"{n:,}".replace(",", " ")

def kb_main():
    # добавляем кнопку открытия WebApp прямо в чат
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🧾 Новый счёт"), KeyboardButton("➕ Блюдо"), KeyboardButton("👤 Участник")],
            [KeyboardButton("🍽 Назначить"), KeyboardButton("⚙️ Сервис"), KeyboardButton("🧮 Рассчитать")],
            [KeyboardButton("🧮 Open (WebApp)", web_app=WebAppInfo(url=WEBAPP_URL))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

def parse_dish_line(text: str) -> Tuple[str, Decimal, Decimal]:
    """
    Поддерживаем два формата:
    1) «ассорти 2 шт 28000»
    2) «плов 45000»  (количество = 1)
    """
    s = text.strip()
    m = re.search(r"(.*)\s+(\d+(?:[.,]\d+)?)\s*шт\s+(\d+(?:[.,]\d+)?)\s*$", s, flags=re.I)
    if m:
        name = m.group(1).strip()
        qty = Decimal(m.group(2).replace(",", "."))
        line_total = Decimal(m.group(3).replace(",", "."))
        return name, qty, line_total

    m = re.search(r"^(.*)\s+(\d+(?:[.,]\d+)?)\s*$", s)
    if m:
        name = m.group(1).strip()
        qty = Decimal(1)
        line_total = Decimal(m.group(2).replace(",", "."))
        return name, qty, line_total

    raise ValueError("Не удалось распознать блюдо. Формат: (название) (количество) шт (цена) — либо (название) (цена).")

def person_checkmarks(bill: Bill) -> List[bool]:
    marks = []
    for i, _ in enumerate(bill.people):
        any_assigned = any(d.assigned and d.assigned[i] > 0 for d in bill.dishes)
        marks.append(any_assigned)
    return marks

def build_people_keyboard(bill: Bill) -> InlineKeyboardMarkup:
    rows = []
    marks = person_checkmarks(bill)
    for i, name in enumerate(bill.people):
        mark = " ✅" if marks[i] else ""
        rows.append([InlineKeyboardButton(f"{i+1}. {name}{mark}", callback_data=f"pick_person:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def build_assign_keyboard(bill: Bill, p_idx: int) -> InlineKeyboardMarkup:
    rows = []
    for i, d in enumerate(bill.dishes):
        left = (d.qty_total - sum(d.assigned)).quantize(Q3)
        left_i = int(left)           # показываем целые остатки
        qty_i = int(d.qty_total)
        has_this = d.assigned[p_idx] > 0
        mark = " ✅" if has_this else ""
        # формат: «Чойхона комплект (1/2) ✅»
        label = f"{d.name} ({left_i}/{qty_i}){mark}"
        rows.append([InlineKeyboardButton(label, callback_data=f"plus:{p_idx}:{i}")])

    rows.append([
        InlineKeyboardButton("🔄 Очистить выбор", callback_data=f"clear:{p_idx}"),
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="back_people"),
    ])
    return InlineKeyboardMarkup(rows)

def summarize_choices_for_person(bill: Bill, p_idx: int) -> str:
    lines = []
    for d in bill.dishes:
        if d.assigned[p_idx] > 0:
            unit = d.unit_price
            qty = int(d.assigned[p_idx])
            lines.append(f"• {d.name}: {qty} шт × {fmt_money(int(unit))} {UZS}")
    if not lines:
        return "—"
    return "\n".join(lines)

def calc_base_total(bill: Bill) -> Decimal:
    return sum((d.line_total for d in bill.dishes), start=Decimal(0))

def render_dishes_lines(bill: Bill) -> str:
    if not bill.dishes:
        return "Нет добавленных блюд"
    lines = []
    for i, d in enumerate(bill.dishes, start=1):
        qty_i = int(d.qty_total)
        unit_i = int(d.unit_price)
        sum_i = int(d.line_total)
        lines.append(f"{i}. {d.name} — {qty_i} шт × {fmt_money(unit_i)} {UZS} = {fmt_money(sum_i)} {UZS}")
    return "\n".join(lines)

# ================== COMMANDS / MENU ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in STATE:
        STATE[chat_id] = Bill()
    await update.message.reply_text(
        "Добро пожаловать! Используйте кнопки ниже.\n"
        "Для работы с мини-приложением нажмите «🧮 Open (WebApp)».",
        reply_markup=kb_main(),
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bill = STATE.setdefault(chat_id, Bill())
    text = (update.message.text or "").strip()

    if text == "🧾 Новый счёт":
        STATE[chat_id] = Bill()
        await update.message.reply_text("Новый счёт начат. Добавьте блюда и участников.", reply_markup=kb_main())
        return

    if text == "⚙️ Сервис":
        context.user_data["mode"] = "svc"
        await update.message.reply_text(
            "Пожалуйста, введите процент сервиса (целое число):",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True),
        )
        return

    if context.user_data.get("mode") == "svc":
        if text == "Отмена":
            context.user_data.pop("mode", None)
            await update.message.reply_text("Отменено.", reply_markup=kb_main())
            return
        try:
            pct = int(text)
            pct = max(0, min(100, pct))
        except Exception:
            await update.message.reply_text("Только число от 0 до 100, пожалуйста.")
            return

        bill.service_pct = Decimal(pct)
        context.user_data.pop("mode", None)

        # Сводка после установки сервиса
        base_total = calc_base_total(bill)
        service_total = (base_total * bill.service_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP)
        total = base_total + service_total

        dishes_block = render_dishes_lines(bill)
        msg = (
            f"✅ Процент сервиса установлен: {pct}%\n\n"
            f"📋 Список блюд:\n{dishes_block}\n\n"
            f"🧮 Итого без сервиса: {fmt_money(base_total)} {UZS}\n"
            f"🧾 Сервис {pct}%: {fmt_money(service_total)} {UZS}\n"
            f"💰 Итого к оплате: {fmt_money(total)} {UZS}"
        )
        await update.message.reply_text(msg, reply_markup=kb_main())
        return

    if text == "➕ Блюдо":
        context.user_data["mode"] = "add_dish"
        await update.message.reply_text(
            "Пожалуйста, введите позицию. Можно сразу так: (название) (количество) шт (сумма)\n"
            "Либо: (название) (сумма)",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True),
        )
        return

    if context.user_data.get("mode") == "add_dish":
        if text == "Отмена":
            context.user_data.pop("mode", None)
            await update.message.reply_text("Добавление отменено.", reply_markup=kb_main())
            return
        try:
            name, qty, line_total = parse_dish_line(text)
        except Exception as e:
            await update.message.reply_text(str(e))
            return

        d = Dish(name=name, qty_total=qty, line_total=line_total)
        d.assigned = [Decimal(0)] * len(bill.people)
        bill.dishes.append(d)

        # >>> ВАЖНО: сбрасываем режим после успешного добавления
        context.user_data.pop("mode", None)

        # Формируем красивую сводку: список с ценами и общая сумма
        dishes_block = render_dishes_lines(bill)
        base_total = calc_base_total(bill)
        msg = (
            f"✅ Блюдо добавлено: {name} — {int(qty)} шт × {fmt_money(int(d.unit_price))} {UZS} = {fmt_money(int(line_total))} {UZS}\n\n"
            f"📋 Список блюд:\n{dishes_block}\n\n"
            f"🧮 Общая сумма без сервиса: {fmt_money(base_total)} {UZS}"
        )
        await update.message.reply_text(msg, reply_markup=kb_main())
        return

    if text == "👤 Участник":
        context.user_data["mode"] = "add_person"
        await update.message.reply_text(
            "Пожалуйста, введите имя участника (или нажмите «Отмена»):",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True),
        )
        return

    if context.user_data.get("mode") == "add_person":
        if text == "Отмена":
            context.user_data.pop("mode", None)
            await update.message.reply_text("Действие отменено.", reply_markup=kb_main())
            return
        name = text.strip()
        if not name:
            await update.message.reply_text("Имя не может быть пустым. Повторите, пожалуйста.")
            return
        bill.people.append(name)
        for d in bill.dishes:
            d.assigned.append(Decimal(0))
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            f"✅ Добавлен участник: {name}\n👥 Текущий список: " + ", ".join(bill.people),
            reply_markup=kb_main()
        )
        return

    if text == "🍽 Назначить":
        if not bill.people or not bill.dishes:
            await update.message.reply_text("Сначала добавьте блюда и участников.", reply_markup=kb_main())
            return
        await update.message.reply_text("Кому назначаем? Выберите участника:", reply_markup=build_people_keyboard(bill))
        return

    if text == "🧮 Рассчитать":
        if not bill.people or not bill.dishes:
            await update.message.reply_text("Нужно добавить блюда и участников.", reply_markup=kb_main())
            return

        # считаем базу
        per_base = [Decimal(0)] * len(bill.people)
        base_total = Decimal(0)
        for d in bill.dishes:
            unit = d.unit_price
            base_total += d.line_total
            for i, cnt in enumerate(d.assigned):
                per_base[i] += unit * cnt

        svc_pct = bill.service_pct
        per_svc = [ (b * svc_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP) for b in per_base ]
        service_total = (base_total * svc_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP)

        lines = [
            "🧮 Итоговый расчёт:",
            f"Без сервиса: {fmt_money(base_total)} {UZS}",
            f"Сервис {bill.service_pct}%: {fmt_money(service_total)} {UZS}",
            f"💰 Итого: {fmt_money(base_total + service_total)} {UZS}",
            "",
            "👥 Разбивка по участникам:",
        ]
        for i, name in enumerate(bill.people):
            lines.append(
                f"{i+1}. {name} — {fmt_money(per_base[i] + per_svc[i])} {UZS}  "
                f"(до сервиса: {fmt_money(per_base[i])} {UZS}, +{fmt_money(per_svc[i])} {UZS})"
            )
        await update.message.reply_text("\n".join(lines), reply_markup=kb_main())
        return

    # по умолчанию просто показываем меню
    await update.message.reply_text("Выберите действие ниже.", reply_markup=kb_main())

# ================== CALLBACKS (назначение) ==================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    bill = STATE.setdefault(chat_id, Bill())

    data = query.data or ""
    if data == "back_main":
        await query.edit_message_text("Главное меню. Выберите действие.", reply_markup=kb_main())
        return

    if data == "back_people":
        await query.edit_message_text("Кому назначаем?\nВыберите участника:", reply_markup=build_people_keyboard(bill))
        return

    if data.startswith("pick_person:"):
        _, p_s = data.split(":")
        p_idx = int(p_s)
        await show_assign_screen(update, bill, p_idx)
        return

    if data.startswith("clear:"):
        _, p_s = data.split(":")
        p_idx = int(p_s)
        bill.ensure_assign_matrix()
        for d in bill.dishes:
            d.assigned[p_idx] = Decimal(0)
        await show_assign_screen(update, bill, p_idx)
        return

    if data.startswith("plus:"):
        _, p_s, d_s = data.split(":")
        p_idx, d_idx = int(p_s), int(d_s)
        bill.ensure_assign_matrix()
        d = bill.dishes[d_idx]
        # проверяем остаток
        if (sum(d.assigned) + Decimal(1)) > d.qty_total:
            await show_assign_screen(update, bill, p_idx, flash="❗ Остатка по позиции нет.")
            return
        d.assigned[p_idx] = d.assigned[p_idx] + Decimal(1)
        await show_assign_screen(update, bill, p_idx)
        return

async def show_assign_screen(update: Update, bill: Bill, p_idx: int, flash: str | None = None):
    bill.ensure_assign_matrix()
    chosen = summarize_choices_for_person(bill, p_idx)
    head = (
        (flash + "\n\n") if flash else ""
    ) + f"👤 Участник: *{bill.people[p_idx]}*\n" \
        f"Нажимайте на блюдо — каждый тап добавляет 1 шт (если есть остаток).\n\n" \
        f"🧾 Выбранные для участника:\n{chosen}"

    await update.callback_query.edit_message_text(
        head,
        parse_mode="Markdown",
        reply_markup=build_assign_keyboard(bill, p_idx)
    )

# ================== HANDLER ДАННЫХ ИЗ WEBAPP ==================
def _format_webapp_message(data: dict) -> str:
    """
    Понимаем ОБА формата:
    A) «Legacy»:
       {
         base_total, service_pct, service_total, total,
         people: [{name, base, service, total}, ...]
       }
    B) «Builder WebApp»:
       {
         type: "calculation",
         servicePercent,
         participants: [{id, name, amount}],
         dishes: [{name, qty, totalPrice, assignments: [...]}, ...],
         total
       }
    """

    # --- Случай A (legacy) ---
    if "people" in data or "base_total" in data:
        def g(key, default=0):
            return int(data.get(key, default))

        base_total   = g("base_total")
        service_pct  = int(data.get("service_pct", 0))
        service_total= g("service_total")
        total        = g("total")

        people = data.get("people", [])
        lines = [
            "🧮 Итоговый расчёт:",
            f"Без сервиса: {fmt_money(base_total)} {UZS}",
            f"Сервис {service_pct}%: {fmt_money(service_total)} {UZS}",
            f"💰 Итого: {fmt_money(total)} {UZS}",
            "",
            "👥 Разбивка по участникам:",
        ]
        for idx, p in enumerate(people, start=1):
            name    = p.get("name", f"Участник {idx}")
            base    = int(p.get("base", 0))
            svc     = int(p.get("service", 0))
            p_total = int(p.get("total", base + svc))
            lines.append(
                f"{idx}. {name} — {fmt_money(p_total)} {UZS}  "
                f"(до сервиса: {fmt_money(base)} {UZS}, +{fmt_money(svc)} {UZS})"
            )
        return "\n".join(lines)

    # --- Случай B (Builder WebApp) ---
    service_pct = Decimal(str(data.get("servicePercent", 0)))
    participants = data.get("participants", [])
    dishes = data.get("dishes", [])

    id_to_idx = {p["id"]: i for i, p in enumerate(participants) if "id" in p}

    per_base = [Decimal(0) for _ in participants]
    base_total = Decimal(0)

    for d in dishes:
        qty = Decimal(str(d.get("qty", 0)))
        total_price = Decimal(str(d.get("totalPrice", 0)))
        assignments = d.get("assignments", [])
        if qty <= 0:
            continue
        unit = (total_price / qty).quantize(Q3, rounding=ROUND_HALF_UP)
        base_total += total_price
        for a in assignments:
            if a is None:
                continue
            idx = id_to_idx.get(a)
            if idx is not None:
                per_base[idx] += unit

    per_svc = [(b * service_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP) for b in per_base]
    service_total = (base_total * service_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP)

    lines = [
        "🧮 Итоговый расчёт:",
        f"Без сервиса: {fmt_money(base_total)} {UZS}",
        f"Сервис {int(service_pct)}%: {fmt_money(service_total)} {UZS}",
        f"💰 Итого: {fmt_money(base_total + service_total)} {UZS}",
        "",
        "👥 Разбивка по участникам:",
    ]
    for i, p in enumerate(participants, start=1):
        name = p.get("name", f"Участник {i}")
        base = int(per_base[i-1])
        svc = int(per_svc[i-1])
        total = base + svc
        lines.append(
            f"{i}. {name} — {fmt_money(total)} {UZS}  "
            f"(до сервиса: {fmt_money(base)} {UZS}, +{fmt_money(svc)} {UZS})"
        )
    return "\n".join(lines)

async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wad = update.message.web_app_data  # type: ignore[attr-defined]
    if not wad:
        return

    try:
        data = json.loads(wad.data or "{}")
    except Exception as e:
        log.exception("Bad web_app_data JSON: %s", e)
        await update.message.reply_text("Не удалось прочитать итог из WebApp.", reply_markup=kb_main())
        return

    text = _format_webapp_message(data)
    await update.message.reply_text(text, reply_markup=kb_main())

# ================== BOOT ==================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Бот запущен (polling). LOG_LEVEL=%s", LOG_LEVEL)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()