"""
UFC Universe bot: Live Card DMs with check-in.

Commands (staff only):
  /dmlivecard   DM everyone you @mention that they're on a Live Card,
                with a Check In button in the DM
  /livecheckin  post a board showing who has checked in (updates by
                itself); close:True closes check-in
"""

import asyncio
import hashlib
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
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
        "Helper,Moderator,Vice President,Owner"
    ).split(",")
    if name.strip()
]


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


def asset_url(
    filename: str
):

    if not ASSET_BASE_URL:
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

# No privileged intents needed.
intents = discord.Intents.default()

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

    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row

    return connection


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

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM card_notices WHERE id = ?", (notice_id,))
    row = cursor.fetchone()
    db.close()

    return row


def get_latest_notice(
    guild_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM card_notices
        WHERE guild_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (guild_id,))
    row = cursor.fetchone()
    db.close()

    return row


def get_active_notices():

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM card_notices WHERE active = 1")
    rows = cursor.fetchall()
    db.close()

    return rows


def get_card_fighters(
    notice_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM card_fighters
        WHERE notice_id = ?
        ORDER BY position ASC
    """, (notice_id,))
    rows = cursor.fetchall()
    db.close()

    return rows


def get_card_fighter(
    notice_id: int,
    user_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM card_fighters
        WHERE notice_id = ? AND user_id = ?
    """, (notice_id, user_id))
    row = cursor.fetchone()
    db.close()

    return row


def update_card_fighter(
    notice_id: int,
    user_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        f"UPDATE card_fighters SET {columns} WHERE notice_id = ? AND user_id = ?",
        (*changes.values(), notice_id, user_id)
    )
    db.commit()
    db.close()


def update_notice(
    notice_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        f"UPDATE card_notices SET {columns} WHERE id = ?",
        (*changes.values(), notice_id)
    )
    db.commit()
    db.close()


def get_meta(
    key: str
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT value FROM bot_meta WHERE key = ?", (key,))
    row = cursor.fetchone()
    db.close()

    return row["value"] if row else None


def set_meta(
    key: str,
    value: str
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
        (key, value)
    )
    db.commit()
    db.close()


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
    "we know you are going to show."
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

        for index, (user_id, name, member) in enumerate(self.fighters):

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

        await interaction.edit_original_response(
            content="\n".join(lines)[:2000]
        )


def create_dm_preview_embed(
    fighters: list,
    event: str,
    event_date: str
):

    lines = []

    for user_id, name, member in fighters:
        line = f"• <@{user_id}>"
        if member is None:
            line += " ⚠️ not in server"
        lines.append(line)

    details = (
        f"**Event:** {event}\n"
        f"**When:** {format_saved_time(event_date)}\n"
        "**Check-in button:** included"
    )

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
# COMMANDS
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
# STARTUP
#
# Runs once when the bot starts (not on every reconnect):
#   1. set up the database
#   2. restore the Check In buttons on open check-ins
#   3. register the slash commands with Discord, only when this
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
