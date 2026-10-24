import asyncio
import base64
import logging
import os
import random
import sqlite3
from datetime import date
from io import BytesIO

import yaml
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
logging.basicConfig(level=logging.INFO)

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

ALLOWED = set(CFG["allowed_users"])
DAILY_LIMIT = int(CFG["daily_limit"])
TASKS = {str(k): v for k, v in CFG["tasks"].items()}

# ---------- LLM (Gemini и OpenAI используют один и тот же клиент) ----------
provider = CFG["llm"]["provider"]
pcfg = CFG["llm"][provider]
llm = AsyncOpenAI(
    api_key=os.environ[pcfg["api_key_env"]],
    base_url=pcfg.get("base_url"),  # для openai можно оставить null
)
MODEL = pcfg["model"]

# ---------- БД: лимиты и текущая задача ----------
db = sqlite3.connect("bot.db")
db.execute("CREATE TABLE IF NOT EXISTS usage(user_id INT, day TEXT, cnt INT, PRIMARY KEY(user_id, day))")
db.execute("CREATE TABLE IF NOT EXISTS pending(user_id INT PRIMARY KEY, task_key TEXT, problem TEXT)")
db.commit()


def used_today(uid: int) -> int:
    row = db.execute("SELECT cnt FROM usage WHERE user_id=? AND day=?", (uid, str(date.today()))).fetchone()
    return row[0] if row else 0


def add_usage(uid: int):
    db.execute(
        "INSERT INTO usage VALUES(?,?,1) ON CONFLICT(user_id, day) DO UPDATE SET cnt=cnt+1",
        (uid, str(date.today())),
    )
    db.commit()


def get_pending(uid: int):
    return db.execute("SELECT task_key, problem FROM pending WHERE user_id=?", (uid,)).fetchone()


def set_pending(uid: int, key: str, problem: str):
    db.execute("INSERT OR REPLACE INTO pending VALUES(?,?,?)", (uid, key, problem))
    db.commit()


def clear_pending(uid: int):
    db.execute("DELETE FROM pending WHERE user_id=?", (uid,))
    db.commit()


# ---------- Доступ только для пользователей из конфига ----------
class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in ALLOWED:
            if isinstance(event, Message):
                await event.answer(f"Нет доступа. Твой ID: {user.id if user else '?'}")
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return
        return await handler(event, data)


dp = Dispatcher()
dp.message.outer_middleware(WhitelistMiddleware())
dp.callback_query.outer_middleware(WhitelistMiddleware())


def menu_kb() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=f"Задача {k}", callback_data=f"task:{k}") for k in TASKS]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def send_long(message: Message, text: str):
    for i in range(0, len(text), 4000):
        await message.answer(text[i : i + 4000])


@dp.message(Command("start"))
async def start(message: Message):
    left = DAILY_LIMIT - used_today(message.from_user.id)
    await message.answer(f"Выбери номер задачи. Осталось попыток сегодня: {max(left, 0)}", reply_markup=menu_kb())


@dp.message(Command("skip"))
async def skip(message: Message):
    clear_pending(message.from_user.id)
    await message.answer("Задача сброшена (попытка не возвращается). Выбери новую:", reply_markup=menu_kb())


@dp.callback_query(F.data.startswith("task:"))
async def choose_task(cb: CallbackQuery):
    uid = cb.from_user.id
    key = cb.data.split(":")[1]
    await cb.answer()

    if get_pending(uid):
        await cb.message.answer("Сначала пришли фото решения текущей задачи (или /skip).")
        return
    if used_today(uid) >= DAILY_LIMIT:
        await cb.message.answer(f"Лимит на сегодня ({DAILY_LIMIT}) исчерпан. Приходи завтра!")
        return

    problem = random.choice(TASKS[key]["problems"])
    add_usage(uid)
    set_pending(uid, key, problem)
    left = DAILY_LIMIT - used_today(uid)
    await cb.message.answer(f"Задача {key}:\n\n{problem}\n\nПришли фото решения. Осталось попыток: {left}")


@dp.message(F.photo)
async def check_solution(message: Message, bot: Bot):
    uid = message.from_user.id
    pending = get_pending(uid)
    if not pending:
        await message.answer("Сначала выбери задачу:", reply_markup=menu_kb())
        return
    key, problem = pending

    wait = await message.answer("Проверяю решение, подожди немного…")
    buf = BytesIO()
    await bot.download(message.photo[-1], destination=buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    try:
        resp = await llm.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": TASKS[key]["prompt"]},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Условие задачи:\n{problem}\n\nНиже фото решения ученика."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    ],
                },
            ],
        )
        answer = resp.choices[0].message.content
    except Exception:
        logging.exception("LLM error")
        await wait.edit_text("Ошибка при проверке. Отправь фото ещё раз.")
        return

    clear_pending(uid)
    await wait.delete()
    await send_long(message, answer)
    left = DAILY_LIMIT - used_today(uid)
    if left > 0:
        await message.answer(f"Осталось попыток сегодня: {left}", reply_markup=menu_kb())
    else:
        await message.answer("На сегодня всё, приходи завтра!")


@dp.message()
async def fallback(message: Message):
    if get_pending(message.from_user.id):
        await message.answer("Пришли решение фотографией (или /skip).")
    else:
        await message.answer("Выбери задачу:", reply_markup=menu_kb())


async def main():
    bot = Bot(os.environ["BOT_TOKEN"])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
