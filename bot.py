"""
UFC Universe bot: Live Card sign-ups, Live Card DMs with check-in, reminder DMs,
and fight stats from scanned results.

Commands (staff only):
  /livesignup   post a Live Card sign-up (Sign Up + Withdraw buttons).
                Optional event name, start time, closing time, card-out time.
  /signuptimes  change the open sign-up's event name or times
  /signupclose  close the open sign-up
  /signuppaste  post the sign-up list
  /dmlivecard   DM everyone you @mention that they're on a Live Card,
                with a Check In button in the DM
  /livecheckin  post a board showing who has checked in (updates by
                itself); close:True closes check-in
  /scanresults  read fight results from a channel, preview, then Import
                (optional card name to tag them with)
  /logfight     log one fight by hand: winner, loser, method, round,
                card name, date
  /removefight  delete one logged fight by its number
  /profile      a fighter's wins: KO/TKO, Submission, Decision
  /resetresults delete all scanned results

Anyone:
  /serverstats  server-wide KO/TKO, Submission and Decision totals

Automatic:
  - Sign-ups with a closing time close by themselves.
  - Everyone on the latest /dmlivecard gets a reminder DM before the
    card starts (1 hour by default, REMINDER_HOURS in Railway). Anyone
    not checked in gets the Check In button again. A note of who was
    reminded (and who still hasn't checked in) goes in the Diddy-Blud
    channel (REMINDER_CHANNEL in Railway to change it).
"""

import asyncio
import hashlib
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
SIGNUP_BANNER = os.getenv("SIGNUP_BANNER", "signup.gif").strip()
SIGNUP_CLOSED_BANNER = os.getenv("SIGNUP_CLOSED_BANNER", "signup_closed.gif").strip()
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
        CREATE TABLE IF NOT EXISTS signup_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            message_id INTEGER,
            channel_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            closes_at INTEGER,
            card_out TEXT,
            event_name TEXT,
            starts_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            discord_user_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            created_at TEXT NOT NULL
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


def find_reminder_channel(
    guild: discord.Guild
):

    setting = REMINDER_CHANNEL

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


def is_clear_word(
    text: str
):

    return (text or "").strip().lower() in ("none", "clear", "-")


def read_time_value(
    text: str
):
    """Stored value for a start / card-out time: ts:unix, the text as typed, or None to clear."""

    if not text or is_clear_word(text):
        return None

    moment = parse_event_datetime(text)

    return f"ts:{int(moment.timestamp())}" if moment else text.strip()[:100]


def time_value_text(
    value
):

    if not value:
        return None

    value = str(value)

    if value.startswith("ts:"):
        unix = int(value[3:])
        return f"<t:{unix}:F> (<t:{unix}:R>)"

    return value


def read_closing_time(
    text: str
):
    """(closes_at unix or None, error or None). 'none' clears it."""

    if not text or is_clear_word(text):
        return None, None

    moment = parse_event_datetime(text)

    if not moment:
        return None, "❌ Write when sign-ups close like **24/10/2026 18:00** or **24/10 6pm** (UK time)."

    if moment <= datetime.now(timezone.utc):
        return None, "❌ That closing time has already passed."

    return int(moment.timestamp()), None


# ============================================================
# SIGN-UPS
# ============================================================

SIGNUP_TITLE = f"{BRAND_NAME} Live Card Sign-Ups"
SIGNUP_MESSAGE = "Live Card Sign-Ups, press the button below to sign up!"
SIGNUP_NO_GUARANTEE = "Signing up does not guarantee you a fight. Fights are picked by staff."


def get_active_session(
    guild_id: int
):

    rows = query("""
        SELECT * FROM signup_sessions
        WHERE guild_id = ? AND active = 1
        ORDER BY id DESC LIMIT 1
    """, (guild_id,))

    return rows[0] if rows else None


def get_session(
    session_id: int
):

    rows = query("SELECT * FROM signup_sessions WHERE id = ?", (session_id,))

    return rows[0] if rows else None


def update_session(
    session_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    query(
        f"UPDATE signup_sessions SET {columns} WHERE id = ?",
        (*changes.values(), session_id),
        write=True
    )


def get_signups(
    session_id: int
):

    return query(
        "SELECT * FROM signups WHERE session_id = ? ORDER BY id ASC",
        (session_id,)
    )


def get_signup_count(
    session_id: int
):

    return query(
        "SELECT COUNT(*) AS n FROM signups WHERE session_id = ?",
        (session_id,)
    )[0]["n"]


def is_signed_up(
    session_id: int,
    user_id: int
):

    return bool(query(
        "SELECT 1 FROM signups WHERE session_id = ? AND discord_user_id = ?",
        (session_id, user_id)
    ))


def add_signup(
    session_id: int,
    user_id: int,
    name: str
):

    if is_signed_up(session_id, user_id):
        return False

    query(
        "INSERT INTO signups (session_id, discord_user_id, player_name, created_at) VALUES (?, ?, ?, ?)",
        (session_id, user_id, name, now_utc()),
        write=True
    )

    return True


def remove_signup(
    session_id: int,
    user_id: int
):

    query(
        "DELETE FROM signups WHERE session_id = ? AND discord_user_id = ?",
        (session_id, user_id),
        write=True
    )


def signup_title(
    session
):

    event = (session["event_name"] or "").strip() if session else ""

    return f"{event} Sign-Ups" if event else SIGNUP_TITLE


def signup_time_lines(
    session,
    closed: bool = False
):

    lines = []

    starts = time_value_text(session["starts_at"])
    if starts:
        lines.append(f"🗓️ **Card starts:** {starts}")

    if session["closes_at"] and not closed:
        unix = session["closes_at"]
        lines.append(f"⏰ **Sign-ups close:** <t:{unix}:F> (<t:{unix}:R>)")

    card_out = time_value_text(session["card_out"])
    if card_out:
        lines.append(f"📋 **Card out:** {card_out}")

    return lines


def create_signup_embed(
    session_id: int
):

    session = get_session(session_id)
    count = get_signup_count(session_id)
    times = signup_time_lines(session)

    embed = discord.Embed(
        title=signup_title(session),
        description=(
            f"**{SIGNUP_MESSAGE}**\n\n"
            f"🥊 **Current Sign-Ups: {count}**\n"
            f"🟢 **Status:** Open\n"
            + (("\n" + "\n".join(times) + "\n") if times else "")
            + f"\n⚠️ *{SIGNUP_NO_GUARANTEE}*"
        ),
        color=BRAND_RED,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=SIGNUP_BANNER,
        author_text=f"{BRAND_TAG} • LIVE CARD"
    )

    embed.set_footer(
        text="Press Sign Up to join • Press Withdraw to take yourself off the list",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


def create_closed_embed(
    session_id: int
):

    session = get_session(session_id)
    count = get_signup_count(session_id)

    embed = discord.Embed(
        title=signup_title(session),
        description=(
            f"🔒 **Sign-Ups Closed! [{count} SIGN-UPS]!**\n\n"
            "This sign-up is no longer accepting entries."
            + "".join(f"\n\n{line}" for line in signup_time_lines(session, closed=True))
        ),
        color=BRAND_RED,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=SIGNUP_CLOSED_BANNER,
        author_text=f"{BRAND_TAG} • LIVE CARD"
    )

    embed.set_footer(text="Closed")

    return embed


async def refresh_signup_message(
    guild: discord.Guild,
    session_id: int
):

    session = get_session(session_id)

    if not session or not session["message_id"]:
        return

    try:
        channel = guild.get_channel(session["channel_id"]) or await bot.fetch_channel(session["channel_id"])
        message = await channel.fetch_message(session["message_id"])

        if session["active"]:
            await message.edit(embed=create_signup_embed(session_id), view=SignupView(session_id))
        else:
            await message.edit(embed=create_closed_embed(session_id), view=None)

    except Exception as error:
        print(f"Could not update sign-up message: {error}")


class SignupView(
    discord.ui.View
):
    """Sign Up / Withdraw under the sign-up post. Works after restarts."""

    def __init__(
        self,
        session_id: int
    ):

        super().__init__(timeout=None)

        self.session_id = session_id

        sign_up = discord.ui.Button(
            label="Sign Up",
            style=discord.ButtonStyle.green,
            emoji="🥊",
            custom_id=f"ufcu_signup_{session_id}"
        )
        sign_up.callback = self.sign_up

        withdraw = discord.ui.Button(
            label="Withdraw",
            style=discord.ButtonStyle.red,
            emoji="↩️",
            custom_id=f"ufcu_withdraw_{session_id}"
        )
        withdraw.callback = self.withdraw

        self.add_item(sign_up)
        self.add_item(withdraw)

    async def sign_up(
        self,
        interaction: discord.Interaction
    ):

        session = get_session(self.session_id)

        if not session or not session["active"]:
            await interaction.response.send_message("❌ This sign-up is closed.", ephemeral=True)
            return

        if not add_signup(self.session_id, interaction.user.id, interaction.user.display_name):
            await interaction.response.send_message("❌ You are already signed up for this card.", ephemeral=True)
            return

        await interaction.response.send_message(
            "✅ **You have been signed up!**\n\n"
            f"**Current Sign-Ups: {get_signup_count(self.session_id)}**\n\n"
            f"⚠️ {SIGNUP_NO_GUARANTEE} "
            "Changed your mind? Press **Withdraw** to take yourself off the list.",
            ephemeral=True
        )

        await refresh_signup_message(interaction.guild, self.session_id)

    async def withdraw(
        self,
        interaction: discord.Interaction
    ):

        session = get_session(self.session_id)

        if not session or not session["active"]:
            await interaction.response.send_message(
                "❌ Sign-ups are closed, so the list can't be changed. Please contact a member of staff.",
                ephemeral=True
            )
            return

        if not is_signed_up(self.session_id, interaction.user.id):
            await interaction.response.send_message("❌ You're not on the sign-up list.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"⚠️ **This will take you off the sign-up list** for **{signup_title(session)}**.\n\n"
            "Are you sure you want to withdraw?",
            view=WithdrawConfirmView(self.session_id, interaction.user.id),
            ephemeral=True
        )


class WithdrawConfirmView(
    discord.ui.View
):
    """'Are you sure?' step before taking someone off the list."""

    def __init__(
        self,
        session_id: int,
        user_id: int
    ):

        super().__init__(timeout=120)

        self.session_id = session_id
        self.user_id = user_id

        confirm = discord.ui.Button(label="Yes, take me off the list", style=discord.ButtonStyle.red)
        confirm.callback = self.confirm

        cancel = discord.ui.Button(label="Stay signed up", style=discord.ButtonStyle.grey)
        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        session = get_session(self.session_id)

        if not session or not session["active"]:
            await interaction.response.edit_message(
                content="❌ Sign-ups have closed, so the list can't be changed. Please contact a member of staff.",
                view=None
            )
            return

        remove_signup(self.session_id, self.user_id)

        await interaction.response.edit_message(
            content="✅ **You've been taken off the sign-up list.**\n"
                    "You can sign up again while sign-ups are still open.",
            view=None
        )

        await refresh_signup_message(interaction.guild, self.session_id)

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(content="👍 You're still signed up.", view=None)


async def close_signup(
    guild: discord.Guild,
    session
):
    """Closes a sign-up and switches the post to CLOSED. Returns the sign-up count."""

    update_session(session["id"], active=0, closed_at=now_utc())

    await refresh_signup_message(guild, session["id"])

    return get_signup_count(session["id"])


async def close_due_signups():
    """Sign-ups with a closing time close by themselves."""

    for session in query(
        "SELECT * FROM signup_sessions WHERE active = 1 AND closes_at IS NOT NULL AND closes_at <= ?",
        (int(time.time()),)
    ):
        guild = bot.get_guild(session["guild_id"])

        if guild:
            await close_signup(guild, session)
        else:
            update_session(session["id"], active=0, closed_at=now_utc())


def restore_signup_buttons():

    sessions = query("SELECT id FROM signup_sessions WHERE active = 1")

    for session in sessions:
        try:
            bot.add_view(SignupView(session["id"]))
        except Exception as error:
            print(f"Could not restore sign-up {session['id']}: {error}")

    print(f"Loaded {len(sessions)} open sign-up(s).")


# ============================================================
# WHO GETS THE DM
# ============================================================

MAX_CARD_FIGHTERS = 60
DM_DELAY_SECONDS = 1.0  # gentle on Discord, avoids being blocked

MENTION_PATTERN = re.compile(r"<@!?(\d+)>")


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

    fighters = get_card_fighters(notice["id"])
    checked = sum(1 for row in fighters if row["checked_in_at"])

    def icon(row):
        if row["checked_in_at"]:
            return "✅"
        if row["dm_status"] == "failed":
            return "⚠️"
        return "⏳"

    lines = [
        f"`{number:>2}` {icon(row)} <@{row['user_id']}>"
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
        lines.append("⚠️ **Not checked in yet:** " + " ".join(f"<@{row['user_id']}>" for row in waiting))
    else:
        lines.append("✅ Everyone has checked in.")

    if failed:
        lines.append("")
        lines.append("📭 **Couldn't DM:** " + ", ".join(f"<@{user_id}> ({why})" for user_id, why in failed))

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
                lines.append(f"• <@{user_id}> ({reason})")
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

    for user_id, _name, member in fighters:
        line = f"• <@{user_id}>"
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
# COMMANDS: SIGN-UPS
# ============================================================

@bot.tree.command(
    name="livesignup",
    description="Post a Live Card sign-up with Sign Up and Withdraw buttons."
)
@app_commands.describe(
    event="Optional: event name for the title, e.g. UFC Universe Live Card 7",
    starts="Optional: when the card starts, UK time, e.g. 07/11/2026 20:00 (or any text)",
    closes="Optional: when sign-ups close by themselves, UK time, e.g. 05/11/2026 18:00",
    card_out="Optional: when the card will be out, e.g. 06/11/2026 12:00 (or any text)"
)
async def livesignup(
    interaction: discord.Interaction,
    event: str = None,
    starts: str = None,
    closes: str = None,
    card_out: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can create sign-ups.", ephemeral=True)
        return

    if get_active_session(interaction.guild.id):
        await interaction.response.send_message(
            "❌ There's already an open sign-up. Close it first with `/signupclose`.",
            ephemeral=True
        )
        return

    closes_at, error = read_closing_time(closes)

    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    await interaction.response.send_message("Creating sign-up...")

    message = await interaction.original_response()

    session_id = query("""
        INSERT INTO signup_sessions
        (guild_id, message_id, channel_id, active, created_at, closes_at, card_out, event_name, starts_at)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
    """, (
        interaction.guild.id,
        message.id,
        message.channel.id,
        now_utc(),
        closes_at,
        read_time_value(card_out),
        (event or "").strip()[:80] or None,
        read_time_value(starts)
    ), write=True)

    await message.edit(
        content=None,
        embed=create_signup_embed(session_id),
        view=SignupView(session_id)
    )


@bot.tree.command(
    name="signuptimes",
    description="Change the open sign-up's event name, start time, closing time or card-out time."
)
@app_commands.describe(
    event="Event name for the title ('none' to remove)",
    starts="When the card starts, UK time ('none' to remove)",
    closes="When sign-ups close, UK time ('none' to remove)",
    card_out="When the card will be out, a date or any text ('none' to remove)"
)
async def signuptimes(
    interaction: discord.Interaction,
    event: str = None,
    starts: str = None,
    closes: str = None,
    card_out: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can change sign-ups.", ephemeral=True)
        return

    session = get_active_session(interaction.guild.id)

    if not session:
        await interaction.response.send_message("❌ There's no open sign-up.", ephemeral=True)
        return

    if event is None and starts is None and closes is None and card_out is None:
        await interaction.response.send_message(
            "❌ Fill in at least one of **event**, **starts**, **closes** or **card_out**.",
            ephemeral=True
        )
        return

    changes = {}

    if closes is not None:
        closes_at, error = read_closing_time(closes)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        changes["closes_at"] = closes_at

    if event is not None:
        changes["event_name"] = None if is_clear_word(event) else event.strip()[:80]

    if starts is not None:
        changes["starts_at"] = read_time_value(starts)

    if card_out is not None:
        changes["card_out"] = read_time_value(card_out)

    update_session(session["id"], **changes)

    session = get_session(session["id"])

    await interaction.response.send_message(
        f"✅ Updated **{signup_title(session)}**:\n"
        + ("\n".join(signup_time_lines(session)) or "Times removed."),
        ephemeral=True
    )

    await refresh_signup_message(interaction.guild, session["id"])


@bot.tree.command(
    name="signupclose",
    description="Close the open sign-up."
)
async def signupclose(
    interaction: discord.Interaction
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can close sign-ups.", ephemeral=True)
        return

    session = get_active_session(interaction.guild.id)

    if not session:
        await interaction.response.send_message("❌ There's no open sign-up.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    count = await close_signup(interaction.guild, session)

    await interaction.followup.send(
        f"✅ Sign-up closed.\n\n**Sign-Ups Closed! [{count}]!**",
        ephemeral=True
    )


@bot.tree.command(
    name="signuppaste",
    description="Post the sign-up list (the open one, or the last one if it's closed)."
)
@app_commands.describe(
    event="Event name for the title (default: the one set on /livesignup)",
    starts="When the card starts, UK time (default: the one set on /livesignup)"
)
async def signuppaste(
    interaction: discord.Interaction,
    event: str = None,
    starts: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can paste the sign-up list.", ephemeral=True)
        return

    session = get_active_session(interaction.guild.id)

    if not session:
        rows = query(
            "SELECT * FROM signup_sessions WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (interaction.guild.id,)
        )
        session = rows[0] if rows else None

    if not session:
        await interaction.response.send_message("❌ No sign-up has been posted yet. Use `/livesignup`.", ephemeral=True)
        return

    signups = get_signups(session["id"])

    if not signups:
        await interaction.response.send_message("❌ Nobody has signed up yet.", ephemeral=True)
        return

    await interaction.response.defer()

    lines = []

    for number, signup in enumerate(signups, start=1):

        member = await get_member(interaction.guild, signup["discord_user_id"])

        if member:
            lines.append(f"**{number}.** [{member.display_name}](https://discord.com/users/{member.id})")
        else:
            lines.append(f"**{number}.** {signup['player_name']} *(left the server)*")

    title = f"{event.strip()} Sign-Ups" if event and event.strip() else signup_title(session)

    start_text = time_value_text(read_time_value(starts)) if starts and starts.strip() else time_value_text(session["starts_at"])
    header = [f"🗓️ **Card starts:** {start_text}", ""] if start_text else []

    embed = discord.Embed(
        title=title,
        description="\n".join(header + lines)[:4096],
        color=BRAND_RED,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=SIGNUP_BANNER,
        author_text=f"{BRAND_TAG} • LIVE CARD • FIGHTER LIST"
    )

    embed.set_footer(
        text=f"🥊 Total Sign-Ups: {len(signups)}",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.followup.send(embed=embed)


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
        "round": int(round_match.group(1)) if round_match else None
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
                 method, round, fought_at, card_name, imported_by, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id, result["key"], result.get("channel_id"), result.get("message_id"),
                result["winner_id"], result["loser_id"], result["method"],
                result.get("round"), result.get("fought_at"), result.get("card"),
                imported_by, now_utc()
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
                f"<@{result['winner_id']}> def. <@{result['loser_id']}> · "
                f"{round_text}{result['method']}"
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

        self.stop()

        await interaction.response.edit_message(
            content=f"✅ Imported **{saved}** result(s). Check `/serverstats`.",
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

                result.update(
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

    embed = create_scan_embed(channel, new_results, already, skipped, card)

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


@bot.tree.command(
    name="profile",
    description="A fighter's wins: KOs, submissions and decisions."
)
@app_commands.describe(member="Who to show (default: you)")
async def profile(
    interaction: discord.Interaction,
    member: discord.Member = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can view profiles for now.", ephemeral=True)
        return

    member = member or interaction.user

    counts = method_counts(interaction.guild.id, member.id)
    wins = sum(counts.values())
    finishes = counts["KO"] + counts["SUB"]

    if wins:
        description = (
            f"**{wins}** win{'s' if wins != 1 else ''} · **{percent(finishes, wins)}** finishes\n\n"
            f"**KO/TKO** {counts['KO']}\n"
            f"**Submission** {counts['SUB']}\n"
            f"**Decision** {counts['DEC']}"
        )
    else:
        description = "No wins logged yet."

    recent = recent_fights(interaction.guild.id, member.id)

    if recent:
        description += "\n\n**Recent fights**\n" + "\n".join(
            fight_line(row, member.id) for row in recent
        )

    embed = discord.Embed(
        title=member.display_name,
        description=description,
        color=BRAND_RED,
        timestamp=datetime.now(timezone.utc)
    )

    apply_branding(embed, banner_file=PROFILE_BANNER, author_text=f"{BRAND_TAG} • FIGHTER PROFILE")

    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(
        text=f"{wins} win{'s' if wins != 1 else ''} logged",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.response.send_message(embed=embed)


def recent_fights(
    guild_id: int,
    member_id: int,
    limit: int = 5
):

    return query(
        """
        SELECT * FROM fight_results
        WHERE guild_id = ? AND (winner_id = ? OR loser_id = ?)
        ORDER BY fought_at DESC, id DESC
        LIMIT ?
        """,
        (guild_id, member_id, member_id, limit)
    )


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
    member_id: int = None
):
    """
    '✅ W vs @Amir Albazi · R3 SUB · UFC Universe 12 · 04/11/2026' on a profile,
    or '@A def. @B · R3 SUB · …' when no member is given.
    """

    if member_id is None:
        start = f"<@{row['winner_id']}> def. <@{row['loser_id']}>"
    elif row["winner_id"] == member_id:
        start = f"✅ **W** vs <@{row['loser_id']}>"
    else:
        start = f"❌ **L** vs <@{row['winner_id']}>"

    parts = [start, (f"R{row['round']} " if row["round"] else "") + row["method"]]

    if row["card_name"]:
        parts.append(row["card_name"])

    date = fight_date_text(row["fought_at"])

    if date:
        parts.append(date)

    return " · ".join(parts)


@bot.tree.command(
    name="logfight",
    description="Log a fight by hand: winner, loser, how it ended, card and date."
)
@app_commands.describe(
    winner="Who won",
    loser="Who lost",
    method="How it ended",
    round="Round it ended in (leave empty for a decision)",
    card="Card name, e.g. UFC Universe 12",
    date="Fight date, e.g. 04/11/2026 (default: today)"
)
@app_commands.choices(method=[
    app_commands.Choice(name="KO/TKO", value="KO"),
    app_commands.Choice(name="Submission", value="SUB"),
    app_commands.Choice(name="Decision", value="DEC")
])
async def logfight(
    interaction: discord.Interaction,
    winner: discord.Member,
    loser: discord.Member,
    method: app_commands.Choice[str],
    round: app_commands.Range[int, 1, 5] = None,
    card: app_commands.Range[str, 1, 80] = None,
    date: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can log fights.", ephemeral=True)
        return

    if winner.id == loser.id:
        await interaction.response.send_message("❌ The winner and loser can't be the same person.", ephemeral=True)
        return

    try:
        fought_at = read_scan_date(date) if date else datetime.now(timezone.utc)
    except ValueError:
        await interaction.response.send_message("❌ Use a date like `04/11/2026`.", ephemeral=True)
        return

    key = f"manual:{interaction.id}"

    save_results(
        interaction.guild.id,
        [{
            "key": key,
            "winner_id": winner.id,
            "loser_id": loser.id,
            "method": method.value,
            "round": round,
            "fought_at": fought_at.isoformat(),
            "card": card.strip() if card else None
        }],
        interaction.user.id
    )

    row = query(
        "SELECT * FROM fight_results WHERE guild_id = ? AND result_key = ?",
        (interaction.guild.id, key)
    )[0]

    embed = discord.Embed(
        description=f"{fight_line(row)}\n\nLogged by {interaction.user.mention}",
        color=BRAND_RED
    )

    apply_branding(embed, author_text=f"{BRAND_TAG} • FIGHT LOGGED")

    embed.set_footer(
        text=f"Fight #{row['id']} • /removefight {row['id']} to undo",
        icon_url=asset_url(LOGO_FILE)
    )

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

    query("DELETE FROM fight_results WHERE id = ?", (fight,), write=True)

    await interaction.response.send_message(
        f"🗑️ Removed fight #{fight}: {fight_line(rows[0])}",
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
# BACKGROUND CHECKS (every minute)
# ============================================================

@tasks.loop(minutes=1)
async def background_checks():

    try:
        await close_due_signups()
    except Exception as error:
        print(f"Sign-up closer error: {error}")

    try:
        await check_card_reminders()
    except Exception as error:
        print(f"Reminder error: {error}")


@background_checks.before_loop
async def before_background_checks():
    await bot.wait_until_ready()


# ============================================================
# STARTUP
#
# Runs once when the bot starts (not on every reconnect):
#   1. set up the database
#   2. restore the Check In and Sign Up buttons
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

    restore_checkin_buttons()
    restore_signup_buttons()

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
