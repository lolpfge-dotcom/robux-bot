# Robux Payment Bot (Discord) — with optional SellAuth integration

Buyers buy a Roblox **game pass**; this bot watches *your* sales feed and either
delivers a key/role itself (**/buy**) or tells **SellAuth** to deliver a MANUAL
invoice (**/pay**). It only reads your own transaction feed — it never moves
Robux or touches buyers' accounts.

---

## ⚠️ Read first

- **The `.ROBLOSECURITY` cookie = full access to your Roblox account.** Treat it
  like a password. Run only on a machine you trust, never commit `.env`.
- **30% Roblox fee** on game passes. To *net* 100 R$, price the pass at ~143 R$
  (`ceil(net / 0.7)` — see `fee_price()` in `bot.py`).
- **ToS.** Cookie-based automation is against Roblox's Terms; the bot only reads
  your own sales, but the account risk is yours.

---

## Two modes

### Mode A — Standalone (`/buy`)
The bot delivers keys/roles directly. Set `gamepass_id`, `price_robux`,
`deliver_message`, optional `give_role_id`, optional `stock` in `products.json`.

### Mode B — SellAuth (`/pay`)  ← what you asked about
SellAuth stays the store + delivers everything; the bot just confirms the Robux.

1. **SellAuth dashboard** → add a payment method of type **MANUAL**, label it
   "Pay with Robux". In its instructions put:
   *"After checkout, join our Discord and run `/pay <your-invoice-id>`."*
2. For each product you want payable in Robux, create a Roblox game pass priced
   so it nets the product's price, and put the SellAuth **product id** +
   **gamepass id** in `products.json` (`sellauth_product_id`, `gamepass_id`).
3. Get a SellAuth API key (Dashboard → Account → API) and your shop id; put both
   in `.env` (`SELLAUTH_API_KEY`, `SELLAUTH_SHOP_ID`).

Flow at runtime:
- Customer checks out, picks "Pay with Robux" → pending invoice.
- Customer runs `/pay 12345 RobloxName` → bot gives them the gamepass link.
- They buy it → bot sees the sale → calls
  `GET /v1/shops/{shop}/invoices/{id}/process` → **SellAuth delivers**.

Both modes can run at the same time. If SellAuth env vars are absent, `/pay`
just reports it's not configured and `/buy` still works.

---

## Setup

1. `pip install -r requirements.txt`  (Python 3.10+)
2. **Discord bot token** — discord.com/developers → New Application → Bot →
   Reset Token. Enable **Server Members Intent**. Invite with `bot` +
   `applications.commands` scopes (+ *Manage Roles* only if you use role delivery).
3. **Roblox user id** — the number in your profile URL.
4. **`.ROBLOSECURITY` cookie** — DevTools (F12) → Application → Cookies →
   roblox.com → copy `.ROBLOSECURITY`.
5. **Game passes** — create one per product; grab the id from its URL.
6. Fill `products.json` and `.env` (copy from `.env.example`).
7. `python bot.py`

---

## Commands
- `/buy product roblox_username` — standalone purchase + delivery.
- `/pay invoice_id roblox_username` — pay a SellAuth MANUAL invoice with Robux.
- `/pending` — admin: list open orders.

## Gotchas
- Buyers must purchase with the exact account name they entered, or no match.
- A game pass can only be bought once per account — fine for one-off orders.
  For repeat buys use a fresh pass per order, or a developer-product flow.
- `state.json` persists pending orders + processed sale IDs across restarts.
