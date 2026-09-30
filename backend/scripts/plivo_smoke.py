"""Controlled real Plivo check (manual; NOT part of the automated tests).

Default: READ-ONLY. Verifies the Auth ID / token and that the phone number is rented on the
account (two GET requests, no cost). Credentials come from the environment (never printed):

    PLIVO_AUTH_ID=... PLIVO_AUTH_TOKEN=... PLIVO_NUMBER=+91... \
        uv run python scripts/plivo_smoke.py

A real, billable test call is placed ONLY through the application (Settings > Integrations >
Plivo, then plan and start a phone call), because the call needs PUBLIC_BASE_URL to be a public
https URL that Plivo can reach for its answer/dial/hangup callbacks and the audio WebSocket.
This script checks those prerequisites and tells you what is missing.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.config import get_settings
from app.integrations.providers.plivo import PlivoCredentials, PlivoTelephonyProvider


async def main() -> int:
    auth_id = os.environ.get("PLIVO_AUTH_ID", "")
    token = os.environ.get("PLIVO_AUTH_TOKEN", "")
    number = os.environ.get("PLIVO_NUMBER", "")
    if not (auth_id and token and number):
        print("BLOCKED: set PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN and PLIVO_NUMBER")
        return 2
    provider = PlivoTelephonyProvider(
        PlivoCredentials(auth_id, token, number), integration_id="smoke", stream_enabled=False
    )
    report = await provider.test_connection()
    for check in report.checks:
        detail = f": {check.detail}" if check.detail else ""
        print(f"{'OK  ' if check.ok else 'FAIL'} {check.label}{detail}")
    s = get_settings()
    public = s.public_base_url.startswith("https://") and "localhost" not in s.public_base_url
    print(f"{'OK  ' if public else 'FAIL'} PUBLIC_BASE_URL is public https (callbacks, media)")
    print(f"{'OK  ' if s.stt_provider != 'mock' else 'FAIL'} STT_PROVIDER is a real provider")
    return 0 if report.ok and public else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
