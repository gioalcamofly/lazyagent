"""Playwright helpers for the browser tests (marker ``e2e``).

Local only (no Playwright in CI): everything here skips cleanly when the
``playwright`` package or a Chromium it can launch is missing. Install it
with ``uv sync --group dev --group e2e``; Playwright's own browser is used
when downloaded, else the system Google Chrome (``channel="chrome"``).
"""

from __future__ import annotations

import mimetypes

import pytest
import pytest_asyncio
from lazyagent.host.cli import STATIC_ROOT

try:
    from playwright import async_api as playwright
except ImportError:  # pragma: no cover - depends on the environment
    playwright = None

# Put on every browser test, so a missing package skips before any fixture
# (a host, a TUI) is started for nothing.
needs_playwright = pytest.mark.skipif(
    playwright is None, reason="playwright is not installed (uv sync --group e2e)"
)

# The origin the static harness serves the page's modules from.
TEST_ORIGIN = "http://lazyagent.test"

# Real GPU when there is one (WebGL renderer tests and the perf numbers
# depend on it); harmless without.
CHROME_ARGS = ["--enable-gpu", "--ignore-gpu-blocklist"]


async def launch(p, **options):
    for extra in ({}, {"channel": "chrome"}):
        try:
            return await p.chromium.launch(args=CHROME_ARGS, **options, **extra)
        except playwright.Error:
            continue
    return None


@pytest_asyncio.fixture
async def browser():
    if playwright is None:
        pytest.skip("playwright is not installed (uv sync --group e2e)")
    async with playwright.async_playwright() as p:
        launched = await launch(p)
        if launched is None:
            pytest.skip("no Chromium Playwright can launch")
        try:
            yield launched
        finally:
            await launched.close()


# xterm.js's own note that a parse ran past its time slice (a busy machine).
BENIGN = ("task queue exceeded allotted deadline",)


class Problems(list):
    """Console errors and warnings, and uncaught page errors."""

    def watch(self, page) -> None:
        page.on(
            "console",
            lambda m: self.append(m.text)
            if m.type in ("error", "warning") and not m.text.startswith(BENIGN)
            else None,
        )
        page.on("pageerror", lambda e: self.append(str(e)))


async def serve_static(page, extra: dict[str, str] | None = None) -> None:
    """Serve the host's static files at TEST_ORIGIN, plus ``extra``
    (path → HTML), with no host and no WebSocket."""

    async def handle(route):
        path = route.request.url.removeprefix(TEST_ORIGIN).split("?")[0]
        if extra and path in extra:
            await route.fulfill(body=extra[path], content_type="text/html")
            return
        file = (STATIC_ROOT / path.lstrip("/")).resolve()
        if STATIC_ROOT.resolve() not in file.parents or not file.is_file():
            await route.fulfill(status=404, body="not found")
            return
        kind = "text/javascript" if file.suffix in (".js", ".mjs") else (
            mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        )
        await route.fulfill(body=file.read_bytes(), content_type=kind)

    await page.route(f"{TEST_ORIGIN}/**", handle)


BLANK = """<!doctype html><html data-theme="dark"><head><meta charset="utf-8">
<meta name="color-scheme" content="dark">
<link rel="stylesheet" href="/vendor/xterm/xterm.css"><link rel="stylesheet" href="/app.css">
</head><body></body></html>"""


async def module_page(browser, **context_options):
    """A page at TEST_ORIGIN/blank.html with the page's CSS, ready to
    ``import()`` its modules in ``page.evaluate``."""
    context = await browser.new_context(**context_options)
    page = await context.new_page()
    problems = Problems()
    problems.watch(page)
    await serve_static(page, {"/blank.html": BLANK})
    await page.goto(f"{TEST_ORIGIN}/blank.html")
    return page, problems


__all__ = ["needs_playwright", "Problems", "TEST_ORIGIN", "browser", "launch", "module_page", "playwright", "serve_static"]
