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


def save_products(products: dict) -> None:
    tmp = PRODUCTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
    os.replace(tmp, PRODUCTS_FILE)   # atomic write, same as state


PRODUCTS = load_products()
STATE = load_state()


def product_for_sellauth_id(sellauth_product_id):
    for key, p in PRODUCTS.items():
        if p.get("sellauth_product_id") == sellauth_product_id:
            return key, p
    return None, None


def find_entry(sellauth_id, variant_id=None):
    """Locate a configured entry by exact product id (+ optional variant id)."""
    for key, p in PRODUCTS.items():
        if p.get("sellauth_product_id") == sellauth_id and \
           (p.get("sellauth_variant_id") or None) == (variant_id or None):
            return key, p
    return None, None


def product_for_item(item):
    """Match a SellAuth invoice item to a config entry: an exact variant entry
    first, then a product-level entry (no variant) as a catch-all."""
    pid, vid = item.get("product_id"), item.get("variant_id")
    if vid is not None:
        key, p = find_entry(pid, vid)
        if p:
            return key, p
    return find_entry(pid, None)


def product_pool(p) -> list:
    """A product's gamepass pool, supporting both the old single-gamepass format
    (gamepass_id) and the new pool format (gamepass_ids)."""
    ids = p.get("gamepass_ids")
    if ids:
        return [int(g) for g in ids]
    one = p.get("gamepass_id")
    return [int(one)] if one else []


def _ensure_pool(p) -> list:
    """Make sure p stores a gamepass_ids list, migrating an old single gamepass_id."""
    if not isinstance(p.get("gamepass_ids"), list):
        old = p.get("gamepass_id")
        p["gamepass_ids"] = [int(old)] if old else []
    p.pop("gamepass_id", None)
    return p["gamepass_ids"]


def order_required(o) -> list:
    """The gamepasses a pending order must have paid (supports the old single-id format)."""
    ids = o.get("gamepass_ids")
    if ids:
        return list(ids)
    return [o["gamepass_id"]] if o.get("gamepass_id") else []


def order_paid(o) -> list:
    """The gamepasses already paid for this order (created on demand)."""
    if not isinstance(o.get("paid_ids"), list):
        o["paid_ids"] = []
    return o["paid_ids"]


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


async def roblox_owns_gamepass(user_id: int, gp_id: int):
    """True = owns it, False = doesn't, None = couldn't tell (caller shouldn't block on None)."""
    url = f"https://inventory.roblox.com/v1/users/{user_id}/items/GamePass/{gp_id}"
    headers = {"Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"}
    try:
        async with _CurlSession(impersonate="chrome", timeout=20) as s:
            r = await s.get(url, headers=headers)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        return len(r.json().get("data", [])) > 0
    except Exception:
        return None


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

            # find a pending order from this buyer that still needs this gamepass
            match = None
            for o in STATE["pending"]:
                if o.get("roblox_id") == sale["buyer_id"] \
                        and sale["target_id"] in order_required(o) \
                        and sale["target_id"] not in order_paid(o):
                    match = o
                    break
            if not match:
                continue

            # record this gamepass as paid + mark the sale processed, persist BEFORE delivering
            processed.add(sale["id"])
            STATE["processed"] = list(processed)[-2000:]
            paid = order_paid(match)
            paid.append(sale["target_id"])
            req = order_required(match)
            done = len(set(paid)) >= len(set(req))
            if done:
                try:
                    STATE["pending"].remove(match)
                except ValueError:
                    pass
            save_state(STATE)

            if DRY_RUN:
                log.info("[DRY_RUN] payment %d/%d for invoice #%s",
                         len(set(paid)), len(set(req)), match.get("sellauth_invoice_id"))
                continue

            if done:
                try:
                    await self.fulfill_sellauth(match)
                except Exception as e:
                    log.error("Fulfillment crashed for invoice %s: %s",
                              match.get("sellauth_invoice_id"), e)
                    await self.alert_admin(
                        f"⚠️ Payment detected for order **#{match.get('sellauth_invoice_id')}** "
                        f"but fulfillment errored: `{e}`. Check it manually.")
            else:
                await self.dm(int(match["discord_id"]),
                    f"✅ Payment **{len(set(paid))}/{len(set(req))}** received for order "
                    f"**#{match.get('sellauth_invoice_id')}** — buy the remaining gamepass(es) "
                    f"to finish and we'll deliver automatically.")

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


async def pick_gamepass(pool, buyer_id, exclude=()):
    """Choose a gamepass the buyer doesn't already own (so repeat buyers get a fresh one).
    Returns (gamepass_id, status) where status is 'ok', 'all_owned', or 'empty'."""
    if not pool:
        return None, "empty"
    candidates = [g for g in pool if g not in exclude]
    unknown = []
    for g in candidates:
        owned = await roblox_owns_gamepass(buyer_id, g)
        if owned is False:
            return g, "ok"
        if owned is None:
            unknown.append(g)
    if unknown:                 # couldn't verify ownership — proceed rather than block
        return unknown[0], "ok"
    return None, "all_owned"    # every candidate is owned (or all were excluded)


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

    # find the matching product entry + total quantity ordered for it
    pool, pname, qty, first_key = [], None, 0, None
    for item in inv.get("items", []):
        key, p = product_for_item(item)
        if not p:
            continue
        if first_key is None:
            first_key, pool, pname = key, product_pool(p), p["name"]
        if key == first_key:
            qty += int(item.get("quantity") or item.get("qty") or 1)
    qty = max(1, qty)
    log.info("Order #%s -> %s ×%d | items=%s", invoice_id, pname, qty,
             [(it.get("product_id"), it.get("variant_id"), it.get("quantity"))
              for it in inv.get("items", [])])

    if not pool:
        await interaction.followup.send(
            "This product isn't set up for Robux payment yet. Contact an admin.", ephemeral=True)
        await client.alert_admin(
            f"⚠️ Someone tried to pay for order **#{invoice_id}** but its product has no "
            f"gamepasses set up. Add some with `/gamepass_add`.")
        return

    resolved = await resolve_username(roblox_username)
    if not resolved:
        await interaction.followup.send(
            f"❌ Roblox user `{roblox_username}` not found. Check the spelling.", ephemeral=True)
        return
    roblox_id, exact = resolved

    # pick `qty` gamepasses this buyer doesn't already own (one per unit)
    busy = set()
    for o in STATE["pending"]:
        if o.get("roblox_id") == roblox_id:
            busy.update(order_required(o))
    chosen = []
    for _ in range(qty):
        gp, st = await pick_gamepass(pool, roblox_id, exclude=busy | set(chosen))
        if st != "ok":
            break
        chosen.append(gp)
    if len(chosen) < qty:
        await interaction.followup.send(
            f"⚠️ This order is for **{qty}× {pname}**, but there aren't enough free gamepasses "
            f"for `{exact}` to cover it right now (the account may already own some). "
            f"Please contact an admin.", ephemeral=True)
        await client.alert_admin(
            f"⚠️ Order #{invoice_id}: needed {qty} gamepasses for **{pname}** but only "
            f"{len(chosen)} available for `{exact}`. Add more with `/gamepass_add`.")
        return

    STATE["pending"] = [o for o in STATE["pending"] if o.get("sellauth_invoice_id") != invoice_id]
    STATE["pending"].append({
        "discord_id": str(interaction.user.id),
        "roblox_id": roblox_id,
        "roblox_name": exact,
        "gamepass_ids": chosen,
        "paid_ids": [],
        "sellauth_invoice_id": invoice_id,
        "order_url": invoice_checkout_url(inv),
        "created": datetime.now(timezone.utc).isoformat(),
    })
    save_state(STATE)

    if qty == 1:
        body = (f"**1.** Buy this gamepass: https://www.roblox.com/game-pass/{chosen[0]}/\n"
                f"**2.** Your order delivers automatically within {POLL_SECONDS}s of purchase.")
    else:
        links = "\n".join(f"• https://www.roblox.com/game-pass/{g}/" for g in chosen)
        body = (f"This order is for **{qty} items**, so please buy **all {qty}** gamepasses below "
                f"(one each):\n{links}\n\nYour order delivers automatically once **all {qty}** are paid.")
    await interaction.followup.send(
        f"🛒 **Order #{invoice_id}** — {pname} ×{qty}\nRoblox account: `{exact}`\n\n"
        f"{body}\n\n"
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


@client.tree.command(name="product_set",
                     description="Add or update a product/variant's name & price (admin)")
@app_commands.describe(
    sellauth_id="The product ID from your wezzy.store",
    variant_id="The variant ID (optional — leave blank for the whole product)",
    name="A label, just for you (optional)",
    price_robux="Robux price, for your reference (optional)",
    gamepass_id="Optionally add a first gamepass to its pool (optional)",
)
async def product_set(interaction: discord.Interaction,
                      sellauth_id: int,
                      variant_id: int = None,
                      name: str = None,
                      price_robux: int = None,
                      gamepass_id: int = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    key, existing = find_entry(sellauth_id, variant_id)
    if existing:
        if name is not None:
            existing["name"] = name
        if price_robux is not None:
            existing["price_robux"] = price_robux
        action = "Updated"
    else:
        key = f"{sellauth_id}:{variant_id}" if variant_id else str(sellauth_id)
        default_name = f"Product {sellauth_id}" + (f" / variant {variant_id}" if variant_id else "")
        PRODUCTS[key] = {
            "name": name or default_name,
            "gamepass_ids": [],
            "price_robux": price_robux,
            "sellauth_product_id": sellauth_id,
            "sellauth_variant_id": variant_id,
            "deliver_message": "Delivered automatically by wezzy.store.",
            "give_role_id": None,
            "stock": None,
        }
        action = "Added"

    p = PRODUCTS[key]
    pool = _ensure_pool(p)
    if gamepass_id is not None and gamepass_id not in pool:
        pool.append(gamepass_id)

    try:
        save_products(PRODUCTS)
    except Exception as e:
        log.exception("save_products failed")
        await interaction.response.send_message(f"⚠️ Couldn't save: {e}", ephemeral=True)
        return

    price = p.get("price_robux")
    await interaction.response.send_message(
        f"✅ **{action}:** {p['name']}\n"
        f"• Store product ID: `{sellauth_id}`"
        + (f"  •  variant `{variant_id}`" if variant_id else "  •  whole product") + "\n"
        f"• Price: {price if price is not None else '—'} R$\n"
        f"• Gamepasses in pool: {len(pool)}\n"
        f"_Add more gamepasses with_ `/gamepass_add`.",
        ephemeral=True,
    )


@client.tree.command(name="gamepass_add", description="Add a gamepass to a product/variant pool (admin)")
@app_commands.describe(sellauth_id="The store product ID", gamepass_id="The gamepass ID to add",
                       variant_id="The variant ID (optional)")
async def gamepass_add(interaction: discord.Interaction, sellauth_id: int, gamepass_id: int,
                       variant_id: int = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    key, p = find_entry(sellauth_id, variant_id)
    if not p:
        await interaction.response.send_message(
            f"No entry for product `{sellauth_id}`"
            + (f" variant `{variant_id}`" if variant_id else "")
            + ". Make it first with `/product_set`.", ephemeral=True)
        return
    pool = _ensure_pool(p)
    if gamepass_id in pool:
        await interaction.response.send_message(
            f"Gamepass `{gamepass_id}` is already in **{p['name']}**'s pool.", ephemeral=True)
        return
    pool.append(gamepass_id)
    try:
        save_products(PRODUCTS)
    except Exception as e:
        log.exception("save_products failed")
        await interaction.response.send_message(f"⚠️ Couldn't save: {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ Added gamepass `{gamepass_id}` to **{p['name']}**. Pool now has {len(pool)}.",
        ephemeral=True)


@client.tree.command(name="gamepass_remove", description="Remove a gamepass from a product/variant pool (admin)")
@app_commands.describe(sellauth_id="The store product ID", gamepass_id="The gamepass ID to remove",
                       variant_id="The variant ID (optional)")
async def gamepass_remove(interaction: discord.Interaction, sellauth_id: int, gamepass_id: int,
                          variant_id: int = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    key, p = find_entry(sellauth_id, variant_id)
    if not p:
        await interaction.response.send_message(
            f"No entry for product `{sellauth_id}`"
            + (f" variant `{variant_id}`" if variant_id else "") + ".", ephemeral=True)
        return
    pool = _ensure_pool(p)
    if gamepass_id not in pool:
        await interaction.response.send_message(
            f"Gamepass `{gamepass_id}` isn't in **{p['name']}**'s pool.", ephemeral=True)
        return
    pool.remove(gamepass_id)
    try:
        save_products(PRODUCTS)
    except Exception as e:
        log.exception("save_products failed")
        await interaction.response.send_message(f"⚠️ Couldn't save: {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"🗑️ Removed `{gamepass_id}` from **{p['name']}**. Pool now has {len(pool)}.",
        ephemeral=True)


@client.tree.command(name="product_list", description="List all Robux products (admin)")
async def product_list(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not PRODUCTS:
        await interaction.response.send_message(
            "No products set up yet. Add one with `/product_set`.", ephemeral=True)
        return

    embed = discord.Embed(title="🎟️  Robux Products", color=PANEL_COLOR)
    for p in list(PRODUCTS.values())[:25]:
        price = p.get("price_robux")
        pool = product_pool(p)
        name = p.get("name", "(unnamed)")
        if p.get("sellauth_variant_id"):
            name += f"  ·  variant {p['sellauth_variant_id']}"
        gp_list = "  ".join(f"`{g}`" for g in pool) if pool else "*none yet — `/gamepass_add`*"
        value = (
            f"🆔  Store ID `{p.get('sellauth_product_id')}`\n"
            f"💰  Price `{price if price is not None else '—'} R$`\n"
            f"🎮  Gamepasses ({len(pool)}): {gp_list}"
        )
        embed.add_field(name=f"📦  {name}", value=value[:1024], inline=False)

    embed.set_footer(text=f"{len(PRODUCTS)} product(s) · edit with /product_set, /gamepass_add")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="product_remove", description="Remove a product/variant entry (admin)")
@app_commands.describe(sellauth_id="The store product ID to remove",
                       variant_id="The variant ID (optional)")
async def product_remove(interaction: discord.Interaction, sellauth_id: int, variant_id: int = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    key, existing = find_entry(sellauth_id, variant_id)
    if not existing:
        await interaction.response.send_message(
            f"No entry for product `{sellauth_id}`"
            + (f" variant `{variant_id}`" if variant_id else "") + ".", ephemeral=True)
        return
    name = existing.get("name", key)
    del PRODUCTS[key]
    try:
        save_products(PRODUCTS)
    except Exception as e:
        log.exception("save_products failed")
        await interaction.response.send_message(f"⚠️ Couldn't save: {e}", ephemeral=True)
        return
    await interaction.response.send_message(f"🗑️ Removed **{name}**.", ephemeral=True)


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
