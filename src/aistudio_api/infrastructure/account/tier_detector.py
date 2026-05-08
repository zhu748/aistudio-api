"""Detect AI Studio account subscription tier by scraping the page header."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aistudio.premium_detect")


class AccountTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ULTRA = "ultra"


@dataclass
class TierResult:
    tier: AccountTier
    email: str | None = None
    raw_header: str | None = None  # for debugging

    @property
    def is_premium(self) -> bool:
        return self.tier in (AccountTier.PRO, AccountTier.ULTRA)


async def detect_tier(
    browser_context,
    timeout_ms: int = 30000,
) -> TierResult:
    """
    Navigate to AI Studio and detect account tier from the page header.

    Premium accounts show a badge (PRO/ULTRA) next to the email.
    Free accounts show an "Upgrade to unlock more" banner.

    Args:
        browser_context: A Playwright BrowserContext with auth cookies loaded.
        timeout_ms: Navigation timeout.

    Returns:
        TierResult with detected tier and email.
    """
    page = await browser_context.new_page()
    try:
        await page.goto(
            "https://aistudio.google.com/",
            wait_until="networkidle",
            timeout=timeout_ms,
        )
        await asyncio.sleep(2)

        result = await page.evaluate("""() => {
            const body = document.body.innerText.toLowerCase();

            // Strategy 1: Check header for PRO/ULTRA badge next to email
            const headerEls = document.querySelectorAll('header, [role="banner"], nav');
            let headerText = '';
            headerEls.forEach(el => {
                const t = el.innerText.trim();
                if (t && t.length < 1000) headerText += t + '\\n';
            });

            // Find email in header
            const emailMatch = headerText.match(/[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}/i);
            const email = emailMatch ? emailMatch[0] : null;

            // Check for tier badges
            const lines = headerText.split('\\n').map(l => l.trim()).filter(Boolean);
            let tier = 'free';

            // Look for PRO or ULTRA on the line after (or near) the email
            if (email) {
                const emailIdx = lines.findIndex(l => l.includes(email));
                if (emailIdx >= 0) {
                    // Check nearby lines (within 2 lines) for tier badge
                    for (let i = Math.max(0, emailIdx - 1); i <= Math.min(lines.length - 1, emailIdx + 2); i++) {
                        const line = lines[i].toUpperCase().trim();
                        if (line === 'PRO' || line === 'AI PRO') {
                            tier = 'pro';
                            break;
                        }
                        if (line === 'ULTRA' || line === 'AI ULTRA') {
                            tier = 'ultra';
                            break;
                        }
                    }
                }
            }

            // Strategy 2: "Upgrade to unlock more" banner = definitely free
            if (tier === 'free' && (body.includes('upgrade to unlock') || body.includes('upgrade to get'))) {
                tier = 'free';  // confirm free
            }

            // Strategy 3: Fallback — check full page for PRO/ULTRA badge
            // Only if header didn't give us a clear answer
            // (Be careful: "pro" appears in many contexts like "prompt", "process", etc.)
            // The header badge is the most reliable signal.

            return {
                tier: tier,
                email: email,
                header: headerText.substring(0, 500),
            };
        }""")

        return TierResult(
            tier=AccountTier(result["tier"]),
            email=result.get("email"),
            raw_header=result.get("header"),
        )
    finally:
        await page.close()


async def detect_tier_for_auth_file(
    auth_file: str | Path,
    camoufox_port: int = 9222,
    timeout_ms: int = 30000,
) -> TierResult:
    """
    Convenience function: connect to running Camoufox, load auth, detect tier.

    Args:
        auth_file: Path to the auth JSON file (Playwright storage state).
        camoufox_port: Camoufox debug port.
        timeout_ms: Navigation timeout.

    Returns:
        TierResult with detected tier and email.
    """
    from playwright.async_api import async_playwright

    auth_file = str(auth_file)
    if not Path(auth_file).exists():
        raise FileNotFoundError(f"Auth file not found: {auth_file}")

    pw = await async_playwright().start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{camoufox_port}/json", timeout=5
        )
        data = json.loads(resp.read())
        ws_url = f"ws://127.0.0.1:{camoufox_port}{data['wsEndpointPath']}"

        browser = await pw.firefox.connect(ws_url)
        ctx = await browser.new_context(storage_state=auth_file)
        try:
            return await detect_tier(ctx, timeout_ms=timeout_ms)
        finally:
            await ctx.close()
    finally:
        await pw.stop()


# --- CLI ---

async def main():
    import sys

    # Walk up to project root (where data/ lives)
    project_root = Path(__file__).resolve().parents[4]  # src/aistudio_api/infrastructure/account/
    accounts_dir = project_root / "data" / "accounts"
    if not accounts_dir.is_dir():
        # Fallback: search upward for data/accounts
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "data" / "accounts"
            if candidate.is_dir():
                accounts_dir = candidate
                break
    if len(sys.argv) > 1:
        # Specific account(s)
        account_ids = sys.argv[1:]
    else:
        # All accounts with auth files
        account_ids = [
            d.name for d in accounts_dir.iterdir()
            if d.is_dir() and (d / "auth.json").exists()
        ]

    print(f"Checking {len(account_ids)} account(s)...\n")

    for aid in sorted(account_ids):
        auth_file = accounts_dir / aid / "auth.json"
        if not auth_file.exists():
            print(f"  {aid}: no auth.json, skipped")
            continue

        try:
            result = await detect_tier_for_auth_file(auth_file)
            badge = "⭐" if result.is_premium else "  "
            print(f"  {badge} {aid}: {result.tier.value.upper():6s}  ({result.email})")
        except Exception as e:
            print(f"  ❌ {aid}: error — {e}")


if __name__ == "__main__":
    asyncio.run(main())
