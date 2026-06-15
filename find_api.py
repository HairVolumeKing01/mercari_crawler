"""Capture Mercari search API from network logs."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import time, json, urllib.parse
from crawler import SEARCH_URL, PROXY
from crawler import _CHROMEDRIVER, _CHROME_BIN

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import subprocess as sp, random

sp.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True, timeout=5)
sp.run(["taskkill", "/f", "/im", "chromedriver.exe"], capture_output=True, timeout=5)
time.sleep(0.5)

port = random.randint(10000, 60000)
cd_proc = sp.Popen([_CHROMEDRIVER, f"--port={port}"], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
time.sleep(1.5)

opts = Options()
opts.binary_location = _CHROME_BIN
opts.add_argument("--headless")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--disable-gpu")
opts.add_argument(f"--proxy-server={PROXY}")
opts.add_argument("--lang=ja")
opts.add_argument("--window-size=1920,1080")

# Enable performance logging
opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

driver = webdriver.Remote(f"http://127.0.0.1:{port}", options=opts)
driver.set_page_load_timeout(30)

keyword = 'ポケモンカード'
url = f'{SEARCH_URL}?keyword={urllib.parse.quote(keyword)}&status=on_sale&sort=created_time&order=desc'
driver.get(url)
time.sleep(6)

# Get performance logs (network requests)
logs = driver.get_log("performance")
print(f'Total log entries: {len(logs)}')

# Filter for network responses that might be API calls
api_urls = set()
for entry in logs:
    try:
        msg = json.loads(entry["message"])["message"]
        if msg.get("method") == "Network.responseReceived":
            resp = msg.get("params", {}).get("response", {})
            url = resp.get("url", "")
            # Filter for JSON API calls
            if "api" in url.lower() or "search" in url.lower() or "items" in url.lower():
                if "woff2" not in url and "css" not in url and "js" not in url:
                    api_urls.add(url)
    except:
        pass

print(f'\nAPI-like URLs ({len(api_urls)}):')
for u in sorted(api_urls):
    print(f'  {u[:150]}')

driver.quit()
cd_proc.terminate()
cd_proc.wait(timeout=5)
