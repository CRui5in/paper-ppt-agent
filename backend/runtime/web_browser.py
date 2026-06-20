"""Shared local Playwright browser for URL sources."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Literal

from backend.config import settings

logger = logging.getLogger(__name__)

# Keep the browser owned by this project instead of sharing a user-wide cache.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class RenderedPage:
    requested_url: str
    final_url: str
    title: str
    visible_text: str
    html: str


class BrowserRenderError(RuntimeError):
    """Raised when Chromium cannot produce a usable rendered page."""


@dataclass(frozen=True)
class BrowserInstallStatus:
    state: Literal["idle", "checking", "installing", "ready", "error"]
    progress: int | None
    message: str


class _BrowserRuntime:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max(1, settings.url_browser_concurrency))
        self._install_attempted = False
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_ready = threading.Event()
        self._worker_start_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._install_status = BrowserInstallStatus("idle", None, "")

    def install_status(self) -> BrowserInstallStatus:
        with self._status_lock:
            return self._install_status

    def _set_install_status(
        self,
        state: Literal["idle", "checking", "installing", "ready", "error"],
        *,
        progress: int | None = None,
        message: str = "",
    ) -> None:
        with self._status_lock:
            self._install_status = BrowserInstallStatus(
                state=state,
                progress=progress,
                message=message,
            )

    def _ensure_worker_loop(self) -> asyncio.AbstractEventLoop:
        """Return a persistent subprocess-capable loop for Windows."""
        if self._worker_loop is not None and self._worker_loop.is_running():
            return self._worker_loop
        with self._worker_start_lock:
            if self._worker_loop is not None and self._worker_loop.is_running():
                return self._worker_loop
            self._worker_ready.clear()

            def _worker() -> None:
                loop = asyncio.ProactorEventLoop()
                self._worker_loop = loop
                asyncio.set_event_loop(loop)
                self._worker_ready.set()
                try:
                    loop.run_forever()
                finally:
                    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    loop.close()

            self._worker_thread = threading.Thread(
                target=_worker,
                name="url-playwright",
                daemon=True,
            )
            self._worker_thread.start()
            if not self._worker_ready.wait(timeout=10):
                raise BrowserRenderError(
                    "Timed out while starting the Playwright browser runtime."
                )
            if self._worker_loop is None:
                raise BrowserRenderError("Playwright browser runtime did not start.")
            return self._worker_loop

    async def _on_browser_loop(self, coroutine):
        if sys.platform != "win32":
            return await coroutine
        loop = self._ensure_worker_loop()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return await asyncio.wrap_future(future)

    async def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            self._set_install_status(
                "checking",
                message="Checking the local Chromium runtime.",
            )
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                self._set_install_status(
                    "error",
                    message="The Playwright Python package is not installed.",
                )
                raise BrowserRenderError(
                    "Playwright is not installed. Run `uv sync`, then "
                    "`python -m playwright install chromium`."
                ) from exc
            try:
                self._playwright = await async_playwright().start()
            except Exception as exc:
                self._playwright = None
                self._set_install_status(
                    "error",
                    message=f"Playwright driver could not start: {exc}",
                )
                raise BrowserRenderError(
                    f"Playwright driver could not start: {exc}"
                ) from exc
            try:
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception as exc:
                try:
                    await self._install_chromium()
                    self._browser = await self._playwright.chromium.launch(headless=True)
                except Exception as retry_exc:
                    await self._playwright.stop()
                    self._playwright = None
                    if isinstance(retry_exc, BrowserRenderError):
                        raise retry_exc from exc
                    self._set_install_status(
                        "error",
                        message=f"Chromium could not start: {retry_exc}",
                    )
                    raise BrowserRenderError(
                        f"Playwright Chromium could not start after installation: {retry_exc}"
                    ) from retry_exc
            self._set_install_status(
                "ready",
                progress=100,
                message="Chromium is ready.",
            )
            logger.info("Playwright Chromium started for URL rendering")
            return self._browser

    async def _install_chromium(self) -> None:
        """Install Chromium once, on demand, in the active Python environment."""
        if self._install_attempted:
            self._set_install_status(
                "error",
                message="Chromium automatic installation already failed.",
            )
            raise BrowserRenderError(
                "Chromium is unavailable and its automatic installation already failed. "
                "Restart the backend to retry."
            )
        self._install_attempted = True
        self._set_install_status(
            "installing",
            progress=None,
            message="Downloading Chromium.",
        )
        logger.warning("Playwright Chromium is missing; installing it on first URL use")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output_chunks: list[str] = []

        async def _read_stream(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            tail = ""
            while True:
                chunk = await stream.read(512)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                output_chunks.append(text)
                combined = tail + text
                percentages = re.findall(r"(?<!\d)(\d{1,3})%", combined)
                tail = combined[-32:]
                if percentages:
                    progress = min(99, max(0, int(percentages[-1])))
                    self._set_install_status(
                        "installing",
                        progress=progress,
                        message="Downloading Chromium.",
                    )

        stdout_task = asyncio.create_task(_read_stream(process.stdout))
        stderr_task = asyncio.create_task(_read_stream(process.stderr))
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=600,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            self._set_install_status(
                "error",
                message="Chromium installation timed out after 10 minutes.",
            )
            raise BrowserRenderError(
                "Chromium installation timed out after 10 minutes."
            ) from exc
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        if process.returncode != 0:
            detail = "".join(output_chunks).strip()
            if len(detail) > 1000:
                detail = detail[-1000:]
            self._set_install_status(
                "error",
                message="Chromium automatic installation failed.",
            )
            raise BrowserRenderError(
                "Chromium automatic installation failed"
                + (f": {detail}" if detail else ".")
            )
        self._set_install_status(
            "ready",
            progress=100,
            message="Chromium installation completed.",
        )
        logger.info("Playwright Chromium installed successfully")

    async def render(self, url: str) -> RenderedPage:
        return await self._on_browser_loop(self._render(url))

    async def _render(self, url: str) -> RenderedPage:
        if not url.startswith(("http://", "https://")):
            raise BrowserRenderError("URL must start with http:// or https://")

        async with self._slots:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                java_script_enabled=True,
                user_agent=_USER_AGENT,
                viewport={"width": 1440, "height": 1000},
            )
            page = await context.new_page()
            timeout_ms = max(1, int(settings.url_fetch_timeout * 1000))
            page.set_default_timeout(timeout_ms)
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if response is not None and response.status >= 400:
                    raise BrowserRenderError(
                        f"The page returned HTTP {response.status}."
                    )
                await self._settle_visible_page(page)
                payload = await page.evaluate(
                    """() => ({
                        title: document.title || "",
                        text: document.body ? document.body.innerText : "",
                        html: document.documentElement
                            ? document.documentElement.outerHTML
                            : "",
                    })"""
                )
                visible_text = _normalize_visible_text(str(payload.get("text") or ""))
                if not visible_text:
                    raise BrowserRenderError(
                        "The rendered page contains no visible text."
                    )
                return RenderedPage(
                    requested_url=url,
                    final_url=page.url,
                    title=str(payload.get("title") or "").strip()[:300],
                    visible_text=visible_text,
                    html=str(payload.get("html") or ""),
                )
            except BrowserRenderError:
                raise
            except Exception as exc:
                raise BrowserRenderError(f"Could not render the page: {exc}") from exc
            finally:
                await context.close()

    async def _settle_visible_page(self, page) -> None:
        """Scroll generically and wait until visible text/height stop changing."""
        started = time.monotonic()
        deadline = started + max(
            settings.url_browser_min_wait_seconds,
            settings.url_browser_settle_seconds,
            0.5,
        )
        stable_samples = 0
        previous: tuple[int, int] | None = None
        while time.monotonic() < deadline:
            metrics = await page.evaluate(
                """() => {
                    const body = document.body;
                    const root = document.documentElement;
                    const height = Math.max(
                        body ? body.scrollHeight : 0,
                        root ? root.scrollHeight : 0
                    );
                    const textLength = body ? body.innerText.length : 0;
                    const nextY = Math.min(
                        window.scrollY + Math.max(window.innerHeight * 0.85, 600),
                        Math.max(0, height - window.innerHeight)
                    );
                    window.scrollTo(0, nextY);
                    return { height, textLength };
                }"""
            )
            current = (int(metrics["height"]), int(metrics["textLength"]))
            if current == previous:
                stable_samples += 1
            else:
                stable_samples = 0
                previous = current
            if (
                time.monotonic() - started >= settings.url_browser_min_wait_seconds
                and stable_samples >= 3
            ):
                break
            await page.wait_for_timeout(350)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(150)

    async def close(self) -> None:
        if sys.platform != "win32":
            await self._close_browser()
            return
        loop = self._worker_loop
        thread = self._worker_thread
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._close_browser(), loop)
        await asyncio.wrap_future(future)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
        self._worker_loop = None
        self._worker_thread = None

    async def _close_browser(self) -> None:
        async with self._start_lock:
            browser, self._browser = self._browser, None
            playwright, self._playwright = self._playwright, None
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()


def _normalize_visible_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\xa0", " ").splitlines()]
    output: list[str] = []
    previous_blank = False
    for line in lines:
        if not line:
            if output and not previous_blank:
                output.append("")
            previous_blank = True
            continue
        output.append(line)
        previous_blank = False
    return "\n".join(output).strip()


_runtime = _BrowserRuntime()


async def render_url(url: str) -> RenderedPage:
    return await _runtime.render(url)


def get_url_browser_install_status() -> BrowserInstallStatus:
    return _runtime.install_status()


async def close_url_browser() -> None:
    await _runtime.close()
