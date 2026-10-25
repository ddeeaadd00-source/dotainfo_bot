"""Telegram-бот со статистикой Dota 2 в стиле Dotabuff.

Меню бота:
  👤 Мой профиль   — сводка по твоему сохранённому профилю
  🎮 Мои матчи     — последние матчи, по номеру приходит картинка с таблицей матча
  🦸 Мои герои     — любимые герои
  🔍 Найти игрока  — поиск по ID, ссылке или нику (профиль, матчи и герои)

Данные берутся из открытого API OpenDota (https://docs.opendota.com).
Работает только с профилями, у которых включена публичная история матчей.
"""

import asyncio
import html
import io
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:  # без Pillow бот работает, но без картинок
    PIL_OK = False

API = "https://api.opendota.com/api"
# Открытые картинки героев и предметов с CDN Steam (запасной адрес на случай сбоя)
CDNS = [
    "https://cdn.cloudflare.steamstatic.com",
    "https://cdn.akamai.steamstatic.com",
]
STEAM64_BASE = 76561197960265728
DATA_FILE = Path(__file__).with_name("profiles.json")  # сохранённые профили

MEDALS = {
    1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
    5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal",
}

TABS = [("overview", "Обзор"), ("matches", "Матчи"), ("heroes", "Герои")]

GAME_MODES = {
    1: "All Pick", 2: "Captains Mode", 3: "Random Draft", 4: "Single Draft",
    5: "All Random", 12: "Least Played", 16: "Captains Draft",
    18: "Ability Draft", 22: "All Draft", 23: "Turbo",
}

BTN_PROFILE = "👤 Мой профиль"
BTN_MATCHES = "🎮 Мои матчи"
BTN_HEROES = "🦸 Мои герои"
BTN_FIND = "🔍 Найти игрока"
MENU_VIEWS = {BTN_PROFILE: "overview", BTN_MATCHES: "matches", BTN_HEROES: "heroes"}
MENU = ReplyKeyboardMarkup(
    [[BTN_PROFILE, BTN_MATCHES], [BTN_HEROES, BTN_FIND]], resize_keyboard=True
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dota-bot")

_heroes = {"data": {}, "short": {}, "ts": 0.0}
_items = {"data": {}, "ts": 0.0}
_img_cache = {}

START_TEXT = (
    "👋 Привет! Я Dota-бот: показываю статистику игроков в стиле Dotabuff.\n\n"
    "Что я умею:\n"
    "👤 Мой профиль — ранг, винрейт, победы и поражения\n"
    "🎮 Мои матчи — последние 10 игр. Нажми номер матча, и я пришлю "
    "картинку с таблицей: герои, K/D/A, золото, предметы\n"
    "🦸 Мои герои — на ком ты играешь чаще всего и какой у тебя винрейт\n"
    "🔍 Найти игрока — то же самое для любого другого игрока\n\n"
    "🚀 Как начать: нажми «👤 Мой профиль» и пришли свой Steam ID, ссылку "
    "на профиль (Dotabuff, OpenDota, Steam) или ник. Я запомню его, и "
    "дальше всё будет открываться одной кнопкой.\n\n"
    "💡 ID — это число в конце ссылки на Dotabuff "
    "(dotabuff.com/players/ЧИСЛО). В Dota 2 он показан как ID друга.\n\n"
    "⚠️ Чтобы я видел матчи, в настройках Dota 2 включи «Открыть "
    "публичную историю матчей». Иначе покажу только ранг."
)
ASK_ID_TEXT = "Пришли Steam ID, ссылку на профиль (Dotabuff, OpenDota, Steam) или ник."


# ---------- сохранённые профили ----------

def load_profiles() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get_saved(user_id: int):
    return load_profiles().get(str(user_id))


def save_profile(user_id: int, account_id: int):
    profiles = load_profiles()
    profiles[str(user_id)] = account_id
    DATA_FILE.write_text(json.dumps(profiles), encoding="utf-8")


# ---------- работа с API ----------

async def api_get(path: str, **params):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def hero_names() -> dict:
    """Словарь id героя -> название, кэш на сутки."""
    if not _heroes["data"] or time.time() - _heroes["ts"] > 86400:
        data = await api_get("/heroes")
        _heroes["data"] = {h["id"]: h["localized_name"] for h in data}
        _heroes["short"] = {
            h["id"]: h["name"].replace("npc_dota_hero_", "")
            for h in data
            if h.get("name")
        }
        _heroes["ts"] = time.time()
    return _heroes["data"]


# ---------- разбор ввода ----------

def parse_account_id(text: str):
    """Достаёт Steam32 ID из числа или ссылки. Возвращает int или None."""
    text = text.strip()
    link = re.search(
        r"(?:dotabuff\.com|opendota\.com|stratz\.com)/players/(\d+)", text
    ) or re.search(r"steamcommunity\.com/profiles/(\d+)", text)
    if link:
        text = link.group(1)
    if not text.isdigit():
        return None
    value = int(text)
    if value >= STEAM64_BASE:  # это Steam64, переводим в Steam32
        value -= STEAM64_BASE
    if not 0 < value < 2**32:
        return None
    return value


# ---------- форматирование ----------

def rank_name(rank_tier) -> str:
    if not rank_tier:
        return "не откалиброван или скрыт"
    medal = MEDALS.get(rank_tier // 10, "?")
    stars = rank_tier % 10
    return f"{medal} {stars}" if stars else medal


def winrate(wins: int, total: int) -> str:
    return f"{wins / total * 100:.1f}%" if total else "нет данных"


def is_win(match: dict) -> bool:
    return (match["player_slot"] < 128) == match["radiant_win"]


def keyboard(account_id: int, active: str, own: bool, extra=None) -> InlineKeyboardMarkup:
    """Три вкладки у любого игрока. Кнопка смены профиля только у своего."""
    tabs = [
        InlineKeyboardButton(
            ("• " if key == active else "") + title,
            callback_data=f"v:{key}:{account_id}",
        )
        for key, title in TABS
    ]
    links = [
        InlineKeyboardButton("Dotabuff", url=f"https://www.dotabuff.com/players/{account_id}"),
        InlineKeyboardButton("OpenDota", url=f"https://www.opendota.com/players/{account_id}"),
    ]
    rows = [tabs, *(extra or []), links]
    if own:
        rows.append([InlineKeyboardButton("✏️ Сменить профиль", callback_data="chg")])
    return InlineKeyboardMarkup(rows)


# ---------- вкладки ----------

async def render_overview(account_id: int) -> str:
    info, wl, recent = await asyncio.gather(
        api_get(f"/players/{account_id}"),
        api_get(f"/players/{account_id}/wl"),
        api_get(f"/players/{account_id}/recentMatches"),
    )
    prof = info.get("profile")
    if not prof:
        return "Игрок не найден. Проверь ID."

    wins, losses = wl.get("win", 0), wl.get("lose", 0)
    total = wins + losses
    lines = [
        f"👤 {prof.get('personaname') or 'Без имени'}",
        f"🏆 Ранг: {rank_name(info.get('rank_tier'))}",
        f"🎮 Матчей: {total}",
        f"✅ Победы: {wins}   ❌ Поражения: {losses}",
        f"📊 Винрейт: {winrate(wins, total)}",
    ]
    if recent:
        recent_wins = sum(is_win(m) for m in recent)
        lines.append(
            f"🔥 Последние {len(recent)}: {recent_wins} побед, "
            f"{len(recent) - recent_wins} поражений "
            f"({winrate(recent_wins, len(recent))})"
        )
    else:
        lines.append(
            "\nМатчей не видно. Скорее всего, история матчей скрыта "
            "в настройках Dota 2."
        )
    return "\n".join(lines)


async def render_matches(account_id: int):
    """Возвращает (текст, ряды кнопок с номерами матчей)."""
    recent, heroes = await asyncio.gather(
        api_get(f"/players/{account_id}/recentMatches"), hero_names()
    )
    if not recent:
        return (
            "Матчей не видно. Скорее всего, история матчей скрыта "
            "в настройках Dota 2."
        ), []
    lines = ["Последние матчи (нажми номер, чтобы открыть подробности):"]
    buttons = []
    for i, m in enumerate(recent[:10], start=1):
        hero = heroes.get(m["hero_id"], f"герой {m['hero_id']}")
        minutes, seconds = divmod(m["duration"], 60)
        lines.append(
            f"{i}. {'✅' if is_win(m) else '❌'} {hero} | "
            f"{m['kills']}/{m['deaths']}/{m['assists']} | "
            f"{minutes}:{seconds:02d}"
        )
        buttons.append(
            InlineKeyboardButton(str(i), callback_data=f"m:{m['match_id']}:{account_id}")
        )
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    return "\n".join(lines), rows


async def render_heroes(account_id: int) -> str:
    stats, heroes = await asyncio.gather(
        api_get(f"/players/{account_id}/heroes"), hero_names()
    )
    played = [h for h in stats if h.get("games", 0) > 0][:8]
    if not played:
        return (
            "Данных по героям нет. Скорее всего, история матчей скрыта "
            "в настройках Dota 2."
        )
    lines = ["Любимые герои (по числу матчей):"]
    for h in played:
        name = heroes.get(int(h["hero_id"]), f"герой {h['hero_id']}")
        lines.append(
            f"{name}: {h['games']} матчей, винрейт {winrate(h['win'], h['games'])}"
        )
    return "\n".join(lines)


RENDERERS = {
    "overview": render_overview,
    "matches": render_matches,
    "heroes": render_heroes,
}

API_DOWN = "OpenDota сейчас недоступен, попробуй позже."


async def render(view: str, account_id: int):
    """Возвращает (текст, дополнительные ряды кнопок) для вкладки."""
    if view == "matches":
        return await render_matches(account_id)
    return await RENDERERS[view](account_id), []


def fmt_k(value) -> str:
    return f"{(value or 0) / 1000:.1f}k"


async def render_match_detail(match_id: int, account_id: int, match=None):
    """Таблица матча как на Dotabuff. Возвращает (HTML-текст, кнопки)."""
    if match is None:
        match = await api_get(f"/matches/{match_id}")
    heroes = await hero_names()
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("← К матчам", callback_data=f"v:matches:{account_id}")],
        [
            InlineKeyboardButton("Dotabuff", url=f"https://www.dotabuff.com/matches/{match_id}"),
            InlineKeyboardButton("OpenDota", url=f"https://www.opendota.com/matches/{match_id}"),
        ],
    ])
    players = match.get("players") or []
    if not players:
        return "Подробности этого матча недоступны.", markup

    minutes, seconds = divmod(match.get("duration", 0), 60)
    winner = "Radiant" if match.get("radiant_win") else "Dire"
    mode = GAME_MODES.get(match.get("game_mode"), f"режим {match.get('game_mode')}")
    started = ""
    if match.get("start_time"):
        started = datetime.fromtimestamp(match["start_time"]).strftime("%d.%m.%Y %H:%M")

    def is_radiant(p):
        return p.get("isRadiant", p.get("player_slot", 0) < 128)

    def table(team):
        rows = [f" {'Герой':<10} {'K/D/A':<8}{'NW':>6}{'GPM':>5}"]
        for p in team:
            hero = heroes.get(p.get("hero_id"), "?")[:10]
            mark = "»" if p.get("account_id") == account_id else " "
            kda = f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)}"
            net = p.get("net_worth", p.get("total_gold"))
            rows.append(
                f"{mark}{hero:<10} {kda:<8}{fmt_k(net):>6}{p.get('gold_per_min', 0):>5}"
            )
        return html.escape("\n".join(rows))

    radiant = [p for p in players if is_radiant(p)]
    dire = [p for p in players if not is_radiant(p)]
    lines = [
        f"🏆 <b>Победа {winner}</b>",
        f"Счёт: {match.get('radiant_score', '?')} : {match.get('dire_score', '?')} "
        f"(Radiant : Dire)",
        f"⏱ {minutes}:{seconds:02d} · {html.escape(str(mode))}"
        + (f" · {started}" if started else ""),
        "",
        "<b>Radiant</b>",
        f"<pre>{table(radiant)}</pre>",
        "<b>Dire</b>",
        f"<pre>{table(dire)}</pre>",
        "» — выбранный игрок. NW — общая стоимость, GPM — золото в минуту.",
    ]

    me = next((p for p in players if p.get("account_id") == account_id), None)
    if me:
        lines += [
            "",
            "<b>Игрок</b>",
            f"Уровень: {me.get('level', '?')}, "
            f"LH/DN: {me.get('last_hits', 0)}/{me.get('denies', 0)}, "
            f"XPM: {me.get('xp_per_min', 0)}",
            f"Урон по героям: {me.get('hero_damage', 0)}, "
            f"по зданиям: {me.get('tower_damage', 0)}, "
            f"лечение: {me.get('hero_healing', 0)}",
        ]
    return "\n".join(lines), markup


# ---------- картинка матча ----------

BG = (17, 21, 29)
PANEL_A = (27, 33, 44)
PANEL_B = (23, 28, 38)
MUTED = (140, 150, 165)
WHITE = (236, 240, 246)

# (подпись, правый край колонки, функция значения)
STAT_COLS = [
    ("LV", 340, lambda p: str(p.get("level", 0))),
    ("K", 385, lambda p: str(p.get("kills", 0))),
    ("D", 425, lambda p: str(p.get("deaths", 0))),
    ("A", 465, lambda p: str(p.get("assists", 0))),
    ("NW", 545, lambda p: fmt_k(p.get("net_worth", p.get("total_gold")))),
    ("LH/DN", 635, lambda p: f"{p.get('last_hits', 0)}/{p.get('denies', 0)}"),
    ("GPM", 700, lambda p: str(p.get("gold_per_min", 0))),
    ("XPM", 765, lambda p: str(p.get("xp_per_min", 0))),
    ("HD", 845, lambda p: fmt_k(p.get("hero_damage"))),
]
ITEMS_X = 880


def load_font(size: int, bold: bool = False):
    names = (
        ["arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
        if bold
        else ["arial.ttf", "segoeui.ttf", "DejaVuSans.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


async def item_map() -> dict:
    """Словарь id предмета -> короткое имя для картинки, кэш на сутки."""
    if not _items["data"] or time.time() - _items["ts"] > 86400:
        data = await api_get("/constants/items")
        _items["data"] = {
            v["id"]: k for k, v in data.items() if "id" in v and not k.startswith("recipe_")
        }
        _items["ts"] = time.time()
    return _items["data"]


async def fetch_image(client, path: str):
    """Скачивает картинку с CDN Steam. Возвращает PIL-картинку или None."""
    if path in _img_cache:
        return _img_cache[path]
    for host in CDNS:
        try:
            r = await client.get(host + path)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            _img_cache[path] = img
            return img
        except Exception:
            continue
    return None


def draw_scoreboard(match: dict, account_id: int, heroes: dict, hero_icons: dict, item_icons: dict) -> bytes:
    """Рисует таблицу матча как на Dotabuff и возвращает PNG."""
    W, ROW_H, TEAM_H, COLS_H, HEADER_H = 1200, 56, 40, 28, 112
    players = match["players"]

    def radiant_of(p):
        return p.get("isRadiant", p.get("player_slot", 0) < 128)

    teams = [
        ("RADIANT", [p for p in players if radiant_of(p)], (104, 178, 70)),
        ("DIRE", [p for p in players if not radiant_of(p)], (214, 82, 66)),
    ]
    H = HEADER_H + sum(TEAM_H + COLS_H + max(len(t[1]), 1) * ROW_H + 18 for t in teams) + 6

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title, f_score = load_font(34, True), load_font(42, True)
    f_team, f_name, f_num = load_font(22, True), load_font(19), load_font(21)
    f_small, f_head = load_font(18), load_font(15, True)

    # шапка
    radiant_win = bool(match.get("radiant_win"))
    win_color = teams[0][2] if radiant_win else teams[1][2]
    d.text((24, 20), f"{'RADIANT' if radiant_win else 'DIRE'} VICTORY", font=f_title, fill=win_color)
    minutes, seconds = divmod(match.get("duration", 0), 60)
    mode = GAME_MODES.get(match.get("game_mode"), f"Mode {match.get('game_mode')}")
    sub = f"{minutes}:{seconds:02d}  |  {mode}"
    if match.get("start_time"):
        sub += "  |  " + datetime.fromtimestamp(match["start_time"]).strftime("%d.%m.%Y %H:%M")
    d.text((24, 72), sub, font=f_small, fill=MUTED)
    d.text((W - 24, 18), f"{match.get('radiant_score', '?')} : {match.get('dire_score', '?')}",
           font=f_score, fill=WHITE, anchor="ra")
    d.text((W - 24, 76), f"Match {match.get('match_id', '')}", font=f_small, fill=MUTED, anchor="ra")

    y = HEADER_H
    for team_name, team, color in teams:
        dark = tuple(int(c * 0.30) for c in color)
        d.rectangle((0, y, W, y + TEAM_H), fill=dark)
        d.rectangle((0, y, 6, y + TEAM_H), fill=color)
        d.text((22, y + TEAM_H // 2), team_name, font=f_team, fill=color, anchor="lm")
        kills = sum(p.get("kills", 0) for p in team)
        net = sum((p.get("net_worth", p.get("total_gold")) or 0) for p in team)
        d.text((W - 24, y + TEAM_H // 2), f"Kills {kills}    NW {fmt_k(net)}",
               font=f_small, fill=WHITE, anchor="rm")
        y += TEAM_H

        d.text((106, y + COLS_H // 2), "HERO", font=f_head, fill=MUTED, anchor="lm")
        for label, x, _ in STAT_COLS:
            d.text((x, y + COLS_H // 2), label, font=f_head, fill=MUTED, anchor="rm")
        d.text((ITEMS_X, y + COLS_H // 2), "ITEMS", font=f_head, fill=MUTED, anchor="lm")
        y += COLS_H

        for i, p in enumerate(team):
            mine = p.get("account_id") == account_id
            d.rectangle((0, y, W, y + ROW_H), fill=(58, 52, 26) if mine else (PANEL_A if i % 2 == 0 else PANEL_B))
            if mine:
                d.rectangle((0, y, 6, y + ROW_H), fill=(240, 200, 60))
            mid = y + ROW_H // 2

            icon = hero_icons.get(p.get("hero_id"))
            box = (16, mid - 22, 16 + 80, mid - 22 + 45)
            if icon:
                img.paste(icon.resize((80, 45), Image.LANCZOS), box[:2], icon.resize((80, 45), Image.LANCZOS))
            else:
                d.rectangle(box, fill=(40, 48, 62))
            d.text((106, mid), heroes.get(p.get("hero_id"), "?"), font=f_name, fill=WHITE, anchor="lm")

            for label, x, value in STAT_COLS:
                fill = WHITE
                if label == "K":
                    fill = (150, 220, 150)
                elif label == "D":
                    fill = (240, 130, 120)
                d.text((x, mid), value(p), font=f_num, fill=fill, anchor="rm")

            for slot in range(6):
                x = ITEMS_X + slot * 48
                top = mid - 16
                d.rectangle((x, top, x + 44, top + 32), fill=(14, 18, 25))
                item_id = p.get(f"item_{slot}")
                pic = item_icons.get(item_id) if item_id else None
                if pic:
                    small = pic.resize((44, 32), Image.LANCZOS)
                    img.paste(small, (x, top), small)
            y += ROW_H
        y += 18

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def render_match_image(match: dict, account_id: int) -> bytes:
    """Скачивает иконки героев и предметов и рисует таблицу матча."""
    heroes = await hero_names()
    try:
        items = await item_map()
    except httpx.HTTPError:
        items = {}
    players = match["players"]

    hero_paths = {}
    for p in players:
        short = _heroes["short"].get(p.get("hero_id"))
        if short:
            hero_paths[p["hero_id"]] = f"/apps/dota2/images/dota_react/heroes/{short}.png"
    item_paths = {}
    for p in players:
        for slot in range(6):
            item_id = p.get(f"item_{slot}")
            if item_id and item_id in items:
                item_paths[item_id] = f"/apps/dota2/images/dota_react/items/{items[item_id]}.png"

    async with httpx.AsyncClient(timeout=15) as client:
        hero_imgs, item_imgs = await asyncio.gather(
            asyncio.gather(*(fetch_image(client, path) for path in hero_paths.values())),
            asyncio.gather(*(fetch_image(client, path) for path in item_paths.values())),
        )
    hero_icons = dict(zip(hero_paths.keys(), hero_imgs))
    item_icons = dict(zip(item_paths.keys(), item_imgs))
    return await asyncio.to_thread(
        draw_scoreboard, match, account_id, heroes, hero_icons, item_icons
    )


def match_caption(match: dict, account_id: int) -> str:
    minutes, seconds = divmod(match.get("duration", 0), 60)
    mode = GAME_MODES.get(match.get("game_mode"), f"режим {match.get('game_mode')}")
    winner = "Radiant" if match.get("radiant_win") else "Dire"
    lines = [
        f"🏆 <b>Победа {winner}</b> · {match.get('radiant_score', '?')} : {match.get('dire_score', '?')}",
        f"⏱ {minutes}:{seconds:02d} · {html.escape(str(mode))}",
    ]
    me = next((p for p in match.get("players", []) if p.get("account_id") == account_id), None)
    if me:
        lines += [
            "",
            f"Игрок: уровень {me.get('level', '?')}, "
            f"LH/DN {me.get('last_hits', 0)}/{me.get('denies', 0)}, "
            f"XPM {me.get('xp_per_min', 0)}",
            f"Урон по героям {me.get('hero_damage', 0)}, "
            f"по зданиям {me.get('tower_damage', 0)}, "
            f"лечение {me.get('hero_healing', 0)}",
        ]
    return "\n".join(lines)


async def send_match(query, match_id: int, account_id: int):
    """Присылает картинку с таблицей матча (или текст, если картинку не вышло)."""
    try:
        match = await api_get(f"/matches/{match_id}")
    except httpx.HTTPError:
        await query.message.reply_text(API_DOWN)
        return

    links = InlineKeyboardMarkup([[
        InlineKeyboardButton("Dotabuff", url=f"https://www.dotabuff.com/matches/{match_id}"),
        InlineKeyboardButton("OpenDota", url=f"https://www.opendota.com/matches/{match_id}"),
    ]])
    photo = None
    if PIL_OK and match.get("players"):
        try:
            photo = await render_match_image(match, account_id)
        except Exception:
            log.exception("Не удалось нарисовать таблицу матча")
    if photo:
        await query.message.reply_photo(
            photo=photo,
            caption=match_caption(match, account_id),
            parse_mode="HTML",
            reply_markup=links,
        )
        return
    text, markup = await render_match_detail(match_id, account_id, match)
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ---------- показ игрока и поиск ----------

async def show_player(message, user_id: int, account_id: int, view: str = "overview"):
    own = get_saved(user_id) == account_id
    try:
        text, extra = await render(view, account_id)
    except httpx.HTTPError:
        await message.reply_text(API_DOWN)
        return
    await message.reply_text(text, reply_markup=keyboard(account_id, view, own, extra))


async def search_by_nick(message, query: str, mode: str, view: str):
    """mode: 'save' (привязываем свой профиль) или 'find' (смотрим чужой)."""
    try:
        results = await api_get("/search", q=query)
    except httpx.HTTPError:
        await message.reply_text(API_DOWN)
        return
    if not results:
        await message.reply_text("Никого не нашёл. Попробуй прислать ID.")
        return
    buttons = []
    for p in results[:5]:
        data = (
            f"s:{p['account_id']}:{view}"
            if mode == "save"
            else f"v:overview:{p['account_id']}"
        )
        buttons.append(
            [InlineKeyboardButton(f"{p['personaname']} · {p['account_id']}", callback_data=data)]
        )
    await message.reply_text(
        "Нашёл несколько игроков, выбери нужного:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def resolve_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, mode: str, view: str):
    """Разбирает ID, ссылку или ник и либо сохраняет профиль, либо показывает игрока."""
    message, user_id = update.message, update.effective_user.id

    if "steamcommunity.com/id/" in text:
        await message.reply_text(
            "По такой ссылке я не могу найти ID. Пришли число из ссылки "
            "на Dotabuff или просто ник."
        )
        return

    account_id = parse_account_id(text)
    if account_id is None:
        if 2 <= len(text) <= 40:
            context.user_data.pop("awaiting", None)
            await search_by_nick(message, text, mode, view)
        else:
            await message.reply_text(ASK_ID_TEXT)
        return

    if mode == "save":
        try:
            info = await api_get(f"/players/{account_id}")
        except httpx.HTTPError:
            await message.reply_text(API_DOWN)
            return
        if not info.get("profile"):
            await message.reply_text("Такой профиль не найден. Проверь ID и пришли ещё раз.")
            return
        save_profile(user_id, account_id)
        await message.reply_text("✅ Профиль сохранён.", reply_markup=MENU)
        context.user_data.pop("awaiting", None)
        await show_player(message, user_id, account_id, view)
    else:
        context.user_data.pop("awaiting", None)
        await show_player(message, user_id, account_id, "overview")


# ---------- обработчики Telegram ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)
    await update.message.reply_text(START_TEXT, reply_markup=MENU)


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text == BTN_FIND:
        context.user_data["awaiting"] = "find"
        await update.message.reply_text("Кого ищем? " + ASK_ID_TEXT)
        return

    view = MENU_VIEWS[text]
    account_id = get_saved(update.effective_user.id)
    if account_id is None:
        context.user_data["awaiting"] = f"save:{view}"
        await update.message.reply_text(
            "Сначала привяжем твой профиль, это нужно один раз. " + ASK_ID_TEXT
        )
        return
    context.user_data.pop("awaiting", None)
    await show_player(update.message, update.effective_user.id, account_id, view)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    if text in MENU_VIEWS or text == BTN_FIND:
        await on_menu(update, context, text)
        return

    awaiting = context.user_data.get("awaiting", "find")
    if awaiting.startswith("save"):
        view = awaiting.split(":", 1)[1] if ":" in awaiting else "overview"
        await resolve_input(update, context, text, "save", view)
    else:
        await resolve_input(update, context, text, "find", "overview")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "chg":
        await query.answer()
        context.user_data["awaiting"] = "save:overview"
        await query.message.reply_text("Пришлём новый профиль. " + ASK_ID_TEXT)
        return

    parts = data.split(":")
    try:
        if parts[0] == "s":  # выбрали себя из списка найденных по нику
            account_id, view = int(parts[1]), parts[2]
            if view not in RENDERERS:
                view = "overview"
            save_profile(user_id, account_id)
            context.user_data.pop("awaiting", None)
            await query.answer("Профиль сохранён")
            await show_player(query.message, user_id, account_id, view)
            return
        if parts[0] == "m":  # подробности матча: присылаем картинку
            match_id, account_id = int(parts[1]), int(parts[2])
            await query.answer("Готовлю картинку матча…")
            await send_match(query, match_id, account_id)
            return
        _, view, raw_id = parts
        account_id = int(raw_id)
        renderer = RENDERERS[view]
    except (ValueError, KeyError, IndexError):
        await query.answer()
        return

    own = get_saved(user_id) == account_id
    await query.answer()
    try:
        text, extra = await render(view, account_id)
    except httpx.HTTPError:
        await query.message.reply_text(API_DOWN)
        return
    try:
        await query.edit_message_text(text, reply_markup=keyboard(account_id, view, own, extra))
    except BadRequest:
        pass  # нажали на ту же вкладку, текст не изменился


def main():
    load_dotenv()  # читает токен из файла .env рядом с bot.py
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Добавь BOT_TOKEN в файл .env (токен от @BotFather).")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(v:|s:|m:|chg$)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
