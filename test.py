"""
Test helper for the Robux bot.

Usage:
  python test.py          -> checks all credentials/connections (no bot needed)
  python test.py mock     -> writes mock_sales.json that matches your first
                             pending order, so the bot (in DRY_RUN) "completes" it
"""

import os
import sys
import json
import asyncio

import aiohttp
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

OK = "\033[92m✓\033[0m"
BAD = "\033[91m✗\033[0m"
INFO = "•"

ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")
ROBLOX_USER_ID = os.getenv("ROBLOX_USER_ID")
SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
MOCK_FILE = os.path.join(BASE, "mock_sales.json")


async def check_env():
    print("\n== Environment ==")
    for name, val in [("DISCORD_TOKEN", DISCORD_TOKEN),
                      ("ROBLOX_COOKIE", ROBLOX_COOKIE),
                      ("ROBLOX_USER_ID", ROBLOX_USER_ID)]:
        print(f"  {OK if val else BAD} {name} {'set' if val else 'MISSING'}")
    for name, val in [("SELLAUTH_API_KEY", SELLAUTH_API_KEY),
                      ("SELLAUTH_SHOP_ID", SELLAUTH_SHOP_ID)]:
        print(f"  {OK if val else INFO} {name} {'set' if val else '(optional, not set)'}")


async def check_roblox():
    print("\n== Roblox ==")
    if not ROBLOX_COOKIE:
        print(f"  {BAD} no cookie, skipping")
        return
    cookie = {"Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"}

    # 1) cookie valid + who am I  (fresh session each call — see bot.py note)
    async with AsyncSession(impersonate="chrome", timeout=20) as s:
        r = await s.get("https://users.roblox.com/v1/users/authenticated", headers=cookie)
    if r.status_code != 200:
        print(f"  {BAD} cookie invalid (HTTP {r.status_code}) — refresh ROBLOX_COOKIE")
        return
    me = r.json()
    print(f"  {OK} cookie valid — logged in as {me['name']} (id {me['id']})")
    if ROBLOX_USER_ID and str(me["id"]) != str(ROBLOX_USER_ID):
        print(f"  {BAD} ROBLOX_USER_ID ({ROBLOX_USER_ID}) != logged-in id ({me['id']})")
    else:
        print(f"  {OK} ROBLOX_USER_ID matches")

    # 2) can we read the sales feed
    url = (f"https://economy.roblox.com/v2/users/{ROBLOX_USER_ID}/transactions"
           f"?limit=10&transactionType=Sale")
    async with AsyncSession(impersonate="chrome", timeout=20) as s:
        r = await s.get(url, headers=cookie)
    if r.status_code == 200:
        n = len(r.json().get("data", []))
        print(f"  {OK} sales feed readable ({n} recent sale(s) visible)")
    else:
        print(f"  {BAD} sales feed not readable (HTTP {r.status_code})")

    # 3) username resolve works
    async with AsyncSession(impersonate="chrome", timeout=20) as s:
        r = await s.post("https://users.roblox.com/v1/usernames/users",
                         json={"usernames": ["Roblox"], "excludeBannedUsers": False})
    ok = r.status_code == 200 and r.json().get("data")
    print(f"  {OK if ok else BAD} username resolve works")


async def check_sellauth(session):
    print("\n== SellAuth ==")
    if not (SELLAUTH_API_KEY and SELLAUTH_SHOP_ID):
        print(f"  {INFO} not configured, skipping (/pay flow disabled)")
        return
    headers = {"Authorization": f"Bearer {SELLAUTH_API_KEY}", "Accept": "application/json"}
    url = f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/invoices?perPage=1"
    async with session.get(url, headers=headers) as r:
        if r.status == 200:
            print(f"  {OK} API key + shop id valid (invoices endpoint reachable)")
        elif r.status in (401, 403):
            print(f"  {BAD} auth failed (HTTP {r.status}) — check SELLAUTH_API_KEY")
        else:
            print(f"  {BAD} unexpected HTTP {r.status} — check SELLAUTH_SHOP_ID")


def make_mock():
    """Build mock_sales.json from the first pending order in state.json."""
    if not os.path.exists(STATE_FILE):
        print(f"{BAD} no state.json yet — run /buy or /pay in Discord first.")
        return
    state = json.load(open(STATE_FILE, encoding="utf-8"))
    if not state.get("pending"):
        print(f"{BAD} no pending orders — run /buy or /pay in Discord first.")
        return
    o = state["pending"][0]
    products = json.load(open(os.path.join(BASE, "products.json"), encoding="utf-8"))
    gp = o.get("gamepass_id") or products.get(o.get("product"), {}).get("gamepass_id")
    mock = {"data": [{
        "id": 999999999,                       # fake transaction id
        "agent": {"id": o["roblox_id"], "type": "User", "name": o["roblox_name"]},
        "details": {"id": gp, "type": "GamePass", "name": "TEST PASS"},
        "currency": {"amount": 1, "type": "Robux"},
        "created": "2026-01-01T00:00:00Z",
    }]}
    json.dump(mock, open(MOCK_FILE, "w", encoding="utf-8"), indent=2)
    print(f"{OK} wrote mock_sales.json matching order for {o['roblox_name']} "
          f"(gamepass {gp}).")
    print("  Now run the bot with MOCK_SALES_FILE=mock_sales.json and DRY_RUN=true.")
    print("  Within POLL_SECONDS you should see a [DRY_RUN] MATCH line in the log.")


async def main():
    await check_env()
    await check_roblox()
    async with aiohttp.ClientSession() as session:
        await check_sellauth(session)
    print("\nDone.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "mock":
        make_mock()
    else:
        asyncio.run(main())
