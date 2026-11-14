"""
title: Web Browser
author: vlad
author_url: https://esmien.ru
description: Открывает веб-страницы в headless Chromium (сервер Playwright) и возвращает их текст. Используй, когда нужен точный текст страницы, а не сниппет поиска, или когда сайт отдаёт пустую страницу без JS.
required_open_webui_version: 0.5.0
version: 1.0.0
license: MIT
"""

import asyncio

from pydantic import BaseModel, Field

MAX_DEFAULT = 12000
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0 Safari/537.36"


class Tools:
    class Valves(BaseModel):
        ws_url: str = Field(
            default="ws://playwright:3000",
            description="WebSocket-адрес сервера Playwright (контейнер playwright в stack-net).",
        )
        timeout_ms: int = Field(default=30000, description="Таймаут навигации, мс.")
        max_chars: int = Field(default=MAX_DEFAULT, description="Сколько символов текста возвращать.")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def browse_page(
        self,
        url: str,
        wait_for_selector: str = "",
        max_chars: int = 0,
        __event_emitter__=None,
    ) -> str:
        """
        Открыть страницу в headless-браузере и получить её текст.

        :param url: абсолютный адрес страницы (http/https).
        :param wait_for_selector: CSS-селектор, появления которого нужно дождаться (для SPA).
        :param max_chars: ограничение длины ответа (0 — значение из настроек).
        :return: заголовок, описание и основной текст страницы.
        """
        from playwright.async_api import async_playwright

        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "Ошибка: нужен абсолютный http(s)-адрес."
        limit = max_chars or self.valves.max_chars

        async def emit(msg: str):
            if __event_emitter__:
                try:
                    await __event_emitter__({"type": "status", "data": {"description": msg, "done": False}})
                except Exception:
                    pass

        await emit(f"Открываю {url}")
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.connect(self.valves.ws_url, timeout=self.valves.timeout_ms)
                try:
                    ctx = await browser.new_context(user_agent=UA, locale="ru-RU", viewport={"width": 1366, "height": 900})
                    page = await ctx.new_page()
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=self.valves.timeout_ms)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        if wait_for_selector:
                            try:
                                await page.wait_for_selector(wait_for_selector, timeout=self.valves.timeout_ms)
                            except Exception:
                                pass
                        title = (await page.title()).strip()
                        text = await page.evaluate(
                            """() => {
                                const bad = 'script,style,noscript,svg,iframe,canvas';
                                const root = document.querySelector('article') || document.querySelector('main') || document.body;
                                const clone = root.cloneNode(true);
                                clone.querySelectorAll(bad).forEach(n => n.remove());
                                return clone.innerText;
                            }"""
                        )
                        desc = ""
                        try:
                            desc = (await page.evaluate(
                                "() => (document.querySelector('meta[name=\"description\"]')||{}).content || ''"
                            )) or ""
                        except Exception:
                            pass
                    finally:
                        await ctx.close()
                finally:
                    await browser.close()
        except Exception as e:
            return f"Ошибка загрузки {url}: {type(e).__name__}: {e}"

        text = "\n".join(line.rstrip() for line in text.splitlines())
        text = "\n".join(line for line in text.split("\n\n") if line.strip())
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")

        head = []
        if title:
            head.append(f"# {title}")
        if desc:
            head.append(f"_{desc[:300]}_")
        body = text.strip()
        full = "\n\n".join(head + [body])
        if len(full) > limit:
            full = full[:limit] + f"\n\n[...обрезано на {limit} символах]"

        await emit(f"Готово: {len(full)} символов")
        if __event_emitter__:
            try:
                await __event_emitter__({"type": "status", "data": {"description": "", "done": True}})
            except Exception:
                pass
        return full or "Страница не содержит текста."
