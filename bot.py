import os
import re
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    MenuButtonWebApp,
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8388611917:AAF4n76JouI7hpaYHKAHxm0AP2uqZFryQis")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bill-splitter-bot.netlify.app/")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bill_splitter")

UZS = "UZS"
Q3 = Decimal("0.001")  # точность для внутренних дробных порций
Q2 = Decimal("0.01")   # точность для процентов сервиса


# ================== DATA MODELS ==================
@dataclass
class Dish:
    name: str
    qty_total: Decimal            # всего штук в позиции (может быть дробным, напр. 0.7)
    line_total: Decimal           # сумма за всю позицию
    assigned: List[Decimal] = field(default_factory=list)  # по людям: сколько штук назначили

    @property
    def unit_price(self) -> Decimal:
        if self.qty_total == 0:
            return Decimal(0)
        return (self.line_total / self.qty_total).quantize(Q3, rounding=ROUND_HALF_UP)

    def remaining(self) -> Decimal:
        return (self.qty_total - sum(self.assigned)).quantize(Q3)


@dataclass
class Group:
    name: str
    members: List[int]  # индексы участников в Bill.people


@dataclass
class Bill:
    people: List[str] = field(default_factory=list)
    dishes: List[Dish] = field(default_factory=list)
    service_pct: Decimal = Decimal("0")  # 0..100
    groups: List[Group] = field(default_factory=list)

    def ensure_assign_matrix(self):
        for d in self.dishes:
            need = len(self.people) - len(d.assigned)
            if need > 0:
                d.assigned.extend([Decimal(0)] * need)


# чат -> состояние
STATE: Dict[int, Bill] = {}


# ================== HELPERS ==================
def fmt_money(n: int | Decimal) -> str:
    n = int(Decimal(n).quantize(Decimal("1."), rounding=ROUND_HALF_UP))
    return f"{n:,}".replace(",", " ")


def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🧾 Новый счёт"), KeyboardButton("➕ Блюдо"), KeyboardButton("👤 Участник")],
            [KeyboardButton("🍽 Назначить"), KeyboardButton("⚙️ Сервис"), KeyboardButton("🧮 Рассчитать")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def parse_dish_freeform(text: str) -> Tuple[str, Decimal, Decimal]:
    """
    Поддерживаем два формата:
      1) «ассорти 2 шт 28000»
      2) «плов 45000»  (количество = 1)
    Возвращает (name, qty_total, line_total)
    """
    s = text.strip()
    # <name> <qty> шт <sum>
    m = re.search(r"(.*)\s+(\d+(?:[.,]\d+)?)\s*шт\s+(\d+(?:[.,]\d+)?)\s*$", s, flags=re.I)
    if m:
        name = m.group(1).strip()
        qty = Decimal(m.group(2).replace(",", "."))
        line_total = Decimal(m.group(3).replace(",", "."))
        if qty <= 0 or line_total < 0:
            raise ValueError("Количество должно быть > 0, сумма — ≥ 0.")
        return name, qty, line_total

    # <name> <sum>  => qty=1
    m = re.search(r"^(.*)\s+(\d+(?:[.,]\d+)?)\s*$", s)
    if m:
        name = m.group(1).strip()
        qty = Decimal(1)
        line_total = Decimal(m.group(2).replace(",", "."))
        if line_total < 0:
            raise ValueError("Сумма должна быть ≥ 0.")
        return name, qty, line_total

    raise ValueError(
        "Не удалось распознать блюдо. Формат: (название) (количество) шт (сумма) — либо (название) (сумма)."
    )


def person_checkmarks(bill: Bill) -> List[bool]:
    marks = []
    for i, _ in enumerate(bill.people):
        any_assigned = any(d.assigned and i < len(d.assigned) and d.assigned[i] > 0 for d in bill.dishes)
        marks.append(any_assigned)
    return marks


def build_people_keyboard(bill: Bill) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора: участник или группа.
    В конце есть кнопка «➕ Создать группу» и «⬅️ Назад».
    """
    rows: List[List[InlineKeyboardButton]] = []
    marks = person_checkmarks(bill)

    # Участники
    for i, name in enumerate(bill.people):
        mark = " ✅" if marks[i] else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{i+1}. {name}{mark}",
                    callback_data=f"pick_person:{i}",
                )
            ]
        )

    # Группы (если есть)
    for g_idx, g in enumerate(bill.groups):
        rows.append(
            [
                InlineKeyboardButton(
                    g.name,
                    callback_data=f"pick_group:{g_idx}",
                )
            ]
        )

    # Создать группу
    if bill.people:
        rows.append(
            [InlineKeyboardButton("➕ Создать группу", callback_data="create_group")]
        )

    # Назад в главное меню
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

    return InlineKeyboardMarkup(rows)


def build_assign_keyboard_person(bill: Bill, p_idx: int) -> InlineKeyboardMarkup:
    """
    Клавиатура назначения блюд для конкретного участника.
    """
    rows: List[List[InlineKeyboardButton]] = []
    for i, d in enumerate(bill.dishes):
        left = (d.qty_total - sum(d.assigned)).quantize(Q3)
        left_i = int(left) if left >= 0 else 0
        qty_i = int(d.qty_total)
        has_this = d.assigned and p_idx < len(d.assigned) and d.assigned[p_idx] > 0
        mark = " ✅" if has_this else ""
        label = f"{d.name} ({left_i}/{qty_i}){mark}"
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"plus_p:{p_idx}:{i}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("🔄 Очистить выбор", callback_data=f"clear_person:{p_idx}")]
    )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_people")])
    return InlineKeyboardMarkup(rows)


def build_assign_keyboard_group(bill: Bill, g_idx: int) -> InlineKeyboardMarkup:
    """
    Клавиатура назначения блюд для группы участников.
    Каждый тап по блюду добавляет 1 условную порцию,
    которая равномерно делится между всеми участниками группы.
    """
    rows: List[List[InlineKeyboardButton]] = []
    group = bill.groups[g_idx]
    member_ids = group.members

    for i, d in enumerate(bill.dishes):
        # Остаток по блюду
        left = (d.qty_total - sum(d.assigned)).quantize(Q3)
        left_i = max(int(left), 0)
        qty_i = int(d.qty_total)

        # Сколько уже назначено этой группе (суммарно по её участникам)
        group_qty = sum(
            d.assigned[m] for m in member_ids
            if d.assigned and m < len(d.assigned)
        )
        has_this = group_qty > 0
        mark = " ✅" if has_this else ""
        label = f"{d.name} ({left_i}/{qty_i}){mark}"

        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"plus_g:{g_idx}:{i}",
                )
            ]
        )

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_people")])
    return InlineKeyboardMarkup(rows)


def summarize_choices_for_person(bill: Bill, p_idx: int) -> str:
    parts = []
    for d in bill.dishes:
        if d.assigned and p_idx < len(d.assigned) and d.assigned[p_idx] > 0:
            qty = d.assigned[p_idx]
            # показываем целые, если целое; иначе до 3 знаков
            if qty == qty.to_integral():
                qty_str = str(int(qty))
            else:
                qty_str = f"{qty.normalize()}"
            parts.append(f"• {d.name} × {qty_str}")
    return "\n".join(parts) if parts else "—"


def summarize_choices_for_group(bill: Bill, g_idx: int) -> str:
    group = bill.groups[g_idx]
    member_ids = group.members
    parts = []
    for d in bill.dishes:
        group_qty = sum(
            d.assigned[m] for m in member_ids
            if d.assigned and m < len(d.assigned)
        )
        if group_qty > 0:
            if group_qty == group_qty.to_integral():
                qty_str = str(int(group_qty))
            else:
                qty_str = f"{group_qty.normalize()}"
            parts.append(f"• {d.name} × {qty_str}")
    return "\n".join(parts) if parts else "—"


def calc_base_total(bill: Bill) -> Decimal:
    return sum((d.line_total for d in bill.dishes), start=Decimal(0))


def format_dishes_list(bill: Bill) -> str:
    if not bill.dishes:
        return "Нет добавленных блюд"
    lines = []
    for i, d in enumerate(bill.dishes, start=1):
        qty_i = int(d.qty_total)
        unit_i = int(d.unit_price)
        sum_i = int(d.line_total)
        lines.append(f"{i}. {d.name} — {qty_i} шт × {fmt_money(unit_i)} {UZS} = {fmt_money(sum_i)} {UZS}")
    return "\n".join(lines)


# ================== РАСЧЁТ ==================
def compute_summary_details(bill: Bill) -> Tuple[int, int, List[int], List[int]]:
    """
    Возвращает:
      total_no_service (int),
      service_amount_total (int),
      per_person_int (List[int]),
      service_each (List[int])
    ЛОГИКА:
      - каждому начисляем назначенные порции: assigned[i] * unit_price
      - если у блюда остался неназначенный остаток (>0) — делим его поровну между ВСЕМИ
      - округления только в самом конце
    """
    n = max(1, len(bill.people))
    per_person = [Decimal(0)] * n

    # по всем блюдам: назначенные + остаток поровну
    for d in bill.dishes:
        unit = d.unit_price
        assigned_sum = sum(d.assigned) if d.assigned else Decimal(0)
        # назначенное
        for i in range(n):
            take = d.assigned[i] if (d.assigned and i < len(d.assigned)) else Decimal(0)
            if take > 0:
                per_person[i] += (take * unit)
        # остаток (только если реально есть)
        left = (d.qty_total - assigned_sum)
        if left > 0 and n > 0:
            share = (left / n)
            # важно: добавляем именно дробную долю каждому, округление — позже
            for i in range(n):
                per_person[i] += (share * unit)

    # переводим в int c округлением HALF_UP
    per_person_int = [int(x.quantize(Decimal("1."), rounding=ROUND_HALF_UP)) for x in per_person]

    # сумма без сервиса — это сумма по людям (должна совпадать с суммой всех позиций, возможна разница ±1 на округлениях)
    total_no_service = sum(per_person_int)

    # сервис считаем от КАЖДОГО per_person_int (как у вас в примерах)
    service_each = [
        int((Decimal(p) * bill.service_pct / Decimal(100)).quantize(Decimal("1."), rounding=ROUND_HALF_UP))
        for p in per_person_int
    ]
    service_amount_total = sum(service_each)

    return total_no_service, service_amount_total, per_person_int, service_each


# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in STATE:
        STATE[chat_id] = Bill()
    await update.message.reply_text(
        "Добро пожаловать! Используйте кнопки ниже.\n"
        "Чтобы работать в мини-приложении, нажмите «🧮 Open (WebApp)».",
        reply_markup=kb_main(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    bill = STATE.setdefault(chat_id, Bill())

    # Новый счёт
    if text == "🧾 Новый счёт":
        STATE[chat_id] = Bill()
        await update.message.reply_text("Новый счёт начат. Добавьте блюда и участников.", reply_markup=kb_main())
        return

    # Настройка сервиса
    if text == "⚙️ Сервис":
        context.user_data["mode"] = "svc"
        await update.message.reply_text(
            "Пожалуйста, введите процент сервиса (целое число 0–100):",
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

        base_total = calc_base_total(bill)
        service_total = (base_total * bill.service_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP)
        total = base_total + service_total

        dishes_block = format_dishes_list(bill)
        msg = (
            f"✅ Процент сервиса установлен: {pct}%\n\n"
            f"📋 Список блюд:\n{dishes_block}\n\n"
            f"🧮 Итого без сервиса: {fmt_money(base_total)} {UZS}\n"
            f"🧾 Сервис {pct}%: {fmt_money(service_total)} {UZS}\n"
            f"💰 Итого к оплате: {fmt_money(total)} {UZS}"
        )
        await update.message.reply_text(msg, reply_markup=kb_main())
        return

    # Добавление блюда
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
            name, qty, line_total = parse_dish_freeform(text)
        except Exception as e:
            await update.message.reply_text(str(e))
            return

        d = Dish(name=name, qty_total=qty, line_total=line_total)
        d.assigned = [Decimal(0)] * len(bill.people)
        bill.dishes.append(d)
        context.user_data.pop("mode", None)

        dishes_block = format_dishes_list(bill)
        base_total = calc_base_total(bill)
        msg = (
            f"✅ Блюдо добавлено: {name} — {int(qty)} шт × {fmt_money(int(d.unit_price))} {UZS} = {fmt_money(int(line_total))} {UZS}\n\n"
            f"📋 Список блюд:\n{dishes_block}\n\n"
            f"🧮 Сумма без сервиса: {fmt_money(base_total)} {UZS}"
        )
        if bill.service_pct and bill.service_pct > 0:
            service_total = (base_total * bill.service_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP)
            msg += (
                f"\n🧾 Сервис {int(bill.service_pct)}%: {fmt_money(service_total)} {UZS}"
                f"\n💰 Итого: {fmt_money(base_total + service_total)} {UZS}"
            )
        await update.message.reply_text(msg, reply_markup=kb_main())
        return

    # Добавление участника
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

    # Назначение блюд
    if text == "🍽 Назначить":
        if not bill.people or not bill.dishes:
            await update.message.reply_text("Сначала добавьте блюда и участников.", reply_markup=kb_main())
            return
        await update.message.reply_text(
            "Кому назначаем? Выберите участника или группу:",
            reply_markup=build_people_keyboard(bill),
        )
        return

    # Итоговый расчёт
    if text == "🧮 Рассчитать":
        if not bill.people or not bill.dishes:
            await update.message.reply_text("Нужно добавить блюда и участников.", reply_markup=kb_main())
            return

        base_total, service_total, per_base, per_svc = compute_summary_details(bill)

        lines = [
            "🧮 Итоговый расчёт:",
            f"Без сервиса: {fmt_money(base_total)} {UZS}",
            f"Сервис {int(bill.service_pct)}%: {fmt_money(service_total)} {UZS}",
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

    # Фолбэк
    await update.message.reply_text("Выберите действие ниже.", reply_markup=kb_main())


# ================== GROUP SELECT UI ==================
def build_group_select_keyboard(bill: Bill, selected: List[int]) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора участников для новой группы.
    """
    rows: List[List[InlineKeyboardButton]] = []
    selected_set = set(selected)

    for i, name in enumerate(bill.people):
        mark = " ✅" if i in selected_set else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{i+1}. {name}{mark}",
                    callback_data=f"group_toggle:{i}",
                )
            ]
        )

    # Управляющие кнопки
    rows.append(
        [
            InlineKeyboardButton("🔄 Очистить", callback_data="group_clear"),
            InlineKeyboardButton("✔️ Готово", callback_data="group_done"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="group_cancel")])

    return InlineKeyboardMarkup(rows)


async def show_group_select_screen(
    update: Update,
    bill: Bill,
    selected: List[int],
    flash: Optional[str] = None,
):
    """
    Показ экрана выбора участников для группы.
    """
    query = update.callback_query
    text = "Выберите участников, которые войдут в группу.\n" \
           "Нажимайте по именам, чтобы отметить или снять отметку.\n" \
           "Группа должна содержать минимум двух участников."
    if flash:
        text = flash + "\n\n" + text

    await query.edit_message_text(
        text,
        reply_markup=build_group_select_keyboard(bill, selected),
    )


# ================== CALLBACKS (назначение + группы) ==================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    bill = STATE.setdefault(chat_id, Bill())
    data = query.data or ""

    # Назад в главное меню
    if data == "back_main":
        await query.edit_message_text("Главное меню. Выберите действие.", reply_markup=kb_main())
        return

    # Назад к списку участников/групп
    if data in ("back_people", "assign_back"):
        await query.edit_message_text(
            "Выберите участника или группу:",
            reply_markup=build_people_keyboard(bill),
        )
        return

    # ---- Работа с группами ----
    # Начать создание группы
    if data == "create_group":
        if not bill.people:
            await query.edit_message_text(
                "Сначала добавьте хотя бы одного участника.",
                reply_markup=build_people_keyboard(bill),
            )
            return
        context.user_data["group_selected_indices"] = []
        await show_group_select_screen(update, bill, [])
        return

    # Тоггл участника при выборе группы
    if data.startswith("group_toggle:"):
        try:
            idx = int(data.split(":")[1])
        except Exception:
            return
        selected: List[int] = context.user_data.get("group_selected_indices", [])
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        context.user_data["group_selected_indices"] = selected
        await show_group_select_screen(update, bill, selected)
        return

    # Очистить выбор при создании группы
    if data == "group_clear":
        context.user_data["group_selected_indices"] = []
        await show_group_select_screen(update, bill, [])
        return

    # Отмена создания группы
    if data == "group_cancel":
        context.user_data.pop("group_selected_indices", None)
        await query.edit_message_text(
            "Выберите участника или группу:",
            reply_markup=build_people_keyboard(bill),
        )
        return

    # Завершить создание группы
    if data == "group_done":
        selected: List[int] = context.user_data.get("group_selected_indices", [])
        if len(selected) < 2:
            await show_group_select_screen(
                update,
                bill,
                selected,
                flash="❗ Группа должна содержать минимум двух участников.",
            )
            return
        # Создаём группу
        group_idx = len(bill.groups) + 1
        names = [bill.people[i] for i in selected]
        name = f"Группа {group_idx} ({', '.join(names)})"
        bill.groups.append(Group(name=name, members=selected.copy()))
        context.user_data.pop("group_selected_indices", None)

        await query.edit_message_text(
            "Группа создана.\n\nВыберите участника или группу:",
            reply_markup=build_people_keyboard(bill),
        )
        return

    # ---- Выбор участника или группы для назначения ----
    if data.startswith("pick_person:"):
        try:
            p_idx = int(data.split(":")[1])
        except Exception:
            return
        await show_assign_screen_person(update, bill, p_idx)
        return

    if data.startswith("pick_group:"):
        try:
            g_idx = int(data.split(":")[1])
        except Exception:
            return
        if g_idx < 0 or g_idx >= len(bill.groups):
            return
        await show_assign_screen_group(update, bill, g_idx)
        return

    # Очистить назначения конкретного участника
    if data.startswith("clear_person:"):
        try:
            p_idx = int(data.split(":")[1])
        except Exception:
            return
        bill.ensure_assign_matrix()
        if 0 <= p_idx < len(bill.people):
            for d in bill.dishes:
                if d.assigned and p_idx < len(d.assigned):
                    d.assigned[p_idx] = Decimal(0)
        await show_assign_screen_person(update, bill, p_idx, flash="🧹 Выбор очищен.")
        return

    # Назначение +1 шт конкретному участнику
    if data.startswith("plus_p:"):
        try:
            _, p_s, d_s = data.split(":")
            p_idx, d_idx = int(p_s), int(d_s)
        except Exception:
            return
        bill.ensure_assign_matrix()
        if d_idx < 0 or d_idx >= len(bill.dishes) or p_idx < 0 or p_idx >= len(bill.people):
            return
        d = bill.dishes[d_idx]
        # проверяем остаток
        if (sum(d.assigned) + Decimal(1)) > d.qty_total:
            await show_assign_screen_person(update, bill, p_idx, flash="❗ Остатка по позиции нет.")
            return
        d.assigned[p_idx] = d.assigned[p_idx] + Decimal(1)
        await show_assign_screen_person(update, bill, p_idx)
        return

    # Назначение 1 условной порции группе (делится поровну между всеми участниками группы)
    if data.startswith("plus_g:"):
        try:
            _, g_s, d_s = data.split(":")
            g_idx, d_idx = int(g_s), int(d_s)
        except Exception:
            return
        bill.ensure_assign_matrix()
        if g_idx < 0 or g_idx >= len(bill.groups) or d_idx < 0 or d_idx >= len(bill.dishes):
            return
        group = bill.groups[g_idx]
        d = bill.dishes[d_idx]

        # Проверяем остаток по блюду (1 условная порция)
        if (sum(d.assigned) + Decimal(1)) > d.qty_total:
            await show_assign_screen_group(update, bill, g_idx, flash="❗ Остатка по позиции нет.")
            return

        members = [m for m in group.members if 0 <= m < len(bill.people)]
        if not members:
            await show_assign_screen_group(update, bill, g_idx, flash="❗ В группе нет валидных участников.")
            return

        share = (Decimal(1) / Decimal(len(members))).quantize(Q3, rounding=ROUND_HALF_UP)
        # Чтобы сумма ровно давала 1, последнему участнику можно скорректировать
        total_added = Decimal(0)
        for idx, m in enumerate(members):
            if idx < len(members) - 1:
                d.assigned[m] = d.assigned[m] + share
                total_added += share
            else:
                # последнему — остаток до 1.0
                last_share = Decimal(1) - total_added
                d.assigned[m] = d.assigned[m] + last_share

        await show_assign_screen_group(update, bill, g_idx)
        return


async def show_assign_screen_person(
    update: Update,
    bill: Bill,
    p_idx: int,
    flash: Optional[str] = None,
):
    bill.ensure_assign_matrix()
    if p_idx < 0 or p_idx >= len(bill.people):
        return
    chosen = summarize_choices_for_person(bill, p_idx)
    head = (
        (flash + "\n\n") if flash else ""
    ) + f"👤 Участник: *{bill.people[p_idx]}*\n" \
        f"Нажимайте на блюдо — каждый тап добавляет 1 шт (если есть остаток).\n\n" \
        f"🧾 Выбранные для участника:\n{chosen}"

    await update.callback_query.edit_message_text(
        head,
        parse_mode="Markdown",
        reply_markup=build_assign_keyboard_person(bill, p_idx)
    )


async def show_assign_screen_group(
    update: Update,
    bill: Bill,
    g_idx: int,
    flash: Optional[str] = None,
):
    bill.ensure_assign_matrix()
    if g_idx < 0 or g_idx >= len(bill.groups):
        return
    group = bill.groups[g_idx]
    chosen = summarize_choices_for_group(bill, g_idx)
    head = (
        (flash + "\n\n") if flash else ""
    ) + f"👥 Группа: *{group.name}*\n" \
        f"Нажимайте на блюдо — каждый тап добавляет 1 порцию,\n" \
        f"которая равномерно делится между всеми участниками группы.\n\n" \
        f"🧾 Выбранные для группы:\n{chosen}"

    await update.callback_query.edit_message_text(
        head,
        parse_mode="Markdown",
        reply_markup=build_assign_keyboard_group(bill, g_idx)
    )


# ================== HANDLER ДАННЫХ ИЗ WEBAPP ==================
def _format_webapp_message(data: dict) -> str:
    log.info("WEBAPP RAW DATA: %s", data)

    service_pct = Decimal(str(data.get("servicePercent", 0)))
    participants = data.get("participants", [])
    dishes = data.get("dishes", [])
    groups_data = data.get("groups", [])

    log.info("Parsed service_pct=%s", service_pct)
    log.info("Participants=%s", participants)
    log.info("Dishes=%s", dishes)
    log.info("Groups=%s", groups_data)

    if not participants or not dishes:
        log.warning("NO PARTICIPANTS OR DISHES RECEIVED")
        return "Нет данных для расчёта."

    # id -> index
    id_to_idx: Dict[str, int] = {
        str(p.get("id")): i for i, p in enumerate(participants) if p.get("id") is not None
    }
    log.info("id_to_idx map: %s", id_to_idx)

    # group_id -> [participant_indices]
    group_map: Dict[str, List[int]] = {}
    for g in groups_data:
        g_id = str(g.get("id"))
        member_ids = g.get("memberIds", [])
        indices = []
        for mid in member_ids:
            if str(mid) in id_to_idx:
                indices.append(id_to_idx[str(mid)])
        group_map[g_id] = indices
    log.info("group_map: %s", group_map)

    per_base = [Decimal(0) for _ in participants]
    base_total = Decimal(0)

    for d in dishes:
        log.info("Processing dish: %s", d)

        qty = Decimal(str(d.get("qty", 0)))
        total_price = Decimal(str(d.get("totalPrice", 0)))

        if qty <= 0:
            log.warning("Dish qty <= 0, skipping")
            continue

        base_total += total_price
        unit = (total_price / qty).quantize(Q3, rounding=ROUND_HALF_UP)

        log.info("Dish qty=%s, total=%s, unit=%s", qty, total_price, unit)

        # flatAssignments -> корректный источник
        raw_assignments = d.get("flatAssignments", None)
        assignments = []

        if isinstance(raw_assignments, list):
            # flat: [participantId|groupId|null, ...]
            assignments = [str(a) if a not in (None, "") else None for a in raw_assignments]
            log.info("Using flatAssignments=%s", assignments)
        else:
            # legacy matrix fallback
            matrix = d.get("assignments", [])
            log.info("Legacy 'assignments' matrix: %s", matrix)
            for unit_assignees in matrix:
                pid = None
                if isinstance(unit_assignees, list):
                    for a in unit_assignees:
                        if isinstance(a, dict) and a.get("type") == "participant":
                            pid = str(a.get("id"))
                            break
                assignments.append(pid)

        qty_int = int(qty)
        if len(assignments) < qty_int:
            assignments.extend([None] * (qty_int - len(assignments)))

        log.info("Final assignments expanded=%s", assignments)

        assigned_units = Decimal(0)

        # назначенные единицы
        for a in assignments[:qty_int]:
            if a is None:
                continue
            
            # 1. Проверяем, это участник?
            if a in id_to_idx:
                idx = id_to_idx[a]
                per_base[idx] += unit
                assigned_units += Decimal(1)
                log.info("Assigned 1 unit to Person %s → idx=%s", a, idx)
            
            # 2. Проверяем, это группа?
            elif a in group_map:
                members = group_map[a]
                if members:
                    share = unit / Decimal(len(members))
                    for m_idx in members:
                        per_base[m_idx] += share
                    assigned_units += Decimal(1)
                    log.info("Assigned 1 unit to Group %s → members=%s, share_each=%s", a, members, share)
                else:
                    log.warning("Group %s has no members, skipping assignment", a)

        # остаток поровну всем
        left = qty - assigned_units
        if left > 0 and len(participants) > 0:
            share = left / Decimal(len(participants))
            log.info("Leftover=%s, share_each=%s", left, share)
            for i in range(len(participants)):
                per_base[i] += (share * unit)

    log.info("PER BASE BEFORE ROUNDING: %s", per_base)

    # сервис
    per_svc = [
        (b * service_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP)
        for b in per_base
    ]
    log.info("PER SERVICE: %s", per_svc)

    service_total = (base_total * service_pct / Decimal(100)).quantize(Q2, rounding=ROUND_HALF_UP)

    log.info("TOTAL base=%s, service_total=%s, grand=%s",
             base_total, service_total, base_total + service_total)

    # Формируем сообщение
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

        base_i = int(per_base[i - 1].quantize(Decimal("1."), rounding=ROUND_HALF_UP))
        svc_i = int(per_svc[i - 1].quantize(Decimal("1."), rounding=ROUND_HALF_UP))
        total_i = base_i + svc_i

        log.info("Participant %s — base=%s, svc=%s, total=%s",
                 name, base_i, svc_i, total_i)

        lines.append(
            f"{i}. {name} — {fmt_money(total_i)} {UZS}  "
            f"(до сервиса: {fmt_money(base_i)} {UZS}, +{fmt_money(svc_i)} {UZS})"
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
async def post_init(application: Application) -> None:
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="OPEN", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    log.info("Menu button set: %s", WEBAPP_URL)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Бот запущен (polling). LOG_LEVEL=%s", LOG_LEVEL)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
