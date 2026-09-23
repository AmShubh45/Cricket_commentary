"""Playwright-based scorecard renderer.

Uses headless Chromium to render the HTML/CSS scorecard template
as a PNG screenshot. This produces broadcast-quality 1920×1080
frames that ffmpeg composites with TTS audio.

Design:
- Browser launched once at pipeline startup (not per-frame)
- Single page instance reused across frames (navigate + screenshot)
- Match state injected via JavaScript DOM manipulation
- Screenshots captured at 1920×1080 native resolution
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

from config.settings import GraphicsSettings
from cricket.domain.models import MatchState

logger = logging.getLogger(__name__)


class PlaywrightRenderer:
    """Headless Chromium scorecard renderer implementing GraphicsRendererProtocol.

    Usage:
        renderer = PlaywrightRenderer(settings)
        await renderer.initialize()
        path = await renderer.render_scorecard(match_state, "output/frame_001.png")
        await renderer.shutdown()
    """

    def __init__(self, settings: GraphicsSettings) -> None:
        self._template_path = settings.scorecard_template_path
        self._width = settings.playwright_screenshot_width
        self._height = settings.playwright_screenshot_height
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._playwright: Any = None  # Playwright context manager

    async def initialize(self) -> None:
        """Launch headless Chromium and load the scorecard template."""
        logger.info("Initializing Playwright renderer (Chromium headless)...")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--font-render-hinting=none",  # Better font rendering
            ],
        )

        self._page = await self._browser.new_page(
            viewport={"width": self._width, "height": self._height},
            device_scale_factor=1,
        )

        # Load the scorecard template
        template_abs = Path(self._template_path).resolve()
        if not template_abs.exists():
            raise FileNotFoundError(f"Scorecard template not found: {template_abs}")

        await self._page.goto(f"file://{template_abs}", wait_until="networkidle")
        logger.info("Playwright renderer initialized. Template loaded from %s", template_abs)

    async def shutdown(self) -> None:
        """Close the browser and clean up."""
        if self._page:
            await self._page.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Playwright renderer shut down.")

    async def render_scorecard(self, match_state: MatchState, output_path: str) -> str:
        """Render the current scorecard as a 1920×1080 PNG.

        Injects match state into the HTML template via JavaScript DOM
        manipulation, then captures a screenshot.

        Args:
            match_state: Current live match state.
            output_path: Where to save the PNG.

        Returns:
            Absolute path to the rendered PNG.
        """
        if not self._page:
            raise RuntimeError("Renderer not initialized. Call initialize() first.")

        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Inject match state into the DOM
        await self._inject_match_state(match_state)

        # Screenshot the full page
        await self._page.screenshot(
            path=output_path,
            full_page=False,
            type="png",
        )

        logger.debug("Scorecard frame rendered: %s", output_path)
        return os.path.abspath(output_path)

    async def update_commentary_ticker(
        self,
        commentary_text: str,
        event_label: str = "",
        event_type: str = "dot",
    ) -> None:
        """Update just the commentary ticker without re-rendering everything.

        Called per-ball to show the latest commentary line.
        """
        if not self._page:
            return

        safe_text = commentary_text.replace("'", "\\'").replace('"', '\\"')
        safe_label = event_label.replace("'", "\\'")

        await self._page.evaluate(f"""() => {{
            const ticker = document.getElementById('ticker-commentary');
            const indicator = document.getElementById('ticker-event');
            if (ticker) ticker.textContent = '{safe_text}';
            if (indicator) {{
                indicator.textContent = '{safe_label}';
                indicator.setAttribute('data-event', '{event_type}');
            }}
        }}""")

    async def _inject_match_state(self, state: MatchState) -> None:
        """Inject match state values into the HTML template via JS."""
        innings = state.get_current_innings()
        if not innings:
            return

        # Build the JavaScript to update all DOM elements
        updates: dict[str, str] = {
            "match-title": f"{state.match_info.title}",
            "batting-team": innings.batting_team.short_name,
            "bowling-team": innings.bowling_team.short_name,
            "total-runs": str(innings.total_runs),
            "total-wickets": str(innings.total_wickets),
            "total-overs": str(innings.total_overs),
            "current-rr": f"{innings.run_rate:.2f}" if innings.run_rate else "0.00",
            "partnership": f"{innings.partnership_runs} ({innings.partnership_balls})",
        }

        # Batsmen info
        for idx, batter in enumerate(innings.batsmen[:2]):
            prefix = f"batsman-{idx + 1}"
            updates[f"{prefix}-name"] = batter.player.display_name
            updates[f"{prefix}-runs"] = str(batter.runs)
            updates[f"{prefix}-balls"] = f"({batter.balls_faced})"
            updates[f"{prefix}-sr"] = f"SR {batter.strike_rate:.1f}"
            updates[f"{prefix}-fours"] = f"4s: {batter.fours}"
            updates[f"{prefix}-sixes"] = f"6s: {batter.sixes}"

        # Bowler info
        if innings.bowler:
            updates["bowler-name"] = innings.bowler.player.display_name
            updates["bowler-figures"] = f"{innings.bowler.wickets}/{innings.bowler.runs_conceded}"
            updates["bowler-overs"] = f"({innings.bowler.overs} ov)"
            updates["bowler-econ"] = f"ECON {innings.bowler.economy:.2f}"

        # Target info (if second innings)
        if innings.target is not None:
            updates["target-value"] = str(innings.target)
            if innings.required_rate is not None:
                updates["required-rr"] = f"{innings.required_rate:.2f}"

        # Build JS update script
        js_lines = []
        for elem_id, value in updates.items():
            safe_value = value.replace("'", "\\'")
            js_lines.append(
                f"const el_{elem_id.replace('-', '_')} = document.getElementById('{elem_id}');"
                f"if (el_{elem_id.replace('-', '_')}) el_{elem_id.replace('-', '_')}.textContent = '{safe_value}';"
            )

        # Show target block if second innings
        if innings.target is not None:
            js_lines.append(
                "const tb = document.getElementById('target-block');"
                "if (tb) tb.style.display = 'flex';"
            )
            js_lines.append(
                "const rr = document.getElementById('req-rate-block');"
                "if (rr) rr.style.display = 'flex';"
            )

        js_code = "\n".join(js_lines)
        await self._page.evaluate(f"() => {{ {js_code} }}")

    async def add_ball_to_over_strip(self, runs: int, is_wicket: bool = False, is_wide: bool = False) -> None:
        """Add a ball indicator to the 'this over' strip."""
        if not self._page:
            return

        if is_wicket:
            css_class = "wicket"
            label = "W"
        elif is_wide:
            css_class = "wide"
            label = "Wd"
        elif runs == 4:
            css_class = "boundary"
            label = "4"
        elif runs == 6:
            css_class = "six"
            label = "6"
        elif runs == 0:
            css_class = "dot"
            label = "0"
        elif runs == 1:
            css_class = "single"
            label = "1"
        else:
            css_class = "single"
            label = str(runs)

        await self._page.evaluate(f"""() => {{
            const strip = document.getElementById('ball-indicators');
            if (strip) {{
                const ball = document.createElement('span');
                ball.className = 'ball {css_class}';
                ball.textContent = '{label}';
                strip.appendChild(ball);
                // Keep only last 6 balls visible
                while (strip.children.length > 6) {{
                    strip.removeChild(strip.firstChild);
                }}
            }}
        }}""")

    async def clear_over_strip(self) -> None:
        """Clear the ball indicators for a new over."""
        if not self._page:
            return
        await self._page.evaluate("""() => {
            const strip = document.getElementById('ball-indicators');
            if (strip) strip.innerHTML = '';
        }""")
