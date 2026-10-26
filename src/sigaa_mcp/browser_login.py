"""Login assistido: abre uma janela do navegador para o usuário logar e
captura o cookie JSESSIONID quando o portal do discente abre.

O usuário digita a senha e resolve o CAPTCHA; este módulo só espera e lê o
cookie. Usa um perfil próprio e persistente, então o navegador pode lembrar a
senha e os próximos logins ficam mais rápidos.
"""

from __future__ import annotations

import asyncio
import os

from playwright.async_api import async_playwright

from .client import BASE_URL, HOST, STATE_DIR

LOGIN_URL = f"{BASE_URL}/sigaa/verTelaLogin.do"
TIMEOUT_S = int(os.environ.get("SIGAA_LOGIN_TIMEOUT", "300"))
# Edge já vem no Windows; em outros sistemas usa o Chromium do Playwright.
CHANNEL = os.environ.get("SIGAA_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "") or None
PROFILE_DIR = STATE_DIR / "browser-profile"


async def capture_jsessionid() -> str | None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, channel=CHANNEL, locale="pt-BR"
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(LOGIN_URL)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + TIMEOUT_S
            while "/portais/" not in page.url:
                if page.is_closed() or loop.time() > deadline:
                    return None
                await asyncio.sleep(1)
            for cookie in await ctx.cookies(BASE_URL):
                if cookie["name"] == "JSESSIONID" and HOST in cookie.get("domain", HOST):
                    return cookie["value"]
            return None
        finally:
            await ctx.close()
