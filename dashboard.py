#!/usr/bin/env python
"""
Mercari Monitor Dashboard
=========================
Local web dashboard to view crawled items stored in SQLite.
Usage: python dashboard.py [--port 5000] [--db ./mercari.db]
"""

import sqlite3
import os
import sys

# Auto-install Flask if missing
try:
    from flask import Flask, request, jsonify, g
except ImportError:
    import subprocess
    print("[dashboard] Installing Flask...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask"])
    from flask import Flask, request, jsonify, g

DB_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mercari.db")

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT,
            keyword TEXT DEFAULT '',
            name TEXT,
            price INTEGER,
            image_url TEXT,
            url TEXT,
            seller TEXT,
            likes INTEGER DEFAULT 0,
            description TEXT,
            comments TEXT,
            crawled_at TEXT,
            listed_at TEXT DEFAULT '',
            PRIMARY KEY (id, keyword)
        )
    """)
    # migration
    cols = [r[1] for r in db.execute("PRAGMA table_info(items)").fetchall()]
    for col in ["keyword", "listed_at"]:
        if col not in cols:
            try:
                db.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception:
                pass
    db.commit()


@app.route("/")
def index():
    return _HTML


@app.route("/api/items")
def api_items():
    init_db()
    db = get_db()
    kw = request.args.get("keyword", "")
    if kw:
        rows = db.execute("SELECT * FROM items WHERE keyword=? ORDER BY crawled_at DESC, id DESC", (kw,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM items ORDER BY crawled_at DESC, id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/keywords")
def api_keywords():
    init_db()
    db = get_db()
    rows = db.execute("SELECT keyword, COUNT(*) as cnt FROM items WHERE keyword!='' GROUP BY keyword ORDER BY keyword").fetchall()
    return jsonify([{"keyword": r["keyword"], "count": r["cnt"]} for r in rows])


@app.route("/api/items", methods=["DELETE"])
def api_clear():
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    db.execute("DELETE FROM items")
    db.commit()
    return jsonify({"deleted": count})


_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mercari Monitor</title>
<style>
  :root { color-scheme: dark; --bg: #0d1117; --card: #161b22; --border: #30363d;
          --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --danger: #da3633; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; }
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
  .header h1 { font-size: 20px; }
  .header .stats { color: var(--muted); font-size: 14px; }
  .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
  .toolbar input { background: var(--card); border: 1px solid var(--border); color: var(--text);
    padding: 6px 12px; border-radius: 6px; font-size: 14px; width: 260px; }
  .toolbar input::placeholder { color: var(--muted); }
  .btn { padding: 6px 16px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--card); color: var(--text); cursor: pointer; font-size: 14px; transition: .15s; }
  .btn:hover { background: #21262d; }
  .btn.danger { color: var(--danger); border-color: var(--danger); }
  .btn.danger:hover { background: var(--danger); color: white; }
  .btn.accent { color: var(--accent); border-color: var(--accent); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; transition: .15s; }
  .card:hover { border-color: #58a6ff55; }
  .card .img { width: 100%; height: 180px; object-fit: contain; background: #0d1117; }
  .card .body { padding: 12px; }
  .card .name { font-size: 14px; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; margin-bottom: 8px; }
  .card .name a { color: var(--text); text-decoration: none; }
  .card .name a:hover { color: var(--accent); }
  .card .meta { display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
  .card .price { font-weight: bold; color: #f0883e; font-size: 16px; }
  .card .likes { color: var(--muted); font-size: 12px; }
  .card .seller { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .card .desc { font-size: 12px; color: var(--muted); margin-top: 6px; max-height: 60px; overflow: hidden; line-height: 1.4; }
  .empty { text-align: center; color: var(--muted); padding: 60px 20px; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--muted); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.6s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .toast { position: fixed; top: 20px; right: 20px; background: #238636; color: white; padding: 10px 20px;
    border-radius: 8px; font-size: 14px; z-index: 999; animation: fadeInOut 2s ease; }
  .toast.err { background: var(--danger); }
  @keyframes fadeInOut { 0% { opacity: 0; transform: translateY(-10px); } 10% { opacity: 1; transform: translateY(0); } 80% { opacity: 1; } 100% { opacity: 0; } }
</style>
</head>
<body>
<div class="header">
  <h1>🛒 Mercari Monitor</h1>
  <span class="stats" id="stats">読み込み中...</span>
</div>
<div class="toolbar">
  <input type="text" id="filter" placeholder="絞り込み (品名・出品者)..." oninput="render()">
  <button class="btn" onclick="location.reload()">🔄 更新</button>
  <button class="btn danger" onclick="clearAll()">🗑 全削除</button>
  <span style="font-size:12px;color:var(--muted)">自動更新: 15秒</span>
</div>
<div class="toolbar" id="keyword-tabs" style="margin-bottom:16px">
  <button class="btn accent active-kw" data-kw="" onclick="setKeyword('')">📋 全部</button>
</div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty">📭 データなし — monitorを起動してね</div>

<script>
let items = [];
let currentKw = '';

async function load() {
  try {
    const url = currentKw ? `/api/items?keyword=${encodeURIComponent(currentKw)}` : '/api/items';
    const r = await fetch(url);
    items = await r.json();
    render();
    loadKeywords();
  } catch(e) {
    document.getElementById('stats').innerHTML = '<span style="color:#da3633">接続エラー</span>';
  }
}

async function loadKeywords() {
  try {
    const r = await fetch('/api/keywords');
    const kws = await r.json();
    const tabs = document.getElementById('keyword-tabs');
    tabs.innerHTML = '<button class="btn accent active-kw" data-kw="" onclick="setKeyword(\'\')">📋 全部</button>';
    kws.forEach(k => {
      const active = k.keyword === currentKw ? ' active-kw' : '';
      tabs.innerHTML += `<button class="btn${active}" data-kw="${esc(k.keyword)}" onclick="setKeyword('${esc(k.keyword)}')">${esc(k.keyword)} (${k.count})</button>`;
    });
  } catch(e) {}
}

function setKeyword(kw) {
  currentKw = kw;
  load();
}

function render() {
  const q = document.getElementById('filter').value.toLowerCase();
  let filtered = items.filter(it =>
    !q || (it.name||'').toLowerCase().includes(q) || (it.seller||'').toLowerCase().includes(q)
  );

  document.getElementById('stats').textContent = (currentKw||'全部') + `: ${items.length}件 | 表示: ${filtered.length}件`;
  const grid = document.getElementById('grid');
  const empty = document.getElementById('empty');

  if (filtered.length === 0) {
    grid.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  grid.innerHTML = filtered.map(it => `
    <div class="card">
      <img class="img" src="${esc(it.image_url)}" loading="lazy"
           onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22280%22 height=%22180%22><rect fill=%22%23161b22%22 width=%22280%22 height=%22180%22/><text fill=%22%238b949e%22 x=%22140%22 y=%2295%22 text-anchor=%22middle%22 font-size=%2214%22>no image</text></svg>'">
      <div class="body">
        <div class="name"><a href="${esc(it.url)}" target="_blank" title="${esc(it.name)}">${esc(it.name)}</a></div>
        <div class="meta">
          <span class="price">¥${Number(it.price).toLocaleString()}</span>
          <span class="likes">❤ ${it.likes||0}</span>
        </div>
        ${it.seller ? `<div class="seller">👤 ${esc(it.seller)}</div>` : ''}
        ${it.description ? `<div class="desc">${esc(it.description)}</div>` : ''}
        <div style="font-size:10px;color:var(--muted);margin-top:6px">🕒 ${it.listed_at||'?'} &nbsp;|&nbsp; 爬取: ${it.crawled_at||''}</div>
      </div>
    </div>
  `).join('');
}

function esc(s) { if (!s) return ''; const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

async function clearAll() {
  if (!confirm('全データを削除します。よろしいですか？')) return;
  try {
    const r = await fetch('/api/items', {method:'DELETE'});
    const j = await r.json();
    showToast(`${j.deleted}件 削除しました`);
    await load();
  } catch(e) { showToast('削除失敗', true); }
}

function showToast(msg, isErr) {
  const t = document.createElement('div');
  t.className = 'toast' + (isErr?' err':'');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

load();
setInterval(load, 15000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mercari Monitor Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Listen port (default: 5000)")
    parser.add_argument("--db", type=str, default=DB_DEFAULT, help="SQLite DB path")
    args = parser.parse_args()

    app.config["DB_PATH"] = args.db
    print(f"[dashboard] DB: {args.db}")
    print(f"[dashboard] ➜ http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)
