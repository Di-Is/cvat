#!/usr/bin/env python3
"""
Playwright-based smoke test that opens the annotation workspace, triggers the
Run Annotation Actions modal (Ctrl+E), and verifies whether the SAM2 tracker
action appears in the Select action dropdown.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


BASE_URL = os.environ.get("CVAT_UI_BASE_URL", "http://192.168.10.190:8080")
TASK_ID = os.environ.get("CVAT_UI_TASK_ID", "1")
JOB_ID = os.environ.get("CVAT_UI_JOB_ID", "1")
SESSION_ID = os.environ.get("CVAT_UI_SESSION_ID")
SCREENSHOT_PATH = Path(os.environ.get("SAM2_RUN_ACTION_SCREENSHOT", "tasks/screenshots/sam2_run_action.png"))
SELECT_SCREENSHOT_PATH = Path(os.environ.get(
    "SAM2_RUN_ACTION_SELECT_SCREENSHOT",
    "tasks/screenshots/sam2_run_action_select.png",
))
LOG_PATH = Path(os.environ.get("SAM2_RUN_ACTION_LOG", "tasks/sam2_run_action_playwright.log"))

TARGET_ACTION_NAME = os.environ.get("SAM2_RUN_ACTION_NAME", "AI Tracker: SAM2")


def _cookie_domain() -> str:
    parsed = urlparse(BASE_URL)
    if not parsed.hostname:
        raise RuntimeError(f"Could not parse hostname from {BASE_URL}")
    return parsed.hostname


async def _ensure_modal(page) -> None:
    await page.keyboard.press("Control+KeyE")
    await page.wait_for_selector(".cvat-action-runner-content", timeout=10000)


async def _check_tracker_option(page):
    await page.click(".cvat-action-runner-content .ant-select-selector", timeout=5000)
    await page.wait_for_selector(".ant-select-dropdown", timeout=5000)
    option_locator = page.locator(
        ".ant-select-dropdown .ant-select-item-option-content",
        has_text=TARGET_ACTION_NAME,
    )
    options = await page.locator(".ant-select-dropdown .ant-select-item-option-content").all_text_contents()
    return await option_locator.count(), options


async def main() -> int:
    if not SESSION_ID:
        raise RuntimeError("CVAT_UI_SESSION_ID environment variable is required")

    SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SELECT_SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    job_url = f"{BASE_URL}/tasks/{TASK_ID}/jobs/{JOB_ID}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        await context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": SESSION_ID,
                    "domain": _cookie_domain(),
                    "path": "/",
                    "httpOnly": True,
                    "secure": BASE_URL.startswith("https"),
                    "sameSite": "Lax",
                },
            ],
        )
        page = await context.new_page()
        result_payload: dict[str, object] = {
            "job_url": job_url,
            "target_action": TARGET_ACTION_NAME,
        }
        try:
            await page.goto(job_url, wait_until="networkidle")
            await page.wait_for_selector(".cvat-objects-sidebar", timeout=20000)
            await _ensure_modal(page)
            match_count, options = await _check_tracker_option(page)
            await page.screenshot(path=SELECT_SCREENSHOT_PATH, full_page=True)
            await page.wait_for_timeout(500)  # keep dropdown open briefly
            await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
            result_payload["options"] = options
            result_payload["found"] = match_count > 0
            result_payload["screenshot"] = str(SCREENSHOT_PATH)
            result_payload["select_screenshot"] = str(SELECT_SCREENSHOT_PATH)
            exit_code = 0 if match_count > 0 else 1
        except PlaywrightTimeoutError as exc:
            await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
            result_payload["error"] = f"Timeout: {exc}"
            result_payload["screenshot"] = str(SCREENSHOT_PATH)
            exit_code = 2
        finally:
            await browser.close()

    LOG_PATH.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")
    print(json.dumps(result_payload, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
