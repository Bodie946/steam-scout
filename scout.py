"""Steam Scout: local recommendations. Python 3.10+, standard library only."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent


class APIError(RuntimeError):
    pass


class Client:
    def __init__(self, key, db):
        self.key, self.db = key, db

    def get(self, base, params, cache=False):
        # Cache keys never contain credentials.
        cache_key = base + '?' + urlencode(params)
        if cache:
            row = self.db.execute('SELECT payload, fetched FROM cache WHERE key=?', (cache_key,)).fetchone()
            if row and time.time() - row[1] < 30 * 86400:
                return json.loads(row[0])
        query = dict(params)
        if 'isthereanydeal.com' in base:
            query['key'] = self.key
        for attempt in range(3):
            try:
                req = Request(base + '?' + urlencode(query), headers={'User-Agent': 'SteamScout/0.1'})
                with urlopen(req, timeout=30) as response:
                    result = json.load(response)
                if cache:
                    self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (cache_key, json.dumps(result), time.time()))
                    self.db.commit()
                time.sleep(0.3)
                return result
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise APIError(f'API returned HTTP {exc.code}; check credentials or try later.') from None
                time.sleep(min(30, 2 ** (attempt + 1)))
            except (URLError, TimeoutError, json.JSONDecodeError):
                if attempt == 2:
                    raise APIError('Network request failed or returned invalid JSON; try again later.') from None
                time.sleep(2 ** (attempt + 1))

    def itad(self, path, cache=False, **params):
        return self.get('https://api.isthereanydeal.com' + path, params, cache)


def database(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT, fetched REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS runs (created TEXT, country TEXT, payload TEXT)')
    return db


def taste(library, favorites):
    weights = Counter()
    for game in library:
        minutes = game.get('playtime_forever', 0)
        if minutes <= 0:
            continue
        weight = min(math.log1p(minutes / 60), 6) + min(game.get('playtime_2weeks', 0) / 120, 3)
        tags = set(t.casefold() for t in game.get('tags', []))
        for tag in tags:
            weights[tag] += weight / max(1, len(tags))
    boost = max(weights.values(), default=1)
    for tag in favorites:
        weights[tag.casefold()] += boost
    peak = max(weights.values(), default=1)
    return {tag: weight / peak for tag, weight in weights.items()}


def rank(library, deals, cfg, owned_ids=None):
    owned = set(owned_ids or ()) | {g['appid'] for g in library} | set(cfg.get('ignored_appids', []))
    profile = taste(library, cfg.get('favorite_tags', []))
    excluded = {t.casefold() for t in cfg.get('excluded_tags', [])}
    output, seen = [], set()
    for game in deals:
        appid = game.get('appid')
        if not appid or appid in owned or appid in seen or game.get('type') != 'game':
            continue
        offer = game['deal']
        if offer.get('shop', {}).get('name', '').casefold() != 'steam':
            continue
        price, cut = offer['price']['amount'], offer['cut']
        if cut <= 0 or price > cfg['max_price'] or cut < cfg['min_discount']:
            continue
        expiry = offer.get('expiry')
        if expiry:
            try:
                if datetime.fromisoformat(expiry.replace('Z', '+00:00')).astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    continue
            except ValueError:
                continue
        tags = {t.casefold() for t in game.get('tags', [])}
        if tags & excluded:
            continue
        matched = sorted(tags & profile.keys(), key=lambda t: profile[t], reverse=True)
        fit = sum(profile[t] for t in matched[:3]) / 3
        reviews = next((r for r in game.get('reviews', []) if r.get('source') == 'Steam'), {})
        count = max(0, reviews.get('count', 0))
        quality = ((reviews.get('score') or 50) * count + 50 * 100) / (count + 100) / 100
        score = 70 * fit + 20 * quality + 10 * min(cut, 100) / 100
        reason = 'Matches your interest in ' + ', '.join(matched[:3]) if matched else 'Discovery pick; no matching play-history tags'
        output.append(dict(game, score=round(score, 1), reason=reason))
        seen.add(appid)
    return sorted(output, key=lambda g: (-g['score'], g['deal']['price']['amount']))[:cfg['top_n']]


def collect(client, cfg, secrets):
    response = client.get('https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/', {
        'key': secrets['steam_api_key'], 'steamid': cfg['steam_id'],
        'include_appinfo': 1, 'include_played_free_games': 1
    }).get('response', {})
    if 'games' not in response:
        raise APIError('Steam did not return a library. Check the Steam ID and make Game details public. An empty library also cannot generate a taste profile.')
    all_games = response['games']
    owned = {g['appid'] for g in all_games}
    sample = sorted(all_games, key=lambda g: min(math.log1p(g.get('playtime_forever', 0)), 10) + min(g.get('playtime_2weeks', 0)/120, 3), reverse=True)[:cfg['library_sample']]
    library = []
    for i, game in enumerate(sample):
        print(f'Library metadata {i+1}/{len(sample)}', flush=True)
        lookup = client.itad('/games/lookup/v1', cache=True, appid=game['appid'])
        info = client.itad('/games/info/v2', cache=True, id=lookup['game']['id']) if lookup.get('found') else {}
        library.append(dict(game, tags=info.get('tags', [])))
    shops = client.itad('/service/shops/v1', country=cfg['country'])
    shop = next((s['id'] for s in shops if s['title'].casefold() == 'steam'), None)
    if shop is None:
        raise APIError('No Steam pricing source available for this country.')
    deals, offset, visited = [], 0, set()
    while len(deals) < cfg['max_deals']:
        if offset in visited:
            raise APIError('Pricing API returned a repeated page offset.')
        visited.add(offset)
        page = client.itad('/deals/v2', country=cfg['country'], shops=str(shop), offset=offset,
                           limit=min(200, cfg['max_deals']-len(deals)))
        deals.extend(page.get('list', []))
        if not page.get('hasMore') or not page.get('list'):
            break
        offset = page['nextOffset']
    candidates = []
    for i, game in enumerate(deals):
        if game.get('type') != 'game' or game['deal']['price']['amount'] > cfg['max_price'] or game['deal']['cut'] < cfg['min_discount']:
            continue
        print(f'Deal metadata {i+1}/{len(deals)}', flush=True)
        info = client.itad('/games/info/v2', cache=True, id=game['id'])
        candidates.append(dict(info, deal=game['deal']))
    return library, candidates, owned, len(deals)


def report(items, cfg, demo, scanned):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for game in items:
        deal = game['deal']
        url = deal.get('url', '')
        if urlparse(url).scheme != 'https':
            url = f"https://store.steampowered.com/app/{int(game['appid'])}/"
        cards.append(f'''<article><div class="discount">−{esc(deal['cut'])}%</div>
        <h2>{esc(game['title'])}</h2><p>{esc(game['reason'])}</p>
        <div class="price">{esc(deal['price']['currency'])} {deal['price']['amount']:.2f}</div>
        <p class="small">Recommendation score {game['score']} · {esc(', '.join(game.get('tags', [])[:4]))}</p>
        <a href="{esc(url)}" target="_blank" rel="noopener noreferrer">View offer ↗</a></article>''')
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')
    mode = 'DEMO · FICTIONAL PRICES AND LIBRARY' if demo else 'PERSONAL STEAM DEALS'
    content = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Steam Scout</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#0c1520;color:#edf5fa;font:16px system-ui,sans-serif}}
    main{{max-width:1100px;margin:auto;padding:55px 24px}}header{{margin-bottom:35px}}.eyebrow{{color:#76e3c2;letter-spacing:2px;font-size:12px}}
    h1{{font-size:48px;letter-spacing:-2px;margin:12px 0}}h2{{font-size:22px;padding-right:65px}}p{{color:#acbecb;line-height:1.6}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:18px}}article{{position:relative;background:#152333;border:1px solid #293c4c;border-radius:16px;padding:25px}}
    .discount{{position:absolute;right:20px;top:25px;color:#76e3c2;font-weight:700}}.price{{font-size:28px;font-weight:650}}.small{{font-size:12px}}
    a{{color:#76e3c2}}footer{{margin-top:30px;font-size:13px}}.notice{{border-left:3px solid #76e3c2;padding-left:16px}}
    </style><main><header><div class="eyebrow">{mode}</div><h1>Steam Scout</h1>
    <p>Your next game, at a better price.</p><p class="notice">{esc(stamp)} · Region {esc(cfg['country'])} · {scanned} offers scanned<br>
    Budget {esc(cfg['max_price'])} in regional currency · Minimum discount {esc(cfg['min_discount'])}%<br>
    Snapshot only: check the offer before buying. This file does not refresh itself.</p></header>
    <section class="grid">{''.join(cards) or '<p>No matches. Try a higher budget, a lower discount threshold, or favorite tags.</p>'}</section>
    <footer>Pricing and game metadata: <a href="https://isthereanydeal.com/">IsThereAnyDeal</a>. Library: Steam.<br>
    Scores are ranking signals, not probabilities. Scan limits may omit relevant deals.</footer></main></html>'''
    path = ROOT / ('demo.html' if demo else 'report.html')
    temp = path.with_suffix('.tmp')
    temp.write_text(content, encoding='utf-8')
    temp.replace(path)
    return path


def demo_data():
    library = [{'appid': 1, 'playtime_forever': 1800, 'playtime_2weeks': 180, 'tags': ['Strategy', 'Turn-Based', 'RPG']}]
    deals = []
    for appid, title, tags, price, cut in [(2, 'Clockwork Expedition', ['Strategy', 'Turn-Based'], 14.99, 50),
                                          (3, 'Emberfall Chronicles', ['RPG', 'Adventure'], 19.99, 40),
                                          (4, 'Orbit Architect', ['Simulation', 'Strategy'], 8.99, 70)]:
        deals.append({'appid': appid, 'title': title, 'type': 'game', 'tags': tags,
                      'deal': {'price': {'amount': price, 'currency': 'USD'}, 'cut': cut,
                               'shop': {'name': 'Steam'}, 'url': 'https://isthereanydeal.com/'}})
    return library, deals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--open', action='store_true', help='Open report in your browser')
    parser.add_argument('--daily', action='store_true', help='Keep running; refresh every 24 hours while this process is alive')
    args = parser.parse_args()
    while True:
        try:
            cfg = json.loads((ROOT / ('config.example.json' if args.demo else 'config.json')).read_text())
            if cfg['max_price'] < 0 or not 0 <= cfg['min_discount'] <= 100 or min(cfg['library_sample'], cfg['max_deals'], cfg['top_n']) < 1:
                raise ValueError('Invalid numeric configuration values')
            if len(cfg['country']) != 2 or not cfg['country'].isalpha():
                raise ValueError('Country must be a two-letter code')
            cfg['country'] = cfg['country'].upper()
            if args.demo:
                library, deals = demo_data()
                items = rank(library, deals, cfg)
                path = report(items, cfg, True, len(deals))
            else:
                if not str(cfg['steam_id']).isdigit() or len(str(cfg['steam_id'])) != 17:
                    raise ValueError('Set your 17-digit Steam ID in config.json')
                secrets = json.loads((ROOT / 'secrets.json').read_text())
                with database(ROOT / 'scout.sqlite3') as db:
                    library, deals, owned, scanned = collect(Client(secrets['itad_api_key'], db), cfg, secrets)
                    items = rank(library, deals, cfg, owned)
                    path = report(items, cfg, False, scanned)
                    db.execute('INSERT INTO runs VALUES (?,?,?)', (datetime.now(timezone.utc).isoformat(), cfg['country'], json.dumps(items)))
            print(f'Ready: {path}')
            if args.open:
                webbrowser.open(path.as_uri())
        except (APIError, OSError, ValueError, KeyError, TypeError) as exc:
            # Never print raw request URLs or secret values.
            message = str(exc) if isinstance(exc, APIError) else 'Check config.json and secrets.json against README; ensure the folder is writable.'
            print('Refresh failed: ' + message)
            print('Any previous report remains a dated snapshot, not current prices.')
            if not args.daily:
                return 1
        if not args.daily or args.demo:
            return 0
        print('Next refresh in 24 hours. Keep this process and computer running. Ctrl+C stops it.', flush=True)
        time.sleep(86400)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Stopped.')
