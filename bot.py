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
        return self.line_total / self.qty_total   # точная цена за 1 шт

    @property
    def qty_left(self) -> Decimal:
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

# чат -> состояние (для режима через меню бота)
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
    )

def parse_dish_freeform(text: str) -> Tuple[str, Decimal, Decimal]:
    """
    Поддерживает:
      1) '(название) (N) шт (цена_за_всю_позицию)' -> qty=N, line_total=цена_позиции
      2) '(название) (цена_позиции)'               -> qty=1
    """
    t = text.strip()

    m = re.search(
        r"^(?P<name>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*шт(?:ук)?\s+(?P<price>\d[\d\s]*)$",
        t, re.IGNORECASE
    )
    if m:
        name = m.group("name").strip()
        qty = Decimal(m.group("qty").replace(",", "."))
        line_total = Decimal(m.group("price").replace(" ", ""))
        return name, qty, line_total

    m = re.search(r"^(?P<name>.+?)\s+(?P<price>\d[\d\s]*)$", t)
    if m:
        name = m.group("name").strip()
        qty = Decimal("1")
        line_total = Decimal(m.group("price").replace(" ", ""))
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
    rows.append([InlineKeyboardButton("🔄 Очистить выбор", callback_data=f"clear_person:{p_idx}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_people")])
    return InlineKeyboardMarkup(rows)

def summarize_choices_for_person(bill: Bill, p_idx: int) -> str:
    parts = []
    for d in bill.dishes:
        if d.assigned and d.assigned[p_idx] > 0:
            parts.append(f"• {d.name} × {int(d.assigned[p_idx])}")
    return "\n".join(parts) if parts else "—"

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Добро пожаловать! Вы можете ввести счёт вручную или открыть WebApp (кнопка ниже).",
        reply_markup=kb_main()
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    bill = STATE.setdefault(chat_id, Bill())

    if text == "🧾 Новый счёт":
        STATE[chat_id] = Bill()
        await update.message.reply_text("🧾 Создан новый счёт.", reply_markup=kb_main())
        return

    if text == "➕ Блюдо":
        context.user_data["mode"] = "add_dish"
        await update.message.reply_text(
            "🍽 Введите название блюда.\n"
            "Можно сразу так: (название блюда) (количество) шт (цена).",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True),
        )
        return

    if context.user_data.get("mode") == "add_dish":
        if text == "Отмена":
            context.user_data.pop("mode", None)
            await update.message.reply_text("Действие отменено.", reply_markup=kb_main())
            return
        try:
            name, qty, line_total = parse_dish_freeform(text)
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        d = Dish(name=name, qty_total=qty, line_total=line_total)
        d.assigned = [Decimal(0)] * len(bill.people)
        bill.dishes.append(d)
        context.user_data.pop("mode", None)

        # 👉 Формируем список с суммами: «qty × unit = line_total»
        def unit_i(x: Dish) -> int:
            return int(x.unit_price.to_integral_value(rounding=ROUND_HALF_UP))

        items = "\n".join(
            f"{i+1}. {x.name} — {int(x.qty_total)} шт × {fmt_money(unit_i(x))} {UZS} = {fmt_money(int(x.line_total))} {UZS}"
            for i, x in enumerate(bill.dishes)
        )
        await update.message.reply_text(
            f"✅ Блюдо добавлено: {d.name} — {int(d.qty_total)} шт × {fmt_money(unit_i(d))} {UZS} = {fmt_money(int(d.line_total))} {UZS}\n"
            f"📋 Список блюд:\n{items}",
            reply_markup=kb_main()
        )
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
            await update.message.reply_text("Пожалуйста, добавьте хотя бы одно блюдо и одного участника.", reply_markup=kb_main())
            return
        await update.message.reply_text("Выберите участника:", reply_markup=build_people_keyboard(bill))
        return

    if text == "⚙️ Сервис":
        context.user_data["mode"] = "set_service"
        await update.message.reply_text(
            f"Текущий сервис: {bill.service_pct}%.\nВведите новое значение от 0 до 30 или нажмите «Отмена».",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True),
        )
        return

    if context.user_data.get("mode") == "set_service":
        if text == "Отмена":
            context.user_data.pop("mode", None)
            await update.message.reply_text("Действие отменено.", reply_markup=kb_main())
            return
        try:
            p = Decimal(text)
            if p < 0 or p > 30:
                raise ValueError
        except Exception:
            await update.message.reply_text("Введите число от 0 до 30 или нажмите «Отмена».")
            return
        bill.service_pct = p
        context.user_data.pop("mode", None)
        await update.message.reply_text(f"Сервис установлен: {bill.service_pct}%.", reply_markup=kb_main())
        return

    if text == "🧮 Рассчитать":
        await send_summary(update, bill)
        return

    await update.message.reply_text("Не удалось распознать команду. Пожалуйста, используйте кнопки ниже.", reply_markup=kb_main())

# ================== CALLBACKS (меню бота) ==================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    bill = STATE.setdefault(chat_id, Bill())
    log.debug("CALLBACK '%s'", data)

    if data == "back_main":
        await query.edit_message_text("Главное меню. Выберите дальнейшее действие.")
        await query.message.reply_text("Пожалуйста, выберите действие на клавиатуре ниже.", reply_markup=kb_main())
        return

    if data in ("back_people", "assign_back"):
        await query.edit_message_text("Выберите участника:", reply_markup=build_people_keyboard(bill))
        return

    if data.startswith("pick_person:"):
        p_idx = int(data.split(":")[1])
        await show_assign_screen(update, bill, p_idx)
        return

    if data.startswith("clear_person:"):
        p_idx = int(data.split(":")[1])
        bill.ensure_assign_matrix()
        for d in bill.dishes:
            d.assigned[p_idx] = Decimal(0)
        await show_assign_screen(update, bill, p_idx, flash="🧹 Выбор очищен.")
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

# ================== SUMMARY (для режима меню бота) ==================
def compute_summary_details(bill: Bill):
    n = max(1, len(bill.people))
    per_person = [Decimal(0)] * n

    # назначенные порции
    for d in bill.dishes:
        unit = d.unit_price
        assigned_sum = sum(d.assigned) if d.assigned else Decimal(0)
        left = (d.qty_total - assigned_sum)
        for i in range(n):
            take = d.assigned[i] if i < len(d.assigned) else Decimal(0)
            per_person[i] += (take * unit)
        if left > 0:
            share = (left / n)
            for i in range(n):
                per_person[i] += (share * unit)

    per_person_int = [int(x.quantize(Decimal("1."), rounding=ROUND_HALF_UP)) for x in per_person]
    total_no_service = sum(per_person_int)

    service_each = [
        int((Decimal(p) * bill.service_pct / Decimal(100)).quantize(Decimal("1."), rounding=ROUND_HALF_UP))
        for p in per_person_int
    ]
    service_amount_total = sum(service_each)
    return total_no_service, service_amount_total, per_person_int, service_each

async def send_summary(update: Update, bill: Bill):
    if not bill.people or not bill.dishes:
        await update.message.reply_text("Пожалуйста, добавьте участников и блюда.", reply_markup=kb_main())
        return

    base_total, service_total, per_base, per_svc = compute_summary_details(bill)
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

# ================== HANDLER ДАННЫХ ИЗ WEBAPP ==================
def _format_webapp_message(data: dict) -> str:
    """Форматируем сообщение строго по заданному шаблону."""
    def g(key, default=0):
        return int(data.get(key, default))

    base_total   = g("base_total")
    service_pct  = int(data.get("service_pct", 0))
    service_total= g("service_total")
    total        = g("total")

    lines = [
        "🧮 Итоговый расчёт:",
        f"Без сервиса: {fmt_money(base_total)} {UZS}",
        f"Сервис {service_pct}%: {fmt_money(service_total)} {UZS}",
        f"💰 Итого: {fmt_money(total)} {UZS}",
        "",
        "👥 Разбивка по участникам:",
    ]

    people = data.get("people", [])
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

async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем данные, которые пришли из WebApp через Telegram.WebApp.sendData(JSON).
    Эти данные автоматически привязаны к текущему пользователю: сообщение уходит в тот же чат.
    """
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
    # базовая логика бота (меню и web-app data) — на polling
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))  # из WebApp
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Бот запущен (polling). LOG_LEVEL=%s", LOG_LEVEL)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
