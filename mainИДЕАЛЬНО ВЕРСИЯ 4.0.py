import asyncio
import logging
import os
import re
from pathlib import Path
import pandas as pd
from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.filters import Command

# ================= НАСТРОЙКИ =================
TOKEN = os.getenv("BOT_TOKEN", "8021456879:AAEQ4cRgiz-bD6Pb8l4jxKG-x7a_TM7RgLA")
logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

BASE_DIR = Path(__file__).parent
BASE_DIR.mkdir(exist_ok=True)

# Хранилище данных пользователя
users = {}

# ================= КНОПКИ =================
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Перезагрузка")],
        [KeyboardButton(text="📥 Скачать шаблон себестоимости")],
        [KeyboardButton(text="📥 Скачать шаблон себестоимости по WB")]
    ],
    resize_keyboard=True
)

tax_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="6%"), KeyboardButton(text="7%")],
        [KeyboardButton(text="15%"), KeyboardButton(text="Без налога")]
    ],
    resize_keyboard=True
)

# ================= УТИЛИТЫ =================
def normalize(text: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", str(text).lower())

def find_col(df, keywords):
    for col in df.columns:
        n = normalize(col)
        if any(k in n for k in keywords):
            return col
    return None

def to_number(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",", ".").str.replace(r"[^\d.-]", "", regex=True),
        errors="coerce"
    ).fillna(0)

# ================= ХЕНДЛЕРЫ =================
@dp.message(Command(commands=["start"]))
async def start(msg: Message):
    await msg.answer(
        "Привет! Пришли мне 2 файла: отчет WB и файл себестоимости.",
        reply_markup=main_keyboard
    )

@dp.message(lambda m: m.text == "🔄 Перезагрузка")
async def reload(msg: Message):
    users.pop(msg.from_user.id, None)
    await start(msg)

@dp.message(lambda m: m.text == "📥 Скачать шаблон себестоимости")
async def send_template(msg: Message):
    path = BASE_DIR / "template_cost.xlsx"
    df = pd.DataFrame(columns=["Артикул поставщика", "Себестоимость"])
    df.to_excel(path, index=False)
    await msg.answer_document(FSInputFile(path), caption="Заполните файл вашей себестоимостью")

@dp.message(lambda m: m.text == "📥 Скачать шаблон себестоимости по WB")
async def send_template_wb(msg: Message):
    uid = msg.from_user.id
    users.setdefault(uid, {})["await_wb_template"] = True
    await msg.answer("Пришлите файл отчета Wildberries, чтобы создать шаблон себестоимости по нему.")

@dp.message(lambda m: m.document is not None)
async def handle_docs(msg: Message):
    uid = msg.from_user.id
    user_dir = BASE_DIR / str(uid)
    user_dir.mkdir(exist_ok=True)

    if users.get(uid, {}).get("await_wb_template"):
        path = user_dir / msg.document.file_name
        file = await bot.get_file(msg.document.file_id)
        await bot.download_file(file.file_path, path)
        df_wb = pd.read_excel(path, engine="openpyxl")
        art_col = find_col(df_wb, ["артикулпоставщика"])
        if not art_col:
            await msg.answer("Не удалось найти колонку 'Артикул поставщика' в отчете WB.")
            return
        unique_arts = df_wb[art_col].astype(str).str.strip().drop_duplicates()
        template = pd.DataFrame({"Артикул поставщика": unique_arts, "Себестоимость": ""})
        template_path = user_dir / "template_cost_from_wb.xlsx"
        template.to_excel(template_path, index=False)
        await msg.answer_document(FSInputFile(template_path), caption="Заполненный шаблон готов! Впишите себестоимость для каждого артикула.")
        users[uid]["await_wb_template"] = False
        return

    users.setdefault(uid, {})["docs"] = users.get(uid, {}).get("docs", [])
    users[uid]["docs"].append(msg.document)

    if len(users[uid]["docs"]) < 2:
        await msg.answer("Первый файл получен. Жду второй.")
        return

    await msg.answer("Файлы приняты! Выберите ставку налога:", reply_markup=tax_keyboard)

@dp.message(lambda m: m.text in ["6%", "7%", "15%", "Без налога"])
async def calculate_all(msg: Message):
    uid = msg.from_user.id
    if uid not in users or "docs" not in users[uid]:
        await msg.answer("Сначала пришлите файлы!", reply_markup=main_keyboard)
        return

    tax_rate = {"6%": 0.06, "7%": 0.07, "15%": 0.15, "Без налога": 0.0}.get(msg.text, 0.0)
    docs = users[uid]["docs"]
    user_dir = BASE_DIR / str(uid)
    user_dir.mkdir(exist_ok=True)

    try:
        wb, costs = None, None
        for d in docs:
            path = user_dir / d.file_name
            file = await bot.get_file(d.file_id)
            await bot.download_file(file.file_path, path)
            df = pd.read_excel(path, engine="openpyxl")
            if find_col(df, ["кперечислению"]): 
                wb = df
            elif find_col(df, ["себестоим"]): 
                costs = df

        if wb is None or costs is None:
            await msg.answer("❌ Не найден отчет WB или файл себестоимости.")
            return

        # Колонки WB
        pay_col = find_col(wb, ["кперечислению"])
        sold_col = find_col(wb, ["вайлдберризреализовал"])
        logistics_col = find_col(wb, ["услугиподоставке"])
        fine_col = find_col(wb, ["общаясуммаштрафов"])
        store_col = find_col(wb, ["хранение"])
        reason_col = find_col(wb, ["обоснованиедляоплаты"])
        art_col_wb = find_col(wb, ["артикулпоставщика"])
        qty_col = find_col(wb, ["колво"])
        deduction_col = find_col(wb, ["удержания", "удержание"])  # ✅ удержания

        for col in [pay_col, sold_col, logistics_col, fine_col, store_col, qty_col, deduction_col]:
            if col: wb[col] = to_number(wb[col])

        # Себестоимость
        cost_val_col = find_col(costs, ["себестоим"])
        art_col_costs = find_col(costs, ["артикулпоставщика"])
        costs[cost_val_col] = to_number(costs[cost_val_col])
        costs[art_col_costs] = costs[art_col_costs].astype(str).str.strip()
        costs_clean = costs[[art_col_costs, cost_val_col]].drop_duplicates(subset=[art_col_costs])

        # --- Только продажи ---
        sales_only = wb[wb[reason_col].astype(str).str.contains("Продажа", case=False)].copy()
        sales_only[art_col_wb] = sales_only[art_col_wb].astype(str).str.strip()
        sales_merged = sales_only.merge(costs_clean, left_on=art_col_wb, right_on=art_col_costs, how='left')
        sales_merged['line_cost'] = sales_merged[qty_col] * sales_merged[cost_val_col].fillna(0)
        total_cost_sum = sales_merged['line_cost'].sum()

        # Расходы и налог
        total_pay = wb[pay_col].sum() if pay_col else 0
        total_sold = wb[sold_col].sum() if sold_col else 0
        total_logistics = wb[logistics_col].sum() if logistics_col else 0
        total_fine = wb[fine_col].sum() if fine_col else 0
        total_store = wb[store_col].sum() if store_col else 0
        total_deductions = wb[deduction_col].sum() if deduction_col else 0  # ✅ удержания
        tax_amount = total_sold * tax_rate

        profit = total_pay - total_logistics - total_fine - total_store - tax_amount - total_cost_sum - total_deductions

        await msg.answer(
            f"📊 **ИТОГ ПО ОТЧЕТУ**\n"
            f"Налог: {msg.text}\n\n"
            f"💰 Реализовано: {total_sold:,.2f} ₽\n"
            f"💳 К перечислению: {total_pay:,.2f} ₽\n"
            f"🚚 Логистика: {total_logistics:,.2f} ₽\n"
            f"📦 Хранение: {total_store:,.2f} ₽\n"
            f"⚠️ Штрафы: {total_fine:,.2f} ₽\n"
            f"📑 Налог: {tax_amount:,.2f} ₽\n"
            f"💸 Удержания: {total_deductions:,.2f} ₽\n"
            f"👟 Себестоимость: {total_cost_sum:,.2f} ₽\n\n"
            f"✅ **ЧИСТАЯ ПРИБЫЛЬ: {profit:,.2f} ₽**",
            reply_markup=main_keyboard
        )

        # Очистка
        users.pop(uid, None)

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await msg.answer(f"❌ Произошла ошибка: {e}")

# ================= ЗАПУСК =================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
