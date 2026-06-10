"""Minimal SellAuth API client — just what the bot needs."""

import aiohttp

API_BASE = "https://api.sellauth.com/v1"


class SellAuth:
    def __init__(self, session: aiohttp.ClientSession, shop_id: str, api_key: str):
        self.session = session
        self.shop_id = shop_id
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def get_invoice(self, invoice_id: str):
        """Fetch a single invoice (status, items, price, custom fields...)."""
        url = f"{API_BASE}/shops/{self.shop_id}/invoices/{invoice_id}"
        async with self.session.get(url, headers=self.headers) as r:
            if r.status != 200:
                return None
            return await r.json()

    async def process_invoice(self, invoice_id: str):
        """Complete + auto-deliver an invoice. Returns (ok, message)."""
        url = f"{API_BASE}/shops/{self.shop_id}/invoices/{invoice_id}/process"
        async with self.session.get(url, headers=self.headers) as r:
            try:
                data = await r.json()
            except aiohttp.ContentTypeError:
                data = {}
            if r.status == 200:
                return True, "processed"
            return False, data.get("error", f"HTTP {r.status}")
