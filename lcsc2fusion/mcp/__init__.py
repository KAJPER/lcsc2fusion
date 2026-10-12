"""MCP (Model Context Protocol) server: lets another Claude search LCSC, convert parts to .lbr
and add them to Fusion through the running add-in. Start it with `python -m lcsc2fusion.mcp`."""

from lcsc2fusion.mcp.server import main

__all__ = ["main"]
