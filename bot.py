"""
UFC Universe bot: Live Card DMs with check-in and reminders, fighter
profiles, fight logging, coins and betting, and fight contracts.

Staff (Owner, Admin, Mods):
  /dmlivecard     DM everyone you @mention that they're on a Live Card,
                  with a Check In button in the DM
  /livecheckin    post a board showing who has checked in (updates by
                  itself); close:True closes check-in
  /profile        (staff and anyone with a Fighter role) a fighter's profile like PFO. Overview: record, streak,
                  finish rate, wins/losses by KO/SUB/DEC, title wins, contract.
                  Fights: every fight, newest first
  /logfight       log a fight: method, winner, loser (or winner_left /
                  loser_left: user ID or name for someone who left), round,
                  card, date, title fight. Pays the fighters and settles bets
  /scanresults    read results from a channel, preview, then Import
  /fights         the fight log with fight numbers (filter by fighter,
                  card or title fights)
  /editfight      change a logged fight (pay and bets redo themselves)
  /removefight    delete a logged fight (pay and bets are undone)
  /resetresults   delete every logged fight
  /openbets       open betting on a card: auto odds or your own (1.60/2.30)
  /closebets      close betting early
  /voidfight      void one fight on a betting card (bets refunded)
  /givecoins      give or take coins
  /contract offer / view / end / list
                  fight contracts (DM to accept or decline), like PFO
  /rankingslink   link the owner's rankings sheet (Google Sheets or OneDrive);
                  re-read every 15 minutes. Only used for the odds, not shown
  /uploadrankings upload the rankings sheet instead of linking it
  /rankingscheck  privately see every fighter the bot read, and any skipped rows
  /rankingsedit link / rank / remove / list / undo
                  fix small mistakes; fixes stay on top of the sheet until undone

Owner:
  /payrates       change the base fight pay (anyone can view it)

Anyone:
  /help (shows staff commands to staff), /coins, /daily, /coinboard, /mybets, /serverstats, /payrates,
  /contract view (their own). Bets are placed from the sportsbook post.

Automatic:
  - Everyone on the latest /dmlivecard gets a reminder DM before the
    card starts (1 hour by default, REMINDER_HOURS in Railway). Anyone
    not checked in gets the Check In button again. A note of who was
    reminded (and who still hasn't checked in) goes in the Diddy-Blud
    channel (REMINDER_CHANNEL in Railway to change it).
  - Betting closes at its closing time; odds on the post update as people bet.
  - A linked rankings sheet is re-read every 15 minutes.
  - Rankings changes (sheet, upload or /rankingsedit) are posted in the
    uni-bot channel (RANKINGS_LOG_CHANNEL in Railway; "off" to stop).
"""

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

# Railway: set DATABASE_FILE=/data/ufcu.db and attach a volume at /data
DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "ufcu.db"
)

# Where the banners live, e.g.
#   https://raw.githubusercontent.com/<you>/<images-repo>/main/assets
# Without it the bot still works, just with no banners or logo.
ASSET_BASE_URL = os.getenv(
    "ASSET_BASE_URL",
    ""
).strip().rstrip("/")

# Bump (2, 3, ...) after changing the GIFs so Discord loads the new ones.
ASSET_VERSION = os.getenv(
    "ASSET_VERSION",
    "1"
).strip()

# Dates are typed in this timezone and shown to each fighter in theirs.
EVENT_TIMEZONE = os.getenv(
    "EVENT_TIMEZONE",
    "Europe/London"
).strip()

# Roles allowed to use the commands (comma-separated). Matching ignores
# capitals, spaces, dashes and emojis. The server owner always can.
STAFF_ROLE_NAMES = [
    name.strip()
    for name in os.getenv(
        "STAFF_ROLES",
        "Owner,Admin,Mods"
    ).split(",")
    if name.strip()
]

# Reminder DMs before a Live Card starts.
CARD_REMINDERS = os.getenv(
    "CARD_REMINDERS",
    "on"
).strip().lower() not in ("off", "no", "false", "0")

REMINDER_HOURS = float(
    os.getenv("REMINDER_HOURS", "1") or 1
)

# Where the "fighters have been reminded" note goes. A channel ID, or
# part of its name. Fancy letters and emojis are ignored, so "Diddy-Blud"
# finds "🩸│𝑫𝒊𝒅𝒅𝒚-𝑩𝒍𝒖𝒅".
REMINDER_CHANNEL = os.getenv(
    "REMINDER_CHANNEL",
    "Diddy-Blud"
).strip()


# ============================================================
# BRANDING
# ============================================================

BRAND_NAME = "UFC Universe"
BRAND_TAG = "UFC UNIVERSE"

BRAND_RED = discord.Color.from_rgb(214, 48, 46)

LOGO_FILE = "ufcu_logo.gif"
SELECTED_LIVE_CARD_BANNER = "selected_live_card.gif"
CHECKIN_BANNER = "checkin.gif"
CHECKED_IN_BANNER = "checked_in.gif"

# Optional banners (add these GIFs to the assets folder, or change the
# names in Railway; set them to "none" to show no banner).
REMINDER_BANNER = os.getenv("REMINDER_BANNER", "reminder.gif").strip()
PROFILE_BANNER = os.getenv("PROFILE_BANNER", "fighter_profile.gif").strip()
STATS_BANNER = os.getenv("STATS_BANNER", "server_stats.gif").strip()


def asset_url(
    filename: str
):

    if not ASSET_BASE_URL or not filename or filename.lower() == "none":
        return None

    return f"{ASSET_BASE_URL}/{filename}?v={ASSET_VERSION}"


def apply_branding(
    embed: discord.Embed,
    banner_file: str = None,
    author_text: str = None
):

    logo = asset_url(LOGO_FILE)
    banner = asset_url(banner_file) if banner_file else None

    if author_text:
        if logo:
            embed.set_author(name=author_text, icon_url=logo)
        else:
            embed.set_author(name=author_text)

    if logo:
        embed.set_thumbnail(url=logo)

    if banner:
        embed.set_image(url=banner)

    return embed


# ============================================================
# BOT SETUP
# ============================================================

# /scanresults needs to read message text: turn on "Message Content
# Intent" in the Discord Developer Portal (Bot tab) or the bot won't start.
intents = discord.Intents.default()
intents.message_content = True

# /uploadrankings matches names in the spreadsheet to members: turn on
# "Server Members Intent" in the Developer Portal (Bot tab) too.
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


def now_utc():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# DATABASE
# ============================================================

def get_db():

    connection = sqlite3.connect(DATABASE_FILE, timeout=15)
    connection.row_factory = sqlite3.Row

    return connection


def query(
    sql: str,
    params: tuple = (),
    write: bool = False
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute(sql, params)

    if write:
        db.commit()
        result = cursor.lastrowid
    else:
        result = cursor.fetchall()

    db.close()

    return result


def setup_database():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS card_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            event TEXT,
            event_date TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            board_channel_id INTEGER,
            board_message_id INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS card_fighters (
            notice_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            dm_status TEXT NOT NULL DEFAULT 'pending',
            checked_in_at TEXT,

            PRIMARY KEY(notice_id, user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fight_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            result_key TEXT NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            winner_id INTEGER NOT NULL,
            loser_id INTEGER NOT NULL,
            method TEXT NOT NULL,
            round INTEGER,
            fought_at TEXT,
            imported_by INTEGER,
            imported_at TEXT NOT NULL,

            UNIQUE(guild_id, result_key)
        )
    """)

    # Added later: the card a result was on.
    cursor.execute("PRAGMA table_info(fight_results)")

    if "card_name" not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE fight_results ADD COLUMN card_name TEXT")

    # Added later: the title on the line, if it was a title fight.
    cursor.execute("PRAGMA table_info(fight_results)")

    if "title" not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE fight_results ADD COLUMN title TEXT")

    # Added later: fighters' names when the fight was logged, so the name
    # still shows if they leave the server.
    cursor.execute("PRAGMA table_info(fight_results)")
    columns = [column[1] for column in cursor.fetchall()]

    for column in ("winner_name", "loser_name"):
        if column not in columns:
            cursor.execute(f"ALTER TABLE fight_results ADD COLUMN {column} TEXT")

    # Added later: remembers that a card's reminder DMs have gone out.
    cursor.execute("PRAGMA table_info(card_notices)")

    if "reminder_sent" not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE card_notices ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0")

    db.commit()
    db.close()


def create_notice(
    guild_id: int,
    created_by: int,
    fighters: list,
    event: str,
    event_date: str
):
    """fighters: list of (user_id, name)"""

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO card_notices
        (guild_id, event, event_date, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (guild_id, event, event_date, created_by, now_utc()))

    notice_id = cursor.lastrowid

    cursor.executemany("""
        INSERT INTO card_fighters (notice_id, user_id, position, name)
        VALUES (?, ?, ?, ?)
    """, [
        (notice_id, user_id, position, name)
        for position, (user_id, name) in enumerate(fighters)
    ])

    db.commit()
    db.close()

    return notice_id


def get_notice(
    notice_id: int
):

    rows = query("SELECT * FROM card_notices WHERE id = ?", (notice_id,))

    return rows[0] if rows else None


def get_latest_notice(
    guild_id: int
):

    rows = query("""
        SELECT * FROM card_notices
        WHERE guild_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (guild_id,))

    return rows[0] if rows else None


def get_active_notices():

    return query("SELECT * FROM card_notices WHERE active = 1")


def get_card_fighters(
    notice_id: int
):

    return query("""
        SELECT * FROM card_fighters
        WHERE notice_id = ?
        ORDER BY position ASC
    """, (notice_id,))


def get_card_fighter(
    notice_id: int,
    user_id: int
):

    rows = query("""
        SELECT * FROM card_fighters
        WHERE notice_id = ? AND user_id = ?
    """, (notice_id, user_id))

    return rows[0] if rows else None


def update_card_fighter(
    notice_id: int,
    user_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    query(
        f"UPDATE card_fighters SET {columns} WHERE notice_id = ? AND user_id = ?",
        (*changes.values(), notice_id, user_id),
        write=True
    )


def update_notice(
    notice_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    query(
        f"UPDATE card_notices SET {columns} WHERE id = ?",
        (*changes.values(), notice_id),
        write=True
    )


def get_meta(
    key: str
):

    rows = query("SELECT value FROM bot_meta WHERE key = ?", (key,))

    return rows[0]["value"] if rows else None


def set_meta(
    key: str,
    value: str
):

    query(
        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
        (key, value),
        write=True
    )


# ============================================================
# STAFF CHECK
# ============================================================

STAFF_ONLY_TEXT = (
    "Only staff ("
    + ", ".join(STAFF_ROLE_NAMES[:-1])
    + (" or " if len(STAFF_ROLE_NAMES) > 1 else "")
    + STAFF_ROLE_NAMES[-1]
    + ")"
)


def role_key(
    name: str
):
    """Lower case letters and numbers only. Fancy letters (𝑫𝒊𝒅𝒅𝒚) count as normal ones."""

    name = unicodedata.normalize("NFKC", name or "")

    return "".join(ch for ch in name.casefold() if ch.isalnum())


def is_staff(
    interaction: discord.Interaction
):

    if not interaction.guild:
        return False

    if interaction.user.id == interaction.guild.owner_id:
        return True

    wanted = {role_key(name) for name in STAFF_ROLE_NAMES}

    return any(
        role_key(role.name) in wanted
        for role in getattr(interaction.user, "roles", [])
    )


# Who can use /profile besides staff: any role whose name contains one of
# these words ("Fighter" matches "Fighter", "UFC Fighter", "Fighters"...).
# Change with PROFILE_ROLES in Railway, comma separated.
PROFILE_ROLE_WORDS = [
    role_key(name)
    for name in os.getenv("PROFILE_ROLES", "Fighter").split(",")
    if role_key(name)
]

PROFILE_ONLY_TEXT = "Only staff and fighters"


def can_view_profiles(
    interaction: discord.Interaction
):
    """Staff, or anyone with a Fighter role."""

    if is_staff(interaction):
        return True

    return any(
        word in role_key(role.name)
        for role in getattr(interaction.user, "roles", [])
        for word in PROFILE_ROLE_WORDS
    )


def find_reminder_channel(
    guild: discord.Guild
):

    return find_named_channel(guild, REMINDER_CHANNEL)


def find_named_channel(
    guild: discord.Guild,
    setting: str
):
    """A text channel by ID, or by name ignoring fancy letters, emojis and dashes."""

    setting = (setting or "").strip()

    if setting.isdigit():
        return guild.get_channel(int(setting))

    wanted = role_key(setting)

    if not wanted:
        return None

    for channel in getattr(guild, "text_channels", []):
        if wanted in role_key(channel.name):
            return channel

    return None


# ============================================================
# DATES AND TIMES
# ============================================================

def uk_offset_hours(
    naive: datetime
):
    """
    Fallback when the server has no timezone data. UK clocks are
    GMT, or BST (+1 hour) from the last Sunday of March (01:00) to
    the last Sunday of October (02:00 local).
    """

    def last_sunday(year, month):
        next_month = datetime(year + (month == 12), month % 12 + 1, 1)
        day = next_month - timedelta(days=1)
        return day - timedelta(days=(day.weekday() - 6) % 7)

    starts = last_sunday(naive.year, 3).replace(hour=1)
    ends = last_sunday(naive.year, 10).replace(hour=2)

    return 1 if starts <= naive < ends else 0


def to_utc(
    naive: datetime
):

    try:
        from zoneinfo import ZoneInfo
        return naive.replace(tzinfo=ZoneInfo(EVENT_TIMEZONE)).astimezone(timezone.utc)
    except Exception:
        offset = uk_offset_hours(naive) if EVENT_TIMEZONE == "Europe/London" else 0
        return (naive - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


def parse_event_datetime(
    text: str
):
    """
    Reads a date and time, e.g.
      24/10/2026 20:00   24/10/2026 8pm   Saturday 24/10 8:30pm   2026-10-24 20:00
    Returns a UTC datetime, or None if it can't be read.
    """

    text = " ".join((text or "").lower().replace(",", " ").replace(" at ", " ").split())
    text = re.sub(
        r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday|rsday)?\s+",
        "",
        text
    )

    match = re.fullmatch(
        r"(?:(?P<d>\d{1,2})[/.-](?P<m>\d{1,2})(?:[/.-](?P<y>\d{2,4}))?"
        r"|(?P<y2>\d{4})-(?P<m2>\d{1,2})-(?P<d2>\d{1,2}))"
        r"\s+(?P<h>\d{1,2})(?:[:.](?P<min>\d{2}))?\s*(?P<ampm>am|pm)?",
        text
    )

    if not match:
        return None

    if match.group("y2"):
        year, month, day = int(match.group("y2")), int(match.group("m2")), int(match.group("d2"))
    else:
        day, month = int(match.group("d")), int(match.group("m"))
        year = match.group("y")
        year = int(year) + (2000 if year and len(year) == 2 else 0) if year else None

    hour = int(match.group("h"))
    minute = int(match.group("min") or 0)
    ampm = match.group("ampm")

    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)

    now = datetime.now(timezone.utc)

    try:
        if year is None:
            year = now.year
            if to_utc(datetime(year, month, day, hour, minute)) < now:
                year += 1
        naive = datetime(year, month, day, hour, minute)
    except ValueError:
        return None

    return to_utc(naive)


def format_saved_time(
    value: str
):
    """A saved timestamp shown in each reader's own timezone."""

    if value and value.isdigit():
        return f"**<t:{value}:F>** (<t:{value}:R>)"

    return f"**{value}**" if value else ""


# ============================================================
# WHO GETS THE DM
# ============================================================

MAX_CARD_FIGHTERS = 60
DM_DELAY_SECONDS = 1.0  # gentle on Discord, avoids being blocked

MENTION_PATTERN = re.compile(r"<@!?(\d+)>")

# "Clay Guida 9-2-2-1" / "Matt Hughes (5-0)" → "Clay Guida" / "Matt Hughes"
RECORD_IN_NAME = re.compile(r"\s*\(?\s*\d{1,3}(?:\s*-\s*\d{1,3}){1,3}\s*\)?\s*$")


def clean_name(
    name: str
):

    return RECORD_IN_NAME.sub("", name or "").strip() or (name or "")


def fighter_name(
    guild,
    user_id: int,
    fallback: str = None
):
    """
    A fighter's server name written out (without their record), like PFO.
    Used instead of @mentions so nothing ever shows as a Discord ID.
    """

    member = guild.get_member(user_id) if guild and user_id else None

    if member:
        return clean_name(member.display_name)

    if fallback:
        return clean_name(fallback)

    user = bot.get_user(user_id) if user_id else None

    if user:
        return clean_name(user.display_name)

    return "Unknown fighter"


class CardInputError(Exception):
    pass


async def get_member(
    guild: discord.Guild,
    user_id: int
):

    member = guild.get_member(user_id)

    if member:
        return member

    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def collect_fighters(
    guild: discord.Guild,
    fighters_text: str
):
    """
    Everyone @mentioned, in the order typed, without repeats.
    Returns a list of (user_id, name, member or None).
    """

    order = []

    for found in MENTION_PATTERN.findall(fighters_text or ""):
        user_id = int(found)
        if user_id not in order:
            order.append(user_id)

    if len(order) > MAX_CARD_FIGHTERS:
        raise CardInputError(
            f"That's {len(order)} people; the limit is {MAX_CARD_FIGHTERS} per command."
        )

    fighters = []

    for user_id in order:

        member = await get_member(guild, user_id)

        if member and member.bot:
            continue

        name = member.display_name if member else "Unknown member"

        fighters.append((user_id, name, member))

    if not fighters:
        raise CardInputError(
            "I didn't find anyone. @mention everyone on the card in "
            "**fighters**, e.g. `@Fighter1 @Fighter2 @Fighter3 ...`"
        )

    return fighters


# ============================================================
# THE DM
# ============================================================

LIVE_CARD_DM_TEXT = (
    "You have been selected to fight on **{event}** on {date}. Please "
    "check the card and make contact with your opponent as soon as possible.\n\n"
    "One person per fight **MUST** stream, please sort this out before the day "
    "and ensure you are lobbied up a few fights before yours so that the event "
    "runs smoothly.\n\n"
    "Failure to show will result in a suspension from upcoming events or "
    "removal of your fighter. Please press the button below to check in so "
    "we know you are going to show.\n\n"
    "Expect a message from @Liuchack some time the day before for another reminder."
)


def event_author(
    event: str
):

    if event and event.upper().startswith(BRAND_TAG):
        return event.upper()

    if event:
        return f"{BRAND_TAG} • {event.upper()}"

    return f"{BRAND_TAG} • LIVE CARD"


def create_selection_dm_embed(
    event: str,
    event_date: str
):

    embed = discord.Embed(
        title="🥊 You've been selected!",
        description=LIVE_CARD_DM_TEXT.format(
            event=event,
            date=format_saved_time(event_date)
        ),
        color=BRAND_RED,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=SELECTED_LIVE_CARD_BANNER,
        author_text=event_author(event)
    )

    embed.set_footer(
        text=BRAND_NAME,
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


class CheckInView(
    discord.ui.View
):
    """The Check In button in Live Card DMs. Works after restarts."""

    def __init__(
        self,
        notice_id: int
    ):

        super().__init__(
            timeout=None
        )

        self.notice_id = notice_id

        button = discord.ui.Button(
            label="Check In",
            style=discord.ButtonStyle.green,
            emoji="✅",
            custom_id=f"ufcu_checkin_{notice_id}"
        )

        button.callback = self.check_in

        self.add_item(button)

    async def check_in(
        self,
        interaction: discord.Interaction
    ):

        notice = get_notice(self.notice_id)

        if not notice or not notice["active"]:

            await interaction.response.send_message(
                f"❌ Check-in for this event has closed. Please contact a member of staff in {BRAND_NAME}."
            )

            return

        row = get_card_fighter(
            self.notice_id,
            interaction.user.id
        )

        if not row:

            await interaction.response.send_message(
                "❌ You're not on this card."
            )

            return

        if not row["checked_in_at"]:
            update_card_fighter(
                self.notice_id,
                interaction.user.id,
                checked_in_at=now_utc()
            )

        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0].copy()
        else:
            embed = create_selection_dm_embed(notice["event"], notice["event_date"])

        banner = asset_url(CHECKED_IN_BANNER)

        if banner:
            embed.set_image(url=banner)

        if not any(field.name.startswith("✅") for field in embed.fields):
            embed.add_field(
                name="✅ Checked in",
                value="Thanks! Staff can see you're coming. See you on the night.",
                inline=False
            )

        done = discord.ui.View(timeout=None)
        done.add_item(discord.ui.Button(
            label="Checked In",
            style=discord.ButtonStyle.grey,
            emoji="✅",
            disabled=True
        ))

        await interaction.response.edit_message(
            embed=embed,
            view=done
        )

        await refresh_checkin_board(self.notice_id)


# ============================================================
# CHECK-IN BOARD
# ============================================================

def create_checkin_board_embed(
    notice
):

    guild = bot.get_guild(notice["guild_id"])

    fighters = get_card_fighters(notice["id"])
    checked = sum(1 for row in fighters if row["checked_in_at"])

    def icon(row):
        if row["checked_in_at"]:
            return "✅"
        if row["dm_status"] == "failed":
            return "⚠️"
        return "⏳"

    lines = [
        f"`{number:>2}` {icon(row)} {fighter_name(guild, row['user_id'])}"
        for number, row in enumerate(fighters, start=1)
    ]

    header = []

    if notice["event_date"]:
        header.append(f"📅 {format_saved_time(notice['event_date'])}")

    header.append(f"**{checked} / {len(fighters)} checked in**")

    if not notice["active"]:
        header.append("🔒 **Check-in closed**")

    embed = discord.Embed(
        title=notice["event"] or "Live Card",
        description=("\n".join(header) + "\n\n" + "\n".join(lines))[:4000],
        color=BRAND_RED,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=CHECKIN_BANNER,
        author_text=f"{BRAND_TAG} • LIVE CARD CHECK-IN"
    )

    embed.set_footer(
        text="✅ Checked in    ⏳ Waiting    ⚠️ Couldn't DM • Updates automatically",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


async def refresh_checkin_board(
    notice_id: int
):

    notice = get_notice(notice_id)

    if not notice or not notice["board_message_id"]:
        return

    try:
        channel = bot.get_channel(notice["board_channel_id"]) or await bot.fetch_channel(
            notice["board_channel_id"]
        )
        message = await channel.fetch_message(notice["board_message_id"])
        await message.edit(embed=create_checkin_board_embed(notice))
    except Exception as error:
        print(f"Could not refresh check-in board: {error}")


# ============================================================
# REMINDER DMS
#
# Everyone on the most recent /dmlivecard gets a DM REMINDER_HOURS
# before the card starts. Anyone not checked in gets the Check In
# button again. A note goes in the Diddy-Blud channel.
# ============================================================

def reminder_hours_text():

    if REMINDER_HOURS == 1:
        return "1 hour"

    return f"{REMINDER_HOURS:g} hours"


def reminder_time(
    notice
):
    """(when to remind, when the card starts) as unix, or (None, None)."""

    value = notice["event_date"]

    if not value or not str(value).isdigit():
        return None, None

    starts = int(value)

    return starts - int(REMINDER_HOURS * 3600), starts


def create_reminder_dm_embed(
    notice,
    fighter
):

    _, starts = reminder_time(notice)
    event = notice["event"] or "the Live Card"

    lines = [f"**{event}** starts <t:{starts}:F> (<t:{starts}:R>)."]

    if fighter["checked_in_at"]:
        lines += ["", "✅ **You're checked in.** See you there!"]
    else:
        lines += ["", "⚠️ **You haven't checked in yet.** Press **Check In** below so staff know you're coming."]

    lines += [
        "",
        "Make sure your stream is sorted and you're lobbied up a few fights before yours.",
    ]

    embed = discord.Embed(
        title=f"⏰ Starts in {reminder_hours_text()}!",
        description="\n".join(lines),
        color=BRAND_RED,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=REMINDER_BANNER,
        author_text=event_author(notice["event"])
    )

    embed.set_footer(
        text=BRAND_NAME,
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


async def send_reminder_dms(
    notice_id: int
):

    notice = get_notice(notice_id)
    guild = bot.get_guild(notice["guild_id"]) if notice else None

    if not guild:
        return

    fighters = get_card_fighters(notice_id)
    sent = 0
    failed = []

    for index, fighter in enumerate(fighters):

        if index:
            await asyncio.sleep(DM_DELAY_SECONDS)

        member = await get_member(guild, fighter["user_id"])

        if not member:
            failed.append((fighter["user_id"], "not in the server"))
            continue

        embed = create_reminder_dm_embed(notice, fighter)
        needs_button = notice["active"] and not fighter["checked_in_at"]

        try:
            if needs_button:
                await member.send(embed=embed, view=CheckInView(notice_id))
            else:
                await member.send(embed=embed)
            sent += 1
        except discord.Forbidden:
            failed.append((fighter["user_id"], "DMs closed"))
        except Exception as error:
            print(f"Reminder DM to {fighter['user_id']} failed: {error}")
            failed.append((fighter["user_id"], "Discord error"))

    event = notice["event"] or "Live Card"
    lines = [f"⏰ Reminder sent to **{sent} of {len(fighters)}** fighters for **{event}**."]

    waiting = [row for row in get_card_fighters(notice_id) if not row["checked_in_at"]]

    if waiting:
        lines.append("")
        lines.append("⚠️ **Not checked in yet:** " + ", ".join(fighter_name(guild, row["user_id"]) for row in waiting))
    else:
        lines.append("✅ Everyone has checked in.")

    if failed:
        lines.append("")
        lines.append("📭 **Couldn't DM:** " + ", ".join(f"{fighter_name(guild, user_id)} ({why})" for user_id, why in failed))

    embed = discord.Embed(
        title="⏰ Reminder DMs sent",
        description="\n".join(lines)[:4000],
        color=BRAND_RED,
        timestamp=discord.utils.utcnow()
    )

    embed.set_author(name=f"{BRAND_TAG} • REMINDERS", icon_url=asset_url(LOGO_FILE))

    channel = find_reminder_channel(guild)

    if not channel:
        print(f"Reminder log: couldn't find a channel matching '{REMINDER_CHANNEL}'. Set REMINDER_CHANNEL in Railway.")
        return

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as error:
        print(f"Could not post the reminder log: {error}")


async def check_card_reminders():

    if not CARD_REMINDERS:
        return

    now = int(time.time())

    # Only the most recent /dmlivecard per server.
    latest = query("""
        SELECT * FROM card_notices
        WHERE id IN (SELECT MAX(id) FROM card_notices GROUP BY guild_id)
          AND reminder_sent = 0
    """)

    for notice in latest:

        remind_at, starts = reminder_time(notice)

        if remind_at is None or now < remind_at:
            continue

        update_notice(notice["id"], reminder_sent=1)

        try:
            created = datetime.fromisoformat(notice["created_at"])
            created_unix = int(created.timestamp())
        except Exception:
            created_unix = 0

        # Too late (the card has started), or the DMs only just went out.
        if now >= starts or created_unix >= remind_at:
            continue

        asyncio.create_task(send_reminder_dms(notice["id"]))


# ============================================================
# SENDING
# ============================================================

class SendDMsView(
    discord.ui.View
):
    """Confirm step before DMing everyone."""

    def __init__(
        self,
        author_id: int,
        guild: discord.Guild,
        fighters: list,
        event: str,
        event_date: str
    ):

        super().__init__(
            timeout=600
        )

        self.author_id = author_id
        self.guild = guild
        self.fighters = fighters
        self.event = event
        self.event_date = event_date

        confirm = discord.ui.Button(
            label=f"Send {len(fighters)} DM{'s' if len(fighters) != 1 else ''}",
            style=discord.ButtonStyle.green,
            emoji="📨"
        )
        confirm.callback = self.confirm

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.grey
        )
        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.author_id:

            await interaction.response.send_message(
                "❌ Only the person who started this can confirm it.",
                ephemeral=True
            )

            return False

        return True

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content="Cancelled. No DMs were sent.",
            embed=None,
            view=None
        )

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content=f"📨 Sending DMs to {len(self.fighters)} fighters… this takes about "
                    f"{max(1, round(len(self.fighters) * DM_DELAY_SECONDS))} seconds.",
            embed=None,
            view=None
        )

        notice_id = create_notice(
            self.guild.id,
            self.author_id,
            [(user_id, name) for user_id, name, _ in self.fighters],
            self.event,
            self.event_date
        )

        sent = []
        failed = []

        for index, (user_id, _name, member) in enumerate(self.fighters):

            if index:
                await asyncio.sleep(DM_DELAY_SECONDS)

            if member is None:
                failed.append((user_id, "not in the server"))
                update_card_fighter(notice_id, user_id, dm_status="failed")
                continue

            try:
                await member.send(
                    embed=create_selection_dm_embed(self.event, self.event_date),
                    view=CheckInView(notice_id)
                )

                sent.append(user_id)
                update_card_fighter(notice_id, user_id, dm_status="sent")

            except discord.Forbidden:
                failed.append((user_id, "DMs closed"))
                update_card_fighter(notice_id, user_id, dm_status="failed")

            except Exception as error:
                print(f"DM to {user_id} failed: {error}")
                failed.append((user_id, "Discord error"))
                update_card_fighter(notice_id, user_id, dm_status="failed")

        lines = [
            f"✅ **DMed {len(sent)} of {len(self.fighters)} fighters.**"
        ]

        if failed:
            lines.append("")
            lines.append("⚠️ **Couldn't DM these, so let them know another way:**")
            for user_id, reason in failed[:25]:
                lines.append(f"• {fighter_name(self.guild, user_id)} ({reason})")
            if len(failed) > 25:
                lines.append(f"• …and {len(failed) - 25} more")

        lines.append("")
        lines.append("Use `/livecheckin` to post the check-in board.")

        if CARD_REMINDERS:
            lines.append(f"⏰ A reminder DM goes out {reminder_hours_text()} before the card starts.")

        await interaction.edit_original_response(
            content="\n".join(lines)[:2000]
        )


def create_dm_preview_embed(
    fighters: list,
    event: str,
    event_date: str
):

    lines = []

    for _user_id, name, member in fighters:
        line = f"• {clean_name(name)}"
        if member is None:
            line += " ⚠️ not in server"
        lines.append(line)

    details = (
        f"**Event:** {event}\n"
        f"**When:** {format_saved_time(event_date)}\n"
        "**Check-in button:** included"
    )

    if CARD_REMINDERS:
        details += f"\n**Reminder:** sent automatically {reminder_hours_text()} before"

    embed = discord.Embed(
        title=f"Send DMs to {len(fighters)} fighters?",
        description=(details + "\n\n" + "\n".join(lines))[:4000],
        color=BRAND_RED
    )

    apply_branding(
        embed,
        author_text=f"{BRAND_TAG} • DM LIVE CARD"
    )

    embed.set_footer(
        text="Times show in each person's own timezone • Nothing is sent until you press Send",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


# ============================================================
# COMMANDS: LIVE CARD DMS AND CHECK-IN
# ============================================================

@bot.tree.command(
    name="dmlivecard",
    description="DM everyone you @mention for a Live Card, with a button to check in."
)
@app_commands.describe(
    fighters="@mention everyone on the card, e.g. @Fighter1 @Fighter2 @Fighter3 ...",
    event="Event name, e.g. UFC Universe Live Card 3",
    date="Date and time in UK time, e.g. 24/10/2026 20:00 or 24/10 8pm"
)
async def dmlivecard(
    interaction: discord.Interaction,
    fighters: str,
    event: str,
    date: str
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can DM fight cards.",
            ephemeral=True
        )

        return

    moment = parse_event_datetime(date)

    if not moment:

        await interaction.response.send_message(
            "❌ Write the date and time like **24/10/2026 20:00** or **24/10 8pm** "
            "(UK time). It's turned into a timestamp everyone sees in their own timezone.",
            ephemeral=True
        )

        return

    if moment < datetime.now(timezone.utc):

        await interaction.response.send_message(
            "❌ That date and time has already passed.",
            ephemeral=True
        )

        return

    event_date = str(int(moment.timestamp()))

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        found = await collect_fighters(
            interaction.guild,
            fighters
        )
    except CardInputError as error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    await interaction.followup.send(
        embed=create_dm_preview_embed(found, event.strip(), event_date),
        view=SendDMsView(
            interaction.user.id,
            interaction.guild,
            found,
            event.strip(),
            event_date
        ),
        ephemeral=True
    )


@bot.tree.command(
    name="livecheckin",
    description="Show who has checked in for the latest Live Card."
)
@app_commands.describe(
    close="Close check-in (the button stops working)"
)
async def livecheckin(
    interaction: discord.Interaction,
    close: bool = False
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can view check-ins.",
            ephemeral=True
        )

        return

    notice = get_latest_notice(
        interaction.guild.id
    )

    if not notice:

        await interaction.response.send_message(
            "❌ No Live Card has been DMed yet. Use `/dmlivecard` first.",
            ephemeral=True
        )

        return

    if close:

        update_notice(notice["id"], active=0)

        await refresh_checkin_board(notice["id"])

        await interaction.response.send_message(
            f"🔒 Check-in for **{notice['event']}** is closed.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        embed=create_checkin_board_embed(notice)
    )

    message = await interaction.original_response()

    update_notice(
        notice["id"],
        board_channel_id=message.channel.id,
        board_message_id=message.id
    )


# ============================================================
# FIGHT RESULTS: /scanresults, /serverstats, /profile
#
# Reads result posts like:
#   @Mighty Mouse 5-2-1 beats @Amir Albazi 2-1 by R3 ARMBAR
#   @Carlos Prates 8-5 (step in) beats @Michael Morales 1-1 by R2 KO
#   @Kevin Holland 1-5 RETAINS the WMF after a R2 KO from @Michel Pereira 3-3
#   @DDP 4-5-0-1 Beats @Royce Gracie 0-1 by R2 Submission (Armbar)
# The first @ is the winner, the second the loser. Records, (step in),
# (C), emojis and any extra words are ignored. Forfeits, DQs, draws
# and no contests are skipped. One result per line.
# ============================================================

MENTION_RE = re.compile(r"<@!?(\d+)>")

WIN_WORDS_RE = re.compile(
    r"\b(beats?|beaten|defeats?|defeated|def|retains?|retained|wins?|won|"
    r"finish(es|ed)?|submits?|submitted|stops?|stopped|knocks?\s+out|"
    r"knocked\s+out|kos|ko'?d|tkos|tko'?d|over|by)\b",
    re.IGNORECASE
)

# "@A loses to @B" puts the winner second.
LOSS_WORDS_RE = re.compile(
    r"\b(loses|lost|falls|fell)\s+to\b",
    re.IGNORECASE
)

SKIP_RE = re.compile(
    r"\b(forfeit\w*|dq|disqualif\w*|no\s+contest|nc|draws?|drew|overturned)\b",
    re.IGNORECASE
)

SUB_RE = re.compile(
    r"\b(submission|subs?|submits?|submitted|tap(s|ped)?|arm\s?bar|triangle|rnc|"
    r"rear[\s-]?naked|choke\w*|guillotine|kimura|americana|heel\s?hook|"
    r"knee\s?bar|ankle\s?lock|toe\s?hold|d'?arce|anaconda|omoplata|ezekiel|"
    r"von\s?flue|neck\s?crank|twister|gogoplata|calf\s?slicer|arm\s?lock|"
    r"arm\s?triangle|bulldog)\b",
    re.IGNORECASE
)

KO_RE = re.compile(
    r"\b(t?ko|t?ko'?d|t?kos|knock\s?out|knocked\s+out|stoppage|"
    r"doctor|ground\s+and\s+pound|gnp)\b",
    re.IGNORECASE
)

DEC_RE = re.compile(
    r"\b(decision|dec|ud|sd|md|unanimous|split|majority|points|"
    r"score\s?cards?|judges)\b",
    re.IGNORECASE
)

ROUND_RE = re.compile(r"\b(?:r|round\s*)([1-5])\b", re.IGNORECASE)

# "RETAINS the WMF", "AND STILL WW CHAMP", "wins the belt" → a title fight.
TITLE_RE = re.compile(
    r"\b(retains?|retained|title|belt|champ\w*|crowned)\b",
    re.IGNORECASE
)

METHOD_NAMES = {
    "KO": "KO/TKO",
    "SUB": "Submission",
    "DEC": "Decision"
}


def parse_result_line(
    line: str
):
    """
    Returns (result, reason). result is a dict with winner_id, loser_id,
    method (KO / SUB / DEC) and round, or None with a reason it was skipped.
    reason is None for lines that just aren't results.
    """

    ids = []

    for found in MENTION_RE.findall(line):
        if int(found) not in ids:
            ids.append(int(found))

    if len(ids) < 2:
        return None, None

    # The words without the @s, so a fighter's name can't count as a method.
    text = MENTION_RE.sub(" ", line)

    if not WIN_WORDS_RE.search(text) and not LOSS_WORDS_RE.search(text):
        return None, None

    if SKIP_RE.search(text):
        return None, "forfeit / DQ / draw / NC"

    if SUB_RE.search(text):
        method = "SUB"
    elif KO_RE.search(text):
        method = "KO"
    elif DEC_RE.search(text):
        method = "DEC"
    else:
        return None, "no KO / SUB / DEC"

    winner_id, loser_id = ids[0], ids[1]

    # Only swap if "loses to" sits between the two fighters.
    first = MENTION_RE.search(line)
    second = list(MENTION_RE.finditer(line))[1]

    if LOSS_WORDS_RE.search(line[first.end():second.start()]):
        winner_id, loser_id = loser_id, winner_id

    round_match = ROUND_RE.search(text)

    return {
        "winner_id": winner_id,
        "loser_id": loser_id,
        "method": method,
        "round": int(round_match.group(1)) if round_match else None,
        "title": "Title fight" if TITLE_RE.search(text) else None
    }, None


def read_scan_date(
    text: str
):
    """'01/11/2026' or '1/11' (this year) → UTC datetime at midnight UK time."""

    text = (text or "").strip()

    if not text:
        return None

    match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", text)

    if not match:
        raise ValueError

    day, month, year = match.groups()
    year = int(year) if year else datetime.now(timezone.utc).year

    if year < 100:
        year += 2000

    local = datetime(year, int(month), int(day))

    return local.replace(tzinfo=timezone.utc) - timedelta(hours=uk_offset_hours(local))


def result_exists(
    guild_id: int,
    result_key: str
):

    return bool(query(
        "SELECT 1 FROM fight_results WHERE guild_id = ? AND result_key = ?",
        (guild_id, result_key)
    ))


def save_results(
    guild_id: int,
    results: list,
    imported_by: int
):

    db = get_db()
    cursor = db.cursor()
    saved = 0

    for result in results:
        cursor.execute(
            """
            INSERT OR IGNORE INTO fight_results
                (guild_id, result_key, channel_id, message_id, winner_id, loser_id,
                 method, round, fought_at, card_name, title, imported_by, imported_at,
                 winner_name, loser_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id, result["key"], result.get("channel_id"), result.get("message_id"),
                result["winner_id"], result["loser_id"], result["method"],
                result.get("round"), result.get("fought_at"), result.get("card"),
                result.get("title"), imported_by, now_utc(),
                result.get("winner_name"), result.get("loser_name")
            )
        )
        saved += cursor.rowcount

    db.commit()
    db.close()

    return saved


def method_counts(
    guild_id: int,
    winner_id: int = None
):

    sql = "SELECT method, COUNT(*) AS n FROM fight_results WHERE guild_id = ?"
    params = [guild_id]

    if winner_id:
        sql += " AND winner_id = ?"
        params.append(winner_id)

    counts = {"KO": 0, "SUB": 0, "DEC": 0}

    for row in query(sql + " GROUP BY method", tuple(params)):
        if row["method"] in counts:
            counts[row["method"]] = row["n"]

    return counts


def stat_bar(
    part: int,
    total: int
):

    filled = round(10 * part / total) if total else 0

    return "▰" * filled + "▱" * (10 - filled)


def percent(
    part: int,
    total: int
):

    return f"{round(100 * part / total)}%" if total else "0%"


def create_scan_embed(
    guild,
    channel,
    new_results: list,
    already: int,
    skipped: dict,
    card: str = None
):

    counts = {"KO": 0, "SUB": 0, "DEC": 0}

    for result in new_results:
        counts[result["method"]] += 1

    lines = [
        f"Scanned {channel.mention}" + (f" · card: **{card}**" if card else ""),
        "",
        f"**{len(new_results)}** new result(s) found",
        f"KO/TKO **{counts['KO']}** · SUB **{counts['SUB']}** · DEC **{counts['DEC']}**"
    ]

    if already:
        lines.append(f"Already imported: {already}")

    if skipped:
        lines.append("Skipped: " + ", ".join(f"{n} {why}" for why, n in skipped.items()))

    if new_results:
        lines.append("")
        lines.append("**Preview**")

        for result in new_results[:10]:
            round_text = f"R{result['round']} " if result["round"] else ""
            lines.append(
                f"{fighter_name(guild, result['winner_id'], result.get('winner_name'))} def. "
                f"{fighter_name(guild, result['loser_id'], result.get('loser_name'))} · "
                f"{round_text}{result['method']}" + (" · 🏆" if result["title"] else "")
            )

        if len(new_results) > 10:
            lines.append(f"…and {len(new_results) - 10} more")

    embed = discord.Embed(
        description="\n".join(lines),
        color=BRAND_RED
    )

    return apply_branding(embed, author_text=f"{BRAND_TAG} • SCAN RESULTS")


class ImportResultsView(
    discord.ui.View
):

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        results: list
    ):

        super().__init__(timeout=600)

        self.guild_id = guild_id
        self.user_id = user_id
        self.results = results

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This isn't your scan.", ephemeral=True)
            return False

        return True

    @discord.ui.button(
        label="Import",
        style=discord.ButtonStyle.success,
        emoji="✅"
    )
    async def import_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        saved = save_results(self.guild_id, self.results, interaction.user.id)
        settled = 0

        for result in self.results:
            rows = query(
                "SELECT id FROM fight_results WHERE guild_id = ? AND result_key = ?",
                (self.guild_id, result["key"])
            )
            if rows and after_fight_logged(interaction.guild, rows[0]["id"]):
                settled += 1

        self.stop()

        await interaction.response.edit_message(
            content=f"✅ Imported **{saved}** result(s). Check `/serverstats`."
            + (f"\n-# Fight pay and bets done for {settled} of them." if settled else ""),
            embed=None,
            view=None
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.stop()

        await interaction.response.edit_message(
            content="Cancelled. Nothing was imported.",
            embed=None,
            view=None
        )


class ResetResultsView(
    discord.ui.View
):

    def __init__(
        self,
        user_id: int
    ):

        super().__init__(timeout=120)

        self.user_id = user_id

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        return interaction.user.id == self.user_id

    @discord.ui.button(
        label="Yes, delete all results",
        style=discord.ButtonStyle.danger
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        for row in query("SELECT id FROM fight_results WHERE guild_id = ?", (interaction.guild.id,)):
            before_fight_changed(interaction.guild.id, row["id"])

        query("DELETE FROM fight_results WHERE guild_id = ?", (interaction.guild.id,), write=True)

        self.stop()

        await interaction.response.edit_message(
            content="🗑️ All results deleted. Run `/scanresults` to import them again.",
            view=None
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.stop()

        await interaction.response.edit_message(content="Cancelled.", view=None)


@bot.tree.command(
    name="scanresults",
    description="Read fight results from a channel and add them to the stats."
)
@app_commands.describe(
    channel="Results channel (default: this one)",
    since="Only results posted on or after this date, e.g. 01/11/2026",
    limit="How many messages to read, newest first (default 1000)",
    card="Card name to put on these results, e.g. UFC Universe 12"
)
async def scanresults(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
    since: str = None,
    limit: app_commands.Range[int, 1, 10000] = 1000,
    card: app_commands.Range[str, 1, 80] = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can scan results.", ephemeral=True)
        return

    channel = channel or interaction.channel

    try:
        after = read_scan_date(since)
    except ValueError:
        await interaction.response.send_message("❌ Use a date like `01/11/2026`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    new_results = []
    seen_keys = set()
    already = 0
    skipped = {}

    try:

        async for message in channel.history(limit=limit, after=after, oldest_first=False):

            if not message.content:
                continue

            for index, line in enumerate(message.content.splitlines()):

                result, reason = parse_result_line(line)

                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1

                if not result:
                    continue

                key = f"{message.id}:{index}"

                if key in seen_keys:
                    continue

                seen_keys.add(key)

                if result_exists(interaction.guild.id, key):
                    already += 1
                    continue

                names = {user.id: clean_name(user.display_name) for user in message.mentions}

                result.update(
                    winner_name=names.get(result["winner_id"]),
                    loser_name=names.get(result["loser_id"]),
                    key=key,
                    channel_id=channel.id,
                    message_id=message.id,
                    fought_at=message.created_at.isoformat(),
                    card=card.strip() if card else None
                )

                new_results.append(result)

    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ I can't read the history in {channel.mention}. "
            "Give me View Channel and Read Message History there.",
            ephemeral=True
        )
        return

    # Oldest first, so the preview reads like the channel.
    new_results.reverse()

    embed = create_scan_embed(interaction.guild, channel, new_results, already, skipped, card)

    if not new_results:
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    await interaction.followup.send(
        embed=embed,
        view=ImportResultsView(interaction.guild.id, interaction.user.id, new_results),
        ephemeral=True
    )


@bot.tree.command(
    name="serverstats",
    description="Server-wide finishes: KOs, submissions and decisions."
)
async def serverstats(
    interaction: discord.Interaction
):

    if not interaction.guild:
        await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
        return

    counts = method_counts(interaction.guild.id)
    total = sum(counts.values())
    finishes = counts["KO"] + counts["SUB"]

    if total:
        lines = [
            f"**{total}** fights logged · **{percent(finishes, total)}** finished",
            ""
        ]

        for method in ("KO", "SUB", "DEC"):
            lines.append(
                f"{METHOD_NAMES[method]}\n"
                f"`{stat_bar(counts[method], total)}` **{counts[method]}** · {percent(counts[method], total)}"
            )
    else:
        lines = ["No results logged yet. Staff can add them with `/scanresults`."]

    embed = discord.Embed(
        title="Universe finishes",
        description="\n".join(lines),
        color=BRAND_RED,
        timestamp=datetime.now(timezone.utc)
    )

    apply_branding(embed, banner_file=STATS_BANNER, author_text=f"{BRAND_TAG} • SERVER STATS")

    embed.set_footer(text="From results scanned with /scanresults", icon_url=asset_url(LOGO_FILE))

    await interaction.response.send_message(embed=embed)


def fighter_record(
    guild_id: int,
    user_id: int
):
    """
    Everything a profile (and the odds) need from someone's logged fights:
    wins, losses, how they won and lost, streak like "W3", finish rate
    and their fights oldest first.
    """

    fights = query(
        """
        SELECT * FROM fight_results
        WHERE guild_id = ? AND (winner_id = ? OR loser_id = ?)
        ORDER BY fought_at, id
        """,
        (guild_id, user_id, user_id)
    )

    wins_by = {"KO": 0, "SUB": 0, "DEC": 0}
    losses_by = {"KO": 0, "SUB": 0, "DEC": 0}
    results = []

    for fight in fights:
        won = fight["winner_id"] == user_id
        (wins_by if won else losses_by)[fight["method"]] += 1
        results.append("W" if won else "L")

    streak = ""

    if results:
        last = results[-1]
        count = 0

        for result in reversed(results):
            if result != last:
                break
            count += 1

        streak = f"{last}{count}"

    wins = sum(wins_by.values())

    return {
        "wins": wins,
        "losses": sum(losses_by.values()),
        "wins_by": wins_by,
        "losses_by": losses_by,
        "streak": streak,
        "finish_rate": round(100 * (wins_by["KO"] + wins_by["SUB"]) / wins) if wins else None,
        "fights": fights,
        "title_wins": sum(1 for f in fights if f["winner_id"] == user_id and f["title"])
    }


def method_short_text(
    counts: dict
):
    """{'KO': 3, 'SUB': 1, 'DEC': 0} → '3 KO · 1 SUB', or '—'."""

    parts = [f"{counts[m]} {m}" for m in ("KO", "SUB", "DEC") if counts.get(m)]

    return " · ".join(parts) if parts else "—"


def create_profile_embed(
    guild,
    member
):
    """The fighter profile, laid out like PFO's first page."""

    record = fighter_record(guild.id, member.id)
    total = record["wins"] + record["losses"]

    lines = []

    if record["title_wins"]:
        n = record["title_wins"]
        lines.append(f"🏆 **{n} title fight win{'s' if n != 1 else ''}**")

    # Same rule as PFO: unbeaten in their first 1-3 fights.
    if 1 <= total <= 3 and not record["losses"]:
        lines.append(f"🌱 **Prospect** · unbeaten in their first {total} fight{'s' if total != 1 else ''}")

    if lines:
        lines.append("")

    streak = record["streak"]

    if streak.startswith("W") and int(streak[1:]) >= 3:
        streak_text = f"🔥 {streak}"
    else:
        streak_text = streak or "no streak"

    finish = (
        f"{record['finish_rate']}% finishes"
        if record["finish_rate"] is not None else "no finishes yet"
    )

    lines.append(f"**{record['wins']}-{record['losses']}** · {streak_text} · {finish}")

    contract = active_contract(guild.id, member.id)

    if contract:
        lines.append(
            f"📝 **Contract:** {coins_text(contract['show_pay'])} show / {coins_text(contract['win_pay'])} win · "
            f"{contract_used(contract['id'])} of {contract['fights']} fights"
        )

    lines.append("")
    lines.append(f"**Wins** {method_short_text(record['wins_by'])}")
    lines.append(f"**Losses** {method_short_text(record['losses_by'])}")

    last = list(reversed(record["fights"]))[:3]

    if last:
        bits = []

        for fight in last:
            won = fight["winner_id"] == member.id
            rnd = f" R{fight['round']}" if fight["round"] and fight["method"] != "DEC" else ""
            bits.append(f"{'✅ W' if won else '❌ L'} {fight['method']}{rnd}")

        lines.append("")
        lines.append(f"-# Last {len(last)}: " + " · ".join(bits))

    embed = discord.Embed(
        title=clean_name(member.display_name),
        url=f"https://discord.com/users/{member.id}",
        description="\n".join(lines),
        color=BRAND_RED,
        timestamp=datetime.now(timezone.utc)
    )

    apply_branding(embed, banner_file=PROFILE_BANNER, author_text=f"{BRAND_TAG} • FIGHTER PROFILE")

    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(
        text=f"{total} fight{'s' if total != 1 else ''} logged",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


PROFILE_FIGHTS_PER_PAGE = 15


def create_profile_fights_embed(
    guild,
    member,
    page: int = 0
):
    """
    Page 2 of the profile: every fight, newest first, 15 a page.
    Returns (embed, page, pages).
    """

    record = fighter_record(guild.id, member.id)
    fights = list(reversed(record["fights"]))
    total = len(fights)
    pages = max(1, -(-total // PROFILE_FIGHTS_PER_PAGE))
    page = min(max(page, 0), pages - 1)

    shown = fights[page * PROFILE_FIGHTS_PER_PAGE:(page + 1) * PROFILE_FIGHTS_PER_PAGE]

    lines = [f"**{record['wins']}-{record['losses']}** · {total} fight{'s' if total != 1 else ''}", ""]

    if shown:
        lines += [fight_line(fight, member.id, guild) for fight in shown]
    else:
        lines.append("No fights logged yet.")

    embed = discord.Embed(
        title=clean_name(member.display_name),
        url=f"https://discord.com/users/{member.id}",
        description="\n".join(lines)[:4096],
        color=BRAND_RED,
        timestamp=datetime.now(timezone.utc)
    )

    apply_branding(embed, banner_file=PROFILE_BANNER, author_text=f"{BRAND_TAG} • FIGHTER PROFILE • FIGHTS")

    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(
        text=f"Page {page + 1}/{pages} • newest first" if pages > 1 else "Newest first",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed, page, pages


class ProfileView(
    discord.ui.View
):
    """
    Overview / Fights tabs under /profile (like PFO). The person who ran it
    flips the tabs; other staff and fighters get their own private copy.
    """

    def __init__(
        self,
        owner_id: int,
        member,
        tab: str = "overview",
        page: int = 0
    ):

        super().__init__(timeout=3600)

        self.owner_id = owner_id
        self.member = member
        self.tab = tab
        self.page = page
        self.pages = 1
        self.message = None

    def render(
        self,
        guild
    ):

        if self.tab == "fights":
            embed, self.page, self.pages = create_profile_fights_embed(guild, self.member, self.page)
        else:
            embed = create_profile_embed(guild, self.member)

        self.build()

        return embed

    def build(self):

        self.clear_items()

        for key, label in (("overview", "Overview"), ("fights", "Fights")):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if key == self.tab else discord.ButtonStyle.secondary,
                row=0
            )
            button.callback = self.tab_callback(key)
            self.add_item(button)

        if self.tab == "fights" and self.pages > 1:
            newer = discord.ui.Button(label="◀ Newer", style=discord.ButtonStyle.secondary, row=1,
                                      disabled=self.page <= 0)
            newer.callback = self.page_callback(-1)
            self.add_item(newer)

            older = discord.ui.Button(label="Older ▶", style=discord.ButtonStyle.secondary, row=1,
                                      disabled=self.page >= self.pages - 1)
            older.callback = self.page_callback(1)
            self.add_item(older)

    def tab_callback(
        self,
        key: str
    ):

        async def callback(interaction: discord.Interaction):
            await self.show(interaction, key, 0)

        return callback

    def page_callback(
        self,
        step: int
    ):

        async def callback(interaction: discord.Interaction):
            await self.show(interaction, self.tab, self.page + step)

        return callback

    async def show(
        self,
        interaction: discord.Interaction,
        tab: str,
        page: int
    ):

        if interaction.user.id != self.owner_id:

            if not can_view_profiles(interaction):
                await interaction.response.send_message(f"❌ {PROFILE_ONLY_TEXT} can view profiles.", ephemeral=True)
                return

            copy = ProfileView(interaction.user.id, self.member, tab, page)
            await interaction.response.send_message(embed=copy.render(interaction.guild), view=copy, ephemeral=True)
            return

        self.tab = tab
        self.page = page

        await interaction.response.edit_message(embed=self.render(interaction.guild), view=self)

    async def on_timeout(self):

        try:
            if self.message:
                await self.message.edit(view=None)
        except Exception:
            pass


@bot.tree.command(
    name="profile",
    description="A fighter's profile: record, streak, how they win and lose, and all their fights."
)
@app_commands.describe(member="Who to show (default: you)")
async def profile(
    interaction: discord.Interaction,
    member: discord.Member = None
):

    if not can_view_profiles(interaction):
        await interaction.response.send_message(f"❌ {PROFILE_ONLY_TEXT} can view profiles.", ephemeral=True)
        return

    member = member or interaction.user
    view = ProfileView(interaction.user.id, member)

    await interaction.response.send_message(embed=view.render(interaction.guild), view=view)

    try:
        view.message = await interaction.original_response()
    except Exception:
        pass


def fight_date_text(
    fought_at: str
):
    """Stored UTC time → UK date, e.g. 04/11/2026."""

    if not fought_at:
        return None

    try:
        when = datetime.fromisoformat(fought_at)
    except ValueError:
        return None

    if when.tzinfo:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)

    when += timedelta(hours=uk_offset_hours(when))

    return when.strftime("%d/%m/%Y")


def fight_line(
    row,
    member_id: int = None,
    guild=None
):
    """
    '@A def. @B · R3 SUB · 🏆 Title fight · UFC Universe 12 · 04/11/2026', or on
    a profile '✅ W vs @B · R3 SUB · …' from that fighter's side.
    """

    winner = fighter_name(guild, row["winner_id"], row["winner_name"])
    loser = fighter_name(guild, row["loser_id"], row["loser_name"])

    if member_id is None:
        start = f"**{winner}** def. {loser}"
    elif row["winner_id"] == member_id:
        start = f"✅ **W** vs {loser}"
    else:
        start = f"❌ **L** vs {winner}"

    parts = [start, (f"R{row['round']} " if row["round"] else "") + row["method"]]

    if row["title"]:
        parts.append(f"🏆 {row['title']}")

    if row["card_name"]:
        parts.append(row["card_name"])

    date = fight_date_text(row["fought_at"])

    if date:
        parts.append(date)

    return " · ".join(parts)


# ------------------------------------------------------------
# Fighters who have left the server (like PFO's winner_left / loser_left)
#
# Type their user ID (best: they keep their record if they come back),
# an @mention, or their name. A name that isn't anyone in the server
# gets a fixed stand-in ID made from the name, so logging the same
# name again adds to the same record. Stand-ins (negative IDs) aren't
# paid, since there's no Discord account to pay.
# ------------------------------------------------------------

class FighterInputError(Exception):
    pass


def left_fighter_id(
    name: str
):
    """A fixed (negative) stand-in ID for a fighter known only by name."""

    digest = hashlib.sha1(role_key(clean_name(name)).encode()).hexdigest()

    return -int(digest[:12], 16)


def logged_name(
    guild_id: int,
    user_id: int
):
    """The name saved the last time this fighter was logged, or None."""

    rows = query(
        """
        SELECT name FROM (
            SELECT winner_name AS name, id FROM fight_results WHERE guild_id = ? AND winner_id = ? AND winner_name IS NOT NULL
            UNION ALL
            SELECT loser_name AS name, id FROM fight_results WHERE guild_id = ? AND loser_id = ? AND loser_name IS NOT NULL
        ) ORDER BY id DESC LIMIT 1
        """,
        (guild_id, user_id, guild_id, user_id)
    )

    return rows[0]["name"] if rows else None


async def departed_name(
    guild,
    user_id: int
):

    saved = logged_name(guild.id, user_id)

    if saved:
        return saved

    user = bot.get_user(user_id)

    if not user:
        try:
            user = await bot.fetch_user(user_id)
        except Exception:
            user = None

    if user:
        return clean_name(getattr(user, "global_name", None) or user.name)

    return "Unknown fighter"


async def resolve_left_fighter(
    guild,
    text: str
):
    """(user_id, name) from a typed user ID, mention or name."""

    text = (text or "").strip()

    found = re.fullmatch(r"<@!?(\d+)>|(\d{15,21})", text)

    if found:
        user_id = int(found.group(1) or found.group(2))
        member = guild.get_member(user_id)

        if member:
            return member.id, clean_name(member.display_name)

        return user_id, await departed_name(guild, user_id)

    name = clean_name(" ".join(text.lstrip("@").split()))

    if not name:
        raise FighterInputError("Type a user ID or a name.")

    member, problem = match_member(guild, member_lookup(guild), name)

    if member:
        return member.id, clean_name(member.display_name)

    if problem and problem.startswith("more than one"):
        raise FighterInputError(f"`{name}` matches more than one member. Use their user ID instead.")

    # Someone already in the fight log under this name (left, or typed before).
    key = role_key(name)

    for row in query(
        """
        SELECT winner_id AS id, winner_name AS name FROM fight_results WHERE guild_id = ?
        UNION ALL
        SELECT loser_id, loser_name FROM fight_results WHERE guild_id = ?
        """,
        (guild.id, guild.id)
    ):
        if row["name"] and role_key(clean_name(row["name"])) == key:
            return row["id"], row["name"]

    return left_fighter_id(name), name


async def pick_fighter(
    guild,
    member,
    left_text: str,
    role: str,
    required: bool = True
):
    """(user_id, name) from whichever of the two options was filled in."""

    if member and left_text:
        raise FighterInputError(f"Fill in only one of **{role}** or **{role}_left**.")

    if member:
        if member.bot:
            raise FighterInputError("Bots can't have fight records.")
        return member.id, clean_name(member.display_name)

    if left_text:
        return await resolve_left_fighter(guild, left_text)

    if required:
        raise FighterInputError(
            f"Pick the **{role}**, or for someone who has left use **{role}_left** (their user ID or name)."
        )

    return None, None


@bot.tree.command(
    name="logfight",
    description="Log a fight by hand: winner, loser, how it ended, card and date."
)
@app_commands.describe(
    method="How it ended",
    winner="Who won",
    loser="Who lost",
    winner_left="Winner who has LEFT the server: their user ID (best) or name",
    loser_left="Loser who has LEFT the server: their user ID (best) or name",
    round="Round it ended in (leave empty for a decision)",
    card="Card name, e.g. UFC Universe 12",
    date="Fight date, e.g. 04/11/2026 (default: today)",
    title_fight="Was it a title fight? (default: False)"
)
@app_commands.choices(method=[
    app_commands.Choice(name="KO/TKO", value="KO"),
    app_commands.Choice(name="Submission", value="SUB"),
    app_commands.Choice(name="Decision", value="DEC")
])
async def logfight(
    interaction: discord.Interaction,
    method: app_commands.Choice[str],
    winner: discord.Member = None,
    loser: discord.Member = None,
    winner_left: str = None,
    loser_left: str = None,
    round: app_commands.Range[int, 1, 5] = None,
    card: app_commands.Range[str, 1, 80] = None,
    date: str = None,
    title_fight: bool = False
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can log fights.", ephemeral=True)
        return

    try:
        fought_at = read_scan_date(date) if date else datetime.now(timezone.utc)
    except ValueError:
        await interaction.response.send_message("❌ Use a date like `04/11/2026`.", ephemeral=True)
        return

    try:
        winner_id, winner_name = await pick_fighter(interaction.guild, winner, winner_left, "winner")
        loser_id, loser_name = await pick_fighter(interaction.guild, loser, loser_left, "loser")
    except FighterInputError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return

    if winner_id == loser_id:
        await interaction.response.send_message("❌ The winner and loser can't be the same person.", ephemeral=True)
        return

    key = f"manual:{interaction.id}"

    save_results(
        interaction.guild.id,
        [{
            "key": key,
            "winner_id": winner_id,
            "loser_id": loser_id,
            "method": method.value,
            "round": round,
            "fought_at": fought_at.isoformat(),
            "card": card.strip() if card else None,
            "title": "Title fight" if title_fight else None,
            "winner_name": winner_name,
            "loser_name": loser_name
        }],
        interaction.user.id
    )

    row = query(
        "SELECT * FROM fight_results WHERE guild_id = ? AND result_key = ?",
        (interaction.guild.id, key)
    )[0]

    extra = after_fight_logged(interaction.guild, row["id"])

    embed = discord.Embed(
        description=f"{fight_line(row, guild=interaction.guild)}\n\nLogged by {clean_name(interaction.user.display_name)}"
        + (f"\n\n{extra}" if extra else ""),
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • FIGHT LOGGED")

    embed.set_footer(
        text=f"Fight #{row['id']} • /removefight {row['id']} to undo",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


FIGHTS_PER_PAGE = 10


def find_fights(
    guild_id: int,
    member_id: int = None,
    card: str = None,
    titles_only: bool = False
):
    """Where-clause and params for /fights, newest first."""

    where = ["guild_id = ?"]
    params = [guild_id]

    if member_id:
        where.append("(winner_id = ? OR loser_id = ?)")
        params += [member_id, member_id]

    if card:
        where.append("card_name LIKE ?")
        params.append(f"%{card.strip()}%")

    if titles_only:
        where.append("title IS NOT NULL")

    return " AND ".join(where), params


def create_fights_embed(
    guild,
    filters: dict,
    page: int
):

    where, params = find_fights(guild.id, **filters)

    total = query(f"SELECT COUNT(*) AS n FROM fight_results WHERE {where}", tuple(params))[0]["n"]
    pages = max(1, -(-total // FIGHTS_PER_PAGE))
    page = min(max(page, 0), pages - 1)

    rows = query(
        f"SELECT * FROM fight_results WHERE {where} "
        "ORDER BY fought_at DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params) + (FIGHTS_PER_PAGE, page * FIGHTS_PER_PAGE)
    )

    shown = []

    if filters.get("member_id"):
        shown.append(f"**{fighter_name(guild, filters['member_id'])}**")
    if filters.get("card"):
        shown.append(f"card: **{filters['card']}**")
    if filters.get("titles_only"):
        shown.append("🏆 title fights")

    lines = []

    if shown:
        lines += ["Showing " + " · ".join(shown), ""]

    if rows:
        lines += [f"`#{row['id']}` {fight_line(row, guild=guild)}" for row in rows]
    else:
        lines.append("No fights logged yet." if not shown else "No fights match that.")

    embed = discord.Embed(
        description="\n".join(lines),
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • FIGHT LOG")

    embed.set_footer(
        text=f"{total} fight{'s' if total != 1 else ''} • Page {page + 1}/{pages} • "
        "/editfight or /removefight with the #",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed, page, pages


class FightsView(
    discord.ui.View
):

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        filters: dict,
        page: int,
        pages: int
    ):

        super().__init__(timeout=600)

        self.guild_id = guild_id
        self.user_id = user_id
        self.filters = filters
        self.page = page
        self.update_buttons(pages)

    def update_buttons(
        self,
        pages: int
    ):

        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= pages - 1

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        return interaction.user.id == self.user_id

    async def turn_page(
        self,
        interaction: discord.Interaction,
        step: int
    ):

        embed, self.page, pages = create_fights_embed(interaction.guild, self.filters, self.page + step)
        self.update_buttons(pages)

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="◀ Newer",
        style=discord.ButtonStyle.secondary
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await self.turn_page(interaction, -1)

    @discord.ui.button(
        label="Older ▶",
        style=discord.ButtonStyle.secondary
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await self.turn_page(interaction, 1)


@bot.tree.command(
    name="fights",
    description="The fight log, newest first. Filter by fighter, card or title fights."
)
@app_commands.describe(
    member="Only this fighter's fights",
    card="Only fights on this card (part of the name is fine)",
    titles_only="Only title fights"
)
async def fights(
    interaction: discord.Interaction,
    member: discord.Member = None,
    card: app_commands.Range[str, 1, 80] = None,
    titles_only: bool = False
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can view the fight log for now.", ephemeral=True)
        return

    filters = {
        "member_id": member.id if member else None,
        "card": card,
        "titles_only": titles_only
    }

    embed, page, pages = create_fights_embed(interaction.guild, filters, 0)

    if pages > 1:
        view = FightsView(interaction.guild.id, interaction.user.id, filters, page, pages)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="editfight",
    description="Change a logged fight. Only fill in what's changing."
)
@app_commands.describe(
    fight="The fight number (see /fights)",
    winner="New winner",
    loser="New loser",
    winner_left="New winner who has LEFT the server: their user ID (best) or name",
    loser_left="New loser who has LEFT the server: their user ID (best) or name",
    method="New way it ended",
    round="New round (picking Decision clears the round)",
    card="New card name (type none to clear it)",
    date="New date, e.g. 04/11/2026",
    title_fight="Was it a title fight?"
)
@app_commands.choices(method=[
    app_commands.Choice(name="KO/TKO", value="KO"),
    app_commands.Choice(name="Submission", value="SUB"),
    app_commands.Choice(name="Decision", value="DEC")
])
async def editfight(
    interaction: discord.Interaction,
    fight: int,
    winner: discord.Member = None,
    loser: discord.Member = None,
    winner_left: str = None,
    loser_left: str = None,
    method: app_commands.Choice[str] = None,
    round: app_commands.Range[int, 1, 5] = None,
    card: app_commands.Range[str, 1, 80] = None,
    date: str = None,
    title_fight: bool = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can edit fights.", ephemeral=True)
        return

    rows = query(
        "SELECT * FROM fight_results WHERE guild_id = ? AND id = ?",
        (interaction.guild.id, fight)
    )

    if not rows:
        await interaction.response.send_message(f"❌ There's no fight #{fight}. Check `/fights`.", ephemeral=True)
        return

    before = rows[0]

    changes = {}

    try:
        new_winner, new_winner_name = await pick_fighter(interaction.guild, winner, winner_left, "winner", required=False)
        new_loser, new_loser_name = await pick_fighter(interaction.guild, loser, loser_left, "loser", required=False)
    except FighterInputError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return

    names = {before["winner_id"]: before["winner_name"], before["loser_id"]: before["loser_name"]}

    if new_winner:
        names[new_winner] = new_winner_name
    if new_loser:
        names[new_loser] = new_loser_name

    winner_id = new_winner or before["winner_id"]
    loser_id = new_loser or before["loser_id"]

    # Picking only the old loser as the winner (or the other way) swaps them.
    if new_winner and not new_loser and new_winner == before["loser_id"]:
        loser_id = before["winner_id"]
    if new_loser and not new_winner and new_loser == before["winner_id"]:
        winner_id = before["loser_id"]

    if winner_id == loser_id:
        await interaction.response.send_message("❌ The winner and loser can't be the same person.", ephemeral=True)
        return

    if winner_id != before["winner_id"]:
        changes["winner_id"] = winner_id
    if loser_id != before["loser_id"]:
        changes["loser_id"] = loser_id

    # Keep the saved names with the right fighters (matters for anyone who left).
    if names.get(winner_id) != before["winner_name"] or winner_id != before["winner_id"]:
        changes["winner_name"] = names.get(winner_id)
    if names.get(loser_id) != before["loser_name"] or loser_id != before["loser_id"]:
        changes["loser_name"] = names.get(loser_id)

    if method:
        changes["method"] = method.value
        if method.value == "DEC" and round is None:
            changes["round"] = None

    if round is not None:
        changes["round"] = round

    if card:
        changes["card_name"] = None if card.strip().lower() == "none" else card.strip()

    if date:
        try:
            changes["fought_at"] = read_scan_date(date).isoformat()
        except ValueError:
            await interaction.response.send_message("❌ Use a date like `04/11/2026`.", ephemeral=True)
            return

    if title_fight is not None:
        changes["title"] = "Title fight" if title_fight else None

    if not changes:
        await interaction.response.send_message(
            "❌ Nothing to change. Fill in at least one option, e.g. `method` or `card`.",
            ephemeral=True
        )
        return

    before_fight_changed(interaction.guild.id, fight)

    query(
        "UPDATE fight_results SET "
        + ", ".join(f"{column} = ?" for column in changes)
        + " WHERE id = ?",
        tuple(changes.values()) + (fight,),
        write=True
    )

    after = query("SELECT * FROM fight_results WHERE id = ?", (fight,))[0]
    extra = after_fight_logged(interaction.guild, fight)

    embed = discord.Embed(
        description=(
            f"**Before**\n{fight_line(before, guild=interaction.guild)}\n\n"
            f"**After**\n{fight_line(after, guild=interaction.guild)}\n\n"
            f"Edited by {clean_name(interaction.user.display_name)}"
            + (f"\n\n{extra}" if extra else "")
        ),
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • FIGHT EDITED")

    embed.set_footer(text=f"Fight #{fight}", icon_url=asset_url(LOGO_FILE))

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="removefight",
    description="Delete one logged fight by its number."
)
@app_commands.describe(fight="The fight number (shown when it was logged)")
async def removefight(
    interaction: discord.Interaction,
    fight: int
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can remove fights.", ephemeral=True)
        return

    rows = query(
        "SELECT * FROM fight_results WHERE guild_id = ? AND id = ?",
        (interaction.guild.id, fight)
    )

    if not rows:
        await interaction.response.send_message(f"❌ There's no fight #{fight}.", ephemeral=True)
        return

    before_fight_changed(interaction.guild.id, fight)
    query("DELETE FROM fight_results WHERE id = ?", (fight,), write=True)

    await interaction.response.send_message(
        f"🗑️ Removed fight #{fight}: {fight_line(rows[0], guild=interaction.guild)}",
        ephemeral=True
    )


@bot.tree.command(
    name="resetresults",
    description="Delete every scanned result (to start the stats again)."
)
async def resetresults(
    interaction: discord.Interaction
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can reset results.", ephemeral=True)
        return

    total = sum(method_counts(interaction.guild.id).values())

    await interaction.response.send_message(
        f"⚠️ Delete all **{total}** logged result(s)? This can't be undone.",
        view=ResetResultsView(interaction.user.id),
        ephemeral=True
    )


# ============================================================
# RANKINGS (from a spreadsheet)
#
# The owner keeps the Universe rankings in their own spreadsheet. The
# bot reads it as it is:
#   - /rankingslink <Google Sheets or OneDrive link>: re-read every
#     RANKINGS_SYNC_MINUTES, so edits show up by themselves
#   - /uploadrankings <.xlsx/.csv>: a one-off upload
# The owner's sheet has a block per weight: "Flyweight | User | Rankings",
# fighters and their Discord names on the left, C / 1-15 / NR by fighter
# name on the right (see "The owner's layout" below). Only the weight,
# fighter, Discord member and rank are kept.
# Other layouts work too: it finds the Discord name column (falling back to the fighter name),
# the division (a column, heading rows like "WELTERWEIGHT", or one tab
# per division) and the rank (a column, or the order down the sheet,
# with C / Champion / 👑 for the champion). Every other column (wins,
# losses, notes) is ignored: records come from the fights logged here.
# The rankings are only used behind the scenes for the betting odds:
# they aren't shown anywhere, the owner's sheet stays the place to see them.
# ============================================================

DIVISION_SHORT = {
    "heavyweight": "HW",
    "lightheavyweight": "LHW",
    "middleweight": "MW",
    "welterweight": "WW",
    "lightweight": "LW",
    "featherweight": "FW",
    "bantamweight": "BW",
    "flyweight": "FLW",
    "strawweight": "SW",
    "poundforpound": "P4P",
    "p4p": "P4P",
    "womensbantamweight": "W-BW",
    "womensflyweight": "W-FLW",
    "womensstrawweight": "W-SW",
    "womensfeatherweight": "W-FW"
}

CHAMPION_WORDS = {"c", "champ", "champion", "0", "👑"}

MAX_RANKINGS_FILE = 2_000_000   # bytes


def setup_rankings_tables():

    db = get_db()

    # The first version had no fighter column and no "not ranked" (NR). The
    # rankings are rebuilt from the sheet on the next read, so it's safe to
    # start this table again.
    columns = [row[1] for row in db.execute("PRAGMA table_info(rankings)").fetchall()]

    if columns and "fighter" not in columns:
        db.execute("DROP TABLE rankings")
        db.execute("DELETE FROM bot_meta WHERE key LIKE 'rankings_hash:%'")

    db.execute("""
        CREATE TABLE IF NOT EXISTS rankings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            division TEXT NOT NULL,
            division_order INTEGER NOT NULL,
            rank INTEGER,
            user_id INTEGER NOT NULL,
            name TEXT,
            fighter TEXT,
            fixed INTEGER NOT NULL DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS rankings_fixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            division TEXT,
            sheet_name TEXT,
            user_id INTEGER NOT NULL,
            rank INTEGER,
            created_by INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS rankings_uploads (
            guild_id INTEGER PRIMARY KEY,
            file_name TEXT,
            uploaded_by INTEGER,
            uploaded_at TEXT NOT NULL
        )
    """)

    db.commit()
    db.close()


def division_short(
    division: str
):

    key = role_key(division.replace("'", ""))

    return DIVISION_SHORT.get(key, division[:12])


def is_p4p(
    division: str
):

    return division_short(division) == "P4P"


def read_rank(
    value
):
    """'C' / 'Champion' / 0 → 0, '#3' / 3 / '3.0' → 3, anything else → None."""

    text = str(value if value is not None else "").strip().lower().lstrip("#").strip()
    text = re.sub(r"(?<=\d)(st|nd|rd|th)$", "", text)

    if text in CHAMPION_WORDS:
        return 0

    try:
        number = float(text)
    except ValueError:
        return None

    return int(number) if number >= 0 and number == int(number) else None


CHAMP_MARKERS = re.compile(r"\(\s*(?:c|ic|interim|champ(?:ion)?)\s*\)|👑|🏆|🥇", re.IGNORECASE)
NAME_SPLIT = re.compile(r"\s*(?:/|\||&|\\|\+)\s*")


def name_key(
    text: str
):
    """'Sean O'Malley (C) 5-1' → 'seanomalley'. Ignores records and (C) / 👑 markers."""

    text = CHAMP_MARKERS.sub(" ", str(text or ""))

    return role_key(clean_name(" ".join(text.split())))


class MemberNames(dict):
    """name key → members, plus .parts for two-fighter nicknames like 'Jam / Garbrandt'."""

    def __init__(self):
        super().__init__()
        self.parts = {}


def member_lookup(
    guild
):
    """Every way a member can be written in the sheet → their member object(s)."""

    names = MemberNames()

    for member in guild.members:

        if member.bot:
            continue

        written = {member.name, member.display_name, getattr(member, "global_name", None)}

        for name in written:
            for key in {role_key(name or ""), name_key(name)}:
                if key:
                    names.setdefault(key, set()).add(member)

        # "Khabib / CM Punk", "Jam / Garbrandt": each side counts on its own.
        for name in written:
            parts = NAME_SPLIT.split(CHAMP_MARKERS.sub(" ", clean_name(name or "")))
            if len(parts) > 1:
                for part in parts:
                    key = name_key(part)
                    if len(key) >= 3:
                        names.parts.setdefault(key, set()).add(member)

    return names


def match_member(
    guild,
    names: dict,
    text: str,
    fighter_name: bool = False
):
    """
    (member, problem). Matches an ID, a mention, or a name: exact, unique
    start, one side of a two-fighter nickname, a near-identical spelling,
    and (for fighter names) a surname that's one side of a nickname.
    """

    text = str(text or "").strip()

    if not text:
        return None, "empty"

    found = re.fullmatch(r"<@!?(\d+)>|(\d{15,21})(?:\.0)?", text)

    if found:
        member = guild.get_member(int(found.group(1) or found.group(2)))
        return (member, None) if member else (None, "not in the server")

    key = name_key(text.lstrip("@"))

    if not key:
        return None, "empty"

    def one(members, problem):
        if len(members) == 1:
            return next(iter(members)), None
        return None, problem if members else None

    member, problem = one(names.get(key, set()), "more than one member has that name")
    if member or problem:
        return member, problem

    parts = getattr(names, "parts", {})

    member, problem = one(parts.get(key, set()), "more than one member has that name")
    if member or problem:
        return member, problem

    member, problem = one(
        {m for name, members in names.items() if name.startswith(key) for m in members},
        "more than one member starts with that name"
    )
    if member or problem:
        return member, problem

    # A small typo or spelling difference ("Figueredo" / "Figueiredo").
    import difflib
    close = difflib.get_close_matches(key, list(names.keys()) + list(parts.keys()), n=3, cutoff=0.88)
    members = {m for name in close for m in names.get(name, set()) | parts.get(name, set())}

    member, problem = one(members, "close to more than one member's name")
    if member or problem:
        return member, problem

    # A fighter name whose surname is one side of a nickname ("Cody Garbrandt" → "Jam / Garbrandt").
    if fighter_name:
        words = re.findall(r"[^\W\d_]+", clean_name(text), flags=re.UNICODE)
        surname = role_key(words[-1]) if words else ""
        if len(surname) >= 4:
            member, problem = one(parts.get(surname, set()), None)
            if member:
                return member, None

    return None, "not found"


def read_workbook(
    file_name: str,
    data: bytes
):
    """[(tab name, rows)] for every tab. A .csv is one tab."""

    if file_name.lower().endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        return [("Sheet1", [row for row in csv.reader(io.StringIO(text))])]

    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    tabs = [(sheet.title, [list(row) for row in sheet.iter_rows(values_only=True)]) for sheet in book.worksheets]
    book.close()

    return tabs


DIVISION_HEADERS = {"division", "weight", "weightclass", "class", "weightdivision", "divisions"}
RANK_HEADERS = {"rank", "ranking", "rankings", "position", "pos", "no", "number", "seed"}
FIGHTER_HEADERS = {"fighter", "fighters", "name", "names", "player", "fightername", "character"}
DEFAULT_TAB_NAMES = {"sheet1", "sheet", "rankings", "ranking", "table1", "universe"}


def is_discord_header(
    key: str
):

    return "discord" in key or key in {"username", "user", "userid", "tag", "member", "handle", "discordname", "id"}


def find_columns(
    rows: list
):
    """
    Looks for the header row in the first 10 rows and returns
    {start, division, rank, names} (names = name columns to try, Discord first),
    or None if the tab has no header we recognise.
    Any other columns (W, L, record, notes...) are ignored.
    """

    for index, row in enumerate(rows[:10]):
        keys = [role_key(str(cell or "")) for cell in row]
        texts = [str(cell or "").strip() for cell in row]

        discord_cols = [i for i, k in enumerate(keys) if k and is_discord_header(k)]
        fighter_cols = [i for i, k in enumerate(keys) if k in FIGHTER_HEADERS]

        if not discord_cols and not fighter_cols:
            continue

        division = next((i for i, k in enumerate(keys) if k in DIVISION_HEADERS), None)
        rank = next(
            (i for i, k in enumerate(keys) if (k in RANK_HEADERS and texts[i]) or texts[i] == "#"),
            None
        )

        return {
            "start": index + 1,
            "division": division,
            "rank": rank,
            "names": discord_cols + [i for i in fighter_cols if i not in discord_cols]
        }

    return None


def heading_text(
    row: list
):
    """A row with one bit of text and nothing else, e.g. 'WELTERWEIGHT DIVISION' → 'Welterweight'."""

    filled = [str(cell).strip() for cell in row if str(cell if cell is not None else "").strip()]

    if len(filled) != 1 or read_rank(filled[0]) is not None:
        return None

    text = re.sub(r"\s*(division|rankings?|top\s*\d+)\s*$", "", filled[0], flags=re.IGNORECASE).strip(" :-")

    if not text or len(text) > 40:
        return None

    return text.title() if text.isupper() else text


def looks_champion(
    row: list
):

    text = " ".join(str(cell or "") for cell in row).lower()

    return "👑" in text or "(c)" in text or re.search(r"\bchamp(ion)?\b", text) is not None


# ---------- The owner's layout: one block per weight ----------
#
#   | Flyweight      | User            | Rankings                      |
#   | Amir Albazi    | spidwery_flan62 | C  | Brandon Royval | 7 | 3 ... |
#   | Asu Almabayev  | PlayerGunMc20   | 1  | Asu Almabayev  | 5 | 2 ... |
#   | ...            |                 | NR | Rai Tsruya     | ...       |
#
# Left: every fighter in the weight and their Discord name (User).
# Right: the rankings (C, 1, 2 ... NR) by fighter name. The W/L columns
# are ignored. The two sides are joined by fighter name, so each Discord
# member gets their fighter, weight and rank. A blank User falls back to
# the fighter name (Uni nicknames are the fighter's name).

NOT_RANKED_WORDS = {"nr", "notranked", "unranked", "notrank"}


def find_weight_blocks(
    rows: list
):
    """[{start, end, weight, fighter_col, user_col, rank_col}] for every weight block in a tab."""

    blocks = []

    for index, row in enumerate(rows):
        keys = [role_key(str(cell if cell is not None else "")) for cell in row]

        user_col = next((i for i, k in enumerate(keys) if k in ("user", "users", "discord", "discordname", "username", "discorduser")), None)
        rank_col = next((i for i, k in enumerate(keys) if k.startswith("ranking")), None)

        if user_col is None or rank_col is None or rank_col <= user_col:
            continue

        weight_col = next((i for i in range(user_col) if keys[i]), None)

        if weight_col is None:
            continue

        weight = str(row[weight_col]).strip()

        if role_key(weight) in FIGHTER_HEADERS:
            # "Fighter | User | Rankings": the weight is a heading above.
            weight = next((heading_text(r) for r in reversed(rows[:index]) if heading_text(r)), None) or "Universe"

        weight = re.sub(r"\s*(division|rankings?)\s*$", "", weight, flags=re.IGNORECASE).strip()
        weight = weight.title() if weight.isupper() else weight

        blocks.append({
            "start": index + 1,
            "weight": weight,
            "fighter_col": weight_col,
            "user_col": user_col,
            "rank_col": rank_col
        })

    for number, block in enumerate(blocks):
        block["end"] = blocks[number + 1]["start"] - 1 if number + 1 < len(blocks) else len(rows)

    return blocks


def ranking_fixes(
    guild_id: int,
    kind: str = None
):

    sql = "SELECT * FROM rankings_fixes WHERE guild_id = ?"
    params = [guild_id]

    if kind:
        sql += " AND kind = ?"
        params.append(kind)

    return query(sql + " ORDER BY id", tuple(params))


def fixed_member(
    guild,
    weight: str,
    texts: list
):
    """A saved /rankingsedit link for any of these sheet names (this weight first)."""

    keys = {role_key(clean_name(t)) for t in texts if t}

    for fix in ranking_fixes(guild.id, "link"):
        if role_key(fix["sheet_name"]) in keys and (not fix["division"] or fix["division"].lower() == weight.lower()):
            member = guild.get_member(fix["user_id"])
            if member:
                return member

    return None


def parse_weight_blocks(
    guild,
    rows: list,
    blocks: list,
    names: dict
):
    """
    Returns (entries, problems, missing). entries: {weight, fighter, user_id,
    name, rank (None = not ranked)}. missing: ranked fighters that couldn't
    be matched to a member: [weight, rank, fighter, why].
    """

    entries = []
    problems = []
    missing = []

    for block in blocks:
        weight = block["weight"]
        roster = []
        ranks = {}

        for number in range(block["start"], block["end"]):
            row = rows[number]

            def cell(col, row=row):
                return row[col] if col is not None and col < len(row) else None

            # Left side: fighter and their Discord name.
            fighter = str(cell(block["fighter_col"]) or "").strip()
            user = str(cell(block["user_col"]) or "").strip()

            if fighter and role_key(fighter) not in FIGHTER_HEADERS:
                roster.append((number + 1, fighter, user))

            # Right side: rank, then the fighter's name (the W/L numbers after it are ignored).
            rank_text = str(cell(block["rank_col"]) or "").strip()

            if not rank_text or role_key(rank_text).startswith("notranked"):
                continue

            name = next(
                (str(value).strip() for value in row[block["rank_col"] + 1:]
                 if value is not None and str(value).strip() and not re.fullmatch(r"[\d.\-\s]+", str(value).strip())),
                None
            )

            if not name:
                continue

            if role_key(rank_text) in NOT_RANKED_WORDS:
                ranks.setdefault(name_key(name), (None, name, number + 1))
                continue

            rank = read_rank(rank_text)

            if rank is None:
                problems.append(f"{weight} row {number + 1}: rank `{rank_text}` isn't C, a number or NR")
                continue

            ranks[name_key(name)] = (rank, name, number + 1)

        used = set()
        seen = set()

        for row_number, fighter, user in roster:
            key = name_key(fighter)
            ranked = ranks.get(key)

            if not ranked:
                # Small spelling differences between the two sides.
                close = [k for k in ranks if k not in used and (k.startswith(key) or key.startswith(k))]
                if len(close) != 1:
                    import difflib
                    close = [k for k in difflib.get_close_matches(key, [k for k in ranks if k not in used], n=1, cutoff=0.85)]
                ranked = ranks[close[0]] if close else None

            rank = ranked[0] if ranked else None

            if ranked:
                used.add(name_key(ranked[1]))

            member = fixed_member(guild, weight, [user, fighter])
            problem = None

            if not member and user:
                member, problem = match_member(guild, names, user)

            if not member:
                by_fighter, _ = match_member(guild, names, fighter, fighter_name=True)
                member = by_fighter

            if not member:
                tag = "champion" if rank == 0 else f"#{rank}" if rank else "not ranked"
                why = f"User `{user}` {problem or 'not found'}" if user else "no User filled in"
                problems.append(f"{weight} row {row_number}: {fighter} ({tag}): {why}")
                if rank is not None:
                    missing.append([weight, rank, fighter, why])
                continue

            if member.id in seen:
                problems.append(f"{weight} row {row_number}: {clean_name(member.display_name)} has two fighters in {weight}")
                continue

            seen.add(member.id)
            entries.append({
                "weight": weight,
                "fighter": fighter,
                "user_id": member.id,
                "name": clean_name(member.display_name),
                "rank": rank
            })

        # Ranked on the right but missing from the left-hand list.
        for key, (rank, name, row_number) in ranks.items():
            if key in used or rank is None:
                continue

            member = fixed_member(guild, weight, [name])

            if not member:
                member, _ = match_member(guild, names, name, fighter_name=True)

            if member and member.id not in seen:
                seen.add(member.id)
                entries.append({"weight": weight, "fighter": name, "user_id": member.id,
                                "name": clean_name(member.display_name), "rank": rank})
            else:
                tag = "champion" if rank == 0 else f"#{rank}"
                problems.append(f"{weight} row {row_number}: {name} ({tag}) isn't in the fighter/User list")
                missing.append([weight, rank, name, "not in the fighter/User list on the left"])

    return entries, problems, missing


def apply_rank_fixes(
    guild,
    rankings: list
):
    """Saved /rankingsedit rank and remove fixes, applied on top of every read of the sheet."""

    divisions = [row["division"] for row in rankings]

    for fix in ranking_fixes(guild.id):

        if fix["kind"] == "remove":
            rankings[:] = [r for r in rankings if not (r["user_id"] == fix["user_id"]
                                                       and r["division"].lower() == fix["division"].lower())]

        elif fix["kind"] == "rank":
            existing = next((r for r in rankings if r["user_id"] == fix["user_id"]
                             and r["division"].lower() == fix["division"].lower()), None)
            if existing:
                existing["rank"] = fix["rank"]
                existing["fixed"] = True
            else:
                member = guild.get_member(fix["user_id"])
                division = next((d for d in divisions if d.lower() == fix["division"].lower()), fix["division"])
                if division not in divisions:
                    divisions.append(division)
                rankings.append({
                    "division": division,
                    "division_order": divisions.index(division),
                    "rank": fix["rank"],
                    "user_id": fix["user_id"],
                    "name": clean_name(member.display_name) if member else fix["sheet_name"],
                    "fighter": fix["sheet_name"],
                    "fixed": True
                })

    return rankings


def parse_rankings(
    guild,
    file_name: str,
    data: bytes
):
    """
    Reads the owner's sheet as it is. Returns (rankings, problems).
    rankings: [{division, division_order, rank, user_id, name}]
    problems: ["Welterweight row 14: jonjones99 (not found)", ...]
    """

    tabs = read_workbook(file_name, data)
    names = member_lookup(guild)
    several_tabs = len([t for t in tabs if find_columns(t[1])]) > 1

    rankings = []
    problems = []
    missing = []
    divisions = []
    seen = set()

    for tab_name, rows in tabs:

        # The owner's layout (a block per weight: Weight | User | Rankings).
        blocks = find_weight_blocks(rows)

        if blocks:
            entries, block_problems, block_missing = parse_weight_blocks(guild, rows, blocks, names)
            problems += block_problems
            missing += block_missing

            for entry in entries:
                weight = entry["weight"]

                if (weight.lower(), entry["user_id"]) in seen:
                    continue

                seen.add((weight.lower(), entry["user_id"]))

                if weight.lower() not in [d.lower() for d in divisions]:
                    divisions.append(weight)

                rankings.append({
                    "division": next(d for d in divisions if d.lower() == weight.lower()),
                    "division_order": [d.lower() for d in divisions].index(weight.lower()),
                    "rank": entry["rank"],
                    "user_id": entry["user_id"],
                    "name": entry["name"],
                    "fighter": entry["fighter"]
                })

            continue

        columns = find_columns(rows)

        if not columns:
            # Old 3-column layout with no header: Division, Rank, Fighter.
            if rows and len(rows[0]) >= 3 and read_rank(rows[0][1]) is not None:
                columns = {"start": 0, "division": 0, "rank": 1, "names": [2]}
            else:
                continue

        tab_division = (
            tab_name if several_tabs or role_key(tab_name) not in DEFAULT_TAB_NAMES else None
        )
        current_division = tab_division
        next_rank = {}

        # A heading above the header row, e.g. "WELTERWEIGHT DIVISION" then "Rank | Fighter | ...".
        for row in reversed(rows[:max(0, columns["start"] - 1)]):
            heading = heading_text(row)
            if heading:
                if role_key(heading) not in ("ufcuniverse", "universe", "ufcuniverserankings"):
                    current_division = heading
                break

        for number, row in enumerate(rows[columns["start"]:], start=columns["start"] + 1):

            def cell(col, row=row):
                return row[col] if col is not None and col < len(row) else None

            heading = heading_text(row)

            if heading and not any(str(cell(col) or "").strip() for col in columns["names"]):
                current_division = heading
                continue

            name_values = [str(cell(col) or "").strip() for col in columns["names"]]

            if not any(name_values):
                continue

            # The header row repeated above each division.
            if all(is_discord_header(role_key(v)) or role_key(v) in FIGHTER_HEADERS for v in name_values if v):
                continue

            division = str(cell(columns["division"]) or "").strip() if columns["division"] is not None else ""
            division = division or current_division or "Universe"
            current_division = division if columns["division"] is not None else current_division

            where = f"{division} row {number}"

            # Rank: the rank column if there is one, otherwise the order down the sheet.
            rank = read_rank(cell(columns["rank"])) if columns["rank"] is not None else None

            if rank is None and looks_champion(row):
                rank = 0

            if rank is None:
                if columns["rank"] is not None and str(cell(columns["rank"]) or "").strip():
                    problems.append(f"{where}: rank `{cell(columns['rank'])}` isn't C or a number")
                    continue
                rank = next_rank.get(division.lower(), 1)

            next_rank[division.lower()] = max(next_rank.get(division.lower(), 1), rank + 1)

            member, problem = fixed_member(guild, division, name_values), "not found"

            for value in name_values:
                if value and not member:
                    member, problem = match_member(guild, names, value)

            if not member:
                shown = next(value for value in name_values if value)
                problems.append(f"{where}: `{shown[:40]}` ({problem})")
                continue

            if (division.lower(), member.id) in seen:
                problems.append(f"{where}: {clean_name(member.display_name)} is in {division} twice")
                continue

            seen.add((division.lower(), member.id))

            if division.lower() not in [d.lower() for d in divisions]:
                divisions.append(division)

            rankings.append({
                "division": next(d for d in divisions if d.lower() == division.lower()),
                "division_order": [d.lower() for d in divisions].index(division.lower()),
                "rank": rank,
                "user_id": member.id,
                "name": clean_name(member.display_name),
                "fighter": None
            })

    set_meta(f"rankings_missing:{guild.id}", json.dumps(missing[:200]))

    return apply_rank_fixes(guild, rankings), problems


def save_rankings(
    guild_id: int,
    rankings: list,
    file_name: str,
    uploaded_by: int
):

    db = get_db()
    db.execute("DELETE FROM rankings WHERE guild_id = ?", (guild_id,))

    for row in rankings:
        db.execute(
            "INSERT INTO rankings (guild_id, division, division_order, rank, user_id, name, fighter, fixed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, row["division"], row["division_order"], row["rank"], row["user_id"], row["name"],
             row.get("fighter"), 1 if row.get("fixed") else 0)
        )

    db.execute(
        "INSERT OR REPLACE INTO rankings_uploads (guild_id, file_name, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?)",
        (guild_id, file_name, uploaded_by, now_utc())
    )

    db.commit()
    db.close()


def user_rankings(
    guild_id: int,
    user_id: int
):
    """[(division, rank)] in sheet order. rank 0 = champion. Not-ranked (NR) fighters aren't included."""

    return [
        (row["division"], row["rank"]) for row in query(
            "SELECT division, rank FROM rankings WHERE guild_id = ? AND user_id = ? AND rank IS NOT NULL "
            "ORDER BY division_order",
            (guild_id, user_id)
        )
    ]


def rank_in_division(
    guild_id: int,
    user_id: int,
    division: str
):

    for name, rank in user_rankings(guild_id, user_id):
        if name.lower() == division.lower():
            return rank

    return None


def shared_division(
    guild_id: int,
    a: int,
    b: int
):
    """The division both fighters are ranked in (a weight class before P4P), or None."""

    a_divisions = {d.lower(): d for d, _ in user_rankings(guild_id, a)}
    shared = [d for d, _ in user_rankings(guild_id, b) if d.lower() in a_divisions]

    for division in shared:
        if not is_p4p(division):
            return division

    return shared[0] if shared else None


def best_rank(
    guild_id: int,
    user_id: int
):
    """Their best rank in a weight class (P4P only if that's all they have)."""

    rows = user_rankings(guild_id, user_id)
    weights = [rank for division, rank in rows if not is_p4p(division)]
    pool = weights or [rank for _, rank in rows]

    return min(pool) if pool else None


@bot.tree.command(
    name="uploadrankings",
    description="Upload the rankings spreadsheet (.xlsx or .csv) so the odds use it."
)
@app_commands.describe(file="The owner's rankings spreadsheet (.xlsx or .csv)")
async def uploadrankings(
    interaction: discord.Interaction,
    file: discord.Attachment
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can upload rankings.", ephemeral=True)
        return

    name = file.filename or ""

    if not name.lower().endswith((".xlsx", ".xlsm", ".csv")):
        await interaction.response.send_message(
            "❌ Upload an Excel file (**.xlsx**) or a **.csv**. In Google Sheets: File → Download → .xlsx.",
            ephemeral=True
        )
        return

    if file.size > MAX_RANKINGS_FILE:
        await interaction.response.send_message("❌ That file is too big (2 MB max).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        data = await file.read()
        ranked, problems = parse_rankings(interaction.guild, name, data)
        keep_last_sheet(interaction.guild.id, name, data)
    except ImportError:
        await interaction.followup.send(
            "❌ The bot can't read Excel files yet: add `openpyxl` to requirements.txt and redeploy. "
            "Or upload the sheet as a .csv.",
            ephemeral=True
        )
        return
    except Exception as error:
        print(f"Rankings upload failed: {error}")
        await interaction.followup.send(
            "❌ Couldn't read that file. Upload the sheet as an **.xlsx** (Excel: File → Save a copy; "
            "Google Sheets: File → Download → Microsoft Excel) or a **.csv**.",
            ephemeral=True
        )
        return

    if not ranked:
        lines = ["❌ No fighters could be matched, so the rankings weren't changed."]
        lines += [f"• {problem}" for problem in problems[:15]]
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    before = rankings_snapshot(interaction.guild.id)
    save_rankings(interaction.guild.id, ranked, name, interaction.user.id)
    set_meta(f"rankings_hash:{interaction.guild.id}", rankings_fingerprint(ranked))
    set_meta(f"rankings_problems:{interaction.guild.id}", "\n".join(problems[:60]))

    divisions = len({row["division"] for row in ranked})

    ranked_count = sum(1 for row in ranked if row["rank"] is not None)
    lines = [f"✅ **{divisions}** weight{'s' if divisions != 1 else ''} · **{ranked_count}** ranked · "
             f"**{len(ranked) - ranked_count}** not ranked (linked to their weight)"]

    if problems:
        lines += ["", f"⚠️ **{len(problems)} row{'s' if len(problems) != 1 else ''} skipped.** Fix them in the sheet, "
                  "or tell the bot who it is with `/rankingsedit link`:"]
        lines += [f"• {problem}" for problem in problems[:15]]

        if len(problems) > 15:
            lines.append(f"• …and {len(problems) - 15} more")

    lines += ["", "-# Replaces the old rankings. Odds on cards that are already open don't change.",
              "-# See every fighter it read (only you can see it): `/rankingscheck`"]

    if get_meta(f"rankings_link:{interaction.guild.id}"):
        lines.append("-# A sheet is linked too: the next time it changes, it replaces this upload.")

    embed = discord.Embed(description="\n".join(lines), color=BRAND_RED)
    apply_branding(embed, author_text=f"{BRAND_TAG} • RANKINGS UPDATED")

    await interaction.followup.send(embed=embed, ephemeral=True)

    await announce_ranking_changes(
        interaction.guild, before,
        f"📤 Sheet uploaded by {clean_name(interaction.user.display_name)}"
    )


# ---------- Linked sheet: Google Sheets or OneDrive (keeps itself up to date) ----------

RANKINGS_SYNC_MINUTES = int(os.getenv("RANKINGS_SYNC_MINUTES", "15") or 15)
MAX_SHEET_DOWNLOAD = 5_000_000


class SheetNotShared(Exception):
    pass


def sheet_download_url(
    link: str
):
    """
    ([download urls to try], file name) for a Google Sheets, OneDrive / Excel
    Online, or direct .xlsx/.csv link, else None.
    """

    link = (link or "").strip().strip("<>")
    found = (
        re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", link)
        or re.search(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)", link)
        or re.search(r"drive\.google\.com/(?:open|uc)\?(?:.*&)?id=([A-Za-z0-9_-]+)", link)
    )

    if found:
        file_id = found.group(1)
        return [
            # A real Google Sheet.
            f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx",
            # An Excel file uploaded to Google Drive and opened in Sheets (links with
            # rtpof=true): Google won't export those, but it will hand over the file.
            f"https://drive.google.com/uc?export=download&id={file_id}",
            f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        ], "google_sheet.xlsx"

    # OneDrive / Excel Online share links (1drv.ms, onedrive.live.com, sharepoint.com).
    if re.match(r"https://(1drv\.ms|onedrive\.live\.com|[\w.-]+\.sharepoint\.com)/", link):
        import base64
        token = "u!" + base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")
        joiner = "&" if "?" in link else "?"
        return [
            f"https://api.onedrive.com/v1.0/shares/{token}/root/content",
            f"{link}{joiner}download=1"
        ], "onedrive_sheet.xlsx"

    path = link.split("?")[0].lower()

    if link.startswith("https://") and path.endswith((".xlsx", ".csv")):
        return [link], path.rsplit("/", 1)[-1]

    return None


SHEET_HEADERS = {"User-Agent": "Mozilla/5.0 (UFC Universe bot; rankings sync)"}


async def download_sheet(
    url: str,
    file_name: str = "sheet.xlsx"
):
    """The file's bytes. Raises SheetNotShared(reason) when we get a page or a refusal instead."""

    import aiohttp

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout, headers=SHEET_HEADERS) as session:
        async with session.get(url, allow_redirects=True) as response:

            if response.status in (401, 403, 404):
                raise SheetNotShared(f"refused ({response.status})")

            if response.status != 200:
                raise RuntimeError(f"download failed ({response.status})")

            # Read until the whole file has arrived (a single read() can stop
            # part-way, which leaves a broken half of the file).
            data = bytearray()

            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)

                if len(data) > MAX_SHEET_DOWNLOAD:
                    raise RuntimeError("the sheet is too big (5 MB max)")

            data = bytes(data)

    # A sign-in or viewer web page instead of the file.
    if data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        raise SheetNotShared("sent a web page, not the file")

    # .xlsx files are zip files: they always start with "PK".
    if file_name.lower().endswith(".xlsx") and not data.startswith(b"PK"):
        raise SheetNotShared("what came back isn't an Excel file")

    return data


async def onedrive_download_urls(
    link: str
):
    """
    Every known way to download a OneDrive share link without signing in.
    Microsoft changed personal share links in 2024 and the old method is
    refused for new links, so this also follows the link to where it really
    points and tries the download forms of that address.
    """

    import base64
    from urllib.parse import parse_qs, urljoin, urlparse
    import aiohttp

    token = "u!" + base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")
    urls = [f"https://api.onedrive.com/v1.0/shares/{token}/root/content"]

    # Follow the short link's redirects to the real address.
    final = link

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=SHEET_HEADERS) as session:
            for _ in range(6):
                async with session.get(final, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status not in (301, 302, 303, 307, 308) or not location:
                        break
                    final = urljoin(final, location)
    except Exception as error:
        print(f"OneDrive redirect check failed: {error}")

    params = {key: values[0] for key, values in parse_qs(urlparse(final).query).items()}
    resid = params.get("resid") or params.get("id")
    authkey = params.get("authkey")
    cid = params.get("cid")

    if resid:
        extra = (f"&authkey={authkey}" if authkey else "") + (f"&cid={cid}" if cid else "")
        urls.append(f"https://onedrive.live.com/download?resid={resid}{extra}")

    for address in (final, link):
        joiner = "&" if "?" in address else "?"
        urls.append(f"{address}{joiner}download=1")

    # Same order, no repeats.
    return list(dict.fromkeys(urls))


def rankings_fingerprint(
    rankings: list
):

    rows = sorted(
        (r["division"].lower(), -1 if r["rank"] is None else r["rank"], r["user_id"], r.get("fighter") or "")
        for r in rankings
    )

    return hashlib.sha256(repr(rows).encode()).hexdigest()


async def sync_rankings_from_link(
    guild,
    by: int = 0
):
    """
    Reads the linked sheet. Returns (rankings, problems, changed, error).
    Only saves when the rankings actually changed, so "updated" means updated.
    """

    link = get_meta(f"rankings_link:{guild.id}")
    target = sheet_download_url(link) if link else None

    if not target:
        return None, [], False, "No sheet is linked. Use `/rankingslink` with the sheet's link."

    onedrive = target[1] == "onedrive_sheet.xlsx"
    tried = []

    try:
        data, last_error = None, None
        urls = await onedrive_download_urls(link) if onedrive else target[0]

        for url in urls:
            try:
                data = await download_sheet(url, target[1])
                break
            except Exception as error:
                last_error = error
                tried.append(f"{urls.index(url) + 1}: {error}")

        if data is None:
            raise last_error

        ranked, problems = parse_rankings(guild, target[1], data)
        keep_last_sheet(guild.id, target[1], data)
    except SheetNotShared:
        print(f"Rankings sync for {guild.id}: couldn't download ({'; '.join(tried)})")

        if onedrive:
            return None, [], False, (
                "OneDrive won't let the bot download the sheet. Microsoft changed personal OneDrive "
                "links in 2024 and they often block bots, even when shared with anyone.\n"
                "**Easiest fix:** put the sheet in **Google Sheets** (File → Import the Excel file, "
                "or open it there), share it as **Anyone with the link: Viewer**, and use that link here. "
                "Or keep it in OneDrive and use `/uploadrankings` after each change.\n"
                f"-# Tried {len(tried)} ways: " + " · ".join(tried)
            )

        return None, [], False, (
            "I can't open the sheet. Make sure anyone with the link can **view** it: "
            "Google Sheets → **Share** → General access → **Anyone with the link** (Viewer). "
            "Or upload the file with `/uploadrankings`.\n"
            f"-# {' · '.join(tried)}"
        )
    except Exception as error:
        print(f"Rankings sync for {guild.id} failed: {error} ({'; '.join(tried)})")

        if "zip" in str(error).lower():
            return None, [], False, (
                "What the link gave me isn't an Excel file. In Google Sheets, check it's shared as "
                "**Anyone with the link: Viewer**. If the sheet was uploaded from Excel, do "
                "**File → Save as Google Sheets** and link the new sheet. Or use `/uploadrankings`."
            )

        return None, [], False, f"I couldn't read the sheet ({error})."

    set_meta(f"rankings_checked:{guild.id}", now_utc())
    set_meta(f"rankings_problems:{guild.id}", "\n".join(problems[:60]))

    if not ranked:
        return ranked, problems, False, "No fighters in the sheet could be matched, so the rankings weren't changed."

    fingerprint = rankings_fingerprint(ranked)
    changed = fingerprint != get_meta(f"rankings_hash:{guild.id}")

    if changed:
        before = rankings_snapshot(guild.id)
        save_rankings(guild.id, ranked, "Linked sheet", by)
        set_meta(f"rankings_hash:{guild.id}", fingerprint)

        member = guild.get_member(by) if by else None
        await announce_ranking_changes(
            guild, before,
            "🔄 From the rankings sheet" + (f" (checked by {clean_name(member.display_name)})" if member else "")
        )

    return ranked, problems, changed, None


async def sync_linked_rankings():
    """Every-minute check: re-read each linked sheet every RANKINGS_SYNC_MINUTES."""

    for guild in bot.guilds:

        if not get_meta(f"rankings_link:{guild.id}"):
            continue

        last = get_meta(f"rankings_checked:{guild.id}")

        try:
            checked = datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None
            if checked and checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
        except ValueError:
            checked = None

        if checked and datetime.now(timezone.utc) - checked < timedelta(minutes=RANKINGS_SYNC_MINUTES):
            continue

        _, _, changed, error = await sync_rankings_from_link(guild)

        if error:
            print(f"Rankings sync ({guild.name}): {error}")
            # Don't retry every minute when the sheet is broken.
            set_meta(f"rankings_checked:{guild.id}", now_utc())
        elif changed:
            print(f"Rankings updated from the sheet for {guild.name}.")


@bot.tree.command(
    name="rankingslink",
    description="Link the rankings sheet (Google Sheets or OneDrive) so the bot keeps itself up to date."
)
@app_commands.describe(
    link="The sheet's share link (leave empty to check it now, or type off to unlink)"
)
async def rankingslink(
    interaction: discord.Interaction,
    link: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can link the rankings sheet.", ephemeral=True)
        return

    guild = interaction.guild

    if link and link.strip().lower() in ("off", "none", "stop", "remove", "unlink"):
        query("DELETE FROM bot_meta WHERE key IN (?, ?, ?)",
              (f"rankings_link:{guild.id}", f"rankings_checked:{guild.id}", f"rankings_problems:{guild.id}"),
              write=True)
        await interaction.response.send_message(
            "✅ Sheet unlinked. The current rankings stay until you upload or link new ones.",
            ephemeral=True
        )
        return

    if link:
        if not sheet_download_url(link):
            await interaction.response.send_message(
                "❌ Paste the sheet's share link: Google Sheets (`docs.google.com/spreadsheets/...` "
                "or a Google Drive file link) "
                "or OneDrive (`1drv.ms/...`).",
                ephemeral=True
            )
            return

        set_meta(f"rankings_link:{guild.id}", link.strip().strip("<>"))

    await interaction.response.defer(ephemeral=True, thinking=True)

    ranked, problems, changed, error = await sync_rankings_from_link(guild, interaction.user.id)

    if error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    divisions = len({row["division"] for row in ranked})

    ranked_count = sum(1 for row in ranked if row["rank"] is not None)
    lines = [
        f"✅ **{divisions}** weight{'s' if divisions != 1 else ''} · **{ranked_count}** ranked · "
        f"**{len(ranked) - ranked_count}** not ranked"
        + (" · updated" if changed else " · no changes since last check"),
        f"🔄 I'll re-check the sheet every **{RANKINGS_SYNC_MINUTES} minutes**, so edits show up by themselves.",
        "-# Only the rankings are read. Wins and losses still come from the fights logged in the bot.",
        "-# See every fighter it read (only you can see it): `/rankingscheck`"
    ]

    if problems:
        lines += ["", f"⚠️ **{len(problems)} row{'s' if len(problems) != 1 else ''} skipped** "
                  "(fix them in the sheet, or `/rankingsedit link`):"]
        lines += [f"• {problem}" for problem in problems[:15]]

        if len(problems) > 15:
            lines.append(f"• …and {len(problems) - 15} more")

    embed = discord.Embed(description="\n".join(lines), color=BRAND_RED)
    apply_branding(embed, author_text=f"{BRAND_TAG} • RANKINGS SHEET")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------- /rankingscheck: staff see what the bot read (privately) ----------

def rankings_check_embed(
    guild,
    view: str = "summary"
):
    """
    view: 'summary', 'skipped', or a division name. Only ever sent privately
    to staff: rankings aren't shown anywhere public.
    """

    divisions = [
        row["division"] for row in query(
            "SELECT division, MIN(division_order) AS o FROM rankings WHERE guild_id = ? GROUP BY division ORDER BY o",
            (guild.id,)
        )
    ]
    upload = query("SELECT * FROM rankings_uploads WHERE guild_id = ?", (guild.id,))
    skipped = [line for line in (get_meta(f"rankings_problems:{guild.id}") or "").split("\n") if line]
    link = get_meta(f"rankings_link:{guild.id}")

    if view == "skipped":
        title = f"Skipped rows ({len(skipped)})"
        lines = [f"• {line}" for line in skipped] or ["Nothing was skipped. Every row matched a member."]
        lines += ["", "-# Fix these in the sheet (usually a typo or an old Discord name), or tell the bot who it is: "
                  "`/rankingsedit link`."]

    elif view in divisions:
        title = view
        rows = query(
            "SELECT * FROM rankings WHERE guild_id = ? AND division = ? ORDER BY rank IS NULL, rank, id",
            (guild.id, view)
        )

        def who(row):
            member = fighter_name(guild, row["user_id"], row["name"])
            fighter = row["fighter"]
            text = f"{fighter} · {member}" if fighter and name_key(fighter) != name_key(member) else member
            return text + (" ✏️" if row["fixed"] else "")

        ranked_rows = [row for row in rows if row["rank"] is not None]
        unranked = [row for row in rows if row["rank"] is None]

        try:
            gaps = [m for m in json.loads(get_meta(f"rankings_missing:{guild.id}") or "[]") if m[0] == view]
        except ValueError:
            gaps = []

        filled = {row["rank"] for row in ranked_rows}
        slots = [(row["rank"], 0, row) for row in ranked_rows]
        slots += [(gap[1], 1, gap) for gap in gaps if gap[1] not in filled]

        lines = []

        for rank, is_gap, item in sorted(slots, key=lambda slot: (slot[0], slot[1])):
            tag = "👑" if rank == 0 else f"`{rank:>2}`"
            if is_gap:
                lines.append(f"{tag} ⚠️ **{item[2]}**: {item[3]}")
            elif rank == 0:
                lines.append(f"{tag} **{who(item)}**")
            else:
                lines.append(f"{tag} {who(item)}")

        if not lines:
            lines = ["Nobody ranked."]

        if any(is_gap for _, is_gap, _ in slots):
            lines += ["", "-# ⚠️ = ranked in the sheet but not matched to a member, so the odds can't use it. "
                          "Fix the User in the sheet, or `/rankingsedit link`."]

        if unranked:
            lines += ["", f"**Not ranked** ({len(unranked)})"] + [f"`NR` {who(row)}" for row in unranked]

        lines += ["", "-# Fighter · Discord member. ✏️ = changed with /rankingsedit"]

    else:
        title = "What the bot has read"
        lines = []

        if upload:
            source = "the linked sheet" if upload[0]["file_name"] == "Linked sheet" else f"`{upload[0]['file_name']}`"
            lines.append(f"From {source} · updated {fight_date_text(upload[0]['uploaded_at']) or '?'}")

        if link:
            checked = get_meta(f"rankings_checked:{guild.id}")
            lines.append(
                f"🔗 Sheet linked, re-checked every {RANKINGS_SYNC_MINUTES} min"
                + (f" (last check {fight_date_text(checked)})" if checked else "")
            )

        lines.append("")

        for division in divisions:
            rows = query(
                "SELECT * FROM rankings WHERE guild_id = ? AND division = ? ORDER BY rank, id",
                (guild.id, division)
            )
            champ = next((row for row in rows if row["rank"] == 0), None)
            ranked_here = sum(1 for row in rows if row["rank"] is not None)

            try:
                gaps = len([m for m in json.loads(get_meta(f"rankings_missing:{guild.id}") or "[]") if m[0] == division])
            except ValueError:
                gaps = 0

            lines.append(
                f"**{division}** · {ranked_here} ranked"
                + (f" (⚠️ {gaps} not matched)" if gaps else "")
                + f" · {len(rows) - ranked_here} NR"
                + (f" · 👑 {champ['fighter'] or fighter_name(guild, champ['user_id'], champ['name'])}" if champ else "")
            )

        if not divisions:
            lines.append("No rankings yet. Use `/rankingslink` or `/uploadrankings`.")

        if skipped:
            lines += ["", f"⚠️ {len(skipped)} row{'s' if len(skipped) != 1 else ''} skipped: pick **Skipped rows** below."]

        fixes = len(ranking_fixes(guild.id))

        if fixes:
            lines.append(f"✏️ {fixes} fix{'es' if fixes != 1 else ''} from `/rankingsedit` on top of the sheet (`/rankingsedit list`).")

        seen_members = len([m for m in guild.members if not m.bot])
        total_members = getattr(guild, "member_count", None)

        lines += [
            "",
            f"-# The bot can see {seen_members} member{'s' if seen_members != 1 else ''}"
            + (f" of {total_members}" if total_members else "")
            + ". If that's far fewer than the server has, turn on Server Members Intent in the Developer Portal.",
            "-# Only you can see this. Pick a weight below to see every rank in it."
        ]

    embed = discord.Embed(title=title, description="\n".join(lines)[:4096], color=BRAND_RED)
    apply_branding(embed, author_text=f"{BRAND_TAG} • RANKINGS CHECK")

    return embed, divisions, skipped


class RankingsCheckView(
    discord.ui.View
):

    def __init__(
        self,
        guild,
        user_id: int,
        current: str = "summary"
    ):

        super().__init__(timeout=900)

        self.guild = guild
        self.user_id = user_id
        self.current = current
        self.build()

    def build(self):

        self.clear_items()
        _, divisions, skipped = rankings_check_embed(self.guild, "summary")

        options = [discord.SelectOption(label="Summary", value="summary", emoji="📋", default=self.current == "summary")]
        options += [
            discord.SelectOption(label=division[:100], value=division[:100], default=self.current == division)
            for division in divisions[:23]
        ]
        options.append(discord.SelectOption(
            label=f"Skipped rows ({len(skipped)})", value="skipped", emoji="⚠️", default=self.current == "skipped"))

        select = discord.ui.Select(placeholder="Choose a division", options=options)
        select.callback = self.choose
        self.add_item(select)
        self.select = select

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        return interaction.user.id == self.user_id

    async def choose(
        self,
        interaction: discord.Interaction
    ):

        self.current = self.select.values[0]
        embed, _, _ = rankings_check_embed(interaction.guild, self.current)
        self.build()

        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(
    name="rankingscheck",
    description="Staff: privately see every fighter the bot read from the rankings sheet."
)
async def rankingscheck(
    interaction: discord.Interaction
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can check the rankings.", ephemeral=True)
        return

    embed, _, _ = rankings_check_embed(interaction.guild, "summary")

    await interaction.response.send_message(
        embed=embed,
        view=RankingsCheckView(interaction.guild, interaction.user.id),
        ephemeral=True
    )


# ---------- /rankingsedit: fix small mistakes without touching the sheet ----------
#
# Fixes are saved and applied on top of every read of the sheet (upload
# or link), so the next sync doesn't undo them. They stay until removed
# with /rankingsedit undo. The bot keeps a copy of the last sheet it read,
# so a fix shows straight away.

def last_sheet_path(
    guild_id: int
):

    folder = os.path.dirname(os.path.abspath(DATABASE_FILE))

    return os.path.join(folder, f"rankings_sheet_{guild_id}.bin")


def keep_last_sheet(
    guild_id: int,
    file_name: str,
    data: bytes
):

    try:
        with open(last_sheet_path(guild_id), "wb") as handle:
            handle.write(data)
        set_meta(f"rankings_sheet_name:{guild_id}", file_name)
    except Exception as error:
        print(f"Couldn't keep a copy of the rankings sheet: {error}")


def reread_last_sheet(
    guild
):
    """Re-reads the kept copy with the current fixes. Returns (rankings, problems) or None."""

    name = get_meta(f"rankings_sheet_name:{guild.id}")
    path = last_sheet_path(guild.id)

    if not name or not os.path.exists(path):
        return None

    with open(path, "rb") as handle:
        data = handle.read()

    ranked, problems = parse_rankings(guild, name, data)

    if ranked:
        source = query("SELECT file_name FROM rankings_uploads WHERE guild_id = ?", (guild.id,))
        save_rankings(guild.id, ranked, source[0]["file_name"] if source else name, 0)
        set_meta(f"rankings_hash:{guild.id}", rankings_fingerprint(ranked))
        set_meta(f"rankings_problems:{guild.id}", "\n".join(problems[:60]))

    return ranked, problems


def read_rank_choice(
    text: str
):
    """'C' → 0, '3' / '#3' → 3, 'NR' → None. Raises ValueError otherwise."""

    if role_key(text) in NOT_RANKED_WORDS:
        return None

    rank = read_rank(text)

    if rank is None:
        raise ValueError

    return rank


async def weight_autocomplete(
    interaction: discord.Interaction,
    current: str
):

    weights = [
        row["division"] for row in query(
            "SELECT division, MIN(division_order) AS o FROM rankings WHERE guild_id = ? GROUP BY division ORDER BY o",
            (interaction.guild.id,)
        )
    ]

    return [app_commands.Choice(name=w[:100], value=w[:100]) for w in weights if current.lower() in w.lower()][:25]


async def fix_autocomplete(
    interaction: discord.Interaction,
    current: str
):

    choices = [app_commands.Choice(name="All of them", value=0)] if "all".startswith(current.lower()) else []

    for fix in ranking_fixes(interaction.guild.id):
        label = f"#{fix['id']} {fix_text(interaction.guild, fix)}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=fix["id"]))

    return choices[:25]


def fix_text(
    guild,
    fix
):

    who = fighter_name(guild, fix["user_id"])
    weight = fix["division"] or "every weight"

    if fix["kind"] == "link":
        return f"'{fix['sheet_name']}' is {who} ({weight})"

    if fix["kind"] == "remove":
        return f"{who} taken out of {weight}"

    rank = "not ranked" if fix["rank"] is None else "champion" if fix["rank"] == 0 else f"#{fix['rank']}"

    return f"{who} is {rank} in {weight}"


def save_fix(
    guild_id: int,
    kind: str,
    division: str,
    user_id: int,
    by: int,
    sheet_name: str = None,
    rank=None
):

    # A newer fix of the same kind for the same person and weight replaces the old one.
    if kind == "link":
        query("DELETE FROM rankings_fixes WHERE guild_id = ? AND kind = 'link' AND lower(sheet_name) = lower(?) "
              "AND COALESCE(lower(division), '') = COALESCE(lower(?), '')",
              (guild_id, sheet_name, division), write=True)
    else:
        query("DELETE FROM rankings_fixes WHERE guild_id = ? AND kind IN ('rank', 'remove') AND user_id = ? "
              "AND lower(division) = lower(?)",
              (guild_id, user_id, division), write=True)

    query(
        "INSERT INTO rankings_fixes (guild_id, kind, division, sheet_name, user_id, rank, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, kind, division, sheet_name, user_id, rank, by, now_utc()),
        write=True
    )


async def finish_fix(
    interaction: discord.Interaction,
    message: str
):

    before = rankings_snapshot(interaction.guild.id)
    result = reread_last_sheet(interaction.guild)

    if result is None:
        tail = "It'll apply the next time the sheet is read (`/rankingslink` or `/uploadrankings`)."
    else:
        tail = "Applied now. `/rankingscheck` to see it."

    await interaction.response.send_message(
        f"✅ {message}\n-# Saved, and kept on every future read of the sheet until you remove it "
        f"with `/rankingsedit undo`. {tail}",
        ephemeral=True
    )

    if result is not None:
        await announce_ranking_changes(
            interaction.guild, before,
            f"✏️ Fixed with /rankingsedit by {clean_name(interaction.user.display_name)}"
        )


rankings_edit_group = app_commands.Group(
    name="rankingsedit",
    description="Fix small rankings mistakes. Fixes stay on top of the sheet until removed."
)


@rankings_edit_group.command(
    name="link",
    description="Tell the bot who a name in the sheet is (e.g. a Discord name it couldn't find)."
)
@app_commands.describe(
    sheet_name="The name as written in the sheet (User or fighter name)",
    member="Who it is",
    weight="Only in this weight (default: every weight)"
)
@app_commands.autocomplete(weight=weight_autocomplete)
async def rankingsedit_link(
    interaction: discord.Interaction,
    sheet_name: app_commands.Range[str, 1, 80],
    member: discord.Member,
    weight: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can edit rankings.", ephemeral=True)
        return

    save_fix(interaction.guild.id, "link", weight, member.id, interaction.user.id, sheet_name.strip())

    await finish_fix(
        interaction,
        f"'{sheet_name.strip()}' in the sheet is **{clean_name(member.display_name)}**"
        + (f" in {weight}." if weight else ".")
    )


@rankings_edit_group.command(
    name="rank",
    description="Set someone's rank in a weight (C, a number, or NR)."
)
@app_commands.describe(
    member="Who",
    weight="Which weight",
    rank="C for champion, a number, or NR for not ranked"
)
@app_commands.autocomplete(weight=weight_autocomplete)
async def rankingsedit_rank(
    interaction: discord.Interaction,
    member: discord.Member,
    weight: str,
    rank: app_commands.Range[str, 1, 4]
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can edit rankings.", ephemeral=True)
        return

    try:
        value = read_rank_choice(rank)
    except ValueError:
        await interaction.response.send_message("❌ Rank must be C, a number or NR.", ephemeral=True)
        return

    current = query(
        "SELECT fighter FROM rankings WHERE guild_id = ? AND user_id = ? AND lower(division) = lower(?)",
        (interaction.guild.id, member.id, weight)
    )
    fighter = current[0]["fighter"] if current and current[0]["fighter"] else clean_name(member.display_name)

    save_fix(interaction.guild.id, "rank", weight.strip(), member.id, interaction.user.id, fighter, value)

    shown = "not ranked" if value is None else "champion" if value == 0 else f"#{value}"

    await finish_fix(interaction, f"**{clean_name(member.display_name)}** is {shown} in {weight.strip()}.")


@rankings_edit_group.command(
    name="remove",
    description="Take someone out of a weight's rankings."
)
@app_commands.describe(member="Who", weight="Which weight")
@app_commands.autocomplete(weight=weight_autocomplete)
async def rankingsedit_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    weight: str
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can edit rankings.", ephemeral=True)
        return

    save_fix(interaction.guild.id, "remove", weight.strip(), member.id, interaction.user.id)

    await finish_fix(interaction, f"**{clean_name(member.display_name)}** is out of {weight.strip()}.")


@rankings_edit_group.command(
    name="list",
    description="Every saved rankings fix."
)
async def rankingsedit_list(
    interaction: discord.Interaction
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can edit rankings.", ephemeral=True)
        return

    fixes = ranking_fixes(interaction.guild.id)
    lines = [f"`#{fix['id']}` {fix_text(interaction.guild, fix)}" for fix in fixes] or ["No fixes saved."]

    embed = discord.Embed(title="Rankings fixes", description="\n".join(lines)[:4000], color=BRAND_RED)
    apply_branding(embed, author_text=f"{BRAND_TAG} • RANKINGS FIXES")
    embed.set_footer(text="These sit on top of the sheet. Remove one with /rankingsedit undo.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@rankings_edit_group.command(
    name="undo",
    description="Remove a saved fix (so the sheet decides again)."
)
@app_commands.describe(fix="Which fix (or All of them)")
@app_commands.autocomplete(fix=fix_autocomplete)
async def rankingsedit_undo(
    interaction: discord.Interaction,
    fix: int
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can edit rankings.", ephemeral=True)
        return

    if fix == 0:
        count = len(ranking_fixes(interaction.guild.id))
        query("DELETE FROM rankings_fixes WHERE guild_id = ?", (interaction.guild.id,), write=True)
        message = f"Removed all {count} fix{'es' if count != 1 else ''}."
    else:
        rows = query("SELECT * FROM rankings_fixes WHERE guild_id = ? AND id = ?", (interaction.guild.id, fix))
        if not rows:
            await interaction.response.send_message("❌ Pick a fix from the list.", ephemeral=True)
            return
        query("DELETE FROM rankings_fixes WHERE id = ?", (fix,), write=True)
        message = f"Removed: {fix_text(interaction.guild, rows[0])}."

    before = rankings_snapshot(interaction.guild.id)
    result = reread_last_sheet(interaction.guild)

    await interaction.response.send_message(
        f"✅ {message} The sheet decides again."
        + ("" if result is None else " Applied now."),
        ephemeral=True
    )

    if result is not None:
        await announce_ranking_changes(
            interaction.guild, before,
            f"↩️ A /rankingsedit fix was removed by {clean_name(interaction.user.display_name)}"
        )


bot.tree.add_command(rankings_edit_group)


# ---------- Rankings changes posted in the uni-bot channel ----------
#
# Every time the rankings change (sheet re-read, upload or /rankingsedit),
# the bot posts what moved in the channel named in RANKINGS_LOG_CHANNEL
# (default "uni-bot"; fancy letters and emojis in the name are fine, or
# use the channel ID). Set it to "off" to stop.

RANKINGS_LOG_CHANNEL = os.getenv("RANKINGS_LOG_CHANNEL", "uni-bot").strip()


def rankings_snapshot(
    guild_id: int
):
    """{(weight, user_id): row} of the rankings as they are now."""

    return {
        (row["division"].lower(), row["user_id"]): dict(row)
        for row in query("SELECT * FROM rankings WHERE guild_id = ?", (guild_id,))
    }


def ranking_label(
    guild,
    row
):
    """'Brandon Royval (Scarman)': the fighter, plus the member if it's a different name."""

    member = fighter_name(guild, row["user_id"], row["name"])
    fighter = row.get("fighter")

    if fighter and name_key(fighter) != name_key(member):
        return f"{fighter} ({member})"

    return fighter or member


def rank_label(
    rank
):

    return "NR" if rank is None else "C" if rank == 0 else f"#{rank}"


def ranking_changes(
    guild,
    before: dict,
    after: dict
):
    """{weight: [lines]} describing what moved. NR-only shuffles are left out."""

    changes = {}
    order = {}

    for row in after.values():
        order.setdefault(row["division"], row["division_order"])

    for row in before.values():
        order.setdefault(row["division"], 99 + row["division_order"])

    def add(weight, sort, line):
        changes.setdefault(weight, []).append((sort, line))

    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        row = new or old
        weight = row["division"]
        name = ranking_label(guild, row)
        old_rank = old["rank"] if old else None
        new_rank = new["rank"] if new else None

        if old_rank == new_rank:
            continue

        if new_rank == 0:
            add(weight, -1, f"👑 **{name}** is the new champion" + (f" (was {rank_label(old_rank)})" if old else ""))
        elif old_rank is None and new_rank is not None:
            add(weight, new_rank, f"🆕 **{name}** enters at {rank_label(new_rank)}")
        elif new_rank is None:
            add(weight, 100 + (old_rank or 0), f"⬇️ **{name}** drops out of the rankings (was {rank_label(old_rank)})"
                + ("" if new else f" and off the {weight} list"))
        elif old_rank == 0:
            add(weight, new_rank, f"🔻 **{name}** loses the title, now {rank_label(new_rank)}")
        elif new_rank < old_rank:
            add(weight, new_rank, f"🔺 **{name}** {rank_label(old_rank)} → {rank_label(new_rank)}")
        else:
            add(weight, new_rank, f"🔻 **{name}** {rank_label(old_rank)} → {rank_label(new_rank)}")

    return {
        weight: [line for _, line in sorted(lines)]
        for weight, lines in sorted(changes.items(), key=lambda item: order.get(item[0], 999))
    }


async def announce_ranking_changes(
    guild,
    before: dict,
    source: str
):
    """Posts what changed in the rankings log channel. Never raises."""

    try:
        if RANKINGS_LOG_CHANNEL.lower() in ("off", "none", "no", ""):
            return

        channel = find_named_channel(guild, RANKINGS_LOG_CHANNEL)

        if not channel:
            print(f"Rankings log: no channel matching '{RANKINGS_LOG_CHANNEL}' in {guild.name}")
            return

        after = rankings_snapshot(guild.id)

        if not before:
            weights = {row["division"] for row in after.values()}
            ranked = sum(1 for row in after.values() if row["rank"] is not None)

            if not after:
                return

            embed = discord.Embed(
                title="Rankings loaded",
                description=f"**{len(weights)}** weight{'s' if len(weights) != 1 else ''} · **{ranked}** ranked fighters\n\n{source}",
                color=BRAND_RED,
                timestamp=datetime.now(timezone.utc)
            )
            apply_branding(embed, author_text=f"{BRAND_TAG} • RANKINGS")
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return

        changes = ranking_changes(guild, before, after)

        if not changes:
            return

        embed = discord.Embed(
            title="Rankings update",
            description=source,
            color=BRAND_RED,
            timestamp=datetime.now(timezone.utc)
        )
        apply_branding(embed, author_text=f"{BRAND_TAG} • RANKINGS")

        for number, (weight, lines) in enumerate(changes.items()):
            if number == 24:
                embed.add_field(name="…", value=f"and changes in {len(changes) - 24} more weights", inline=False)
                break

            value = ""

            for line in lines:
                if len(value) + len(line) + 1 > 1000:
                    value += "\n…"
                    break
                value += ("\n" if value else "") + line

            embed.add_field(name=weight, value=value, inline=False)

        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    except Exception as error:
        print(f"Rankings log failed: {error}")


# ============================================================
# COINS
#
# Everyone starts with START_BALANCE coins. /coins (or /daily) gives a
# daily bonus that grows with a streak, and tops you up if you're
# nearly broke. Fighters are paid for every fight logged after coins
# were switched on, on their contract or the base rate (see FIGHT PAY
# below): by default 150 for a win (+100 for a KO/TKO or submission)
# and 25 for a loss.
# Every coin movement goes through coin_ledger, so it can be undone.
# ============================================================

COIN = os.getenv("UNI_COIN_EMOJI", "🪙").strip() or "🪙"

START_BALANCE = int(os.getenv("COINS_START", "1000"))
DAILY_BONUS = int(os.getenv("COINS_DAILY", "100"))
DAILY_STREAK_STEP = 10            # +10 per day in a row
DAILY_STREAK_MAX = 100            # up to +100
BROKE_TOP_UP = 250                # the daily tops you up to this if you're nearly out

FIGHT_PAY_WIN = int(os.getenv("FIGHT_PAY_WIN", "150"))
FIGHT_PAY_FINISH = int(os.getenv("FIGHT_PAY_FINISH", "100"))
FIGHT_PAY_LOSS = int(os.getenv("FIGHT_PAY_LOSS", "25"))


def setup_coin_tables():

    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS coin_wallets (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            balance INTEGER NOT NULL,
            daily_last TEXT,
            daily_streak INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS coin_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bet_markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            closes_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            channel_id INTEGER,
            message_id INTEGER,
            created_by INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bet_fights (
            market_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            fighter_a INTEGER NOT NULL,
            fighter_b INTEGER NOT NULL,
            name_a TEXT,
            name_b TEXT,
            division TEXT,
            open_pa REAL NOT NULL,
            manual INTEGER NOT NULL DEFAULT 0,
            title INTEGER NOT NULL DEFAULT 0,
            result TEXT,
            method TEXT,
            rnd INTEGER,
            fight_id INTEGER,
            PRIMARY KEY(market_id, position)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            stake INTEGER NOT NULL,
            odds REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            payout INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bet_legs (
            bet_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            pick TEXT NOT NULL,
            method TEXT,
            odds REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            fixed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(bet_id, position)
        )
    """)

    db.commit()
    db.close()

    # Fights before this moment are never paid (so importing old results
    # doesn't hand out a fortune).
    if not get_meta("coins_started_at"):
        set_meta("coins_started_at", now_utc())


def coins_text(
    amount: int
):

    return f"{int(amount):,}"


def get_wallet(
    guild_id: int,
    user_id: int
):
    """Creates the wallet (with the starting balance) the first time."""

    rows = query("SELECT * FROM coin_wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))

    if rows:
        return rows[0]

    query(
        "INSERT OR IGNORE INTO coin_wallets (guild_id, user_id, balance, daily_streak) VALUES (?, ?, ?, 0)",
        (guild_id, user_id, START_BALANCE), write=True
    )
    query(
        "INSERT INTO coin_ledger (guild_id, user_id, amount, reason, created_at) VALUES (?, ?, ?, 'start', ?)",
        (guild_id, user_id, START_BALANCE, now_utc()), write=True
    )

    return query("SELECT * FROM coin_wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))[0]


def add_coins(
    guild_id: int,
    user_id: int,
    amount: int,
    reason: str
):

    if not amount:
        return

    get_wallet(guild_id, user_id)

    query(
        "UPDATE coin_wallets SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
        (amount, guild_id, user_id), write=True
    )
    query(
        "INSERT INTO coin_ledger (guild_id, user_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, amount, reason, now_utc()), write=True
    )


def reverse_coins(
    guild_id: int,
    reason: str
):
    """Undoes every payment made for this reason (e.g. a fight that was removed)."""

    for row in query(
        "SELECT user_id, SUM(amount) AS total FROM coin_ledger WHERE guild_id = ? AND reason = ? GROUP BY user_id",
        (guild_id, reason)
    ):
        if row["total"]:
            add_coins(guild_id, row["user_id"], -row["total"], f"undo:{reason}")

    query(
        "UPDATE coin_ledger SET reason = ? WHERE guild_id = ? AND reason = ?",
        (f"undone:{reason}", guild_id, reason), write=True
    )


def uk_now():
    """Current UK local time (naive)."""

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London")).replace(tzinfo=None)
    except Exception:
        return now + timedelta(hours=uk_offset_hours(now))


def next_uk_midnight_unix():

    tomorrow = (uk_now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    return int(to_utc(tomorrow).timestamp())


def claim_daily(
    guild_id: int,
    user_id: int
):
    """
    Returns (claimed, amount, streak). Resets at midnight UK time.
    100 + 10 for each day in a row (up to +100), and never leaves you
    with less than BROKE_TOP_UP.
    """

    wallet = get_wallet(guild_id, user_id)
    today = uk_now().date()

    if wallet["daily_last"] == today.isoformat():
        return False, 0, wallet["daily_streak"]

    yesterday = (today - timedelta(days=1)).isoformat()
    streak = wallet["daily_streak"] + 1 if wallet["daily_last"] == yesterday else 1

    amount = DAILY_BONUS + min(DAILY_STREAK_STEP * (streak - 1), DAILY_STREAK_MAX)

    if wallet["balance"] + amount < BROKE_TOP_UP:
        amount = BROKE_TOP_UP - wallet["balance"]

    query(
        "UPDATE coin_wallets SET daily_last = ?, daily_streak = ? WHERE guild_id = ? AND user_id = ?",
        (today.isoformat(), streak, guild_id, user_id), write=True
    )
    add_coins(guild_id, user_id, amount, "daily")

    return True, amount, streak


def daily_ready(
    guild_id: int,
    user_id: int
):

    return get_wallet(guild_id, user_id)["daily_last"] != uk_now().date().isoformat()


def coin_rank(
    guild_id: int,
    user_id: int
):
    """(position, out of) by balance."""

    balance = get_wallet(guild_id, user_id)["balance"]

    above = query(
        "SELECT COUNT(*) AS n FROM coin_wallets WHERE guild_id = ? AND balance > ?",
        (guild_id, balance)
    )[0]["n"]
    total = query("SELECT COUNT(*) AS n FROM coin_wallets WHERE guild_id = ?", (guild_id,))[0]["n"]

    return above + 1, total


def fight_paid(
    guild_id: int,
    fight_id: int
):

    return bool(query(
        "SELECT 1 FROM coin_ledger WHERE guild_id = ? AND reason = ? LIMIT 1",
        (guild_id, f"fight:{fight_id}")
    ))


def pay_fighters_for_fight(
    guild_id: int,
    fight
):
    """
    Pays both fighters once per fight (their contract, or the base rate).
    Returns {user_id: amount} paid, or {} if it was already paid or
    happened before coins started.
    """

    started = get_meta("coins_started_at")

    if not fight or fight_paid(guild_id, fight["id"]):
        return {}

    if started and (fight["fought_at"] or "") < started[:10]:
        return {}

    paid = {}

    # Each fighter is paid on their own contract, or the base rate.
    for user_id in (fight["winner_id"], fight["loser_id"]):

        # Typed by name only (no Discord account): nothing to pay.
        if user_id < 0:
            paid[user_id] = 0
            continue

        rates, contract = fight_pay_for(guild_id, user_id, fight["id"])
        amount = rates["show"]

        if user_id == fight["winner_id"]:
            amount += rates["win"] + (rates["finish"] if fight["method"] in ("KO", "SUB") else 0)

        add_coins(guild_id, user_id, amount, f"fight:{fight['id']}")
        paid[user_id] = amount

        if contract:
            use_contract_fight(contract, fight["id"])

    return paid


def fight_pay_text(
    guild,
    fight,
    paid: dict
):

    if not paid:
        return None

    def tag(user_id):
        if user_id < 0:
            return " (no Discord account, not paid)"
        return " (contract)" if query(
            "SELECT 1 FROM contract_fights JOIN fight_contracts ON fight_contracts.id = contract_fights.contract_id "
            "WHERE contract_fights.fight_id = ? AND fight_contracts.user_id = ?",
            (fight["id"], user_id)
        ) else ""

    return (
        f"💰 Fight pay: {fighter_name(guild, fight['winner_id'], fight['winner_name'])} "
        f"+{coins_text(paid[fight['winner_id']])}{tag(fight['winner_id'])} · "
        f"{fighter_name(guild, fight['loser_id'], fight['loser_name'])} "
        f"+{coins_text(paid[fight['loser_id']])}{tag(fight['loser_id'])}"
    )


def coins_embed(
    guild,
    member,
    note: str = None
):

    wallet = get_wallet(guild.id, member.id)
    position, total = coin_rank(guild.id, member.id)

    staked = query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stake), 0) AS s FROM bets "
        "WHERE guild_id = ? AND user_id = ? AND status = 'open'",
        (guild.id, member.id)
    )[0]

    lines = []

    if note:
        lines += [note, ""]

    lines.append(f"{COIN} **{coins_text(wallet['balance'])}** coins")
    lines.append(f"#{position} of {total} in the server")

    if staked["n"]:
        lines.append(f"🧾 {staked['n']} open bet{'s' if staked['n'] != 1 else ''} · {coins_text(staked['s'])} staked")

    if daily_ready(guild.id, member.id):
        lines.append("🎁 Daily bonus ready")
    else:
        lines.append(f"🎁 Next daily <t:{next_uk_midnight_unix()}:R> · streak {wallet['daily_streak']}")

    embed = discord.Embed(
        title=clean_name(member.display_name),
        description="\n".join(lines),
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • COINS")
    embed.set_thumbnail(url=member.display_avatar.url)

    return embed


class CoinsView(
    discord.ui.View
):

    def __init__(
        self,
        user_id: int
    ):

        super().__init__(timeout=600)

        self.user_id = user_id

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Use `/coins` to see your own.", ephemeral=True)
            return False

        return True

    @discord.ui.button(
        label="Claim daily",
        emoji="🎁",
        style=discord.ButtonStyle.success
    )
    async def daily_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        claimed, amount, streak = claim_daily(interaction.guild.id, interaction.user.id)

        if claimed:
            note = f"🎁 **+{coins_text(amount)}** daily bonus · {streak} day{'s' if streak != 1 else ''} in a row"
        else:
            note = f"Already claimed today. Next one <t:{next_uk_midnight_unix()}:R>."

        button.disabled = True

        await interaction.response.edit_message(
            embed=coins_embed(interaction.guild, interaction.user, note),
            view=self
        )

    @discord.ui.button(
        label="My bets",
        emoji="🧾",
        style=discord.ButtonStyle.secondary
    )
    async def bets_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await send_my_bets(interaction)


@bot.tree.command(
    name="coins",
    description="Your coins (or someone else's), and your daily bonus."
)
@app_commands.describe(member="Whose coins to show (default: yours)")
async def coins(
    interaction: discord.Interaction,
    member: discord.Member = None
):

    member = member or interaction.user
    own = member.id == interaction.user.id
    embed = coins_embed(interaction.guild, member)

    if own:
        view = CoinsView(member.id)
        view.daily_button.disabled = not daily_ready(interaction.guild.id, member.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="daily",
    description="Claim your daily coins."
)
async def daily(
    interaction: discord.Interaction
):

    claimed, amount, streak = claim_daily(interaction.guild.id, interaction.user.id)

    if claimed:
        note = f"🎁 **+{coins_text(amount)}** daily bonus · {streak} day{'s' if streak != 1 else ''} in a row"
    else:
        note = f"Already claimed today. Next one <t:{next_uk_midnight_unix()}:R>."

    await interaction.response.send_message(embed=coins_embed(interaction.guild, interaction.user, note), ephemeral=True)


@bot.tree.command(
    name="coinboard",
    description="Who has the most coins."
)
async def coinboard(
    interaction: discord.Interaction
):

    rows = query(
        "SELECT * FROM coin_wallets WHERE guild_id = ? ORDER BY balance DESC LIMIT 10",
        (interaction.guild.id,)
    )

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    lines = [
        f"{medals.get(number, f'`{number:>2}`')} {fighter_name(interaction.guild, row['user_id'])} · "
        f"{COIN} **{coins_text(row['balance'])}**"
        for number, row in enumerate(rows, start=1)
    ]

    embed = discord.Embed(
        title="Richest in the Universe",
        description="\n".join(lines) or "Nobody has coins yet. Try `/coins`.",
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • COINS")

    if interaction.guild.get_member(interaction.user.id):
        position, total = coin_rank(interaction.guild.id, interaction.user.id)
        embed.set_footer(text=f"You: #{position} of {total}", icon_url=asset_url(LOGO_FILE))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="givecoins",
    description="Give (or take, with a minus number) coins."
)
@app_commands.describe(
    member="Who",
    amount="How many (use a minus number to take coins away)",
    reason="Why (shows in the log)"
)
async def givecoins(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, -100000, 100000],
    reason: app_commands.Range[str, 1, 100] = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can give coins.", ephemeral=True)
        return

    if not amount:
        await interaction.response.send_message("❌ Pick an amount that isn't 0.", ephemeral=True)
        return

    add_coins(interaction.guild.id, member.id, amount, f"staff:{interaction.user.id}:{reason or ''}"[:200])

    verb = "Gave" if amount > 0 else "Took"
    balance = get_wallet(interaction.guild.id, member.id)["balance"]

    await interaction.response.send_message(
        f"✅ {verb} {COIN} **{coins_text(abs(amount))}** "
        f"{'to' if amount > 0 else 'from'} {clean_name(member.display_name)}"
        + (f" ({reason})" if reason else "")
        + f". They now have {COIN} **{coins_text(balance)}**.",
        ephemeral=True
    )


# ============================================================
# FIGHT PAY: BASE RATE AND CONTRACTS (same as PFO)
#
# Every fight pays show money to both fighters (win or lose), plus win
# money (and a finish bonus for a KO/TKO or submission) to the winner.
# The base rate starts at 25 show + 125 win + 100 finish, so a win pays
# 150 (250 by finish) and a loss pays 25.
#
#   /payrates [show] [win] [finish]   see the base rate (Owner can change it)
#   /contract offer @fighter show win fights [finish]
#                                     staff: offer a contract. They get a DM to
#                                     accept or decline (7 days). Once signed it
#                                     pays instead of the base rate for that many fights.
#   /contract view [@fighter]         see a contract (yours, or anyone's for staff);
#                                     accept a waiting offer here too
#   /contract end @fighter            staff: end a contract or withdraw an offer
#   /contract list                    staff: every contract and waiting offer
# ============================================================

PAY_DEFAULTS = {
    "show": FIGHT_PAY_LOSS,
    "win": max(0, FIGHT_PAY_WIN - FIGHT_PAY_LOSS),
    "finish": FIGHT_PAY_FINISH
}

CONTRACT_OFFER_DAYS = 7

_contracts_finished = []


def setup_contract_tables():

    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS fight_contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            show_pay INTEGER NOT NULL,
            win_pay INTEGER NOT NULL,
            finish_pay INTEGER NOT NULL,
            fights INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'offered',
            offered_by INTEGER,
            offered_at INTEGER NOT NULL,
            answered_at INTEGER,
            ended_at INTEGER,
            dm_status TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS contract_fights (
            contract_id INTEGER NOT NULL,
            fight_id INTEGER NOT NULL,
            PRIMARY KEY(contract_id, fight_id)
        )
    """)

    db.commit()
    db.close()


def is_owner(
    interaction: discord.Interaction
):
    """The server owner, or anyone with a role called Owner."""

    if not interaction.guild:
        return False

    if interaction.user.id == interaction.guild.owner_id:
        return True

    return any(role_key(role.name) == "owner" for role in getattr(interaction.user, "roles", []))


def pay_rates(
    guild_id: int
):
    """The base rate: {'show', 'win', 'finish'}."""

    rates = dict(PAY_DEFAULTS)
    saved = get_meta(f"pay_rates:{guild_id}")

    if saved:
        try:
            rates.update({key: int(value) for key, value in json.loads(saved).items() if key in rates})
        except Exception as error:
            print(f"Pay rates couldn't be read: {error}")

    return rates


def get_contract(
    contract_id: int
):

    rows = query("SELECT * FROM fight_contracts WHERE id = ?", (contract_id,))

    return rows[0] if rows else None


def contract_used(
    contract_id: int
):

    return query("SELECT COUNT(*) AS n FROM contract_fights WHERE contract_id = ?", (contract_id,))[0]["n"]


def active_contract(
    guild_id: int,
    user_id: int
):

    rows = query(
        "SELECT * FROM fight_contracts WHERE guild_id = ? AND user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (guild_id, user_id)
    )

    return rows[0] if rows else None


def pending_contract(
    guild_id: int,
    user_id: int
):

    rows = query(
        "SELECT * FROM fight_contracts WHERE guild_id = ? AND user_id = ? AND status = 'offered' "
        "AND offered_at > ? ORDER BY id DESC LIMIT 1",
        (guild_id, user_id, int(time.time()) - CONTRACT_OFFER_DAYS * 86400)
    )

    return rows[0] if rows else None


def fight_pay_for(
    guild_id: int,
    user_id: int,
    fight_id: int = None
):
    """
    (rates, contract or None) that pay this fighter for this fight. A fight
    already counted on a contract keeps that contract.
    """

    if fight_id:
        rows = query("""
            SELECT fight_contracts.* FROM contract_fights
            JOIN fight_contracts ON fight_contracts.id = contract_fights.contract_id
            WHERE contract_fights.fight_id = ? AND fight_contracts.user_id = ? AND fight_contracts.guild_id = ?
        """, (fight_id, user_id, guild_id))

        if rows:
            c = rows[0]
            return {"show": c["show_pay"], "win": c["win_pay"], "finish": c["finish_pay"]}, c

    contract = active_contract(guild_id, user_id)

    if contract and contract_used(contract["id"]) < contract["fights"]:
        return {"show": contract["show_pay"], "win": contract["win_pay"], "finish": contract["finish_pay"]}, contract

    return pay_rates(guild_id), None


def use_contract_fight(
    contract,
    fight_id: int
):

    query("INSERT OR IGNORE INTO contract_fights (contract_id, fight_id) VALUES (?, ?)",
          (contract["id"], fight_id), write=True)

    if contract_used(contract["id"]) >= contract["fights"] and contract["status"] == "active":
        query("UPDATE fight_contracts SET status = 'done', ended_at = ? WHERE id = ?",
              (int(time.time()), contract["id"]), write=True)
        _contracts_finished.append(contract["id"])


def undo_contract_fight(
    fight_id: int
):
    """A fight was edited or removed: it no longer counts on anyone's contract."""

    for row in query("SELECT contract_id FROM contract_fights WHERE fight_id = ?", (fight_id,)):
        query("DELETE FROM contract_fights WHERE contract_id = ? AND fight_id = ?",
              (row["contract_id"], fight_id), write=True)
        query("UPDATE fight_contracts SET status = 'active', ended_at = NULL WHERE id = ? AND status = 'done'",
              (row["contract_id"],), write=True)


def contract_text(
    contract,
    used: int = None
):

    used = contract_used(contract["id"]) if used is None else used

    return (
        f"{COIN} **{coins_text(contract['show_pay'])}** show + **{coins_text(contract['win_pay'])}** to win"
        + (f" + **{coins_text(contract['finish_pay'])}** finish bonus" if contract["finish_pay"] else "")
        + f" · {used} of {contract['fights']} fight{'s' if contract['fights'] != 1 else ''} done"
    )


def contract_embed(
    guild,
    contract,
    note: str = None
):

    base = pay_rates(contract["guild_id"])
    used = contract_used(contract["id"])

    status = {
        "offered": f"Waiting for an answer · the offer ends <t:{contract['offered_at'] + CONTRACT_OFFER_DAYS * 86400}:R>",
        "active": "✅ Active",
        "declined": "❌ Declined",
        "withdrawn": "↩️ Withdrawn by staff",
        "ended": "🛑 Ended by staff",
        "done": "🏁 All fights done. Back on the base rate.",
        "expired": "⌛ The offer ran out",
        "replaced": "🔁 Replaced by a newer contract"
    }.get(contract["status"], contract["status"])

    embed = discord.Embed(
        title="📝 Fight contract",
        description=((note + "\n\n") if note else "") + status,
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • CONTRACT")

    win_total = contract["show_pay"] + contract["win_pay"]

    embed.add_field(name="Fighter", value=fighter_name(guild, contract["user_id"]), inline=True)
    embed.add_field(
        name="Fights",
        value=f"{used} of {contract['fights']} done" if contract["status"] in ("active", "done") else str(contract["fights"]),
        inline=True
    )
    embed.add_field(name="Show (win or lose)", value=f"{COIN} {coins_text(contract['show_pay'])}", inline=True)
    embed.add_field(name="Win", value=f"{COIN} {coins_text(contract['win_pay'])}", inline=True)
    embed.add_field(name="Finish bonus", value=f"{COIN} {coins_text(contract['finish_pay'])}", inline=True)
    embed.add_field(
        name="A win pays",
        value=f"{COIN} {coins_text(win_total)}"
        + (f" ({coins_text(win_total + contract['finish_pay'])} by finish)" if contract["finish_pay"] else ""),
        inline=True
    )

    embed.set_footer(
        text=f"Base rate: {coins_text(base['show'])} show + {coins_text(base['win'])} win + "
        f"{coins_text(base['finish'])} finish. The contract pays instead of it for these fights.",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


class ContractView(
    discord.ui.View
):
    """Accept / Decline on a contract offer. Keeps working after restarts (and in DMs)."""

    def __init__(
        self,
        contract_id: int
    ):

        super().__init__(timeout=None)

        self.contract_id = contract_id

        yes = discord.ui.Button(label="Accept", emoji="✍️", style=discord.ButtonStyle.success,
                                custom_id=f"uni_contract_{contract_id}_yes")
        yes.callback = self.accept
        self.add_item(yes)

        no = discord.ui.Button(label="Decline", style=discord.ButtonStyle.danger,
                               custom_id=f"uni_contract_{contract_id}_no")
        no.callback = self.decline
        self.add_item(no)

    async def answer(
        self,
        interaction: discord.Interaction,
        accept: bool
    ):

        contract = get_contract(self.contract_id)

        if not contract or interaction.user.id != contract["user_id"]:
            await interaction.response.send_message("❌ This contract isn't yours.", ephemeral=True)
            return

        guild = bot.get_guild(contract["guild_id"])

        if contract["status"] != "offered":
            await interaction.response.edit_message(embed=contract_embed(guild, contract), view=None)
            return

        now = int(time.time())

        if now - contract["offered_at"] > CONTRACT_OFFER_DAYS * 86400:
            query("UPDATE fight_contracts SET status = 'expired' WHERE id = ?", (contract["id"],), write=True)
            await interaction.response.edit_message(
                embed=contract_embed(guild, get_contract(contract["id"]), "This offer ran out. Ask staff for a new one."),
                view=None
            )
            return

        if accept:
            for old in query(
                "SELECT id FROM fight_contracts WHERE guild_id = ? AND user_id = ? AND status = 'active'",
                (contract["guild_id"], contract["user_id"])
            ):
                query("UPDATE fight_contracts SET status = 'replaced', ended_at = ? WHERE id = ?",
                      (now, old["id"]), write=True)

            query("UPDATE fight_contracts SET status = 'active', answered_at = ? WHERE id = ?",
                  (now, contract["id"]), write=True)
            note = "✍️ Signed. Your next fights pay on this contract."
        else:
            query("UPDATE fight_contracts SET status = 'declined', answered_at = ? WHERE id = ?",
                  (now, contract["id"]), write=True)
            note = "You declined. You stay on your current pay."

        await interaction.response.edit_message(embed=contract_embed(guild, get_contract(contract["id"]), note), view=None)

    async def accept(
        self,
        interaction: discord.Interaction
    ):

        await self.answer(interaction, True)

    async def decline(
        self,
        interaction: discord.Interaction
    ):

        await self.answer(interaction, False)


def restore_contract_buttons():

    rows = query("SELECT id FROM fight_contracts WHERE status = 'offered'")

    for row in rows:
        try:
            bot.add_view(ContractView(row["id"]))
        except Exception as error:
            print(f"Could not restore contract {row['id']}: {error}")

    print(f"Loaded {len(rows)} contract offer(s).")


def finished_contracts_note(
    guild
):
    """'🏁 Matt Hughes has finished their contract (3 fights)...' for contracts that just ended."""

    notes = []

    while _contracts_finished:
        contract = get_contract(_contracts_finished.pop())
        if contract and contract["guild_id"] == guild.id:
            notes.append(
                f"🏁 {fighter_name(guild, contract['user_id'])} has fought all {contract['fights']} "
                "fights on their contract and is back on the base rate."
            )

    return "\n".join(notes) or None


contract_group = app_commands.Group(
    name="contract",
    description="Fight contracts: pay that replaces the base rate for a number of fights."
)


@contract_group.command(
    name="offer",
    description="Offer a fighter a contract. They get a DM to accept or decline."
)
@app_commands.describe(
    fighter="Who it's for",
    show="Show money, paid win or lose",
    win="Win money, paid on top of show for a win",
    fights="How many fights it lasts",
    finish="Finish bonus for a KO/TKO or submission win (default: the base rate)"
)
async def contract_offer(
    interaction: discord.Interaction,
    fighter: discord.Member,
    show: app_commands.Range[int, 0, 100000],
    win: app_commands.Range[int, 0, 100000],
    fights: app_commands.Range[int, 1, 50],
    finish: app_commands.Range[int, 0, 100000] = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can offer contracts.", ephemeral=True)
        return

    if fighter.bot:
        await interaction.response.send_message("❌ That's a bot.", ephemeral=True)
        return

    guild = interaction.guild
    finish = pay_rates(guild.id)["finish"] if finish is None else finish
    now = int(time.time())

    # A new offer replaces any offer they haven't answered yet.
    for old in query(
        "SELECT id FROM fight_contracts WHERE guild_id = ? AND user_id = ? AND status = 'offered'",
        (guild.id, fighter.id)
    ):
        query("UPDATE fight_contracts SET status = 'withdrawn', ended_at = ? WHERE id = ?", (now, old["id"]), write=True)

    contract_id = query(
        "INSERT INTO fight_contracts (guild_id, user_id, show_pay, win_pay, finish_pay, fights, offered_by, offered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild.id, fighter.id, show, win, finish, fights, interaction.user.id, now),
        write=True
    )

    contract = get_contract(contract_id)

    await interaction.response.defer(ephemeral=True)

    note = (
        f"📝 **You've been offered a fight contract in {guild.name}.** "
        "Accept it to be paid this for your next fights instead of the base rate."
    )

    current = active_contract(guild.id, fighter.id)

    if current:
        note += f"\n-# Accepting replaces your current contract ({contract_used(current['id'])} of {current['fights']} fights done)."

    name = clean_name(fighter.display_name)

    try:
        await fighter.send(embed=contract_embed(guild, contract, note), view=ContractView(contract_id))
        query("UPDATE fight_contracts SET dm_status = 'sent' WHERE id = ?", (contract_id,), write=True)
        sent = f"📨 Sent to {name} to accept or decline."
    except Exception:
        query("UPDATE fight_contracts SET dm_status = 'failed' WHERE id = ?", (contract_id,), write=True)
        sent = f"⚠️ Couldn't DM {name} (DMs closed). They can accept it with `/contract view`."

    await interaction.followup.send(f"{sent}\n{contract_text(contract, 0)}", ephemeral=True)


@contract_group.command(
    name="view",
    description="See a contract (yours if empty). Accept a waiting offer here too."
)
@app_commands.describe(fighter="Whose contract (staff can look at anyone's)")
async def contract_view(
    interaction: discord.Interaction,
    fighter: discord.Member = None
):

    target = fighter or interaction.user

    if target.id != interaction.user.id and not is_staff(interaction):
        await interaction.response.send_message("❌ You can only see your own contract.", ephemeral=True)
        return

    guild = interaction.guild
    offer = pending_contract(guild.id, target.id)
    current = active_contract(guild.id, target.id)
    base = pay_rates(guild.id)

    if offer and target.id == interaction.user.id:
        await interaction.response.send_message(
            embed=contract_embed(guild, offer, "📝 You have a contract offer waiting."),
            view=ContractView(offer["id"]),
            ephemeral=True
        )
        return

    if current:
        await interaction.response.send_message(
            embed=contract_embed(guild, current, "📝 Offer waiting for them too." if offer else None),
            ephemeral=True
        )
        return

    who = "You are" if target.id == interaction.user.id else f"{clean_name(target.display_name)} is"

    await interaction.response.send_message(
        f"{who} on the base rate: {COIN} **{coins_text(base['show'])}** show + **{coins_text(base['win'])}** "
        f"to win + **{coins_text(base['finish'])}** finish bonus."
        + ("\n-# They have an offer waiting." if offer else ""),
        ephemeral=True
    )


@contract_group.command(
    name="end",
    description="End a fighter's contract, or withdraw an offer."
)
@app_commands.describe(fighter="Whose contract")
async def contract_end(
    interaction: discord.Interaction,
    fighter: discord.Member
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can end contracts.", ephemeral=True)
        return

    now = int(time.time())
    changed = []

    for row in query(
        "SELECT * FROM fight_contracts WHERE guild_id = ? AND user_id = ? AND status IN ('offered', 'active')",
        (interaction.guild.id, fighter.id)
    ):
        status = "withdrawn" if row["status"] == "offered" else "ended"
        query("UPDATE fight_contracts SET status = ?, ended_at = ? WHERE id = ?", (status, now, row["id"]), write=True)
        changed.append("offer withdrawn" if status == "withdrawn" else "contract ended")

    name = clean_name(fighter.display_name)

    if not changed:
        await interaction.response.send_message(f"{name} has no contract or offer.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"✅ {name}: {', '.join(changed)}. They're on the base rate.",
        ephemeral=True
    )


@contract_group.command(
    name="list",
    description="Every active contract and waiting offer."
)
async def contract_list(
    interaction: discord.Interaction
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can list contracts.", ephemeral=True)
        return

    guild = interaction.guild
    rows = query(
        "SELECT * FROM fight_contracts WHERE guild_id = ? AND status IN ('active', 'offered') ORDER BY status, id DESC",
        (guild.id,)
    )
    base = pay_rates(guild.id)

    lines = [
        f"**Base rate:** {coins_text(base['show'])} show + {coins_text(base['win'])} win + {coins_text(base['finish'])} finish",
        ""
    ]

    for row in rows[:40]:
        tag = "✅" if row["status"] == "active" else "⏳ offer"
        lines.append(f"{tag} **{fighter_name(guild, row['user_id'])}** · {contract_text(row)}")

    if not rows:
        lines.append("No contracts yet. Offer one with `/contract offer`.")

    embed = discord.Embed(title="📝 Contracts", description="\n".join(lines)[:4000], color=BRAND_RED)
    apply_branding(embed, author_text=f"{BRAND_TAG} • CONTRACTS")

    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(contract_group)


@bot.tree.command(
    name="payrates",
    description="See the base fight pay everyone gets (the Owner can change it)."
)
@app_commands.describe(
    show="Show money, paid to both fighters win or lose",
    win="Win money, on top of show",
    finish="Finish bonus for a KO/TKO or submission win"
)
async def payrates(
    interaction: discord.Interaction,
    show: app_commands.Range[int, 0, 100000] = None,
    win: app_commands.Range[int, 0, 100000] = None,
    finish: app_commands.Range[int, 0, 100000] = None
):

    rates = pay_rates(interaction.guild.id)

    if show is None and win is None and finish is None:
        await interaction.response.send_message(
            f"**Base fight pay:** {COIN} {coins_text(rates['show'])} show (win or lose) + {coins_text(rates['win'])} to win "
            f"+ {coins_text(rates['finish'])} finish bonus.\n"
            f"A win pays **{coins_text(rates['show'] + rates['win'])}** "
            f"({coins_text(rates['show'] + rates['win'] + rates['finish'])} by finish). A loss pays **{coins_text(rates['show'])}**.\n"
            "-# Contracts (`/contract offer`) pay instead of this for a set number of fights.",
            ephemeral=True
        )
        return

    if not is_owner(interaction):
        await interaction.response.send_message("❌ Only the Owner can change the base rate.", ephemeral=True)
        return

    for key, value in (("show", show), ("win", win), ("finish", finish)):
        if value is not None:
            rates[key] = value

    set_meta(f"pay_rates:{interaction.guild.id}", json.dumps(rates))

    await interaction.response.send_message(
        f"✅ Base fight pay is now {COIN} **{coins_text(rates['show'])}** show + **{coins_text(rates['win'])}** win + "
        f"**{coins_text(rates['finish'])}** finish. It counts from the next result logged.",
        ephemeral=True
    )


# ============================================================
# SPORTSBOOK
#
# /openbets posts a card with live odds. Odds open from the rankings
# spreadsheet and the fight log (or from odds staff type in), then move
# with the money. Everything is kept sensible the same way as PFO:
#   - the winner pick is always priced between 15% and 85%
#   - fighters with under NEW_FIGHTER_FIGHTS logged fights lean to 50/50
#   - a 5% house edge on every price
#   - method picks capped at 15x (6x for new fighters), round picks at
#     12x (8x), and any bet (accumulators too) at MAX_BET_ODDS x the stake
#   - 10 to 500 coins a bet, no betting on your own fight
#   - single bets can be refunded within BET_REFUND_MINUTES while betting
#     is open; accumulators can be cashed out at 90% of their value once
#     a pick has won
# Bets settle by themselves when the result is logged (/logfight or
# /scanresults), and redo themselves if it's edited or removed.
# ============================================================

MIN_BET = 10
MAX_BET = int(os.getenv("COINS_MAX_BET", "500"))

ODDS_PRIOR = 2500                 # how much the opening odds weigh against real bets
HOUSE_EDGE = 0.95
MIN_FIGHT_CHANCE = 0.15           # winner picks: never longer than this chance...
MAX_FIGHT_CHANCE = 0.85           # ...or shorter than this one

METHOD_SHARE = {"KO": 0.45, "SUB": 0.25, "DEC": 0.30}
METHOD_PRIOR_FIGHTS = 4           # "average" fights mixed in, so 1-2 fights don't swing it
PICK_NAMES = {
    "KO": "by KO/TKO",
    "SUB": "by Submission",
    "DEC": "by Decision",
    "R1": "to finish in Round 1",
    "R2": "to finish in Round 2",
    "R3": "to finish in Round 3"
}
PICK_SHORT = {"KO": "KO/TKO", "SUB": "SUB", "DEC": "DEC", "R1": "R1 finish", "R2": "R2 finish", "R3": "R3 finish"}

MAX_BET_ODDS = float(os.getenv("MAX_BET_ODDS", "50") or 50)
NEW_FIGHTER_FIGHTS = int(os.getenv("NEW_FIGHTER_FIGHTS", "3") or 3)
MAX_METHOD_ODDS = 15.0
NEW_FIGHTER_METHOD_ODDS = 6.0

ROUND_PICKS = {"R1": 1, "R2": 2, "R3": 3}
ROUND_SHARE = {1: 0.45, 2: 0.32, 3: 0.23}
ROUND_PRIOR_FIGHTS = 10
MAX_ROUND_ODDS = 12.0
NEW_FIGHTER_ROUND_ODDS = 8.0
MIN_ROUND_SHARE = 0.06

BET_REFUND_MINUTES = int(os.getenv("BET_REFUND_MINUTES", "60"))
CASHOUT_MARGIN = 0.9
CASHOUT_MIN = 10

STAKE_BUTTONS = [50, 100, 250, 500]

_markets_to_refresh = set()


# ---------- odds ----------

def fights_on_record(
    guild_id: int,
    user_id: int
):

    return query(
        "SELECT COUNT(*) AS n FROM fight_results WHERE guild_id = ? AND (winner_id = ? OR loser_id = ?)",
        (guild_id, user_id, user_id)
    )[0]["n"]


def fighter_strength(
    guild_id: int,
    user_id: int,
    division: str = None
):
    """
    Rough rating for opening odds (same as PFO). The rankings sheet matters
    most (champion vs #1 should be close), then win rate (eased towards 50%
    with only a few fights), then the current streak.
    """

    rank = rank_in_division(guild_id, user_id, division) if division else best_rank(guild_id, user_id)

    # Champion 95, #1 90 ... #15 34, unranked 25.
    if rank is None:
        score = 25
    elif rank == 0:
        score = 95
    else:
        score = max(30, 90 - 4 * (rank - 1))

    record = fighter_record(guild_id, user_id)
    fights = record["wins"] + record["losses"]
    win_rate = (record["wins"] + 2) / (fights + 4)
    score += (win_rate - 0.5) * 50

    streak = record["streak"]

    if len(streak) > 1:
        score += (2.5 if streak[0] == "W" else -2.5) * min(4, int(streak[1:]))

    return score


def clamp_chance(
    p: float
):

    return min(MAX_FIGHT_CHANCE, max(MIN_FIGHT_CHANCE, p))


def opening_probability(
    guild_id: int,
    a: int,
    b: int,
    division: str = None
):
    """Chance fighter A wins, from rankings and the fight log."""

    gap = fighter_strength(guild_id, b, division) - fighter_strength(guild_id, a, division)
    p = 1 / (1 + 10 ** (gap / 50))

    # Not much history yet: pull it towards 50/50 so new fighters don't open at huge odds.
    experience = min(fights_on_record(guild_id, a), fights_on_record(guild_id, b))

    if experience < NEW_FIGHTER_FIGHTS:
        trust = 0.4 + 0.6 * experience / NEW_FIGHTER_FIGHTS
        p = 0.5 + (p - 0.5) * trust

    return clamp_chance(p)


def manual_probability(
    odds_a: float,
    odds_b: float
):
    """Staff odds like 1.60 / 2.30 → chance A wins (kept inside the same 15-85% limits)."""

    a, b = 1 / odds_a, 1 / odds_b

    return clamp_chance(a / (a + b))


def fight_pools(
    market_id: int,
    position: int
):
    """Coins bet on each side of a fight (accumulator legs count too)."""

    pools = {"a": 0, "b": 0}

    for row in query("""
        SELECT bet_legs.pick, SUM(bets.stake) AS total FROM bet_legs
        JOIN bets ON bets.id = bet_legs.bet_id
        WHERE bets.market_id = ? AND bet_legs.position = ? AND bet_legs.fixed = 0
        GROUP BY bet_legs.pick
    """, (market_id, position)):
        pools[row["pick"]] = row["total"] or 0

    return pools


def to_odds(
    p: float
):

    return max(1.05, round(HOUSE_EDGE / max(p, 0.01), 2))


def capped_odds(
    odds: float
):
    """Total odds for a bet, never above MAX_BET_ODDS."""

    return round(min(odds, MAX_BET_ODDS), 2)


def live_odds(
    bout
):
    """{'a': 1.45, 'b': 2.80, 'open_a': ..., 'open_b': ..., 'pool': total}"""

    pools = fight_pools(bout["market_id"], bout["position"])
    p = (ODDS_PRIOR * bout["open_pa"] + pools["a"]) / (ODDS_PRIOR + pools["a"] + pools["b"])

    return {
        "a": to_odds(p),
        "b": to_odds(1 - p),
        "open_a": to_odds(bout["open_pa"]),
        "open_b": to_odds(1 - bout["open_pa"]),
        "pool": pools["a"] + pools["b"]
    }


def league_method_shares(
    guild_id: int
):
    """How wins end across the whole server (a sensible default until there are 10+ fights)."""

    counts = {method: 0 for method in METHOD_SHARE}

    for row in query("SELECT method, COUNT(*) AS n FROM fight_results WHERE guild_id = ? GROUP BY method", (guild_id,)):
        if row["method"] in counts:
            counts[row["method"]] = row["n"]

    total = sum(counts.values())

    if total < 10:
        return dict(METHOD_SHARE)

    return {method: counts[method] / total for method in counts}


def method_shares(
    guild_id: int,
    winner_id: int,
    loser_id: int
):
    """
    Chance of each method if this fighter wins: how THEY win and how their
    OPPONENT loses, mixed with the server average.
    """

    league = league_method_shares(guild_id)
    counts = {method: league[method] * METHOD_PRIOR_FIGHTS for method in league}

    wins_by = fighter_record(guild_id, winner_id)["wins_by"]
    losses_by = fighter_record(guild_id, loser_id)["losses_by"]

    for method in counts:
        counts[method] += wins_by.get(method, 0) + losses_by.get(method, 0)

    total = sum(counts.values())
    shares = {method: max(0.08, counts[method] / total) for method in counts}
    scale = sum(shares.values())

    return {method: share / scale for method, share in shares.items()}


def round_shares(
    guild_id: int
):
    """Share of server finishes (KO/TKO or SUB) in each of rounds 1-3."""

    counts = {rnd: share * ROUND_PRIOR_FIGHTS for rnd, share in ROUND_SHARE.items()}
    extra = 0

    for row in query(
        "SELECT round, COUNT(*) AS n FROM fight_results WHERE guild_id = ? AND method IN ('KO', 'SUB') "
        "AND round IS NOT NULL GROUP BY round",
        (guild_id,)
    ):
        if row["round"] in counts:
            counts[row["round"]] += row["n"]
        elif row["round"] and row["round"] > 3:
            extra += row["n"]

    total = sum(counts.values()) + extra

    return {rnd: counts[rnd] / total for rnd in counts}


def bout_sides(
    bout,
    pick: str
):

    return (bout["fighter_a"], bout["fighter_b"]) if pick == "a" else (bout["fighter_b"], bout["fighter_a"])


def pick_odds(
    bout,
    pick: str,
    method: str = None
):

    odds = live_odds(bout)[pick]

    if not method:
        return odds

    market = get_market(bout["market_id"])
    guild_id = market["guild_id"]
    winner, loser = bout_sides(bout, pick)
    new_fighters = min(fights_on_record(guild_id, winner), fights_on_record(guild_id, loser)) < NEW_FIGHTER_FIGHTS
    methods = method_shares(guild_id, winner, loser)

    if method in ROUND_PICKS:
        share = (methods["KO"] + methods["SUB"]) * round_shares(guild_id)[ROUND_PICKS[method]]
        cap = MAX_ROUND_ODDS

        if new_fighters:
            share = max(share, 0.18)            # no real stats yet: keep round odds tame
            cap = NEW_FIGHTER_ROUND_ODDS

        share = max(share, MIN_ROUND_SHARE)

        # Always pays more than just picking the winner, never more than the cap.
        return max(round(odds * 1.5, 2), min(cap, round(odds / share, 2)))

    share = methods[method]
    cap = MAX_METHOD_ODDS

    if new_fighters:
        share = max(share, 0.25)                # no real stats yet: no long shots on a method
        cap = NEW_FIGHTER_METHOD_ODDS

    return max(odds, min(cap, round(odds / share, 2)))


# ---------- markets ----------

def get_market(
    market_id: int
):

    rows = query("SELECT * FROM bet_markets WHERE id = ?", (market_id,))

    return rows[0] if rows else None


def market_bouts(
    market_id: int
):

    return query("SELECT * FROM bet_fights WHERE market_id = ? ORDER BY position", (market_id,))


def get_bout(
    market_id: int,
    position: int
):

    rows = query("SELECT * FROM bet_fights WHERE market_id = ? AND position = ?", (market_id, position))

    return rows[0] if rows else None


def market_is_open(
    market
):

    return bool(market) and market["status"] == "open" and time.time() < market["closes_at"]


def bout_label(
    position: int
):

    return {1: "Main Event", 2: "Co-Main Event"}.get(position, f"Fight {position}")


def side_name(
    guild,
    bout,
    pick: str
):

    if pick == "a":
        return fighter_name(guild, bout["fighter_a"], bout["name_a"])

    return fighter_name(guild, bout["fighter_b"], bout["name_b"])


def pick_label(
    guild,
    bout,
    pick: str,
    method: str = None
):

    name = side_name(guild, bout, pick)

    return f"{name} {PICK_NAMES[method]}" if method else name


def form_text(
    guild_id: int,
    user_id: int,
    size: int = 5
):
    """'W W L W W' (oldest to newest), or 'no fights yet'."""

    fights = fighter_record(guild_id, user_id)["fights"][-size:]

    return " ".join("W" if f["winner_id"] == user_id else "L" for f in fights) or "no fights yet"


def fighter_tag(
    guild_id: int,
    user_id: int,
    division: str = None
):
    """'5-0 · W W W W W' (rankings stay in the owner's sheet, so they aren't shown)"""

    record = fighter_record(guild_id, user_id)
    bits = [f"{record['wins']}-{record['losses']}", form_text(guild_id, user_id)]

    return " · ".join(bit for bit in bits if bit)


def arrow(
    now: float,
    opening: float
):

    if now < opening - 0.01:
        return " 🔻"
    if now > opening + 0.01:
        return " 🔺"

    return ""


def sportsbook_embed(
    market,
    guild
):

    bouts = market_bouts(market["id"])
    open_now = market_is_open(market)

    if open_now:
        status = f"Betting closes <t:{market['closes_at']}:F> (<t:{market['closes_at']}:R>)"
    elif market["status"] == "settled":
        status = "🏁 Every fight is settled"
    else:
        status = "🔒 Betting has closed"

    def build(with_form: bool):

        lines = [status, ""]

        for bout in bouts:
            name_a = side_name(guild, bout, "a")
            name_b = side_name(guild, bout, "b")
            head = f"**{bout_label(bout['position'])}**" + (" · 🏆 Title fight" if bout["title"] else "")
            lines.append(head)

            if bout["result"] == "void":
                lines.append(f"{name_a} vs {name_b} · ↩️ void, bets refunded")
            elif bout["result"]:
                winner, loser = (name_a, name_b) if bout["result"] == "a" else (name_b, name_a)
                how = (f"R{bout['rnd']} " if bout["rnd"] and bout["method"] != "DEC" else "") + (bout["method"] or "")
                lines.append(f"✅ **{winner}** def. {loser} · {how}")
            else:
                odds = live_odds(bout)
                lines.append(
                    f"{name_a} `{odds['a']:.2f}`{arrow(odds['a'], odds['open_a'])} vs "
                    f"{name_b} `{odds['b']:.2f}`{arrow(odds['b'], odds['open_b'])}"
                    + (" · odds set by staff" if bout["manual"] else "")
                )

            if with_form and not bout["result"]:
                lines.append(
                    f"-# {fighter_tag(guild.id, bout['fighter_a'], bout['division'])} | "
                    f"{fighter_tag(guild.id, bout['fighter_b'], bout['division'])}"
                )

            lines.append("")

        if open_now:
            lines.append("-# Odds move as people bet. Yours lock when you place it.")

        return "\n".join(lines).strip()

    description = build(True)

    if len(description) > 4000:
        description = build(False)[:4000]

    totals = query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stake), 0) AS s FROM bets WHERE market_id = ? AND status != 'void'",
        (market["id"],)
    )[0]

    embed = discord.Embed(
        title=market["event"],
        description=description,
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • SPORTSBOOK")

    embed.set_footer(
        text=f"{coins_text(totals['s'])} coins bet · {totals['n']} bet{'s' if totals['n'] != 1 else ''} · "
        f"Bets {MIN_BET}–{MAX_BET} coins",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


async def refresh_sportsbook(
    market_id: int
):

    market = get_market(market_id)

    if not market or not market["channel_id"] or not market["message_id"]:
        return

    guild = bot.get_guild(market["guild_id"])
    channel = bot.get_channel(market["channel_id"])

    if not guild or not channel:
        return

    try:
        await channel.get_partial_message(market["message_id"]).edit(
            embed=sportsbook_embed(market, guild),
            view=SportsbookView(market_id)
        )
    except discord.NotFound:
        query("UPDATE bet_markets SET message_id = NULL WHERE id = ?", (market_id,), write=True)
    except Exception as error:
        print(f"Sportsbook refresh {market_id} failed: {error}")


async def refresh_waiting_sportsbooks():
    """Close markets whose time is up, then redraw any whose odds changed."""

    for market in query("SELECT * FROM bet_markets WHERE status = 'open' AND closes_at <= ?", (int(time.time()),)):
        query("UPDATE bet_markets SET status = 'locked' WHERE id = ?", (market["id"],), write=True)
        _markets_to_refresh.add(market["id"])

    while _markets_to_refresh:
        await refresh_sportsbook(_markets_to_refresh.pop())


def parse_fight_pairs(
    text: str
):
    """'@Joe @Dan @Tom @Ben' → [(Joe, Dan), (Tom, Ben)]. An odd one out pairs with None."""

    ids = [int(found) for found in MENTION_PATTERN.findall(text or "")]

    return [(ids[i], ids[i + 1] if i + 1 < len(ids) else None) for i in range(0, len(ids), 2)]


def parse_odds_list(
    text: str,
    count: int
):
    """
    'auto, 1.60/2.30' → [None, (1.6, 2.3)]. Missing entries are auto.
    Raises ValueError with a message for anything it can't read.
    """

    if not (text or "").strip():
        return [None] * count

    parts = [part.strip() for part in re.split(r"[,;\n]", text)]

    if len(parts) > count:
        raise ValueError(f"You gave odds for {len(parts)} fights but there are only {count}.")

    result = []

    for number, part in enumerate(parts, start=1):

        if not part or part.lower() in ("auto", "a", "-"):
            result.append(None)
            continue

        found = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:/|\s|v|vs)\s*(\d+(?:\.\d+)?)", part.lower())

        if not found:
            raise ValueError(f"Fight {number}: write odds like `1.60/2.30` (or `auto`).")

        a, b = float(found.group(1)), float(found.group(2))

        if a <= 1 or b <= 1:
            raise ValueError(f"Fight {number}: odds have to be above 1.00.")

        result.append((a, b))

    return result + [None] * (count - len(result))


def parse_positions(
    text: str
):
    """'1, 3' → {1, 3}"""

    return {int(n) for n in re.findall(r"\d+", text or "")}


# ---------- bets ----------

def place_bet(
    guild,
    user_id: int,
    market_id: int,
    legs: dict,
    stake: int
):
    """legs: {position: (pick 'a'/'b', method or None)}. Returns (ok, message, bet_id)."""

    market = get_market(market_id)

    if not market or market["guild_id"] != guild.id:
        return False, "❌ That betting event doesn't exist.", None

    if not market_is_open(market):
        return False, "🔒 Betting has closed for this event.", None

    bouts = {bout["position"]: bout for bout in market_bouts(market_id) if not bout["result"]}
    legs = {position: leg for position, leg in legs.items() if position in bouts}

    if not legs:
        return False, "❌ Pick at least one fight that hasn't finished.", None

    for position, (pick, method) in legs.items():
        if pick not in ("a", "b") or (method and method not in PICK_NAMES):
            return False, "❌ That pick isn't valid.", None
        if user_id in (bouts[position]["fighter_a"], bouts[position]["fighter_b"]):
            return False, "❌ You can't bet on your own fight. Take that pick off your slip.", None

    if not MIN_BET <= stake <= MAX_BET:
        return False, f"❌ Bets must be between {MIN_BET} and {coins_text(MAX_BET)} coins.", None

    balance = get_wallet(guild.id, user_id)["balance"]

    if balance < stake:
        return False, f"❌ You only have {COIN} **{coins_text(balance)}**. Lower your stake.", None

    odds_each = {position: pick_odds(bouts[position], pick, method) for position, (pick, method) in legs.items()}
    total = 1.0

    for value in odds_each.values():
        total *= value

    total = capped_odds(total)

    bet_id = query(
        "INSERT INTO bets (market_id, guild_id, user_id, stake, odds, status, payout, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', 0, ?)",
        (market_id, guild.id, user_id, stake, total, now_utc()), write=True
    )

    for position, (pick, method) in legs.items():
        query(
            "INSERT INTO bet_legs (bet_id, position, pick, method, odds, status) VALUES (?, ?, ?, ?, ?, 'pending')",
            (bet_id, position, pick, method, odds_each[position]), write=True
        )

    add_coins(guild.id, user_id, -stake, f"stake:{bet_id}")
    _markets_to_refresh.add(market_id)

    first = next(iter(legs))
    summary = pick_label(guild, bouts[first], *legs[first]) if len(legs) == 1 else f"a {len(legs)}-pick accumulator"

    return (
        True,
        f"✅ **Bet placed:** {COIN} {coins_text(stake)} on {summary} @ **{total:.2f}** "
        f"→ returns {COIN} **{coins_text(round(stake * total))}**",
        bet_id
    )


def evaluate_bet(
    guild_id: int,
    bet_id: int
):
    """Works out a bet from its legs and pays (or takes back) the difference."""

    bet = query("SELECT * FROM bets WHERE id = ?", (bet_id,))[0]

    if bet["status"] == "cashed":
        return

    legs = query("SELECT * FROM bet_legs WHERE bet_id = ?", (bet_id,))
    statuses = [leg["status"] for leg in legs]

    if "lost" in statuses:
        status, payout = "lost", 0
    elif "pending" in statuses:
        status, payout = "open", 0
    elif all(s == "void" for s in statuses):
        status, payout = "void", bet["stake"]
    else:
        odds = 1.0
        for leg in legs:
            if leg["status"] == "won":
                odds *= leg["odds"]
        status, payout = "won", round(bet["stake"] * capped_odds(odds))

    difference = payout - bet["payout"]

    if difference:
        add_coins(guild_id, bet["user_id"], difference, f"bet:{bet_id}")

    query("UPDATE bets SET status = ?, payout = ? WHERE id = ?", (status, payout, bet_id), write=True)


def leg_result(
    leg,
    result: str,
    method: str,
    fight_round: int
):

    if result == "void":
        return "void"

    if leg["method"] in ROUND_PICKS:
        won = leg["pick"] == result and method in ("KO", "SUB") and fight_round == ROUND_PICKS[leg["method"]]
        return "won" if won else "lost"

    if leg["pick"] == result and (not leg["method"] or leg["method"] == method):
        return "won"

    return "lost"


def settle_bout(
    guild_id: int,
    bout,
    result: str,
    method: str = None,
    fight_round: int = None,
    fight_id: int = None
):
    """result 'a' / 'b' / 'void'."""

    query(
        "UPDATE bet_fights SET result = ?, method = ?, rnd = ?, fight_id = ? WHERE market_id = ? AND position = ?",
        (result, method, fight_round, fight_id, bout["market_id"], bout["position"]), write=True
    )

    for leg in query("""
        SELECT bet_legs.* FROM bet_legs JOIN bets ON bets.id = bet_legs.bet_id
        WHERE bets.market_id = ? AND bet_legs.position = ? AND bet_legs.fixed = 0
    """, (bout["market_id"], bout["position"])):

        query(
            "UPDATE bet_legs SET status = ? WHERE bet_id = ? AND position = ?",
            (leg_result(leg, result, method, fight_round), leg["bet_id"], leg["position"]), write=True
        )
        evaluate_bet(guild_id, leg["bet_id"])

    if not query("SELECT 1 FROM bet_fights WHERE market_id = ? AND result IS NULL", (bout["market_id"],)):
        query("UPDATE bet_markets SET status = 'settled' WHERE id = ?", (bout["market_id"],), write=True)

    _markets_to_refresh.add(bout["market_id"])


def settle_bets_for_fight(
    guild_id: int,
    fight
):
    """A result was logged: settle the matching fight on any betting card. Returns how many."""

    pair = {fight["winner_id"], fight["loser_id"]}
    settled = 0

    for bout in query("""
        SELECT bet_fights.* FROM bet_fights
        JOIN bet_markets ON bet_markets.id = bet_fights.market_id
        WHERE bet_markets.guild_id = ? AND bet_fights.result IS NULL
        ORDER BY bet_markets.id DESC
    """, (guild_id,)):

        if {bout["fighter_a"], bout["fighter_b"]} != pair:
            continue

        result = "a" if fight["winner_id"] == bout["fighter_a"] else "b"
        settle_bout(guild_id, bout, result, fight["method"], fight["round"], fight["id"])
        settled += 1

        break       # one card per result

    return settled


def unsettle_bets_for_fight(
    guild_id: int,
    fight_id: int
):
    """A result was removed or changed: undo its payouts."""

    for bout in query("SELECT * FROM bet_fights WHERE fight_id = ?", (fight_id,)):

        query(
            "UPDATE bet_fights SET result = NULL, method = NULL, rnd = NULL, fight_id = NULL "
            "WHERE market_id = ? AND position = ?",
            (bout["market_id"], bout["position"]), write=True
        )

        for leg in query("""
            SELECT bet_legs.* FROM bet_legs JOIN bets ON bets.id = bet_legs.bet_id
            WHERE bets.market_id = ? AND bet_legs.position = ? AND bet_legs.fixed = 0
        """, (bout["market_id"], bout["position"])):
            query(
                "UPDATE bet_legs SET status = 'pending' WHERE bet_id = ? AND position = ?",
                (leg["bet_id"], leg["position"]), write=True
            )
            evaluate_bet(guild_id, leg["bet_id"])

        market = get_market(bout["market_id"])

        if market and market["status"] == "settled":
            new_status = "open" if time.time() < market["closes_at"] else "locked"
            query("UPDATE bet_markets SET status = ? WHERE id = ?", (new_status, market["id"]), write=True)

        _markets_to_refresh.add(bout["market_id"])


def after_fight_logged(
    guild,
    fight_id: int
):
    """Fight pay + settle bets. Returns a short note for staff, or None."""

    rows = query("SELECT * FROM fight_results WHERE id = ?", (fight_id,))

    if not rows:
        return None

    fight = rows[0]
    notes = []

    pay = fight_pay_text(guild, fight, pay_fighters_for_fight(guild.id, fight))

    if pay:
        notes.append(pay)

    finished = finished_contracts_note(guild)

    if finished:
        notes.append(finished)

    if settle_bets_for_fight(guild.id, fight):
        notes.append("🎲 Bets on this fight have been settled.")

    return "\n".join(notes) or None


def before_fight_changed(
    guild_id: int,
    fight_id: int
):
    """Undo fight pay and settled bets before a fight is edited or removed."""

    reverse_coins(guild_id, f"fight:{fight_id}")
    undo_contract_fight(fight_id)
    unsettle_bets_for_fight(guild_id, fight_id)


def refund_check(
    bet
):
    """(True, None) if it can be refunded, otherwise (False, reason)."""

    if bet["status"] != "open":
        return False, "Only open bets can be refunded."

    legs = query("SELECT * FROM bet_legs WHERE bet_id = ?", (bet["id"],))

    if len(legs) != 1:
        return False, "Accumulators can't be refunded."

    if not market_is_open(get_market(bet["market_id"])):
        return False, "Betting has closed for this event, so the bet is locked."

    if refund_minutes_left(bet) <= 0:
        return False, f"Refunds are only within {BET_REFUND_MINUTES} minutes of placing a bet."

    return True, None


def refund_minutes_left(
    bet
):

    try:
        placed = datetime.fromisoformat(str(bet["created_at"]).replace("Z", "+00:00"))
        if placed.tzinfo is None:
            placed = placed.replace(tzinfo=timezone.utc)
        left = placed + timedelta(minutes=BET_REFUND_MINUTES) - datetime.now(timezone.utc)
        return max(0, int(left.total_seconds() // 60))
    except Exception:
        return 0


def refund_bet(
    guild_id: int,
    user_id: int,
    bet_id: int
):
    """Returns (ok, message)."""

    rows = query("SELECT * FROM bets WHERE id = ? AND guild_id = ?", (bet_id, guild_id))

    if not rows or rows[0]["user_id"] != user_id:
        return False, "❌ That isn't your bet."

    bet = rows[0]
    ok, reason = refund_check(bet)

    if not ok:
        return False, f"❌ {reason}"

    db = get_db()
    changed = db.execute(
        "UPDATE bets SET status = 'void', payout = ? WHERE id = ? AND status = 'open'",
        (bet["stake"], bet_id)
    ).rowcount == 1
    db.commit()
    db.close()

    if not changed:
        return False, "❌ That bet has already been settled."

    query("UPDATE bet_legs SET status = 'void', fixed = 1 WHERE bet_id = ?", (bet_id,), write=True)
    add_coins(guild_id, user_id, bet["stake"], f"bet_refund:{bet_id}")
    _markets_to_refresh.add(bet["market_id"])

    return True, f"↩️ Refunded {COIN} **{coins_text(bet['stake'])}**."


def cashout_offer(
    bet
):
    """(amount, reason). amount is None when it can't be cashed out."""

    if bet["status"] != "open":
        return None, "Only open bets can be cashed out."

    legs = query("SELECT * FROM bet_legs WHERE bet_id = ?", (bet["id"],))

    if len(legs) < 2:
        return None, "Cash out is for accumulators only."

    if any(leg["status"] == "lost" for leg in legs):
        return None, "A pick has already lost."

    if not any(leg["status"] == "won" for leg in legs):
        return None, "Cash out opens once one of your picks has won."

    value = bet["stake"]

    for leg in legs:
        if leg["status"] == "won":
            value *= leg["odds"]

    for leg in legs:
        if leg["status"] == "pending":
            bout = get_bout(bet["market_id"], leg["position"])
            if not bout:
                return None, "Couldn't price this bet."
            value /= max(pick_odds(bout, leg["pick"], leg["method"]), 1.01)

    amount = int(min(value, bet["stake"] * MAX_BET_ODDS) * CASHOUT_MARGIN)

    if amount < CASHOUT_MIN:
        return None, "The offer is too small to cash out."

    return amount, None


def cash_out(
    guild_id: int,
    user_id: int,
    bet_id: int
):
    """Returns (ok, message)."""

    rows = query("SELECT * FROM bets WHERE id = ? AND guild_id = ?", (bet_id, guild_id))

    if not rows or rows[0]["user_id"] != user_id:
        return False, "❌ That isn't your bet."

    amount, reason = cashout_offer(rows[0])

    if amount is None:
        return False, f"❌ {reason}"

    db = get_db()
    changed = db.execute(
        "UPDATE bets SET status = 'cashed', payout = ? WHERE id = ? AND status = 'open'",
        (amount, bet_id)
    ).rowcount == 1
    db.commit()
    db.close()

    if not changed:
        return False, "❌ That bet has already been settled."

    query("UPDATE bet_legs SET fixed = 1 WHERE bet_id = ?", (bet_id,), write=True)
    add_coins(guild_id, user_id, amount, f"cashout:{bet_id}")
    _markets_to_refresh.add(rows[0]["market_id"])

    return True, f"💸 Cashed out for {COIN} **{coins_text(amount)}**."


def bet_summary(
    guild,
    bet
):
    """'Matt Hughes by KO/TKO + Clay Guida'"""

    parts = []
    legs = query("SELECT * FROM bet_legs WHERE bet_id = ? ORDER BY position", (bet["id"],))

    for leg in legs:
        bout = get_bout(bet["market_id"], leg["position"])
        # Ticks per pick only help on accumulators (a single shows its result anyway).
        icon = {"won": "✅ ", "lost": "❌ ", "void": "↩️ "}.get(leg["status"], "") if len(legs) > 1 else ""
        label = pick_label(guild, bout, leg["pick"], leg["method"]) if bout else "?"
        parts.append(f"{icon}{label}")

    return " + ".join(parts)


def my_bets_embed(
    guild,
    user_id: int,
    note: str = None
):

    open_bets = query(
        "SELECT * FROM bets WHERE guild_id = ? AND user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 15",
        (guild.id, user_id)
    )
    settled = query(
        "SELECT * FROM bets WHERE guild_id = ? AND user_id = ? AND status != 'open' ORDER BY id DESC LIMIT 5",
        (guild.id, user_id)
    )

    lines = []

    if note:
        lines += [note, ""]

    if open_bets:
        lines.append("**Open**")

        for bet in open_bets:
            market = get_market(bet["market_id"])
            extra = []
            ok, _ = refund_check(bet)
            if ok:
                extra.append(f"refund for {refund_minutes_left(bet)} more min")
            offer, _ = cashout_offer(bet)
            if offer:
                extra.append(f"cash out {COIN} {coins_text(offer)}")

            lines.append(
                f"`#{bet['id']}` {COIN} {coins_text(bet['stake'])} @ **{bet['odds']:.2f}** → "
                f"{coins_text(round(bet['stake'] * bet['odds']))} · {bet_summary(guild, bet)}"
            )
            lines.append(f"-# {market['event'] if market else ''}" + (" · " + " · ".join(extra) if extra else ""))
    else:
        lines.append("No open bets.")

    if settled:
        lines += ["", "**Settled**"]

        for bet in settled:
            icon = {"won": "✅", "lost": "❌", "void": "↩️", "cashed": "💸"}.get(bet["status"], "")
            result = (
                f"+{coins_text(bet['payout'])}" if bet["status"] in ("won", "cashed")
                else "refunded" if bet["status"] == "void" else f"-{coins_text(bet['stake'])}"
            )
            lines.append(f"{icon} `#{bet['id']}` {bet_summary(guild, bet)} · **{result}**")

    lines += ["", f"Your balance: {COIN} **{coins_text(get_wallet(guild.id, user_id)['balance'])}**"]

    embed = discord.Embed(title="My bets", description="\n".join(lines)[:4096], color=BRAND_RED)
    apply_branding(embed, author_text=f"{BRAND_TAG} • SPORTSBOOK")

    return embed


class MyBetsView(
    discord.ui.View
):
    """A dropdown of bets that can be refunded or cashed out right now."""

    def __init__(
        self,
        guild,
        user_id: int
    ):

        super().__init__(timeout=600)

        self.guild = guild
        self.user_id = user_id
        self.actions = {}
        self.rebuild()

    def rebuild(self):

        self.clear_items()
        self.actions = {}
        options = []

        for bet in query(
            "SELECT * FROM bets WHERE guild_id = ? AND user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 25",
            (self.guild.id, self.user_id)
        ):
            if refund_check(bet)[0]:
                key = f"refund:{bet['id']}"
                self.actions[key] = bet["id"]
                options.append(discord.SelectOption(
                    label=f"Refund #{bet['id']} ({coins_text(bet['stake'])} back)", value=key, emoji="↩️"))
            else:
                offer, _ = cashout_offer(bet)
                if offer:
                    key = f"cash:{bet['id']}"
                    self.actions[key] = bet["id"]
                    options.append(discord.SelectOption(
                        label=f"Cash out #{bet['id']} for {coins_text(offer)}", value=key, emoji="💸"))

        if options:
            select = discord.ui.Select(placeholder="Refund or cash out a bet", options=options[:25])
            select.callback = self.choose
            self.add_item(select)
            self.select = select

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        return interaction.user.id == self.user_id

    async def choose(
        self,
        interaction: discord.Interaction
    ):

        key = self.select.values[0]
        bet_id = self.actions.get(key)

        if key.startswith("refund:"):
            ok, message = refund_bet(interaction.guild.id, interaction.user.id, bet_id)
        else:
            ok, message = cash_out(interaction.guild.id, interaction.user.id, bet_id)

        self.rebuild()

        await interaction.response.edit_message(
            embed=my_bets_embed(interaction.guild, interaction.user.id, message),
            view=self if self.children else None
        )


async def send_my_bets(
    interaction: discord.Interaction
):

    view = MyBetsView(interaction.guild, interaction.user.id)

    await interaction.response.send_message(
        embed=my_bets_embed(interaction.guild, interaction.user.id),
        view=view if view.children else None,
        ephemeral=True
    )


@bot.tree.command(
    name="mybets",
    description="Your bets: open, settled, refunds and cash outs."
)
async def mybets(
    interaction: discord.Interaction
):

    await send_my_bets(interaction)


class StakeModal(
    discord.ui.Modal,
    title="How many coins?"
):

    amount = discord.ui.TextInput(
        label="Stake",
        placeholder=f"Between {MIN_BET} and {MAX_BET}",
        max_length=6
    )

    def __init__(
        self,
        slip
    ):

        super().__init__()

        self.slip = slip

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        text = (self.amount.value or "").replace(",", "").strip()

        if not text.isdigit():
            await self.slip.redraw(interaction, "❌ Type a number of coins.")
            return

        self.slip.stake = int(text)
        await self.slip.redraw(interaction)


class BetSlipView(
    discord.ui.View
):

    def __init__(
        self,
        market_id: int,
        guild,
        user_id: int
    ):

        super().__init__(timeout=900)

        self.market_id = market_id
        self.guild = guild
        self.user_id = user_id
        self.legs = {}          # position -> (pick, method)
        self.chosen = None      # fight picked in the first dropdown
        self.stake = 100
        self.rebuild()

    def bouts(self):

        return {bout["position"]: bout for bout in market_bouts(self.market_id) if not bout["result"]}

    def slip_odds(self):

        bouts = self.bouts()
        total = 1.0

        for position, (pick, method) in self.legs.items():
            if position in bouts:
                total *= pick_odds(bouts[position], pick, method)

        return round(total, 2)

    def embed(
        self,
        note: str = None
    ):

        bouts = self.bouts()
        balance = get_wallet(self.guild.id, self.user_id)["balance"]
        lines = []

        if note:
            lines += [note, ""]

        if self.legs:
            kind = "Single" if len(self.legs) == 1 else f"Accumulator ({len(self.legs)} picks, all must win)"
            lines.append(f"**{kind}**")

            for position, (pick, method) in sorted(self.legs.items()):
                bout = bouts.get(position)
                if bout:
                    lines.append(
                        f"`{position:>2}` {pick_label(self.guild, bout, pick, method)} · "
                        f"`{pick_odds(bout, pick, method):.2f}`"
                    )

            raw = self.slip_odds()
            odds = capped_odds(raw)

            lines += [
                "",
                f"Stake: {COIN} **{coins_text(self.stake)}** · Odds **{odds:.2f}**"
                + (f" (max, was {raw:,.2f})" if raw > odds else ""),
                f"Returns: {COIN} **{coins_text(round(self.stake * odds))}** if it wins"
            ]

            if raw > odds:
                lines.append(f"-# Bets pay at most {MAX_BET_ODDS:g}× your stake")
        else:
            lines.append("1) Choose a fight  2) choose your pick. Add picks from other fights for an accumulator.")

        chosen = bouts.get(self.chosen)

        if chosen:
            lines += [
                "",
                "**Form** (last 5, newest on the right)",
                f"{side_name(self.guild, chosen, 'a')}: `{form_text(self.guild.id, chosen['fighter_a'])}`",
                f"{side_name(self.guild, chosen, 'b')}: `{form_text(self.guild.id, chosen['fighter_b'])}`"
            ]

        lines += ["", f"Your balance: {COIN} **{coins_text(balance)}**"]

        embed = discord.Embed(title="Your bet slip", description="\n".join(lines), color=BRAND_RED)
        apply_branding(embed, author_text=f"{BRAND_TAG} • SPORTSBOOK")
        embed.set_footer(text=f"Bets {MIN_BET}–{MAX_BET} coins • odds lock when you place the bet")

        return embed

    def rebuild(self):

        self.clear_items()
        bouts = self.bouts()

        fight_select = discord.ui.Select(
            placeholder="1) Choose a fight",
            options=[
                discord.SelectOption(
                    label=f"{position}. {side_name(self.guild, bout, 'a')} vs {side_name(self.guild, bout, 'b')}"[:100],
                    value=str(position),
                    description="✅ on your slip" if position in self.legs else None,
                    default=position == self.chosen
                )
                for position, bout in list(bouts.items())[:25]
            ] or [discord.SelectOption(label="No fights left to bet on", value="0")],
            disabled=not bouts,
            row=0
        )
        fight_select.callback = self.choose_fight
        self.add_item(fight_select)
        self.fight_select = fight_select

        bout = bouts.get(self.chosen)

        if bout:
            current = self.legs.get(self.chosen)
            options = [
                discord.SelectOption(
                    label=f"{pick_label(self.guild, bout, pick, method)} · {pick_odds(bout, pick, method):.2f}"[:100],
                    value=f"{pick}|{method or ''}",
                    default=current == (pick, method)
                )
                for pick in ("a", "b")
                for method in (None, "KO", "SUB", "DEC", *ROUND_PICKS)
            ]
            pick_select = discord.ui.Select(placeholder="2) Choose your pick", options=options, row=1)
        else:
            pick_select = discord.ui.Select(
                placeholder="2) Choose your pick (pick a fight first)",
                options=[discord.SelectOption(label="-", value="-")],
                disabled=True,
                row=1
            )

        pick_select.callback = self.choose_pick
        self.add_item(pick_select)
        self.pick_select = pick_select

        for amount in STAKE_BUTTONS:
            button = discord.ui.Button(
                label=coins_text(amount),
                style=discord.ButtonStyle.primary if self.stake == amount else discord.ButtonStyle.secondary,
                row=2
            )
            button.callback = self.stake_callback(amount)
            self.add_item(button)

        other = discord.ui.Button(label="Other", style=discord.ButtonStyle.secondary, row=2)
        other.callback = self.other_stake
        self.add_item(other)

        place = discord.ui.Button(label="Place bet", emoji="✅", style=discord.ButtonStyle.success, row=3,
                                  disabled=not self.legs)
        place.callback = self.place
        self.add_item(place)

        remove = discord.ui.Button(label="Remove this pick", style=discord.ButtonStyle.secondary, row=3,
                                   disabled=self.chosen not in self.legs)
        remove.callback = self.remove_pick
        self.add_item(remove)

        clear = discord.ui.Button(label="Clear slip", style=discord.ButtonStyle.danger, row=3, disabled=not self.legs)
        clear.callback = self.clear
        self.add_item(clear)

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        return interaction.user.id == self.user_id

    async def redraw(
        self,
        interaction: discord.Interaction,
        note: str = None
    ):

        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(note), view=self)

    async def choose_fight(
        self,
        interaction: discord.Interaction
    ):

        value = int(self.fight_select.values[0])
        self.chosen = value or None
        await self.redraw(interaction)

    async def choose_pick(
        self,
        interaction: discord.Interaction
    ):

        value = self.pick_select.values[0]

        if value == "-" or self.chosen is None:
            await self.redraw(interaction)
            return

        pick, method = value.split("|")
        self.legs[self.chosen] = (pick, method or None)
        await self.redraw(interaction)

    def stake_callback(
        self,
        amount: int
    ):

        async def callback(interaction: discord.Interaction):
            self.stake = amount
            await self.redraw(interaction)

        return callback

    async def other_stake(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.send_modal(StakeModal(self))

    async def remove_pick(
        self,
        interaction: discord.Interaction
    ):

        self.legs.pop(self.chosen, None)
        await self.redraw(interaction)

    async def clear(
        self,
        interaction: discord.Interaction
    ):

        self.legs = {}
        self.chosen = None
        await self.redraw(interaction)

    async def place(
        self,
        interaction: discord.Interaction
    ):

        ok, message, _ = place_bet(self.guild, self.user_id, self.market_id, dict(self.legs), self.stake)

        if not ok and not market_is_open(get_market(self.market_id)):
            await interaction.response.edit_message(content=message, embed=None, view=None)
            return

        if ok:
            self.legs = {}
            self.chosen = None

        await self.redraw(interaction, message)


class SportsbookView(
    discord.ui.View
):
    """Buttons under the sportsbook post. They keep working after restarts."""

    def __init__(
        self,
        market_id: int
    ):

        super().__init__(timeout=None)

        self.market_id = market_id
        market = get_market(market_id)

        bet = discord.ui.Button(
            label="Place a bet", emoji="💰", style=discord.ButtonStyle.success,
            custom_id=f"uni_book_{market_id}_bet", disabled=not market_is_open(market)
        )
        bet.callback = self.place_bet
        self.add_item(bet)

        mine = discord.ui.Button(
            label="My bets", emoji="🧾", style=discord.ButtonStyle.secondary,
            custom_id=f"uni_book_{market_id}_mine"
        )
        mine.callback = self.my_bets
        self.add_item(mine)

    async def place_bet(
        self,
        interaction: discord.Interaction
    ):

        if not market_is_open(get_market(self.market_id)):
            await interaction.response.send_message("🔒 Betting has closed for this event.", ephemeral=True)
            return

        slip = BetSlipView(self.market_id, interaction.guild, interaction.user.id)
        await interaction.response.send_message(embed=slip.embed(), view=slip, ephemeral=True)

    async def my_bets(
        self,
        interaction: discord.Interaction
    ):

        await send_my_bets(interaction)


def restore_sportsbook_buttons():

    markets = query("SELECT id FROM bet_markets WHERE message_id IS NOT NULL AND status != 'void'")

    for market in markets:
        try:
            bot.add_view(SportsbookView(market["id"]))
        except Exception as error:
            print(f"Could not restore sportsbook {market['id']}: {error}")

    print(f"Loaded {len(markets)} sportsbook post(s).")


async def market_autocomplete(
    interaction: discord.Interaction,
    current: str
):

    choices = []

    for market in query(
        "SELECT * FROM bet_markets WHERE guild_id = ? ORDER BY id DESC LIMIT 25",
        (interaction.guild.id,)
    ):
        label = f"#{market['id']} {market['event']} ({market['status']})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=market["id"]))

    return choices


@bot.tree.command(
    name="openbets",
    description="Open betting on a card: auto odds from the rankings and fight log, or your own."
)
@app_commands.describe(
    event="Event name, e.g. UFC Universe 13",
    closes="When betting closes, UK time, e.g. 14/11/2026 19:00",
    fighters="The fights in order, main event first: @Joe @Dan @Tom @Ben = Joe v Dan, Tom v Ben",
    odds="Optional, per fight in the same order: auto or 1.60/2.30, e.g. auto, 1.60/2.30",
    titles="Which fights are title fights, by number, e.g. 1 or 1, 3"
)
async def openbets(
    interaction: discord.Interaction,
    event: app_commands.Range[str, 1, 80],
    closes: str,
    fighters: str,
    odds: str = None,
    titles: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can open betting.", ephemeral=True)
        return

    moment = parse_event_datetime(closes)

    if not moment:
        await interaction.response.send_message(
            "❌ Write when betting closes like **14/11/2026 19:00** or **14/11 7pm** (UK time).",
            ephemeral=True
        )
        return

    if moment <= datetime.now(timezone.utc):
        await interaction.response.send_message("❌ That time has already passed.", ephemeral=True)
        return

    pairs = parse_fight_pairs(fighters)

    if not pairs:
        await interaction.response.send_message(
            "❌ @mention the fights in order, e.g. `@Joe @Dan @Tom @Ben` (Joe v Dan, Tom v Ben).",
            ephemeral=True
        )
        return

    if pairs[-1][1] is None:
        await interaction.response.send_message(
            f"❌ Odd number of fighters: {fighter_name(interaction.guild, pairs[-1][0])} has no opponent.",
            ephemeral=True
        )
        return

    if len(pairs) > 25:
        await interaction.response.send_message("❌ Betting can cover up to 25 fights.", ephemeral=True)
        return

    for a, b in pairs:
        if a == b:
            await interaction.response.send_message(
                f"❌ {fighter_name(interaction.guild, a)} can't fight themselves.", ephemeral=True)
            return

    try:
        manual = parse_odds_list(odds, len(pairs))
    except ValueError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return

    title_fights = parse_positions(titles)
    guild = interaction.guild

    market_id = query(
        "INSERT INTO bet_markets (guild_id, event, closes_at, status, channel_id, created_by, created_at) "
        "VALUES (?, ?, ?, 'open', ?, ?, ?)",
        (guild.id, event.strip(), int(moment.timestamp()), interaction.channel.id, interaction.user.id, now_utc()),
        write=True
    )

    adjusted = []

    for position, (a, b) in enumerate(pairs, start=1):
        typed = manual[position - 1]
        division = shared_division(guild.id, a, b)

        if typed:
            p = manual_probability(*typed)
            raw = (1 / typed[0]) / ((1 / typed[0]) + (1 / typed[1]))
            if abs(p - raw) > 0.001:
                adjusted.append(position)
        else:
            p = opening_probability(guild.id, a, b, division)

        query(
            "INSERT INTO bet_fights (market_id, position, fighter_a, fighter_b, name_a, name_b, division, "
            "open_pa, manual, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (market_id, position, a, b, fighter_name(guild, a), fighter_name(guild, b), division,
             p, 1 if typed else 0, 1 if position in title_fights else 0),
            write=True
        )

    await interaction.response.send_message(
        embed=sportsbook_embed(get_market(market_id), guild),
        view=SportsbookView(market_id)
    )

    message = await interaction.original_response()
    query("UPDATE bet_markets SET message_id = ? WHERE id = ?", (message.id, market_id), write=True)

    if adjusted:
        await interaction.followup.send(
            "⚠️ Odds for fight " + ", ".join(str(n) for n in adjusted)
            + " were pulled in to the limits (no side shorter than 85% or longer than 15%).",
            ephemeral=True
        )


@bot.tree.command(
    name="closebets",
    description="Close betting on a card now (before its closing time)."
)
@app_commands.describe(card="Which betting card")
@app_commands.autocomplete(card=market_autocomplete)
async def closebets(
    interaction: discord.Interaction,
    card: int
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can close betting.", ephemeral=True)
        return

    market = get_market(card)

    if not market or market["guild_id"] != interaction.guild.id:
        await interaction.response.send_message("❌ Pick a card from the list.", ephemeral=True)
        return

    if market["status"] != "open":
        await interaction.response.send_message("Betting on that card is already closed.", ephemeral=True)
        return

    query("UPDATE bet_markets SET status = 'locked' WHERE id = ?", (card,), write=True)
    await refresh_sportsbook(card)

    await interaction.response.send_message(f"🔒 Betting closed on **{market['event']}**.", ephemeral=True)


@bot.tree.command(
    name="voidfight",
    description="Void one fight on a betting card: every bet on it is refunded (accumulators drop that pick)."
)
@app_commands.describe(card="Which betting card", fight="Fight number on that card (1 = main event)")
@app_commands.autocomplete(card=market_autocomplete)
async def voidfight(
    interaction: discord.Interaction,
    card: int,
    fight: app_commands.Range[int, 1, 25]
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can void fights.", ephemeral=True)
        return

    market = get_market(card)
    bout = get_bout(card, fight) if market and market["guild_id"] == interaction.guild.id else None

    if not bout:
        await interaction.response.send_message("❌ That fight isn't on that card.", ephemeral=True)
        return

    if bout["fight_id"]:
        unsettle_bets_for_fight(interaction.guild.id, bout["fight_id"])
        bout = get_bout(card, fight)

    settle_bout(interaction.guild.id, bout, "void")
    await refresh_sportsbook(card)

    await interaction.response.send_message(
        f"↩️ Voided {side_name(interaction.guild, bout, 'a')} vs {side_name(interaction.guild, bout, 'b')}. "
        "Single bets are refunded and accumulators carry on without it.",
        ephemeral=True
    )


# ============================================================
# HELP
# ============================================================

HELP_EVERYONE = [
    ("🪙 Coins", [
        ("/coins", "your coins, rank and daily bonus"),
        ("/daily", "claim your daily coins (more for a streak)"),
        ("/coinboard", "who has the most coins"),
    ]),
    ("🎲 Betting", [
        ("Place a bet", "the button on a sportsbook post: winner, KO/TKO, SUB, DEC or round, singles or accumulators"),
        ("/mybets", "your bets, refunds (within 60 min) and cash outs"),
    ]),
    ("📝 Contracts and pay", [
        ("/contract view", "your contract, or accept an offer waiting for you"),
        ("/payrates", "what every fight pays"),
    ]),
    ("📊 Stats", [
        ("/serverstats", "server-wide KOs, submissions and decisions"),
    ]),
]

HELP_STAFF = [
    ("🥊 Live Cards", [
        ("/dmlivecard", "DM everyone on a Live Card with a Check In button"),
        ("/livecheckin", "post the check-in board (close:True closes it)"),
    ]),
    ("📋 Fights and profiles", [
        ("/logfight", "log a result: pays fighters and settles bets"),
        ("/scanresults", "import results posted in a channel"),
        ("/fights", "the fight log with fight numbers"),
        ("/editfight · /removefight", "fix or delete a logged fight"),
        ("/profile", "a fighter's profile (Overview and Fights pages)"),
        ("/resetresults", "delete every logged fight"),
    ]),
    ("🎲 Betting (staff)", [
        ("/openbets", "open betting on a card (auto odds or e.g. 1.60/2.30)"),
        ("/closebets · /voidfight", "close betting early, or void a fight"),
        ("/givecoins", "give or take coins"),
    ]),
    ("📝 Contracts (staff)", [
        ("/contract offer", "offer a fighter a contract (they get a DM)"),
        ("/contract end · /contract list", "end one, or see them all"),
    ]),
    ("🏆 Rankings sheet", [
        ("/rankingslink", "link the owner's sheet (empty: check now, off: unlink)"),
        ("/uploadrankings", "upload the sheet as a file instead"),
        ("/rankingscheck", "privately see every fighter the bot read, and skipped rows"),
        ("/rankingsedit", "fix small mistakes: link a name, set a rank, remove, list, undo"),
    ]),
]

HELP_FIGHTER = [
    ("🥊 Fighters", [
        ("/profile", "any fighter's profile (Overview and Fights pages)"),
    ]),
]

HELP_OWNER = [
    ("👑 Owner", [
        ("/payrates show win finish", "change the base fight pay"),
    ]),
]


@bot.tree.command(
    name="help",
    description="What the bot can do (and the commands you can use)."
)
async def help_command(
    interaction: discord.Interaction
):

    staff = is_staff(interaction)
    owner = is_owner(interaction)

    sections = list(HELP_EVERYONE)

    if staff:
        sections += HELP_STAFF
    elif can_view_profiles(interaction):
        sections += HELP_FIGHTER
    if owner:
        sections += HELP_OWNER

    embed = discord.Embed(
        title="Bot help",
        description=(
            "Commands you can use. Everything replies just to you unless it says otherwise."
            + ("\n-# Showing staff commands too." if staff else "")
        ),
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • HELP")

    for name, commands_list in sections:
        embed.add_field(
            name=name,
            value="\n".join(f"**{command}** · {what}" for command, what in commands_list)[:1024],
            inline=False
        )

    embed.set_footer(
        text=f"Bets {MIN_BET}–{MAX_BET} coins • start with {coins_text(START_BALANCE)} coins",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# BACKGROUND CHECKS (every minute)
# ============================================================

@tasks.loop(minutes=1)
async def background_checks():

    try:
        await check_card_reminders()
    except Exception as error:
        print(f"Reminder error: {error}")

    try:
        await refresh_waiting_sportsbooks()
    except Exception as error:
        print(f"Sportsbook error: {error}")

    try:
        await sync_linked_rankings()
    except Exception as error:
        print(f"Rankings sync error: {error}")


@background_checks.before_loop
async def before_background_checks():
    await bot.wait_until_ready()


# ============================================================
# STARTUP
#
# Runs once when the bot starts (not on every reconnect):
#   1. set up the database
#   2. restore the Check In, sportsbook and contract buttons
#   3. start the every-minute checks (auto-close, reminders)
#   4. register the slash commands with Discord, only when this
#      file has changed (re-registering every start can get the
#      bot temporarily blocked for too many requests)
# ============================================================

def commands_fingerprint():

    try:
        with open(os.path.abspath(__file__), "rb") as source:
            code = source.read()
    except OSError:
        code = b""

    return hashlib.sha256(code + str(GUILD_ID).encode()).hexdigest()


def restore_checkin_buttons():

    notices = get_active_notices()

    for notice in notices:
        try:
            bot.add_view(CheckInView(notice["id"]))
        except Exception as error:
            print(f"Could not restore check-in {notice['id']}: {error}")

    print(f"Loaded {len(notices)} active check-in(s).")


async def sync_commands_if_changed():

    fingerprint = commands_fingerprint()

    if (
        os.getenv("FORCE_COMMAND_SYNC", "").strip() not in ("1", "true", "yes")
        and get_meta("commands_fingerprint") == fingerprint
    ):
        print("Slash commands unchanged; skipping sync.")
        return

    try:

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print("Slash commands synced to your server.")
        else:
            await bot.tree.sync()
            print("Global slash commands synced.")

        set_meta("commands_fingerprint", fingerprint)

    except Exception as error:
        print(f"Could not sync slash commands (will retry next start): {error}")


async def setup_hook():

    setup_database()
    setup_rankings_tables()
    setup_coin_tables()
    setup_contract_tables()

    restore_checkin_buttons()
    restore_sportsbook_buttons()
    restore_contract_buttons()

    if not background_checks.is_running():
        background_checks.start()

    await sync_commands_if_changed()


bot.setup_hook = setup_hook


@bot.event
async def on_ready():

    print(f"Logged in as {bot.user}")
    print(f"Bot ID: {bot.user.id}")


# ============================================================
# START BOT
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "DISCORD_TOKEN is missing from your environment variables."
    )


setup_database()
setup_rankings_tables()
setup_coin_tables()
setup_contract_tables()

# If Discord temporarily blocks the bot (Cloudflare "error 1015" / 429),
# wait before exiting instead of restarting straight away, which would
# make the block last longer.
BLOCKED_WAIT_SECONDS = int(
    os.getenv("BLOCKED_WAIT_SECONDS", "1800")
)

try:

    bot.run(TOKEN)

except discord.HTTPException as error:

    if error.status == 429:

        print(
            "Discord has temporarily blocked this bot for too many requests "
            "(Cloudflare error 1015 / 429). This usually clears on its own. "
            f"Waiting {BLOCKED_WAIT_SECONDS // 60} minutes before trying again."
        )

        time.sleep(BLOCKED_WAIT_SECONDS)

    raise
