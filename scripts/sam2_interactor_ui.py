import asyncio
import json
import os
from pathlib import Path

import requests
from playwright.async_api import async_playwright

CVAT_URL = os.environ.get("CVAT_URL", "http://localhost:8080")
USERNAME = os.environ.get("CVAT_USERNAME", "admin")
PASSWORD = os.environ.get("CVAT_PASSWORD", "Admin123!")
TASK_ID = int(os.environ.get("CVAT_UI_TASK_ID", "5"))
JOB_ID = int(os.environ.get("CVAT_UI_JOB_ID", "3"))
SCREENSHOT_PATH = Path(os.environ.get("SAM2_UI_SCREENSHOT", "tasks/screenshots/sam2_ui.png"))
LOG_PATH = Path(os.environ.get("SAM2_UI_LOG", "tasks/sam2_ui_playwright.log"))

SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

def login():
    resp = requests.post(
        f"{CVAT_URL}/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
    )
    resp.raise_for_status()
    return resp.cookies["sessionid"], resp.cookies["csrftoken"]

async def main():
    sessionid, csrftoken = login()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        await context.add_cookies([
            {"name": "sessionid", "value": sessionid, "domain": "localhost", "path": "/"},
            {"name": "csrftoken", "value": csrftoken, "domain": "localhost", "path": "/"},
        ])
        page = await context.new_page()
        await page.goto(f"{CVAT_URL}/tasks/{TASK_ID}/jobs/{JOB_ID}")
        await page.wait_for_selector('.cvat-tools-control', timeout=60000)
        await page.click('.cvat-tools-control')
        await page.wait_for_selector('.cvat-tools-control-popover-content', timeout=10000)
        await page.get_by_role('tab', name='Interactors').click()
        await page.wait_for_timeout(1000)
        tabs = await page.locator('.ant-tabs-tab').all_inner_texts()
        interactors_present = any('Interactors' in tab for tab in tabs)
        await page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
        with LOG_PATH.open('w', encoding='utf-8') as fh:
            fh.write(json.dumps({
                'tabs': tabs,
                'interactors_present': interactors_present,
            }, indent=2))
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
