"""Dashboard package exposing FastAPI LAN server and TradingView Lightweight Charts UI."""

from dashboard.server import create_dashboard_app

__all__ = ["create_dashboard_app"]
