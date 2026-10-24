import base64
import logging
import os
import random
import sqlite3
from datetime import date
from urllib.parse import urlparse

import httpx
import yaml
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telethon import Button, TelegramClient, connection, events

load_dotenv()
logging.basicConfig(level=logging.INFO)


# ---------- Configs ----------
def load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


PUB = load_yaml("config.public.yaml")
PRIV = load_yaml("config.private.yaml")

ALLOWED = set(PRIV["allowed_users"])
DAILY_LIMIT = int(PUB["daily_limit"])
MSG = PUB["messages"]
PROMPTS = {str(k): v for k, v in PRIV["prompts"].items()}
TASKS = {str(k): v for k, v in PUB["tasks"].items()}

missing = [k for k in TASKS if k not in PROMPTS]
if missing:
    raise SystemExit(f"No prompt in config.private.yaml for tasks: {missing}")


# ---------- Telegram transport (MTProto proxy / SOCKS5 / HTTP / none) ----------
def to_hex(secret: str) -> str:
    """MTProxy secret may be hex or base64url (as in some tg://proxy links)."""
    try:
        bytes.fromhex(secret)
        return secret
    except ValueError:
        padded = secret + "=" * (-len(secret) % 4)
        return base64.urlsafe_b64decode(padded).hex()


def build_transport() -> dict:
    mtproxy = os.getenv("MTPROXY", "").strip()
    tg_proxy = os.getenv("TG_PROXY", "").strip()

    if mtproxy:
        try:
            host, port, secret = mtproxy.split(":")
        except ValueError:
            raise SystemExit("MTPROXY must look like host:port:secret")
        secret = to_hex(secret)
        prefix = secret[:2].lower()
        if prefix == "ee":
            raise SystemExit(
                "This MTProxy uses a FakeTLS secret (starts with 'ee'), which Telethon does not support. "
                "Use a proxy with a plain or 'dd' secret, or use TG_PROXY (SOCKS5/HTTP) instead."
            )
        if prefix == "dd" and len(secret) == 34:
            conn = connection.ConnectionTcpMTProxyRandomizedIntermediate
        else:
            conn = connection.ConnectionTcpMTProxyIntermediate
        logging.info("Using MTProto proxy %s:%s", host, port)
        return {"connection": conn, "proxy": (host, int(port), secret)}

    if tg_proxy:
        u = urlparse(tg_proxy)
        scheme = "socks5" if u.scheme.startswith("socks5") else u.scheme  # socks5, socks4, http
        logging.info("Using %s proxy %s:%s", scheme, u.hostname, u.port)
        return {
            "proxy": {
                "proxy_type": scheme,
                "addr": u.hostname,
                "port": u.port,
                "username": u.username,
                "password": u.password,
                "rdns": True,
            }
        }
    return {}


client = TelegramClient(
    "bot_session",
    int(os.environ["API_ID"]),
    os.environ["API_HASH"],
    **build_transport(),
)

# ---------- LLM (Gemini and OpenAI share one OpenAI-compatible client) ----------
provider = PRIV["llm"]["provider"]
pcfg = PRIV["llm"][provider]
LLM_PROXY = os.getenv("LLM_PROXY", "").strip() or None
llm = AsyncOpenAI(
    api_key=os.environ[pcfg["api_key_env"]],
    base_url=pcfg.get("base_url"),
    http_client=httpx.AsyncClient(proxy=LLM_PROXY, timeout=120) if LLM_PROXY else None,
)
MODEL = pcfg["model"]

# ---------- DB: daily limits and current task ----------
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


# ---------- Helpers ----------
def menu_kb():
    return [[Button.inline(MSG["button"].format(key=k), data=f"task:{k}".encode()) for k in TASKS]]


async def send_long(event, text: str):
    for i in range(0, len(text), 4000):
        await event.respond(text[i : i + 4000])


# ---------- Handlers ----------
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_message(event):
    uid = event.sender_id
    if uid not in ALLOWED:
        await event.respond(MSG["no_access"].format(user_id=uid))
        return

    text = (event.raw_text or "").strip()
    is_image = bool(event.photo) or bool(
        event.document and (event.document.mime_type or "").startswith("image/")
    )

    if is_image:
        await check_solution(event)
    elif text.startswith("/start"):
        left = DAILY_LIMIT - used_today(uid)
        await event.respond(MSG["menu"].format(left=max(left, 0)), buttons=menu_kb())
    elif text.startswith("/skip"):
        clear_pending(uid)
        await event.respond(MSG["skipped"], buttons=menu_kb())
    elif get_pending(uid):
        await event.respond(MSG["send_photo"])
    else:
        await event.respond(MSG["choose_first"], buttons=menu_kb())


@client.on(events.CallbackQuery(pattern=rb"task:.*"))
async def on_task_choice(event):
    uid = event.sender_id
    if uid not in ALLOWED:
        await event.answer("No access", alert=True)
        return
    await event.answer()

    key = event.data.decode().split(":", 1)[1]
    if key not in TASKS:
        return
    if get_pending(uid):
        await event.respond(MSG["finish_current_first"])
        return
    if used_today(uid) >= DAILY_LIMIT:
        await event.respond(MSG["limit_reached"].format(limit=DAILY_LIMIT))
        return

    problem = random.choice(TASKS[key]["problems"])
    add_usage(uid)
    set_pending(uid, key, problem)
    left = DAILY_LIMIT - used_today(uid)
    await event.respond(MSG["task"].format(key=key, problem=problem, left=left))


async def check_solution(event):
    uid = event.sender_id
    pending = get_pending(uid)
    if not pending:
        await event.respond(MSG["choose_first"], buttons=menu_kb())
        return
    key, problem = pending

    wait = await event.respond(MSG["checking"])
    mime = "image/jpeg" if event.photo else event.document.mime_type
    data = await event.download_media(file=bytes)
    img_b64 = base64.b64encode(data).decode()

    try:
        resp = await llm.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": PROMPTS[key]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Problem statement:\n{problem}\n\nBelow is a photo of the student's solution.",
                        },
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    ],
                },
            ],
        )
        answer = resp.choices[0].message.content
    except Exception:
        logging.exception("LLM error")
        await wait.edit(MSG["llm_error"])
        return

    clear_pending(uid)
    await wait.delete()
    await send_long(event, answer)
    left = DAILY_LIMIT - used_today(uid)
    if left > 0:
        await event.respond(MSG["attempts_left"].format(left=left), buttons=menu_kb())
    else:
        await event.respond(MSG["done_today"])


async def main():
    await client.start(bot_token=os.environ["BOT_TOKEN"])
    me = await client.get_me()
    logging.info("Bot started as @%s", me.username)
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
