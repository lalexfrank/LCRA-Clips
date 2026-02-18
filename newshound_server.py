#!/usr/bin/env python3
"""
NewsHound — Local RSS News Digest Builder
Run: python3 newshound_server.py
Then open: http://localhost:8765
"""

import http.server
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import threading
import webbrowser
import time
import os
from datetime import datetime

# ── RSS SOURCES ───────────────────────────────────────────────────────────────
SOURCES = {
    "BBC News": [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
    ],
    "The Guardian": [
        "https://www.theguardian.com/world/rss",
        "https://www.theguardian.com/us-news/rss",
    ],
    "Reuters": [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/Reuters/worldNews",
    ],
    "AP News": [
        "https://feeds.apnews.com/rss/apf-topnews",
        "https://feeds.apnews.com/rss/apf-WorldNews",
    ],
}

# ── RSS PARSER ─────────────────────────────────────────────────────────────────
NS = {
    'dc': 'http://purl.org/dc/elements/1.1/',
    'content': 'http://purl.org/rss/1.0/modules/content/',
    'atom': 'http://www.w3.org/2005/Atom',
    'media': 'http://search.yahoo.com/mrss/',
}

def fetch_feed(url, max_items=50):
    """Fetch and parse an RSS/Atom feed, return list of article dicts."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; NewsHound/1.0; +https://newshound.local)',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [WARN] Failed to fetch {url}: {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  [WARN] XML parse error for {url}: {e}")
        return []

    items = []

    # Detect feed type
    tag = root.tag.lower()
    if 'feed' in tag:
        # Atom feed
        for entry in root.findall('{http://www.w3.org/2005/Atom}entry')[:max_items]:
            title = _text(entry, '{http://www.w3.org/2005/Atom}title')
            link_el = entry.find('{http://www.w3.org/2005/Atom}link')
            link = link_el.get('href', '') if link_el is not None else ''
            pub = _text(entry, '{http://www.w3.org/2005/Atom}updated') or \
                  _text(entry, '{http://www.w3.org/2005/Atom}published')
            desc = _text(entry, '{http://www.w3.org/2005/Atom}summary') or \
                   _text(entry, '{http://www.w3.org/2005/Atom}content')
            items.append({'title': title, 'link': link, 'pubDate': pub, 'description': desc or ''})
    else:
        # RSS feed
        channel = root.find('channel') or root
        for item in channel.findall('item')[:max_items]:
            title = _text(item, 'title')
            link  = _text(item, 'link') or _text(item, 'guid')
            pub   = _text(item, 'pubDate') or _text(item, 'dc:date',
                    '{http://purl.org/dc/elements/1.1/}date')
            desc  = _text(item, 'description') or \
                    _text(item, '{http://purl.org/rss/1.0/modules/content/}encoded')
            items.append({'title': title or '', 'link': link or '', 'pubDate': pub or '', 'description': desc or ''})

    return items

def _text(el, *tags):
    for tag in tags:
        child = el.find(tag)
        if child is not None and child.text:
            return child.text.strip()
    return ''

def strip_html(s):
    import re
    return re.sub(r'<[^>]+>', '', s or '')

def fmt_date(s):
    if not s:
        return ''
    for fmt in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S %Z',
                '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            return dt.strftime('%-m/%-d/%y')
        except Exception:
            continue
    return s[:10]  # fallback: first 10 chars

# ── HTTP HANDLER ──────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress request logging

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_html()
        elif self.path == '/sources':
            self._serve_json(list(SOURCES.keys()))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/search':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            result = self._do_search(body)
            self._serve_json(result)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_json(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(payload))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)

    def _do_search(self, body):
        """
        body: {
          categories: [{name, keywords:[]}],
          sources: [source_name, ...],
          max_per_source: int,
          match_field: 'both'|'title'|'desc'
        }
        Returns: [{catName, articles:[{title,link,date,pub,hitKws}]}]
        """
        categories   = body.get('categories', [])
        src_names    = body.get('sources', list(SOURCES.keys()))
        max_items    = min(int(body.get('max_per_source', 50)), 100)
        match_field  = body.get('match_field', 'both')

        # Fetch all articles from selected sources
        all_articles = []
        for src_name in src_names:
            urls = SOURCES.get(src_name, [])
            seen = set()
            for url in urls:
                print(f"  Fetching {src_name}: {url}")
                items = fetch_feed(url, max_items)
                for it in items:
                    key = it.get('link') or it.get('title')
                    if key and key not in seen:
                        seen.add(key)
                        it['sourceName'] = src_name
                        all_articles.append(it)

        print(f"  Total articles fetched: {len(all_articles)}")

        # Match per category
        results = []
        for cat in categories:
            cat_name = cat.get('name', '').upper()
            kws = [k.lower() for k in cat.get('keywords', []) if k.strip()]
            if not kws:
                continue

            matched = []
            seen_links = set()
            for item in all_articles:
                link = item.get('link', '') or item.get('title', '')
                if link in seen_links:
                    continue

                title = (item.get('title') or '').lower()
                desc  = strip_html(item.get('description') or '').lower()

                hit_kws = []
                for kw in kws:
                    if match_field == 'title' and kw in title:
                        hit_kws.append(kw)
                    elif match_field == 'desc' and kw in desc:
                        hit_kws.append(kw)
                    elif match_field == 'both' and (kw in title or kw in desc):
                        hit_kws.append(kw)

                if hit_kws:
                    matched.append({
                        'title':  item.get('title', 'Untitled'),
                        'link':   item.get('link', '#'),
                        'date':   fmt_date(item.get('pubDate', '')),
                        'pub':    item.get('sourceName', ''),
                        'hitKws': hit_kws,
                    })
                    seen_links.add(link)

            # Sort newest first (by date string — approximate)
            matched.sort(key=lambda x: x['date'], reverse=True)
            results.append({'catName': cat_name, 'articles': matched})
            print(f"  Category '{cat_name}': {len(matched)} matches")

        return results

    def _serve_html(self):
        html = get_html()
        payload = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(payload))
        self.end_headers()
        self.wfile.write(payload)

# ── FRONTEND HTML ─────────────────────────────────────────────────────────────
def get_html():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NewsHound</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --ink:#0f0f0f; --paper:#f5f0e8; --cream:#ede8de; --rule:#c8bfae;
    --accent:#c1440e; --accent-dark:#8f3209; --muted:#7a7267; --blue:#0073C8;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'IBM Plex Sans',sans-serif;background:var(--paper);color:var(--ink);min-height:100vh}

  header{border-bottom:3px double var(--ink);padding:18px 36px 13px;display:flex;align-items:baseline;gap:16px;background:var(--paper);position:sticky;top:0;z-index:100}
  header h1{font-family:'Playfair Display',serif;font-size:clamp(1.4rem,2.5vw,1.9rem);font-weight:700;letter-spacing:-0.02em}
  header h1 span{color:var(--accent)}
  header .tagline{font-family:'IBM Plex Mono',monospace;font-size:0.6rem;color:var(--muted);letter-spacing:0.14em;text-transform:uppercase;margin-left:auto}

  .shell{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 65px)}

  aside{border-right:1px solid var(--rule);padding:22px 18px;background:var(--cream);display:flex;flex-direction:column;gap:18px;overflow-y:auto}
  .slabel{font-family:'IBM Plex Mono',monospace;font-size:0.58rem;letter-spacing:0.18em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--rule);padding-bottom:4px;margin-bottom:7px}
  .hint{font-size:0.66rem;color:var(--muted);margin-top:4px;line-height:1.5}

  /* category builder */
  .cat-builder{display:flex;gap:5px}
  .cat-builder input{flex:1;background:var(--paper);border:1px solid var(--rule);padding:7px 9px;font-family:'IBM Plex Mono',monospace;font-size:0.75rem;color:var(--ink);outline:none;text-transform:uppercase}
  .cat-builder input::placeholder{text-transform:none}
  .cat-builder input:focus{border-color:var(--blue)}
  .cat-builder button{background:var(--blue);color:#fff;border:none;padding:7px 11px;font-family:'IBM Plex Mono',monospace;font-size:0.8rem;cursor:pointer;flex-shrink:0;transition:opacity .15s}
  .cat-builder button:hover{opacity:.85}

  /* category cards */
  .cat-list{display:flex;flex-direction:column;gap:7px}
  .cat-card{background:var(--paper);border:1px solid var(--rule);padding:9px 11px}
  .cat-card-hdr{display:flex;align-items:center;gap:7px;margin-bottom:7px}
  .cat-lbl{font-family:'IBM Plex Mono',monospace;font-size:0.7rem;font-weight:500;color:var(--blue);letter-spacing:.1em;text-transform:uppercase;flex:1}
  .cat-rm{background:none;border:none;color:var(--rule);cursor:pointer;font-size:1rem;line-height:1;padding:0;transition:color .15s}
  .cat-rm:hover{color:var(--accent)}
  .kw-row{display:flex;gap:4px;margin-bottom:5px}
  .kw-row input{flex:1;background:var(--cream);border:1px solid var(--rule);padding:5px 7px;font-family:'IBM Plex Mono',monospace;font-size:0.68rem;color:var(--ink);outline:none}
  .kw-row input:focus{border-color:var(--blue)}
  .kw-row button{background:var(--ink);color:var(--paper);border:none;padding:5px 9px;font-size:0.78rem;cursor:pointer;transition:background .15s}
  .kw-row button:hover{background:var(--blue)}
  .chips{display:flex;flex-wrap:wrap;gap:3px;min-height:16px}
  .chip{display:inline-flex;align-items:center;gap:4px;background:var(--ink);color:var(--paper);font-family:'IBM Plex Mono',monospace;font-size:0.61rem;padding:2px 6px 2px 8px;border-radius:2px;animation:pop .15s ease}
  @keyframes pop{from{transform:scale(.8);opacity:0}to{transform:scale(1);opacity:1}}
  .chip button{background:none;border:none;color:#aaa;cursor:pointer;font-size:.82rem;line-height:1;padding:0}
  .chip button:hover{color:var(--accent)}
  .no-kw{font-family:'IBM Plex Mono',monospace;font-size:0.6rem;color:var(--muted);font-style:italic}

  /* sources */
  .src-list{display:flex;flex-direction:column;gap:5px}
  .src-item{display:flex;align-items:center;gap:8px;padding:7px 9px;background:var(--paper);border:1px solid var(--rule);cursor:pointer;transition:border-color .2s;user-select:none}
  .src-item:hover{border-color:var(--ink)}
  .src-item.on{border-color:var(--accent);background:#fff8f5}
  .src-dot{width:7px;height:7px;border-radius:50%;background:var(--rule);flex-shrink:0;transition:background .2s}
  .src-item.on .src-dot{background:var(--accent)}
  .src-name{font-size:0.8rem;font-weight:500}
  .src-tag{margin-left:auto;font-family:'IBM Plex Mono',monospace;font-size:0.57rem;color:var(--muted)}

  /* options */
  .opt-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
  .opt-lbl{font-size:0.76rem}
  select,input[type=number]{background:var(--paper);border:1px solid var(--rule);padding:5px 8px;font-family:'IBM Plex Mono',monospace;font-size:0.68rem;color:var(--ink);outline:none;cursor:pointer}
  select:focus,input[type=number]:focus{border-color:var(--accent)}
  input[type=number]{width:62px}

  #digest-title{width:100%;background:var(--paper);border:1px solid var(--rule);padding:7px 9px;font-family:'IBM Plex Mono',monospace;font-size:0.76rem;color:var(--ink);outline:none}
  #digest-title:focus{border-color:var(--accent)}

  .btn-run{width:100%;background:var(--accent);color:var(--paper);border:none;padding:12px;font-family:'Playfair Display',serif;font-size:.95rem;font-weight:700;letter-spacing:.03em;cursor:pointer;transition:background .2s,transform .1s}
  .btn-run:hover{background:var(--accent-dark)}
  .btn-run:active{transform:scale(.98)}
  .btn-run:disabled{background:var(--rule);cursor:not-allowed}
  .btn-dl{width:100%;background:var(--ink);color:var(--paper);border:none;padding:10px;font-family:'IBM Plex Mono',monospace;font-size:0.7rem;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:background .2s;margin-top:6px;display:none}
  .btn-dl:hover{background:var(--blue)}
  .btn-dl.show{display:block}

  /* main */
  main{padding:26px 30px;overflow-y:auto}
  #status-bar{display:none;align-items:center;gap:10px;padding:9px 13px;background:var(--cream);border:1px solid var(--rule);margin-bottom:18px;font-family:'IBM Plex Mono',monospace;font-size:0.68rem;color:var(--muted)}
  #status-bar.show{display:flex}
  .spin{width:12px;height:12px;border:2px solid var(--rule);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
  @keyframes spin{to{transform:rotate(360deg)}}
  #sum-bar{display:none;margin-bottom:18px;padding:10px 15px;background:var(--ink);color:var(--paper);font-family:'IBM Plex Mono',monospace;font-size:0.68rem;letter-spacing:.04em;gap:16px;flex-wrap:wrap}
  #sum-bar.show{display:flex}
  #sum-bar strong{color:#f5c57a}

  .cat-sec{margin-bottom:26px;animation:fadeUp .3s ease forwards;opacity:0}
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
  .cat-hdr{display:flex;align-items:baseline;gap:11px;padding-bottom:7px;border-bottom:3px solid var(--blue);margin-bottom:11px}
  .cat-hdr h2{font-family:'IBM Plex Mono',monospace;font-size:1.05rem;font-weight:500;letter-spacing:.12em;color:var(--blue);text-transform:uppercase}
  .cat-hdr .cnt{font-family:'IBM Plex Mono',monospace;font-size:0.63rem;color:var(--muted)}

  .art-card{display:grid;grid-template-columns:1fr auto;gap:11px;padding:10px 0;border-bottom:1px solid var(--rule);text-decoration:none;color:inherit}
  .art-card:hover .art-title{color:var(--blue)}
  .art-title{font-family:'Playfair Display',serif;font-size:.9rem;font-weight:700;line-height:1.35;transition:color .15s;margin-bottom:3px}
  .art-meta{font-family:'IBM Plex Mono',monospace;font-size:.61rem;color:var(--muted)}
  .art-kws{display:flex;flex-wrap:wrap;gap:3px;margin-top:3px}
  .kw-badge{font-family:'IBM Plex Mono',monospace;font-size:.55rem;padding:1px 5px;background:#dff0ff;color:#005a9e;border-radius:2px}
  .art-icon{align-self:center;color:var(--rule);font-size:.76rem;transition:color .15s;flex-shrink:0}
  .art-card:hover .art-icon{color:var(--blue)}

  .empty{text-align:center;padding:55px 28px;color:var(--muted)}
  .empty .big{font-family:'Playfair Display',serif;font-size:3.8rem;color:var(--rule);display:block;margin-bottom:11px}
  .empty p{font-size:.84rem;line-height:1.8}
  .nores{font-family:'IBM Plex Mono',monospace;font-size:.71rem;color:var(--muted);padding:8px 0;font-style:italic}
</style>
</head>
<body>
<header>
  <h1>News<span>Hound</span></h1>
  <p class="tagline">Category Digest Builder</p>
</header>
<div class="shell">
  <aside>
    <div>
      <p class="slabel">Categories</p>
      <div class="cat-builder">
        <input type="text" id="new-cat" placeholder="e.g. WATER, POWER…">
        <button type="button" id="add-cat-btn">+ Add</button>
      </div>
      <p class="hint">Each category = one blue section in the digest. Add keywords to match articles.</p>
    </div>
    <div id="cat-list" class="cat-list"></div>
    <div>
      <p class="slabel">Sources</p>
      <div class="src-list" id="src-list"></div>
    </div>
    <div>
      <p class="slabel">Options</p>
      <div class="opt-row"><span class="opt-lbl">Max per source</span><input type="number" id="max-per" value="50" min="5" max="100" step="5"></div>
      <div class="opt-row"><span class="opt-lbl">Match in</span>
        <select id="match-field">
          <option value="both">Title + Description</option>
          <option value="title">Title only</option>
          <option value="desc">Description only</option>
        </select>
      </div>
    </div>
    <div>
      <p class="slabel">Digest Masthead</p>
      <input type="text" id="digest-title" value="LCRA" placeholder="e.g. LCRA">
      <p class="hint" style="margin-top:4px">Top header in the downloaded HTML digest.</p>
    </div>
    <div style="margin-top:auto">
      <button class="btn-run" id="run-btn">Search Articles →</button>
      <button class="btn-dl" id="dl-btn">⬇ Download Digest</button>
    </div>
  </aside>
  <main>
    <div class="empty" id="empty-state">
      <span class="big">⌕</span>
      <p>Create categories like <strong>WATER</strong> or <strong>POWER</strong>,<br>
      add keywords to each, pick sources,<br>then hit <strong>Search Articles</strong>.<br><br>
      Use <strong>⬇ Download Digest</strong> to export<br>an LCRA-style HTML report.</p>
    </div>
    <div id="status-bar"><div class="spin"></div><span id="status-txt">Fetching feeds…</span></div>
    <div id="sum-bar"></div>
    <div id="results"></div>
  </main>
</div>

<script>
const SOURCES_LIST = [];
let categories = [];
let activeSources = new Set();
let digestData = [];

// Load sources from server
fetch('/sources').then(r => r.json()).then(names => {
  names.forEach((name, i) => {
    SOURCES_LIST.push(name);
    activeSources.add(name);
    const el = document.createElement('div');
    el.className = 'src-item on';
    el.innerHTML = `<div class="src-dot"></div><span class="src-name">${name}</span>`;
    el.addEventListener('click', () => {
      if (activeSources.has(name)) {
        if (activeSources.size === 1) return;
        activeSources.delete(name); el.classList.remove('on');
      } else { activeSources.add(name); el.classList.add('on'); }
    });
    document.getElementById('src-list').appendChild(el);
  });
});

// Categories
document.getElementById('add-cat-btn').addEventListener('click', addCat);
document.getElementById('new-cat').addEventListener('keydown', e => { if (e.key === 'Enter') addCat(); });

function addCat() {
  const inp = document.getElementById('new-cat');
  const name = inp.value.trim().toUpperCase();
  if (!name || categories.find(c => c.name === name)) { inp.select(); return; }
  categories.push({ id: 'c' + Date.now(), name, keywords: [] });
  inp.value = '';
  renderCats();
}

function removeCat(id) { categories = categories.filter(c => c.id !== id); renderCats(); }
function addKw(id, raw) {
  const cat = categories.find(c => c.id === id);
  if (!cat) return;
  const kw = raw.trim().toLowerCase().replace(/,/g, '');
  if (!kw || cat.keywords.includes(kw)) return;
  cat.keywords.push(kw); renderCats();
}
function removeKw(id, kw) {
  const cat = categories.find(c => c.id === id);
  if (cat) { cat.keywords = cat.keywords.filter(k => k !== kw); renderCats(); }
}

function renderCats() {
  const el = document.getElementById('cat-list');
  el.innerHTML = '';
  categories.forEach(cat => {
    const card = document.createElement('div');
    card.className = 'cat-card';
    const chips = cat.keywords.length
      ? cat.keywords.map(kw => `<span class="chip">${kw}<button data-id="${cat.id}" data-kw="${kw}">×</button></span>`).join('')
      : `<span class="no-kw">No keywords yet</span>`;
    card.innerHTML = `
      <div class="cat-card-hdr">
        <span class="cat-lbl">${cat.name}</span>
        <button type="button" class="cat-rm" data-rm="${cat.id}">×</button>
      </div>
      <div class="kw-row">
        <input type="text" placeholder="Add keyword…" data-inp="${cat.id}">
        <button type="button" data-add="${cat.id}">+</button>
      </div>
      <div class="chips">${chips}</div>`;
    el.appendChild(card);

    card.querySelector(`[data-rm="${cat.id}"]`).addEventListener('click', () => removeCat(cat.id));
    const kwInp = card.querySelector(`[data-inp="${cat.id}"]`);
    card.querySelector(`[data-add="${cat.id}"]`).addEventListener('click', () => { addKw(cat.id, kwInp.value); kwInp.value = ''; });
    kwInp.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addKw(cat.id, kwInp.value); kwInp.value = ''; } });
    card.querySelectorAll('.chip button').forEach(b => b.addEventListener('click', () => removeKw(b.dataset.id, b.dataset.kw)));
  });
}

// Search
document.getElementById('run-btn').addEventListener('click', runSearch);

async function runSearch() {
  const catsWithKws = categories.filter(c => c.keywords.length);
  if (!catsWithKws.length) { alert('Add at least one category with keywords.'); return; }

  const runBtn = document.getElementById('run-btn');
  const statusBar = document.getElementById('status-bar');
  const statusTxt = document.getElementById('status-txt');
  const resultsEl = document.getElementById('results');
  const sumBar    = document.getElementById('sum-bar');
  const dlBtn     = document.getElementById('dl-btn');
  const emptyState = document.getElementById('empty-state');

  runBtn.disabled = true;
  emptyState.style.display = 'none';
  resultsEl.innerHTML = '';
  sumBar.innerHTML = ''; sumBar.classList.remove('show');
  dlBtn.classList.remove('show');
  statusBar.classList.add('show');
  statusTxt.textContent = 'Fetching feeds from server…';
  digestData = [];

  try {
    const payload = {
      categories: catsWithKws.map(c => ({ name: c.name, keywords: c.keywords })),
      sources: [...activeSources],
      max_per_source: parseInt(document.getElementById('max-per').value) || 50,
      match_field: document.getElementById('match-field').value,
    };

    const resp = await fetch('/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    digestData = data;

    let total = 0;
    let delay = 0;
    for (const section of data) {
      total += section.articles.length;
      const sec = document.createElement('div');
      sec.className = 'cat-sec';
      sec.style.animationDelay = `${delay}ms`;
      delay += 70;

      let html = `<div class="cat-hdr"><h2>${section.catName}</h2><span class="cnt">${section.articles.length} article${section.articles.length !== 1 ? 's' : ''}</span></div>`;
      if (!section.articles.length) {
        html += `<p class="nores">No articles matched this category's keywords.</p>`;
      } else {
        for (const art of section.articles) {
          html += `
            <a class="art-card" href="${art.link}" target="_blank" rel="noopener">
              <div>
                <div class="art-title">${art.title}</div>
                <div class="art-meta">${art.date ? art.date + ' · ' : ''}${art.pub}</div>
                <div class="art-kws">${art.hitKws.map(k => `<span class="kw-badge">${k}</span>`).join('')}</div>
              </div>
              <span class="art-icon">↗</span>
            </a>`;
        }
      }
      sec.innerHTML = html;
      resultsEl.appendChild(sec);
    }

    sumBar.innerHTML = `<span><strong>${total}</strong> total matches</span><span><strong>${catsWithKws.length}</strong> categories</span><span><strong>${[...activeSources].length}</strong> sources</span>`;
    sumBar.classList.add('show');
    if (total > 0) dlBtn.classList.add('show');
    if (total === 0) {
      resultsEl.innerHTML = `<div class="empty"><span class="big" style="font-size:3rem">∅</span><p>No articles matched any category.<br>Try broader keywords.</p></div>`;
    }
  } catch(e) {
    resultsEl.innerHTML = `<div class="empty"><span class="big" style="font-size:2.5rem">!</span><p>Error: ${e.message}</p></div>`;
  }

  statusBar.classList.remove('show');
  runBtn.disabled = false;
}

// Download digest
document.getElementById('dl-btn').addEventListener('click', () => {
  const title = document.getElementById('digest-title').value.trim() || 'News Digest';
  const today = new Date().toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });

  let sections = '';
  for (const sec of digestData) {
    if (!sec.articles.length) continue;

    // Blue category header
    sections += `
    <table style="min-width:100%;border-collapse:collapse" width="100%" cellspacing="0" cellpadding="0" border="0">
      <tbody><tr><td style="padding-top:9px" valign="top">
        <table style="max-width:100%;min-width:100%;border-collapse:collapse;float:left" width="100%" cellspacing="0" cellpadding="0" border="0" align="left">
          <tbody><tr>
            <td style="padding:0px 18px 9px;text-align:left;word-break:break-word;color:#696969;font-family:Helvetica;font-size:16px;line-height:100%" valign="top">
              <h1 style="display:block;margin:0;padding:0;color:#202020;font-family:Helvetica;font-size:26px;font-style:normal;font-weight:bold;line-height:125%;letter-spacing:normal;text-align:left">
                <span style="color:#0073C8">${sec.catName}</span>
              </h1>
            </td>
          </tr></tbody>
        </table>
      </td></tr></tbody>
    </table>`;

    let artRows = '';
    for (const art of sec.articles) {
      artRows += `
        <p style="font-style:normal;font-weight:normal;line-height:125%;margin:10px 0;padding:0;color:#696969;font-family:Helvetica;font-size:16px;text-align:left" dir="ltr">
          <a style="color:#0073c8;font-weight:normal;text-decoration:underline" href="${art.link}" target="_blank" rel="noopener noreferrer">${art.title}</a>
        </p>
        <p style="font-style:normal;font-weight:normal;line-height:125%;margin:2px 0 14px;padding:0;color:#696969;font-family:Helvetica;font-size:16px;text-align:left" dir="ltr">
          ${art.date ? art.date + ' ' : ''}${art.pub}
        </p>`;
    }

    sections += `
    <table style="min-width:100%;border-collapse:collapse" width="100%" cellspacing="0" cellpadding="0" border="0">
      <tbody><tr><td style="padding-top:9px" valign="top">
        <table style="max-width:100%;min-width:100%;border-collapse:collapse;float:left" width="100%" cellspacing="0" cellpadding="0" border="0" align="left">
          <tbody><tr>
            <td style="padding:0px 18px 9px;font-style:normal;font-weight:normal;line-height:125%;word-break:break-word;color:#696969;font-family:Helvetica;font-size:16px;text-align:left" valign="top">
              ${artRows}
            </td>
          </tr></tbody>
        </table>
      </td></tr></tbody>
    </table>`;
  }

  const html = `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>${title} – ${today}</title></head>
<body style="background:#ffffff;margin:0;padding:0;font-family:Helvetica,sans-serif;">
<table style="width:100%;max-width:680px;margin:0 auto;background:#ffffff" cellspacing="0" cellpadding="0" border="0">
  <tbody>
    <tr><td style="padding:28px 18px 4px;border-bottom:2px solid #cccccc;">
      <table width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>
        <td><h1 style="font-family:Helvetica;font-size:26px;font-weight:bold;margin:0;padding:0;line-height:125%;text-align:left"><span style="color:#0073C8">${title}</span></h1></td>
        <td style="text-align:right;vertical-align:bottom;"><span style="font-family:Helvetica;font-size:12px;color:#888888;">${today}</span></td>
      </tr></tbody></table>
    </td></tr>
    <tr><td style="background:#ffffff;padding-top:0" valign="top">${sections}</td></tr>
    <tr><td style="padding:16px 18px;border-top:1px solid #cccccc;">
      <p style="font-family:Helvetica;font-size:11px;color:#aaaaaa;margin:0;text-align:center;">Generated by LCRA Clips· ${today}</p>
    </td></tr>
  </tbody>
</table></body></html>`;

  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
  a.download = `${title.replace(/\s+/g,'-').toLowerCase()}-${new Date().toISOString().slice(0,10)}.html`;
  a.click();
});
</script>
</body>
</html>"""

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 8765))
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'NewsHound starting on port {PORT}...')

    # Only auto-open browser when running locally (no PORT env var set by host)
    if not os.environ.get('PORT'):
        url = f'http://localhost:{PORT}'
        print(f'Open in browser: {url}')
        def open_browser():
            time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('NewsHound stopped.')
