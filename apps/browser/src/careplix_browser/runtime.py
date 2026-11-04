"""Browser sessions: one isolated Chromium context per run (Playwright).

Each session has its own context (cookies, storage), a fixed viewport, and an optional saved
storage state to restore a staff sign-in. Actions arrive in screenshot pixel coordinates.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

log = logging.getLogger("careplix.browser")

VIEWPORT = {"width": 1280, "height": 800}
IDLE_TIMEOUT_S = 30 * 60
ACTION_TIMEOUT_MS = 15_000

# What a person or the agent may ask the browser to do.
KINDS = {
    "click",
    "double_click",
    "right_click",
    "triple_click",
    "hover",
    "type",
    "key",
    "scroll",
    "drag",
    "navigate",
    "back",
    "forward",
    "wait",
}

# Key names from the models (xdotool-like and Gemini) to Playwright's.
_KEYS = {
    "return": "Enter",
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": "Space",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "home": "Home",
    "end": "End",
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "shift": "Shift",
    "cmd": "Meta",
    "super": "Meta",
    "meta": "Meta",
}


def playwright_key(combo: str) -> str:
    """'ctrl+a' → 'Control+a'; 'Return' → 'Enter'."""
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    out = []
    for p in parts:
        mapped = _KEYS.get(p.lower())
        if mapped:
            out.append(mapped)
        elif len(p) == 1:
            out.append(p)
        elif p[0].lower() == "f" and p[1:].isdigit():
            out.append(p.upper())
        else:
            out.append(p[:1].upper() + p[1:])
    return "+".join(out)


# Reads the element under a point (or the focused one) without changing the page.
_INSPECT_JS = """
([x, y, focused]) => {
  let el = focused ? document.activeElement : document.elementFromPoint(x, y);
  if (!el) return null;
  const pick = el.closest('button, a, input, select, textarea, label, [role], [onclick]') || el;
  const text = (pick.innerText || pick.value || pick.getAttribute('aria-label') ||
                pick.getAttribute('title') || pick.getAttribute('alt') || '').trim().slice(0, 160);
  const form = pick.form || pick.closest('form');
  return {
    tag: pick.tagName.toLowerCase(),
    role: pick.getAttribute('role') || '',
    type: (pick.getAttribute('type') || '').toLowerCase(),
    name: pick.getAttribute('name') || pick.getAttribute('id') || '',
    text,
    href: pick.getAttribute('href') || '',
    is_password: (pick.getAttribute('type') || '').toLowerCase() === 'password',
    in_form: !!form,
    editable: pick.isContentEditable ||
      ['input', 'textarea', 'select'].includes(pick.tagName.toLowerCase()),
  };
}
"""


class ActionError(Exception):
    pass


@dataclass
class Session:
    id: str
    context: BrowserContext
    page: Page
    allowed_hosts: list[str]
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_used = time.monotonic()


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def host_allowed(host: str, allowed: list[str]) -> bool:
    return any(host == a or host.endswith("." + a) for a in allowed)


class Runtime:
    def __init__(self, executable_path: str | None = None, headless: bool = True):
        self._executable_path = executable_path or None
        self._headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.sessions: dict[str, Session] = {}
        self._start_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        async with self._start_lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=self._headless,
                    executable_path=self._executable_path,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            return self._browser

    async def open(
        self, start_url: str, allowed_hosts: list[str], storage_state: dict | None = None
    ) -> Session:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport=VIEWPORT,
            storage_state=storage_state or None,
            accept_downloads=False,
            locale="en-US",
        )
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        page = await context.new_page()
        session = Session(uuid.uuid4().hex, context, page, [h.lower() for h in allowed_hosts if h])
        self.sessions[session.id] = session
        # New tabs (target=_blank) are folded back into the session's single page.
        context.on("page", lambda p: asyncio.ensure_future(self._adopt(session, p)))
        if start_url:
            await self._goto(session, start_url)
        return session

    async def _adopt(self, session: Session, page: Page) -> None:
        if page is session.page:
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
            url = page.url
            await page.close()
            if url and url != "about:blank":
                await self._goto(session, url)
        except Exception:  # a popup that closed itself
            pass

    def get(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.touch()
        return session

    async def close(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            await session.context.close()

    async def close_idle(self) -> int:
        now = time.monotonic()
        idle = [s.id for s in self.sessions.values() if now - s.last_used > IDLE_TIMEOUT_S]
        for sid in idle:
            await self.close(sid)
        return len(idle)

    async def shutdown(self) -> None:
        for sid in list(self.sessions):
            await self.close(sid)
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    # --- reading ---------------------------------------------------------------------------------

    async def screenshot(self, session: Session, fmt: str = "png") -> dict[str, Any]:
        kwargs: dict[str, Any] = {"type": "jpeg", "quality": 60} if fmt == "jpeg" else {}
        data = await session.page.screenshot(**kwargs)
        return {
            "image": base64.b64encode(data).decode(),
            "media_type": "image/jpeg" if fmt == "jpeg" else "image/png",
            "url": session.page.url,
            "title": await session.page.title(),
            "width": VIEWPORT["width"],
            "height": VIEWPORT["height"],
        }

    async def inspect(
        self, session: Session, x: int | None = None, y: int | None = None
    ) -> dict | None:
        focused = x is None or y is None
        try:
            return await session.page.evaluate(_INSPECT_JS, [x or 0, y or 0, focused])
        except Exception:
            return None

    async def storage_state(self, session: Session) -> dict:
        return await session.context.storage_state()

    # --- acting ----------------------------------------------------------------------------------

    async def _goto(self, session: Session, url: str) -> None:
        host = host_of(url)
        if session.allowed_hosts and not host_allowed(host, session.allowed_hosts):
            raise ActionError(f"{host} is not one of this run's sites")
        try:
            await session.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            raise ActionError(f"could not open {host}: {type(exc).__name__}") from exc

    async def act(self, session: Session, action: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("kind")
        if kind not in KINDS:
            raise ActionError(f"unknown action {kind!r}")
        page, mouse = session.page, session.page.mouse
        x, y = action.get("x"), action.get("y")

        def point() -> tuple[float, float]:
            if x is None or y is None:
                raise ActionError(f"{kind} needs a position")
            if not (0 <= x <= VIEWPORT["width"] and 0 <= y <= VIEWPORT["height"]):
                raise ActionError("position is outside the page")
            return float(x), float(y)

        async with session.lock:
            try:
                if kind in ("click", "double_click", "right_click", "triple_click"):
                    px, py = point()
                    modifiers = action.get("modifiers") or []
                    for m in modifiers:
                        await page.keyboard.down(playwright_key(m))
                    clicks = {"double_click": 2, "triple_click": 3}.get(kind, 1)
                    button = "right" if kind == "right_click" else "left"
                    await mouse.click(px, py, button=button, click_count=clicks)
                    for m in reversed(modifiers):
                        await page.keyboard.up(playwright_key(m))
                elif kind == "hover":
                    await mouse.move(*point())
                elif kind == "type":
                    if x is not None and y is not None:
                        await mouse.click(*point())
                        if action.get("clear_first"):
                            await page.keyboard.press("Control+a")
                            await page.keyboard.press("Backspace")
                    await page.keyboard.type(str(action.get("text", "")), delay=15)
                    if action.get("press_enter"):
                        await page.keyboard.press("Enter")
                elif kind == "key":
                    for _ in range(max(1, min(int(action.get("repeat") or 1), 20))):
                        await page.keyboard.press(playwright_key(str(action.get("text", ""))))
                elif kind == "scroll":
                    if x is not None and y is not None:
                        await mouse.move(*point())
                    amount = max(1, min(int(action.get("amount") or 3), 20)) * 120
                    dx, dy = {
                        "up": (0, -amount),
                        "down": (0, amount),
                        "left": (-amount, 0),
                        "right": (amount, 0),
                    }.get(str(action.get("direction", "down")), (0, amount))
                    await mouse.wheel(dx, dy)
                elif kind == "drag":
                    sx, sy = action.get("start_x"), action.get("start_y")
                    if sx is None or sy is None:
                        raise ActionError("drag needs a start position")
                    await mouse.move(float(sx), float(sy))
                    await mouse.down()
                    await mouse.move(*point(), steps=10)
                    await mouse.up()
                elif kind == "navigate":
                    await self._goto(session, str(action.get("url", "")))
                elif kind == "back":
                    await page.go_back(wait_until="domcontentloaded")
                elif kind == "forward":
                    await page.go_forward(wait_until="domcontentloaded")
                elif kind == "wait":
                    await asyncio.sleep(max(0.0, min(float(action.get("seconds") or 2), 10.0)))
                # Let navigation or rendering triggered by the action settle a little.
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                await asyncio.sleep(0.4)
            except ActionError:
                raise
            except Exception as exc:
                raise ActionError(f"{kind} failed: {type(exc).__name__}") from exc

        host = host_of(page.url)
        left_sites = bool(session.allowed_hosts) and not host_allowed(host, session.allowed_hosts)
        return {"url": page.url, "title": await page.title(), "left_allowed_sites": left_sites}
