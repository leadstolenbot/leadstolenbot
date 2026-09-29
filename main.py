import os, hmac, hashlib, json, time, asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl
import asyncpg
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

TOKEN = os.environ["BOT_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
CHANNEL = os.environ.get("CHANNEL", "@leadstolen")

# Вопросы и ответы живут ТОЛЬКО на сервере: "a" — индекс верного варианта
QUIZ = [
    {"q": "Пример вопроса 1 — замените на свой", "o": ["A", "B", "C"], "a": 0},
    {"q": "Пример вопроса 2 — замените на свой", "o": ["A", "B", "C"], "a": 1},
    {"q": "Пример вопроса 3 — замените на свой", "o": ["A", "B", "C"], "a": 2},
]
SCHEMA = """
create table if not exists users(
  id bigint primary key, username text,
  points int not null default 0, streak int not null default 0, last_day date);
create table if not exists events(
  id serial primary key, user_id bigint not null, kind text not null,
  pts int not null, day date not null default current_date,
  at timestamptz not null default now());
create index if not exists ev_user on events(user_id, kind, day);
"""
pool: asyncpg.Pool = None
SESS = {}  # короткие сессии квиза/игры (в памяти; при рестарте сбрасываются)
bot = Bot(TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    await pool.execute(
        "insert into users(id,username) values($1,$2) on conflict (id) do update set username=$2",
        m.from_user.id, m.from_user.username or m.from_user.first_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть рейтинг", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Добро пожаловать в рейтинг фандома!", reply_markup=kb)


@asynccontextmanager
async def lifespan(app):
    global pool
    pool = await asyncpg.create_pool(DB_URL)
    await pool.execute(SCHEMA)
    task = asyncio.create_task(dp.start_polling(bot))  # держите ровно 1 реплику
    yield
    task.cancel()
    await pool.close()


app = FastAPI(lifespan=lifespan)


def check_init_data(raw: str) -> dict:
    d = dict(parse_qsl(raw, keep_blank_values=True))
    got = d.pop("hash", "")
    data = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got) or time.time() - int(d.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "bad initData")
    return json.loads(d["user"])


async def current_user(x_init_data: str = Header("")) -> int:
    u = check_init_data(x_init_data)
    await pool.execute(
        "insert into users(id,username) values($1,$2) on conflict (id) do update set username=$2",
        u["id"], u.get("username") or u.get("first_name"))
    return u["id"]


async def award(uid, kind, pts):
    await pool.execute("insert into events(user_id,kind,pts) values($1,$2,$3)", uid, kind, pts)
    await pool.execute("update users set points=greatest(0,points+$2) where id=$1", uid, pts)


async def once(uid, kind, ever=False):
    q = "select 1 from events where user_id=$1 and kind=$2" + ("" if ever else " and day=current_date")
    return await pool.fetchval(q, uid, kind)


@app.get("/api/me")
async def me(uid: int = Depends(current_user)):
    if not await once(uid, "login"):
        await pool.execute(
            "update users set streak=case when last_day=current_date-1 then streak+1 else 1 end,"
            " last_day=current_date where id=$1", uid)
        await award(uid, "login", 1)
    r = await pool.fetchrow(
        "select username,points,streak,(select count(*)+1 from users u where u.points>users.points) as rank"
        " from users where id=$1", uid)
    week = await pool.fetchval(
        "select coalesce(sum(pts),0) from events where user_id=$1 and at>now()-interval '7 days'", uid)
    done = [x["kind"] for x in await pool.fetch(
        "select distinct kind from events where user_id=$1 and (day=current_date or kind='sub')", uid)]
    return {**dict(r), "week": int(week), "done": done}


@app.get("/api/top")
async def top(period: str = "week", uid: int = Depends(current_user)):
    if period == "all":
        q = "select username, points as pts from users order by pts desc limit 50"
    else:
        q = ("select u.username, sum(e.pts)::int as pts from events e join users u on u.id=e.user_id"
             " where e.at>now()-interval '7 days' group by u.id order by pts desc limit 50")
    return [dict(x) for x in await pool.fetch(q)]


@app.post("/api/quiz/start")
async def quiz_start(uid: int = Depends(current_user)):
    if await once(uid, "quiz"):
        raise HTTPException(409, "Квиз уже пройден сегодня")
    SESS[("quiz", uid)] = time.time()
    return [{"q": x["q"], "o": x["o"]} for x in QUIZ]  # без ответов


@app.post("/api/quiz/finish")
async def quiz_finish(body: dict, uid: int = Depends(current_user)):
    t = SESS.pop(("quiz", uid), None)
    if not t or time.time() - t < 3 or await once(uid, "quiz"):
        raise HTTPException(400, "bad session")
    score = sum(1 for a, x in zip(body.get("answers", []), QUIZ) if a == x["a"])
    ok = score >= 2
    await award(uid, "quiz", 20 if ok else 0)  # событие фиксирует попытку даже при 0 очков
    return {"score": score, "passed": ok}


@app.post("/api/sub")
async def sub(uid: int = Depends(current_user)):
    if await once(uid, "sub", ever=True):
        return {"ok": True}
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
    except Exception:
        raise HTTPException(400, "Бот должен быть админом канала")
    if m.status not in ("member", "administrator", "creator"):
        raise HTTPException(400, "Вы ещё не подписаны")
    await award(uid, "sub", 20)
    return {"ok": True}


@app.post("/api/share")
async def share(uid: int = Depends(current_user)):
    if await once(uid, "share"):
        raise HTTPException(409, "Уже получено сегодня")
    await award(uid, "share", 10)
    return {"ok": True}


@app.post("/api/pong/start")
async def pong_start(uid: int = Depends(current_user)):
    n = await pool.fetchval(
        "select count(*) from events where user_id=$1 and kind='pong' and day=current_date", uid)
    if n >= 3:
        raise HTTPException(429, "Лимит: 3 игры в день")
    await pool.execute("insert into events(user_id,kind,pts) values($1,'pong',0)", uid)
    sid = os.urandom(8).hex()
    SESS[("pong", uid)] = (sid, time.time())
    return {"sid": sid}


@app.post("/api/pong/finish")
async def pong_finish(body: dict, uid: int = Depends(current_user)):
    s = SESS.pop(("pong", uid), None)
    if not s or s[0] != body.get("sid"):
        raise HTTPException(400, "bad session")
    won = bool(body.get("won")) and time.time() - s[1] >= 25  # 5 раундов быстрее 25с — подозрительно
    await award(uid, "pong_win" if won else "pong_loss", 20 if won else -5)
    return {"won": won}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
