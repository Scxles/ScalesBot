import os
import json
import time
import random
import logging

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv



# ----------------------------
# Config + Env loading
# ----------------------------

def load_config(path: str = "config.json") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing {path} in project root.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_int_env(key: str) -> int | None:
    v = os.getenv(key)
    if not v:
        return None
    v = v.strip()
    if not v.isdigit():
        raise ValueError(f"Env {key} must be an integer ID, got: {v!r}")
    return int(v)


def activity_from_config(cfg: dict) -> discord.Activity | None:
    act = cfg.get("bot", {}).get("activity")
    if not act:
        return None

    text = act.get("text", "")
    typ = (act.get("type") or "").lower()

    # Map a few common ones
    if typ == "playing":
        return discord.Game(name=text)
    if typ == "listening":
        return discord.Activity(type=discord.ActivityType.listening, name=text)
    if typ == "watching":
        return discord.Activity(type=discord.ActivityType.watching, name=text)
    if typ == "competing":
        return discord.Activity(type=discord.ActivityType.competing, name=text)

    # fallback
    return discord.Game(name=text)


def is_clean(text: str, banned_substrings: list[str]) -> bool:
    if not banned_substrings:
        return True
    lower = text.lower()
    return not any(bad.lower() in lower for bad in banned_substrings)


# ----------------------------
# Bot setup
# ----------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

GUILD_ID = parse_int_env("GUILD_ID")         # optional (dev sync)
OWNER_ID = parse_int_env("OWNER_ID")         # optional (admin override)
ENV = (os.getenv("ENV") or "dev").lower()
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()

cfg = load_config("config.json")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("scalesbot")

intents = discord.Intents.default()  # slash-only MVP
bot = commands.Bot(command_prefix="!", intents=intents)

# Simple per-invoker cooldown store
roast_last_ts: dict[int, float] = {}


# ----------------------------
# Scope / gating helpers
# ----------------------------

def guild_allowed(guild_id: int | None) -> bool:
    allowed = cfg.get("commands", {}).get("allowed_guild_ids", [])
    if not allowed:
        return True
    if guild_id is None:
        return False
    return guild_id in allowed


def channel_allowed(channel_id: int | None) -> bool:
    allowed = cfg.get("commands", {}).get("allowed_channel_ids", [])
    if not allowed:
        return True
    if channel_id is None:
        return False
    return channel_id in allowed


def dms_allowed() -> bool:
    return not bool(cfg.get("commands", {}).get("disable_in_dms", True))


def is_owner(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    return False


async def precheck(interaction: discord.Interaction) -> tuple[bool, str | None]:
    # DM handling
    if interaction.guild is None and not dms_allowed():
        return False, "Not in DMs. Go to a server."

    # Guild restriction
    gid = interaction.guild.id if interaction.guild else None
    if not guild_allowed(gid):
        return False, "Not allowed in this server."

    # Channel restriction
    cid = interaction.channel.id if interaction.channel else None
    if not channel_allowed(cid):
        return False, "Not allowed in this channel."

    return True, None


async def send_error(interaction: discord.Interaction, msg: str):
    ephemeral = bool(cfg.get("commands", {}).get("ephemeral_errors", True))
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(msg, ephemeral=ephemeral)


# ----------------------------
# Events
# ----------------------------

@bot.event
async def on_ready():
    # Presence
    status_str = (cfg.get("bot", {}).get("status") or "online").lower()
    status_map = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "do_not_disturb": discord.Status.dnd,
        "invisible": discord.Status.invisible,
        "offline": discord.Status.offline
    }
    status = status_map.get(status_str, discord.Status.online)
    activity = activity_from_config(cfg)

    await bot.change_presence(status=status, activity=activity)

    # Command sync behavior
    # NOTE: Your commands are registered as GLOBAL commands via @bot.tree.command.
    # If you want fast dev sync to a single guild, you must copy global -> guild first.
    log.info("Tree currently has %d command(s) registered locally", len(bot.tree.get_commands()))

    scope = (cfg.get("commands", {}).get("sync_scope") or "guild").lower()
    if scope == "guild":
        if not GUILD_ID:
            log.warning("sync_scope=guild but no GUILD_ID in .env; syncing globally instead.")
            synced = await bot.tree.sync()
            log.info("Synced %d commands globally", len(synced))
        else:
            guild_obj = discord.Object(id=GUILD_ID)

            # Visibility/debug
            local_global = bot.tree.get_commands()
            local_guild_pre = bot.tree.get_commands(guild=guild_obj)
            log.info("Local global commands: %d", len(local_global))
            log.info("Local guild commands (pre-copy): %d", len(local_guild_pre))

            # Copy global -> guild for fast propagation
            bot.tree.copy_global_to(guild=guild_obj)
            local_guild_post = bot.tree.get_commands(guild=guild_obj)
            log.info("Local guild commands (post-copy): %d", len(local_guild_post))

            # SAFETY: never push an empty set to the guild (it wipes commands).
            if len(local_guild_post) == 0 and len(local_global) > 0:
                log.error("Refusing to sync EMPTY guild command set (would wipe commands). Reinvite bot with applications.commands scope.")
            else:
                synced = await bot.tree.sync(guild=guild_obj)
                log.info("Sync returned %d command(s) for guild %d", len(synced), GUILD_ID)

                try:
                    remote = await bot.tree.fetch_commands(guild=guild_obj)
                    log.info("Remote guild currently has %d command(s)", len(remote))
                except Exception as e:
                    log.warning("Could not fetch remote guild commands: %r", e)
    else:
        synced = await bot.tree.sync()
        log.info("Synced %d commands globally", len(synced))

    log.info("Logged in as %s (%d)", bot.user, bot.user.id)


# ----------------------------
# Slash commands
# ----------------------------

@bot.tree.command(name="ping", description="Latency check.")
async def ping(interaction: discord.Interaction):
    ok, err = await precheck(interaction)
    if not ok:
        await send_error(interaction, err or "Blocked.")
        return
    await interaction.response.send_message(f"Pong. `{round(bot.latency * 1000)}ms`")


@bot.tree.command(name="coinflip", description="Flip a coin.")
async def coinflip(interaction: discord.Interaction):
    ok, err = await precheck(interaction)
    if not ok:
        await send_error(interaction, err or "Blocked.")
        return
    await interaction.response.send_message(random.choice(["Heads.", "Tails."]))


@bot.tree.command(name="userinfo", description="Show info about a user.")
@app_commands.describe(user="User to inspect (defaults to you).")
async def userinfo(interaction: discord.Interaction, user: discord.Member | None = None):
    ok, err = await precheck(interaction)
    if not ok:
        await send_error(interaction, err or "Blocked.")
        return

    user = user or interaction.user
    created = int(user.created_at.timestamp())
    joined = int(user.joined_at.timestamp()) if getattr(user, "joined_at", None) else None

    lines = [
        f"User: {user.mention} (`{user.id}`)",
        f"Created: <t:{created}:F>"
    ]
    if joined:
        lines.append(f"Joined: <t:{joined}:F>")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="avatar", description="Show a user's avatar.")
@app_commands.describe(user="User to show (defaults to you).")
async def avatar(interaction: discord.Interaction, user: discord.User | discord.Member | None = None):
    ok, err = await precheck(interaction)
    if not ok:
        await send_error(interaction, err or "Blocked.")
        return

    target = user or interaction.user
    asset = target.display_avatar
    try:
        url = asset.replace(size=4096).url
    except Exception:
        url = asset.url

    embed = discord.Embed(title=f"Avatar: {target}")
    embed.description = f"[Open full-size]({url})"
    embed.set_image(url=url)
    embed.set_footer(text=f"User ID: {target.id}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Show available commands.")
async def help_cmd(interaction: discord.Interaction):
    # Help should always respond; still show block reason if config would block usage.
    ok, err = await precheck(interaction)
    block_note = None if ok else (err or "Blocked.")

    cmds = sorted(bot.tree.get_commands(), key=lambda c: c.name.lower())

    roast_cd = int(cfg.get("personality", {}).get("roast_cooldown_seconds", 0))
    purge_max = int(cfg.get("limits", {}).get("purge_max", 100))

    def req_note(name: str) -> str:
        if name == "say":
            return " (requires Manage Server or owner)"
        if name == "purge":
            return f" (requires Manage Messages or owner; max {purge_max})"
        if name == "roast" and roast_cd > 0:
            return f" (cooldown {roast_cd}s)"
        return ""

    lines = []
    for c in cmds:
        desc = (c.description or "").strip() or "—"
        lines.append(f"/{c.name} — {desc}{req_note(c.name)}")

    header = []
    if interaction.guild:
        header.append(f"Context: guild `{interaction.guild.id}` / channel `{interaction.channel.id}`")
    else:
        header.append("Context: DM")

    scope = (cfg.get("commands", {}).get("sync_scope") or "guild")
    header.append(f"Sync scope: `{scope}`")
    header.append(f"GUILD_ID env: `{GUILD_ID}`" if GUILD_ID else "GUILD_ID env: (not set)")

    if block_note:
        header.append(f"Note: this context is blocked by config: {block_note}")

    body = "\n".join(header) + "\n\n" + "\n".join(lines)
    if len(body) > 4000:
        body = body[:3990] + "\n…"

    embed = discord.Embed(title="Commands", description=body)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="roast", description="Blunt roast (generic).")
@app_commands.describe(user="Target user")
async def roast(interaction: discord.Interaction, user: discord.Member):
    ok, err = await precheck(interaction)
    if not ok:
        await send_error(interaction, err or "Blocked.")
        return

    personality = cfg.get("personality", {})
    cooldown = int(personality.get("roast_cooldown_seconds", 8))
    now = time.time()
    last = roast_last_ts.get(interaction.user.id, 0.0)
    if now - last < cooldown:
        await send_error(interaction, personality.get("cooldown_msg", "Cooldown."))
        return
    roast_last_ts[interaction.user.id] = now

    if user.id == interaction.user.id:
        msg = personality.get("self_roast", "Roasting yourself? Weird.")
    elif user.bot:
        msg = personality.get("bot_roast", "Pick on someone else.")
    else:
        roasts = personality.get("roasts", [])
        msg = random.choice(roasts) if roasts else "You exist. That's unfortunate."

    banned = personality.get("banned_substrings", [])
    if not is_clean(msg, banned):
        msg = personality.get("blocked_output_msg", "Nope.")

    await interaction.response.send_message(f"{user.mention} — {msg}")


@bot.tree.command(name="say", description="Make the bot repeat text (admin/owner).")
@app_commands.describe(text="What to say")
async def say(interaction: discord.Interaction, text: str):
    ok, err = await precheck(interaction)
    if not ok:
        await send_error(interaction, err or "Blocked.")
        return

    # Permissions: Manage Guild OR owner override
    if interaction.guild is not None:
        perms = interaction.user.guild_permissions
        allowed = perms.manage_guild or is_owner(interaction.user.id)
    else:
        allowed = is_owner(interaction.user.id)

    if not allowed:
        await send_error(interaction, cfg.get("personality", {}).get("no_perms_msg", "No perms."))
        return

    personality = cfg.get("personality", {})
    max_len = int(personality.get("max_say_length", 1500))
    if len(text) > max_len:
        await send_error(interaction, "Too long. Learn brevity.")
        return

    banned = personality.get("banned_substrings", [])
    if not is_clean(text, banned):
        await send_error(interaction, personality.get("blocked_output_msg", "Nope."))
        return

    await interaction.response.send_message(text)


@bot.tree.command(name="purge", description="Delete last N messages (Manage Messages).")
@app_commands.describe(count="Number of messages to delete (1-100).")
async def purge(interaction: discord.Interaction, count: int):
    ok, err = await precheck(interaction)
    if not ok:
        await send_error(interaction, err or "Blocked.")
        return

    if interaction.guild is None:
        await send_error(interaction, "No purging in DMs.")
        return

    if not interaction.user.guild_permissions.manage_messages and not is_owner(interaction.user.id):
        await send_error(interaction, cfg.get("personality", {}).get("no_perms_msg", "No perms."))
        return

    purge_max = int(cfg.get("limits", {}).get("purge_max", 100))
    if count < 1 or count > purge_max:
        await send_error(interaction, f"Count must be 1-{purge_max}. Don’t be dumb.")
        return

    if not isinstance(interaction.channel, discord.TextChannel):
        await send_error(interaction, "Wrong channel type.")
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=count)
    await interaction.followup.send(f"Deleted {len(deleted)} messages. Try behaving.", ephemeral=True)


# ----------------------------
# Global app-command error handler
# ----------------------------

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Don’t leak stack traces to Discord
    log.exception("App command error: %s", error)

    # Common errors
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_error(interaction, "Cooldown. Relax.")
        return

    await send_error(interaction, "Error. Fix your setup.")


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
