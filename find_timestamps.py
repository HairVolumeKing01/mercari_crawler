"""Dump rendered product page to find timestamp element."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import time, re
from crawler import PROXY, BASE_URL
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
opts.add_argument("--window-size=1400,900")

driver = webdriver.Remote(f"http://127.0.0.1:{port}", options=opts)
driver.set_page_load_timeout(25)

item_id = 'm88864934764'
url = f'{BASE_URL}/item/{item_id}'
print(f'Loading {url}')
driver.get(url)
time.sleep(3)

# Dump HTML to file
html = driver.page_source
with open('product_page.html', 'w', encoding='utf-8') as f:
    f.write(html)
print(f'Saved {len(html)} bytes to product_page.html')

# Search for time elements
times = re.findall(r'<time[^>]*>(.*?)</time>', html)
print(f'<time> elements: {len(times)}')
for t in times[:5]:
    print(f'  text: {t[:100]}')

datetimes = re.findall(r'datetime=[\"\\\']([^\"\\\']+)', html, re.IGNORECASE)
print(f'datetime attrs: {len(datetimes)}')
for d in datetimes[:5]:
    print(f'  val: {d[:100]}')

# Search for date/time patterns in visible text
body_text = driver.find_element(By.TAG_NAME, "body").text
date_patterns = re.findall(r'(\d{4}[年/-]\d{1,2}[月/-]\d{1,2})', body_text)
print(f'Date patterns: {date_patterns[:5]}')

# Search for relative time patterns
rel_patterns = re.findall(r'(\d+[時間分日前])', body_text)
print(f'Relative time patterns: {rel_patterns[:5]}')

# Look for "出品" (listed) context
for line in body_text.split('\n'):
    if any(w in line for w in ['出品', '投稿', '公開', '更新', 'created', 'listed']):
        print(f'Time-related line: {line.strip()[:120]}')

driver.quit()
cd_proc.terminate()
cd_proc.wait(timeout=5)
