import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open('debug_product.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Get all visible text lines
from html.parser import HTMLParser
class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip = True
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = False
    def handle_data(self, data):
        if not self.skip:
            self.text.append(data.strip())

extractor = TextExtractor()
extractor.feed(html)
visible = '\n'.join(t for t in extractor.text if t)

# Search for lines with time-related keywords
for line in visible.split('\n'):
    line = line.strip()
    if not line:
        continue
    if any(w in line for w in ['時間', '分前', '日前', '出品', '投稿', '公開', '更新日', '発送']):
        print(f'  [{line[:120]}]')

print('\n=== Lines with numbers near text ===')
for line in visible.split('\n'):
    line = line.strip()
    if re.search(r'\d{1,2}[時間分日]', line):
        print(f'  [{line[:120]}]')

# Also check for any ISO date in visible text
iso_dates = re.findall(r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}', visible)
print(f'\nISO-like dates in visible text: {iso_dates[:10]}')
