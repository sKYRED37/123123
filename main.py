import asyncio
import os
import re
import traceback
import random
import json
import aiosqlite
import aiohttp
import logging
import math
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest


def md_escape(text: str) -> str:
    text = str(text)
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    return text


_HTML_TAGS = ["b", "i", "u", "s", "code", "pre"]

def _close_open_tags(chunk: str) -> str:
    """Append closing tags for any HTML tags opened but not closed in chunk."""
    open_stack = []
    i = 0
    while i < len(chunk):
        if chunk[i] == '<':
            end = chunk.find('>', i)
            if end == -1:
                break
            tag_content = chunk[i+1:end].strip()
            if tag_content.startswith('/'):
                tag_name = tag_content[1:].split()[0].lower()
                if open_stack and open_stack[-1] == tag_name:
                    open_stack.pop()
            else:
                tag_name = tag_content.split()[0].lower()
                if tag_name in _HTML_TAGS:
                    open_stack.append(tag_name)
            i = end + 1
        else:
            i += 1
    # Close all still-open tags in reverse order
    suffix = "".join(f"</{t}>" for t in reversed(open_stack))
    return chunk + suffix

def _split_by_bytes(text: str, max_bytes: int = 3800) -> list[str]:
    """
    Split text into chunks whose UTF-8 byte length does not exceed max_bytes.
    Splits on newlines where possible to avoid cutting mid-tag.
    Telegram counts bytes (UTF-8), not Python str characters.
    We use 3800 (not 4096) as a safe margin for closing tags appended later.
    """
    chunks: list[str] = []
    current_lines: list[str] = []
    current_bytes = 0

    for line in text.split("\n"):
        line_bytes = len((line + "\n").encode("utf-8"))
        # If a single line itself exceeds the limit, hard-split it by bytes
        if line_bytes > max_bytes:
            # flush whatever we have first
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_bytes = 0
            # hard-split the oversized line
            encoded = line.encode("utf-8")
            pos = 0
            while pos < len(encoded):
                slice_bytes = encoded[pos:pos + max_bytes]
                # decode safely, ignoring incomplete trailing multibyte chars
                chunk_str = slice_bytes.decode("utf-8", errors="ignore")
                # adjust if decode shrunk the slice
                while len(chunk_str.encode("utf-8")) > max_bytes:
                    chunk_str = chunk_str[:-1]
                chunks.append(chunk_str)
                pos += len(chunk_str.encode("utf-8"))
            continue

        if current_bytes + line_bytes > max_bytes:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_bytes = line_bytes
        else:
            current_lines.append(line)
            current_bytes += line_bytes

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks

async def safe_send_long(msg, full_text: str) -> None:
    """Split full_text into byte-safe chunks and send each with properly closed HTML tags."""
    for chunk in _split_by_bytes(full_text):
        await msg.answer(_close_open_tags(chunk))

BOT_TOKEN = os.getenv("", "")
OWNER_ID  = int(os.getenv("1766395031", "1766395031"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Папка с составами для регистрации команд лиги файлами (см. блок "HOTS ROSTERS"
# ниже). Каждый .txt файл в этой папке — один состав/одна команда.
HOTS_ROSTERS_DIR = os.getenv("HOTS_ROSTERS_DIR", "hots_rosters")

MAP_POOL = ["Sandstone", "Dune", "Breeze", "Rust", "Hanami", "Prison", "Province"]
ROLES    = ["Rifler", "AWPer", "Support", "Entry Fragger", "Lurker", "IGL", "Coach"]

# Маркеры «вторых/третьих» составов организаций (академии, фарм-команды и т.п.).
# Если в названии команды (без учёта регистра) встречается любой из этих маркеров —
# например «Virtus.pro Academy Brazil» — это суб-состав материнской организации
# (Virtus.pro), а не самостоятельная топ-команда, и такая команда исключается
# из топа по балансу.
ACADEMY_TEAM_MARKERS = ["academy", "acd", "brazil", "junior", "youth", "female", "u21", "u19"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. На bothost.ru добавьте переменную окружения "
        "BOT_TOKEN в настройках бота (раздел Environment Variables)."
    )
if OWNER_ID == 1766395031 :
    logger.warning("OWNER_ID не задан — команды владельца будут недоступны.")

DB_PATH = "esports_league.db"


async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS teams (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            spirit      REAL DEFAULT 100.0,
            chemistry   REAL DEFAULT 50.0
        );
        CREATE TABLE IF NOT EXISTS spirit_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_name   TEXT NOT NULL,
            change      REAL NOT NULL,
            reason      TEXT NOT NULL,
            changed_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS players (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nick        TEXT NOT NULL,
            team_name   TEXT NOT NULL,
            role        TEXT NOT NULL,
            rating      REAL DEFAULT 0.0,
            is_reserve  INTEGER DEFAULT 0,
            age         INTEGER DEFAULT 18
        );
        CREATE TABLE IF NOT EXISTS match_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_a      TEXT,
            team_b      TEXT,
            result      TEXT,
            score       TEXT,
            played_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS tournaments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS tournament_matches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tour_name   TEXT NOT NULL,
            team_a      TEXT NOT NULL,
            team_b      TEXT NOT NULL,
            winner      TEXT NOT NULL,
            place_a     INTEGER DEFAULT 0,
            place_b     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tournament_player_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tour_name   TEXT NOT NULL,
            nick        TEXT NOT NULL,
            team_name   TEXT NOT NULL,
            kills       INTEGER DEFAULT 0,
            deaths      INTEGER DEFAULT 0,
            assists     INTEGER DEFAULT 0,
            adr_dmg     REAL DEFAULT 0.0,
            kast_rounds INTEGER DEFAULT 0,
            total_rounds INTEGER DEFAULT 0,
            entry_k     INTEGER DEFAULT 0,
            entry_d     INTEGER DEFAULT 0,
            mk3         INTEGER DEFAULT 0,
            mk4         INTEGER DEFAULT 0,
            mk5         INTEGER DEFAULT 0,
            clutches_won INTEGER DEFAULT 0,
            util_dmg    REAL DEFAULT 0.0,
            flash_assists INTEGER DEFAULT 0,
            UNIQUE(tour_name, nick)
        );
        CREATE TABLE IF NOT EXISTS admins (
            telegram_id  INTEGER PRIMARY KEY,
            username     TEXT,
            added_at     TEXT
        );
        CREATE TABLE IF NOT EXISTS tournament_registry (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            tier        TEXT DEFAULT 'B',
            is_done     INTEGER DEFAULT 0,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS loans (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            nick            TEXT NOT NULL,
            from_team       TEXT NOT NULL,
            to_team         TEXT NOT NULL,
            until_tour      TEXT NOT NULL,
            returned        INTEGER DEFAULT 0,
            loaned_at       TEXT
        );
        CREATE TABLE IF NOT EXISTS vrs (
            team_name   TEXT PRIMARY KEY,
            points      INTEGER DEFAULT 0,
            wins        INTEGER DEFAULT 0,
            draws       INTEGER DEFAULT 0,
            losses      INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS merch (
            team_name   TEXT PRIMARY KEY,
            revenue     INTEGER DEFAULT 0,
            units_sold  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS moderators (
            telegram_id  INTEGER PRIMARY KEY,
            username     TEXT,
            added_at     TEXT
        );
        CREATE TABLE IF NOT EXISTS team_tournament_places (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_name   TEXT NOT NULL,
            tour_name   TEXT NOT NULL,
            place       INTEGER NOT NULL CHECK(place BETWEEN 1 AND 3),
            added_at    TEXT,
            UNIQUE(team_name, tour_name)
        );
        CREATE TABLE IF NOT EXISTS team_leaders (
            team_name   TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            username    TEXT,
            added_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS transfer_requests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id     INTEGER NOT NULL,
            nick          TEXT NOT NULL,
            from_team     TEXT NOT NULL,
            to_team       TEXT NOT NULL,
            initiated_by  INTEGER NOT NULL,
            mode          TEXT NOT NULL,
            status        TEXT DEFAULT 'pending',
            notify_chat_ids TEXT DEFAULT '',
            created_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS hltv_players (
            nickname        TEXT PRIMARY KEY,
            team            TEXT DEFAULT '',
            kills           INTEGER DEFAULT 0,
            deaths          INTEGER DEFAULT 0,
            assists         INTEGER DEFAULT 0,
            rating          REAL DEFAULT 0.0,
            kd              REAL DEFAULT 0.0,
            dpr             REAL DEFAULT 0.0,
            kpr             REAL DEFAULT 0.0,
            impact          REAL DEFAULT 0.0,
            maps_played     INTEGER DEFAULT 0,
            maps_won        INTEGER DEFAULT 0,
            maps_lost       INTEGER DEFAULT 0,
            mvp_count       INTEGER DEFAULT 0,
            mvp_list        TEXT DEFAULT '',
            evp_count       INTEGER DEFAULT 0,
            evp_list        TEXT DEFAULT '',
            tournaments_won INTEGER DEFAULT 0,
            tournaments_list TEXT DEFAULT ''
        );
        """)
        for migration in [
            "ALTER TABLE players ADD COLUMN is_reserve INTEGER DEFAULT 0",
            "ALTER TABLE players ADD COLUMN age INTEGER DEFAULT 18",
            "ALTER TABLE teams ADD COLUMN spirit REAL DEFAULT 100.0",
            """CREATE TABLE IF NOT EXISTS spirit_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name   TEXT NOT NULL,
                change      REAL NOT NULL,
                reason      TEXT NOT NULL,
                changed_at  TEXT
            )""",
            "ALTER TABLE tournament_registry ADD COLUMN tier TEXT DEFAULT 'B'",
            "ALTER TABLE tournament_player_stats ADD COLUMN assists INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN adr_dmg REAL DEFAULT 0.0",
            "ALTER TABLE tournament_player_stats ADD COLUMN kast_rounds INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN total_rounds INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN entry_k INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN entry_d INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN mk3 INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN mk4 INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN mk5 INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN clutches_won INTEGER DEFAULT 0",
            "ALTER TABLE tournament_player_stats ADD COLUMN util_dmg REAL DEFAULT 0.0",
            "ALTER TABLE tournament_player_stats ADD COLUMN flash_assists INTEGER DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS team_leaders (
                team_name   TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                username    TEXT,
                added_at    TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS transfer_requests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id     INTEGER NOT NULL,
                nick          TEXT NOT NULL,
                from_team     TEXT NOT NULL,
                to_team       TEXT NOT NULL,
                initiated_by  INTEGER NOT NULL,
                mode          TEXT NOT NULL,
                status        TEXT DEFAULT 'pending',
                notify_chat_ids TEXT DEFAULT '',
                created_at    TEXT
            )""",
            "ALTER TABLE transfer_requests ADD COLUMN request_type TEXT DEFAULT 'transfer'",
            "ALTER TABLE transfer_requests ADD COLUMN until_tour TEXT DEFAULT ''",
            "ALTER TABLE transfer_requests ADD COLUMN partner_player_id INTEGER DEFAULT 0",
            "ALTER TABLE transfer_requests ADD COLUMN partner_nick TEXT DEFAULT ''",
            """CREATE TABLE IF NOT EXISTS faceit_stats (
                nick        TEXT PRIMARY KEY,
                team_name   TEXT NOT NULL,
                elo         INTEGER DEFAULT 1000,
                matches     INTEGER DEFAULT 0,
                wins        INTEGER DEFAULT 0,
                losses      INTEGER DEFAULT 0,
                kills       INTEGER DEFAULT 0,
                deaths      INTEGER DEFAULT 0,
                headshots   INTEGER DEFAULT 0,
                updated_at  TEXT
            )""",
            "ALTER TABLE teams ADD COLUMN balance REAL DEFAULT 10000.0",
            """CREATE TABLE IF NOT EXISTS balance_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_team   TEXT,
                to_team     TEXT,
                amount      REAL NOT NULL,
                reason      TEXT NOT NULL,
                initiated_by INTEGER,
                created_at  TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS league_bank (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                balance     REAL NOT NULL DEFAULT 0.0
            )""",
            "INSERT OR IGNORE INTO league_bank (id, balance) VALUES (1, 0.0)",
            """CREATE TABLE IF NOT EXISTS free_agent_signings (
                team_name   TEXT PRIMARY KEY,
                count       INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS map_stats (
                team_name   TEXT NOT NULL,
                map_name    TEXT NOT NULL,
                winrate     REAL DEFAULT 80.0,
                wins        INTEGER DEFAULT 0,
                losses      INTEGER DEFAULT 0,
                UNIQUE(team_name, map_name)
            )""",
            "ALTER TABLE map_stats ADD COLUMN winrate REAL DEFAULT 80.0",
            "ALTER TABLE teams ADD COLUMN winrate_boost REAL DEFAULT 0.0",
            """CREATE TABLE IF NOT EXISTS shop_purchases (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name     TEXT NOT NULL,
                item_key      TEXT NOT NULL,
                price         REAL NOT NULL,
                effect_value  REAL NOT NULL,
                bought_by     INTEGER,
                bought_at     TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS hots_imports (
                team_name     TEXT PRIMARY KEY,
                source_file   TEXT,
                imported_at   TEXT
            )""",
        ]:
            try:
                await db.execute(migration)
                await db.commit()
            except Exception:
                pass





# ── SPIRIT helpers ────────────────────────────────────────────────────────────

def spirit_status(spirit: float) -> tuple[str, str]:
    """Returns (label, emoji) for a spirit value."""
    if spirit <= 30:
        return "Тлеющие угли", "🪨"
    elif spirit <= 75:
        return "Стабильное пламя", "🔥"
    else:
        return "Синее пламя", "💠"

def spirit_bar(spirit: float) -> str:
    filled = max(0, min(10, int(spirit / 10)))
    empty  = 10 - filled
    return "█" * filled + "░" * empty

async def db_change_spirit(team_name: str, delta: float, reason: str) -> float:
    """Changes spirit by delta (clamped 0–100), logs to spirit_history. Returns new value."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT spirit FROM teams WHERE name=?", (team_name,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return 0.0
        new_spirit = max(0.0, min(100.0, row[0] + delta))
        actual_delta = new_spirit - row[0]
        await db.execute("UPDATE teams SET spirit=? WHERE name=?", (new_spirit, team_name))
        await db.execute(
            "INSERT INTO spirit_history (team_name, change, reason, changed_at) VALUES (?,?,?,?)",
            (team_name, actual_delta, reason, datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()
    return new_spirit

async def db_set_spirit(team_name: str, value: float, reason: str) -> float:
    """Sets spirit to exact value (clamped 0–100), logs it."""
    value = max(0.0, min(100.0, value))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT spirit FROM teams WHERE name=?", (team_name,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return 0.0
        await db.execute("UPDATE teams SET spirit=? WHERE name=?", (value, team_name))
        await db.execute(
            "INSERT INTO spirit_history (team_name, change, reason, changed_at) VALUES (?,?,?,?)",
            (team_name, value - row[0], reason, datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()
    return value

async def db_get_spirit_history(team_name: str, limit: int = 5) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT change, reason, changed_at FROM spirit_history WHERE team_name=? ORDER BY id DESC LIMIT ?",
            (team_name, limit)
        ) as cursor:
            return await cursor.fetchall()

# ─────────────────────────────────────────────────────────────────────────────



async def db_get_team(name: str) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, spirit, chemistry, winrate_boost FROM teams WHERE name=?", (name,)
        ) as cursor:
            row = await cursor.fetchone()
    return row


async def db_get_players(team_name: str) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT nick, role, rating, is_reserve, age FROM players "
            "WHERE team_name=? ORDER BY is_reserve ASC, id ASC",
            (team_name,)
        ) as cursor:
            rows = await cursor.fetchall()
    return rows


async def db_create_team(name: str, players: list[dict], reserve: list[dict], initial_vrs: int = 0, coach: dict | None = None) -> None:
    """players and reserve — list of dicts with keys: nick, rating, role, age. coach — optional dict same keys."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO teams (name, spirit, chemistry) VALUES (?,?,50)",
                (name, 100.0)
            )
            for p in players:
                await db.execute(
                    "INSERT INTO players (nick, team_name, role, rating, is_reserve, age) VALUES (?,?,?,?,0,?)",
                    (p["nick"], name, p["role"], p["rating"], p["age"])
                )
                await db.execute(
                    "INSERT OR IGNORE INTO hltv_players (nickname, team) VALUES (?,?)",
                    (p["nick"], name)
                )
            for p in reserve:
                await db.execute(
                    "INSERT INTO players (nick, team_name, role, rating, is_reserve, age) VALUES (?,?,?,?,1,?)",
                    (p["nick"], name, p["role"], p["rating"], p["age"])
                )
                await db.execute(
                    "INSERT OR IGNORE INTO hltv_players (nickname, team) VALUES (?,?)",
                    (p["nick"], name)
                )
            if coach:
                await db.execute(
                    "INSERT INTO players (nick, team_name, role, rating, is_reserve, age) VALUES (?,?,?,?,2,?)",
                    (coach["nick"], name, "Coach", coach["rating"], coach["age"])
                )
            await db.execute(
                "INSERT OR IGNORE INTO vrs (team_name, points, wins, draws, losses) VALUES (?,?,0,0,0)",
                (name, initial_vrs)
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# ─────────────────────────────────────────────────────────────────────────────
# HOTS ROSTERS — составы из папки HOTS_ROSTERS_DIR, регистрируются как ПОЛНОЦЕННЫЕ
# команды лиги (в тех же таблицах teams/players/vrs, что и /create_team).
#
# Формат файла (кодировка UTF-8, расширение .txt), одна команда — один файл:
#
#     Team Liquid
#     Nazeric 20.5 AWPer 24
#     Glaurung 19.0 Rifler 22
#     Snitch 18.5 Support 23
#     Fan 17.5 Entry Fragger 21
#     Bakery 18.0 IGL 25
#
# Первая непустая строка — название команды. Каждая следующая строка — игрок:
# "Ник Рейтинг Роль Возраст". Роль должна быть одной из ролей бота — тех же,
# что и в /create_team: Rifler, AWPer, Support, Entry Fragger, Lurker, IGL,
# Coach (регистр не важен). Основной состав — ровно 5 игроков.
# Строки, начинающиеся с "#", считаются комментариями и игнорируются.
# Необязательно можно добавить секции "Резерв" (до 2 игроков) и "Тренер"
# (1 игрок, роль обязательно Coach) — так же, как в /create_team.
#
# Сыгранность и дух выставляются командой автоматически по максимуму
# (100 и 50 соответственно), а ВРС всегда 0 — как и требуется для новых
# команд лиги. Указывать их в файле не нужно.
#
# При каждом запуске бота, а также по команде /hots_reload (для модераторов
# и админов), папка HOTS_ROSTERS_DIR пересканируется. Уже существующая в лиге
# команда данными из файла НЕ перезаписывается (чтобы не затереть баланс,
# трансферы, изменившийся дух/сыгранность/ВРС) — регистрируются только те
# файлы, для которых команды с таким названием в лиге ещё нет.
# ─────────────────────────────────────────────────────────────────────────────

def hots_parse_player_line(line: str) -> tuple[dict | None, str | None]:
    """Разбирает строку игрока «Ник Рейтинг Роль Возраст» (роль — всё между
    рейтингом и возрастом). Роль должна быть одной из ROLES бота (Rifler,
    AWPer, Support, Entry Fragger, Lurker, IGL, Coach — как в /create_team).
    Возвращает (результат, текст_ошибки); результат = None при ошибке."""
    tokens = line.split()
    if len(tokens) < 4:
        return None, f"«{line}» — нужно минимум 4 токена (Ник Рейтинг Роль Возраст)"
    nick = tokens[0]
    try:
        rating = float(tokens[1].replace(",", "."))
    except ValueError:
        return None, f"«{line}»: рейтинг «{tokens[1]}» должен быть числом"
    try:
        age = int(tokens[-1])
    except ValueError:
        return None, f"«{line}»: возраст «{tokens[-1]}» должен быть целым числом"
    role_raw = " ".join(tokens[2:-1]).strip()
    role = VALID_ROLES_LOWER.get(role_raw.lower())
    if role is None:
        return None, f"«{line}»: роль «{role_raw}» не распознана. Доступные роли: {', '.join(ROLES)}"
    return {"nick": nick, "rating": rating, "role": role, "age": age}, None


def hots_parse_roster_file(path: str) -> tuple[dict | None, list[str]]:
    """Разбирает один файл состава. Возвращает (результат, ошибки).
    результат = {"name", "main_players", "reserve_players", "coach_player"} или
    None, если есть ошибки."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError as e:
        return None, [f"не удалось прочитать файл: {e}"]

    non_empty = [ln.strip() for ln in raw_lines if ln.strip() and not ln.strip().startswith("#")]
    if not non_empty:
        return None, ["файл пуст"]

    name = non_empty[0]
    errors: list[str] = []
    main_players: list[dict] = []
    reserve_players: list[dict] = []
    coach_player: dict | None = None
    in_reserve = False
    in_coach = False

    for line in non_empty[1:]:
        low = line.lower()
        if low == "резерв":
            in_reserve, in_coach = True, False
            continue
        if low == "тренер":
            in_coach, in_reserve = True, False
            continue
        player, err = hots_parse_player_line(line)
        if player is None:
            errors.append(f"«{name}»: {err}")
            continue
        if in_coach:
            if player["role"] != "Coach":
                errors.append(f"«{name}»: тренер должен иметь роль Coach (строка «{line}»)")
            elif coach_player is not None:
                errors.append(f"«{name}»: можно указать только одного тренера")
            else:
                coach_player = player
        elif in_reserve:
            reserve_players.append(player)
        else:
            main_players.append(player)

    if len(main_players) != 5:
        errors.append(f"«{name}»: основной состав должен содержать ровно 5 игроков (сейчас: {len(main_players)})")
    if len(reserve_players) > 2:
        errors.append(f"«{name}»: в резерве не более 2 игроков")

    if errors:
        return None, errors

    return {
        "name": name,
        "main_players": main_players,
        "reserve_players": reserve_players,
        "coach_player": coach_player,
    }, []


async def db_hots_mark_imported(team_name: str, source_file: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO hots_imports (team_name, source_file, imported_at) VALUES (?,?,?)",
            (team_name, source_file, datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()


async def db_hots_is_imported(team_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM hots_imports WHERE team_name=? COLLATE NOCASE", (team_name,)
        ) as cursor:
            return (await cursor.fetchone()) is not None


async def db_hots_list_imported() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name, source_file, imported_at FROM hots_imports ORDER BY team_name COLLATE NOCASE"
        ) as cursor:
            return await cursor.fetchall()


async def hots_scan_and_load_rosters() -> tuple[list[str], list[str]]:
    """Сканирует HOTS_ROSTERS_DIR и регистрирует новые команды лиги из файлов.
    Возвращает (список созданных команд, список ошибок/пропусков по файлам)."""
    os.makedirs(HOTS_ROSTERS_DIR, exist_ok=True)
    created: list[str] = []
    errors: list[str] = []
    for fname in sorted(os.listdir(HOTS_ROSTERS_DIR)):
        if not fname.lower().endswith(".txt"):
            continue
        fpath = os.path.join(HOTS_ROSTERS_DIR, fname)
        result, parse_errors = hots_parse_roster_file(fpath)
        if parse_errors:
            errors.extend(f"{fname}: {e}" for e in parse_errors)
            continue

        team_name = result["name"]
        if await db_get_team(team_name):
            if not await db_hots_is_imported(team_name):
                errors.append(
                    f"{fname}: команда «{team_name}» уже есть в лиге (создана не из этой папки) — пропущено"
                )
            continue

        try:
            await db_create_team(
                team_name, result["main_players"], result["reserve_players"],
                0, coach=result["coach_player"]
            )
            await db_hots_mark_imported(team_name, fname)
            created.append(team_name)
        except Exception as e:
            errors.append(f"{fname}: ошибка при создании команды — {e}")
    return created, errors


async def db_transfer_player(nick: str, from_team: str, to_team: str, loan: bool = False) -> bool:
    """Переводит игрока из from_team в to_team."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM players WHERE nick=? COLLATE NOCASE AND team_name=? COLLATE NOCASE",
            (nick, from_team)
        ) as cursor:
            prow = await cursor.fetchone()
        if not prow:
            return False
        player_id = prow[0]

        cursor = await db.execute(
            "UPDATE players SET team_name=? WHERE nick=? COLLATE NOCASE AND team_name=? COLLATE NOCASE",
            (to_team, nick, from_team)
        )
        if cursor.rowcount == 0:
            return False
        chem_loss = 2 if loan else 5
        for team in (from_team, to_team):
            await db.execute(
                "UPDATE teams SET chemistry = MAX(0, chemistry - ?) WHERE name=?",
                (chem_loss, team)
            )

        await db.commit()
    # Spirit penalty for roster change
    if not loan:
        reason = f"Трансфер игрока {nick}"
        await db_change_spirit(from_team, -25.0, reason)
        await db_change_spirit(to_team, -25.0, reason)
    return True


async def db_swap_players(nick_a: str, team_a: str, nick_b: str, team_b: str) -> tuple[bool, str]:
    """Обмен двумя игроками между командами."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, is_reserve FROM players WHERE nick=? COLLATE NOCASE AND team_name=? COLLATE NOCASE",
            (nick_a, team_a)
        ) as cursor:
            row_a = await cursor.fetchone()
        if not row_a:
            return False, f"Игрок «{nick_a}» не найден в команде «{team_a}»."
        player_id_a = row_a[0]

        async with db.execute(
            "SELECT id, is_reserve FROM players WHERE nick=? COLLATE NOCASE AND team_name=? COLLATE NOCASE",
            (nick_b, team_b)
        ) as cursor:
            row_b = await cursor.fetchone()
        if not row_b:
            return False, f"Игрок «{nick_b}» не найден в команде «{team_b}»."
        player_id_b = row_b[0]

        await db.execute(
            "UPDATE players SET team_name=? WHERE nick=? COLLATE NOCASE AND team_name=? COLLATE NOCASE",
            (team_b, nick_a, team_a)
        )
        await db.execute(
            "UPDATE players SET team_name=? WHERE nick=? COLLATE NOCASE AND team_name=? COLLATE NOCASE",
            (team_a, nick_b, team_b)
        )
        for team in (team_a, team_b):
            await db.execute(
                "UPDATE teams SET chemistry = MAX(0, chemistry - 3) WHERE name=?",
                (team,)
            )
        await db.commit()
    # Spirit penalty for player swap (roster disruption)
    await db_change_spirit(team_a, -25.0, f"Обмен игрока {nick_a} ↔ {nick_b}")
    await db_change_spirit(team_b, -25.0, f"Обмен игрока {nick_b} ↔ {nick_a}")
    return True, ""


async def db_update_after_match(winner: str, loser: str, score: str, is_tournament: bool = False) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO match_history (team_a, team_b, result, score, played_at) VALUES (?,?,?,?,?)",
            (winner, loser, f"{winner} WIN", score, datetime.now().isoformat(timespec='seconds'))
        )
        await db.commit()
    # Дух меняется только в турнирных матчах
    if is_tournament:
        await db_change_spirit(winner, +6.0, f"Победа ({score}) +сыгранность")   # +5 победа + 1 матч
        await db_change_spirit(loser,  -4.0, f"Поражение ({score}) +сыгранность") # -5 поражение + 1 матч


# ── ВИНРЕЙТ ПО КАРТАМ ─────────────────────────────────────────────────────────

async def db_update_map_stats(team_name: str, map_name: str, won: bool) -> float:
    """Обновляет «плавающий» винрейт команды на карте.
    Стартовое значение — 80%, диапазон 70–100%. После каждого сыгранного
    матча значение немного сдвигается (вверх при победе, вниз при поражении)
    на случайную величину, оставаясь в рамках 70–100 — реалистичное
    колебание от матча к матчу, а не фиксированное число."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT winrate FROM map_stats WHERE team_name=? AND map_name=?",
            (team_name, map_name)
        ) as cursor:
            row = await cursor.fetchone()
        current = row[0] if row and row[0] is not None else 80.0

        delta = random.uniform(2.0, 6.0)
        new_winrate = current + delta if won else current - delta
        new_winrate = max(70.0, min(100.0, new_winrate))

        await db.execute(
            "INSERT INTO map_stats (team_name, map_name, winrate, wins, losses) VALUES (?,?,?,?,?) "
            "ON CONFLICT(team_name, map_name) DO UPDATE SET "
            "winrate = excluded.winrate, "
            "wins = wins + excluded.wins, losses = losses + excluded.losses",
            (team_name, map_name, new_winrate, 1 if won else 0, 0 if won else 1)
        )
        await db.commit()
    return new_winrate


async def db_get_team_map_winrates(team_name: str) -> dict[str, float]:
    """Возвращает {map_name: winrate%} по всем картам. Для карт без сыгранных
    матчей винрейт равен стартовому значению — 80%."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT map_name, winrate FROM map_stats WHERE team_name=?",
            (team_name,)
        ) as cursor:
            rows = await cursor.fetchall()
    return {map_name: winrate for map_name, winrate in rows}


def fmt_map_winrate_block(map_winrates: dict[str, float]) -> str:
    """Строит блок 'Winrate:' по картам в фиксированном порядке.
    Значение по умолчанию (карта ещё не сыграна) — 80%, диапазон 70–100%."""
    display_order = ["Province", "Dune", "Sandstone", "Breeze", "Hanami", "Rust", "Prison"]
    lines = ["Winrate:"]
    for map_name in display_order:
        wr = map_winrates.get(map_name, 80.0)
        lines.append(f"{map_name} - {wr:.1f}%")
    return "\n".join(lines)


async def db_get_vrs_rank(team_name: str) -> int:
    """Возвращает текущий ранг команды в VRS (1 = лидер). Если команды нет — возвращает 999."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name FROM vrs ORDER BY points DESC"
        ) as cursor:
            rows = await cursor.fetchall()
    for i, (name,) in enumerate(rows, 1):
        if name == team_name:
            return i
    return 999


async def db_update_vrs(winner: str | None, loser: str | None, is_draw: bool, team_a: str, team_b: str, tour_name: str = "", winner_maps: int = 0, loser_maps: int = 0) -> None:
    """Обновляет VRS рейтинг используя calculate_rating_change (Elo-стиль).

    Тир берётся из tournament_registry по названию турнира (S / A / B).
    Ранги команд определяются их текущей позицией в таблице VRS.
    Ничья: каждой команде начисляется половина суммы win-очков за победу над соперником.
    winner_maps/loser_maps — счёт по картам (если есть), влияет на итоговые
    очки через множитель разгромности (см. _match_margin_factor).
    """
    # ── Определяем тир из tournament_registry ────────────────────────────────
    tier = "B"  # дефолт
    if tour_name:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT tier FROM tournament_registry WHERE name=?", (tour_name,)
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                tier = row[0].upper()
                if tier not in ("S", "A", "B"):
                    tier = "B"

    margin = _match_margin_factor(winner_maps, loser_maps)

    # ── Текущие ранги ─────────────────────────────────────────────────────────
    rank_a = await db_get_vrs_rank(team_a)
    rank_b = await db_get_vrs_rank(team_b)

    async with aiosqlite.connect(DB_PATH) as db:
        for team in (team_a, team_b):
            await db.execute(
                "INSERT OR IGNORE INTO vrs (team_name, points, wins, draws, losses) VALUES (?,0,0,0,0)",
                (team,)
            )

        if is_draw:
            # Ничья: среднее между "как будто победил соперника" для каждой стороны
            pts_a = calculate_rating_change(rank_a, rank_b, tier, "win")
            pts_b = calculate_rating_change(rank_b, rank_a, tier, "win")
            draw_pts_a = round((pts_a + pts_b) / 2, 1)
            draw_pts_b = draw_pts_a
            await db.execute(
                "UPDATE vrs SET points = points + ?, draws = draws + 1 WHERE team_name=?",
                (draw_pts_a, team_a)
            )
            await db.execute(
                "UPDATE vrs SET points = points + ?, draws = draws + 1 WHERE team_name=?",
                (draw_pts_b, team_b)
            )
        elif winner and loser:
            win_rank      = rank_a if winner == team_a else rank_b
            loss_rank     = rank_a if loser  == team_a else rank_b
            opp_win_rank  = rank_b if winner == team_a else rank_a
            opp_loss_rank = rank_b if loser  == team_a else rank_a

            win_pts  = calculate_rating_change(win_rank,  opp_win_rank,  tier, "win",  margin)
            loss_pts = calculate_rating_change(loss_rank, opp_loss_rank, tier, "loss", margin)  # отрицательное

            await db.execute(
                "UPDATE vrs SET points = points + ?, wins = wins + 1 WHERE team_name=?",
                (win_pts, winner)
            )
            await db.execute(
                "UPDATE vrs SET points = MAX(0, points + ?), losses = losses + 1 WHERE team_name=?",
                (loss_pts, loser)  # loss_pts уже отрицательный
            )

        await db.commit()


# ── VRS: расчёт изменения рейтинга по системе HLTV ───────────────────────────

def calculate_rating_change(
    team_rank: int,
    opponent_rank: int,
    tournament_tier: str,
    match_result: str,
    margin_factor: float = 1.0,
) -> float:
    """
    Рассчитывает изменение VRS-очков команды после матча (Elo-стиль).

    Аргументы:
        team_rank       — текущий ранг нашей команды (например, 5)
        opponent_rank   — ранг соперника (например, 20)
        tournament_tier — тир турнира: 'S', 'A' или 'B'
        match_result    — результат: 'win' (победа) или 'loss' (поражение)
        margin_factor   — множитель за разгромность счёта (0.9..1.0),
                           см. _match_margin_factor()

    Возвращает:
        float — изменение очков со знаком (+ победа, − поражение)

    Логика (исправлена — раньше была перепутана местами направленность):
        • Победа над СИЛЬНЫМ соперником (меньший номер ранга) — близко к МАКСИМУМУ очков.
        • Победа над СЛАБЫМ соперником (больший номер ранга) — близко к МИНИМУМУ очков.
        • Поражение от СИЛЬНОГО соперника — теряем МАЛО очков (ожидаемый результат).
        • Поражение от СЛАБОГО соперника — теряем МНОГО очков (неожиданный результат).
        Зависимость от разницы рангов — не линейная, а сигмоидальная (логистическая
        кривая, как ожидаемый результат в Elo/шахматном рейтинге): небольшая разница
        рангов почти не влияет на итог, а после определённого порога влияние быстро
        насыщается и выходит на плато. Максимальная учитываемая разница — 50 позиций.
    """

    # ── Диапазоны очков по тирам ─────────────────────────────────────────────
    TIER_RANGES = {
        # тир: { win: (мин, макс), loss: (мин, макс) }
        "S": {"win": (14.0, 42.0), "loss": (3.0,  10.0)},
        "A": {"win": (9.0,  17.0), "loss": (1.0,   5.0)},
        "B": {"win": (4.0,   8.0), "loss": (1.0,   3.0)},
    }

    tier = tournament_tier.upper()
    if tier not in TIER_RANGES:
        raise ValueError(f"Неверный тир турнира: «{tournament_tier}». Ожидается 'S', 'A' или 'B'.")

    if match_result not in ("win", "loss"):
        raise ValueError(f"Неверный результат матча: «{match_result}». Ожидается 'win' или 'loss'.")

    ranges = TIER_RANGES[tier]

    # ── Ограничиваем разницу рангов до 50 позиций ────────────────────────────
    # raw_diff > 0  →  соперник слабее нас (его номер больше)
    # raw_diff < 0  →  соперник сильнее нас (его номер меньше)
    MAX_RANK_DIFF = 50
    raw_diff = opponent_rank - team_rank
    rank_diff = max(-MAX_RANK_DIFF, min(MAX_RANK_DIFF, raw_diff))

    # ── Логистическая (Elo-подобная) «сила соперника относительно нас» ──────
    # x нормализован в [-1..1]: +1 → соперник намного слабее, −1 → намного сильнее.
    # s → 1, когда соперник намного СИЛЬНЕЕ нас (мы — андердог);
    # s → 0, когда соперник намного СЛАБЕЕ нас (мы — фаворит).
    x = rank_diff / MAX_RANK_DIFF
    STEEPNESS = 4.0
    s = 1.0 / (1.0 + math.exp(STEEPNESS * x))

    if match_result == "win":
        # Победили: чем сильнее был соперник (s→1), тем ближе к максимуму.
        low, high = ranges["win"]
        points = low + s * (high - low)
    else:
        # Проиграли: чем сильнее был соперник (s→1), тем МЕНЬШЕ теряем (ближе к low);
        # чем слабее был соперник (s→0), тем БОЛЬШЕ теряем (ближе к high).
        low, high = ranges["loss"]
        points = high - s * (high - low)

    points *= margin_factor

    result = points if match_result == "win" else -points
    return round(result, 1)


def _match_margin_factor(winner_maps: int, loser_maps: int) -> float:
    """
    Множитель за разгромность счёта матча (0.9..1.0).

    Чистая победа (например 2:0, 3:0) — полный множитель 1.0.
    Победа в решающей карте с минимальным перевесом (2:1, 3:2) — 0.9.
    Добавляет реализма: разгром сильнее двигает рейтинг, чем волевая
    победа на тай-брейке. Если карт не было (Bo1 или данные отсутствуют),
    возвращает 1.0.
    """
    total = winner_maps + loser_maps
    if total <= 0 or loser_maps <= 0:
        return 1.0
    dominance = winner_maps / total  # 0.5..1.0 (минимум зависит от формата)
    dominance = max(0.5, min(1.0, dominance))
    return round(0.9 + 0.1 * ((dominance - 0.5) / 0.5), 3)

# ── Примеры использования calculate_rating_change ────────────────────────────

def vrs_rating_examples() -> list[dict]:
    """
    Возвращает список примеров для /vrs_info.
    Каждый пример: словарь с параметрами и результатом.
    """
    cases = [
        # (team_rank, opponent_rank, tier, result, описание)
        (5,  1,  "S", "win",  "Аутсайдер #5 побеждает лидера #1 на S-тире"),
        (5,  1,  "S", "loss", "Аутсайдер #5 проигрывает лидеру #1 на S-тире"),
        (5,  50, "S", "win",  "Топ-команда #5 легко побеждает #50 на S-тире"),
        (5,  50, "S", "loss", "Топ-команда #5 неожиданно проигрывает #50 на S-тире"),
        (10, 10, "A", "win",  "Равные команды (оба #10) на A-тире, победа"),
        (10, 10, "A", "loss", "Равные команды (оба #10) на A-тире, поражение"),
        (20, 8,  "B", "win",  "Команда #20 обыгрывает #8 на B-тире"),
        (20, 8,  "B", "loss", "Команда #20 проигрывает #8 на B-тире"),
        (15, 15, "B", "win",  "Равные команды #15 на B-тире, победа"),
        (3,  200,"S", "win",  "Топ-3 vs далёкий аутсайдер #200 (обрезка до 50), S-тир"),
    ]
    results = []
    for team_rank, opp_rank, tier, result, desc in cases:
        change = calculate_rating_change(team_rank, opp_rank, tier, result)
        results.append({
            "desc":         desc,
            "team_rank":    team_rank,
            "opp_rank":     opp_rank,
            "tier":         tier,
            "result":       result,
            "change":       change,
        })
    return results

# ─────────────────────────────────────────────────────────────────────────────


async def db_add_rating(nick: str, rating: float) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE players SET rating=? WHERE nick=?",
            (rating, nick)
        )
        rowcount = cursor.rowcount
        await db.commit()
    return rowcount > 0


async def db_get_match_history(team_name: str, limit: int = 5) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT team_a, team_b, result, score, played_at
               FROM match_history
               WHERE team_a=? OR team_b=?
               ORDER BY id DESC LIMIT ?""",
            (team_name, team_name, limit)
        ) as cursor:
            rows = await cursor.fetchall()
    return rows


async def db_is_admin(telegram_id: int) -> bool:
    if telegram_id == OWNER_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM admins WHERE telegram_id=?", (telegram_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def db_is_moderator(telegram_id: int) -> bool:
    """Возвращает True если пользователь — модератор, администратор или владелец."""
    if await db_is_admin(telegram_id):
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM moderators WHERE telegram_id=?", (telegram_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


# ── ЛИДЕРЫ КОМАНД ──────────────────────────────────────────────────────────

async def db_set_leader(team_name: str, telegram_id: int, username: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO team_leaders (team_name, telegram_id, username, added_at) VALUES (?,?,?,?) "
            "ON CONFLICT(team_name) DO UPDATE SET telegram_id=excluded.telegram_id, username=excluded.username, added_at=excluded.added_at",
            (team_name, telegram_id, username, datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()


async def db_remove_leader(team_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM team_leaders WHERE team_name=?", (team_name,))
        await db.commit()
        return cursor.rowcount > 0


async def db_get_team_leader(team_name: str) -> tuple | None:
    """Возвращает (telegram_id, username) лидера команды или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, username FROM team_leaders WHERE team_name=?", (team_name,)
        ) as cursor:
            return await cursor.fetchone()


async def db_get_leader_teams(telegram_id: int) -> list[str]:
    """Команды, в которых пользователь является лидером."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name FROM team_leaders WHERE telegram_id=?", (telegram_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def db_list_leaders() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name, telegram_id, username FROM team_leaders ORDER BY team_name"
        ) as cursor:
            return await cursor.fetchall()


async def db_is_team_leader(telegram_id: int, team_name: str) -> bool:
    leader = await db_get_team_leader(team_name)
    return bool(leader and leader[0] == telegram_id)


# ── ПАНЕЛЬ ЛИДЕРА: ИГРОКИ ────────────────────────────────────────────────────

async def db_get_player_by_id(player_id: int) -> tuple | None:
    """Возвращает (id, nick, team_name, role, rating, is_reserve, age) по id игрока."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, nick, team_name, role, rating, is_reserve, age FROM players WHERE id=?",
            (player_id,)
        ) as cursor:
            return await cursor.fetchone()


async def db_get_roster_for_panel(team_name: str) -> list[tuple]:
    """Возвращает (id, nick, role, is_reserve) без тренера, для панели лидера."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, nick, role, is_reserve FROM players WHERE team_name=? AND is_reserve IN (0,1) "
            "ORDER BY is_reserve ASC, id ASC",
            (team_name,)
        ) as cursor:
            return await cursor.fetchall()


async def db_toggle_reserve_by_id(player_id: int) -> tuple[bool, int, str, str] | None:
    """Переключает резерв/основа по id игрока. Возвращает (ok, new_status, nick, team_name)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_reserve, nick, team_name FROM players WHERE id=?", (player_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row or row[0] == 2:
            return None
        new_status = 0 if row[0] == 1 else 1
        await db.execute("UPDATE players SET is_reserve=? WHERE id=?", (new_status, player_id))
        await db.commit()
        return True, new_status, row[1], row[2]


async def db_set_role_by_id(player_id: int, role: str) -> tuple[str, str] | None:
    """Меняет роль по id игрока. Возвращает (nick, team_name)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT nick, team_name FROM players WHERE id=?", (player_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        await db.execute("UPDATE players SET role=? WHERE id=?", (role, player_id))
        await db.commit()
        return row


# ── ЗАЯВКИ НА ТРАНСФЕР МЕЖДУ ЛИДЕРАМИ ────────────────────────────────────────

async def db_create_transfer_request(player_id: int, nick: str, from_team: str, to_team: str,
                                      initiated_by: int, mode: str, request_type: str = "transfer",
                                      until_tour: str = "", partner_player_id: int = 0,
                                      partner_nick: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO transfer_requests (player_id, nick, from_team, to_team, initiated_by, mode, status, "
            "request_type, until_tour, partner_player_id, partner_nick, created_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?,?,?,?, ?)",
            (player_id, nick, from_team, to_team, initiated_by, mode,
             request_type, until_tour, partner_player_id, partner_nick,
             datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()
        return cursor.lastrowid


async def db_get_transfer_request(request_id: int) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, player_id, nick, from_team, to_team, initiated_by, mode, status, "
            "request_type, until_tour, partner_player_id, partner_nick FROM transfer_requests WHERE id=?",
            (request_id,)
        ) as cursor:
            return await cursor.fetchone()


async def db_set_transfer_status(request_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE transfer_requests SET status=? WHERE id=?", (status, request_id))
        await db.commit()


async def db_add_moderator(telegram_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO moderators (telegram_id, username, added_at) VALUES (?,?,?)",
            (telegram_id, username, datetime.now().isoformat(timespec='seconds'))
        )
        await db.commit()


async def db_remove_moderator(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM moderators WHERE telegram_id=?", (telegram_id,)
        )
        rowcount = cursor.rowcount
        await db.commit()
    return rowcount > 0


async def db_list_moderators() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, username, added_at FROM moderators ORDER BY added_at"
        ) as cursor:
            return await cursor.fetchall()


async def db_add_admin(telegram_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (telegram_id, username, added_at) VALUES (?,?,?)",
            (telegram_id, username, datetime.now().isoformat(timespec='seconds'))
        )
        await db.commit()


async def db_remove_admin(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM admins WHERE telegram_id=?", (telegram_id,)
        )
        rowcount = cursor.rowcount
        await db.commit()
    return rowcount > 0


async def db_list_admins() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, username, added_at FROM admins ORDER BY added_at"
        ) as cursor:
            return await cursor.fetchall()

class MapSimulator:

    def __init__(
        self,
        map_name: str,
        team_a: tuple,
        team_b: tuple,
        side_a: str,
        players_a: list[tuple],
        players_b: list[tuple],
    ) -> None:
        self.map_name  = map_name
        self.team_a    = team_a
        self.team_b    = team_b
        self.side_a    = side_a
        self.players_a = players_a
        self.players_b = players_b

        def _chem_bonus(c):
            # Сыгранность 0–100 → множитель 1.00–1.90 (было 1.00–1.70) —
            # сыгранность теперь весомее влияет на итоговую силу команды.
            return 1.0 + (c / 50) * 0.45

        def _spirit_bonus(s):
            # Дух 0–100 → множитель 0.75–1.25 (было 0.90–1.15) — разброс
            # расширен вдвое, дух команды теперь ощутимо решает исход.
            return 0.75 + (s / 100) * 0.50

        def _player_strength(rating):
            # Плавная непрерывная кривая без резких порогов и БЕЗ потолка —
            # каждая десятая рейтинга имеет значение на любом уровне силы лиги.
            # Экспонента увеличена с 1.6 до 1.8 — разница в рейтинге между
            # игроками теперь даёт более выраженный разрыв в силе.
            # rating 15 → 0, 17 → ~10, 19 → ~33, 21 → ~76, 23 → ~121, 26 → ~135,
            # 27 → ~157, 27.8 → ~176, 30 → ~236, 32 → ~299.
            return max(0.0, 2.5 * (rating - 15) ** 1.8)

        active_only_a = [p for p in players_a if p[1] != "Coach"]
        active_only_b = [p for p in players_b if p[1] != "Coach"]
        avg_strength_a = sum(_player_strength(p[2]) for p in active_only_a) / len(active_only_a) if active_only_a else 35.0
        avg_strength_b = sum(_player_strength(p[2]) for p in active_only_b) / len(active_only_b) if active_only_b else 35.0

        chem_mult_a = _chem_bonus(team_a[3])
        chem_mult_b = _chem_bonus(team_b[3])
        spirit_mult_a = _spirit_bonus(team_a[2])
        spirit_mult_b = _spirit_bonus(team_b[2])

        strength_a = avg_strength_a * chem_mult_a * spirit_mult_a
        strength_b = avg_strength_b * chem_mult_b * spirit_mult_b

        has_igl_a = any(p[1] == "IGL" for p in players_a)
        has_support_a = any(p[1] == "Support" for p in players_a)
        has_awper_a = any(p[1] == "AWPer" for p in players_a)
        has_igl_b = any(p[1] == "IGL" for p in players_b)
        has_support_b = any(p[1] == "Support" for p in players_b)
        has_awper_b = any(p[1] == "AWPer" for p in players_b)
        
        if has_igl_a:
            strength_a *= 1.08
        if has_support_a:
            strength_a *= 1.05
        if has_awper_a:
            strength_a *= 1.06
        if has_igl_b:
            strength_b *= 1.08
        if has_support_b:
            strength_b *= 1.05
        if has_awper_b:
            strength_b *= 1.06

        # Coach bonus: rating 15–32 → multiplier 1.00–1.07
        def _coach_mult(coach_rating: float) -> float:
            return 1.0 + ((coach_rating - 15.0) / 17.0) * 0.07

        coach_a = next((p for p in players_a if p[1] == "Coach"), None)
        coach_b = next((p for p in players_b if p[1] == "Coach"), None)
        if coach_a:
            strength_a *= _coach_mult(coach_a[2])
        if coach_b:
            strength_b *= _coach_mult(coach_b[2])
        
        total = strength_a + strength_b
        if total > 0:
            # Плавная логистическая кривая вместо резких порогов — откалибрована
            # так, чтобы сильный фаворит побеждал заметно чаще (реалистичнее):
            # ratio 1.15→62%, 1.3→70%, 1.6→82%, 2.0→89%, 3.0→96%(clip 95%).
            # Крутизна увеличена с 2.35 до 3.0, чтобы разница в силе весомее
            # влияла на исход, но без резких скачков на границах.
            log_ratio = math.log(max(strength_a, 0.01) / max(strength_b, 0.01))
            prob = 1 / (1 + math.exp(-3.0 * log_ratio))
        else:
            prob = 0.5

        # Бонус к винрейту из магазина лиги (буткемп) — накопленные проценты
        # команды прибавляются/вычитаются напрямую к вероятности победы в раунде.
        boost_a = (team_a[4] if len(team_a) > 4 else 0.0) or 0.0
        boost_b = (team_b[4] if len(team_b) > 4 else 0.0) or 0.0
        prob += (boost_a - boost_b) / 100.0

        # Случайный шум уменьшен (было ±0.03), чтобы не «съедать» явное
        # преимущество фаворита случайностью.
        prob = max(0.05, min(0.95, prob + random.uniform(-0.015, 0.015)))
        
        self.base_prob = prob
        self.strength_a = strength_a
        self.strength_b = strength_b
        
        self.score_a      = 0
        self.score_b      = 0
        self.pauses_a     = 4
        self.pauses_b     = 4
        self.losestreak_a = 0
        self.losestreak_b = 0
        self.paused_a     = False
        self.paused_b     = False
        self.logs: list[str] = []
        # kd[nick] = [K, D, A, adr_dmg, kast_rounds, entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists]
        self.kd: dict[str, list] = {}
        for nick, role, _, _r, *_ in players_a + players_b:
            if role != "Coach":
                self.kd[nick] = [0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0]
        self._round_count = 0

    def _igl(self, players: list[tuple]) -> str:
        for nick, role, _, _r, *_ in players:
            if role == "IGL":
                return nick
        active = [p for p in players if p[1] != "Coach"]
        return (active[0][0] if active else players[0][0])

    def _weighted_player(self, players: list[tuple]) -> tuple:
        weights = []
        for p in players:
            rating = p[2]
            role   = p[1]
            is_key = role == "IGL"

            # Экспоненциальная зависимость: топ-игроки получают сильно больший вес
            if rating >= 26:
                w = rating * random.uniform(5.5, 7.0)   # элита — явно доминируют
            elif rating >= 24:
                w = rating * random.uniform(4.0, 5.5)
            elif rating >= 22:
                w = rating * random.uniform(3.0, 4.5)
            elif rating >= 20:
                w = rating * random.uniform(2.2, 3.5)
            elif rating >= 18:
                w = rating * random.uniform(1.2, 2.2)
            else:
                # Слабые игроки — маленький, но ненулевой шанс
                perf_chance = 0.35 if is_key else 0.15
                if random.random() < perf_chance:
                    w = rating * 1.5
                else:
                    w = rating * 0.6

            weights.append(max(0.1, w))
        return random.choices(players, weights=weights, k=1)[0]

    def _random_player(self, players: list[tuple]) -> tuple:
        active = [p for p in players if p[1] != "Coach"]
        return self._weighted_player(active if active else players)

    def _round_flavor(self, winner_players: list[tuple], loser_players: list[tuple]) -> str:
        p = self._random_player(winner_players)
        nick, role, rating, _r, *_ = p

        enemy = self._random_player(loser_players)
        enemy_nick = enemy[0]

        role_lines = {
            "AWPer": [
                f"🎯 {nick} снял {enemy_nick} через пик с AWP — чистый хэдшот с первого выстрела",
                f"🎯 {nick} держал угол на AWP, {enemy_nick} даже не успел среагировать",
                f"🎯 {nick} поймал moving shot — {enemy_nick} бежал, но это его не спасло",
                f"🎯 {nick} сделал no-scope через дым — чат: 'КАК?!'",
                f"🎯 {nick} удержал сайд в одиночку — два тела, AWP перезаряжен",
            ],
            "Rifler": [
                f"🔫 {nick} вышел на размене с {enemy_nick} и выиграл дуэль на M4/AK",
                f"🔫 {nick} закрыл угол очередью — трое в линию, пробило насквозь",
                f"🔫 {nick} поднялся с эко и сделал форс-раунд реальностью",
                f"🔫 {nick} торговал позицию — два килла и время на тиммейтов",
                f"🔫 {nick} разрезал ротацию {enemy_nick} в одиночку, не дав команде перестроиться",
            ],
            "Support": [
                f"🛡 {nick} залил сайт молотовами и флешками, команда прошла чисто без потерь",
                f"🛡 {nick} бросил молотов точно в стек — {enemy_nick} вышел прямо под стволы",
                f"🛡 {nick} поставил дым в последний момент, закрыв AWP-позицию",
                f"🛡 {nick} организовал выход через B — флеш в глаза, проход чистый",
                f"🛡 {nick} торговал позицию тиммейта, вытащил раунд через утилити",
            ],
            "Entry Fragger": [
                f"⚡ {nick} первым ворвался на сайт — {enemy_nick} не успел среагировать",
                f"⚡ {nick} открыл раунд через пик в агрессию и получил double-kill на входе",
                f"⚡ {nick} пробил инфо для команды ценой жизни — и раунд взяли",
                f"⚡ {nick} вскрыл ротацию противника, весь тим прошёл за ним на сайт",
                f"⚡ {nick} зашёл первым под флеш — двух снял, сайт открыт",
            ],
            "Lurker": [
                f"🕵 {nick} тихо зашёл с фланга — {enemy_nick} не слышал шагов до последнего",
                f"🕵 {nick} сделал ротацию за 3 секунды до взрыва и закрыл раунд",
                f"🕵 {nick} додавил 1v1 против {enemy_nick} — дефуз не состоялся",
                f"🕵 {nick} вскрыл ротацию противника, весь тим прошёл за ним",
                f"🕵 {nick} затаился за углом — вышел когда никто не ждал",
            ],
            "IGL": [
                f"📢 {nick} прочитал тайминг {enemy_nick} — коллит стек за 10 секунд до взрыва",
                f"📢 {nick} перестраивает ротацию в реальном времени — B меняется на A мгновенно",
                f"📢 {nick} решает идти через мид в силовой — пять человек, один выход",
                f"📢 {nick} объявляет eco-round fake — и команда разбирает полный закуп без потерь",
                f"📢 {nick} читает игру противника насквозь, следующий раунд уже распланирован",
            ],
        }

        clutch_lines = [
            f"🔥 {nick} остался 1v{random.randint(2,3)} — выдохнул, взял всех, раунд за командой",
            f"🔥 {nick} сделал клатч на таймере — бомба тикала, нервы стальные",
            f"🔥 {nick} поднялся с 12 хп и закрыл раунд — чат не успел написать 'GG'",
            f"💫 {nick} эйс — пять тел, одна обойма, ноль смертей",
            f"💫 {nick} triple kill через один угол — противник не понял откуда летит",
            f"⚡ {nick} открыл раунд double-peek и вытащил инициативу для всей команды",
        ]

        if rating >= 24 and random.random() < 0.40:
            return random.choice(clutch_lines)
        elif rating >= 20 and random.random() < 0.20:
            return random.choice(clutch_lines)

        lines = role_lines.get(role, role_lines["Rifler"])
        return random.choice(lines)

    def _simulate_round(self, round_num: int, half: int) -> bool:
        prob = self.base_prob

        # В 1-м тайме CT = сторона, стартовавшая как CT.
        # Во 2-м тайме стороны меняются: A стартовала T → теперь CT.
        ct_side_is_a = (
            (half == 1 and self.side_a == "CT") or
            (half == 2 and self.side_a == "T")
        )

        if ct_side_is_a:
            prob = min(0.95, prob + 0.03)
        else:
            prob = max(0.05, prob - 0.03)

        actual_round = round_num if round_num < 1000 else round_num - 1000
        is_pistol = actual_round in (1, 13)
        if is_pistol:
            pistol_log = "🔫 Пистолетный раунд"
            if ct_side_is_a:
                prob = min(0.95, prob + 0.05)
                pistol_log += f" — {self.team_a[1]} на CT с преимуществом позиций"
            else:
                pistol_log += f" — {self.team_b[1]} на CT с преимуществом позиций"
            self.logs.append(f"  {pistol_log}")

        is_eco = actual_round in (2, 14)
        if is_eco:
            self.logs.append("  💸 Команды экономят после пистолета — эко/форс раунд")
            if ct_side_is_a:
                prob = max(0.05, prob - 0.08)
            else:
                prob = min(0.95, prob + 0.08)

        if self.losestreak_a >= 2 and not is_eco and not is_pistol:
            self.logs.append(f"  💰 {self.team_a[1]} форс-байт — рискуют закупом")
            prob = min(0.95, prob + 0.04)
        if self.losestreak_b >= 2 and not is_eco and not is_pistol:
            self.logs.append(f"  💰 {self.team_b[1]} форс-байт — рискуют закупом")
            prob = max(0.05, prob - 0.04)

        if self.losestreak_a >= 3 and self.pauses_a > 0 and not self.paused_a:
            igl = self._igl(self.players_a)
            self.logs.append(f"  ⏸ {igl} (IGL) берёт тайм-аут — {self.pauses_a} пауз осталось")
            self.logs.append(f"  📋 {self.team_a[1]} перестраивают тактику на следующий раунд")
            self.pauses_a -= 1
            self.paused_a = True
            prob = min(0.95, prob + 0.07)

        if self.losestreak_b >= 3 and self.pauses_b > 0 and not self.paused_b:
            igl = self._igl(self.players_b)
            self.logs.append(f"  ⏸ {igl} (IGL) берёт тайм-аут — {self.pauses_b} пауз осталось")
            self.logs.append(f"  📋 {self.team_b[1]} перестраивают тактику на следующий раунд")
            self.pauses_b -= 1
            self.paused_b = True
            prob = max(0.05, prob - 0.07)

        a_wins = random.random() < prob

        # Снимок статистики ДО начисления киллов/ассистов этого раунда (для KAST)
        kd_before = {nick: list(stats) for nick, stats in self.kd.items()}

        if a_wins:
            self.score_a += 1
            self.losestreak_a = 0
            self.losestreak_b += 1
            self.paused_b = False
            winner_players_list = self.players_a
            loser_players_list  = self.players_b
        else:
            self.score_b += 1
            self.losestreak_b = 0
            self.losestreak_a += 1
            self.paused_a = False
            winner_players_list = self.players_b
            loser_players_list  = self.players_a

        self._round_count += 1
        flavor = self._round_flavor(winner_players_list, loser_players_list)

        killer_tuple = self._random_player(winner_players_list)
        killer = killer_tuple[0]
        killer_rating = killer_tuple[2]

        if killer_rating >= 26:
            killer_kills = random.choices([3, 4, 5], weights=[20, 45, 35])[0]
        elif killer_rating >= 22:
            killer_kills = random.choices([2, 3, 4, 5], weights=[15, 40, 35, 10])[0]
        else:
            killer_kills = random.choices([1, 2, 3, 4], weights=[20, 45, 28, 7])[0]

        killer_kills = min(killer_kills, 5)
        self.kd[killer][0] += killer_kills

        # Multi-kill tracking for killer
        if killer_kills == 3:
            self.kd[killer][7] += 1   # mk3
        elif killer_kills == 4:
            self.kd[killer][8] += 1   # mk4
        elif killer_kills == 5:
            self.kd[killer][9] += 1   # mk5

        remaining_kills = max(0, 5 - killer_kills)
        other_winners = [p for p in winner_players_list if p[0] != killer]
        for _ in range(remaining_kills):
            if other_winners:
                extra_killer = self._random_player(other_winners)[0]
                self.kd[extra_killer][0] += 1

        dead_count = 5
        # Смерти: слабые игроки умирают ЧАЩЕ — обратные веса по рейтингу
        loser_active = [p for p in loser_players_list if p[1] != "Coach"]
        if loser_active:
            max_r = max(p[2] for p in loser_active)
            death_weights = [max(0.3, (max_r - p[2] + 1.0)) for p in loser_active]
            for _ in range(dead_count):
                v = random.choices(loser_active, weights=death_weights, k=1)[0][0]
                self.kd[v][1] += 1

        for p in winner_players_list:
            nick_p, role_p, _, _r, *_ = p
            if role_p == "Coach":
                continue
            assist_chance = 0.55 if role_p == "Support" else 0.30
            if nick_p != killer and random.random() < assist_chance:
                self.kd[nick_p][2] += 1
                # Flash assist for Support role
                if role_p == "Support" and random.random() < 0.45:
                    self.kd[nick_p][12] += 1  # flash_assists

        # ADR simulation — damage per round per player
        all_active_w = [p for p in winner_players_list if p[1] != "Coach"]
        all_active_l = [p for p in loser_players_list if p[1] != "Coach"]
        for p in all_active_w:
            nick_p, role_p, rating_p, *_ = p
            base_adr = 65 + (rating_p - 19) * 4.0
            adr = max(20.0, base_adr + random.uniform(-20, 25))
            self.kd[nick_p][3] += adr  # adr_dmg (total dmg this round)
        for p in all_active_l:
            nick_p, role_p, rating_p, *_ = p
            base_adr = 45 + (rating_p - 19) * 3.0
            adr = max(10.0, base_adr + random.uniform(-15, 20))
            self.kd[nick_p][3] += adr

        # Utility damage — Support and IGL generate more
        for players_list in (all_active_w, all_active_l):
            for p in players_list:
                nick_p, role_p, *_ = p
                if role_p in ("Support", "IGL"):
                    ud = random.uniform(0, 18)
                else:
                    ud = random.uniform(0, 8)
                self.kd[nick_p][11] += ud  # util_dmg

        # KAST tracking — did the player Kill/Assist/Survive/Trade this round?
        # Используем дельты за ЭТОТ раунд, а не накопленные суммы
        for p in all_active_w:
            nick_p = p[0]
            round_k = self.kd[nick_p][0] - kd_before[nick_p][0]
            round_a = self.kd[nick_p][2] - kd_before[nick_p][2]
            survived = random.random() < 0.70
            if round_k > 0 or round_a > 0 or survived:
                self.kd[nick_p][4] += 1  # kast_rounds
        for p in all_active_l:
            nick_p = p[0]
            round_k = self.kd[nick_p][0] - kd_before[nick_p][0]
            round_a = self.kd[nick_p][2] - kd_before[nick_p][2]
            if round_k > 0 or round_a > 0 or random.random() < 0.20:
                self.kd[nick_p][4] += 1

        # Entry kills/deaths — Entry Fragger role has higher chance
        entry_done = False
        for players_list, is_winner in ((winner_players_list, True), (loser_players_list, False)):
            for p in players_list:
                nick_p, role_p, *_ = p
                if role_p == "Coach":
                    continue
                entry_chance = 0.35 if role_p == "Entry Fragger" else 0.10
                if not entry_done and random.random() < entry_chance:
                    if is_winner:
                        self.kd[nick_p][5] += 1  # entry_k
                    else:
                        self.kd[nick_p][6] += 1  # entry_d
                    entry_done = True
                    break

        # Clutch simulation — if 1 winner vs multiple losers scenario
        if random.random() < 0.08:
            # Pick a winner who clutched
            clutch_player = self._random_player(all_active_w) if all_active_w else None
            if clutch_player:
                self.kd[clutch_player[0]][10] += 1  # clutches_won

        winner_is_ct = (a_wins and ct_side_is_a) or (not a_wins and not ct_side_is_a)
        ct_team_name = self.team_a[1] if ct_side_is_a else self.team_b[1]

        if not winner_is_ct and random.random() < 0.65:
            t_winner_list = winner_players_list if not winner_is_ct else loser_players_list
            planter = self._random_player(t_winner_list)
            extra = f" | 💣 {planter[0]} поставил бомбу — {ct_team_name} не успели задефузить"
            flavor += extra
        elif winner_is_ct and random.random() < 0.40:
            defuser_list = winner_players_list
            defuser = self._random_player(defuser_list)
            seconds = random.randint(1, 4)
            extra = f" | 🔵 {defuser[0]} задефузил за {seconds}с до взрыва"
            flavor += extra

        self.logs.append(
            f"  R{actual_round:02d} [{self.score_a}:{self.score_b}] — {flavor}"
        )
        return a_wins

    def simulate(self) -> tuple[int, int, list[str], dict[str, list], int]:
        ct_team = self.team_a[1] if self.side_a == "CT" else self.team_b[1]
        t_team  = self.team_b[1] if self.side_a == "CT" else self.team_a[1]
        self.logs.append(
            f"\n🗺 <b>Карта: {self.map_name}</b>\n"
            f"  🔵 CT: <b>{ct_team}</b>  vs  🔴 T: <b>{t_team}</b>"
        )

        for r in range(1, 13):
            self._simulate_round(r, half=1)
            if self.score_a == 13 or self.score_b == 13:
                return self.score_a, self.score_b, self.logs, self.kd, self._round_count

        self.logs.append(f"  ── Смена сторон: {self.score_a}:{self.score_b} ──")

        for r in range(13, 25):
            self._simulate_round(r, half=2)
            if self.score_a == 13 or self.score_b == 13:
                return self.score_a, self.score_b, self.logs, self.kd, self._round_count

        ot_round = 1
        ot_period = 0
        while self.score_a == self.score_b:
            ot_period += 1
            if ot_period > 10:  # защита от бесконечного цикла
                # принудительно завершаем: побеждает команда с большей силой
                if self.strength_a >= self.strength_b:
                    self.score_a += 1
                else:
                    self.score_b += 1
                break
            self.logs.append(f"  ⚡ Овертайм! Счёт {self.score_a}:{self.score_b}")
            ot_start_a = self.score_a
            ot_start_b = self.score_b
            for r in range(6):
                self._simulate_round(1000 + ot_round, half=2)
                ot_round += 1
            diff_a = self.score_a - ot_start_a
            diff_b = self.score_b - ot_start_b
            if diff_a != diff_b:
                break

        return self.score_a, self.score_b, self.logs, self.kd, self._round_count

def veto_maps(fmt: str) -> list[str]: #скайред это есче я добнул для более менее нормального выбора мап
    pool = MAP_POOL[:]
    random.shuffle(pool)
    maps: list[str] = []

    match fmt:
        case "Bo1":
            for _ in range(3):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            for _ in range(3):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            maps = pool[:1]

        case "Bo2":
            for _ in range(1):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            for _ in range(1):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            if len(pool) >= 1:
                maps.append(pool.pop(random.randrange(len(pool))))
            if len(pool) >= 1:
                maps.append(pool.pop(random.randrange(len(pool))))

        case "Bo3":
            for _ in range(2):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            for _ in range(2):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            maps.append(pool.pop(random.randrange(len(pool))))
            maps.append(pool.pop(random.randrange(len(pool))))
            maps.append(pool[0] if pool else "Mirage")

        case "Bo5":
            for _ in range(1):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            for _ in range(1):
                if len(pool) > 1:
                    pool.pop(random.randrange(len(pool)))
            maps = pool[:5]

    limit = {"Bo1": 1, "Bo2": 2, "Bo3": 3, "Bo5": 5}.get(fmt, 1)
    return maps[:limit]


def compute_hltv30(stats: list, rounds: int) -> float:
    """Compute HLTV 3.0 Rating from extended player stats list.
    stats = [K, D, A, adr_dmg, kast_rounds, entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists]
    rounds = total rounds played
    """
    K, D, A, adr_dmg, kast_rounds, entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists = stats
    rounds = max(1, rounds)
    # 1. KD Rating
    kd = K / D if D > 0 else float(K)
    kd_rating = kd * 0.3
    # 2. ADR Rating
    adr = adr_dmg / rounds
    adr_rating = (adr / 75.0) * 0.25
    # 3. KAST Rating
    kast_pct = (kast_rounds / rounds) * 100.0
    kast_rating = (kast_pct / 70.0) * 0.15
    # 4. Impact 3.0
    mk_score = mk3 * 1.2 + mk4 * 1.4 + mk5 * 1.6
    entry_net = entry_k - entry_d
    entry_score = max(0.0, entry_net * 0.05)
    clutch_score = clutches_won * 0.05
    impact = ((mk_score + entry_score + clutch_score) / 2.0) * 0.20
    # 5. Utility Rating
    util_rating = ((util_dmg / 10.0) + (flash_assists * 0.5)) * 0.10
    rating = kd_rating + adr_rating + kast_rating + impact + util_rating
    return round(rating, 2)


def compute_hltv20(stats: list, rounds: int) -> float:
    """Compute official HLTV Rating 2.0 from extended player stats list.
    stats = [K, D, A, adr_dmg, kast_rounds, entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists]
    rounds = total rounds played
    Formula: Rating 2.0 = 0.007387*KAST + 0.359123*KPR + (-0.532934)*DPR + 0.237233*Impact + 0.003235*ADR + 0.1587
    """
    K, D, A, adr_dmg, kast_rounds, entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists = stats
    rounds = max(1, rounds)
    KPR = K / rounds
    DPR = D / rounds
    ADR = adr_dmg / rounds
    KAST = (kast_rounds / rounds) * 100.0
    # Impact: full formula if entry data available
    entry_kpr = entry_k / rounds
    entry_dpr = entry_d / rounds
    mk_factor = (mk3 * 0.5 + mk4 * 0.75 + mk5 * 1.0 + clutches_won * 0.5) / rounds
    Impact = (1.61 * KPR) + (1.35 * entry_kpr) - (1.10 * entry_dpr) + (0.05 * mk_factor) - 0.1
    rating = (0.007387 * KAST) + (0.359123 * KPR) + (-0.532934 * DPR) + (0.237233 * Impact) + (0.003235 * ADR) + 0.1587
    return round(rating, 2)


def hltv30_verdict(rating: float) -> str:
    if rating >= 1.60:
        return "🔱 Godlike — запредельный уровень игры"
    elif rating >= 1.40:
        return "🔥 Феноменальный перформанс — игрок в топе матча"
    elif rating >= 1.25:
        return "⚡ Отличная игра — выше среднего про-уровня"
    elif rating >= 1.10:
        return "✅ Хорошая игра — стабильный про-перформанс"
    elif rating >= 0.95:
        return "🟡 Средняя игра — на уровне типичного про"
    elif rating >= 0.80:
        return "📉 Ниже среднего — не хватило импакта"
    else:
        return "❌ Слабая игра — игрок почти не влиял на раунды"


def fmt_hltv30_match(nick: str, team: str, stats: list, rounds: int) -> str:
    """Format a single player's HLTV 2.0 / 3.0 block for match output."""
    K, D, A, adr_dmg, kast_rounds, entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists = stats
    rounds = max(1, rounds)
    kd_val = round(K / D, 2) if D > 0 else float(K)
    adr = round(adr_dmg / rounds, 1)
    kast_pct = round((kast_rounds / rounds) * 100, 1)
    rating20 = compute_hltv20(stats, rounds)
    mk_str = f"3k:{mk3} 4k:{mk4} 5k:{mk5}" if (mk3 + mk4 + mk5) > 0 else "—"
    entry_str = f"{entry_k}-{entry_d}"
    ud_r = round(util_dmg / rounds, 1)
    lines = [
        f"  <b>{md_escape(nick)}</b> ({md_escape(team)})",
        f"    K/D/A: <b>{K}/{D}/{A}</b>  КД: <b>{kd_val:.2f}</b>  ADR: <b>{adr}</b>  KAST: <b>{kast_pct}%</b>",
        f"    Entry: {entry_str}  Multikills: {mk_str}  Clutches: {clutches_won}  UD/r: {ud_r}",
        f"    🏆 <b>Rating 2.0: {rating20:.2f}</b>",
    ]
    return "\n".join(lines)


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if await db_is_moderator(msg.from_user.id):
        await msg.answer(
            "🛡 Вы модератор/администратор — все управляющие команды собраны в <code>/admin</code>."
        )
    await msg.answer(
        "🏆 <b>Standoff 2 Киберспортивная Лига — Бот</b>\n\n"
        "👁 <b>Просмотр (для всех):</b>\n"
        "<code>/team_info [название]</code> — карточка команды (дух, состав, карты)\n"
        "<code>/stats [название]</code> — история матчей команды\n"
        "<code>/stat_tour [турнир]</code> — статистика турнира (HLTV 3.0)\n"
        "<code>/tours</code> — список всех турниров\n"
        "<code>/vrs_info</code> — таблица VRS рейтинга команд\n"
        "<code>/vrs_help</code> — справка по системе расчёта VRS\n"
        "<code>/merch_stats</code> — статистика продаж мерча команд\n"
        "<code>/hltv</code> — калькулятор HLTV 3.0 Rating по статистике игрока\n"
        "<code>/hltv_top</code> — топ-50 игроков по Rating 3.0, K/D, киллам, MVP\n"
        "<code>/card_player [ник]</code> — подробная карточка игрока\n"
        "<code>/bank</code> — общий счёт лиги (сюда уходят списанные с команд деньги)\n"
        "<code>/balance [название]</code> — баланс своей или любой команды\n"
        "<code>/balance_top</code> — топ команд лиги по балансу\n"
        "<code>/shop</code> — магазин улучшений команды (буткемп/устройства/девайсы)\n"
    )
    await msg.answer(
        "⚔️ <b>Матчи (для всех):</b>\n"
        "<code>/match [Bo1/Bo2/Bo3/Bo5] [Команда1] [Команда2] [Турнир | Стадия]</code> — симуляция матча\n"
    )
    await msg.answer(
        "🎮 <b>FACEIT (для всех):</b>\n"
        "<code>/faceit</code> — главное меню FACEIT (сыграть матч / топ FACEIT)\n"
        "<code>/faceit_profile [ник]</code> — карточка статистики FACEIT игрока\n"
    )
    await msg.answer(
        "🏟 <b>Команды 🔒:</b>\n"
        "<code>/create_team [название]</code> (2-я строка: VRS 150, затем каждый игрок: Ник Рейтинг Роль Возраст) — создать команду\n"
        "<code>/create_teams</code> — создать сразу НЕСКОЛЬКО команд одним сообщением (блоки через строку <code>---</code>)\n"
        "<code>/delete_team [название]</code> — удалить команду\n"
        "<code>/merch</code> — сгенерировать волну продаж мерча\n"
        "<code>/renameteam [старое] | [новое]</code> — переименовать команду\n"
        "<code>/addchemistry [команда] [1–50]</code> — задать сыгранность\n"
        "<code>/addchemistryall [команда1] [команда2] ... [1–50]</code> — сыгранность нескольким\n"
        "<code>/addspirit [команда] [0–100 или +N/-N]</code> — установить/изменить дух команды\n"
        "<code>/addspiritall [команда1] [команда2] ... [значение]</code> — дух нескольким\n"
    )
    await msg.answer(
        "👤 <b>Игроки 🔒:</b>\n"
        "<code>/create_player [команда] [ник] [рейтинг] [роль]</code> — добавить игрока\n"

        "<code>/reserve [команда] [ник]</code> — переключить резерв/основа\n"
        "<code>/set_leader [команда] [id]</code> — назначить лидера команды (админ, можно ответом на сообщение)\n"
        "<code>/remove_leader [команда]</code> — снять лидера (админ)\n"
        "<code>/leaders</code> — список лидеров команд\n"
        "<code>/addroles [команда] [ник] [роль]</code> — сменить роль\n"
        "<code>/addrolesall [команда] [ник1] [роль1] [ник2] [роль2] ...</code> — роли нескольким\n"
        "<code>/addrating [ник] [15.0–32.0]</code> — задать рейтинг\n"
        "<code>/addratingall [ник1] [рейт1] [ник2] [рейт2] ...</code> — рейтинг нескольким\n"
        "<code>/setage [ник] [14–40]</code> — задать возраст игрока\n"
        "<code>/fixagerating confirm</code> — авто-исправить перепутанные возраст/рейтинг у всех игроков\n"
        "<code>/tranings [команда]</code> — тренировка одной команды\n"
        "<code>/trainall</code> — тренировка всех команд лиги сразу (+35% Духа, +25 Сыгранности)\n"
    )
    await msg.answer(
        "🆓 <b>Свободные агенты:</b>\n"
        "<code>/free_agents</code> — список свободных агентов лиги (для всех)\n"
        "<code>/create_free_agent [ник] [рейтинг] [роль] [возраст]</code> — создать "
        "свободного агента 🔒\n"
        "<code>/sign_free_agent [команда] [ник]</code> — "
        "подписать агента в команду (лидер команды). "
        f"Лимит: не более {FREE_AGENT_SIGN_LIMIT_PER_TOUR} агентов за тур, сброс по /donetour\n"
    )
    await msg.answer(
        "🌍 <b>Лига 🔒:</b>\n"
        "<code>/teamboost</code> — выдать всем командам +20 Духа и +10 Сыгранности\n"
        "<code>/seed_tournaments</code> — загрузить сезонные турниры в реестр\n"
        "<code>/clear_merch_stats confirm</code> — сбросить статистику мерча\n"
        "<code>/faceit_reset_elo</code> — сбросить всю статистику FACEIT (админ)\n"
    )
    await msg.answer(
        "🔄 <b>Трансферы 🔒:</b>\n"
        "<code>/transfer [игрок] [откуда] [куда]</code> — постоянный трансфер\n"
        "<code>/transfer [игрок] [откуда] [куда] аренда [тур]</code> — аренда до конца тура\n"
        "<code>/swap [Игрок1] [Команда1] [Игрок2] [Команда2]</code> — обмен игроками\n"
        "<code>/loans</code> — список активных аренд\n"
    )
    await msg.answer(
        "📂 <b>Регистрация команд файлами (для всех):</b>\n"
        f"Составы читаются из файлов .txt в папке <code>{md_escape(HOTS_ROSTERS_DIR)}</code> "
        "— по одному файлу на команду, формат «Ник Рейтинг Роль Возраст» (как в /create_team). "
        "Дух и сыгранность выставляются по максимуму, ВРС — 0.\n"
        "<code>/hots</code> — список команд, зарегистрированных из этой папки\n"
        "<code>/hots_reload</code> — пересканировать папку и зарегистрировать новые команды 🔒\n"
    )
    await msg.answer(
        "🏆 <b>Турниры 🔒:</b>\n"
        "<code>/addtour [название]</code> — добавить турнир в реестр\n"
        "<code>/donetour [тур]</code> — завершить тур: вернуть арендованных игроков "
        "и сбросить лимит подписаний свободных агентов\n"
        "<code>/deltour [тур]</code> — удалить турнир и всю его статистику\n\n"
        "👑 <b>Только владелец:</b>\n"
        "<code>/addadmin [ID]</code> · <code>/removeadmin [ID]</code> · <code>/listadmins</code>\n\n"
        "🛡 <b>Управление модераторами (владелец / администратор):</b>\n"
        "<code>/addmoderator [ID]</code> — добавить модератора\n"
        "<code>/removemoderator [ID]</code> — удалить модератора\n"
        "<code>/listmoderators</code> — список модераторов\n\n"
        "🛡 <b>Права модератора:</b> /transfer · /swap · /addroles · /addrolesall · /merch · /loans"
    )


@dp.message(Command("hots"))
async def cmd_hots_list(msg: Message) -> None:
    """/hots — список команд лиги, зарегистрированных из папки HOTS_ROSTERS_DIR."""
    imported = await db_hots_list_imported()
    if not imported:
        await msg.answer(
            "📭 Пока ни одна команда не зарегистрирована через эту папку.\n\n"
            f"Создайте файл <code>{md_escape(HOTS_ROSTERS_DIR)}/название.txt</code>: "
            "первая строка — имя команды, дальше 5 игроков по одному на строке "
            "в формате <code>Ник Рейтинг Роль Возраст</code>, затем выполните "
            "<code>/hots_reload</code>."
        )
        return
    lines = ["📂 <b>Команды, зарегистрированные из файлов</b>", ""]
    for team_name, source_file, imported_at in imported:
        lines.append(f"• <b>{md_escape(team_name)}</b> (файл: {md_escape(source_file)})")
    lines.append("")
    lines.append("Подробности по составу и статистике: <code>/team_info [название]</code>")
    await safe_send_long(msg, "\n".join(lines))


@dp.message(Command("hots_reload"))
async def cmd_hots_reload(msg: Message) -> None:
    """/hots_reload — пересканировать папку HOTS_ROSTERS_DIR и зарегистрировать
    новые команды из файлов (уже существующие в лиге команды не трогает).
    Только для модераторов/админов."""
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ Команда доступна только модераторам и администраторам.")
        return
    created, errors = await hots_scan_and_load_rosters()
    if created:
        text = f"✅ Зарегистрировано новых команд: <b>{len(created)}</b>\n" + "\n".join(
            f"  • {md_escape(n)}" for n in created
        )
    else:
        text = "ℹ️ Новых команд для регистрации не найдено."
    if errors:
        text += "\n\n⚠️ Пропущено/ошибки:\n" + "\n".join(f"  • {md_escape(e)}" for e in errors)
    await safe_send_long(msg, text)


@dp.message(Command("admin"))
async def cmd_admin(msg: Message) -> None:
    """
    /admin — меню администраторов/модераторов: список всех управляющих команд
    по разделам. Доступно только модераторам, администраторам и владельцу.
    """
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ Команда доступна только модераторам и администраторам.")
        return

    is_admin = await db_is_admin(msg.from_user.id)
    is_owner = msg.from_user.id == OWNER_ID

    await msg.answer(
        "🛡 <b>Меню администраторов</b>\n\n"
        "👤 <b>Игроки:</b>\n"
        "<code>/addroles [команда] [ник] [роль]</code> — сменить роль\n"
        "<code>/addrolesall [команда] [ник1] [роль1] [ник2] [роль2] ...</code> — роли нескольким\n"
        "<code>/addrating [ник] [15.0–32.0]</code> — задать рейтинг\n"
        "<code>/addratingall [ник1] [рейт1] [ник2] [рейт2] ...</code> — рейтинг нескольким\n"
        "<code>/setage [ник] [14–40]</code> — задать возраст игрока\n"
        "<code>/fixagerating confirm</code> — авто-исправить перепутанные возраст/рейтинг у всех игроков\n"
        "<code>/tranings [команда]</code> — тренировка одной команды\n"
        "<code>/trainall</code> — тренировка всех команд лиги сразу (+35% Духа, +25 Сыгранности)\n"
    )

    if is_admin:
        await msg.answer(
            "🌍 <b>Лига 🔒:</b>\n"
            "<code>/teamboost</code> — выдать всем командам +20 Духа и +10 Сыгранности\n"
            "<code>/seed_tournaments</code> — загрузить сезонные турниры в реестр\n"
            "<code>/clear_merch_stats confirm</code> — сбросить статистику мерча\n"
            "<code>/faceit_reset_elo</code> — сбросить всю статистику FACEIT (админ)\n"
        )
        await msg.answer(
            "🏆 <b>Турниры 🔒:</b>\n"
            "<code>/addtour [название]</code> — добавить турнир в реестр\n"
            "<code>/donetour [тур]</code> — завершить тур: вернуть арендованных игроков "
            "и сбросить лимит подписаний свободных агентов\n"
            "<code>/deltour [тур]</code> — удалить турнир и всю его статистику\n"
        )
        await msg.answer(
            "🛡 <b>Модераторы (владелец / администратор):</b>\n"
            "<code>/addmoderator [ID]</code> — добавить модератора\n"
            "<code>/removemoderator [ID]</code> — удалить модератора\n"
            "<code>/listmoderators</code> — список модераторов\n"
        )
    else:
        await msg.answer(
            "🛡 <b>Права модератора:</b> /transfer · /swap · /addroles · /addrolesall · /merch · /loans\n"
            "Разделы «Лига», «Турниры» и управление модераторами доступны только администраторам."
        )

    if is_owner:
        await msg.answer(
            "👑 <b>Только владелец:</b>\n"
            "<code>/addadmin [ID]</code> · <code>/removeadmin [ID]</code> · <code>/listadmins</code>\n"
        )


VALID_ROLES_LOWER = {r.lower(): r for r in ROLES}


def parse_team_block(block_lines: list[str], line_offset: int = 0) -> tuple[dict | None, list[str]]:
    """Парсит один блок состава команды.

    block_lines[0]      — название команды
    block_lines[1]      — 'VRS 150'
    block_lines[2:]     — игроки / Резерв / Тренер (см. /create_team)

    line_offset — номер строки в исходном сообщении, с которой начинается
    block_lines[0] (нужен только для человекочитаемых номеров в ошибках).

    Возвращает (результат, ошибки). Если ошибки не пусты — результат = None.
    результат = {"name", "initial_vrs", "main_players", "reserve_players", "coach_player"}
    """
    errors: list[str] = []

    non_empty = [l for l in block_lines if l.strip()]
    if len(non_empty) < 3:
        errors.append("Блок слишком короткий — нужно минимум: Название, VRS N, и 5 игроков.")
        return None, errors

    name = block_lines[0].strip()
    if not name:
        errors.append("Не указано название команды.")
        return None, errors

    second_line = block_lines[1].strip()
    second_parts = second_line.split()
    if len(second_parts) != 2 or second_parts[0].upper() != "VRS":
        errors.append(f"«{md_escape(name)}»: вторая строка должна быть «VRS 150», а не «{md_escape(second_line)}».")
        return None, errors
    try:
        initial_vrs = int(second_parts[1])
        if initial_vrs < 0:
            raise ValueError
    except ValueError:
        errors.append(f"«{md_escape(name)}»: VRS должен быть целым числом ≥ 0.")
        return None, errors

    main_players: list[dict] = []
    reserve_players: list[dict] = []
    coach_player: dict | None = None
    in_reserve = False
    in_coach = False

    def parse_player_line(line: str, line_num: int) -> dict | None:
        tokens = line.split()
        if len(tokens) < 4:
            errors.append(f"«{md_escape(name)}», строка {line_num}: «{md_escape(line)}» — нужно минимум 4 токена (Ник Рейтинг Роль Возраст)")
            return None
        nick = tokens[0]
        try:
            rating = float(tokens[1])
            if not (15.0 <= rating <= 32.0):
                raise ValueError
        except ValueError:
            errors.append(f"«{md_escape(name)}», строка {line_num}: рейтинг «{md_escape(tokens[1])}» должен быть числом от 15.0 до 32.0")
            return None
        try:
            age = int(tokens[-1])
            if not (14 <= age <= 40):
                raise ValueError
        except ValueError:
            errors.append(f"«{md_escape(name)}», строка {line_num}: возраст «{md_escape(tokens[-1])}» должен быть целым числом от 14 до 40")
            return None
        role_raw = " ".join(tokens[2:-1])
        role = VALID_ROLES_LOWER.get(role_raw.lower())
        if role is None:
            errors.append(
                f"«{md_escape(name)}», строка {line_num}: роль «{md_escape(role_raw)}» не распознана. "
                f"Доступные: {', '.join(ROLES)}"
            )
            return None
        return {"nick": nick, "rating": rating, "role": role, "age": age}

    for i, raw_line in enumerate(block_lines[2:], start=line_offset + 3):
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == "резерв":
            in_reserve = True
            in_coach = False
            continue
        if line.lower() == "тренер":
            in_coach = True
            in_reserve = False
            continue
        player = parse_player_line(line, i)
        if player:
            if in_coach:
                if player["role"] != "Coach":
                    errors.append(f"«{md_escape(name)}», строка {i}: тренер должен иметь роль Coach")
                elif coach_player is not None:
                    errors.append(f"«{md_escape(name)}», строка {i}: можно указать только одного тренера")
                else:
                    coach_player = player
            elif in_reserve:
                reserve_players.append(player)
            else:
                main_players.append(player)

    if errors:
        return None, errors

    if len(main_players) != 5:
        errors.append(f"«{md_escape(name)}»: основной состав должен содержать ровно 5 игроков (сейчас: {len(main_players)}).")
    if len(reserve_players) > 2:
        errors.append(f"«{md_escape(name)}»: в резерве не более 2 игроков.")

    if errors:
        return None, errors

    return {
        "name": name,
        "initial_vrs": initial_vrs,
        "main_players": main_players,
        "reserve_players": reserve_players,
        "coach_player": coach_player,
    }, []


@dp.message(Command("create_teams"))
async def cmd_create_teams(msg: Message) -> None:
    """
    /create_teams — массовое создание сразу НЕСКОЛЬКИХ команд одним сообщением.

    Блоки команд разделяются строкой из трёх дефисов: ---
    Каждый блок — тот же формат, что и у /create_team (без самой команды в начале):

    <code>/create_teams
    NaVi
    VRS 150
    s1mple 20.5 AWPer 26
    electroNic 19.0 Rifler 25
    b1t 18.5 Entry Fragger 22
    Perfecto 17.5 Support 26
    navi_igl 18.0 IGL 28
    ---
    Virtus.pro
    VRS 140
    ...5 игроков...
    Резерв
    ...
    Тренер
    ...
    ---
    Spirit
    VRS 130
    ...5 игроков...</code>

    Если хотя бы в одном блоке есть ошибка или команда с таким названием уже
    существует — НИЧЕГО не создаётся (все блоки проверяются заранее), чтобы
    не получить наполовину созданную лигу.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    USAGE = (
        "❌ Формат: <code>/create_teams</code>, затем блоки команд через строку из "
        "трёх дефисов <code>---</code>. Каждый блок — как в <code>/create_team</code> "
        "(без самой команды в первой строке блока):\n\n"
        "<code>/create_teams\n"
        "NaVi\n"
        "VRS 150\n"
        "s1mple 20.5 AWPer 26\n"
        "electroNic 19.0 Rifler 25\n"
        "b1t 18.5 Entry Fragger 22\n"
        "Perfecto 17.5 Support 26\n"
        "navi_igl 18.0 IGL 28\n"
        "---\n"
        "Virtus.pro\n"
        "VRS 140\n"
        "FL1T 19.0 AWPer 22\n"
        "jL 18.0 Rifler 24\n"
        "ICY 17.5 Entry Fragger 21\n"
        "fame 18.5 Support 20\n"
        "electroNic 19.5 IGL 25</code>\n\n"
        "Можно указывать любое количество блоков подряд."
    )

    raw = msg.text.replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    # lines[0] — сама команда "/create_teams", остальное — блоки
    body = lines[1:]

    # Разбиваем на блоки по строкам-разделителям "---"
    raw_blocks: list[list[str]] = []
    current: list[str] = []
    for line in body:
        if line.strip() == "---":
            raw_blocks.append(current)
            current = []
        else:
            current.append(line)
    raw_blocks.append(current)
    # Отбрасываем полностью пустые блоки (например, лишний --- в конце)
    raw_blocks = [b for b in raw_blocks if any(l.strip() for l in b)]

    if not raw_blocks:
        await msg.answer(USAGE)
        return

    parsed_teams: list[dict] = []
    all_errors: list[str] = []
    seen_names_lower: set[str] = set()

    line_offset = 1  # первая строка сообщения — команда /create_teams
    for block in raw_blocks:
        result, errors = parse_team_block(block, line_offset=line_offset)
        line_offset += len(block) + 1  # +1 за строку-разделитель
        if errors:
            all_errors.extend(errors)
            continue
        name_lower = result["name"].lower()
        if name_lower in seen_names_lower:
            all_errors.append(f"«{md_escape(result['name'])}»: название повторяется дважды в этом сообщении.")
            continue
        seen_names_lower.add(name_lower)
        parsed_teams.append(result)

    # Проверяем, что ни одна из команд ещё не существует в базе
    for team in parsed_teams:
        if await db_get_team(team["name"]):
            all_errors.append(f"«{md_escape(team['name'])}»: команда с таким названием уже существует.")

    if all_errors:
        err_text = "❌ <b>Не удалось создать команды — исправьте ошибки и отправьте заново:</b>\n\n" + "\n".join(
            f"• {e}" for e in all_errors
        )
        await safe_send_long(msg, err_text)
        return

    created: list[dict] = []
    failed: list[str] = []
    for team in parsed_teams:
        try:
            await db_create_team(
                team["name"], team["main_players"], team["reserve_players"],
                team["initial_vrs"], coach=team["coach_player"]
            )
            created.append(team)
        except Exception as e:
            failed.append(f"«{md_escape(team['name'])}»: {md_escape(str(e))}")

    lines_out = [f"✅ <b>Создано команд: {len(created)}/{len(parsed_teams)}</b>\n"]
    for team in created:
        extra = []
        if team["reserve_players"]:
            extra.append(f"рез. {len(team['reserve_players'])}")
        if team["coach_player"]:
            extra.append("тренер")
        extra_text = f" ({', '.join(extra)})" if extra else ""
        lines_out.append(
            f"  🏟 <b>{md_escape(team['name'])}</b> — 5 игроков{extra_text}, VRS {team['initial_vrs']}"
        )
    if failed:
        lines_out.append("\n❌ <b>Ошибки при создании:</b>")
        lines_out.extend(f"  • {f}" for f in failed)

    await safe_send_long(msg, "\n".join(lines_out))


@dp.message(Command("create_team"))
async def cmd_create_team(msg: Message) -> None:
    """
    /create_team НазваниеКоманды
    VRS 150
    Ник Рейтинг Роль Возраст
    ...
    Резерв
    Ник Рейтинг Роль Возраст

    Каждый игрок — отдельная строка: Ник Рейтинг Роль Возраст
    Роли: Rifler, AWPer, Support, Entry Fragger, Lurker, IGL
    Рейтинг: 15.0–32.0 | Возраст: 14–40
    Основной состав: ровно 5 игроков | Резерв: 0–2 игрока
    VRS: отдельная строка после названия — «VRS 150»
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    USAGE = (
        "❌ Формат:\n"
        "<code>/create_team НазваниеКоманды\n"
        "VRS 150\n"
        "Ник Рейтинг Роль Возраст\n"
        "Ник Рейтинг Роль Возраст\n"
        "Ник Рейтинг Роль Возраст\n"
        "Ник Рейтинг Роль Возраст\n"
        "Ник Рейтинг Роль Возраст\n"
        "Резерв\n"
        "Ник Рейтинг Роль Возраст\n"
        "Тренер\n"
        "Ник Рейтинг Coach Возраст</code>\n\n"
        "• <b>VRS [число]</b> — вторая строка, начальные очки VRS (целое число ≥ 0)\n"
        "• Ровно 5 основных игроков\n"
        "• Слово <b>Резерв</b> — разделитель (необязательно), после него 0–2 игрока\n"
        "• Слово <b>Тренер</b> и сам тренер — <b>полностью необязательны</b>: "
        "если блок «Тренер» не указан, команда создаётся без тренера, ошибки не будет\n"
        "• Роли: <code>Rifler</code>, <code>AWPer</code>, <code>Support</code>, "
        "<code>Entry Fragger</code>, <code>Lurker</code>, <code>IGL</code>, <code>Coach</code>\n"
        "• Рейтинг: 15.0–32.0 | Возраст: 14–40\n\n"
        "Пример без тренера:\n"
        "<code>/create_team NaVi\n"
        "VRS 150\n"
        "s1mple 20.5 AWPer 26\n"
        "electroNic 19.0 Rifler 25\n"
        "b1t 18.5 Entry Fragger 22\n"
        "Perfecto 17.5 Support 26\n"
        "navi_igl 18.0 IGL 28</code>\n\n"
        "Пример с тренером и резервом:\n"
        "<code>/create_team NaVi\n"
        "VRS 150\n"
        "s1mple 20.5 AWPer 26\n"
        "electroNic 19.0 Rifler 25\n"
        "b1t 18.5 Entry Fragger 22\n"
        "Perfecto 17.5 Support 26\n"
        "navi_igl 18.0 IGL 28\n"
        "Резерв\n"
        "young1 16.0 Rifler 18\n"
        "Тренер\n"
        "coach_pro 19.0 Coach 35</code>"
    )

    lines = msg.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) < 3:
        await msg.answer(USAGE)
        return

    # Первая строка: /create_team НазваниеКоманды
    first_parts = lines[0].split(maxsplit=1)
    if len(first_parts) < 2 or not first_parts[1].strip():
        await msg.answer(USAGE)
        return
    name = first_parts[1].strip()

    # Вторая строка: VRS 150
    second_line = lines[1].strip()
    second_parts = second_line.split()
    if len(second_parts) != 2 or second_parts[0].upper() != "VRS":
        await msg.answer(
            "❌ Вторая строка должна содержать VRS.\n"
            "Формат: <code>VRS 150</code>\n\n" + USAGE
        )
        return
    try:
        initial_vrs = int(second_parts[1])
        if initial_vrs < 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ VRS должен быть целым числом ≥ 0.\nПример: <code>VRS 150</code>")
        return

    # Разбираем остальные строки (с 3-й)
    main_players: list[dict] = []
    reserve_players: list[dict] = []
    coach_player: dict | None = None
    in_reserve = False
    in_coach = False
    parse_errors: list[str] = []

    valid_roles_lower = {r.lower(): r for r in ROLES}

    def parse_player_line(line: str, line_num: int) -> dict | None:
        """Парсит строку 'Ник Рейтинг Роль Возраст'. Роль может быть двухсловной."""
        tokens = line.split()
        if len(tokens) < 4:
            parse_errors.append(f"Строка {line_num}: «{line}» — нужно минимум 4 токена (Ник Рейтинг Роль Возраст)")
            return None
        nick = tokens[0]
        # Рейтинг — второй токен
        try:
            rating = float(tokens[1])
            if not (15.0 <= rating <= 32.0):
                raise ValueError
        except ValueError:
            parse_errors.append(f"Строка {line_num}: рейтинг «{tokens[1]}» должен быть числом от 15.0 до 32.0")
            return None
        # Возраст — последний токен
        try:
            age = int(tokens[-1])
            if not (14 <= age <= 40):
                raise ValueError
        except ValueError:
            parse_errors.append(f"Строка {line_num}: возраст «{tokens[-1]}» должен быть целым числом от 14 до 40")
            return None
        # Роль — всё между рейтингом и возрастом
        role_raw = " ".join(tokens[2:-1])
        role = valid_roles_lower.get(role_raw.lower())
        if role is None:
            parse_errors.append(
                f"Строка {line_num}: роль «{role_raw}» не распознана. "
                f"Доступные: {', '.join(ROLES)}"
            )
            return None
        return {"nick": nick, "rating": rating, "role": role, "age": age}

    for i, raw_line in enumerate(lines[2:], start=3):
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == "резерв":
            in_reserve = True
            in_coach = False
            continue
        if line.lower() == "тренер":
            in_coach = True
            in_reserve = False
            continue
        player = parse_player_line(line, i)
        if player:
            if in_coach:
                if player["role"] != "Coach":
                    parse_errors.append(f"Строка {i}: тренер должен иметь роль Coach")
                elif coach_player is not None:
                    parse_errors.append(f"Строка {i}: можно указать только одного тренера")
                else:
                    coach_player = player
            elif in_reserve:
                reserve_players.append(player)
            else:
                main_players.append(player)

    if parse_errors:
        await msg.answer("❌ Ошибки при разборе игроков:\n" + "\n".join(parse_errors))
        return

    if len(main_players) != 5:
        await msg.answer(f"❌ Основной состав должен содержать ровно 5 игроков (сейчас: {len(main_players)}).\n\n{USAGE}")
        return

    if len(reserve_players) > 2:
        await msg.answer("❌ В резерве не более 2 игроков.")
        return

    if await db_get_team(name):
        await msg.answer(f"❌ Команда «{md_escape(str(name))}» уже существует.")
        return

    try:
        await db_create_team(name, main_players, reserve_players, initial_vrs, coach=coach_player)
    except Exception as e:
        await msg.answer(f"❌ Ошибка при создании команды: <code>{md_escape(str(e))}</code>")
        return

    main_lines = []
    for p in main_players:
        main_lines.append(
            f"  • <b>{md_escape(p['nick'])}</b> [{md_escape(p['role'])}] "
            f"рейтинг {p['rating']:.1f}, {p['age']} лет"
        )

    res_text = ""
    if reserve_players:
        res_lines = [
            f"  • <b>{md_escape(p['nick'])}</b> [{md_escape(p['role'])}] "
            f"рейтинг {p['rating']:.1f}, {p['age']} лет"
            for p in reserve_players
        ]
        res_text = "\n\n🪑 <b>Резерв:</b>\n" + "\n".join(res_lines)

    coach_text = ""
    if coach_player:
        coach_text = (
            f"\n\n🎓 <b>Тренер:</b>\n"
            f"  • <b>{md_escape(coach_player['nick'])}</b> "
            f"рейтинг {coach_player['rating']:.1f}, {coach_player['age']} лет"
        )

    await msg.answer(
        f"✅ Команда <b>{md_escape(str(name))}</b> создана!\n\n"
        f"👥 <b>Основной состав:</b>\n" + "\n".join(main_lines) +
        res_text + coach_text + "\n\n"
        "💠 Дух команды: 100%  [Синее пламя]\n"
        "⚗️ Сыгранность: 50/50\n"
        f"🏅 VRS: <b>{initial_vrs} очков</b>"
    )


@dp.message(Command("swap"))
async def cmd_swap(msg: Message) -> None:
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) != 5:
        await msg.answer(
            "❌ Формат:\n"
            "<code>/swap Игрок1 Команда1 Игрок2 Команда2</code>\n\n"
            "Пример: <code>/swap SnipeKing TeamAlpha FragMaster TeamBeta</code>\n"
            "• Оба игрока меняются командами\n"
            "• Сыгранность обеих команд −3"
        )
        return

    nick_a = parts[1]
    team_a = parts[2]
    nick_b = parts[3]
    team_b = parts[4]

    if team_a == team_b:
        await msg.answer("❌ Игроки должны быть из разных команд.")
        return

    if not await db_get_team(team_a):
        await msg.answer(f"❌ Команда «{md_escape(str(team_a))}» не найдена.")
        return
    if not await db_get_team(team_b):
        await msg.answer(f"❌ Команда «{md_escape(str(team_b))}» не найдена.")
        return

    ok, err = await db_swap_players(nick_a, team_a, nick_b, team_b)
    if not ok:
        await msg.answer(f"❌ {md_escape(str(err))}")
        return

    await msg.answer(
        f"🔀 <b>Обмен игроками завершён!</b>\n\n"
        f"<b>{md_escape(str(nick_a))}</b> перешёл в <b>{md_escape(str(team_b))}</b>\n"
        f"<b>{md_escape(str(nick_b))}</b> перешёл в <b>{md_escape(str(team_a))}</b>\n\n"
        f"📉 Сыгранность обеих команд −3\n"
        f"🔻 Дух обеих команд −25%"
    )


@dp.message(Command("transfer"))
async def cmd_transfer(msg: Message) -> None:
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) < 4:
        await msg.answer(
            "❌ Формат:\n"
            "<code>/transfer Игрок Откуда Куда</code> — обычный трансфер (−5 сыгранности)\n"
            "<code>/transfer Игрок Откуда Куда аренда НазваниеТурнира</code> — аренда до конца турнира (−2 сыгранности)"
        )
        return

    nick   = parts[1]
    from_t = parts[2]
    to_t   = parts[3]
    loan   = len(parts) >= 5 and parts[4].lower() == "аренда"
    until_tour = " ".join(parts[5:]) if loan and len(parts) >= 6 else None

    if loan and not until_tour:
        await msg.answer(
            "❌ Для аренды нужно указать турнир:\n"
            "<code>/transfer Игрок Откуда Куда аренда НазваниеТурнира</code>"
        )
        return

    if not await db_get_team(from_t):
        await msg.answer(f"❌ Команда «{md_escape(str(from_t))}» не найдена.")
        return
    if not await db_get_team(to_t):
        await msg.answer(f"❌ Команда «{md_escape(str(to_t))}» не найдена.")
        return

    if loan:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, is_done FROM tournament_registry WHERE name=?", (until_tour,)
            ) as cursor:
                tour_row = await cursor.fetchone()
        if not tour_row:
            await msg.answer(
                f"❌ Турнир «{md_escape(str(until_tour))}» не найден в реестре.\n"
                f"Сначала добавь турнир через <code>/addtour {md_escape(str(until_tour))}</code>"
            )
            return
        if tour_row[1] == 1:
            await msg.answer(f"❌ Турнир «{md_escape(str(until_tour))}» уже завершён — нельзя арендовать до него.")
            return

    ok = await db_transfer_player(nick, from_t, to_t, loan=loan)
    if not ok:
        await msg.answer(f"❌ Игрок «{md_escape(str(nick))}» не найден в команде «{md_escape(str(from_t))}».")
        return

    if loan:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO loans (nick, from_team, to_team, until_tour, returned, loaned_at) VALUES (?,?,?,?,0,?)",
                (nick, from_t, to_t, until_tour, datetime.now().isoformat(timespec="seconds"))
            )
            await db.commit()

    chem_loss = 2 if loan else 5
    transfer_type = "Аренда" if loan else "Трансфер"
    msg_text = (
        f"🔄 <b>{transfer_type}</b>: <b>{md_escape(str(nick))}</b>  <b>{md_escape(str(from_t))}</b> → <b>{md_escape(str(to_t))}</b>\n"
        f"📉 Сыгранность обеих команд −{chem_loss}"
    )
    if loan:
        msg_text += f"\n📅 Действует до конца турнира: <b>{md_escape(str(until_tour))}</b>\n"
        msg_text += f"ℹ️ После завершения тура игрок вернётся в <b>{md_escape(str(from_t))}</b>"
    await msg.answer(msg_text)


@dp.message(Command("vrs_info"))
async def cmd_vrs_info(msg: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name, points, wins, draws, losses FROM vrs ORDER BY points DESC"
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await msg.answer("📋 VRS таблица пуста — пока не сыграно ни одного турнирного матча.")
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    lines = ["🏅 <b>VRS — Рейтинговая таблица</b>\n"]
    lines.append(f"{'#':<3} {'Команда':<18} {'Очки':>6}   {'В':>3} {'Н':>3} {'П':>3}")
    lines.append("─" * 42)

    for i, (team_name, points, wins, draws, losses) in enumerate(rows, 1):
        prefix = medals.get(i, f"{i}.")
        lines.append(
            f"{prefix:<3} {md_escape(str(team_name)):<18} {points:>7.1f}   {wins:>3} {draws:>3} {losses:>3}"
        )

    lines.append("")
    lines.append("📌 <i>В — победы · Н — ничьи · П — поражения</i>")

    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/stats НазваниеКоманды</code>")
        return
    name = parts[1].strip()
    team = await db_get_team(name)
    if not team:
        await msg.answer(f"❌ Команда «{md_escape(str(name))}» не найдена.")
        return

    _, tname, spirit, chem, _winrate_boost = team
    players = await db_get_players(tname)
    status_label, status_emoji = spirit_status(spirit)

    header = (
        f"📊 <b>{md_escape(tname)}</b>\n"
        f"{status_emoji} Дух: {spirit:.0f}%  [{spirit_bar(spirit)}]  {status_label}\n"
        f"⚗️ Сыгранность: {chem:.0f}/50\n\n"
        f"{'Игрок':<16} {'Рейтинг':>8} {'Роль':<10}\n"
        f"{'─' * 38}\n"
    )
    rows = ""
    for nick, role, rating, is_reserve, age in players:
        if is_reserve == 0:
            reserve_mark = ""
        elif is_reserve == 1:
            reserve_mark = " (рез.)"
        else:
            reserve_mark = " (тренер)"
        rows += f"{md_escape(str(nick)):<16} {rating:>6.1f} {md_escape(str(role)):<10}{reserve_mark}\n"

    await msg.answer(f"{header}<code>{md_escape(rows)}</code>")


async def _build_team_info_text(name: str) -> tuple[str, str] | None:
    """Строит текст карточки команды. Возвращает (текст, реальное_имя_команды) или None, если не найдена."""
    team = await db_get_team(name)
    if not team:
        return None

    _, tname, spirit, chem, _winrate_boost = team
    players     = await db_get_players(tname)
    spirit_hist = await db_get_spirit_history(tname, 3)

    # Получаем место в VRS рейтинге
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name, points FROM vrs ORDER BY points DESC"
        ) as cursor:
            vrs_rows = await cursor.fetchall()

    vrs_place = None
    vrs_points = None
    for i, (vname, vpoints) in enumerate(vrs_rows, 1):
        if vname == tname:
            vrs_place = i
            vrs_points = vpoints
            break

    main_roster    = [(n, ro, ra, ag) for n, ro, ra, ir, ag in players if ir == 0]
    reserve_roster = [(n, ro, ra, ag) for n, ro, ra, ir, ag in players if ir == 1]

    role_icons = {
        "Rifler":        "🔫",
        "AWPer":         "🎯",
        "Support":       "🛡",
        "Entry Fragger": "⚡",
        "Lurker":        "🕵",
        "IGL":           "📢",
        "Coach":         "🎓",
    }

    lines: list[str] = []

    lines.append(f"<b>{md_escape(str(tname))}</b>")
    lines.append("")

    status_label, status_emoji = spirit_status(spirit)
    lines.append(f"{status_emoji} <b>Дух команды: {spirit:.0f}%</b>  —  {status_label}")
    lines.append(f"[{spirit_bar(spirit)}]")
    if spirit_hist:
        lines.append("📋 История духа:")
        for ch, reason, ts in spirit_hist:
            sign = "+" if ch >= 0 else ""
            lines.append(f"  {sign}{ch:.0f}%  {md_escape(str(reason))}")
    lines.append("")

    lines.append("")

    lines.append("Состав:")
    for nick, role, rating, age in main_roster:
        icon = role_icons.get(role, "•")
        age_str = f", {age} лет" if age is not None else ""
        lines.append(f"{icon} {md_escape(str(nick))} {rating:.1f} [{md_escape(str(role))}{age_str}]")

    if reserve_roster:
        lines.append("Резерв")
        for nick, role, rating, age in reserve_roster:
            icon = role_icons.get(role, "•")
            age_str = f", {age} лет" if age is not None else ""
            lines.append(f"{icon} {md_escape(str(nick))} {rating:.1f} [{md_escape(str(role))}{age_str}]")

    coach_roster = [(n, ro, ra, ag) for n, ro, ra, ir, ag in players if ir == 2]
    if coach_roster:
        lines.append("Тренер")
        for nick, role, rating, age in coach_roster:
            age_str = f", {age} лет" if age is not None else ""
            lines.append(f"🎓 {md_escape(str(nick))} {rating:.1f} [Coach{age_str}]")

    lines.append("")
    # Средний рейтинг основного состава (ровно 5 игроков)
    if main_roster:
        avg_rating = sum(ra for _, _, ra, _ in main_roster) / len(main_roster)
        lines.append(f"Средний рейтинг команды: <b>{avg_rating:.1f}</b>")

    # Средний потенциал команды (по возрасту основного состава)
    if main_roster:
        pot_gain, pot_stars = _team_avg_potential(main_roster)
        ages_main = [ag for _, _, _, ag in main_roster if ag is not None]
        avg_age = sum(ages_main) / len(ages_main) if ages_main else 0.0
        if pot_gain > 0.0:
            pot_desc = f"+{pot_gain:.1f} за тренировку"
        else:
            pot_desc = "не качается"
        lines.append(
            f"Средний потенциал: {pot_stars}  "
            f"<i>(ср. возраст {avg_age:.1f} лет — {pot_desc})</i>"
        )

    lines.append(f"Сыгранность: {int(chem)}")
    if _winrate_boost:
        sign = "+" if _winrate_boost >= 0 else ""
        lines.append(f"Бонус винрейта (магазин): {sign}{_winrate_boost:.1f}%")

    map_stats = await db_get_team_map_winrates(tname)
    lines.append("")
    lines.append(fmt_map_winrate_block(map_stats))

    balance = await db_get_team_balance(tname)
    if balance is not None:
        lines.append(f"💰 Баланс: <b>{fmt_money(balance)}</b>")

    if vrs_place is not None:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        place_icon = medals.get(vrs_place, "🏅")
        lines.append(f"VRS рейтинг: {place_icon} #{vrs_place} место  ({vrs_points} очков)")
    else:
        lines.append("VRS рейтинг: — (нет данных)")

    # Показываем призовые места на турнирах (1–3 место)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tour_name, place FROM team_tournament_places WHERE team_name=? ORDER BY place ASC, id DESC",
            (tname,)
        ) as cursor:
            tour_places = await cursor.fetchall()

    if tour_places:
        place_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines.append("")
        lines.append("🏆 <b>Призовые места на турнирах:</b>")
        for t_name, t_place in tour_places:
            medal = place_medals.get(t_place, "🏅")
            lines.append(f"  {medal} {t_place} место — {md_escape(str(t_name))}")

    return "\n".join(lines), tname


def _kb_team_info(team_id: int | None, is_leader: bool) -> InlineKeyboardMarkup | None:
    if team_id is None:
        return None
    rows = [[InlineKeyboardButton(text="🔄 Обновить", callback_data=f"ti|show|{team_id}")]]
    if is_leader:
        rows.append([InlineKeyboardButton(text="⚙️ Панель управления", callback_data=f"lp|roster|{team_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _answer_team_card(msg: Message, team_name: str) -> None:
    """Строит и отправляет карточку команды (с разбиением на части и кнопками под последней)."""
    built = await _build_team_info_text(team_name)
    if not built:
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    full_text, tname = built
    team_id = await db_get_team_id_by_name(tname)
    is_leader = await db_is_team_leader(msg.from_user.id, tname)
    kb = _kb_team_info(team_id, is_leader)

    chunks = _split_by_bytes(full_text)
    for chunk in chunks[:-1]:
        await msg.answer(_close_open_tags(chunk))
    await msg.answer(_close_open_tags(chunks[-1]), reply_markup=kb)


@dp.message(Command("team_info"))
async def cmd_team_info(msg: Message) -> None:
    """
    /team_info [Название] — карточка команды (дух, состав, карты).
    Без аргумента: если ты лидер одной команды — покажет сразу её,
    если лидер нескольких — предложит выбрать кнопками.
    """
    parts = msg.text.split(maxsplit=1)
    explicit_team = parts[1].strip() if len(parts) == 2 else None

    if explicit_team:
        await _answer_team_card(msg, explicit_team)
        return

    leader_teams = await db_get_leader_teams(msg.from_user.id)
    if len(leader_teams) == 1:
        await _answer_team_card(msg, leader_teams[0])
    elif len(leader_teams) > 1:
        rows = []
        for t in leader_teams:
            tid = await db_get_team_id_by_name(t)
            rows.append([InlineKeyboardButton(text=t, callback_data=f"ti|show|{tid}")])
        await msg.answer(
            "У тебя несколько команд. Выбери, какую показать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    else:
        await msg.answer(
            "❌ Формат: <code>/team_info НазваниеКоманды</code>\n\n"
            "ℹ️ Если ты лидер команды, можно написать просто <code>/team_info</code> "
            "без аргументов — бот сразу покажет твою команду."
        )


@dp.callback_query(F.data.startswith("ti|"))
async def cb_team_info(cq: CallbackQuery) -> None:
    data = cq.data.split("|")
    action = data[1]

    if action == "show":
        team_id = int(data[2])
        team_name = await db_get_team_name_by_id(team_id)
        built = await _build_team_info_text(team_name) if team_name else None
        if not built:
            await cq.answer("❌ Команда не найдена.", show_alert=True)
            return

        full_text, tname = built
        is_leader = await db_is_team_leader(cq.from_user.id, tname)
        kb = _kb_team_info(team_id, is_leader)
        chunks = _split_by_bytes(full_text)

        if len(chunks) == 1:
            try:
                await cq.message.edit_text(_close_open_tags(chunks[0]), reply_markup=kb)
                await cq.answer()
                return
            except TelegramBadRequest:
                pass  # например, "message is not modified" — просто отправим заново

        for chunk in chunks[:-1]:
            await cq.message.answer(_close_open_tags(chunk))
        await cq.message.answer(_close_open_tags(chunks[-1]), reply_markup=kb)
        try:
            await cq.message.delete()
        except Exception:
            pass
        await cq.answer()
        return


@dp.message(Command("addtourplace"))
async def cmd_addtourplace(msg: Message) -> None:
    """
    /addtourplace [Команда] [Турнир] [Место 1-3]
    Добавляет призовое место команды на турнире (только 1, 2 или 3).
    Только для администраторов.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Только администратор может добавлять места на турнирах.")
        return

    # Формат: /addtourplace Команда Турнир Место
    # Место — последний аргумент (1-3), команда — первый, турнир — всё между ними
    raw_parts = msg.text.split()
    USAGE = (
        "❌ Формат: <code>/addtourplace [Команда] [Турнир] [Место]</code>\n\n"
        "Место должно быть от 1 до 3.\n\n"
        "Примеры:\n"
        "<code>/addtourplace NaVi WINLINE_Major 1</code>\n"
        "<code>/addtourplace Virtus.pro ESL_Pro_League 2</code>"
    )
    if len(raw_parts) < 4:
        await msg.answer(USAGE)
        return

    place_raw = raw_parts[-1]
    if not place_raw.isdigit() or int(place_raw) not in (1, 2, 3):
        await msg.answer("❌ Место должно быть числом от <b>1</b> до <b>3</b>.\n\n" + USAGE)
        return

    team_name = raw_parts[1]
    tour_name = " ".join(raw_parts[2:-1])
    place = int(place_raw)

    # Проверяем существование команды
    team = await db_get_team(team_name)
    if not team:
        await msg.answer(f"❌ Команда «{md_escape(team_name)}» не найдена.")
        return

    place_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    medal = place_medals[place]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO team_tournament_places (team_name, tour_name, place, added_at)
               VALUES (?,?,?,?)
               ON CONFLICT(team_name, tour_name) DO UPDATE SET place=excluded.place, added_at=excluded.added_at""",
            (team_name, tour_name, place, datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()

    await msg.answer(
        f"✅ Записано!\n\n"
        f"{medal} Команда <b>{md_escape(team_name)}</b> заняла <b>{place} место</b> "
        f"на турнире <b>{md_escape(tour_name)}</b>.\n\n"
        f"Теперь место отображается в <code>/team_info {md_escape(team_name)}</code>."
    )


# ── Призовые за турниры ──────────────────────────────────────────────────

TOURNAMENT_PRIZES = {
    "S": {1: 100_000, 2: 50_000, 3: 25_000},
    "A": {1: 50_000,  2: 25_000, 3: 10_000},
    "B": {1: 20_000,  2: 10_000, 3: 5_000},
}


async def db_get_tournament_tier(tour_name: str) -> str:
    """Тир турнира из tournament_registry (S/A/B). По умолчанию — B."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tier FROM tournament_registry WHERE name=? COLLATE NOCASE", (tour_name,)
        ) as cursor:
            row = await cursor.fetchone()
    tier = (row[0].upper() if row and row[0] else "B")
    return tier if tier in TOURNAMENT_PRIZES else "B"


# user_id -> {"step": "team"|"amount", "team": Optional[str]}
balance_give_pending: dict[int, dict] = {}


def _parse_balance_amount(token: str):
    """
    Парсит сумму. Понимает как '50000' / '50000.5' / '50000,5',
    так и '1.500.000' (точки как разделители тысяч, без дробной части).
    Возвращает float или None, если не удалось распознать.
    """
    t = token.strip().replace(" ", "")
    if not t:
        return None
    # Число вида 1.500.000 / 400.000 — точки как разделители тысяч
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", t):
        try:
            return float(t.replace(".", ""))
        except ValueError:
            return None
    try:
        return float(t.replace(",", "."))
    except ValueError:
        return None


def _parse_bulk_balance_entries(text: str):
    """
    Парсит строки вида 'Команда Сумма, Команда Сумма, ...'.
    Возвращает (entries, errors), где entries — список (team_name, amount).
    """
    entries: list[tuple[str, float]] = []
    errors: list[str] = []
    for raw_entry in text.split(","):
        raw_entry = raw_entry.strip()
        if not raw_entry:
            continue
        parts = raw_entry.split()
        if len(parts) < 2:
            errors.append(f"«{raw_entry}» — не указана сумма")
            continue
        amount_raw = parts[-1]
        team_name = " ".join(parts[:-1])
        amount = _parse_balance_amount(amount_raw)
        if amount is None:
            errors.append(f"«{raw_entry}» — сумма «{amount_raw}» не распознана")
            continue
        if amount <= 0:
            errors.append(f"«{raw_entry}» — сумма должна быть больше нуля")
            continue
        entries.append((team_name, amount))
    return entries, errors


def _has_pending_balance_give(message: Message) -> bool:
    """Ловит обычные текстовые сообщения от админа, который сейчас в процессе /balance_give."""
    if not message.from_user or message.from_user.id not in balance_give_pending:
        return False
    text = (message.text or "").strip()
    if text.startswith("/"):
        # Другая команда — прерываем незавершённый /balance_give и пропускаем дальше.
        balance_give_pending.pop(message.from_user.id, None)
        return False
    return True


@dp.message(Command("balance_give"))
async def cmd_balance_give(msg: Message) -> None:
    """
    /balance_give [Команда] [Сумма]
    Просто начисляет команде деньги на баланс (без привязки к турнирам).
    Можно также вызвать без аргументов (или только с командой) — бот сам
    спросит недостающее по шагам. Только для администраторов.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Только администратор может выдавать деньги командам.")
        return

    full_text = msg.text or ""
    USAGE = (
        "❌ Формат: <code>/balance_give [Команда] [Сумма]</code>\n\n"
        "Пример: <code>/balance_give NaVi 50000</code>\n\n"
        "Несколько команд сразу — через запятую:\n"
        "<code>/balance_give NaVi 50000, Virtus.Pro 60.000, Team2 100.000</code>\n\n"
        "Или просто отправьте <code>/balance_give</code> без аргументов — "
        "я сам спрошу команду и сумму по очереди."
    )

    # Массовое начисление: "/balance_give Команда Сумма, Команда Сумма, ..."
    _, _, rest_text = full_text.partition(" ")
    rest_text = rest_text.strip()
    if "," in rest_text:
        entries, errors = _parse_bulk_balance_entries(rest_text)
        if errors:
            err_list = "\n".join(f"• {e}" for e in errors)
            await msg.answer(
                "❌ Не удалось разобрать часть списка. Проверьте и отправьте команду заново:\n\n"
                f"{err_list}"
            )
            return
        if not entries:
            await msg.answer(USAGE)
            return

        balance_give_pending.pop(msg.from_user.id, None)

        ok_lines = []
        fail_lines = []
        for team_name, amount in entries:
            ok, err, new_balance = await db_add_balance(
                team_name, amount, "Начисление от организатора", msg.from_user.id
            )
            if ok:
                ok_lines.append(
                    f"✅ <b>{md_escape(team_name)}</b>: +{fmt_money(amount)} → {fmt_money(new_balance)}"
                )
            else:
                fail_lines.append(f"❌ <b>{md_escape(team_name)}</b>: {err}")

        reply_parts = []
        if ok_lines:
            reply_parts.append("💰 Начислено:\n" + "\n".join(ok_lines))
        if fail_lines:
            reply_parts.append("Не удалось начислить:\n" + "\n".join(fail_lines))
        await msg.answer("\n\n".join(reply_parts))
        return

    raw_parts = full_text.split()

    # /balance_give — без аргументов: запускаем пошаговый режим
    if len(raw_parts) == 1:
        balance_give_pending[msg.from_user.id] = {"step": "team", "team": None}
        await msg.answer(
            "💰 Начисление денег команде.\n\nВведите <b>название команды</b> "
            "(или напишите «отмена», чтобы отменить):"
        )
        return

    # /balance_give Команда — сумма не указана: спрашиваем только сумму
    if len(raw_parts) == 2:
        balance_give_pending[msg.from_user.id] = {"step": "amount", "team": raw_parts[1]}
        await msg.answer(
            f"Команда: <b>{md_escape(raw_parts[1])}</b>\n\nТеперь введите <b>сумму</b> начисления "
            "(или напишите «отмена»):"
        )
        return

    # /balance_give Команда Сумма — как раньше, всё одной командой
    amount_raw = raw_parts[-1]
    team_name = " ".join(raw_parts[1:-1])

    try:
        amount = float(amount_raw.replace(",", ".").replace(" ", ""))
    except ValueError:
        await msg.answer("❌ Сумма должна быть числом.\n\n" + USAGE)
        return
    if amount <= 0:
        await msg.answer("❌ Сумма должна быть больше нуля.")
        return

    balance_give_pending.pop(msg.from_user.id, None)

    ok, err, new_balance = await db_add_balance(
        team_name, amount, "Начисление от организатора", msg.from_user.id
    )
    if not ok:
        await msg.answer(f"❌ {err}")
        return

    await msg.answer(
        f"💰 Команде <b>{md_escape(team_name)}</b> начислено <b>{fmt_money(amount)}</b>.\n"
        f"Новый баланс: <b>{fmt_money(new_balance)}</b>"
    )


@dp.message(_has_pending_balance_give)
async def handle_balance_give_flow(msg: Message) -> None:
    """Обрабатывает шаги пошагового /balance_give (команда → сумма)."""
    user_id = msg.from_user.id
    state = balance_give_pending.get(user_id)
    if not state:
        return

    text = (msg.text or "").strip()

    if text.lower() in ("отмена", "cancel", "стоп"):
        balance_give_pending.pop(user_id, None)
        await msg.answer("❌ Начисление отменено.")
        return

    # На всякий случай перепроверяем права — вдруг их сняли по ходу диалога.
    if not await db_is_admin(user_id):
        balance_give_pending.pop(user_id, None)
        await msg.answer("⛔ Только администратор может выдавать деньги командам.")
        return

    if state["step"] == "team":
        if not text:
            await msg.answer("❌ Название команды не может быть пустым. Введите ещё раз:")
            return
        state["team"] = text
        state["step"] = "amount"
        await msg.answer(
            f"Команда: <b>{md_escape(text)}</b>\n\nТеперь введите <b>сумму</b> начисления "
            "(или напишите «отмена»):"
        )
        return

    if state["step"] == "amount":
        try:
            amount = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            await msg.answer("❌ Сумма должна быть числом. Введите сумму ещё раз (или «отмена»):")
            return
        if amount <= 0:
            await msg.answer("❌ Сумма должна быть больше нуля. Введите сумму ещё раз:")
            return

        team_name = state["team"]
        balance_give_pending.pop(user_id, None)

        ok, err, new_balance = await db_add_balance(
            team_name, amount, "Начисление от организатора", user_id
        )
        if not ok:
            await msg.answer(f"❌ {err}")
            return

        await msg.answer(
            f"💰 Команде <b>{md_escape(team_name)}</b> начислено <b>{fmt_money(amount)}</b>.\n"
            f"Новый баланс: <b>{fmt_money(new_balance)}</b>"
        )


@dp.message(Command("tourprize"))
async def cmd_tourprize(msg: Message) -> None:
    """
    /tourprize [Команда] [Турнир] [Место 1-3]
    Начисляет команде призовые деньги за место на турнире (сумма зависит от
    тира турнира S/A/B и занятого места) и заодно записывает это место —
    как в /addtourplace. Только для администраторов.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Только администратор может начислять призовые за турниры.")
        return

    raw_parts = msg.text.split()
    USAGE = (
        "❌ Формат: <code>/tourprize [Команда] [Турнир] [Место]</code>\n\n"
        "Место должно быть от 1 до 3. Сумма приза считается от общего "
        "призового фонда турнира (50% / 30% / 20% за 1/2/3 место) — "
        "см. <code>/tours</code>. Если турнир добавлен вручную и фонд для "
        "него не задан — используется запасная тир-таблица (S/A/B):\n\n"
        "  🏆 S-тир: 🥇 " + fmt_money(TOURNAMENT_PRIZES["S"][1]) +
        " · 🥈 " + fmt_money(TOURNAMENT_PRIZES["S"][2]) +
        " · 🥉 " + fmt_money(TOURNAMENT_PRIZES["S"][3]) + "\n"
        "  🏆 A-тир: 🥇 " + fmt_money(TOURNAMENT_PRIZES["A"][1]) +
        " · 🥈 " + fmt_money(TOURNAMENT_PRIZES["A"][2]) +
        " · 🥉 " + fmt_money(TOURNAMENT_PRIZES["A"][3]) + "\n"
        "  🏆 B-тир: 🥇 " + fmt_money(TOURNAMENT_PRIZES["B"][1]) +
        " · 🥈 " + fmt_money(TOURNAMENT_PRIZES["B"][2]) +
        " · 🥉 " + fmt_money(TOURNAMENT_PRIZES["B"][3]) + "\n\n"
        "Пример:\n"
        "<code>/tourprize NaVi WINLINE_Major 1</code>"
    )
    if len(raw_parts) < 4:
        await msg.answer(USAGE)
        return

    place_raw = raw_parts[-1]
    if not place_raw.isdigit() or int(place_raw) not in (1, 2, 3):
        await msg.answer("❌ Место должно быть числом от <b>1</b> до <b>3</b>.\n\n" + USAGE)
        return

    team_name = raw_parts[1]
    tour_name = " ".join(raw_parts[2:-1])
    place = int(place_raw)

    team = await db_get_team(team_name)
    if not team:
        await msg.answer(f"❌ Команда «{md_escape(team_name)}» не найдена.")
        return

    # Сначала пробуем призовые из общего фонда турнира (TOURNAMENT_PRIZE_POOL),
    # если турнир не найден там — используем старую тир-based таблицу.
    breakdown = tournament_prize_breakdown(tour_name)
    if breakdown is not None:
        prize = breakdown[place]
        tier = await db_get_tournament_tier(tour_name)
        pool_note = f" (из общего фонда {fmt_money(tournament_prize_pool(tour_name))})"
    else:
        tier = await db_get_tournament_tier(tour_name)
        prize = TOURNAMENT_PRIZES[tier][place]
        pool_note = f" ({tier}-тир)"

    ok, err, new_balance = await db_add_balance(
        team_name, float(prize),
        f"Приз за {place} место на турнире «{tour_name}»{pool_note}",
        msg.from_user.id
    )
    if not ok:
        await msg.answer(f"❌ {err}")
        return

    # Заодно фиксируем место, как /addtourplace (если ещё не было записано)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO team_tournament_places (team_name, tour_name, place, added_at)
               VALUES (?,?,?,?)
               ON CONFLICT(team_name, tour_name) DO UPDATE SET place=excluded.place, added_at=excluded.added_at""",
            (team_name, tour_name, place, datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()

    place_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    medal = place_medals[place]

    await msg.answer(
        f"💰 <b>Призовые начислены!</b>\n\n"
        f"{medal} Команда <b>{md_escape(team_name)}</b> — <b>{place} место</b> на турнире "
        f"<b>{md_escape(tour_name)}</b>{md_escape(pool_note)}\n\n"
        f"Приз: <b>+{fmt_money(prize)}</b>\n"
        f"Новый баланс команды: <b>{fmt_money(new_balance)}</b>"
    )


@dp.message(Command("tourprizeall"))
async def cmd_tourprizeall(msg: Message) -> None:
    """
    /tourprizeall Турнир / 1 Команда / 2 Команда / 3 Команда
    Сразу выдаёт призовые призёрам турнира одним махом явными парами
    «место → команда» (порядок пар не важен, можно указать не все три
    места). Суммы берутся из общего призового фонда турнира
    (50% / 30% / 20%), как в реестре /tours. Только для администраторов.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Только администратор может начислять призовые за турниры.")
        return

    raw = msg.text.split(maxsplit=1)
    USAGE = (
        "❌ Формат: <code>/tourprizeall Турнир / 1 Команда / 2 Команда / 3 Команда</code>\n\n"
        "Части разделяются символом «/»: сначала название турнира, затем "
        "пары «место команда» (место — число 1, 2 или 3, команда — которая "
        "его заняла). Порядок пар не важен, можно указать не все три места.\n\n"
        "Пример:\n"
        "<code>/tourprizeall Winline Epic Standoff 2 Jumble Rumble S1 / 1 NaVi / 2 Spirit / 3 Aurora</code>"
    )
    if len(raw) != 2 or raw[1].count("/") < 1:
        await msg.answer(USAGE)
        return

    fields = [p.strip() for p in raw[1].split("/")]
    tour_name = fields[0]
    place_parts = fields[1:]

    if not tour_name or not place_parts:
        await msg.answer(USAGE)
        return

    place_teams: dict[int, str] = {}
    for part in place_parts:
        pm = re.match(r"^([1-3])\s+(.+)$", part)
        if not pm:
            await msg.answer(
                f"❌ Не могу разобрать «{md_escape(part)}» — нужно указывать место "
                f"(1, 2 или 3) и через пробел команду, которая его заняла.\n\n" + USAGE
            )
            return
        place = int(pm.group(1))
        team_name = pm.group(2).strip()
        if place in place_teams:
            await msg.answer(f"❌ Место {place} указано дважды.")
            return
        place_teams[place] = team_name

    breakdown = tournament_prize_breakdown(tour_name)
    if breakdown is None:
        tier = await db_get_tournament_tier(tour_name)
        breakdown = TOURNAMENT_PRIZES[tier]
        pool_note = f" ({tier}-тир)"
    else:
        pool_note = f" (из общего фонда {fmt_money(tournament_prize_pool(tour_name))})"

    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        for place, team_name in place_teams.items():
            team = await db_get_team(team_name)
            if not team:
                results.append(f"❌ Команда «{md_escape(team_name)}» не найдена — пропущена.")
                continue

            prize = breakdown[place]
            ok, err, new_balance = await db_add_balance(
                team_name, float(prize),
                f"Приз за {place} место на турнире «{tour_name}»{pool_note}",
                msg.from_user.id
            )
            if not ok:
                results.append(f"❌ {md_escape(team_name)}: {err}")
                continue

            await db.execute(
                """INSERT INTO team_tournament_places (team_name, tour_name, place, added_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(team_name, tour_name) DO UPDATE SET place=excluded.place, added_at=excluded.added_at""",
                (team_name, tour_name, place, datetime.now().isoformat(timespec="seconds"))
            )
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}[place]
            results.append(
                f"{medal} <b>{md_escape(team_name)}</b> — +{fmt_money(prize)} "
                f"(баланс: {fmt_money(new_balance)})"
            )
        await db.commit()

    lines = [
        f"💰 <b>Призовые турнира «{md_escape(tour_name)}» выданы!</b>{md_escape(pool_note)}\n",
        *results,
    ]
    await safe_send_long(msg, "\n".join(lines))


@dp.message(Command("match"))
async def cmd_match(msg: Message) -> None:

    parts = msg.text.split()
    if len(parts) < 4:
        await msg.answer(
            "❌ Формат: <code>/match [Bo1/Bo2/Bo3/Bo5] Команда1 Команда2 [Турнир | Стадия]</code>\n\n"
            "Примеры:\n"
            "<code>/match Bo3 NaVi Virtus.pro WINLINE Major</code>\n"
            "<code>/match Bo3 NaVi Virtus.pro WINLINE Major | Групповой этап</code>\n"
            "<code>/match Bo5 NaVi Virtus.pro WINLINE Major | Финал</code>"
        )
        return

    fmt    = parts[1]
    name_a = parts[2]
    name_b = parts[3]

    # Всё после команд — это «Турнир | Стадия» или просто «Турнир»
    tour_stage_raw = " ".join(parts[4:]) if len(parts) > 4 else None
    if tour_stage_raw and "|" in tour_stage_raw:
        tour_split = tour_stage_raw.split("|", 1)
        tour_name  = tour_split[0].strip()
        stage_name = tour_split[1].strip()
    else:
        tour_name  = tour_stage_raw.strip() if tour_stage_raw else ""
        stage_name = None

    if fmt not in ("Bo1", "Bo2", "Bo3", "Bo5"):
        await msg.answer("❌ Формат матча: Bo1, Bo2, Bo3 или Bo5.")
        return

    team_a = await db_get_team(name_a)
    team_b = await db_get_team(name_b)

    if not team_a:
        await msg.answer(f"❌ Команда «{md_escape(str(name_a))}» не найдена.")
        return
    if not team_b:
        await msg.answer(f"❌ Команда «{md_escape(str(name_b))}» не найдена.")
        return

    try:
        players_a = await db_get_players(name_a)
        players_b = await db_get_players(name_b)
        active_a = [p for p in players_a if p[3] == 0]
        active_b = [p for p in players_b if p[3] == 0]
        # Include coach (is_reserve=2) in sim lists for bonus calculation
        sim_players_a = active_a + [p for p in players_a if p[3] == 2]
        sim_players_b = active_b + [p for p in players_b if p[3] == 2]

        if not active_a:
            await msg.answer(f"❌ У команды «{md_escape(name_a)}» нет игроков в основном составе.")
            return
        if not active_b:
            await msg.answer(f"❌ У команды «{md_escape(name_b)}» нет игроков в основном составе.")
            return

        maps = veto_maps(fmt)

        wins_a = 0
        wins_b = 0
        needed = {"Bo1": 1, "Bo2": 2, "Bo3": 2, "Bo5": 3}[fmt]

        total_kd: dict[str, list] = {}
        for nick, role, _, _r, *_ in active_a + active_b:
            if role != "Coach":
                total_kd[nick] = [0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0]

        total_rounds_per_player: dict[str, int] = {
            nick: 0 for nick in total_kd
        }
        map_results: list[str] = []

        for map_name in maps:
            side_a = random.choice(["CT", "T"])

            sim = MapSimulator(map_name, team_a, team_b, side_a, sim_players_a, sim_players_b)

            sa, sb, logs, kd, map_rounds = sim.simulate()

            for nick, stat in kd.items():
                if nick in total_kd:
                    for i in range(13):
                        total_kd[nick][i] += stat[i]
                    total_rounds_per_player[nick] = total_rounds_per_player.get(nick, 0) + map_rounds

            if sa > sb:
                wins_a += 1
                map_results.append(f"🗺 <b>{md_escape(map_name)}</b>: <b>{md_escape(name_a)}</b> {sa}–{sb} {md_escape(name_b)}")
                await db_update_map_stats(name_a, map_name, won=True)
                await db_update_map_stats(name_b, map_name, won=False)
            else:
                wins_b += 1
                map_results.append(f"🗺 <b>{md_escape(map_name)}</b>: <b>{md_escape(name_b)}</b> {sb}–{sa} {md_escape(name_a)}")
                await db_update_map_stats(name_b, map_name, won=True)
                await db_update_map_stats(name_a, map_name, won=False)

            if wins_a == needed or wins_b == needed:
                break

        if wins_a > wins_b:
            winner = name_a
            loser  = name_b
        elif wins_b > wins_a:
            winner = name_b
            loser  = name_a
        else:
            winner = None  # ничья Bo2
            loser  = None

        def kd_ratio(nick: str) -> float:
            k, d = total_kd[nick][0], total_kd[nick][1]
            return k / d if d > 0 else float(k)

        def kd_score(nick: str) -> float:
            k, d, a = total_kd[nick][0], total_kd[nick][1], total_kd[nick][2]
            return k + a * 0.5 - d * 0.7

        is_draw = (winner is None)

        if is_draw:
            winner_nicks = [p[0] for p in active_a]
            loser_nicks  = [p[0] for p in active_b]
        else:
            winner_nicks = [p[0] for p in (active_a if winner == name_a else active_b)]
            loser_nicks  = [p[0] for p in (active_b if winner == name_a else active_a)]

        def mvp_weight(nick: str) -> float:
            """Вес для выбора MVP/EVP: K/D + бонус за рейтинг + случайность."""
            kd = kd_ratio(nick)
            # Ищем рейтинг игрока из активного состава
            all_active = active_a + active_b
            p_rating = next((p[2] for p in all_active if p[0] == nick), 20.0)
            # Нормализуем рейтинг: 15–32 → 0–1
            rating_factor = max(0.0, min(1.0, (p_rating - 15.0) / 17.0))
            rounds = max(1, total_rounds_per_player.get(nick, 26))
            rating30 = compute_hltv30(total_kd.get(nick, [0]*13), rounds)
            noise = random.uniform(0.85, 1.15)
            return (kd * 0.50 + rating_factor * 0.20 + rating30 * 0.30) * noise

        mvp = max(winner_nicks, key=mvp_weight) if winner_nicks else "—"
        evp = max(loser_nicks, key=mvp_weight) if loser_nicks else "—"

        out: list[str] = []

        if is_draw:
            header = f"🤝 <b>Ничья! {md_escape(name_a)} {wins_a} – {wins_b} {md_escape(name_b)}</b>"
        else:
            header = f"⚔️ <b>{md_escape(name_a)} {wins_a} – {wins_b} {md_escape(name_b)}</b>"
        if tour_name:
            header += f"  |  🏟 {md_escape(tour_name)}"
        if stage_name:
            header += f"  |  🎯 {md_escape(stage_name)}"
        out.append(header)
        out.append(f"📋 Формат: {fmt}\n")

        out.append("🗺 <b>Карты</b>")
        out.extend(map_results)
        out.append("")

        if is_draw:
            out.append(f"⭐ <b>Лучший {md_escape(name_a)}</b>: {md_escape(mvp)}")
            out.append(f"⭐ <b>Лучший {md_escape(name_b)}</b>: {md_escape(evp)}")
        else:
            out.append(f"🥇 <b>МВП</b>: {md_escape(mvp)}")
            out.append(f"💀 <b>ЕВП</b>: {md_escape(evp)}")
        out.append("")

        def fmt_player_stats(nicks_list: list[str]) -> list[str]:
            rows = []
            sorted_nicks = sorted(nicks_list, key=kd_score, reverse=True)
            for nick in sorted_nicks:
                stats = total_kd.get(nick, [0]*13)
                k, d, a = stats[0], stats[1], stats[2]
                kd_val = round(k / d, 2) if d > 0 else float(k)
                rounds = max(1, total_rounds_per_player.get(nick, 26))
                adr = round(stats[3] / rounds, 1)
                kast_pct = round((stats[4] / rounds) * 100, 1)
                rating20 = compute_hltv20(stats, rounds)
                rows.append(
                    f"  {md_escape(nick)}  {k}/{d}/{a}  КД {kd_val:.2f}  "
                    f"ADR {adr}  KAST {kast_pct}%  "
                    f"<b>Rating 2.0: {rating20:.2f}</b>"
                )
            return rows

        out.append("📊 <b>СТАТИСТИКА</b>")
        out.append("")
        out.append(f"👥 <b>{md_escape(name_a)}</b>")
        out.extend(fmt_player_stats([p[0] for p in active_a]))
        out.append("")
        out.append(f"👥 <b>{md_escape(name_b)}</b>")
        out.extend(fmt_player_stats([p[0] for p in active_b]))

        if not is_draw:
            await db_update_after_match(winner, loser, f"{wins_a}:{wins_b}", is_tournament=bool(tour_name))
        else:
            # Ничья Bo2 — только +1 за сыгранность обеим
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO match_history (team_a, team_b, result, score, played_at) VALUES (?,?,?,?,?)",
                    (name_a, name_b, "DRAW", f"{wins_a}:{wins_b}", datetime.now().isoformat(timespec='seconds'))
                )
                await db.commit()
            # Дух при ничье меняется только в турнирных матчах
            if tour_name:
                await db_change_spirit(name_a, +1.0, f"Ничья ({wins_a}:{wins_b}) +сыгранность")
                await db_change_spirit(name_b, +1.0, f"Ничья ({wins_a}:{wins_b}) +сыгранность")

        # Clean play bonus (+2%) — только в турнирных матчах
        if not is_draw and tour_name:
            await db_change_spirit(winner, +2.0, "Чистая игра (бонус)")

        # VRS рейтинг — только в турнирных матчах
        if tour_name:
            w_maps = wins_a if winner == name_a else wins_b
            l_maps = wins_b if winner == name_a else wins_a
            await db_update_vrs(winner, loser, is_draw, name_a, name_b, tour_name, w_maps, l_maps)

        # Build spirit status block
        spirit_lines = ["", "🔥 <b>Дух после матча:</b>"]
        for tname in (name_a, name_b):
            t = await db_get_team(tname)
            if t:
                sp = t[2]
                sl, se = spirit_status(sp)
                spirit_lines.append(f"  {se} <b>{md_escape(tname)}</b>: {sp:.0f}%  [{spirit_bar(sp)}]  {sl}")
        out.extend(spirit_lines)

        if tour_name:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO tournaments (name, created_at) VALUES (?,?)",
                    (tour_name, datetime.now().isoformat(timespec='seconds'))
                )
                db_winner = winner if winner else "DRAW"
                await db.execute(
                    "INSERT INTO tournament_matches (tour_name, team_a, team_b, winner, place_a, place_b) VALUES (?,?,?,?,?,?)",
                    (tour_name, name_a, name_b, db_winner, wins_a, wins_b)
                )
                nick_to_team = {p[0]: name_a for p in active_a}
                nick_to_team.update({p[0]: name_b for p in active_b})
                for nick, stat in total_kd.items():
                    k, d, a = stat[0], stat[1], stat[2]
                    adr_dmg      = stat[3]
                    kast_rounds  = stat[4]
                    entry_k      = stat[5]
                    entry_d      = stat[6]
                    mk3          = stat[7]
                    mk4          = stat[8]
                    mk5          = stat[9]
                    clutches_won = stat[10]
                    util_dmg     = stat[11]
                    flash_assists = stat[12]
                    rounds_nick  = max(1, total_rounds_per_player.get(nick, 26))
                    team_of_nick = nick_to_team.get(nick, name_a)
                    await db.execute(
                        """INSERT INTO tournament_player_stats
                           (tour_name, nick, team_name, kills, deaths, assists,
                            adr_dmg, kast_rounds, total_rounds,
                            entry_k, entry_d, mk3, mk4, mk5,
                            clutches_won, util_dmg, flash_assists)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(tour_name, nick) DO UPDATE SET
                               kills         = kills         + excluded.kills,
                               deaths        = deaths        + excluded.deaths,
                               assists       = assists       + excluded.assists,
                               adr_dmg       = adr_dmg       + excluded.adr_dmg,
                               kast_rounds   = kast_rounds   + excluded.kast_rounds,
                               total_rounds  = total_rounds  + excluded.total_rounds,
                               entry_k       = entry_k       + excluded.entry_k,
                               entry_d       = entry_d       + excluded.entry_d,
                               mk3           = mk3           + excluded.mk3,
                               mk4           = mk4           + excluded.mk4,
                               mk5           = mk5           + excluded.mk5,
                               clutches_won  = clutches_won  + excluded.clutches_won,
                               util_dmg      = util_dmg      + excluded.util_dmg,
                               flash_assists = flash_assists  + excluded.flash_assists""",
                        (tour_name, nick, team_of_nick, k, d, a,
                         adr_dmg, kast_rounds, rounds_nick,
                         entry_k, entry_d, mk3, mk4, mk5,
                         clutches_won, util_dmg, flash_assists)
                    )
                await db.commit()

            # Синхронизируем HLTV-статистику (/hltv_top, /card_player) с реальными
            # турнирными данными — она теперь считается только с турниров.
            maps_this_match = len(map_results)
            a_nicks = {p[0] for p in active_a}
            b_nicks = {p[0] for p in active_b}
            for nick in total_kd.keys():
                team_of_nick = nick_to_team.get(nick, name_a)
                await db_hltv_ensure_player(nick, team_of_nick)
                await db_hltv_sync_from_tournaments(nick, team_of_nick)
                if nick in a_nicks:
                    await db_hltv_add_match_maps(nick, maps_this_match, wins_a, wins_b)
                elif nick in b_nicks:
                    await db_hltv_add_match_maps(nick, maps_this_match, wins_b, wins_a)

        try:
            full_text = "\n".join(out)
            await safe_send_long(msg, full_text)
        except Exception as e:
            await msg.answer(f"❌ Ошибка при отправке результата:\n<code>{type(e).__name__}: {e}</code>")
    except Exception as e:
        tb = traceback.format_exc()
        await msg.answer(f"❌ Ошибка симуляции:\n<code>{type(e).__name__}: {e}</code>\n\n<code>{tb[:1500]}</code>")



@dp.message(Command("stat_tour"))
async def cmd_stat_tour(msg: Message) -> None:
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/stat_tour НазваниеТурнира</code>")
        return

    tour_name = parts[1].strip()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT created_at FROM tournament_registry WHERE name=?", (tour_name,)
        ) as cursor:
            tour_row = await cursor.fetchone()

        # Fallback: турнир мог быть создан через /match без /addtour
        if not tour_row:
            async with db.execute(
                "SELECT created_at FROM tournaments WHERE name=?", (tour_name,)
            ) as cursor:
                tour_row = await cursor.fetchone()

        if not tour_row:
            await msg.answer(
                f"❌ Турнир «{md_escape(str(tour_name))}» не найден.\n"
                f"Добавь турнир через <code>/addtour</code>, затем проведи матчи."
            )
            return

        async with db.execute(
            "SELECT team_a, team_b, winner, place_a, place_b FROM tournament_matches WHERE tour_name=?",
            (tour_name,)
        ) as cursor:
            matches = await cursor.fetchall()

        async with db.execute(
            """SELECT nick, team_name, kills, deaths, assists,
                      adr_dmg, kast_rounds, total_rounds,
                      entry_k, entry_d, mk3, mk4, mk5,
                      clutches_won, util_dmg, flash_assists
               FROM tournament_player_stats WHERE tour_name=?""",
            (tour_name,)
        ) as cursor:
            player_rows = await cursor.fetchall()

    if not matches:
        await msg.answer(f"ℹ️ В турнире <b>{md_escape(str(tour_name))}</b> ещё не сыграно ни одного матча.")
        return

    team_stats: dict[str, dict] = {}
    for team_a, team_b, winner, score_a, score_b in matches:
        for team in (team_a, team_b):
            if team not in team_stats:
                team_stats[team] = {"wins": 0, "losses": 0, "draws": 0, "maps_w": 0, "maps_l": 0}
        if winner == "DRAW":
            team_stats[team_a]["draws"] += 1
            team_stats[team_b]["draws"] += 1
        elif winner == team_a:
            team_stats[team_a]["wins"]   += 1
            team_stats[team_b]["losses"] += 1
        else:
            team_stats[team_b]["wins"]   += 1
            team_stats[team_a]["losses"] += 1
        team_stats[team_a]["maps_w"] += score_a
        team_stats[team_a]["maps_l"] += score_b
        team_stats[team_b]["maps_w"] += score_b
        team_stats[team_b]["maps_l"] += score_a

    sorted_teams = sorted(
        team_stats.items(),
        key=lambda x: (-x[1]["wins"], -x[1]["maps_w"])
    )

    player_stats = []
    for row in player_rows:
        nick, team_name = row[0], row[1]
        kills, deaths, assists = row[2], row[3], row[4] if len(row) > 4 else 0
        adr_dmg      = row[5]  if len(row) > 5  else 0.0
        kast_rounds  = row[6]  if len(row) > 6  else 0
        total_rounds = max(1, row[7] if len(row) > 7 else 26)
        entry_k      = row[8]  if len(row) > 8  else 0
        entry_d      = row[9]  if len(row) > 9  else 0
        mk3          = row[10] if len(row) > 10 else 0
        mk4          = row[11] if len(row) > 11 else 0
        mk5          = row[12] if len(row) > 12 else 0
        clutches_won = row[13] if len(row) > 13 else 0
        util_dmg     = row[14] if len(row) > 14 else 0.0
        flash_assists = row[15] if len(row) > 15 else 0
        kd = round(kills / deaths, 2) if deaths > 0 else float(kills)
        adr = round(adr_dmg / total_rounds, 1)
        kast_pct = round((kast_rounds / total_rounds) * 100, 1)
        stats_list = [kills, deaths, assists, adr_dmg, kast_rounds,
                      entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists]
        rating20 = compute_hltv20(stats_list, total_rounds)
        player_stats.append((nick, team_name, kills, deaths, kd, adr, kast_pct, rating20))
    player_stats.sort(key=lambda x: (-x[7], -x[4], -x[2]))  # sort by rating20

    medal = {1: "🥇", 2: "🥈", 3: "🥉"}

    header_lines = [f"🏆 Статистика <b>{md_escape(str(tour_name))}</b>", ""]
    for i, (team, ts) in enumerate(sorted_teams, 1):
        prefix = medal.get(i, str(i))
        maps_str = f"{ts['maps_w']}-{ts['maps_l']}"
        draws_part = f"/{ts['draws']}Н" if ts.get('draws', 0) > 0 else ""
        header_lines.append(f"{prefix} {md_escape(str(team))}  {ts['wins']}В{draws_part}/{ts['losses']}П  карты {maps_str}")

    await msg.answer("\n".join(header_lines))

    player_lines = ["📊 <b>Статистика Игроков (HLTV 2.0)</b>", ""]
    for i, (nick, team_name, kills, deaths, kd, adr, kast_pct, rating20) in enumerate(player_stats[:50], 1):
        prefix = medal.get(i, str(i))
        player_lines.append(
            f"{prefix} <b>{md_escape(str(nick))}</b> ({md_escape(str(team_name))})  "
            f"{kills}/{deaths}  КД {kd:.2f}  ADR {adr}  KAST {kast_pct}%  "
            f"<b>Rating 2.0: {rating20:.2f}</b>"
        )

    try:
        full_text = "\n".join(player_lines)
        await safe_send_long(msg, full_text)
    except Exception as e:
        await msg.answer(f"❌ Ошибка при отправке результата:\n<code>{type(e).__name__}: {e}</code>")


@dp.message(Command("addrolesall"))
async def cmd_addrolesall(msg: Message) -> None:
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) < 4:
        await msg.answer(
            "❌ Формат: <code>/addrolesall Команда Ник1 Роль1 Ник2 Роль2 ...</code>\n"
            f"Доступные роли: {', '.join(ROLES)}"
        )
        return
    team_name = parts[1]
    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    # Строим пары (ник, роль), учитывая двухсловные роли
    valid_roles_lower = {r.lower(): r for r in ROLES}
    tail = parts[2:]
    pairs = []
    i = 0
    while i < len(tail):
        nick = tail[i]
        # Пробуем сначала двухсловную роль, потом однословную
        if i + 2 < len(tail):
            two_word = (tail[i + 1] + " " + tail[i + 2]).lower()
            if two_word in valid_roles_lower:
                pairs.append((nick, valid_roles_lower[two_word]))
                i += 3
                continue
        if i + 1 < len(tail):
            one_word = tail[i + 1].lower()
            if one_word in valid_roles_lower:
                pairs.append((nick, valid_roles_lower[one_word]))
                i += 2
                continue
        # Не удалось распознать роль — пропускаем
        pairs.append((nick, None))
        i += 2 if i + 1 < len(tail) else 1

    if not pairs:
        await msg.answer(
            "❌ Формат: <code>/addrolesall Команда Ник1 Роль1 Ник2 Роль2 ...</code>\n"
            f"Доступные роли: {', '.join(ROLES)}"
        )
        return
    lines = [f"🎭 <b>Роли в команде {md_escape(str(team_name))}:</b>\n"]
    async with aiosqlite.connect(DB_PATH) as db:
        for nick, role in pairs:
            if role is None:
                lines.append(f"❌ <code>{md_escape(str(nick))}</code> — роль не распознана. Доступные: {', '.join(ROLES)}")
                continue
            cursor = await db.execute(
                "UPDATE players SET role=? "
                "WHERE nick=? AND team_name=?",
                (role, nick, team_name)
            )
            if cursor.rowcount == 0:
                lines.append(f"❌ <code>{md_escape(str(nick))}</code> — игрок не найден в команде")
            else:
                lines.append(f"✅ <b>{md_escape(str(nick))}</b> → <b>{md_escape(str(role))}</b>")
        await db.commit()
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("addroles"))
async def cmd_addroles(msg: Message) -> None:
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) < 4:
        await msg.answer(
            "❌ Формат: <code>/addroles Команда Ник Роль</code>\n"
            f"Доступные роли: {', '.join(ROLES)}"
        )
        return

    team_name = parts[1]
    nick = parts[2]
    role = " ".join(parts[3:])  # поддержка двухсловных ролей (Entry Fragger)

    if role not in ROLES:
        await msg.answer(
            f"❌ Роль «{md_escape(str(role))}» не существует.\n"
            f"Доступные роли: {', '.join(ROLES)}"
        )
        return

    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE players SET role=? WHERE nick=? AND team_name=?",
            (role, nick, team_name)
        )
        await db.commit()
        if cursor.rowcount == 0:
            await msg.answer(f"❌ Игрок «{md_escape(str(nick))}» не найден в команде «{md_escape(str(team_name))}».")
            return

    await msg.answer(
        f"✅ Роль игрока <b>{md_escape(str(nick))}</b> в команде <b>{md_escape(str(team_name))}</b> изменена на <b>{md_escape(str(role))}</b>."
    )


@dp.message(Command("addratingall"))
async def cmd_addratingall(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()[1:]
    if not parts or len(parts) % 2 != 0:
        await msg.answer(
            "❌ Формат: <code>/addratingall Ник1 Рейтинг1 Ник2 Рейтинг2 ...</code>\n"
            "Рейтинг — от 15.0 до 32.0."
        )
        return
    pairs = [(parts[i], parts[i + 1]) for i in range(0, len(parts), 2)]
    lines = ["📊 <b>Результат установки рейтингов:</b>\n"]
    for nick, rating_str in pairs:
        try:
            rating = float(rating_str)
            if not (15.0 <= rating <= 32.0): raise ValueError
        except ValueError:
            lines.append(f"❌ <code>{md_escape(str(nick))}</code> — рейтинг «{md_escape(str(rating_str))}» некорректен (нужно 15.0–32.0)")
            continue
        found = await db_add_rating(nick, rating)
        if found:
            lines.append(f"✅ <b>{md_escape(str(nick))}</b> → <b>{rating:.1f}</b>")
        else:
            lines.append(f"❌ <code>{md_escape(str(nick))}</code> — игрок не найден")
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("addrating"))
async def cmd_addrating(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer("❌ Формат: <code>/addrating Игрок Рейтинг</code>\nРейтинг — число от 15.0 до 32.0.")
        return
    nick = parts[1]
    try:
        rating = float(parts[2])
        if not (15.0 <= rating <= 32.0): raise ValueError
    except ValueError:
        await msg.answer("❌ Рейтинг должен быть числом от 15.0 до 32.0.")
        return

    found = await db_add_rating(nick, rating)
    if not found:
        await msg.answer(f"❌ Игрок «{md_escape(str(nick))}» не найден.")
        return
    await msg.answer(f"✅ Рейтинг игрока <b>{md_escape(str(nick))}</b> установлен на <b>{rating:.1f}</b>.")


@dp.message(Command("setage"))
async def cmd_setage(msg: Message) -> None:
    """
    /setage Ник Возраст — установить возраст игрока (14–40).
    Только для администраторов.
    Пример: /setage s1mple 27
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer(
            "❌ Формат: <code>/setage Ник Возраст</code>\n"
            "Возраст — целое число от 14 до 40.\n"
            "Пример: <code>/setage s1mple 27</code>"
        )
        return
    nick = parts[1]
    try:
        age = int(parts[2])
        if not (14 <= age <= 40):
            raise ValueError
    except ValueError:
        await msg.answer("❌ Возраст должен быть целым числом от 14 до 40.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE players SET age=? WHERE nick=?", (age, nick)
        )
        rowcount = cursor.rowcount
        await db.commit()

    if rowcount == 0:
        await msg.answer(f"❌ Игрок «{md_escape(nick)}» не найден.")
        return
    await msg.answer(f"✅ Возраст игрока <b>{md_escape(nick)}</b> установлен: <b>{age} лет</b>.")


@dp.message(Command("fixagerating"))
async def cmd_fixagerating(msg: Message) -> None:
    """
    /fixagerating — автоматически исправить перепутанные местами возраст и рейтинг
    у ВСЕХ игроков в базе.

    Признак ошибки: rating выглядит как возраст (целое число 14–40),
    а age выглядит как рейтинг (15.0–32.0).
    Команда меняет их местами для таких игроков.

    Только для администраторов. Требует подтверждения: /fixagerating confirm
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split()
    if len(parts) < 2 or parts[1].lower() != "confirm":
        await msg.answer(
            "⚠️ <b>Исправление перепутанных возраста и рейтинга</b>\n\n"
            "Команда найдёт всех игроков у кого <code>rating</code> выглядит как возраст "
            "(целое 14–40), а <code>age</code> — как рейтинг (15–32), "
            "и поменяет их местами.\n\n"
            "Для подтверждения введите:\n"
            "<code>/fixagerating confirm</code>"
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT nick, team_name, rating, age FROM players") as cursor:
            all_players = await cursor.fetchall()

        fixed = []
        skipped = []
        for nick, team_name, rating, age in all_players:
            # Признак перепутанных данных:
            # rating — целое число в диапазоне возраста (14–40)
            # age    — число в диапазоне рейтинга (15–32)
            try:
                rating_looks_like_age = (rating is not None and rating == int(rating)) and (14 <= int(rating) <= 40)
                age_looks_like_rating = (age is not None and 15.0 <= float(age) <= 32.0)
            except (TypeError, ValueError):
                skipped.append((nick, team_name))
                continue
            if rating_looks_like_age and age_looks_like_rating:
                new_rating = float(age)   # бывший age становится рейтингом
                new_age    = int(rating)  # бывший rating становится возрастом
                await db.execute(
                    "UPDATE players SET rating=?, age=? WHERE nick=? AND team_name=?",
                    (new_rating, new_age, nick, team_name)
                )
                fixed.append((nick, team_name, rating, age, new_rating, new_age))
            else:
                skipped.append((nick, team_name))

        await db.commit()

    if not fixed:
        await msg.answer(
            "ℹ️ Не найдено игроков с явно перепутанными возрастом и рейтингом.\n"
            "Используйте <code>/setage</code> и <code>/addrating</code> для ручного исправления."
        )
        return

    lines = [f"✅ <b>Исправлено {len(fixed)} игроков:</b>\n"]
    for nick, team_name, old_r, old_a, new_r, new_a in fixed:
        lines.append(
            f"  • <b>{md_escape(nick)}</b> ({md_escape(team_name)}): "
            f"рейтинг {old_r:.0f}→<b>{new_r:.1f}</b>, возраст {old_a}→<b>{new_a}</b>"
        )
    if skipped:
        lines.append(f"\nПропущено (данные корректны): {len(skipped)} игр.")
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("addspirit"))
async def cmd_addspirit(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer("❌ Формат: <code>/addspirit Команда Значение</code>\nЗначение — от 0 до 100 (установить), или +N / -N (изменить).")
        return
    team_name = parts[1]
    raw = parts[2]
    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return
    try:
        if raw.startswith("+") or raw.startswith("-"):
            delta = float(raw)
            new_val = await db_change_spirit(team_name, delta, "Ручная корректировка администратором")
            action = f"{'+'if delta>=0 else ''}{delta:.0f}%"
        else:
            val = float(raw)
            if not (0.0 <= val <= 100.0): raise ValueError
            new_val = await db_set_spirit(team_name, val, "Ручная установка администратором")
            action = f"= {val:.0f}%"
    except ValueError:
        await msg.answer("❌ Значение должно быть числом от 0 до 100, или +N / -N.")
        return
    status_label, status_emoji = spirit_status(new_val)
    await msg.answer(
        f"{status_emoji} Дух <b>{md_escape(str(team_name))}</b> изменён ({action})\n"
        f"📊 Текущий дух: <b>{new_val:.0f}%</b>  [{spirit_bar(new_val)}]\n"
        f"Статус: <b>{status_label}</b>"
    )


@dp.message(Command("addspiritall"))
async def cmd_addspiritall(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.answer("❌ Формат: <code>/addspiritall Команда1 Команда2 ... Значение</code>\nЗначение — +N, -N или 0–100.")
        return
    raw = parts[-1]
    team_names = parts[1:-1]
    try:
        is_delta = raw.startswith("+") or raw.startswith("-")
        delta_or_val = float(raw)
        if not is_delta and not (0.0 <= delta_or_val <= 100.0):
            raise ValueError
    except ValueError:
        await msg.answer("❌ Последний аргумент должен быть числом 0–100 или +N/-N.")
        return
    lines = ["🔥 <b>Дух команд изменён:</b>\n"]
    not_found = []
    for tn in team_names:
        if not await db_get_team(tn):
            not_found.append(tn)
            continue
        if is_delta:
            new_val = await db_change_spirit(tn, delta_or_val, "Массовая корректировка")
        else:
            new_val = await db_set_spirit(tn, delta_or_val, "Массовая установка")
        sl, se = spirit_status(new_val)
        lines.append(f"{se} <b>{md_escape(str(tn))}</b>: <b>{new_val:.0f}%</b>  {sl}")
    if not_found:
        lines.append("\n❌ Не найдены: " + ", ".join(f"<code>{md_escape(str(t))}</code>" for t in not_found))
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("delete_team"))
async def cmd_delete_team(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/delete_team НазваниеКоманды</code>")
        return

    team_name = parts[1].strip()

    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM players WHERE team_name=?", (team_name,))
        await db.execute("DELETE FROM teams WHERE name=?", (team_name,))
        await db.execute("DELETE FROM vrs WHERE team_name=?", (team_name,))
        await db.execute("DELETE FROM merch WHERE team_name=?", (team_name,))
        await db.execute("DELETE FROM spirit_history WHERE team_name=?", (team_name,))
        await db.execute("DELETE FROM loans WHERE from_team=? OR to_team=?", (team_name, team_name))
        await db.execute("DELETE FROM team_tournament_places WHERE team_name=?", (team_name,))
        await db.execute("DELETE FROM map_stats WHERE team_name=?", (team_name,))
        await db.commit()

    await msg.answer(
        f"🗑 Команда <b>{md_escape(str(team_name))}</b> успешно удалена.\n"
        "Все игроки, аренды и история духа удалены из базы данных."
    )


@dp.message(Command("reserve"))
async def cmd_reserve(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer("❌ Формат: <code>/reserve Команда Ник</code>")
        return

    team_name = parts[1]
    nick      = parts[2]

    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_reserve FROM players WHERE nick=? AND team_name=?",
            (nick, team_name)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await msg.answer(f"❌ Игрок «{md_escape(str(nick))}» не найден в команде «{md_escape(str(team_name))}».")
            return

        if row[0] == 2:
            await msg.answer("⛔ Нельзя менять статус тренера через /reserve. Тренер — отдельная роль.")
            return
        new_status = 0 if row[0] == 1 else 1
        status_text = "основной состав" if new_status == 0 else "резерв"

        await db.execute(
            "UPDATE players SET is_reserve=? WHERE nick=? AND team_name=?",
            (new_status, nick, team_name)
        )
        await db.commit()

    emoji = "✅" if new_status == 0 else "🪑"
    await msg.answer(f"{emoji} Игрок <b>{md_escape(str(nick))}</b> переведён в <b>{status_text}</b> команды <b>{md_escape(str(team_name))}</b>.")


# ═════════════════════════════════════════════════════════════════════════
# ПАНЕЛЬ ЛИДЕРА КОМАНДЫ (кнопки: резерв/основа, роли, трансферы)
# ═════════════════════════════════════════════════════════════════════════

async def db_get_all_teams_id_name() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM teams ORDER BY name") as cursor:
            return await cursor.fetchall()


async def db_get_team_id_by_name(team_name: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM teams WHERE name=?", (team_name,)) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def db_get_team_name_by_id(team_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM teams WHERE id=?", (team_id,)) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


PANEL_ROLES = [r for r in ROLES if r != "Coach"]


# ═════════════════════════════════════════════════════════════════════════
# БАЛАНС КОМАНД (/balance, /balance_top, переводы денег)
# ═════════════════════════════════════════════════════════════════════════

STARTING_BALANCE = 10000.0


def fmt_money(amount: float) -> str:
    return f"{amount:,.0f} ₽".replace(",", " ")


async def db_get_team_balance(team_name: str) -> float | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM teams WHERE name=? COLLATE NOCASE", (team_name,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def db_find_team_name_ci(team_name: str) -> str | None:
    """Находит настоящее (с правильным регистром) имя команды по вводу без учёта регистра."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM teams WHERE name=? COLLATE NOCASE", (team_name,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


def _academy_filter_sql() -> tuple[str, list]:
    """Строит SQL-условие и параметры, исключающие «академии»/фарм-команды
    (см. ACADEMY_TEAM_MARKERS) из выборки по названию команды."""
    if not ACADEMY_TEAM_MARKERS:
        return "", []
    conditions = " AND ".join("lower(name) NOT LIKE ?" for _ in ACADEMY_TEAM_MARKERS)
    params = [f"%{marker.lower()}%" for marker in ACADEMY_TEAM_MARKERS]
    return conditions, params


async def db_balance_top_page(offset: int, limit: int) -> list[tuple]:
    where_sql, where_params = _academy_filter_sql()
    query = "SELECT name, balance FROM teams"
    if where_sql:
        query += f" WHERE {where_sql}"
    query += " ORDER BY balance DESC, name ASC LIMIT ? OFFSET ?"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, (*where_params, limit, offset)) as cursor:
            return await cursor.fetchall()


async def db_add_balance(
    team_name: str, amount: float, reason: str, initiated_by: int | None = None
) -> tuple[bool, str, float]:
    """Начисляет команде деньги «из воздуха» (призовые, штраф-возврат и т.п.),
    без списания у другой команды. Возвращает (успех, ошибка, новый_баланс)."""
    if amount <= 0:
        return False, "Сумма должна быть больше нуля.", 0.0

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, balance FROM teams WHERE name=? COLLATE NOCASE", (team_name,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, f"Команда «{md_escape(team_name)}» не найдена.", 0.0

        real_name, balance = row
        new_balance = balance + amount
        await db.execute(
            "UPDATE teams SET balance = balance + ? WHERE name=?", (amount, real_name)
        )
        await db.execute(
            "INSERT INTO balance_history (from_team, to_team, amount, reason, initiated_by, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (None, real_name, amount, reason, initiated_by,
             datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()

    return True, "", new_balance


async def db_get_league_bank() -> float:
    """Возвращает текущий баланс общего счёта лиги (виден всем игрокам)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM league_bank WHERE id=1"
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0.0


async def db_add_to_league_bank(amount: float) -> float:
    """Зачисляет сумму на общий счёт лиги (например, покупку в магазине команд).
    Возвращает новый баланс банка."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE league_bank SET balance = balance + ? WHERE id=1", (amount,)
        )
        async with db.execute("SELECT balance FROM league_bank WHERE id=1") as cursor:
            row = await cursor.fetchone()
        await db.commit()
    return row[0] if row else amount


FREE_AGENT_TEAM_NAME = "🆓 Свободный агент"

# Лимит подписаний свободных агентов на команду за один игровой тур. Счётчик
# обнуляется при завершении любого турнира командой /donetour.
FREE_AGENT_SIGN_LIMIT_PER_TOUR = 2


async def db_get_free_agent_signings_count(team_name: str) -> int:
    """Сколько свободных агентов команда уже подписала в текущем туре."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count FROM free_agent_signings WHERE team_name=? COLLATE NOCASE",
            (team_name,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


async def db_increment_free_agent_signings(team_name: str) -> int:
    """Увеличивает счётчик подписаний свободных агентов команды на 1 за текущий
    тур. Возвращает новое значение счётчика."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO free_agent_signings (team_name, count) VALUES (?, 1) "
            "ON CONFLICT(team_name) DO UPDATE SET count = count + 1",
            (team_name,)
        )
        async with db.execute(
            "SELECT count FROM free_agent_signings WHERE team_name=? COLLATE NOCASE",
            (team_name,)
        ) as cursor:
            row = await cursor.fetchone()
        await db.commit()
    return row[0] if row else 1


async def db_reset_free_agent_signings() -> None:
    """Обнуляет счётчики подписаний свободных агентов у всех команд. Вызывается
    при завершении любого турнира через /donetour."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM free_agent_signings")
        await db.commit()


async def db_transfer_balance(
    from_team: str, to_team: str, amount: float, initiated_by: int
) -> tuple[bool, str]:
    """Переводит деньги от одной команды к другой. Возвращает (успех, сообщение об ошибке/пусто)."""
    if amount <= 0:
        return False, "Сумма перевода должна быть больше нуля."
    if from_team.strip().lower() == to_team.strip().lower():
        return False, "Нельзя перевести деньги самим себе."

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, balance FROM teams WHERE name=? COLLATE NOCASE", (from_team,)
        ) as cursor:
            from_row = await cursor.fetchone()
        async with db.execute(
            "SELECT name, balance FROM teams WHERE name=? COLLATE NOCASE", (to_team,)
        ) as cursor:
            to_row = await cursor.fetchone()

        if not from_row:
            return False, f"Команда «{md_escape(from_team)}» не найдена."
        if not to_row:
            return False, f"Команда «{md_escape(to_team)}» не найдена."

        from_real_name, from_balance = from_row
        to_real_name, to_balance = to_row

        if from_balance < amount:
            return False, (
                f"Недостаточно средств: на балансе «{md_escape(from_real_name)}» "
                f"{fmt_money(from_balance)}, а нужно {fmt_money(amount)}."
            )

        await db.execute(
            "UPDATE teams SET balance = balance - ? WHERE name=?", (amount, from_real_name)
        )
        await db.execute(
            "UPDATE teams SET balance = balance + ? WHERE name=?", (amount, to_real_name)
        )
        await db.execute(
            "INSERT INTO balance_history (from_team, to_team, amount, reason, initiated_by, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (from_real_name, to_real_name, amount, "Перевод между командами",
             initiated_by, datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()

    return True, ""


def balance_pick_team_kb(teams: list[str], to_team: str, amount: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора своей команды-отправителя, если менеджер управляет несколькими."""
    rows = []
    for tname in teams:
        rows.append([InlineKeyboardButton(
            text=tname, callback_data=f"bal|from|{tname}|{to_team}|{amount}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("bank"))
async def cmd_bank(msg: Message) -> None:
    """/bank — общий счёт лиги (виден всем). Сюда уходят все деньги, списанные
    с команд (например, покупки в магазине команды)."""
    bank_balance = await db_get_league_bank()
    await msg.answer(
        "🏦 <b>Банк лиги</b>\n\n"
        f"Текущий баланс: <b>{fmt_money(bank_balance)}</b>\n\n"
        "<i>Сюда зачисляются все деньги, списанные с команд — например, "
        "покупки в магазине улучшений.</i>"
    )


# ═════════════════════════════════════════════════════════════════════════
# МАГАЗИН ЛИГИ (/shop) — покупка улучшений команды за баланс
# ═════════════════════════════════════════════════════════════════════════

SHOP_ITEMS = {
    "bootcamp": {
        "title": "🏕 Буткемп",
        "price": 600_000.0,
        "effect": "winrate",
        "min": 1.0,
        "max": 6.0,
        "desc": "Повышает винрейт команды на случайную величину от 1% до 6%.",
    },
    "devices": {
        "title": "💻 Устройства",
        "price": 300_000.0,
        "effect": "chemistry",
        "min": 5.0,
        "max": 10.0,
        "desc": "Повышает сыгранность команды на случайную величину от 5 до 10.",
    },
    "new_devices": {
        "title": "📱 Новые девайсы",
        "price": 250_000.0,
        "effect": "spirit",
        "min": 5.0,
        "max": 10.0,
        "desc": "Повышает дух команды на случайную величину от 5 до 10.",
    },
}


def shop_menu_text() -> str:
    lines = ["🛒 <b>Магазин лиги</b>", "", "Улучшения покупаются с баланса вашей команды:", ""]
    for item in SHOP_ITEMS.values():
        lines.append(f"{item['title']} — <b>{fmt_money(item['price'])}</b>")
        lines.append(f"  <i>{md_escape(item['desc'])}</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def shop_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{item['title']} — {fmt_money(item['price'])}", callback_data=f"shop|item|{key}")]
        for key, item in SHOP_ITEMS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def db_shop_buy(team_name: str, item_key: str, initiated_by: int) -> tuple[bool, str]:
    """Списывает цену товара с баланса команды и применяет случайный эффект.
    Возвращает (успех, текст сообщения)."""
    item = SHOP_ITEMS.get(item_key)
    if not item:
        return False, "⛔ Такого товара нет в магазине."

    price = item["price"]
    effect_value = round(random.uniform(item["min"], item["max"]), 1)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, balance, spirit FROM teams WHERE name=? COLLATE NOCASE", (team_name,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, f"⛔ Команда «{md_escape(team_name)}» не найдена."

        real_name, balance, spirit = row
        if balance < price:
            return False, (
                f"⛔ Недостаточно средств на балансе «{md_escape(real_name)}»: "
                f"{fmt_money(balance)}, а нужно {fmt_money(price)}."
            )

        await db.execute(
            "UPDATE teams SET balance = balance - ? WHERE name=?", (price, real_name)
        )
        await db.execute(
            "INSERT INTO balance_history (from_team, to_team, amount, reason, initiated_by, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (real_name, None, price, f"Магазин: {item['title']}", initiated_by,
             datetime.now().isoformat(timespec="seconds"))
        )
        await db.execute(
            "UPDATE league_bank SET balance = balance + ? WHERE id=1", (price,)
        )

        if item["effect"] == "winrate":
            await db.execute(
                "UPDATE teams SET winrate_boost = winrate_boost + ? WHERE name=?",
                (effect_value, real_name)
            )
            effect_text = f"📈 Винрейт команды повышен на <b>+{effect_value}%</b>."
        elif item["effect"] == "chemistry":
            await db.execute(
                "UPDATE teams SET chemistry = MIN(50, chemistry + ?) WHERE name=?",
                (effect_value, real_name)
            )
            effect_text = f"🤝 Сыгранность команды повышена на <b>+{effect_value}</b>."
        else:  # spirit
            new_spirit = max(0.0, min(100.0, spirit + effect_value))
            actual_delta = new_spirit - spirit
            await db.execute(
                "UPDATE teams SET spirit=? WHERE name=?", (new_spirit, real_name)
            )
            await db.execute(
                "INSERT INTO spirit_history (team_name, change, reason, changed_at) VALUES (?,?,?,?)",
                (real_name, actual_delta, f"Магазин: {item['title']}",
                 datetime.now().isoformat(timespec="seconds"))
            )
            effect_text = f"🔥 Дух команды повышен на <b>+{round(actual_delta, 1)}</b>."

        await db.execute(
            "INSERT INTO shop_purchases (team_name, item_key, price, effect_value, bought_by, bought_at) "
            "VALUES (?,?,?,?,?,?)",
            (real_name, item_key, price, effect_value, initiated_by,
             datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()

    return True, (
        f"✅ Команда «{md_escape(real_name)}» купила «{item['title']}» за {fmt_money(price)}.\n"
        f"{effect_text}"
    )


async def _shop_execute_purchase(call: CallbackQuery, item_key: str, team_id: int) -> None:
    team_name = await db_get_team_name_by_id(team_id)
    if not team_name:
        await call.answer("⛔ Команда не найдена.", show_alert=True)
        return
    ok, text = await db_shop_buy(team_name, item_key, call.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 В магазин", callback_data="shop|menu")]
    ])
    if ok:
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer("Готово!")
    else:
        await call.answer(text, show_alert=True)


@dp.message(Command("shop"))
async def cmd_shop(msg: Message) -> None:
    """/shop — магазин улучшений команды: буткемп (винрейт), устройства
    (сыгранность), новые девайсы (дух). Покупка списывается с баланса команды."""
    await msg.answer(shop_menu_text(), reply_markup=shop_menu_kb())


@dp.callback_query(F.data == "shop|menu")
async def cq_shop_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(shop_menu_text(), reply_markup=shop_menu_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("shop|item|"))
async def cq_shop_item(call: CallbackQuery) -> None:
    item_key = call.data.split("|")[2]
    item = SHOP_ITEMS.get(item_key)
    if not item:
        await call.answer("⛔ Товар не найден.", show_alert=True)
        return

    teams = await db_get_leader_teams(call.from_user.id)
    if not teams:
        await call.answer("⛔ Вы не являетесь лидером ни одной команды лиги.", show_alert=True)
        return

    if len(teams) == 1:
        team_id = await db_get_team_id_by_name(teams[0])
        await _shop_execute_purchase(call, item_key, team_id)
        return

    rows = []
    for tname in teams:
        tid = await db_get_team_id_by_name(tname)
        rows.append([InlineKeyboardButton(text=tname, callback_data=f"shop|buy|{item_key}|{tid}")])
    rows.append([InlineKeyboardButton(text="🔙 В магазин", callback_data="shop|menu")])
    await call.message.edit_text(
        f"Вы лидер нескольких команд. За какую купить «{item['title']}»?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("shop|buy|"))
async def cq_shop_buy(call: CallbackQuery) -> None:
    _, _, item_key, team_id_s = call.data.split("|")
    await _shop_execute_purchase(call, item_key, int(team_id_s))


@dp.message(Command("balance"))
async def cmd_balance(msg: Message) -> None:
    """/balance — баланс своей команды (своих команд).
    /balance <Название команды> — посмотреть баланс любой команды лиги."""
    parts = msg.text.split(maxsplit=1)

    if len(parts) == 2 and parts[1].strip():
        team_query = parts[1].strip()
        real_name = await db_find_team_name_ci(team_query)
        if not real_name:
            await msg.answer(f"❌ Команда «{md_escape(team_query)}» не найдена.")
            return
        balance = await db_get_team_balance(real_name)
        await msg.answer(
            f"💰 Баланс команды <b>{md_escape(real_name)}</b>: <b>{fmt_money(balance)}</b>"
        )
        return

    teams = await db_get_leader_teams(msg.from_user.id)
    if not teams:
        await msg.answer(
            "⛔ Вы не менеджер ни одной команды лиги.\n"
            "Чтобы посмотреть баланс другой команды, напишите: "
            "<code>/balance Название команды</code>"
        )
        return

    lines = ["💰 <b>Баланс ваших команд:</b>", ""]
    for tname in teams:
        balance = await db_get_team_balance(tname)
        lines.append(f"  • <b>{md_escape(tname)}</b> — {fmt_money(balance)}")
    await msg.answer("\n".join(lines))


BALANCE_TOP_PAGE_SIZE = 20


def _kb_balance_top(page: int, has_next: bool) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"bal|top|{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"bal|top|{page+1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="bal|top|close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_balance_top_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    offset = page * BALANCE_TOP_PAGE_SIZE
    # запрашиваем на 1 больше, чтобы понять, есть ли следующая страница
    rows = await db_balance_top_page(offset, BALANCE_TOP_PAGE_SIZE + 1)
    has_next = len(rows) > BALANCE_TOP_PAGE_SIZE
    rows = rows[:BALANCE_TOP_PAGE_SIZE]

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 <b>Топ команд по балансу</b>", "<i>(без учёта академий/фарм-составов)</i>", ""]
    if not rows and page == 0:
        lines.append("Команд пока нет.")
    for i, (name, balance) in enumerate(rows, start=offset + 1):
        place = medals.get(i, f"{i}.")
        lines.append(f"{place} <b>{md_escape(name)}</b> — {fmt_money(balance)}")
    if page > 0 and not rows:
        lines.append("Дальше команд нет.")

    text = "\n".join(lines)
    kb = _kb_balance_top(page, has_next)
    return text, kb


@dp.message(Command("balance_top"))
async def cmd_balance_top(msg: Message) -> None:
    """/balance_top — топ команд лиги по балансу, с бесконечной пагинацией
    кнопками «Вперёд ▶️» / «◀️ Назад» (показывает все команды, не только 10)."""
    text, kb = await _render_balance_top_page(0)
    await msg.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("bal|top|"))
async def cq_balance_top_page(call: CallbackQuery) -> None:
    action = call.data.split("|")[2]
    if action == "close":
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer()
        return

    page = int(action)
    text, kb = await _render_balance_top_page(page)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await call.answer()


@dp.message(Command("balance_transfer"))
async def cmd_balance_transfer(msg: Message) -> None:
    """/balance_transfer <сумма> <команда-получатель> — перевести деньги
    от своей команды другой команде лиги."""
    parts = msg.text.split(maxsplit=2)
    if len(parts) != 3:
        await msg.answer(
            "❌ Формат: <code>/balance_transfer Сумма Название_команды</code>\n"
            "Например: <code>/balance_transfer 2000 Dragons</code>"
        )
        return

    amount_raw, to_team_raw = parts[1].strip(), parts[2].strip()
    try:
        amount = int(float(amount_raw.replace(",", ".").replace(" ", "")))
    except ValueError:
        await msg.answer("❌ Сумма перевода должна быть числом. Пример: <code>/balance_transfer 2000 Dragons</code>")
        return
    if amount <= 0:
        await msg.answer("❌ Сумма перевода должна быть больше нуля.")
        return

    to_real_name = await db_find_team_name_ci(to_team_raw)
    if not to_real_name:
        await msg.answer(f"❌ Команда-получатель «{md_escape(to_team_raw)}» не найдена.")
        return

    my_teams = await db_get_leader_teams(msg.from_user.id)
    if not my_teams:
        await msg.answer("⛔ Вы не менеджер ни одной команды лиги — переводить деньги не от кого.")
        return

    if len(my_teams) > 1:
        await msg.answer(
            f"Вы менеджер нескольких команд. С какой команды перевести "
            f"{fmt_money(amount)} команде «{md_escape(to_real_name)}»?",
            reply_markup=balance_pick_team_kb(my_teams, to_real_name, amount)
        )
        return

    from_team = my_teams[0]
    ok, err = await db_transfer_balance(from_team, to_real_name, float(amount), msg.from_user.id)
    if not ok:
        await msg.answer(f"❌ {err}")
        return

    new_from = await db_get_team_balance(from_team)
    new_to = await db_get_team_balance(to_real_name)
    await msg.answer(
        f"✅ Перевод выполнен: <b>{md_escape(from_team)}</b> → <b>{md_escape(to_real_name)}</b>, "
        f"сумма {fmt_money(amount)}.\n\n"
        f"💰 Баланс <b>{md_escape(from_team)}</b>: {fmt_money(new_from)}\n"
        f"💰 Баланс <b>{md_escape(to_real_name)}</b>: {fmt_money(new_to)}"
    )


@dp.callback_query(F.data.startswith("bal|from|"))
async def cq_balance_pick_from(call: CallbackQuery) -> None:
    """Менеджер нескольких команд выбрал, с какой команды перевести деньги."""
    _, _, from_team, to_team, amount_s = call.data.split("|")
    amount = int(amount_s)

    # На всякий случай перепроверим, что это действительно его команда
    my_teams = await db_get_leader_teams(call.from_user.id)
    if from_team not in my_teams:
        await call.answer("⛔ Это не ваша команда.", show_alert=True)
        return

    ok, err = await db_transfer_balance(from_team, to_team, float(amount), call.from_user.id)
    if not ok:
        await call.message.edit_text(f"❌ {err}")
        await call.answer()
        return

    new_from = await db_get_team_balance(from_team)
    new_to = await db_get_team_balance(to_team)
    await call.message.edit_text(
        f"✅ Перевод выполнен: <b>{md_escape(from_team)}</b> → <b>{md_escape(to_team)}</b>, "
        f"сумма {fmt_money(amount)}.\n\n"
        f"💰 Баланс <b>{md_escape(from_team)}</b>: {fmt_money(new_from)}\n"
        f"💰 Баланс <b>{md_escape(to_team)}</b>: {fmt_money(new_to)}"
    )
    await call.answer("Готово.")


@dp.message(F.text.regexp(r"(?i)^баланс\s+.+"))
async def msg_balance_lookup(msg: Message) -> None:
    """Текстовое сообщение вида «баланс Название команды» — быстрый просмотр чужого баланса."""
    team_query = msg.text.split(maxsplit=1)[1].strip()
    real_name = await db_find_team_name_ci(team_query)
    if not real_name:
        await msg.answer(f"❌ Команда «{md_escape(team_query)}» не найдена.")
        return
    balance = await db_get_team_balance(real_name)
    await msg.answer(
        f"💰 Баланс команды <b>{md_escape(real_name)}</b>: <b>{fmt_money(balance)}</b>"
    )




# ── СВОБОДНЫЕ АГЕНТЫ ─────────────────────────────────────────────────────────

@dp.message(Command("free_agents"))
async def cmd_free_agents(msg: Message) -> None:
    """/free_agents — список свободных агентов лиги (доступно всем)."""
    rows = await db_get_players(FREE_AGENT_TEAM_NAME)
    if not rows:
        await msg.answer(
            "🆓 <b>Свободные агенты</b>\n\n"
            "Пул свободных агентов сейчас пуст.\n"
            "Админ может создать нового свободного агента: "
            "<code>/create_free_agent Ник Рейтинг Роль [Возраст]</code>"
        )
        return

    role_icons = {
        "Rifler": "🔫", "AWPer": "🎯", "Support": "🛡",
        "Entry Fragger": "⚡", "Lurker": "🕵", "IGL": "📢", "Coach": "🎓",
    }
    lines = ["🆓 <b>Свободные агенты лиги</b>", ""]
    for nick, role, rating, is_reserve, age in rows:
        icon = role_icons.get(role, "•")
        lines.append(f"{icon} <b>{md_escape(str(nick))}</b> — {rating:.1f} [{md_escape(str(role))}, {age} лет]")
    lines.append("")
    lines.append(
        "<i>Подписать в команду (лидер команды):</i>\n"
        "<code>/sign_free_agent Команда Ник</code>"
    )
    await safe_send_long(msg, "\n".join(lines))


@dp.message(Command("create_free_agent"))
async def cmd_create_free_agent(msg: Message) -> None:
    """/create_free_agent Ник Рейтинг Роль [Возраст] — создать нового игрока сразу
    в пуле свободных агентов, без привязки к команде. Только для админов."""
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) < 4:
        await msg.answer(
            "❌ Формат: <code>/create_free_agent Ник Рейтинг Роль [Возраст]</code>\n"
            f"Доступные роли: {', '.join(ROLES)}\n"
            "Рейтинг — от 15.0 до 32.0. Возраст — от 14 до 40 (необязательно, по умолчанию 18)."
        )
        return

    nick = parts[1]
    try:
        rating = float(parts[2])
        if not (15.0 <= rating <= 32.0):
            raise ValueError
    except ValueError:
        await msg.answer("❌ Рейтинг должен быть числом от 15.0 до 32.0.")
        return

    age = 18
    role_tokens = parts[3:]
    if role_tokens:
        try:
            maybe_age = int(role_tokens[-1])
            if 14 <= maybe_age <= 40:
                age = maybe_age
                role_tokens = role_tokens[:-1]
        except ValueError:
            pass
    role = " ".join(role_tokens)

    if role not in ROLES:
        await msg.answer(f"❌ Роль «{md_escape(str(role))}» не существует.\nДоступные роли: {', '.join(ROLES)}")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM players WHERE nick=? COLLATE NOCASE AND team_name=?",
            (nick, FREE_AGENT_TEAM_NAME)
        ) as cursor:
            if await cursor.fetchone():
                await msg.answer(f"❌ Свободный агент «{md_escape(str(nick))}» уже существует.")
                return
        await db.execute(
            "INSERT INTO players (nick, team_name, role, rating, is_reserve, age) VALUES (?,?,?,?,0,?)",
            (nick, FREE_AGENT_TEAM_NAME, role, rating, age)
        )
        await db.commit()

    await msg.answer(
        f"🆓 Свободный агент создан: <b>{md_escape(str(nick))}</b> — {rating:.1f} "
        f"[{md_escape(str(role))}, {age} лет]\n"
        f"Подписать в команду: <code>/sign_free_agent Команда {md_escape(str(nick))}</code>"
    )


@dp.message(Command("sign_free_agent"))
async def cmd_sign_free_agent(msg: Message) -> None:
    """/sign_free_agent Команда Ник — лидер команды подписывает свободного
    агента в состав. Доступно лидеру указанной команды (или админу). Лимит:
    не более FREE_AGENT_SIGN_LIMIT_PER_TOUR (2) свободных агентов на команду
    за тур — сбрасывается при завершении любого турнира через /donetour."""
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer(
            "❌ Формат: <code>/sign_free_agent Команда Ник</code>\n"
            f"Например: <code>/sign_free_agent NaVi f0rest</code>\n"
            f"Список свободных агентов: /free_agents"
        )
        return

    team_name, nick = parts[1], parts[2]

    real_team = await db_find_team_name_ci(team_name)
    if not real_team:
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    is_admin = await db_is_admin(msg.from_user.id)
    if not is_admin and not await db_is_team_leader(msg.from_user.id, real_team):
        await msg.answer("⛔ Подписывать свободных агентов может только лидер этой команды (или админ).")
        return

    if not is_admin:
        signed_so_far = await db_get_free_agent_signings_count(real_team)
        if signed_so_far >= FREE_AGENT_SIGN_LIMIT_PER_TOUR:
            await msg.answer(
                f"⛔ Команда «{md_escape(str(real_team))}» уже подписала "
                f"{FREE_AGENT_SIGN_LIMIT_PER_TOUR} свободных агента в этом туре — "
                f"больше нельзя. Лимит обновится, когда завершится любой турнир (/donetour)."
            )
            return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, nick FROM players WHERE nick=? COLLATE NOCASE AND team_name=?",
            (nick, FREE_AGENT_TEAM_NAME)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await msg.answer(f"❌ Свободный агент «{md_escape(str(nick))}» не найден. Список: /free_agents")
        return
    if len(rows) > 1:
        await msg.answer("⚠️ Найдено несколько свободных агентов с таким ником. Уточните ник.")
        return

    player_id, real_nick = rows[0]

    ok = await db_transfer_player(real_nick, FREE_AGENT_TEAM_NAME, real_team, loan=False)
    if not ok:
        await msg.answer("❌ Не удалось подписать игрока (возможно, он уже покинул пул свободных агентов).")
        return

    signed_count = await db_increment_free_agent_signings(real_team)

    await msg.answer(
        f"✅ <b>{md_escape(real_nick)}</b> подписан в команду <b>{md_escape(real_team)}</b>!\n"
        f"🆓 Подписано свободных агентов в этом туре: <b>{signed_count}/{FREE_AGENT_SIGN_LIMIT_PER_TOUR}</b>"
    )


@dp.message(Command("set_leader"))
async def cmd_set_leader(msg: Message) -> None:
    """
    /set_leader Команда [telegram_id]
    Можно также ответить (reply) на сообщение игрока — тогда telegram_id брать не нужно.
    Только для админов/владельца — назначает лидера команды, который получает доступ
    к панели управления командой (кнопка «⚙️ Панель управления» в карточке /team).
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split()
    target_id = None
    target_username = None

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id = msg.reply_to_message.from_user.id
        target_username = msg.reply_to_message.from_user.username
        if len(parts) < 2:
            await msg.answer("❌ Формат: <code>/set_leader Команда</code> (ответом на сообщение игрока)")
            return
        team_name = " ".join(parts[1:])
    else:
        if len(parts) < 3:
            await msg.answer(
                "❌ Формат:\n"
                "<code>/set_leader Команда telegram_id</code>\n"
                "или ответь этой командой на сообщение игрока в чате."
            )
            return
        try:
            target_id = int(parts[-1])
        except ValueError:
            await msg.answer("❌ telegram_id должен быть числом.")
            return
        team_name = " ".join(parts[1:-1])

    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    await db_set_leader(team_name, target_id, target_username)
    uname = f"@{target_username}" if target_username else str(target_id)
    await msg.answer(
        f"👑 Лидером команды <b>{md_escape(str(team_name))}</b> назначен <b>{md_escape(uname)}</b>.\n"
        f"Теперь он может открыть панель управления через <code>/team {md_escape(str(team_name))}</code> "
        f"(кнопка «⚙️ Панель управления»)."
    )
    try:
        await bot.send_message(
            target_id,
            f"👑 Тебя назначили лидером команды <b>{md_escape(str(team_name))}</b>!\n"
            f"Открой панель управления: <code>/team {md_escape(str(team_name))}</code> "
            f"(кнопка «⚙️ Панель управления»)."
        )
    except Exception:
        pass


@dp.message(Command("remove_leader"))
async def cmd_remove_leader(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/remove_leader Команда</code>")
        return
    team_name = parts[1]
    if await db_remove_leader(team_name):
        await msg.answer(f"✅ Лидер команды <b>{md_escape(str(team_name))}</b> снят.")
    else:
        await msg.answer(f"❌ У команды «{md_escape(str(team_name))}» не было лидера.")


@dp.message(Command("leaders"))
async def cmd_leaders(msg: Message) -> None:
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    rows = await db_list_leaders()
    if not rows:
        await msg.answer("Лидеры команд пока не назначены.")
        return
    lines = ["👑 <b>Лидеры команд:</b>", ""]
    for team_name, tg_id, username in rows:
        uname = f"@{username}" if username else str(tg_id)
        lines.append(f"• <b>{md_escape(str(team_name))}</b> — {md_escape(uname)} (<code>{tg_id}</code>)")
    await safe_send_long(msg, "\n".join(lines))


def _kb_roster(team_id: int, roster: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for pid, nick, role, is_reserve in roster:
        mark = "🪑" if is_reserve == 1 else "✅"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {nick} — {role}",
            callback_data=f"lp|player|{pid}"
        )])
    rows.append([InlineKeyboardButton(text="🔄 Трансфер игрока", callback_data=f"lp|transfer|{team_id}")])
    rows.append([InlineKeyboardButton(text="📅 Аренда игрока", callback_data=f"lp|loan|{team_id}")])
    rows.append([InlineKeyboardButton(text="🔀 Обмен игроками", callback_data=f"lp|swap|{team_id}")])
    rows.append([InlineKeyboardButton(text="💰 Перевод денег", callback_data=f"lp|balxfer|{team_id}|0")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="lp|close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_player(player_id: int, team_id: int, is_reserve: int) -> InlineKeyboardMarkup:
    toggle_text = "✅ Перевести в основу" if is_reserve == 1 else "🪑 Перевести в резерв"
    rows = [
        [InlineKeyboardButton(text=toggle_text, callback_data=f"lp|toggle|{player_id}")],
        [InlineKeyboardButton(text="🎭 Сменить роль", callback_data=f"lp|role|{player_id}")],
        [InlineKeyboardButton(text="🔄 Трансфер этого игрока", callback_data=f"lp|trplayer|{player_id}")],
        [InlineKeyboardButton(text="📅 Отдать в аренду", callback_data=f"lp|loanplayer|{player_id}")],
        [InlineKeyboardButton(text="🔀 Обменять этого игрока", callback_data=f"lp|swapplayer|{player_id}")],
        [InlineKeyboardButton(text="🔙 Назад к составу", callback_data=f"lp|roster|{team_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_roles(player_id: int, team_id: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, role in enumerate(PANEL_ROLES):
        row.append(InlineKeyboardButton(text=role, callback_data=f"lp|setrole|{player_id}|{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|player|{player_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_transfer_players(team_id: int, roster: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for pid, nick, role, is_reserve in roster:
        mark = "🪑" if is_reserve == 1 else "✅"
        rows.append([InlineKeyboardButton(text=f"{mark} {nick} — {role}", callback_data=f"lp|trplayer|{pid}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|roster|{team_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


BALANCE_PANEL_TEAMS_PAGE_SIZE = 8
BALANCE_TRANSFER_AMOUNTS = [500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000]


def _kb_balxfer_recipients(team_id: int, page: int, teams: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Кнопки выбора команды-получателя перевода, с пагинацией."""
    start = page * BALANCE_PANEL_TEAMS_PAGE_SIZE
    page_teams = teams[start:start + BALANCE_PANEL_TEAMS_PAGE_SIZE]

    rows = []
    for tid, tname in page_teams:
        rows.append([InlineKeyboardButton(text=tname, callback_data=f"lp|balxfer_amt|{team_id}|{tid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"lp|balxfer|{team_id}|{page-1}"))
    if start + BALANCE_PANEL_TEAMS_PAGE_SIZE < len(teams):
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"lp|balxfer|{team_id}|{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🔙 В панель управления", callback_data=f"lp|roster|{team_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_balxfer_amounts(team_id: int, to_team_id: int) -> InlineKeyboardMarkup:
    """Кнопки выбора суммы перевода."""
    rows = []
    row: list[InlineKeyboardButton] = []
    for amt in BALANCE_TRANSFER_AMOUNTS:
        row.append(InlineKeyboardButton(
            text=fmt_money(amt), callback_data=f"lp|balxfer_go|{team_id}|{to_team_id}|{amt}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Другая сумма", callback_data=f"lp|balxfer_custom|{team_id}|{to_team_id}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|balxfer|{team_id}|0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_transfer_teams(player_id: int, own_team_id: int, teams: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tid, tname in teams:
        if tid == own_team_id:
            continue
        row.append(InlineKeyboardButton(text=tname, callback_data=f"lp|trteam|{player_id}|{tid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|player|{player_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_loan_players(team_id: int, roster: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for pid, nick, role, is_reserve in roster:
        mark = "🪑" if is_reserve == 1 else "✅"
        rows.append([InlineKeyboardButton(text=f"{mark} {nick} — {role}", callback_data=f"lp|loanplayer|{pid}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|roster|{team_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_loan_teams(player_id: int, own_team_id: int, teams: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tid, tname in teams:
        if tid == own_team_id:
            continue
        row.append(InlineKeyboardButton(text=tname, callback_data=f"lp|loanteam|{player_id}|{tid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|player|{player_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_loan_tours(player_id: int, target_team_id: int, tours: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tour_id, tour_name in tours:
        row.append(InlineKeyboardButton(text=tour_name, callback_data=f"lp|loantour|{player_id}|{target_team_id}|{tour_id}"))
        if len(row) == 1:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|loanplayer|{player_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_swap_players(team_id: int, roster: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for pid, nick, role, is_reserve in roster:
        mark = "🪑" if is_reserve == 1 else "✅"
        rows.append([InlineKeyboardButton(text=f"{mark} {nick} — {role}", callback_data=f"lp|swapplayer|{pid}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|roster|{team_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_swap_teams(player_id: int, own_team_id: int, teams: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tid, tname in teams:
        if tid == own_team_id:
            continue
        row.append(InlineKeyboardButton(text=tname, callback_data=f"lp|swapteam|{player_id}|{tid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|player|{player_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_swap_target_players(player_id: int, roster: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for pid, nick, role, is_reserve in roster:
        mark = "🪑" if is_reserve == 1 else "✅"
        rows.append([InlineKeyboardButton(text=f"{mark} {nick} — {role}", callback_data=f"lp|swaptarget|{player_id}|{pid}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"lp|swapplayer|{player_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_request_decision(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Согласиться", callback_data=f"lp|treq|acc|{request_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"lp|treq|dec|{request_id}"),
    ]])


async def _panel_can_manage(user_id: int, team_name: str) -> bool:
    """Лидер своей команды или админ/владелец может управлять командой через панель."""
    if await db_is_admin(user_id):
        return True
    return await db_is_team_leader(user_id, team_name)


async def _panel_route_request(cq: CallbackQuery, request_id: int, to_team: str,
                                header: str, prompt: str) -> None:
    """Отправляет заявку (трансфер/аренда/обмен) лидеру команды-получателя,
    либо, если у неё нет лидера, организаторам."""
    target_leader = await db_get_team_leader(to_team)
    mode = "leader" if target_leader else "org"
    notified = []
    if mode == "leader":
        target_id = target_leader[0]
        try:
            sent = await bot.send_message(
                target_id, header + prompt, reply_markup=_kb_request_decision(request_id)
            )
            notified.append(f"{sent.chat.id}:{sent.message_id}")
            await cq.message.edit_text(
                header + "⏳ Запрос отправлен лидеру команды-получателя. Ждём его решения."
            )
        except Exception:
            await cq.message.edit_text(
                header + "⚠️ Не удалось связаться с лидером команды-получателя "
                "(он ещё не запускал бота). Запрос переадресован организаторам."
            )
            mode = "org"
    if mode == "org":
        admin_ids = {OWNER_ID}
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT telegram_id FROM admins") as cursor:
                async for row in cursor:
                    admin_ids.add(row[0])
        for admin_id in admin_ids:
            if not admin_id:
                continue
            try:
                sent = await bot.send_message(
                    admin_id, header + "У команды-получателя нет лидера — решение за организатором.",
                    reply_markup=_kb_request_decision(request_id)
                )
                notified.append(f"{sent.chat.id}:{sent.message_id}")
            except Exception:
                pass
        if not target_leader:
            await cq.message.edit_text(
                header + "⏳ У команды-получателя нет лидера — запрос отправлен организаторам."
            )

    if notified:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE transfer_requests SET notify_chat_ids=? WHERE id=?",
                (",".join(notified), request_id)
            )
            await db.commit()

    await cq.answer("Запрос отправлен")


@dp.callback_query(F.data.startswith("lp|"))
async def cb_leader_panel(cq: CallbackQuery) -> None:
    data = cq.data.split("|")
    action = data[1]
    user_id = cq.from_user.id

    if action == "close":
        try:
            await cq.message.delete()
        except Exception:
            pass
        await cq.answer()
        return

    if action == "roster":
        team_id = int(data[2])
        team_name = await db_get_team_name_by_id(team_id)
        if not team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        roster = await db_get_roster_for_panel(team_name)
        if not roster:
            await cq.answer("В команде нет игроков.", show_alert=True)
            return
        await cq.message.edit_text(
            f"🎛 <b>Панель управления командой «{md_escape(str(team_name))}»</b>\n"
            f"Выбери игрока, чтобы изменить его резерв/основу, роль или сделать трансфер:",
            reply_markup=_kb_roster(team_id, roster)
        )
        await cq.answer()
        return

    if action == "player":
        player_id = int(data[2])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        _, nick, team_name, role, rating, is_reserve, age = player
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        team_id = await db_get_team_id_by_name(team_name)
        status_text = "Резерв 🪑" if is_reserve == 1 else "Основа ✅"
        await cq.message.edit_text(
            f"👤 <b>{md_escape(str(nick))}</b>\n"
            f"Команда: {md_escape(str(team_name))}\n"
            f"Роль: {md_escape(str(role))}\n"
            f"Статус: {status_text}\n"
            f"Рейтинг: {rating:.1f} | Возраст: {age}",
            reply_markup=_kb_player(player_id, team_id, is_reserve)
        )
        await cq.answer()
        return

    if action == "toggle":
        player_id = int(data[2])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        result = await db_toggle_reserve_by_id(player_id)
        if not result:
            await cq.answer("⛔ Нельзя менять статус тренера.", show_alert=True)
            return
        _, new_status, nick, team_name = result
        status_text = "основной состав ✅" if new_status == 0 else "резерв 🪑"
        await cq.answer(f"{nick} переведён в {status_text}")
        # Обновляем карточку игрока
        team_id = await db_get_team_id_by_name(team_name)
        await cq.message.edit_reply_markup(reply_markup=_kb_player(player_id, team_id, new_status))
        return

    if action == "role":
        player_id = int(data[2])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        team_id = await db_get_team_id_by_name(team_name)
        await cq.message.edit_text(
            f"🎭 Выбери новую роль для <b>{md_escape(str(player[1]))}</b>:",
            reply_markup=_kb_roles(player_id, team_id)
        )
        await cq.answer()
        return

    if action == "setrole":
        player_id = int(data[2])
        role_idx = int(data[3])
        if role_idx < 0 or role_idx >= len(PANEL_ROLES):
            await cq.answer("Некорректная роль.", show_alert=True)
            return
        role = PANEL_ROLES[role_idx]
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        result = await db_set_role_by_id(player_id, role)
        if not result:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        nick, team_name = result
        await cq.answer(f"Роль {nick} изменена на {role}")
        team_id = await db_get_team_id_by_name(team_name)
        player = await db_get_player_by_id(player_id)
        is_reserve = player[5]
        await cq.message.edit_text(
            f"👤 <b>{md_escape(str(nick))}</b>\n"
            f"Команда: {md_escape(str(team_name))}\n"
            f"Роль: {md_escape(str(role))}\n"
            f"Статус: {'Резерв 🪑' if is_reserve == 1 else 'Основа ✅'}",
            reply_markup=_kb_player(player_id, team_id, is_reserve)
        )
        return

    if action == "transfer":
        team_id = int(data[2])
        team_name = await db_get_team_name_by_id(team_id)
        if not team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        roster = await db_get_roster_for_panel(team_name)
        await cq.message.edit_text(
            f"🔄 Кого хочешь трансферить из «{md_escape(str(team_name))}»?",
            reply_markup=_kb_transfer_players(team_id, roster)
        )
        await cq.answer()
        return

    if action == "trplayer":
        player_id = int(data[2])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        team_id = await db_get_team_id_by_name(team_name)
        teams = await db_get_all_teams_id_name()
        if len(teams) < 2:
            await cq.answer("Нет других команд для трансфера.", show_alert=True)
            return
        await cq.message.edit_text(
            f"🔄 Выбери команду, в которую перейдёт <b>{md_escape(str(player[1]))}</b>:",
            reply_markup=_kb_transfer_teams(player_id, team_id, teams)
        )
        await cq.answer()
        return

    if action == "loan":
        team_id = int(data[2])
        team_name = await db_get_team_name_by_id(team_id)
        if not team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        roster = await db_get_roster_for_panel(team_name)
        await cq.message.edit_text(
            f"📅 Кого хочешь отдать в аренду из «{md_escape(str(team_name))}»?",
            reply_markup=_kb_loan_players(team_id, roster)
        )
        await cq.answer()
        return

    if action == "loanplayer":
        player_id = int(data[2])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        team_id = await db_get_team_id_by_name(team_name)
        teams = await db_get_all_teams_id_name()
        if len(teams) < 2:
            await cq.answer("Нет других команд для аренды.", show_alert=True)
            return
        await cq.message.edit_text(
            f"📅 Выбери команду, в которую <b>{md_escape(str(player[1]))}</b> перейдёт в аренду:",
            reply_markup=_kb_loan_teams(player_id, team_id, teams)
        )
        await cq.answer()
        return

    if action == "loanteam":
        player_id = int(data[2])
        target_team_id = int(data[3])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, name FROM tournament_registry WHERE is_done=0 ORDER BY id DESC"
            ) as cursor:
                tours = await cursor.fetchall()
        if not tours:
            await cq.answer(
                "Нет активных турниров — сначала добавь турнир через /addtour.", show_alert=True
            )
            return
        await cq.message.edit_text(
            f"📅 До какого турнира отдать <b>{md_escape(str(player[1]))}</b> в аренду?",
            reply_markup=_kb_loan_tours(player_id, target_team_id, tours)
        )
        await cq.answer()
        return

    if action == "loantour":
        player_id = int(data[2])
        target_team_id = int(data[3])
        tour_id = int(data[4])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        _, nick, from_team, role, rating, is_reserve, age = player
        if not await _panel_can_manage(user_id, from_team):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        to_team = await db_get_team_name_by_id(target_team_id)
        if not to_team:
            await cq.answer("Команда не найдена.", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT name, is_done FROM tournament_registry WHERE id=?", (tour_id,)
            ) as cursor:
                tour_row = await cursor.fetchone()
        if not tour_row or tour_row[1] == 1:
            await cq.answer("Турнир не найден или уже завершён.", show_alert=True)
            return
        until_tour = tour_row[0]

        target_leader = await db_get_team_leader(to_team)
        mode = "leader" if target_leader else "org"
        request_id = await db_create_transfer_request(
            player_id, nick, from_team, to_team, user_id, mode,
            request_type="loan", until_tour=until_tour
        )
        header = (
            f"📅 <b>Запрос на аренду</b>\n\n"
            f"Игрок: <b>{md_escape(str(nick))}</b>\n"
            f"Из команды: <b>{md_escape(str(from_team))}</b>\n"
            f"В команду: <b>{md_escape(str(to_team))}</b>\n"
            f"До конца турнира: <b>{md_escape(str(until_tour))}</b>\n\n"
        )
        await _panel_route_request(
            cq, request_id, to_team, header, "Лидер другой команды предлагает аренду игрока. Согласен?"
        )
        return

    if action == "swap":
        team_id = int(data[2])
        team_name = await db_get_team_name_by_id(team_id)
        if not team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        roster = await db_get_roster_for_panel(team_name)
        await cq.message.edit_text(
            f"🔀 Кого хочешь обменять из «{md_escape(str(team_name))}»?",
            reply_markup=_kb_swap_players(team_id, roster)
        )
        await cq.answer()
        return

    if action == "swapplayer":
        player_id = int(data[2])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        team_id = await db_get_team_id_by_name(team_name)
        teams = await db_get_all_teams_id_name()
        if len(teams) < 2:
            await cq.answer("Нет других команд для обмена.", show_alert=True)
            return
        await cq.message.edit_text(
            f"🔀 С какой командой обменять <b>{md_escape(str(player[1]))}</b>?",
            reply_markup=_kb_swap_teams(player_id, team_id, teams)
        )
        await cq.answer()
        return

    if action == "swapteam":
        player_id = int(data[2])
        target_team_id = int(data[3])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        team_name = player[2]
        if not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        to_team = await db_get_team_name_by_id(target_team_id)
        if not to_team:
            await cq.answer("Команда не найдена.", show_alert=True)
            return
        target_roster = await db_get_roster_for_panel(to_team)
        if not target_roster:
            await cq.answer(f"В команде «{to_team}» нет игроков для обмена.", show_alert=True)
            return
        await cq.message.edit_text(
            f"🔀 На кого из «{md_escape(str(to_team))}» обменять <b>{md_escape(str(player[1]))}</b>?",
            reply_markup=_kb_swap_target_players(player_id, target_roster)
        )
        await cq.answer()
        return

    if action == "swaptarget":
        player_id = int(data[2])
        partner_player_id = int(data[3])
        player = await db_get_player_by_id(player_id)
        partner = await db_get_player_by_id(partner_player_id)
        if not player or not partner:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        _, nick, from_team, role, rating, is_reserve, age = player
        _, partner_nick, to_team, p_role, p_rating, p_is_reserve, p_age = partner
        if not await _panel_can_manage(user_id, from_team):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        if from_team == to_team:
            await cq.answer("Игроки уже в одной команде.", show_alert=True)
            return

        target_leader = await db_get_team_leader(to_team)
        mode = "leader" if target_leader else "org"
        request_id = await db_create_transfer_request(
            player_id, nick, from_team, to_team, user_id, mode,
            request_type="swap", partner_player_id=partner_player_id, partner_nick=partner_nick
        )
        header = (
            f"🔀 <b>Запрос на обмен</b>\n\n"
            f"<b>{md_escape(str(nick))}</b> ({md_escape(str(from_team))}) ↔ "
            f"<b>{md_escape(str(partner_nick))}</b> ({md_escape(str(to_team))})\n\n"
        )
        await _panel_route_request(
            cq, request_id, to_team, header, "Лидер другой команды предлагает обмен игроками. Согласен?"
        )
        return

    if action == "trteam":
        player_id = int(data[2])
        target_team_id = int(data[3])
        player = await db_get_player_by_id(player_id)
        if not player:
            await cq.answer("Игрок не найден.", show_alert=True)
            return
        _, nick, from_team, role, rating, is_reserve, age = player
        if not await _panel_can_manage(user_id, from_team):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        to_team = await db_get_team_name_by_id(target_team_id)
        if not to_team:
            await cq.answer("Команда не найдена.", show_alert=True)
            return

        target_leader = await db_get_team_leader(to_team)
        mode = "leader" if target_leader else "org"
        request_id = await db_create_transfer_request(player_id, nick, from_team, to_team, user_id, mode)

        header = (
            f"🔄 <b>Запрос на трансфер</b>\n\n"
            f"Игрок: <b>{md_escape(str(nick))}</b>\n"
            f"Из команды: <b>{md_escape(str(from_team))}</b>\n"
            f"В команду: <b>{md_escape(str(to_team))}</b>\n\n"
        )
        await _panel_route_request(
            cq, request_id, to_team, header, "Лидер другой команды предлагает трансфер. Согласен?"
        )
        return

    if action == "treq":
        decision = data[2]
        request_id = int(data[3])
        request = await db_get_transfer_request(request_id)
        if not request:
            await cq.answer("Заявка не найдена.", show_alert=True)
            return
        (req_id, player_id, nick, from_team, to_team, initiated_by, mode, status,
         request_type, until_tour, partner_player_id, partner_nick) = request
        request_type = request_type or "transfer"
        if status != "pending":
            await cq.answer("Эта заявка уже обработана.", show_alert=True)
            return

        allowed = await db_is_admin(user_id)
        if not allowed and mode == "leader":
            allowed = await db_is_team_leader(user_id, to_team)
        if not allowed:
            await cq.answer("⛔ Только лидер команды-получателя или организатор может решить это.", show_alert=True)
            return

        if decision == "acc":
            if request_type == "loan":
                ok = await db_transfer_player(nick, from_team, to_team, loan=True)
                if ok:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "INSERT INTO loans (nick, from_team, to_team, until_tour, returned, loaned_at) "
                            "VALUES (?,?,?,?,0,?)",
                            (nick, from_team, to_team, until_tour, datetime.now().isoformat(timespec="seconds"))
                        )
                        await db.commit()
                    await db_hltv_set_team(nick, to_team)
                    await db_set_transfer_status(request_id, "accepted")
                    result_text = (
                        f"✅ <b>Аренда оформлена!</b>\n\n"
                        f"<b>{md_escape(str(nick))}</b>: {md_escape(str(from_team))} → {md_escape(str(to_team))}\n"
                        f"📅 До конца турнира: <b>{md_escape(str(until_tour))}</b>\n"
                        f"📉 Сыгранность обеих команд −2"
                    )
                else:
                    await db_set_transfer_status(request_id, "failed")
                    result_text = (
                        f"❌ Не удалось оформить аренду — игрок «{md_escape(str(nick))}» "
                        f"уже не в команде «{md_escape(str(from_team))}»."
                    )
            elif request_type == "swap":
                ok, err = await db_swap_players(nick, from_team, partner_nick, to_team)
                if ok:
                    await db_hltv_set_team(nick, to_team)
                    await db_hltv_set_team(partner_nick, from_team)
                    await db_set_transfer_status(request_id, "accepted")
                    result_text = (
                        f"✅ <b>Обмен завершён!</b>\n\n"
                        f"<b>{md_escape(str(nick))}</b> перешёл в <b>{md_escape(str(to_team))}</b>\n"
                        f"<b>{md_escape(str(partner_nick))}</b> перешёл в <b>{md_escape(str(from_team))}</b>\n\n"
                        f"📉 Сыгранность обеих команд −3\n🔻 Дух обеих команд −25%"
                    )
                else:
                    await db_set_transfer_status(request_id, "failed")
                    result_text = f"❌ Не удалось выполнить обмен — {md_escape(str(err))}"
            else:
                ok = await db_transfer_player(nick, from_team, to_team)
                if ok:
                    await db_hltv_set_team(nick, to_team)
                    await db_set_transfer_status(request_id, "accepted")
                    result_text = (
                        f"✅ <b>Трансфер завершён!</b>\n\n"
                        f"<b>{md_escape(str(nick))}</b>: {md_escape(str(from_team))} → {md_escape(str(to_team))}\n"
                        f"📉 Сыгранность обеих команд −5\n🔻 Дух обеих команд −25%"
                    )
                else:
                    await db_set_transfer_status(request_id, "failed")
                    result_text = (
                        f"❌ Не удалось выполнить трансфер — игрок «{md_escape(str(nick))}» "
                        f"уже не в команде «{md_escape(str(from_team))}»."
                    )
        else:
            await db_set_transfer_status(request_id, "declined")
            if request_type == "loan":
                result_text = (
                    f"❌ <b>Аренда отклонена</b>\n\n"
                    f"<b>{md_escape(str(nick))}</b>: {md_escape(str(from_team))} → {md_escape(str(to_team))}"
                )
            elif request_type == "swap":
                result_text = (
                    f"❌ <b>Обмен отклонён</b>\n\n"
                    f"<b>{md_escape(str(nick))}</b> ({md_escape(str(from_team))}) ↔ "
                    f"<b>{md_escape(str(partner_nick))}</b> ({md_escape(str(to_team))})"
                )
            else:
                result_text = (
                    f"❌ <b>Трансфер отклонён</b>\n\n"
                    f"<b>{md_escape(str(nick))}</b>: {md_escape(str(from_team))} → {md_escape(str(to_team))}"
                )

        # Обновляем все уведомления (у org могло быть несколько админов)
        try:
            await cq.message.edit_text(result_text)
        except Exception:
            pass
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT notify_chat_ids FROM transfer_requests WHERE id=?", (request_id,)
                ) as cursor:
                    nrow = await cursor.fetchone()
            if nrow and nrow[0]:
                for entry in nrow[0].split(","):
                    if not entry or ":" not in entry:
                        continue
                    chat_id_s, message_id_s = entry.split(":")
                    try:
                        await bot.edit_message_text(
                            result_text, chat_id=int(chat_id_s), message_id=int(message_id_s)
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            await bot.send_message(initiated_by, result_text)
        except Exception:
            pass

        await cq.answer("Готово" if decision == "acc" else "Отклонено")
        return

    if action == "balxfer":
        team_id = int(data[2])
        page = int(data[3]) if len(data) > 3 else 0
        team_name = await db_get_team_name_by_id(team_id)
        if not team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        all_teams = await db_get_all_teams_id_name()
        candidates = [t for t in all_teams if t[0] != team_id]
        balance = await db_get_team_balance(team_name)
        await cq.message.edit_text(
            f"💰 Баланс «<b>{md_escape(str(team_name))}</b>»: <b>{fmt_money(balance)}</b>\n\n"
            f"Выбери команду-получателя перевода:",
            reply_markup=_kb_balxfer_recipients(team_id, page, candidates)
        )
        await cq.answer()
        return

    if action == "balxfer_amt":
        team_id = int(data[2])
        to_team_id = int(data[3])
        team_name = await db_get_team_name_by_id(team_id)
        to_team_name = await db_get_team_name_by_id(to_team_id)
        if not team_name or not to_team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        balance = await db_get_team_balance(team_name)
        await cq.message.edit_text(
            f"💰 Перевод: «<b>{md_escape(str(team_name))}</b>» → «<b>{md_escape(str(to_team_name))}</b>»\n"
            f"Баланс отправителя: {fmt_money(balance)}\n\n"
            f"Выбери сумму перевода:",
            reply_markup=_kb_balxfer_amounts(team_id, to_team_id)
        )
        await cq.answer()
        return

    if action == "balxfer_custom":
        team_id = int(data[2])
        to_team_id = int(data[3])
        team_name = await db_get_team_name_by_id(team_id)
        to_team_name = await db_get_team_name_by_id(to_team_id)
        if not team_name or not to_team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return
        await cq.answer(
            f"Отправь команду: /balance_transfer Сумма {to_team_name} — можно указать "
            f"любую сумму, в том числе больше 100 000.",
            show_alert=True
        )
        return

    if action == "balxfer_go":
        team_id = int(data[2])
        to_team_id = int(data[3])
        amount = int(data[4])
        team_name = await db_get_team_name_by_id(team_id)
        to_team_name = await db_get_team_name_by_id(to_team_id)
        if not team_name or not to_team_name or not await _panel_can_manage(user_id, team_name):
            await cq.answer("⛔ Нет доступа.", show_alert=True)
            return

        ok, err = await db_transfer_balance(team_name, to_team_name, float(amount), user_id)
        if not ok:
            await cq.answer(f"❌ {err}", show_alert=True)
            return

        new_from = await db_get_team_balance(team_name)
        new_to = await db_get_team_balance(to_team_name)
        await cq.message.edit_text(
            f"✅ Перевод выполнен: «<b>{md_escape(str(team_name))}</b>» → «<b>{md_escape(str(to_team_name))}</b>», "
            f"сумма {fmt_money(amount)}.\n\n"
            f"💰 Баланс «{md_escape(str(team_name))}»: {fmt_money(new_from)}\n"
            f"💰 Баланс «{md_escape(str(to_team_name))}»: {fmt_money(new_to)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В панель управления", callback_data=f"lp|roster|{team_id}")]
            ])
        )
        await cq.answer("Готово.")
        return

    await cq.answer()


@dp.message(Command("tranings", "trainings"))
async def cmd_tranings(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/tranings НазваниеКоманды</code>")
        return

    team_name = parts[1].strip()
    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return

    players = await db_get_players(team_name)
    if not players:
        await msg.answer(f"❌ В команде «{md_escape(str(team_name))}» нет игроков.")
        return

    # Потенциал по среднему возрасту основного состава
    main_roster = [(n, ro, ra, ag) for n, ro, ra, ir, ag in players if ir == 0]
    team_boost, team_stars = _team_avg_potential(main_roster)

    lines = [
        f"🏋️ <b>Тренировка команды {md_escape(str(team_name))}</b>\n",
        f"<i>Прирост определяется средним возрастом основного состава</i>",
        f"<i>Потенциал команды: {team_stars}  (+{team_boost:.1f} за тренировку)</i>\n",
    ]
    async with aiosqlite.connect(DB_PATH) as db:
        for nick, role, rating, is_reserve, age in players:
            if is_reserve == 2:
                lines.append(f"  <code>{md_escape(str(nick))}</code> (тренер)  {rating:.2f} — <i>не тренируется</i>")
                continue

            mark = " (рез.)" if is_reserve == 1 else ""

            if team_boost == 0.0:
                lines.append(f"  <code>{md_escape(str(nick))}</code>{mark}  {team_stars}  {rating:.2f} — <i>не качается</i>")
            else:
                new_rating = round(min(32.0, rating + team_boost), 2)
                await db.execute(
                    "UPDATE players SET rating=? WHERE nick=? AND team_name=?",
                    (new_rating, nick, team_name)
                )
                lines.append(f"  <code>{md_escape(str(nick))}</code>{mark}  {team_stars}  {rating:.2f} → <b>{new_rating:.2f}</b>  (+{team_boost:.1f})")
        await db.commit()

    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("addchemistry"))
async def cmd_addchemistry(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer("❌ Формат: <code>/addchemistry Команда Значение</code>\nЗначение — от 1 до 50.")
        return
    team_name = parts[1]
    try:
        amount = float(parts[2])
        if not (1.0 <= amount <= 50.0): raise ValueError
    except ValueError:
        await msg.answer("❌ Значение должно быть числом от 1 до 50.")
        return
    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE teams SET chemistry = MIN(50, chemistry + ?) WHERE name=?",
            (amount, team_name)
        )
        await db.commit()
        async with db.execute("SELECT chemistry FROM teams WHERE name=?", (team_name,)) as cursor:
            row = await cursor.fetchone()
    new_chem = row[0] if row else 0
    await msg.answer(
        f"🤝 Сыгранность <b>{md_escape(str(team_name))}</b> повышена на <b>+{amount:.0f}</b>\n"
        f"📈 Текущая сыгранность: <b>{new_chem:.0f}/50</b>"
    )


@dp.message(Command("addchemistryall"))
async def cmd_addchemistryall(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.answer(
            "❌ Формат: <code>/addchemistryall Команда1 Команда2 ... Значение</code>\n"
            "Значение — от 1 до 50. Команд можно указать любое количество."
        )
        return

    try:
        amount = float(parts[-1])
        if not (1.0 <= amount <= 50.0): raise ValueError
    except ValueError:
        await msg.answer("❌ Последний аргумент должен быть числом от 1 до 50.")
        return

    team_names = parts[1:-1]

    results = []
    not_found = []

    async with aiosqlite.connect(DB_PATH) as db:
        for team_name in team_names:
            async with db.execute("SELECT chemistry FROM teams WHERE name=?", (team_name,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                not_found.append(team_name)
                continue
            old_chem = row[0]
            await db.execute(
                "UPDATE teams SET chemistry = MIN(50, chemistry + ?) WHERE name=?",
                (amount, team_name)
            )
            new_chem = min(50.0, old_chem + amount)
            results.append((team_name, old_chem, new_chem))
        await db.commit()

    lines = [f"🤝 Сыгранность повышена на <b>+{amount:.0f}</b>\n"]
    for team_name, old_chem, new_chem in results:
        lines.append(f"✅ <b>{md_escape(str(team_name))}</b>: {old_chem:.0f} → <b>{new_chem:.0f}/50</b>")
    if not_found:
        lines.append("")
        lines.append("❌ <b>Не найдены:</b> " + ", ".join(f"<code>{md_escape(str(t))}</code>" for t in not_found))

    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("create_player"))
async def cmd_create_player(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split()
    if len(parts) < 5:
        await msg.answer(
            "❌ Формат: <code>/create_player Команда Ник Рейтинг Роль [Возраст]</code>\n"
            f"Доступные роли: {', '.join(ROLES)}\n"
            "Рейтинг — от 15.0 до 32.0. Возраст — от 14 до 40 (необязательно, по умолчанию 18)."
        )
        return
    team_name = parts[1]
    nick      = parts[2]
    try:
        rating = float(parts[3])
        if not (15.0 <= rating <= 32.0): raise ValueError
    except ValueError:
        await msg.answer("❌ Рейтинг должен быть числом от 15.0 до 32.0.")
        return

    # Определяем роль и возраст: последний токен может быть возрастом (14-40)
    age = 18
    role_tokens = parts[4:]
    if role_tokens:
        try:
            maybe_age = int(role_tokens[-1])
            if 14 <= maybe_age <= 40:
                age = maybe_age
                role_tokens = role_tokens[:-1]
        except ValueError:
            pass
    role = " ".join(role_tokens)

    if role not in ROLES:
        await msg.answer(f"❌ Роль «{md_escape(str(role))}» не существует.\nДоступные роли: {', '.join(ROLES)}")
        return
    if not await db_get_team(team_name):
        await msg.answer(f"❌ Команда «{md_escape(str(team_name))}» не найдена.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM players WHERE nick=? AND team_name=?", (nick, team_name)
        ) as cursor:
            if await cursor.fetchone():
                await msg.answer(f"❌ Игрок «{md_escape(str(nick))}» уже есть в команде «{md_escape(str(team_name))}».")
                return
        async with db.execute(
            "SELECT COUNT(*) FROM players WHERE team_name=? AND is_reserve=0", (team_name,)
        ) as cursor:
            count_row = await cursor.fetchone()
        main_count = count_row[0] if count_row else 0
        if role == "Coach":
            # Check if team already has a coach
            async with db.execute(
                "SELECT COUNT(*) FROM players WHERE team_name=? AND is_reserve=2", (team_name,)
            ) as cursor2:
                coach_count_row = await cursor2.fetchone()
            if coach_count_row and coach_count_row[0] >= 1:
                await msg.answer(f"❌ В команде «{md_escape(str(team_name))}» уже есть тренер.")
                return
            is_reserve = 2
        else:
            is_reserve = 1 if main_count >= 5 else 0
        await db.execute(
            "INSERT INTO players (nick, team_name, role, rating, is_reserve, age) VALUES (?,?,?,?,?,?)",
            (nick, team_name, role, rating, is_reserve, age)
        )
        if is_reserve != 2:
            await db.execute(
                "INSERT OR IGNORE INTO hltv_players (nickname, team) VALUES (?,?)",
                (nick, team_name)
            )
        await db.commit()
    if is_reserve == 2:
        status = "тренер"
        emoji  = "🎓"
    elif is_reserve == 1:
        status = "резерв"
        emoji  = "🪑"
    else:
        status = "основной состав"
        emoji  = "✅"
    await msg.answer(
        f"{emoji} Игрок <b>{md_escape(str(nick))}</b> добавлен в команду <b>{md_escape(str(team_name))}</b>\n"
        f"🎭 Роль: <b>{md_escape(str(role))}</b>\n"
        f"⭐ Рейтинг: <b>{rating:.1f}</b>\n"
        f"🎂 Возраст: <b>{age} лет</b>\n"
        f"📋 Статус: <b>{status}</b>"
        + ("\n\nℹ️ Основной состав полный — добавлен в резерв." if is_reserve == 1 else "")
    )


@dp.message(Command("addadmin"))
async def cmd_addadmin(msg: Message) -> None:
    if msg.from_user.id != OWNER_ID:
        await msg.answer("⛔ Только владелец бота может добавлять администраторов.")
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("❌ Формат: <code>/addadmin [Telegram ID]</code>")
        return
    new_id = int(parts[1])
    if new_id == OWNER_ID:
        await msg.answer("ℹ️ Владелец уже имеет все права.")
        return
    username = ""
    await db_add_admin(new_id, username)
    await msg.answer(f"✅ Пользователь <code>{md_escape(str(new_id))}</code> добавлен как администратор.")


@dp.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message) -> None:
    if msg.from_user.id != OWNER_ID:
        await msg.answer("⛔ Только владелец бота может удалять администраторов.")
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("❌ Формат: <code>/removeadmin [Telegram ID]</code>")
        return
    rem_id = int(parts[1])
    removed = await db_remove_admin(rem_id)
    if removed:
        await msg.answer(f"✅ Администратор <code>{md_escape(str(rem_id))}</code> удалён.")
    else:
        await msg.answer(f"❌ Пользователь <code>{md_escape(str(rem_id))}</code> не найден среди администраторов.")


@dp.message(Command("listadmins"))
async def cmd_listadmins(msg: Message) -> None:
    if msg.from_user.id != OWNER_ID:
        await msg.answer("⛔ Только владелец может просматривать список администраторов.")
        return
    admins = await db_list_admins()
    if not admins:
        await msg.answer("👤 Администраторов пока нет.\nДобавь через <code>/addadmin [ID]</code>.")
        return
    lines = ["👥 <b>Администраторы:</b>", f"👑 Владелец: <code>{OWNER_ID}</code>", ""]
    for tid, uname, added_at in admins:
        label = f"@{uname}" if uname else f"<code>{tid}</code>"
        lines.append(f"• {label} — добавлен {added_at[:10]}")
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)

@dp.message(Command("addtour"))
async def cmd_addtour(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/addtour НазваниеТурнира</code>")
        return
    name = parts[1].strip()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO tournament_registry (name, is_done, created_at) VALUES (?,0,?)",
                (name, datetime.now().isoformat(timespec="seconds"))
            )
            await db.commit()
        except Exception:
            await msg.answer(f"❌ Турнир «{md_escape(str(name))}» уже существует в реестре.")
            return
    await msg.answer(f"✅ Турнир <b>{md_escape(str(name))}</b> добавлен в реестр.\n📅 Статус: <b>Активный</b>")


@dp.message(Command("tours"))
async def cmd_tours(msg: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, is_done, created_at FROM tournament_registry ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await msg.answer(
            "📋 Реестр турниров пуст.\n"
            "Добавь турнир: <code>/addtour НазваниеТурнира</code>"
        )
        return

    lines = ["📋 <b>Реестр турниров:</b>\n"]
    active_count = 0
    done_count   = 0
    total_pool   = 0
    for name, is_done, created_at in rows:
        safe_name = md_escape(name)
        teams = tournament_teams_count(name)
        pool = tournament_prize_pool(name)

        if is_done:
            lines.append(f"✅ {safe_name} — завершён")
            done_count += 1
        else:
            lines.append(f"🔲 <b>{safe_name}</b> — активный")
            active_count += 1

        lines.append(f"    👥 Команд: <b>{teams}</b>")
        if pool is not None:
            total_pool += pool
            breakdown = tournament_prize_breakdown(name)
            lines.append(f"    💰 Призовой фонд: <b>{fmt_money(pool)}</b>")
            lines.append(
                f"    🏆 Выдача призовых: 🥇 {fmt_money(breakdown[1])}  "
                f"🥈 {fmt_money(breakdown[2])}  🥉 {fmt_money(breakdown[3])}"
            )
        else:
            lines.append("    💰 Призовой фонд: не задан")
        lines.append("")

    lines.append(f"📊 Активных: <b>{active_count}</b>  |  Завершённых: <b>{done_count}</b>")
    lines.append(f"💵 Общий призовой фонд сезона: <b>{fmt_money(total_pool)}</b>")
    lines.append("\n<i>Чтобы завершить тур и вернуть арендованных игроков:</i>")
    lines.append("<code>/donetour НазваниеТурнира</code>")
    lines.append("\n<i>Чтобы выдать призовые за место:</i>")
    lines.append("<code>/tourprize Команда Турнир Место</code>")

    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("donetour"))
async def cmd_donetour(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/donetour НазваниеТурнира</code>")
        return
    tour_name = parts[1].strip()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, is_done FROM tournament_registry WHERE name=?", (tour_name,)
        ) as cursor:
            tour_row = await cursor.fetchone()

        if not tour_row:
            await msg.answer(
                f"❌ Турнир «{md_escape(str(tour_name))}» не найден в реестре.\n"
                f"Добавь его: <code>/addtour {md_escape(str(tour_name))}</code>"
            )
            return

        if tour_row[1] == 1:
            await msg.answer(f"ℹ️ Турнир <b>{md_escape(str(tour_name))}</b> уже был отмечен как завершённый.")
            return

        await db.execute(
            "UPDATE tournament_registry SET is_done=1 WHERE name=?", (tour_name,)
        )

        async with db.execute(
            "SELECT id, nick, from_team, to_team FROM loans WHERE until_tour=? AND returned=0",
            (tour_name,)
        ) as cursor:
            loan_rows = await cursor.fetchall()

        returned_players = []
        for loan_id, nick, from_team, to_team in loan_rows:
            cursor2 = await db.execute(
                "UPDATE players SET team_name=? WHERE nick=? AND team_name=?",
                (from_team, nick, to_team)
            )
            if cursor2.rowcount > 0:
                await db.execute("UPDATE loans SET returned=1 WHERE id=?", (loan_id,))
                returned_players.append((nick, from_team, to_team))
            else:
                await db.execute("UPDATE loans SET returned=1 WHERE id=?", (loan_id,))
                returned_players.append((nick, from_team, "❓ (не найден в арендной команде)"))

        await db.commit()

    # Сбрасываем лимит подписаний свободных агентов — начинается новый тур.
    await db_reset_free_agent_signings()

    lines = [f"✅ Турнир <b>{md_escape(str(tour_name))}</b> завершён!\n"]

    if returned_players:
        lines.append(f"🔄 <b>Возвращены из аренды ({len(returned_players)} игр.):</b>")
        for nick, from_team, to_team in returned_players:
            lines.append(f"  • <b>{md_escape(str(nick))}</b>: {md_escape(str(to_team))} → <b>{md_escape(str(from_team))}</b>")
    else:
        lines.append("ℹ️ Арендованных игроков до этого тура не было.")

    lines.append("")
    lines.append(
        f"🆓 Лимит подписаний свободных агентов ({FREE_AGENT_SIGN_LIMIT_PER_TOUR} на команду) обновлён — "
        f"счётчики всех команд сброшены."
    )

    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)

    # После завершения турнира автоматически прогоняем /teamboost и /trainall для всей лиги.
    await msg.answer("⚡ Турнир завершён — автоматически запускаю <code>/teamboost</code> и <code>/trainall</code>...")
    boost_text = await perform_teamboost()
    await safe_send_long(msg, boost_text)
    train_text = await perform_trainall()
    await safe_send_long(msg, train_text)


@dp.message(Command("deltour"))
async def cmd_deltour(msg: Message) -> None:
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ Формат: <code>/deltour НазваниеТурнира</code>")
        return
    tour_name = parts[1].strip()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, is_done FROM tournament_registry WHERE name=?", (tour_name,)
        ) as cursor:
            tour_row = await cursor.fetchone()

        if not tour_row:
            await msg.answer(f"❌ Турнир «{md_escape(str(tour_name))}» не найден в реестре.")
            return

        async with db.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE tour_name=?", (tour_name,)
        ) as cursor:
            match_count = (await cursor.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM tournament_player_stats WHERE tour_name=?", (tour_name,)
        ) as cursor:
            player_stat_count = (await cursor.fetchone())[0]

        await db.execute("DELETE FROM tournament_registry WHERE name=?", (tour_name,))
        await db.execute("DELETE FROM tournament_matches WHERE tour_name=?", (tour_name,))
        await db.execute("DELETE FROM tournament_player_stats WHERE tour_name=?", (tour_name,))
        await db.execute("DELETE FROM tournaments WHERE name=?", (tour_name,))
        await db.commit()

    status = "завершённый" if tour_row[1] == 1 else "активный"
    await msg.answer(
        f"🗑 Турнир <b>{md_escape(str(tour_name))}</b> удалён.\n"
        f"  • Статус был: {status}\n"
        f"  • Матчей удалено: {match_count}\n"
        f"  • Записей статистики игроков: {player_stat_count}"
    )


@dp.message(Command("renameteam"))
async def cmd_renameteam(msg: Message) -> None:
    """
    /renameteam СтароеНазвание | НовоеНазвание
    Переименовывает команду и обновляет все связанные записи.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2 or "|" not in parts[1]:
        await msg.answer(
            "❌ Формат: <code>/renameteam СтароеНазвание | НовоеНазвание</code>\n"
            "Пример: <code>/renameteam OldTeam | NewTeam</code>"
        )
        return

    old_name, new_name = [s.strip() for s in parts[1].split("|", 1)]

    if not old_name or not new_name:
        await msg.answer("❌ Названия не могут быть пустыми.")
        return

    if old_name == new_name:
        await msg.answer("❌ Старое и новое названия совпадают.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, что старая команда существует
        async with db.execute("SELECT id FROM teams WHERE name=?", (old_name,)) as cursor:
            if not await cursor.fetchone():
                await msg.answer(f"❌ Команда «{md_escape(old_name)}» не найдена.")
                return

        # Проверяем, что новое название не занято
        async with db.execute("SELECT id FROM teams WHERE name=?", (new_name,)) as cursor:
            if await cursor.fetchone():
                await msg.answer(f"❌ Команда с названием «{md_escape(new_name)}» уже существует.")
                return

        # Обновляем все таблицы
        await db.execute("UPDATE teams SET name=? WHERE name=?", (new_name, old_name))
        await db.execute("UPDATE players SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE spirit_history SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE match_history SET team_a=? WHERE team_a=?", (new_name, old_name))
        await db.execute("UPDATE match_history SET team_b=? WHERE team_b=?", (new_name, old_name))
        await db.execute("UPDATE match_history SET result=REPLACE(result, ?, ?) WHERE result LIKE ?",
                         (old_name, new_name, f"%{old_name}%"))
        await db.execute("UPDATE tournament_matches SET team_a=? WHERE team_a=?", (new_name, old_name))
        await db.execute("UPDATE tournament_matches SET team_b=? WHERE team_b=?", (new_name, old_name))
        await db.execute("UPDATE tournament_matches SET winner=? WHERE winner=?", (new_name, old_name))
        await db.execute("UPDATE tournament_player_stats SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE loans SET from_team=? WHERE from_team=?", (new_name, old_name))
        await db.execute("UPDATE loans SET to_team=? WHERE to_team=?", (new_name, old_name))
        await db.execute("UPDATE vrs SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE map_stats SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE merch SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE tournaments SET name=? WHERE name=?", (new_name, old_name))
        await db.execute("UPDATE team_tournament_places SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE team_leaders SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE balance_history SET from_team=? WHERE from_team=?", (new_name, old_name))
        await db.execute("UPDATE balance_history SET to_team=? WHERE to_team=?", (new_name, old_name))
        await db.execute("UPDATE free_agent_signings SET team_name=? WHERE team_name=?", (new_name, old_name))
        await db.execute("UPDATE transfer_requests SET from_team=? WHERE from_team=?", (new_name, old_name))
        await db.execute("UPDATE transfer_requests SET to_team=? WHERE to_team=?", (new_name, old_name))
        await db.commit()

    await msg.answer(
        f"✅ Команда успешно переименована!\n"
        f"🔄 <b>{md_escape(old_name)}</b> → <b>{md_escape(new_name)}</b>\n"
        f"<i>Все данные (игроки, матчи, статистика, аренды, VRS) обновлены.</i>"
    )


@dp.message(Command("renameplayer"))
async def cmd_renameplayer(msg: Message) -> None:
    """
    /renameplayer Команда | СтарыйНик | НовыйНик
    Переименовывает игрока и обновляет все связанные записи
    (основной профиль, статистика турниров, аренды, трансферы, HLTV, FACEIT).
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2 or parts[1].count("|") != 2:
        await msg.answer(
            "❌ Формат: <code>/renameplayer Команда | СтарыйНик | НовыйНик</code>\n"
            "Пример: <code>/renameplayer Virtus.pro | OldNick | NewNick</code>"
        )
        return

    team_name, old_nick, new_nick = [s.strip() for s in parts[1].split("|", 2)]

    if not team_name or not old_nick or not new_nick:
        await msg.answer("❌ Команда и ники не могут быть пустыми.")
        return

    if old_nick == new_nick:
        await msg.answer("❌ Старый и новый ник совпадают.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, что игрок существует именно в этой команде
        async with db.execute(
            "SELECT id FROM players WHERE team_name=? AND nick=?", (team_name, old_nick)
        ) as cursor:
            player_row = await cursor.fetchone()
        if not player_row:
            await msg.answer(
                f"❌ Игрок «{md_escape(old_nick)}» не найден в команде «{md_escape(team_name)}»."
            )
            return
        player_id = player_row[0]

        # Проверяем, что новый ник не занят в этой же команде
        async with db.execute(
            "SELECT id FROM players WHERE team_name=? AND nick=? AND id!=?",
            (team_name, new_nick, player_id)
        ) as cursor:
            if await cursor.fetchone():
                await msg.answer(
                    f"❌ В команде «{md_escape(team_name)}» уже есть игрок с ником «{md_escape(new_nick)}»."
                )
                return

        # Проверяем конфликты в глобальных таблицах статистики (nick — PRIMARY KEY)
        async with db.execute(
            "SELECT team FROM hltv_players WHERE nickname=?", (new_nick,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] != team_name:
                await msg.answer(
                    f"❌ Ник «{md_escape(new_nick)}» уже занят в статистике HLTV другим игроком (команда «{md_escape(row[0])}»)."
                )
                return
        async with db.execute(
            "SELECT team_name FROM faceit_stats WHERE nick=?", (new_nick,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] != team_name:
                await msg.answer(
                    f"❌ Ник «{md_escape(new_nick)}» уже занят в статистике FACEIT другим игроком (команда «{md_escape(row[0])}»)."
                )
                return

        # Обновляем все таблицы, где фигурирует ник игрока
        await db.execute("UPDATE players SET nick=? WHERE id=?", (new_nick, player_id))
        await db.execute(
            "UPDATE tournament_player_stats SET nick=? WHERE nick=? AND team_name=?",
            (new_nick, old_nick, team_name)
        )
        await db.execute(
            "UPDATE loans SET nick=? WHERE nick=? AND (from_team=? OR to_team=?)",
            (new_nick, old_nick, team_name, team_name)
        )
        await db.execute(
            "UPDATE transfer_requests SET nick=? WHERE nick=? AND (from_team=? OR to_team=?)",
            (new_nick, old_nick, team_name, team_name)
        )
        await db.execute(
            "UPDATE transfer_requests SET partner_nick=? WHERE partner_nick=? AND (from_team=? OR to_team=?)",
            (new_nick, old_nick, team_name, team_name)
        )
        await db.execute(
            "UPDATE hltv_players SET nickname=? WHERE nickname=? AND team=?",
            (new_nick, old_nick, team_name)
        )
        await db.execute(
            "UPDATE faceit_stats SET nick=? WHERE nick=? AND team_name=?",
            (new_nick, old_nick, team_name)
        )
        await db.commit()

    await msg.answer(
        f"✅ Игрок успешно переименован!\n"
        f"🔄 <b>{md_escape(old_nick)}</b> → <b>{md_escape(new_nick)}</b>\n"
        f"🚩 Команда: <b>{md_escape(team_name)}</b>\n"
        f"<i>Все данные (профиль, статистика турниров, аренды, трансферы, HLTV, FACEIT) обновлены.</i>"
    )


@dp.message(Command("loans"))
async def cmd_loans(msg: Message) -> None:
    """
    /loans — список всех активных аренд
    """
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT nick, from_team, to_team, until_tour, loaned_at
               FROM loans WHERE returned=0 ORDER BY id ASC"""
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await msg.answer("📋 Активных аренд нет.")
        return

    lines = [f"📋 <b>Активные аренды ({len(rows)}):</b>\n"]
    for nick, from_team, to_team, until_tour, loaned_at in rows:
        date = loaned_at[:10] if loaned_at else "—"
        lines.append(
            f"👤 <b>{md_escape(str(nick))}</b>\n"
            f"   {md_escape(str(from_team))} → {md_escape(str(to_team))}\n"
            f"   📅 До конца: <b>{md_escape(str(until_tour))}</b>  |  выдан {date}\n"
        )
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)

def _trainall_gain(age: int | float | None) -> tuple[float, str]:
    """
    Возвращает (прирост_рейтинга, звёзды) по возрасту игрока.
    ⭐⭐⭐⭐⭐ до 21.5 лет  → +0.4
    ⭐⭐⭐⭐   21.6–24.5   → +0.3
    ⭐⭐⭐     24.6–26.5   → +0.2
    ⭐⭐       26.6–28.5   → +0.1
    ⭐        28.6+        → не качается (0.0)
    """
    if age is None:
        return 0.1, "⭐⭐⭐"
    if age <= 21.5:
        return 0.4, "⭐⭐⭐⭐⭐"
    elif age <= 24.5:
        return 0.3, "⭐⭐⭐⭐"
    elif age <= 26.5:
        return 0.2, "⭐⭐⭐"
    elif age <= 28.5:
        return 0.1, "⭐⭐"
    else:
        return 0.0, "⭐"


def _team_avg_potential(main_roster: list) -> tuple[float, str]:
    """
    Считает средний потенциал команды по возрасту основных игроков.
    avg_age = сумма возрастов / количество игроков
    ⭐⭐⭐⭐⭐  avg_age <= 21.5  → максимальная прокачка (+0.4)
    ⭐⭐⭐⭐    avg_age <= 24.5  → хорошая прокачка (+0.3)
    ⭐⭐⭐      avg_age <= 26.5  → средняя прокачка (+0.2)
    ⭐⭐        avg_age <= 28.5  → слабая прокачка (+0.1)
    ⭐          avg_age >  28.5  → не качается (0.0)
    """
    ages = [ag for _, _, _, ag in main_roster if ag is not None]
    if not ages:
        return 0.0, "⭐"
    avg_age = sum(ages) / len(ages)
    if avg_age <= 21.5:
        return 0.4, "⭐⭐⭐⭐⭐"
    elif avg_age <= 24.5:
        return 0.3, "⭐⭐⭐⭐"
    elif avg_age <= 26.5:
        return 0.2, "⭐⭐⭐"
    elif avg_age <= 28.5:
        return 0.1, "⭐⭐"
    else:
        return 0.0, "⭐"


def _team_stars(avg_rating: float) -> tuple[float, str]:
    """
    Возвращает (прирост_рейтинга, звёзды) по СРЕДНЕМУ рейтингу команды.
    ⭐⭐⭐⭐⭐  avg >= 26.0  → +0.4
    ⭐⭐⭐⭐    avg >= 23.0  → +0.3
    ⭐⭐⭐      avg >= 20.0  → +0.2
    ⭐⭐        avg >= 17.0  → +0.1
    ⭐          avg <  17.0  → не качается (0.0)
    """
    if avg_rating >= 26.0:
        return 0.4, "⭐⭐⭐⭐⭐"
    elif avg_rating >= 23.0:
        return 0.3, "⭐⭐⭐⭐"
    elif avg_rating >= 20.0:
        return 0.2, "⭐⭐⭐"
    elif avg_rating >= 17.0:
        return 0.1, "⭐⭐"
    else:
        return 0.0, "⭐"


async def perform_trainall() -> str:
    """
    Тренировка для ВСЕХ игроков во ВСЕХ командах лиги — общая логика,
    используется и командой /trainall, и автоматически при /donetour.
    Прирост рейтинга определяется средним возрастом основного состава команды.
    Тренеры не тренируются. Всем командам также даётся +35% Духа и +25 Сыгранности
    (до максимума 50). Возвращает готовый текст отчёта.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM teams ORDER BY name") as cursor:
            all_teams = await cursor.fetchall()

    if not all_teams:
        return "❌ В лиге нет ни одной команды."

    total_players = 0
    total_skipped = 0
    lines = [
        "🏋️ <b>Тренировка всей лиги!</b>\n",
        "<i>Прирост определяется средним возрастом основного состава каждой команды</i>",
        "<i>⭐⭐⭐⭐⭐ avg≤21.5 (+0.4)  |  ⭐⭐⭐⭐ avg≤24.5 (+0.3)  |  ⭐⭐⭐ avg≤26.5 (+0.2)  |  ⭐⭐ avg≤28.5 (+0.1)  |  ⭐ avg>28.5 (—)</i>\n",
    ]

    async with aiosqlite.connect(DB_PATH) as db:
        for (team_name,) in all_teams:
            cursor = await db.execute(
                "SELECT nick, role, rating, is_reserve, age FROM players WHERE team_name=? ORDER BY is_reserve ASC, id ASC",
                (team_name,)
            )
            players = await cursor.fetchall()
            await cursor.close()

            if not players:
                continue

            # Потенциал по среднему возрасту основного состава
            main_roster = [(n, ro, ra, ag) for n, ro, ra, ir, ag in players if ir == 0]
            gain, stars = _team_avg_potential(main_roster)

            lines.append(f"🔹 <b>{md_escape(team_name)}</b>  {stars}  (+{gain:.1f})")

            # +35% Духа команде за тренировку, +25 Сыгранности
            spirit_cursor = await db.execute(
                "SELECT spirit, chemistry FROM teams WHERE name=?",
                (team_name,)
            )
            spirit_row = await spirit_cursor.fetchone()
            await spirit_cursor.close()
            if spirit_row is not None:
                old_spirit, old_chemistry = spirit_row
                spirit_gain = old_spirit * 0.35
                new_spirit = max(0.0, min(100.0, old_spirit + spirit_gain))
                new_chemistry = min(50.0, old_chemistry + 25.0)
                await db.execute(
                    "UPDATE teams SET spirit=?, chemistry=? WHERE name=?",
                    (new_spirit, new_chemistry, team_name)
                )
                await db.execute(
                    "INSERT INTO spirit_history (team_name, change, reason, changed_at) VALUES (?,?,?,?)",
                    (team_name, new_spirit - old_spirit, "Тренировка лиги (+35% духа)", datetime.now().isoformat(timespec="seconds"))
                )
                lines.append(
                    f"   ✨ Дух: {old_spirit:.1f} → <b>{new_spirit:.1f}</b> (+{new_spirit - old_spirit:.1f}, +35%)"
                )
                lines.append(
                    f"   🤝 Сыгранность: {old_chemistry:.1f} → <b>{new_chemistry:.1f}</b> (+{new_chemistry - old_chemistry:.1f})"
                )
            for nick, role, rating, is_reserve, age in players:
                if is_reserve == 2:
                    lines.append(
                        f"  • <b>{md_escape(nick)}</b> [Coach] — <i>не тренируется</i>"
                    )
                    total_skipped += 1
                    continue

                status = "Резерв" if is_reserve == 1 else role

                if gain == 0.0:
                    lines.append(
                        f"  • <b>{md_escape(nick)}</b> [{status}] "
                        f"{rating:.2f} — <i>не качается</i>"
                    )
                    total_skipped += 1
                    continue

                new_rating = round(min(32.0, rating + gain), 2)
                await db.execute(
                    "UPDATE players SET rating=? WHERE nick=? AND team_name=?",
                    (new_rating, nick, team_name)
                )
                lines.append(
                    f"  • <b>{md_escape(nick)}</b> [{status}] "
                    f"{rating:.2f} → <b>{new_rating:.2f}</b> (+{gain:.1f})"
                )
                total_players += 1
            lines.append("")
        await db.commit()

    lines.append(
        f"✅ Тренировка завершена!\n"
        f"Прокачано: <b>{total_players}</b> игр. | Не качались: <b>{total_skipped}</b> игр. | "
        f"Команд: <b>{len(all_teams)}</b> | Всем командам выдано <b>+35% Духа</b> и <b>+25 Сыгранности</b>"
    )
    return "\n".join(lines)


@dp.message(Command("trainall"))
async def cmd_trainall(msg: Message) -> None:
    """
    /trainall — тренировка для ВСЕХ игроков во ВСЕХ командах лиги.
    Прирост рейтинга определяется средним возрастом основного состава команды.
    Тренеры не тренируются. Всем командам также даётся +35% Духа и +25 Сыгранности
    (до максимума 50).

    ⭐⭐⭐⭐⭐ avg_age ≤21.5  → +0.4
    ⭐⭐⭐⭐   avg_age ≤24.5  → +0.3
    ⭐⭐⭐     avg_age ≤26.5  → +0.2
    ⭐⭐       avg_age ≤28.5  → +0.1
    ⭐        avg_age >28.5   → не качается
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    await msg.answer("⏳ Тренировка лиги запущена, подождите...")
    full_text = await perform_trainall()
    await safe_send_long(msg, full_text)


async def perform_teamboost() -> str:
    """
    Выдать ВСЕМ командам +20 Духа и +10 Сыгранности — общая логика,
    используется и командой /teamboost, и автоматически при /donetour.
    Возвращает готовый текст отчёта.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, spirit, chemistry FROM teams ORDER BY name") as cursor:
            all_teams = await cursor.fetchall()

    if not all_teams:
        return "❌ В лиге нет ни одной команды."

    lines = ["💥 <b>Командный буст для всей лиги!</b>\n"]

    async with aiosqlite.connect(DB_PATH) as db:
        for team_name, old_spirit, old_chemistry in all_teams:
            new_spirit = max(0.0, min(100.0, old_spirit + 20.0))
            new_chemistry = min(50.0, old_chemistry + 10.0)
            await db.execute(
                "UPDATE teams SET spirit=?, chemistry=? WHERE name=?",
                (new_spirit, new_chemistry, team_name)
            )
            await db.execute(
                "INSERT INTO spirit_history (team_name, change, reason, changed_at) VALUES (?,?,?,?)",
                (team_name, 20.0, "Командный буст для лиги (+20 духа)", datetime.now().isoformat(timespec="seconds"))
            )
            spirit_label, spirit_emoji = spirit_status(new_spirit)
            lines.append(
                f"{spirit_emoji} <b>{md_escape(team_name)}</b>\n"
                f"   Дух: {old_spirit:.1f} → <b>{new_spirit:.1f}</b> (+20) — {spirit_label}\n"
                f"   Сыгранность: {old_chemistry:.1f} → <b>{new_chemistry:.1f}</b> (+10)\n"
            )
        await db.commit()

    lines.append(f"✅ Буст выдан всем <b>{len(all_teams)}</b> командам!")
    return "\n".join(lines)


@dp.message(Command("teamboost"))
async def cmd_teamboost(msg: Message) -> None:
    """
    /teamboost — выдать ВСЕМ командам +20 Духа и +10 Сыгранности.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    full_text = await perform_teamboost()
    await safe_send_long(msg, full_text)



# ── MERCH helpers ─────────────────────────────────────────────────────────────

# Виды товаров: (название, цена_мин, цена_макс, эмодзи) — цены в рублях
MERCH_ITEMS = [
    ("Игровая майка",        4500,   9000, "👕"),
    ("Худи команды",         6000,  11000, "🧥"),
    ("Коврик для мыши",      2000,   4000, "🖱"),
    ("Кепка с логотипом",    2500,   4500, "🧢"),
    ("Наклейки (набор)",      500,   1500, "🎨"),
    ("Флаг команды",         1500,   3000, "🚩"),
    ("Кружка",               1200,   2500, "☕"),
    ("Скин-пак (Standoff 2 drops)", 1000,   5000, "🎮"),
]

# Организации, у которых в турнирной таблице (VRS) фигурирует несколько
# отдельных названий команд, принадлежащих на самом деле одному клубу
# (фарм-составы, академии, региональные ростеры и т.п.). Деньги с продаж
# мерча ЛЮБОГО состава зачисляются на баланс ГЛАВНОЙ команды.
#
# Есть два механизма (работают вместе):
#
# 1) Общее правило "Префикс.Суффикс" — если название команды имеет вид
#    "Организация.Суффикс", где Суффикс — один из FARM_SUFFIX_MARKERS
#    (Brazil, Academy, Acd и т.п., без учёта регистра), то команда
#    автоматически считается суб-составом «Организация».
#    Примеры: "Vp.Brazil", "VeyroN.Academy", "CYBERSHOKE.Brazil",
#    "CYBERHERO.Brazil", "Mafiozy.Acd" — суффикс подхватывается сам,
#    ничего дописывать не нужно.
#
# 2) Точечные алиасы (TEAM_ORG_ALIASES) — для случаев, которые НЕ
#    подчиняются правилу выше: либо название не содержит точку и суффикс
#    (например "Бразилка"), либо суффикс не входит в FARM_SUFFIX_MARKERS
#    (например "Vp.Prodigy" — Prodigy это не фарм-маркер, а отдельный
#    суб-бренд именно Virtus.pro).
#
# 3) ORG_PREFIX_ALIASES — если короткий префикс в названии отличается от
#    полного имени команды в /teams (например префикс "Vp" — это клуб
#    "Virtus.pro", а не команда с названием "Vp").

FARM_SUFFIX_MARKERS = {"brazil", "academy", "acd", "junior", "youth", "female", "u21", "u19"}

ORG_PREFIX_ALIASES: dict[str, str] = {
    "vp": "Virtus.pro",
    "veyron": "VeyroN.eSports",
    "mafiozy": "Mafiozy.eSports",
    # добавляйте сюда новые короткие префиксы → полное имя команды в /teams
}

TEAM_ORG_ALIASES: dict[str, list[str]] = {
    "Virtus.pro": ["Бразилка", "Vp.Prodigy", "Prodigy"],
    # добавляйте сюда точечные исключения для других клубов при необходимости
}

# Обратный индекс: любое известное точечное название состава (в нижнем регистре) → главная команда.
_TEAM_ORG_LOOKUP: dict[str, str] = {}
for _main_team, _aliases in TEAM_ORG_ALIASES.items():
    _TEAM_ORG_LOOKUP[_main_team.strip().lower()] = _main_team
    for _alias in _aliases:
        if _alias:
            _TEAM_ORG_LOOKUP[_alias.strip().lower()] = _main_team


def resolve_org_team(team_name: str) -> str:
    """
    Возвращает «главную» команду-организацию для зачисления денег с мерча.

    Порядок проверки:
      1. Точечный алиас из TEAM_ORG_ALIASES (спец-случаи вроде "Prodigy",
         "Бразилка" без явного суффикса-паттерна).
      2. Общий шаблон "Префикс.Суффикс", где Суффикс — один из
         FARM_SUFFIX_MARKERS (Brazil, Academy, Acd и т.п.) — тогда
         организация = Префикс, с подстановкой полного имени через
         ORG_PREFIX_ALIASES, если короткий префикс отличается от
         официального имени команды в /teams.
      3. Если ничего не подошло — команда возвращается как есть
         (деньги идут на её собственный баланс).
    """
    raw = team_name.strip()
    lowered = raw.lower()

    if lowered in _TEAM_ORG_LOOKUP:
        return _TEAM_ORG_LOOKUP[lowered]

    if "." in raw:
        prefix, suffix = raw.rsplit(".", 1)
        if suffix.strip().lower() in FARM_SUFFIX_MARKERS:
            prefix_key = prefix.strip().lower()
            return ORG_PREFIX_ALIASES.get(prefix_key, prefix.strip())

    return raw


def _simulate_merch(target_revenue: int) -> dict:
    """
    Симулирует продажи 4 видов товаров так, чтобы суммарный доход
    был близок к target_revenue. Возвращает dict с деталями по каждому товару.
    """
    result = {}
    remaining = target_revenue

    for idx, (name, price_min, price_max, emoji) in enumerate(MERCH_ITEMS):
        # Последний товар добирает остаток
        if idx == len(MERCH_ITEMS) - 1:
            if remaining <= 0:
                result[name] = {"units": 0, "revenue": 0, "emoji": emoji}
                continue
            avg_price = (price_min + price_max) / 2
            units = max(1, int(remaining / avg_price))
        else:
            # Доля этого товара — случайная часть оставшегося бюджета
            share = random.uniform(0.15, 0.40)
            chunk = max(0, int(remaining * share))
            avg_price = (price_min + price_max) / 2
            units = max(0, int(chunk / avg_price))

        price_per_unit = random.randint(price_min, price_max)
        revenue = units * price_per_unit
        remaining = max(0, remaining - revenue)
        result[name] = {"units": units, "revenue": revenue, "emoji": emoji}

    return result


def _merch_revenue_for_place(place: int, total: int) -> tuple:
    """
    Базовый доход от ₽200 000 (последнее место) до ₽1 000 000 (1-е место).
    Максимальная выручка за одну волну жёстко ограничена ₽1 000 000 (1 лям) —
    даже редкие события (jackpot/viral) не могут превысить этот потолок.
    Специальные события (возвращает (revenue, event_tag)):
      - 'upset'   : команда неожиданно выстреливает до уровня топ-3 (10%)
      - 'slump'   : 1-е место проваливается до середины (6%)
      - 'jackpot' : любая команда хайп-волна, x1.5–x2.5 от базы (~5%), но не выше потолка
      - 'viral'   : сверхредкий вирусный взрыв, почти всегда упирается в потолок ₽1 000 000 (~0.5%)
      - None      : обычная волна с ±20% джиттером
    """
    max_rev = 1_000_000
    min_rev = 200_000

    if total <= 1:
        return max_rev, None

    def _base(p: int) -> float:
        return max_rev - (max_rev - min_rev) * (p - 1) / (total - 1)

    base = _base(place)
    event_tag = None

    # Сверхредкий вирусный взрыв — почти всегда в потолок ₽1 000 000
    if random.random() < 0.005:
        base = random.randint(950_000, max_rev)
        event_tag = "viral"

    # Jackpot-волна — x1.5..x2.5 от базы (~5%), но не выше потолка
    elif random.random() < 0.05:
        base = base * random.uniform(1.5, 2.5)
        event_tag = "jackpot"

    # 1-е место проваливается (насыщение / скандал)
    elif place == 1 and random.random() < 0.06:
        lo = max(2, total // 3)
        hi = max(lo, total // 2)
        fake_place = random.randint(lo, hi)
        base = _base(fake_place)
        event_tag = "slump"

    # Неожиданный взлёт до уровня топ-3 (10%)
    elif random.random() < 0.10:
        fake_place = random.randint(1, max(1, min(3, total // 3)))
        if fake_place < place:
            base = _base(fake_place)
            event_tag = "upset"

    jitter = random.uniform(0.80, 1.20)
    result = int(round(base * jitter, -1))
    return max(min_rev, min(max_rev, result)), event_tag


@dp.message(Command("merch"))
async def cmd_merch(msg: Message) -> None:
    """
    /merch — ТОЛЬКО ДЛЯ АДМИНИСТРАТОРА.
    Запускает новую волну продаж мерча, генерирует доходы для всех команд
    из VRS и сохраняет результат в БД. Выводит детальный отчёт с разбивкой
    по товарам и пометкой ⚡ для неожиданных взлётов.
    """
    if not await db_is_moderator(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name, points FROM vrs ORDER BY points DESC LIMIT 32"
        ) as cursor:
            vrs_rows = await cursor.fetchall()

    if not vrs_rows:
        await msg.answer(
            "📦 Мерч-статистика недоступна — VRS таблица пуста.\n"
            "Сыграйте хотя бы один турнирный матч, чтобы команды появились в рейтинге."
        )
        return

    total = len(vrs_rows)

    await msg.answer("⏳ Генерируем волну продаж мерча, подождите...")

    # Генерируем продажи и обновляем БД
    team_results = []  # (team_name, wave_revenue, wave_units, breakdown, is_upset, vrs_place)
    target_by_team: dict[str, float] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for place, (team_name, _) in enumerate(vrs_rows, 1):
            target, event_tag = _merch_revenue_for_place(place, total)
            target_by_team[team_name] = target
            details      = _simulate_merch(target)
            wave_revenue = sum(v["revenue"] for v in details.values())
            wave_units   = sum(v["units"]   for v in details.values())

            await db.execute(
                """INSERT INTO merch (team_name, revenue, units_sold)
                   VALUES (?, ?, ?)
                   ON CONFLICT(team_name) DO UPDATE SET
                       revenue    = revenue    + excluded.revenue,
                       units_sold = units_sold + excluded.units_sold""",
                (team_name, wave_revenue, wave_units)
            )
            team_results.append((team_name, wave_revenue, wave_units, details, event_tag, place))
        await db.commit()

    # Зачисляем деньги на баланс команд. Если команда — известный суб-состав
    # (см. TEAM_ORG_ALIASES), деньги уходят на баланс главной организации,
    # а не на её собственный (не существующий отдельно) счёт.
    credit_info: dict[str, tuple[str, bool, str]] = {}  # team_name -> (org_name, ok, new_balance_str)
    for team_name, wave_revenue, wave_units, details, event_tag, vrs_place in team_results:
        if wave_revenue <= 0:
            continue
        org_name = resolve_org_team(team_name)
        reason = "Продажа мерча" if org_name == team_name else f"Продажа мерча ({team_name})"
        ok, err, new_balance = await db_add_balance(
            org_name, wave_revenue, reason, msg.from_user.id
        )
        credit_info[team_name] = (org_name, ok, fmt_money(new_balance) if ok else err)

    # Сортируем по доходу этой волны (от большего к меньшему)
    team_results.sort(key=lambda x: x[1], reverse=True)

    place_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["👕 <b>Продажи мерча — сезонный отчёт</b> (администратор)\n"]

    for i, (team_name, wave_revenue, wave_units, details, event_tag, vrs_place) in enumerate(team_results, 1):
        medal = place_medals.get(i, f"{i}.")
        if event_tag == "viral":
            event_label = "  <b>🌋 ВИРУСНЫЙ ВЗРЫВ!</b>"
        elif event_tag == "jackpot":
            event_label = "  <b>💥 Джекпот-волна!</b>"
        elif event_tag == "upset":
            event_label = "  <b>⚡ Неожиданный взлёт!</b>"
        elif event_tag == "slump":
            event_label = "  <b>📉 Провал продаж!</b>"
        else:
            event_label = ""
        lines.append(
            f"{medal} <b>{md_escape(str(team_name))}</b>{event_label}  "
            f"💰 {wave_revenue:,} ₽  |  📦 {wave_units:,} ед."
        )
        lines.append(
            f"    <i>место в VRS: {vrs_place}/{total} · цель формулы: {int(round(target_by_team.get(team_name, 0))):,} ₽</i>"
        )

        org_name, ok, info = credit_info.get(team_name, (None, False, ""))
        if org_name is None:
            continue
        if not ok:
            lines.append(f"    ⚠️ Не зачислено на баланс: {info}")
        elif org_name != team_name:
            lines.append(
                f"    💳 Зачислено на счёт материнской команды "
                f"<b>{md_escape(org_name)}</b> (новый баланс: {info})"
            )
        else:
            lines.append(f"    💳 Зачислено на баланс команды (новый баланс: {info})")

    total_revenue = sum(r[1] for r in team_results)
    total_units   = sum(r[2] for r in team_results)
    lines.append(
        f"\n<i>📊 Команд: {len(team_results)} | "
        f"Итого выручка: {total_revenue:,} ₽ | "
        f"Итого единиц: {total_units:,} ед.</i>"
    )
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("merch_stats"))
async def cmd_merch_stats(msg: Message) -> None:
    """
    /merch_stats — публичная статистика накопленных продаж мерча.
    Показывает суммарный доход и количество единиц по каждой команде
    за всё время (без детальной разбивки по товарам — только итоги).
    Доступна всем пользователям.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name, revenue, units_sold FROM merch ORDER BY revenue DESC"
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await msg.answer(
            "📦 Данных о продажах мерча пока нет.\n"
            "Администратор должен запустить <code>/merch</code> для генерации статистики."
        )
        return

    place_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["👕 <b>Статистика продаж мерча</b>\n"]

    for i, (team_name, revenue, units_sold) in enumerate(rows, 1):
        medal = place_medals.get(i, f"{i}.")
        lines.append(
            f"{medal} <b>{md_escape(str(team_name))}</b>\n"
            f"   💰 {revenue:,} ₽  |  📦 {units_sold:,} ед."
        )
        lines.append("")

    total_rev   = sum(r[1] for r in rows)
    total_units = sum(r[2] for r in rows)
    lines.append(
        f"<i>📊 Всего команд: {len(rows)} | "
        f"Общая выручка: {total_rev:,} ₽ | "
        f"Единиц продано: {total_units:,} ед.</i>"
    )
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("clear_merch_stats"))
async def cmd_clear_merch_stats(msg: Message) -> None:
    """
    /clear_merch_stats — ТОЛЬКО ДЛЯ АДМИНИСТРАТОРА.
    Полностью очищает таблицу merch (revenue и units_sold обнуляются для всех команд).
    Требует подтверждения через /clear_merch_stats confirm.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split()
    if len(parts) < 2 or parts[1].lower() != "confirm":
        await msg.answer(
            "⚠️ <b>Вы собираетесь полностью очистить статистику мерча!</b>\n\n"
            "Это действие удалит накопленные данные о продажах для <b>всех команд</b>.\n\n"
            "Для подтверждения введите:\n"
            "<code>/clear_merch_stats confirm</code>"
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM merch")
        await db.commit()

    await msg.answer(
        "🗑️ <b>Статистика мерча очищена.</b>\n\n"
        "Все данные о продажах удалены. Запустите <code>/merch</code> для генерации новой волны."
    )



@dp.message(Command("vrs_help"))
async def cmd_vrs_help(msg: Message) -> None:
    """
    /vrs_help — справка по системе расчёта VRS-рейтинга (аналог HLTV World Ranking).

    Показывает:
      • Тиры турниров и диапазоны очков
      • Логику динамического расчёта (зависимость от разницы рангов)
      • Таблицу примеров для разных ситуаций
    """

    # ── Шапка ────────────────────────────────────────────────────────────────
    lines = [
        "🌍 <b>VRS — Virtual Ranking System</b>",
        "<i>Система рейтинга команд, вдохновлённая HLTV World Ranking</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📋 <b>Тиры турниров и диапазоны очков:</b>",
        "",

        # S-Tier
        "🏆 <b>S-Tier</b>  <i>(Winline Epic Standoff 2 Jumble Rumble S1 — S8,</i>",
        "<i>Winline Major Standoff 2 Jumble Rumble S1, Winline Major Standoff 2 Jumble Rumble S2,</i>",
        "<i>SPL Pro League Season 1 — Season 5, SPL Pro League LAN Finals 1)</i>",
        "   ✅ Победа:    от <b>+14</b> до <b>+42</b> очков",
        "   ❌ Поражение: от <b>−3</b>  до <b>−10</b> очков",
        "",

        # A-Tier
        "🥈 <b>A-Tier</b>  <i>(CONDR Season 1 — Season 8,</i>",
        "<i>ES1ZE Cup S1 — S4)</i>",
        "   ✅ Победа:    от <b>+9</b>  до <b>+17</b> очков",
        "   ❌ Поражение: от <b>−1</b>  до <b>−5</b>  очков",
        "",

        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚙️ <b>Как работает динамический расчёт:</b>",
        "",
        "Итоговые очки внутри диапазона зависят от <b>разницы рангов</b> команд.",
        "Максимальная учитываемая разница — <b>50 позиций</b> (всё что выше — считается как 50).",
        "",
        "📈 <b>Победа:</b>",
        "  • Обыграли <b>более сильного</b> (меньший номер) → ближе к <b>максимуму</b>",
        "  • Обыграли <b>более слабого</b>  (больший номер) → ближе к <b>минимуму</b>",
        "",
        "📉 <b>Поражение:</b>",
        "  • Проиграли <b>более сильному</b> → теряете <b>мало</b> (ближе к минимуму потерь)",
        "  • Проиграли <b>более слабому</b>  → теряете <b>много</b> (ближе к максимуму потерь)",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧪 <b>Примеры расчётов:</b>",
        "",
    ]

    # ── Таблица примеров ─────────────────────────────────────────────────────
    tier_emoji = {"S": "🏆", "A": "🥈", "B": "🥉"}
    result_emoji = {"win": "✅", "loss": "❌"}

    examples = vrs_rating_examples()
    for i, ex in enumerate(examples, 1):
        te      = tier_emoji.get(ex["tier"], "❓")
        res_e   = result_emoji.get(ex["result"], "❓")
        chg = ex["change"]
        sign = "+" if chg >= 0 else ""
        lines.append(
            f"{i}. {te} {res_e} <b>{md_escape(str(ex['desc']))}</b>\n"
            f"   Ранг <b>#{ex['team_rank']}</b> vs <b>#{ex['opp_rank']}</b>  →  "
            f"<b>{sign}{chg}</b> очков"
        )

    lines += [
        "",
        "🎯 <b>Разгромность счёта:</b>",
        "  • Чистая победа (например 2:0, 3:0) — очки без урезания",
        "  • Победа на тай-брейке (2:1, 3:2) — очки немного меньше (до −10%)",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "💡 <i>Формула: логистическая (Elo-подобная) кривая ожидаемого результата</i>",
        "<i>на основе нормализованной разницы рангов, плюс множитель за счёт матча.</i>",
    ]

    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


# ── Сезонный список турниров с тирами ────────────────────────────────────────
SEASON_TOURNAMENTS: list[tuple[str, str]] = [
    ("Winline Epic Standoff 2 Jumble Rumble S1", "S"),
    ("CONDR Season 1", "A"),
    ("Winline Epic Standoff 2 Jumble Rumble S2", "S"),
    ("CONDR Season 2", "A"),
    ("ES1ZE Cup S1", "A"),
    ("SPL Pro League Season 1", "S"),
    ("Winline Epic Standoff 2 Jumble Rumble S3", "S"),
    ("CONDR Season 3", "A"),
    ("Winline Epic Standoff 2 Jumble Rumble S4", "S"),
    ("CONDR Season 4", "A"),
    ("SPL Pro League Season 2", "S"),
    ("ES1ZE Cup S2", "A"),
    ("Winline Major Standoff 2 Jumble Rumble S1", "S"),
    ("Winline Epic Standoff 2 Jumble Rumble S5", "S"),
    ("CONDR Season 5", "A"),
    ("SPL Pro League Season 3", "S"),
    ("Winline Epic Standoff 2 Jumble Rumble S6", "S"),
    ("CONDR Season 6", "A"),
    ("SPL Pro League Season 4", "S"),
    ("ES1ZE Cup S3", "A"),
    ("Winline Epic Standoff 2 Jumble Rumble S7", "S"),
    ("CONDR Season 7", "A"),
    ("SPL Pro League Season 5", "S"),
    ("Winline Epic Standoff 2 Jumble Rumble S8", "S"),
    ("CONDR Season 8", "A"),
    ("SPL Pro League LAN Finals 1", "S"),
    ("ES1ZE Cup S4", "A"),
    ("Winline Major Standoff 2 Jumble Rumble S2", "S"),
]

# Число команд-участников и общий призовой фонд (в рублях) по каждому турниру
# сезона. Во всех турнирах участвует 16 команд, кроме LAN-финала SPL Pro
# League (8 команд). Призовой фонд распределяется между 1–3 местом в
# пропорции 50% / 30% / 20% — см. tournament_prize_breakdown().
TOURNAMENT_PRIZE_POOL: dict[str, tuple[int, int]] = {
    "Winline Epic Standoff 2 Jumble Rumble S1": (16, 2_500_000),
    "CONDR Season 1": (16, 750_000),
    "Winline Epic Standoff 2 Jumble Rumble S2": (16, 2_500_000),
    "CONDR Season 2": (16, 750_000),
    "ES1ZE Cup S1": (16, 500_000),
    "SPL Pro League Season 1": (16, 2_000_000),
    "Winline Epic Standoff 2 Jumble Rumble S3": (16, 2_500_000),
    "CONDR Season 3": (16, 750_000),
    "Winline Epic Standoff 2 Jumble Rumble S4": (16, 2_500_000),
    "CONDR Season 4": (16, 750_000),
    "SPL Pro League Season 2": (16, 2_000_000),
    "ES1ZE Cup S2": (16, 500_000),
    "Winline Major Standoff 2 Jumble Rumble S1": (16, 5_500_000),
    "Winline Epic Standoff 2 Jumble Rumble S5": (16, 2_500_000),
    "CONDR Season 5": (16, 750_000),
    "SPL Pro League Season 3": (16, 2_000_000),
    "Winline Epic Standoff 2 Jumble Rumble S6": (16, 2_500_000),
    "CONDR Season 6": (16, 750_000),
    "SPL Pro League Season 4": (16, 2_000_000),
    "ES1ZE Cup S3": (16, 500_000),
    "Winline Epic Standoff 2 Jumble Rumble S7": (16, 2_500_000),
    "CONDR Season 7": (16, 750_000),
    "SPL Pro League Season 5": (16, 2_000_000),
    "Winline Epic Standoff 2 Jumble Rumble S8": (16, 2_500_000),
    "CONDR Season 8": (16, 750_000),
    "SPL Pro League LAN Finals 1": (8, 5_000_000),
    "ES1ZE Cup S4": (16, 500_000),
    "Winline Major Standoff 2 Jumble Rumble S2": (16, 5_000_000),
}


def _find_prize_pool_entry(tour_name: str) -> tuple[int, int] | None:
    """Ищет турнир в TOURNAMENT_PRIZE_POOL без учёта регистра."""
    entry = TOURNAMENT_PRIZE_POOL.get(tour_name)
    if entry:
        return entry
    lower = tour_name.lower()
    for name, val in TOURNAMENT_PRIZE_POOL.items():
        if name.lower() == lower:
            return val
    return None


def tournament_teams_count(tour_name: str) -> int:
    """Число команд-участников турнира. Дефолт — 16, если турнир не найден
    в TOURNAMENT_PRIZE_POOL (например, добавлен вручную через /addtour)."""
    entry = _find_prize_pool_entry(tour_name)
    return entry[0] if entry else 16


def tournament_prize_pool(tour_name: str) -> int | None:
    """Общий призовой фонд турнира, либо None, если турнир не найден."""
    entry = _find_prize_pool_entry(tour_name)
    return entry[1] if entry else None


def tournament_prize_breakdown(tour_name: str) -> dict[int, int] | None:
    """Призовые за 1/2/3 место, рассчитанные от общего призового фонда турнира
    в пропорции 50% / 30% / 20%. Возвращает None, если турнир не найден в
    TOURNAMENT_PRIZE_POOL — тогда используется старая тир-based таблица
    TOURNAMENT_PRIZES (S/A/B) как запасной вариант."""
    pool = tournament_prize_pool(tour_name)
    if pool is None:
        return None
    return {
        1: round(pool * 0.5),
        2: round(pool * 0.3),
        3: round(pool * 0.2),
    }


async def db_seed_tournaments() -> int:
    """Вставляет сезонные турниры в tournament_registry (INSERT OR IGNORE).
    Возвращает количество добавленных строк."""
    added = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for name, tier in SEASON_TOURNAMENTS:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO tournament_registry (name, tier, is_done, created_at) VALUES (?,?,0,?)",
                (name, tier, datetime.now().isoformat(timespec="seconds"))
            )
            added += cursor.rowcount
        await db.commit()
    return added


@dp.message(Command("seed_tournaments"))
async def cmd_seed_tournaments(msg: Message) -> None:
    """/seed_tournaments — (только владелец / админ)
    Заполняет tournament_registry сезонным списком турниров с тирами S/A/B.
    Уже существующие турниры не перезаписываются (INSERT OR IGNORE).
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    added = await db_seed_tournaments()
    tier_emoji = {"S": "🏆", "A": "🥈", "B": "🥉"}
    lines = [f"🗂 <b>Сезонные турниры загружены</b> (+{added} новых)\n"]
    for name, tier in SEASON_TOURNAMENTS:
        lines.append(f"{tier_emoji.get(tier, '❓')} [{tier}] {md_escape(name)}")
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("addmoderator"))
async def cmd_addmoderator(msg: Message) -> None:
    """
    /addmoderator [Telegram ID] — добавить модератора.
    Только для владельца или администратора.
    Модератор может: /transfer, /swap, /addroles, /addrolesall, /merch, /loans.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Только владелец или администратор может добавлять модераторов.")
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("❌ Формат: <code>/addmoderator [Telegram ID]</code>")
        return
    new_id = int(parts[1])
    if new_id == OWNER_ID:
        await msg.answer("ℹ️ Владелец уже имеет все права.")
        return
    if await db_is_admin(new_id):
        await msg.answer("ℹ️ Этот пользователь уже является администратором — у него уже есть все права.")
        return
    await db_add_moderator(new_id, "")
    await msg.answer(
        f"🛡 Пользователь <code>{md_escape(str(new_id))}</code> добавлен как <b>модератор</b>.\n\n"
        f"Доступные команды модератора:\n"
        f"<code>/transfer</code> — трансферы и аренды\n"
        f"<code>/swap</code> — обмен игроками\n"
        f"<code>/addroles</code> / <code>/addrolesall</code> — смена ролей\n"
        f"<code>/merch</code> — генерация волны продаж\n"
        f"<code>/loans</code> — просмотр активных аренд"
    )


@dp.message(Command("removemoderator"))
async def cmd_removemoderator(msg: Message) -> None:
    """
    /removemoderator [Telegram ID] — удалить модератора.
    Только для владельца или администратора.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Только владелец или администратор может удалять модераторов.")
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("❌ Формат: <code>/removemoderator [Telegram ID]</code>")
        return
    rem_id = int(parts[1])
    removed = await db_remove_moderator(rem_id)
    if removed:
        await msg.answer(f"✅ Модератор <code>{md_escape(str(rem_id))}</code> удалён.")
    else:
        await msg.answer(f"❌ Пользователь <code>{md_escape(str(rem_id))}</code> не найден среди модераторов.")


@dp.message(Command("listmoderators"))
async def cmd_listmoderators(msg: Message) -> None:
    """
    /listmoderators — список модераторов.
    Только для владельца или администратора.
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Только владелец или администратор может просматривать список модераторов.")
        return
    mods = await db_list_moderators()
    if not mods:
        await msg.answer(
            "🛡 Модераторов пока нет.\n"
            "Добавь через <code>/addmoderator [ID]</code>."
        )
        return
    lines = ["🛡 <b>Модераторы:</b>", ""]
    for tid, uname, added_at in mods:
        label = f"@{uname}" if uname else f"<code>{tid}</code>"
        lines.append(f"• {label} — добавлен {added_at[:10]}")
    lines.append(
        "\n<i>Доступны команды: /transfer, /swap, /addroles, /addrolesall, /merch, /loans</i>"
    )
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("hltv"))
async def cmd_hltv(msg: Message) -> None:
    """
    /hltv — расчёт официального HLTV Rating 2.0 по статистике игрока.
    Данные вводятся в свободном формате после команды.
    Пример:
    /hltv
    donk vs Vitality. 24 раунда, 26 киллов, 12 смертей, 98 ADR, 83.3% KAST, 3 опен-фрага, 1 опен-дез
    """
    

    text = msg.text or ""
    body = "\n".join(text.split("\n")[1:]).strip()

    USAGE = (
        "📊 <b>Калькулятор HLTV Rating 2.0</b>\n\n"
        "Напиши статистику в свободном формате после команды:\n\n"
        "<code>/hltv\n"
        "donk vs Vitality. 24 раунда, 26 киллов, 12 смертей,\n"
        "98 ADR, 83.3% KAST, 3 опен-фрага, 1 опен-дез</code>\n\n"
        "<i>Недостающие данные заменяются средними по про-сцене.</i>"
    )

    if not body:
        await msg.answer(USAGE)
        return

    SYSTEM_PROMPT = """Ты — специализированный ИИ-аналитик Standoff 2 и официальный калькулятор индивидуальной статистики игроков по точной формуле HLTV Rating 2.0.
Твоя задача — принимать статистику игрока, рассчитывать его рейтинг по математической модели HLTV 2.0 и выдавать структурированный отчет.

### 📐 МАТЕМАТИЧЕСКАЯ МОДЕЛЬ HLTV RATING 2.0:
Rating 2.0 = (0.007387 * KAST) + (0.359123 * KPR) + (-0.532934 * DPR) + (0.237233 * Impact) + (0.003235 * ADR) + 0.1587

Где:
1. KAST — процент раундов с полезным действием (например, 72.5).
2. KPR (Kills Per Round) = Kills / Rounds.
3. DPR (Deaths Per Round) = Deaths / Rounds.
4. ADR — средний урон за раунд.
5. Impact = (1.61 * KPR) + (1.35 * (Opening_Kills / Rounds)) - (1.10 * (Opening_Deaths / Rounds)) + (0.05 * MultiKills_Factor) - 0.1
   Если Opening Kills/Deaths неизвестны: Impact = (1.61 * KPR) - 0.1 (+0.05 за каждый клатч 1vX).

### ⚠️ ДЕФОЛТНЫЕ ЗНАЧЕНИЯ (если данных нет):
- ADR = 75.0
- KAST = 71.5%
- Impact рассчитывается упрощённо по KPR.

### 📤 ШАБЛОН ОТВЕТА (строго этот формат, HTML-теги Telegram):
📊 <b>СТАТИСТИЧЕСКИЙ ОТЧЕТ HLTV RATING 2.0</b>

<b>Игрок:</b> [Никнейм] ([Команда])
<b>Матч/Карта:</b> [Название, если указано]
<b>Всего раундов:</b> [Количество]

⚔️ <b>Индивидуальные показатели:</b>
• K/D: <b>[Kills] / [Deaths]</b> ([Разница +/-])
• KPR: <b>[KPR]</b>
• DPR: <b>[DPR]</b>
• ADR: <b>[ADR]</b>
• KAST: <b>[KAST]%</b>

💥 <b>Показатели Импакта:</b>
• Entry (Опен-фраги): <b>[OK] - [OD]</b> ([Разница +/-])
• Клатчи / Мульти-киллы: [Данные или —]
• Рассчитанный Impact: <b>[Значение]</b>

━━━━━━━━━━━━━━━━━━━━━━━━
🏆 <b>ИТОГОВЫЙ HLTV 2.0 RATING: [ЗНАЧЕНИЕ]</b>

📈 <b>Аналитика:</b> [1-2 предложения профессионального анализа перформанса]

Отвечай ТОЛЬКО по этому шаблону, без лишнего текста."""

    wait_msg = await msg.answer("⏳ <i>Считаю рейтинг...</i>")

    if not ANTHROPIC_API_KEY:
        await wait_msg.delete()
        await msg.answer("❌ ANTHROPIC_API_KEY не задан. Попроси владельца добавить ключ в переменные окружения.")
        return

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": body}]
            }
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                data=json.dumps(payload),
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    err_body = await resp.text()
                    logger.error(f"Anthropic API error {resp.status}: {err_body[:200]}")
                    await wait_msg.delete()
                    await msg.answer(f"❌ Ошибка API ({resp.status}). Попробуй позже.")
                    return
                data = await resp.json()

        result = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                result += block["text"]

        if not result:
            await wait_msg.delete()
            await msg.answer("❌ Аналитик вернул пустой ответ. Попробуй позже.")
            return

        await wait_msg.delete()
        try:
            await msg.answer(result)
        except Exception:
            # Fallback: strip parse_mode if Telegram rejects the HTML
            await msg.answer(result, parse_mode=None)

    except aiohttp.ClientError as e:
        logger.error(f"HLTV network error: {e}")
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await msg.answer("❌ Сетевая ошибка при обращении к аналитику. Попробуй позже.")
    except Exception as e:
        logger.error(f"HLTV AI error: {e}", exc_info=True)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await msg.answer("❌ Внутренняя ошибка. Попробуй позже.")


# ═══════════════════════════════════════════════════════════════════════════
# HLTV-СТИЛЬ: ИНДИВИДУАЛЬНАЯ СТАТИСТИКА ИГРОКА (таблица hltv_players)
# ═══════════════════════════════════════════════════════════════════════════

HLTV_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _fmt_signed(value: float) -> str:
    return f"+{value:g}" if value > 0 else f"{value:g}"


async def db_hltv_seed() -> int:
    """Больше не заполняет hltv_players тестовыми/демо-игроками — таблица наполняется
    только реальными игроками команд, а их статистика обновляется с турнирных матчей
    (см. db_hltv_ensure_player / db_hltv_sync_from_tournaments)."""
    return 0


async def db_hltv_ensure_player(nickname: str, team_name: str = "") -> None:
    """Создаёт запись игрока в hltv_players с нулевой статистикой, если её ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO hltv_players (nickname, team) VALUES (?,?)",
            (nickname, team_name)
        )
        await db.commit()


async def db_hltv_set_team(nickname: str, team_name: str) -> None:
    """Обновляет команду игрока в hltv_players (создаёт запись, если её не было)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO hltv_players (nickname, team) VALUES (?,?) "
            "ON CONFLICT(nickname) DO UPDATE SET team=excluded.team",
            (nickname, team_name)
        )
        await db.commit()


async def db_hltv_sync_from_tournaments(nick: str, team_name: str) -> None:
    """Пересчитывает статистику игрока в hltv_players на основе суммарных
    турнирных данных (tournament_player_stats) — вызывается после каждого
    турнирного матча, чтобы /hltv_top и /card_player всегда показывали
    актуальную статистику «с турниров»."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COALESCE(SUM(kills),0), COALESCE(SUM(deaths),0), COALESCE(SUM(assists),0),
                      COALESCE(SUM(adr_dmg),0), COALESCE(SUM(kast_rounds),0), COALESCE(SUM(total_rounds),0),
                      COALESCE(SUM(entry_k),0), COALESCE(SUM(entry_d),0),
                      COALESCE(SUM(mk3),0), COALESCE(SUM(mk4),0), COALESCE(SUM(mk5),0),
                      COALESCE(SUM(clutches_won),0), COALESCE(SUM(util_dmg),0), COALESCE(SUM(flash_assists),0)
               FROM tournament_player_stats WHERE nick=? COLLATE NOCASE""",
            (nick,)
        ) as cursor:
            row = await cursor.fetchone()

        (kills, deaths, assists, adr_dmg, kast_rounds, total_rounds,
         entry_k, entry_d, mk3, mk4, mk5, clutches_won, util_dmg, flash_assists) = row

        rounds = max(1, total_rounds)
        kd  = round(kills / deaths, 2) if deaths > 0 else float(kills)
        dpr = round(deaths / rounds, 2)
        kpr = round(kills / rounds, 2)
        stats_list = [kills, deaths, assists, adr_dmg, kast_rounds, entry_k, entry_d,
                      mk3, mk4, mk5, clutches_won, util_dmg, flash_assists]
        rating = compute_hltv30(stats_list, rounds)
        mk_score = mk3 * 1.2 + mk4 * 1.4 + mk5 * 1.6
        entry_net = entry_k - entry_d
        impact = round((mk_score + max(0.0, entry_net * 0.05) + clutches_won * 0.05) / 2.0, 2)

        await db.execute(
            """INSERT INTO hltv_players
                   (nickname, team, kills, deaths, assists, rating, kd, dpr, kpr, impact)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(nickname) DO UPDATE SET
                   team=excluded.team, kills=excluded.kills, deaths=excluded.deaths,
                   assists=excluded.assists, rating=excluded.rating, kd=excluded.kd,
                   dpr=excluded.dpr, kpr=excluded.kpr, impact=excluded.impact""",
            (nick, team_name, kills, deaths, assists, rating, kd, dpr, kpr, impact)
        )
        await db.commit()


async def db_hltv_add_match_maps(nick: str, maps_played: int, maps_won: int, maps_lost: int) -> None:
    """Прибавляет сыгранные/выигранные/проигранные карты игроку после турнирного матча."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO hltv_players (nickname, maps_played, maps_won, maps_lost)
               VALUES (?,?,?,?)
               ON CONFLICT(nickname) DO UPDATE SET
                   maps_played = maps_played + excluded.maps_played,
                   maps_won    = maps_won    + excluded.maps_won,
                   maps_lost   = maps_lost   + excluded.maps_lost""",
            (nick, maps_played, maps_won, maps_lost)
        )
        await db.commit()


async def db_hltv_get_top(limit: int = 50) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT nickname, team, kills, deaths, assists, kd, rating, mvp_count
               FROM hltv_players
               ORDER BY rating DESC, kd DESC, kills DESC, mvp_count DESC
               LIMIT ?""",
            (limit,)
        ) as cursor:
            return await cursor.fetchall()


async def db_hltv_get_player(nickname: str) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM hltv_players WHERE nickname=? COLLATE NOCASE",
            (nickname,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


async def db_hltv_add_win_team(nicknames: list[str], tournament: str) -> tuple[list[str], list[str]]:
    """+1 к tournaments_won и добавление турнира в tournaments_list. Возвращает (найдены, не найдены)."""
    found, not_found = [], []
    async with aiosqlite.connect(DB_PATH) as db:
        for nick in nicknames:
            async with db.execute(
                "SELECT nickname, tournaments_list FROM hltv_players WHERE nickname=? COLLATE NOCASE",
                (nick,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                not_found.append(nick)
                continue
            real_nick, current_list = row
            new_list = f"{current_list}, {tournament}" if current_list else tournament
            await db.execute(
                "UPDATE hltv_players SET tournaments_won = tournaments_won + 1, tournaments_list=? WHERE nickname=?",
                (new_list, real_nick)
            )
            found.append(real_nick)
        await db.commit()
    return found, not_found


async def db_hltv_add_evp_team(nicknames: list[str], tournament: str) -> tuple[list[str], list[str]]:
    """+1 к evp_count и добавление турнира в evp_list. Возвращает (найдены, не найдены)."""
    found, not_found = [], []
    async with aiosqlite.connect(DB_PATH) as db:
        for nick in nicknames:
            async with db.execute(
                "SELECT nickname, evp_list FROM hltv_players WHERE nickname=? COLLATE NOCASE",
                (nick,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                not_found.append(nick)
                continue
            real_nick, current_list = row
            new_list = f"{current_list}, {tournament}" if current_list else tournament
            await db.execute(
                "UPDATE hltv_players SET evp_count = evp_count + 1, evp_list=? WHERE nickname=?",
                (new_list, real_nick)
            )
            found.append(real_nick)
        await db.commit()
    return found, not_found


async def db_hltv_add_mvp(nickname: str, tournament: str) -> str | None:
    """+1 к mvp_count и добавление турнира в mvp_list для одного игрока.
    Возвращает реальный никнейм при успехе, иначе None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT nickname, mvp_list FROM hltv_players WHERE nickname=? COLLATE NOCASE",
            (nickname,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        real_nick, current_list = row
        new_list = f"{current_list}, {tournament}" if current_list else tournament
        await db.execute(
            "UPDATE hltv_players SET mvp_count = mvp_count + 1, mvp_list=? WHERE nickname=?",
            (new_list, real_nick)
        )
        await db.commit()
    return real_nick


@dp.message(Command("hltv_top"))
async def cmd_hltv_top(msg: Message) -> None:
    """
    /hltv_top — Топ-50 игроков по Rating 3.0, K/D, Киллам, MVP (в этом порядке).
    """
    rows = await db_hltv_get_top(50)
    if not rows:
        await msg.answer(
            "📭 В базе пока нет ни одного игрока.\n"
            "Игроки появятся автоматически при первом запуске бота, "
            "либо администратор может добавить их напрямую в БД."
        )
        return

    lines = ["🏆 <b>HLTV TOP-50</b>", ""]
    for i, (nickname, team, kills, deaths, assists, kd, rating, mvp_count) in enumerate(rows, start=1):
        place = HLTV_MEDALS.get(i, f"{i}.")
        lines.append(
            f"{place} <b>{md_escape(nickname)}</b> | "
            f"🎯 {kills}/{deaths}/{assists} | "
            f"К/Д {kd:g} | "
            f"Rating 3.0 {rating:g} | "
            f"🏅 {mvp_count}"
        )
    full_text = "\n".join(lines)
    await safe_send_long(msg, full_text)


@dp.message(Command("card_player"))
async def cmd_card_player(msg: Message) -> None:
    """
    /card_player <ник_игрока> — подробная карточка игрока.
    """
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await msg.answer("❌ Формат: <code>/card_player Ник</code>")
        return

    nickname = parts[1].strip()
    player = await db_hltv_get_player(nickname)
    if not player:
        await msg.answer(f"❌ Игрок «{md_escape(nickname)}» не найден в базе.")
        return

    tournaments_list = player["tournaments_list"] or "—"
    mvp_list = player["mvp_list"] or "—"
    evp_list = player["evp_list"] or "—"
    team = player["team"] or "Без команды"

    text = (
        f"👤 <b>{md_escape(player['nickname'].upper())}</b>\n"
        f"🚩 Команда: <b>{md_escape(team)}</b>\n\n"
        f"⚔️ <b>Статистика:</b> {player['kills']} / {player['deaths']} / {player['assists']}\n\n"
        f"📊 <b>Рейтинг и Метрики:</b>\n"
        f"  • Rating 3.0: <b>{player['rating']:g}</b>\n"
        f"  • K/D: <b>{player['kd']:g}</b>\n"
        f"  • DPR: <b>{player['dpr']:g}</b>\n"
        f"  • KPR: <b>{player['kpr']:g}</b>\n"
        f"  • Impact: <b>{player['impact']:g}</b>\n\n"
        f"🗺 <b>Карты:</b> {player['maps_played']} "
        f"(🟢 {player['maps_won']} / 🔴 {player['maps_lost']})\n\n"
        f"🏆 <b>Достижения и Награды:</b>\n"
        f"  • Выигранные турниры: <b>{player['tournaments_won']}</b> ({md_escape(tournaments_list)})\n"
        f"  • MVP: <b>{player['mvp_count']}</b> ({md_escape(mvp_list)})\n"
        f"  • EVP: <b>{player['evp_count']}</b> ({md_escape(evp_list)})"
    )
    await safe_send_long(msg, text)


@dp.message(Command("add_win_team"))
async def cmd_add_win_team(msg: Message) -> None:
    """
    /add_win_team Ник1, Ник2, Ник3 | Название Турнира
    Массово выдаёт победу на турнире (+1 tournaments_won, добавляет турнир в список).
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2 or "|" not in parts[1]:
        await msg.answer(
            "❌ Формат: <code>/add_win_team Ник1, Ник2, Ник3 | Название Турнира</code>"
        )
        return

    nicks_raw, tournament = parts[1].split("|", 1)
    tournament = tournament.strip()
    nicknames = [n.strip() for n in nicks_raw.split(",") if n.strip()]

    if not nicknames or not tournament:
        await msg.answer("❌ Укажи хотя бы одного игрока и название турнира.")
        return

    found, not_found = await db_hltv_add_win_team(nicknames, tournament)

    lines = [f"🏆 <b>Победа на турнире:</b> {md_escape(tournament)}", ""]
    if found:
        lines.append("✅ <b>Зачислено:</b>")
        lines.extend(f"  • {md_escape(n)}" for n in found)
    if not_found:
        lines.append("")
        lines.append("❌ <b>Не найдены:</b>")
        lines.extend(f"  • {md_escape(n)}" for n in not_found)
    await safe_send_long(msg, "\n".join(lines))


@dp.message(Command("add_evp_team"))
async def cmd_add_evp_team(msg: Message) -> None:
    """
    /add_evp_team Ник1, Ник2 | Название Турнира
    Массово выдаёт EVP на турнире (+1 evp_count, добавляет турнир в список).
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2 or "|" not in parts[1]:
        await msg.answer(
            "❌ Формат: <code>/add_evp_team Ник1, Ник2 | Название Турнира</code>"
        )
        return

    nicks_raw, tournament = parts[1].split("|", 1)
    tournament = tournament.strip()
    nicknames = [n.strip() for n in nicks_raw.split(",") if n.strip()]

    if not nicknames or not tournament:
        await msg.answer("❌ Укажи хотя бы одного игрока и название турнира.")
        return

    found, not_found = await db_hltv_add_evp_team(nicknames, tournament)

    lines = [f"🥈 <b>EVP на турнире:</b> {md_escape(tournament)}", ""]
    if found:
        lines.append("✅ <b>Зачислено:</b>")
        lines.extend(f"  • {md_escape(n)}" for n in found)
    if not_found:
        lines.append("")
        lines.append("❌ <b>Не найдены:</b>")
        lines.extend(f"  • {md_escape(n)}" for n in not_found)
    await safe_send_long(msg, "\n".join(lines))


@dp.message(Command("add_mvp"))
async def cmd_add_mvp(msg: Message) -> None:
    """
    /add_mvp Ник | Название Турнира
    Выдаёт MVP турнира одному игроку (+1 mvp_count, добавляет турнир в список).
    """
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2 or "|" not in parts[1]:
        await msg.answer(
            "❌ Формат: <code>/add_mvp Ник | Название Турнира</code>"
        )
        return

    nick_raw, tournament = parts[1].split("|", 1)
    nickname = nick_raw.strip()
    tournament = tournament.strip()

    if not nickname or not tournament:
        await msg.answer("❌ Укажи ник игрока и название турнира.")
        return

    real_nick = await db_hltv_add_mvp(nickname, tournament)
    if real_nick:
        await msg.answer(
            f"🥇 MVP турнира <b>{md_escape(tournament)}</b> выдан игроку "
            f"<b>{md_escape(real_nick)}</b>."
        )
    else:
        await msg.answer(f"❌ Игрок «{md_escape(nickname)}» не найден в базе.")


# ═════════════════════════════════════════════════════════════════════════
# FACEIT: уровни и Elo, симуляция матчей 5x5, топ лиги, карточка профиля
# ═════════════════════════════════════════════════════════════════════════

# Оригинальная шкала уровней FACEIT — верхние границы Elo для LVL 1..10.
# (LVL9 официально начинается сразу после LVL8, поэтому диапазон 1651–1660
#  из ТЗ считается частью LVL9 — иначе эти 10 очков Elo не принадлежали бы
#  ни одному уровню.)
FACEIT_LEVELS: list[tuple[int, int]] = [
    (1, 500), (2, 750), (3, 900), (4, 1050), (5, 1200),
    (6, 1350), (7, 1500), (8, 1650), (9, 2000), (10, 10 ** 9),
]
FACEIT_LEVEL_ICONS = {
    1: "⬜", 2: "⬜", 3: "🟨", 4: "🟨", 5: "🟧",
    6: "🟧", 7: "🟥", 8: "🟥", 9: "🟪", 10: "🟪",
}
FACEIT_STARTING_ELO = 1000
FACEIT_ELO_MIN = 100
FACEIT_ELO_CHANGE_RANGE = (20, 30)   # "~25 Elo" за матч, с небольшим разбросом
FACEIT_PAGE_SIZE = 6                 # ровно 6 игроков на странице пагинации

FACEIT_MATCH_COOLDOWN_SECONDS = 120        # КД 2 минуты между матчами для одной команды
FACEIT_LAST_PLAYED: dict[int, datetime] = {}   # team_id -> время последнего запуска матча


def faceit_level(elo: int) -> int:
    """Определяет FACEIT LVL (1–10) по количеству Elo согласно оригинальной шкале."""
    for lvl, upper in FACEIT_LEVELS:
        if elo <= upper:
            return lvl
    return 10


def faceit_next_threshold(elo: int) -> int | None:
    """Elo, необходимое для перехода на следующий уровень. None, если уже LVL 10."""
    lvl = faceit_level(elo)
    if lvl >= 10:
        return None
    return FACEIT_LEVELS[lvl - 1][1] + 1


# ── База данных FACEIT ──────────────────────────────────────────────────

async def db_faceit_get(nick: str, team_name: str) -> tuple:
    """Возвращает (elo, matches, wins, losses, kills, deaths, headshots),
    создавая запись со стартовым Elo при первом обращении к игроку."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT elo, matches, wins, losses, kills, deaths, headshots "
            "FROM faceit_stats WHERE nick=?",
            (nick,)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return row
        await db.execute(
            "INSERT INTO faceit_stats (nick, team_name, elo) VALUES (?,?,?)",
            (nick, team_name, FACEIT_STARTING_ELO)
        )
        await db.commit()
    return (FACEIT_STARTING_ELO, 0, 0, 0, 0, 0, 0)


async def db_faceit_top(limit: int = 10) -> list[tuple]:
    """Топ игроков лиги по Elo (nick, team_name, elo, matches, wins)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT nick, team_name, elo, matches, wins FROM faceit_stats "
            "ORDER BY elo DESC, wins DESC LIMIT ?",
            (limit,)
        ) as cursor:
            return await cursor.fetchall()


async def db_get_current_team(nick: str) -> str | None:
    """Актуальная команда игрока (на случай, если он был трансферован после матча)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT team_name FROM players WHERE nick=? COLLATE NOCASE", (nick,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def db_faceit_apply_match_result(
    winners: list[str],
    losers: list[str],
    stats: dict[str, tuple[int, int, int]],   # nick -> (kills, deaths, headshots)
    team_of: dict[str, str],
) -> dict[str, tuple[int, int]]:
    """Обновляет Elo/статистику всех 10 участников матча.
    Возвращает {nick: (elo_до, elo_после)}."""
    changes: dict[str, tuple[int, int]] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for nick in winners + losers:
            team_name = team_of[nick]
            async with db.execute(
                "SELECT elo FROM faceit_stats WHERE nick=?", (nick,)
            ) as cursor:
                row = await cursor.fetchone()

            old_elo = row[0] if row else FACEIT_STARTING_ELO
            if row is None:
                await db.execute(
                    "INSERT INTO faceit_stats (nick, team_name, elo) VALUES (?,?,?)",
                    (nick, team_name, FACEIT_STARTING_ELO)
                )

            delta = random.randint(*FACEIT_ELO_CHANGE_RANGE)
            is_win = nick in winners
            new_elo = old_elo + delta if is_win else max(FACEIT_ELO_MIN, old_elo - delta)
            k, d, hs = stats.get(nick, (0, 0, 0))

            await db.execute(
                "UPDATE faceit_stats SET elo=?, team_name=?, matches=matches+1, "
                "wins=wins+?, losses=losses+?, kills=kills+?, deaths=deaths+?, "
                "headshots=headshots+?, updated_at=? WHERE nick=?",
                (new_elo, team_name, int(is_win), int(not is_win), k, d, hs,
                 datetime.now().isoformat(timespec="seconds"), nick)
            )
            changes[nick] = (old_elo, new_elo)
        await db.commit()
    return changes


# ── Симуляция матча 5x5 ─────────────────────────────────────────────────

async def faceit_pick_random_opponents(exclude_nicks: set[str], count: int = 5) -> list[tuple]:
    """Собирает случайных соперников из ВСЕХ игроков лиги, которые есть в БД
    (не одна команда, а сборная из случайных игроков), исключая уже занятых
    в матче (свой состав). Возвращает список (id, nick, team_name, role, is_reserve)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, nick, team_name, role, is_reserve FROM players"
        ) as cursor:
            all_players = await cursor.fetchall()

    pool = [p for p in all_players if p[1] not in exclude_nicks]
    if len(pool) < count:
        return []
    return random.sample(pool, count)


def faceit_simulate_score(prob_a_win: float) -> tuple[int, int, bool]:
    """Определяет исход матча (с вероятностью prob_a_win за команду A)
    и генерирует счёт в формате Standoff 2 (победитель — 13 раундов)."""
    a_won = random.random() < prob_a_win
    loser_rounds = random.randint(2, 11)
    return (13, loser_rounds, True) if a_won else (loser_rounds, 13, False)


def faceit_gen_player_stats(total_rounds: int, is_winner: bool) -> tuple[int, int, int]:
    """Генерирует условные (kills, deaths, headshots) игрока за матч
    с небольшим статистическим перекосом в пользу победившей стороны."""
    bias = 1.15 if is_winner else 0.85
    kills = max(0, round(random.uniform(0.55, 0.95) * total_rounds * bias))
    deaths = max(1, round(random.uniform(0.55, 0.95) * total_rounds * (2 - bias)))
    headshots = round(kills * random.uniform(0.3, 0.6))
    return kills, deaths, headshots


async def faceit_play_match(own_team_id: int, key_player_ids: list[int]) -> str:
    """Проводит полный матч 5x5: 2 выбранных менеджером + 3 случайных игрока
    своей команды против сборной из 5 случайных игроков, найденных в БД
    (не обязательно одной команды). Обновляет Elo и возвращает готовый текст
    отчёта о матче."""
    own_team_name = await db_get_team_name_by_id(own_team_id)
    if not own_team_name:
        return "❌ Команда не найдена."

    roster = await db_get_roster_for_panel(own_team_name)   # (id, nick, role, is_reserve)
    by_id = {r[0]: r for r in roster}

    key_players = [by_id[pid] for pid in key_player_ids if pid in by_id]
    if len(key_players) != 2:
        return "❌ Не удалось определить выбранных ключевых игроков. Попробуйте снова через /faceit."

    remaining = [r for r in roster if r[0] not in key_player_ids]
    if len(remaining) < 3:
        return "❌ В составе команды недостаточно свободных игроков для сбора 5x5 (нужно минимум 5 всего)."

    auto_three = random.sample(remaining, 3)
    team_a_players = key_players + auto_three          # 5 x (id, nick, role, is_reserve)

    exclude_nicks = {p[1] for p in team_a_players}
    team_b_players = await faceit_pick_random_opponents(exclude_nicks, 5)
    # 5 x (id, nick, team_name, role, is_reserve)
    if not team_b_players:
        return "❌ В базе недостаточно других игроков, чтобы собрать случайных соперников (нужно минимум 5)."

    opp_label = "Случайные соперники"

    # Средний Elo обеих сторон (создаёт запись со стартовым Elo для новичков)
    team_a_elos = [(await db_faceit_get(nick, own_team_name))[0] for _, nick, _, _ in team_a_players]
    team_b_elos = [(await db_faceit_get(nick, team_name))[0] for _, nick, team_name, _, _ in team_b_players]
    avg_a = sum(team_a_elos) / len(team_a_elos)
    avg_b = sum(team_b_elos) / len(team_b_elos)

    # Стандартная формула ожидаемого результата по Elo
    prob_a_win = 1 / (1 + 10 ** ((avg_b - avg_a) / 400))
    score_a, score_b, a_won = faceit_simulate_score(prob_a_win)
    total_rounds = score_a + score_b
    map_name = random.choice(MAP_POOL)

    winners = [p[1] for p in (team_a_players if a_won else team_b_players)]
    losers = [p[1] for p in (team_b_players if a_won else team_a_players)]

    team_of: dict[str, str] = {}
    stats: dict[str, tuple[int, int, int]] = {}
    for pid, nick, role, is_res in team_a_players:
        team_of[nick] = own_team_name
        stats[nick] = faceit_gen_player_stats(total_rounds, a_won)
    for pid, nick, team_name, role, is_res in team_b_players:
        team_of[nick] = team_name
        stats[nick] = faceit_gen_player_stats(total_rounds, not a_won)

    changes = await db_faceit_apply_match_result(winners, losers, stats, team_of)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO match_history (team_a, team_b, result, score, played_at) VALUES (?,?,?,?,?)",
            (own_team_name, opp_label, "win" if a_won else "loss",
             f"{score_a}:{score_b}", datetime.now().isoformat(timespec="seconds"))
        )
        await db.commit()

    key_ids = set(key_player_ids)
    lines = [
        f"🎮 <b>FACEIT МАТЧ</b> — 🗺 {map_name}",
        f"<b>{md_escape(own_team_name)}</b>  {score_a} : {score_b}  <b>{md_escape(opp_label)}</b>",
        f"🏆 Победа: <b>{md_escape(own_team_name if a_won else opp_label)}</b>",
        "",
        f"👥 <b>{md_escape(own_team_name)}</b> (🔑 — ключевой игрок, 🎲 — доброр из состава):",
    ]
    for pid, nick, role, is_res in team_a_players:
        k, d, hs = stats[nick]
        old_elo, new_elo = changes[nick]
        delta = new_elo - old_elo
        sign = "+" if delta >= 0 else ""
        tag = "🔑" if pid in key_ids else "🎲"
        lines.append(
            f"  {tag} {md_escape(nick)} ({role}) — {k}/{d} (HS {hs}) — "
            f"Elo {old_elo}→{new_elo} ({sign}{delta}) LVL {faceit_level(new_elo)}"
        )

    lines.append("")
    lines.append(f"👥 <b>{md_escape(opp_label)}</b> (случайные игроки из БД):")
    for pid, nick, team_name, role, is_res in team_b_players:
        k, d, hs = stats[nick]
        old_elo, new_elo = changes[nick]
        delta = new_elo - old_elo
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"  • {md_escape(nick)} [{md_escape(team_name)}] ({role}) — {k}/{d} (HS {hs}) — "
            f"Elo {old_elo}→{new_elo} ({sign}{delta}) LVL {faceit_level(new_elo)}"
        )

    return "\n".join(lines)


# ── Клавиатуры ───────────────────────────────────────────────────────────

def faceit_main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Играть матч", callback_data="fc|play")],
        [InlineKeyboardButton(text="🏆 Топ FACEIT", callback_data="fc|top")],
    ])


def _faceit_sel_csv(selected: list[int]) -> str:
    """Кодирует список выбранных id игроков в callback_data (без пустой строки)."""
    return "-".join(str(x) for x in selected) if selected else "0"


def _faceit_parse_sel(sel_csv: str) -> list[int]:
    return [] if sel_csv == "0" else [int(x) for x in sel_csv.split("-")]


def faceit_roster_kb(team_id: int, page: int, roster: list[tuple], selected: list[int]) -> InlineKeyboardMarkup:
    """Клавиатура выбора состава: пагинация по 6 игроков + выбор 2 ключевых."""
    total_pages = max(1, math.ceil(len(roster) / FACEIT_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    page_items = roster[page * FACEIT_PAGE_SIZE: page * FACEIT_PAGE_SIZE + FACEIT_PAGE_SIZE]
    sel_csv = _faceit_sel_csv(selected)

    rows: list[list[InlineKeyboardButton]] = []
    for pid, nick, role, is_res in page_items:
        mark = "✅" if pid in selected else "☐"
        reserve_tag = " (рез.)" if is_res else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark} {nick} — {role}{reserve_tag}",
            callback_data=f"fc|tg|{team_id}|{page}|{sel_csv}|{pid}"
        )])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"fc|pg|{team_id}|{page - 1}|{sel_csv}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"fc|pg|{team_id}|{page + 1}|{sel_csv}"))
    if nav:
        rows.append(nav)

    if len(selected) == 2:
        rows.append([InlineKeyboardButton(
            text="▶️ Начать матч (выбрано 2/2)", callback_data=f"fc|go|{team_id}|{sel_csv}"
        )])
    else:
        rows.append([InlineKeyboardButton(
            text=f"Выбрано ключевых игроков: {len(selected)}/2", callback_data="fc|noop"
        )])

    rows.append([InlineKeyboardButton(text="🔙 В меню FACEIT", callback_data="fc|menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def faceit_show_roster(call: CallbackQuery, team_id: int, page: int, selected: list[int]) -> None:
    team_name = await db_get_team_name_by_id(team_id)
    if not team_name:
        await call.answer("❌ Команда не найдена.", show_alert=True)
        return
    roster = await db_get_roster_for_panel(team_name)
    if len(roster) < 5:
        await call.answer("❌ В составе команды меньше 5 игроков — матч невозможен.", show_alert=True)
        return
    text = (
        f"🎮 <b>Играть матч — {md_escape(team_name)}</b>\n\n"
        f"Выберите <b>2 ключевых игроков</b> состава.\n"
        f"Остальные 3 участника матча 5x5 будут добраны случайно из "
        f"оставшихся свободных игроков этой команды."
    )
    await call.message.edit_text(text, reply_markup=faceit_roster_kb(team_id, page, roster, selected))
    await call.answer()


# ── Хендлеры команд FACEIT ──────────────────────────────────────────────

@dp.message(Command("faceit"))
async def cmd_faceit(msg: Message) -> None:
    """/faceit — главное меню системы FACEIT (Играть матч / Топ FACEIT)."""
    await msg.answer(
        "🟠 <b>FACEIT — Симулятор матчей лиги</b>\n\nВыберите действие:",
        reply_markup=faceit_main_menu_kb()
    )


@dp.message(Command("faceit_profile"))
async def cmd_faceit_profile(msg: Message) -> None:
    """/faceit_profile <ник> — карточка статистики FACEIT игрока."""
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await msg.answer("❌ Формат: <code>/faceit_profile Ник</code>")
        return
    nickname = parts[1].strip()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT nick, team_name, elo, matches, wins, losses, kills, deaths, headshots "
            "FROM faceit_stats WHERE nick=? COLLATE NOCASE",
            (nickname,)
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        await msg.answer(
            f"❌ У игрока «{md_escape(nickname)}» ещё нет статистики FACEIT.\n"
            f"Она появится после первого сыгранного матча (см. /faceit)."
        )
        return

    nick, stored_team, elo, matches, wins, losses, kills, deaths, headshots = row
    current_team = await db_get_current_team(nick)
    team_display = current_team or stored_team or "Без команды"

    lvl = faceit_level(elo)
    next_threshold = faceit_next_threshold(elo)
    to_next = f"осталось {next_threshold - elo} Elo до LVL {lvl + 1}" if next_threshold else "максимальный уровень"

    winrate = (wins / matches * 100) if matches else 0.0
    kd = (kills / deaths) if deaths else float(kills)
    hs_pct = (headshots / kills * 100) if kills else 0.0
    icon = FACEIT_LEVEL_ICONS.get(lvl, "⬜")

    text = (
        f"{icon} <b>{md_escape(nick.upper())}</b> — FACEIT LVL {lvl}\n"
        f"🚩 Команда: <b>{md_escape(team_display)}</b>\n\n"
        f"⚡ <b>Elo:</b> {elo} ({to_next})\n\n"
        f"📊 <b>Статистика матчей:</b>\n"
        f"  • Сыграно: <b>{matches}</b>\n"
        f"  • Победы: <b>{wins}</b>  /  Поражения: <b>{losses}</b>\n"
        f"  • Винрейт: <b>{winrate:.1f}%</b>\n\n"
        f"🎯 <b>Показатели Standoff 2:</b>\n"
        f"  • K/D: <b>{kd:.2f}</b> ({kills}/{deaths})\n"
        f"  • Headshots: <b>{hs_pct:.1f}%</b>"
    )
    await safe_send_long(msg, text)


# ── Callback-хендлеры FACEIT (callback_data начинается с "fc|") ────────

@dp.callback_query(F.data == "fc|menu")
async def cq_faceit_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "🟠 <b>FACEIT — Симулятор матчей лиги</b>\n\nВыберите действие:",
        reply_markup=faceit_main_menu_kb()
    )
    await call.answer()


@dp.callback_query(F.data == "fc|noop")
async def cq_faceit_noop(call: CallbackQuery) -> None:
    await call.answer("Нужно выбрать ровно 2 ключевых игроков.", show_alert=False)


@dp.callback_query(F.data == "fc|top")
async def cq_faceit_top(call: CallbackQuery) -> None:
    """Топ-10 игроков лиги по Elo (и, соответственно, по LVL)."""
    rows = await db_faceit_top(10)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню FACEIT", callback_data="fc|menu")]
    ])
    if not rows:
        await call.message.edit_text(
            "📭 Пока нет статистики FACEIT — сыграйте первый матч через «Играть матч».",
            reply_markup=kb
        )
        await call.answer()
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 <b>ТОП-10 FACEIT ЛИГИ</b>", ""]
    for i, (nick, team_name, elo, matches, wins) in enumerate(rows, start=1):
        place = medals.get(i, f"{i}.")
        lvl = faceit_level(elo)
        lines.append(
            f"{place} <b>{md_escape(nick)}</b> [{md_escape(team_name)}] — "
            f"LVL {lvl} · {elo} Elo · {wins}/{matches} побед"
        )
    await call.message.edit_text("\n".join(lines), reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "fc|play")
async def cq_faceit_play(call: CallbackQuery) -> None:
    """Начало сценария «Играть матч» — определяем, какой командой управляет менеджер."""
    teams = await db_get_leader_teams(call.from_user.id)
    if not teams:
        await call.answer("⛔ Вы не являетесь менеджером ни одной команды лиги.", show_alert=True)
        return

    if len(teams) == 1:
        team_id = await db_get_team_id_by_name(teams[0])
        await faceit_show_roster(call, team_id, page=0, selected=[])
        return

    rows = []
    for tname in teams:
        tid = await db_get_team_id_by_name(tname)
        rows.append([InlineKeyboardButton(text=tname, callback_data=f"fc|team|{tid}")])
    rows.append([InlineKeyboardButton(text="🔙 В меню FACEIT", callback_data="fc|menu")])
    await call.message.edit_text(
        "Вы менеджер нескольких команд. За какую сыграть матч?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("fc|team|"))
async def cq_faceit_team(call: CallbackQuery) -> None:
    team_id = int(call.data.split("|")[2])
    await faceit_show_roster(call, team_id, page=0, selected=[])


@dp.callback_query(F.data.startswith("fc|pg|"))
async def cq_faceit_page(call: CallbackQuery) -> None:
    """Переключение страницы пагинации (◀️ Назад / Вперёд ▶️)."""
    _, _, team_id, page, sel_csv = call.data.split("|")
    await faceit_show_roster(call, int(team_id), int(page), _faceit_parse_sel(sel_csv))


@dp.callback_query(F.data.startswith("fc|tg|"))
async def cq_faceit_toggle(call: CallbackQuery) -> None:
    """Клик по игроку — выбрать/снять как одного из 2 ключевых игроков."""
    _, _, team_id_s, page_s, sel_csv, pid_s = call.data.split("|")
    team_id, page, pid = int(team_id_s), int(page_s), int(pid_s)
    selected = _faceit_parse_sel(sel_csv)

    if pid in selected:
        selected.remove(pid)
    elif len(selected) < 2:
        selected.append(pid)
    else:
        await call.answer("Уже выбрано 2 ключевых игрока. Сначала снимите выбор с одного.", show_alert=False)
        return

    team_name = await db_get_team_name_by_id(team_id)
    roster = await db_get_roster_for_panel(team_name)
    await call.message.edit_reply_markup(reply_markup=faceit_roster_kb(team_id, page, roster, selected))
    await call.answer()


@dp.callback_query(F.data.startswith("fc|go|"))
async def cq_faceit_go(call: CallbackQuery) -> None:
    """Запуск симуляции матча 5x5 после выбора 2 ключевых игроков."""
    _, _, team_id_s, sel_csv = call.data.split("|")
    team_id = int(team_id_s)
    selected = _faceit_parse_sel(sel_csv)
    if len(selected) != 2:
        await call.answer("Нужно выбрать ровно 2 ключевых игроков.", show_alert=True)
        return

    # ── КД 2 минуты между матчами для одной команды ──
    now = datetime.now()
    last_played = FACEIT_LAST_PLAYED.get(team_id)
    if last_played is not None:
        elapsed = (now - last_played).total_seconds()
        if elapsed < FACEIT_MATCH_COOLDOWN_SECONDS:
            wait_left = math.ceil(FACEIT_MATCH_COOLDOWN_SECONDS - elapsed)
            await call.answer(
                f"⏳ Кулдаун матча: подождите ещё {wait_left} сек.",
                show_alert=True
            )
            return

    FACEIT_LAST_PLAYED[team_id] = now

    await call.answer("⏳ Симулируем матч...")
    result_text = await faceit_play_match(team_id, selected)
    await safe_send_long(call.message, result_text)
    await call.message.answer("Матч завершён.", reply_markup=faceit_main_menu_kb())


@dp.message(Command("faceit_reset_elo"))
async def cmd_faceit_reset_elo(msg: Message) -> None:
    """/faceit_reset_elo — сброс всей статистики FACEIT (Elo, матчи, победы,
    поражения, kills/deaths/headshots) до стартовых значений. Только для админов."""
    if not await db_is_admin(msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, сбросить", callback_data="fc|resetelo|yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="fc|resetelo|no"),
        ]
    ])
    await msg.answer(
        "⚠️ Вы уверены, что хотите сбросить <b>всю статистику FACEIT</b> "
        "(Elo, матчи, победы/поражения, kills/deaths/headshots) у "
        "<b>всех игроков</b> лиги до стартовых значений?\n\n"
        "Это действие необратимо.",
        reply_markup=kb
    )


@dp.callback_query(F.data == "fc|resetelo|yes")
async def cq_faceit_reset_elo_confirm(call: CallbackQuery) -> None:
    if not await db_is_admin(call.from_user.id):
        await call.answer("⛔ Только для администраторов.", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM faceit_stats")
        await db.commit()

    FACEIT_LAST_PLAYED.clear()

    await call.message.edit_text(
        f"✅ Статистика FACEIT сброшена. Все игроки начнут со стартового Elo "
        f"({FACEIT_STARTING_ELO})."
    )
    await call.answer("Готово.")


@dp.callback_query(F.data == "fc|resetelo|no")
async def cq_faceit_reset_elo_cancel(call: CallbackQuery) -> None:
    await call.message.edit_text("Отменено. Статистика FACEIT не изменена.")
    await call.answer()


async def main() -> None:
    await db_init()
    await db_seed_tournaments()   # автоматически заполняем турниры при старте
    await db_hltv_seed()          # больше не создаёт тестовых игроков — оставлено для совместимости
    hots_created, hots_errors = await hots_scan_and_load_rosters()
    logger.info("Регистрация команд из файлов: создано — %d, пропущено/ошибок — %d", len(hots_created), len(hots_errors))
    logger.info("БД инициализирована, запуск polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
