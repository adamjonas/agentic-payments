"""Application configuration — all tunables in one place."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Paths
DATA_DIR = Path(os.environ.get("AP_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "payments.db"

# Budget
DAILY_BUDGET_SATS = int(os.environ.get("AP_DAILY_BUDGET_SATS", "1000"))

# Agent
AGENT_MODEL = os.environ.get("AP_AGENT_MODEL", "claude-haiku-4-5-20251001")
AGENT_MAX_TURNS = int(os.environ.get("AP_AGENT_MAX_TURNS", "25"))

# Lightning — MoneyDevKit agent-wallet CLI
# Default invocation: npx @moneydevkit/agent-wallet@latest <command>
# Override with AP_MDK_CMD for custom installs (e.g. a global binary path)
MDK_CMD: list[str] = os.environ.get(
    "AP_MDK_CMD", "npx @moneydevkit/agent-wallet@latest"
).split()

# Vendor blocklist — comma-separated domains the agent must never pay
_blocklist_raw = os.environ.get("AP_VENDOR_BLOCKLIST", "")
VENDOR_BLOCKLIST: set[str] = {d.strip() for d in _blocklist_raw.split(",") if d.strip()}
