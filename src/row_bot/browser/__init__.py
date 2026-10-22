"""Row-Bot's browser: Chromium with its own signed-in profile, shown in the app's Browser pane.

This package owns Row-Bot's Python Playwright integration. It does not own,
attach to, or share sessions with native Computer Use or external MCP
browser servers.
"""

from row_bot.browser.service import BrowserEngine, BrowserSession, BrowserSessionManager

__all__ = ["BrowserEngine", "BrowserSession", "BrowserSessionManager"]
