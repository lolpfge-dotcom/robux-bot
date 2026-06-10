"""
Robux Payment Bot for Discord  (+ optional SellAuth integration)
----------------------------------------------------------------
Two ways to use it:

  1. Standalone:  /buy  -> deliver a key/role straight from this bot.
  2. SellAuth:    /pay  -> confirm a Robux payment for a SellAuth MANUAL
                  invoice, then call SellAuth's /process endpoint so
                  SellAuth delivers the product itself.

It only READS your own Roblox sales feed (read-only) using your
.ROBLOSECURITY cookie. It never moves Robux or touches buyers' accounts.
"""

import os
import json
import math
import logging
from datetime import datetime

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
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))

SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID")

# Testing knobs
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
MOCK_SALES_FILE = os.getenv("MOCK_SALES_FILE")  # read fake sales from this file instead of Roblox

BASE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(BASE, "products.json")
STATE_FILE = os.path.join(BASE, "state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("robuxbot")


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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


PRODUCTS = load_products()
STATE = load_state()


def order_gamepass(order: dict):
    """Resolve which gamepass an order is waiting on."""
    if order.get("gamepass_id"):
        return order["gamepass_id"]
    return PRODUCTS.get(order.get("product") or "", {}).get("gamepass_id")


def product_for_sellauth_id(sellauth_product_id):
    for key, p in PRODUCTS.items():
        if p.get("sellauth_product_id") == sellauth_product_id:
            return key, p
    return None, None


# ---------------------------------------------------------------- roblox api
# Roblox blocks ordinary Python HTTP clients via TLS fingerprinting, so every
# Roblox call goes through curl_cffi impersonating Chrome. We open a FRESH
# session per call: a reused session keeps Roblox's Set-Cookie responses and
# replays them on the next request, which makes the transactions endpoint 500.


async def resolve_username(session, username: str):
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
    sales = []
    for t in items:
        details = t.get("details") or {}
        agent = t.get("agent") or {}
        sales.append({
            "id": str(t.get("id")),
            "buyer_id": agent.get("id"),
            "buyer_name": agent.get("name"),
            "target_id": details.get("id"),
            "target_type": details.get("type"),
            "created": t.get("created"),
        })
    return sales


async def get_recent_sales(session: aiohttp.ClientSession):
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
        log.error("Roblox cookie invalid/expired (401). Update ROBLOX_COOKIE.")
        return []
    if r.status_code != 200:
        log.warning("Sales fetch failed: HTTP %s", r.status_code)
        return []
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

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        if SELLAUTH_API_KEY and SELLAUTH_SHOP_ID:
            self.sellauth = SellAuth(self.session, SELLAUTH_SHOP_ID, SELLAUTH_API_KEY)
            log.info("SellAuth integration enabled (shop %s).", SELLAUTH_SHOP_ID)
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
        except discord.Forbidden:
            log.warning("Could not DM %s (DMs closed).", discord_id)

    async def deliver_local(self, order: dict):
        """Standalone delivery (no SellAuth): send key/message/role."""
        product = PRODUCTS.get(order.get("product") or "")
        if not product:
            return
        discord_id = int(order["discord_id"])

        delivered_key = None
        stock = product.get("stock")
        if isinstance(stock, list) and stock:
            delivered_key = stock.pop(0)
            with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
                json.dump(PRODUCTS, f, indent=2)

        msg = product.get("deliver_message", "Thanks for your purchase!")
        if delivered_key:
            msg += f"\n\n**Your key:** `{delivered_key}`"
        elif stock is not None:
            msg += "\n\n⚠️ Out of stock — an admin will sort it out shortly."
        await self.dm(discord_id, msg)

        role_id = product.get("give_role_id")
        if role_id and GUILD_ID:
            try:
                guild = self.get_guild(int(GUILD_ID))
                member = guild.get_member(discord_id) or await guild.fetch_member(discord_id)
                role = guild.get_role(int(role_id))
                if member and role:
                    await member.add_roles(role, reason="Robux purchase")
            except discord.HTTPException as e:
                log.warning("Role assign failed: %s", e)

        if LOG_CHANNEL_ID:
            ch = self.get_channel(LOG_CHANNEL_ID)
            if ch:
                await ch.send(
                    f"✅ **Sale fulfilled** — <@{discord_id}> got **{product['name']}** "
                    f"(Roblox: `{order['roblox_name']}`)"
                    + (f", key `{delivered_key}`" if delivered_key else ""))

    async def fulfill_sellauth(self, order: dict):
        """Confirm payment -> tell SellAuth to deliver."""
        inv_id = order["sellauth_invoice_id"]
        discord_id = int(order["discord_id"])
        ok, msg = await self.sellauth.process_invoice(inv_id)
        if ok:
            await self.dm(discord_id,
                          f"✅ Payment confirmed! Order **#{inv_id}** has been delivered. "
                          f"Check your email / order page on wezzy.store.")
        else:
            await self.dm(discord_id,
                          f"⚠️ Robux received, but auto-delivery failed ({msg}). "
                          f"An admin will help you shortly.")
        if LOG_CHANNEL_ID:
            ch = self.get_channel(LOG_CHANNEL_ID)
            if ch:
                ico = "✅" if ok else "⚠️"
                await ch.send(f"{ico} Invoice **#{inv_id}** via Robux "
                              f"(`{order['roblox_name']}`) — {msg}")

    @tasks.loop(seconds=POLL_SECONDS)
    async def check_sales(self):
        if not STATE["pending"]:
            return
        sales = await get_recent_sales(self.session)
        processed = set(STATE["processed"])
        changed = False

        for sale in sales:
            if sale["id"] in processed or sale.get("target_type") != "GamePass":
                continue

            match = None
            for order in STATE["pending"]:
                if (order["roblox_id"] == sale["buyer_id"]
                        and order_gamepass(order) == sale["target_id"]):
                    match = order
                    break

            if match:
                if DRY_RUN:
                    target = (f"SellAuth invoice #{match['sellauth_invoice_id']}"
                              if match.get("sellauth_invoice_id")
                              else f"local product {match.get('product')}")
                    log.info("[DRY_RUN] MATCH for %s (buyer %s) -> would fulfill: %s",
                             match["roblox_name"], sale["buyer_id"], target)
                elif match.get("sellauth_invoice_id") and self.sellauth:
                    await self.fulfill_sellauth(match)
                else:
                    await self.deliver_local(match)
                STATE["pending"].remove(match)
                processed.add(sale["id"])
                changed = True

        if changed:
            STATE["processed"] = list(processed)[-1000:]
            save_state(STATE)

    @check_sales.before_loop
    async def before_check_sales(self):
        await self.wait_until_ready()


client = RobuxBot()


# ---------------------------------------------------------------- slash commands

async def product_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    out = []
    for key, p in PRODUCTS.items():
        if cur in key.lower() or cur in p["name"].lower():
            out.append(app_commands.Choice(name=f"{p['name']} ({p['price_robux']} R$)", value=key))
    return out[:25]


@client.tree.command(name="buy", description="Buy a product directly with Robux (without SellAuth)")
@app_commands.describe(product="Which product?", roblox_username="Your Roblox username")
@app_commands.autocomplete(product=product_autocomplete)
async def buy(interaction: discord.Interaction, product: str, roblox_username: str):
    await interaction.response.defer(ephemeral=True)
    prod = PRODUCTS.get(product)
    if not prod:
        await interaction.followup.send("Unknown product.", ephemeral=True)
        return
    resolved = await resolve_username(client.session, roblox_username)
    if not resolved:
        await interaction.followup.send(f"Roblox user `{roblox_username}` not found.", ephemeral=True)
        return
    roblox_id, exact = resolved

    STATE["pending"] = [o for o in STATE["pending"]
                        if not (o["discord_id"] == str(interaction.user.id)
                                and o.get("product") == product)]
    STATE["pending"].append({
        "discord_id": str(interaction.user.id),
        "roblox_id": roblox_id, "roblox_name": exact,
        "product": product, "gamepass_id": None,
        "sellauth_invoice_id": None,
        "created": datetime.utcnow().isoformat(),
    })
    save_state(STATE)

    link = f"https://www.roblox.com/game-pass/{prod['gamepass_id']}/"
    await interaction.followup.send(
        f"🛒 **{prod['name']}** — **{prod['price_robux']} R$**\nRoblox: `{exact}`\n\n"
        f"1. Buy the gamepass: {link}\n"
        f"2. Auto-delivery within {POLL_SECONDS}s.\n\n"
        f"⚠️ Buy using the account `{exact}` — otherwise it won't match.", ephemeral=True)


@client.tree.command(name="pay", description="Pay your wezzy.store order with Robux")
@app_commands.describe(invoice_id="Your SellAuth invoice ID", roblox_username="Your Roblox username")
async def pay(interaction: discord.Interaction, invoice_id: str, roblox_username: str):
    await interaction.response.defer(ephemeral=True)
    if not client.sellauth:
        await interaction.followup.send("SellAuth is not configured.", ephemeral=True)
        return

    inv = await client.sellauth.get_invoice(invoice_id)
    if not inv:
        await interaction.followup.send("Invoice not found. Is the ID correct?", ephemeral=True)
        return
    if inv.get("status") == "completed":
        await interaction.followup.send("This order is already paid. ✅", ephemeral=True)
        return

    gamepass_id, pname = None, None
    for item in inv.get("items", []):
        _, p = product_for_sellauth_id(item.get("product_id"))
        if p:
            gamepass_id, pname = p["gamepass_id"], p["name"]
            break
    if not gamepass_id:
        await interaction.followup.send(
            "No Robux gamepass is set up for this product. Please contact an admin.",
            ephemeral=True)
        return

    resolved = await resolve_username(client.session, roblox_username)
    if not resolved:
        await interaction.followup.send(f"Roblox user `{roblox_username}` not found.", ephemeral=True)
        return
    roblox_id, exact = resolved

    STATE["pending"] = [o for o in STATE["pending"]
                        if o.get("sellauth_invoice_id") != str(invoice_id)]
    STATE["pending"].append({
        "discord_id": str(interaction.user.id),
        "roblox_id": roblox_id, "roblox_name": exact,
        "product": None, "gamepass_id": gamepass_id,
        "sellauth_invoice_id": str(invoice_id),
        "created": datetime.utcnow().isoformat(),
    })
    save_state(STATE)

    link = f"https://www.roblox.com/game-pass/{gamepass_id}/"
    await interaction.followup.send(
        f"🛒 Order **#{invoice_id}** — {pname}\nRoblox: `{exact}`\n\n"
        f"1. Buy this gamepass: {link}\n"
        f"2. wezzy.store delivers automatically within {POLL_SECONDS}s.\n\n"
        f"⚠️ Buy using `{exact}` — otherwise it won't match.", ephemeral=True)


@client.tree.command(name="pending", description="Open orders (admin)")
async def pending(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not STATE["pending"]:
        await interaction.response.send_message("No open orders.", ephemeral=True)
        return
    lines = []
    for o in STATE["pending"]:
        label = (f"Invoice #{o['sellauth_invoice_id']}" if o.get("sellauth_invoice_id")
                 else PRODUCTS.get(o.get("product"), {}).get("name", o.get("product")))
        lines.append(f"• <@{o['discord_id']}> → {label} (Roblox `{o['roblox_name']}`)")
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
