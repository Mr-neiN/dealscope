"""Local web dashboard for the scanner database.

Serves a single-page UI plus a small JSON API on top of the same SQLite
file the scanner writes. Reads are opened per-request in read-only mode so
the dashboard can be a separate process or container next to the scanner.
"""

from src.gui.server import DashboardStore, GuiServer, make_handler

__all__ = ["DashboardStore", "GuiServer", "make_handler"]
