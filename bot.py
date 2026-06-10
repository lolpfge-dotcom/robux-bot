"""
Robux Payment Bot for Discord (SellAuth / wezzy.store)
------------------------------------------------------
Customers pay via a button panel:
  click "Pay with Robux" -> enter Order ID + Roblox username -> buy the
  gamepass -> the bot detects the Robux landing in your sales feed and tells
  SellAuth to deliver the order automatically.

It only READS your own Roblox sales feed (read-only) via your .ROBLOSECURITY
cookie. It never moves Robux or touches buyers' accounts.

Admin commands: /panel (post the panel), /pending (view open orders).
"""

import os
import json
import math
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from sellauth import SellAuth
from curl_cffi.requests import AsyncSession as _CurlSession

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")
ROBLOX_USER_ID = int(os.getenv("ROBLOX_USER_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))   # gets DM'd on critical issues
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
PENDING_TTL_DAYS = int(os.getenv("PENDING_TTL_DAYS", "14"))   # auto-drop abandoned orders after N days

SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID")

DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
MOCK_SALES_FILE = os.getenv("MOCK_SALES_FILE")

BASE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(BASE, "products.json")
STATE_FILE = os.path.join(BASE, "state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("robuxbot")

PANEL_BUTTON_ID = "wezzy_pay_panel_button"

# ---- panel look & feel (all optional; sensible defaults) -----------------
STORE_URL = os.getenv("STORE_URL", "https://wezzy.store").strip()
# Embed accent line. Change the hex to taste (e.g. 0xFFFFFF white, 0x111111 near-black).
PANEL_COLOR = int(os.getenv("PANEL_COLOR", "E3E3E3"), 16)
# Wide image shown across the bottom of the panel (your store banner).
PANEL_BANNER_URL = os.getenv("PANEL_BANNER_URL", "").strip()
# Small logo shown top-right + next to the title/footer (your store icon).
PANEL_THUMB_URL = os.getenv("PANEL_THUMB_URL", "").strip()


# ---------------------------------------------------------------- config / state

def load_products() -> dict:
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": [], "pending": []}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)   # atomic: never leaves a half-written state file


PRODUCTS = load_products()
STATE = load_state()


def product_for_sellauth_id(sellauth_product_id):
    for key, p in PRODUCTS.items():
        if p.get("sellauth_product_id") == sellauth_product_id:
            return key, p
    return None, None


def _too_old(order: dict) -> bool:
    """True if a pending order is older than PENDING_TTL_DAYS (or has a bad/missing date)."""
    ts = order.get("created")
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return True
    if dt.tzinfo is None:                 # older entries were saved without a timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days >= PENDING_TTL_DAYS


# ---------------------------------------------------------------- roblox api
# Roblox blocks ordinary Python HTTP clients via TLS fingerprinting, so every
# Roblox call goes through curl_cffi impersonating Chrome, with a FRESH session
# per call (a reused session replays Set-Cookies and 500s the next request).

class RobloxAuthError(Exception):
    """Raised when the .ROBLOSECURITY cookie is rejected (expired/invalid)."""


async def resolve_username(username: str):
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": False}
    async with _CurlSession(impersonate="chrome", timeout=20) as s:
        r = await s.post(url, json=payload)
        if r.status_code != 200:
            return None
        data = r.json()
    if not data.get("data"):
        return None
    u = data["data"][0]
    return u["id"], u["name"]


def _normalize(items):
    out = []
    for t in items:
        details = t.get("details") or {}
        agent = t.get("agent") or {}
        out.append({
            "id": str(t.get("idHash") or t.get("id")),
            "buyer_id": agent.get("id"),
            "buyer_name": agent.get("name"),
            "target_id": details.get("id"),
            "target_type": details.get("type"),
            "created": t.get("created"),
        })
    return out


async def get_recent_sales():
    # TEST MODE: read fake sales from a local file instead of hitting Roblox
    if MOCK_SALES_FILE and os.path.exists(MOCK_SALES_FILE):
        with open(MOCK_SALES_FILE, "r", encoding="utf-8") as f:
            return _normalize(json.load(f).get("data", []))

    url = (f"https://economy.roblox.com/v2/users/{ROBLOX_USER_ID}/transactions"
           f"?limit=25&transactionType=Sale")
    headers = {"Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"}
    async with _CurlSession(impersonate="chrome", timeout=20) as s:
        r = await s.get(url, headers=headers)
    if r.status_code == 401:
        raise RobloxAuthError("Roblox cookie invalid/expired (401)")
    if r.status_code != 200:
        raise RuntimeError(f"sales fetch HTTP {r.status_code}")
    return _normalize(r.json().get("data", []))


# ---------------------------------------------------------------- bot

class RobuxBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None
        self.sellauth: SellAuth | None = None
        self._cookie_alerted = False   # so we alert once, not every 20s

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        if SELLAUTH_API_KEY and SELLAUTH_SHOP_ID:
            self.sellauth = SellAuth(self.session, SELLAUTH_SHOP_ID, SELLAUTH_API_KEY)
            log.info("SellAuth integration enabled (shop %s).", SELLAUTH_SHOP_ID)
        self.add_view(PayPanelView())   # persistent: panel button survives restarts
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        self.check_sales.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def dm(self, discord_id: int, text: str):
        try:
            user = await self.fetch_user(discord_id)
            await user.send(text)
        except discord.HTTPException:
            log.warning("Could not DM %s (DMs closed?).", discord_id)

    async def alert_admin(self, text: str):
        """Critical notice -> DM the admin if set, else the log channel."""
        log.warning("ALERT: %s", text)
        try:
            if ADMIN_USER_ID:
                await self.dm(ADMIN_USER_ID, text)
            elif LOG_CHANNEL_ID:
                ch = self.get_channel(LOG_CHANNEL_ID)
                if ch:
                    await ch.send(text)
        except discord.HTTPException as e:
            log.error("Failed to send admin alert: %s", e)

    async def fulfill_sellauth(self, order: dict):
        """Confirm payment -> tell SellAuth to deliver."""
        inv_id = order["sellauth_invoice_id"]
        discord_id = int(order["discord_id"])
        ok, msg = await self.sellauth.process_invoice(inv_id)
        already = (not ok) and isinstance(msg, str) and "complet" in msg.lower()
        if ok or already:
            page = order.get("order_url") or STORE_URL
            await self.dm(discord_id,
                          f"✅ **Payment confirmed!** Your order **#{inv_id}** has been delivered.\n"
                          f"👉 Check your email or refresh your order page on wezzy.store to grab it: {page}")
        else:
            await self.dm(discord_id,
                          f"⚠️ Robux received, but auto-delivery failed ({msg}). "
                          f"An admin will help you shortly.")
            await self.alert_admin(
                f"⚠️ Order **#{inv_id}** was paid via Robux but SellAuth processing "
                f"failed: `{msg}`. Process it manually.")
        if LOG_CHANNEL_ID:
            ch = self.get_channel(LOG_CHANNEL_ID)
            if ch:
                ico = "✅" if (ok or already) else "⚠️"
                await ch.send(f"{ico} Invoice **#{inv_id}** via Robux "
                              f"(`{order['roblox_name']}`) — {msg}")

    @tasks.loop(seconds=POLL_SECONDS)
    async def check_sales(self):
        # drop abandoned orders so the pending list stays clean over time
        fresh = [o for o in STATE["pending"] if not _too_old(o)]
        if len(fresh) != len(STATE["pending"]):
            STATE["pending"] = fresh
            save_state(STATE)
        if not STATE["pending"]:
            return

        try:
            sales = await get_recent_sales()
        except RobloxAuthError:
            if not self._cookie_alerted:
                self._cookie_alerted = True
                await self.alert_admin(
                    "🚨 **Roblox cookie expired.** The bot can't see payments until you "
                    "refresh `ROBLOX_COOKIE` in `.env` and restart. Orders are NOT being "
                    "auto-completed right now.")
            return
        except Exception as e:
            log.warning("Sales poll error (will retry): %s", e)
            return

        if self._cookie_alerted:   # recovered
            self._cookie_alerted = False
            await self.alert_admin("✅ Roblox cookie is working again — payments are processing normally.")

        processed = set(STATE["processed"])
        for sale in sales:
            if sale["id"] in processed or sale.get("target_type") != "GamePass":
                continue

            match = next((o for o in STATE["pending"]
                          if o["roblox_id"] == sale["buyer_id"]
                          and o.get("gamepass_id") == sale["target_id"]), None)
            if not match:
                continue

            # Mark processed + drop from pending and PERSIST *before* fulfilling.
            # If we crash mid-fulfill, restart won't double-deliver.
            processed.add(sale["id"])
            STATE["processed"] = list(processed)[-2000:]
            try:
                STATE["pending"].remove(match)
            except ValueError:
                pass
            save_state(STATE)

            if DRY_RUN:
                log.info("[DRY_RUN] MATCH invoice #%s for %s",
                         match.get("sellauth_invoice_id"), match["roblox_name"])
                continue
            try:
                await self.fulfill_sellauth(match)
            except Exception as e:
                log.error("Fulfillment crashed for invoice %s: %s",
                          match.get("sellauth_invoice_id"), e)
                await self.alert_admin(
                    f"⚠️ Payment detected for order **#{match.get('sellauth_invoice_id')}** "
                    f"but fulfillment errored: `{e}`. Check it manually.")

    @check_sales.before_loop
    async def before_check_sales(self):
        await self.wait_until_ready()


client = RobuxBot()


# ---------------------------------------------------------------- payment flow

def invoice_checkout_url(inv: dict):
    """Build the customer's order page link: wezzy.store/checkout/{salt}-{paddedID}."""
    uid = inv.get("unique_id")
    if not uid:
        salt, iid = inv.get("salt"), inv.get("id")
        if salt and iid is not None:
            uid = f"{salt}-{int(iid):013d}"
    if not uid:
        return None
    return f"{STORE_URL.rstrip('/')}/checkout/{uid}"


async def start_payment(interaction: discord.Interaction, invoice_id: str, roblox_username: str):
    """Shared logic behind the panel: look up the order, hand back a gamepass link."""
    await interaction.response.defer(ephemeral=True)
    invoice_id = invoice_id.strip()
    roblox_username = roblox_username.strip()

    if not client.sellauth:
        await interaction.followup.send("Payments aren't configured. Contact an admin.", ephemeral=True)
        return

    inv = await client.sellauth.get_invoice(invoice_id)
    if not inv:
        await interaction.followup.send("❌ Order not found. Double-check your **Order ID**.", ephemeral=True)
        return
    if inv.get("status") == "completed":
        await interaction.followup.send("✅ This order is already paid.", ephemeral=True)
        return

    gamepass_id, pname = None, None
    for item in inv.get("items", []):
        _, p = product_for_sellauth_id(item.get("product_id"))
        if p:
            gamepass_id, pname = p["gamepass_id"], p["name"]
            break
    if not gamepass_id:
        await interaction.followup.send(
            "This product isn't set up for Robux payment yet. Contact an admin.", ephemeral=True)
        return

    resolved = await resolve_username(roblox_username)
    if not resolved:
        await interaction.followup.send(
            f"❌ Roblox user `{roblox_username}` not found. Check the spelling.", ephemeral=True)
        return
    roblox_id, exact = resolved

    STATE["pending"] = [o for o in STATE["pending"] if o.get("sellauth_invoice_id") != invoice_id]
    STATE["pending"].append({
        "discord_id": str(interaction.user.id),
        "roblox_id": roblox_id,
        "roblox_name": exact,
        "gamepass_id": gamepass_id,
        "sellauth_invoice_id": invoice_id,
        "order_url": invoice_checkout_url(inv),
        "created": datetime.now(timezone.utc).isoformat(),
    })
    save_state(STATE)

    link = f"https://www.roblox.com/game-pass/{gamepass_id}/"
    await interaction.followup.send(
        f"🛒 **Order #{invoice_id}** — {pname}\nRoblox account: `{exact}`\n\n"
        f"**1.** Buy this gamepass: {link}\n"
        f"**2.** Your order delivers automatically within {POLL_SECONDS}s of purchase.\n\n"
        f"⚠️ Buy with the account `{exact}`, and keep your **DMs open** so we can deliver.",
        ephemeral=True)


class PayModal(discord.ui.Modal, title="Pay with Robux"):
    order_id = discord.ui.TextInput(label="Order ID", placeholder="From your wezzy.store checkout",
                                    required=True, max_length=64)
    roblox_username = discord.ui.TextInput(label="Roblox Username",
                                           placeholder="The account you'll buy with",
                                           required=True, max_length=32)

    async def on_submit(self, interaction: discord.Interaction):
        await start_payment(interaction, str(self.order_id), str(self.roblox_username))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("PayModal error: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong — please try again.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong — please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


def build_panel_embed() -> discord.Embed:
    """The customer-facing payment panel. Looks good with or without images set."""
    embed = discord.Embed(
        title="Pay with Robux",
        description=(
            "Complete your **wezzy.store** order with **Robux** — instant, automatic, secure.\n"
            "Tap the button below and follow the steps. Your product is delivered the moment "
            "your payment is confirmed.\n\u200b"
        ),
        color=PANEL_COLOR,
    )
    embed.set_author(name="wezzy.store", url=STORE_URL, icon_url=PANEL_THUMB_URL or None)
    embed.add_field(
        name="📋  How it works",
        value=(
            ">>> **1**  Tap **Pay with Robux** below\n"
            "**2**  Enter your **Order ID** + **Roblox username**\n"
            "**3**  Buy the gamepass link the bot sends you\n"
            "**4**  Done — your order is delivered automatically ✅"
        ),
        inline=False,
    )
    embed.add_field(
        name="🧾  No Order ID yet?",
        value=f"Check out at [**wezzy.store**]({STORE_URL}) and pick **Pay with Robux**.",
        inline=True,
    )
    embed.add_field(
        name="⚠️  Heads up",
        value="Keep your Discord **DMs open** so we can deliver.",
        inline=True,
    )
    if PANEL_THUMB_URL:
        embed.set_thumbnail(url=PANEL_THUMB_URL)
    if PANEL_BANNER_URL:
        embed.set_image(url=PANEL_BANNER_URL)
    embed.set_footer(text="Wezzy Pay  •  Automated Robux checkout",
                     icon_url=PANEL_THUMB_URL or None)
    return embed


class PayPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)   # persistent
        if STORE_URL.startswith("http"):
            self.add_item(discord.ui.Button(
                label="Visit Store",
                style=discord.ButtonStyle.link,
                url=STORE_URL,
                emoji="🛒",
            ))

    @discord.ui.button(label="Pay with Robux", style=discord.ButtonStyle.success,
                       emoji="💎", custom_id=PANEL_BUTTON_ID)
    async def pay_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PayModal())


# ---------------------------------------------------------------- admin commands

@client.tree.command(name="panel", description="Post the Pay-with-Robux panel in this channel (admin)")
async def panel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.channel.send(embed=build_panel_embed(), view=PayPanelView())
    await interaction.response.send_message("Panel posted. ✅", ephemeral=True)


@client.tree.command(name="pending", description="View open orders (admin)")
async def pending(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not STATE["pending"]:
        await interaction.response.send_message("No open orders.", ephemeral=True)
        return
    lines = [f"• <@{o['discord_id']}> → Invoice **#{o.get('sellauth_invoice_id')}** "
             f"(Roblox `{o['roblox_name']}`)" for o in STATE["pending"]]
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@client.event
async def on_ready():
    log.info("Logged in as %s | watching Roblox ID %s", client.user, ROBLOX_USER_ID)


def fee_price(net_robux: int) -> int:
    """Gamepass price needed to NET `net_robux` after Roblox's 30% fee."""
    return math.ceil(net_robux / 0.7)


if __name__ == "__main__":
    if not all([TOKEN, ROBLOX_COOKIE, ROBLOX_USER_ID]):
        raise SystemExit("Missing DISCORD_TOKEN, ROBLOX_COOKIE or ROBLOX_USER_ID in .env")
    client.run(TOKEN)
