"""Public catalogue crawler. Python 3.10+. Run --help for commands.

No private API, browser impersonation, or challenge bypass. Site adapters
are deliberately conservative: missing product evidence is not fabricated.
"""
import argparse
import gzip
import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from bs4 import BeautifulSoup
from defusedxml import ElementTree
from protego import Protego

LOG = logging.getLogger('crawler')
BOT = 'ZaCatalogueBot'
MAX_BYTES = 30 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def array(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def types(node):
    return {str(t).rstrip('/').split('/')[-1] for t in array(node.get('@type'))}


def clean_text(value):
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)


def amount(value):
    """Schema prices use a decimal dot; ambiguous localized strings stay null."""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not re.fullmatch(r'\d+(?:\.\d+)?', s):
        return None
    try:
        d = Decimal(s)
        return str(d) if d.is_finite() and d >= 0 else None
    except InvalidOperation:
        return None


def normalize(url, base):
    p = urlsplit(urljoin(base, url))
    b = urlsplit(base)
    # Only the explicitly configured origin. Never forward to unknown hosts.
    if p.scheme != 'https' or p.netloc.lower() != b.netloc.lower():
        return None
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in
             {'fbclid', 'gclid', 'itm_source', 'itm_medium', 'itm_campaign', 'itm_content'}]
    return urlunsplit(('https', b.netloc, p.path or '/', urlencode(query), ''))


def roots(value):
    """Only document-level entities, not Products buried in recommendation lists."""
    if isinstance(value, list):
        for v in value:
            yield from roots(v)
    elif isinstance(value, dict):
        yield value
        for key in ('@graph', 'mainEntity'):
            if key in value:
                yield from roots(value[key])


def extract(html, url, site, config):
    soup = BeautifulSoup(html, 'html.parser')
    nodes, warnings = [], []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            nodes.extend(roots(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError, RecursionError):
            warnings.append('invalid_jsonld')
    products = [n for n in nodes if 'Product' in types(n)]
    for n in nodes:
        if 'ProductGroup' in types(n):
            products.extend(v for v in array(n.get('hasVariant'))
                            if isinstance(v, dict) and 'Product' in types(v))
    breadcrumbs = []
    for n in nodes:
        if 'BreadcrumbList' in types(n):
            for v in array(n.get('itemListElement')):
                if isinstance(v, dict):
                    item = v.get('item')
                    label = v.get('name') or (item.get('name') if isinstance(item, dict) else None)
                    if label:
                        breadcrumbs.append(clean_text(label))
    specs = {}
    if config.get('spec_rows'):
        for row in soup.select(config['spec_rows']):
            key, val = row.select_one(config['spec_key']), row.select_one(config['spec_value'])
            if key and val and key is not val:
                k, v = key.get_text(' ', strip=True), val.get_text(' ', strip=True)
                if k and v:
                    specs[k] = v
    records = []
    for p in products:
        name = clean_text(p.get('name'))
        if not name:
            continue
        brand = p.get('brand')
        brand = brand.get('name') if isinstance(brand, dict) else brand
        images = []
        for image in array(p.get('image')):
            image = image.get('contentUrl') or image.get('url') if isinstance(image, dict) else image
            if isinstance(image, str) and urlsplit(urljoin(url, image)).scheme in ('https', 'http'):
                images.append(urljoin(url, image))
        offers = []
        for o in array(p.get('offers')):
            if not isinstance(o, dict):
                continue
            offers.append({
                'type': o.get('@type'), 'price': amount(o.get('price')),
                'low_price': amount(o.get('lowPrice')), 'high_price': amount(o.get('highPrice')),
                'currency': o.get('priceCurrency'), 'availability': o.get('availability'),
                'url': o.get('url'), 'raw': o,
            })
        own_specs = dict(specs)
        for prop in array(p.get('additionalProperty')):
            if isinstance(prop, dict) and prop.get('name') and prop.get('value') is not None:
                own_specs[str(prop['name'])] = prop['value']
        sku = p.get('sku')
        identity = str(sku or p.get('@id') or p.get('url') or name)
        record = {
            'id': hashlib.sha256(f'{site}|{url}|{identity}'.encode()).hexdigest(),
            'source': site, 'source_url': url, 'collected_at': now(),
            'name': name, 'brand': brand, 'sku': sku, 'mpn': p.get('mpn'),
            'gtin': next((p[k] for k in ('gtin', 'gtin13', 'gtin14', 'gtin12', 'gtin8') if p.get(k)), None),
            'category': p.get('category'), 'breadcrumbs': breadcrumbs,
            'description': clean_text(p.get('description')) or None,
            'images': list(dict.fromkeys(images)), 'offers': offers,
            'specifications': own_specs, 'color': p.get('color'), 'size': p.get('size'),
            'rating': p.get('aggregateRating'), 'extraction': 'jsonld',
            'warnings': warnings + ([] if offers else ['missing_offers'])
                        + ([] if own_specs else ['missing_specifications']),
            'raw_product': p,
        }
        records.append(record)
    links = [a['href'] for a in soup.select('a[href]')]
    return records, links


class StopSite(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, config, delay, agent):
        self.base, self.delay, self.agent = config['base'], delay, agent
        self.last = 0
        self.robot = None
        self.opener = build_opener(NoRedirect())

    def get(self, url, robots=False, hops=0):
        if hops > 5:
            raise ValueError('redirect loop')
        if not normalize(url, self.base):
            raise ValueError('outside configured origin')
        if not robots and (self.robot is None or not self.robot.can_fetch(url, BOT)):
            raise PermissionError('robots_disallowed')
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                with self.opener.open(Request(url, headers={'User-Agent': self.agent,
                                                          'Accept-Encoding': 'identity'}), timeout=30) as r:
                    body = r.read(MAX_BYTES + 1)
                    if len(body) > MAX_BYTES:
                        raise ValueError('response exceeds 30 MiB')
                    return body, r.headers.get('Content-Type', '')
            except HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = exc.headers.get('Location')
                    target = normalize(urljoin(url, location), self.base) if location else None
                    if not target:
                        raise ValueError('redirect outside configured origin') from exc
                    # Refuse redirects rather than parsing a category as an old product URL.
                    if not robots:
                        raise ValueError(f'redirect_requires_new_seed: {target}') from exc
                    return self.get(target, robots=True, hops=hops + 1)
                if exc.code in (401, 403, 429):
                    raise StopSite(f'HTTP {exc.code}; stop, no bypass') from exc
                if exc.code < 500 or attempt == 2:
                    raise
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(2 ** (attempt + 1))

    def load_robots(self):
        try:
            body, _ = self.get(self.base + '/robots.txt', robots=True)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            body = b'User-agent: *\nAllow: /'
        if b'<html' in body.lower():
            raise StopSite('robots.txt returned HTML; unable to verify rules')
        self.robot = Protego.parse(body.decode('utf-8', errors='replace'))
        delay = self.robot.crawl_delay(BOT)
        if delay:
            self.delay = max(self.delay, float(delay))
        return list(self.robot.sitemaps or [])


def database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS queue (
      site TEXT, url TEXT, kind TEXT, status TEXT DEFAULT 'pending', error TEXT,
      PRIMARY KEY(site,url));
    CREATE TABLE IF NOT EXISTS products (
      id TEXT PRIMARY KEY, site TEXT, url TEXT, updated_at TEXT, payload TEXT);
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY, site TEXT, started_at TEXT, ended_at TEXT, result TEXT);
    ''')
    return db


def enqueue(db, site, config, url, kind):
    url = normalize(url, config['base'])
    if not url:
        return
    path = urlsplit(url).path
    if kind == 'page' and (any(path.startswith(x) for x in config['exclude']) or
       re.search(r'\.(?:jpg|jpeg|png|gif|webp|svg|pdf|zip|css|js|mp4)$', path, re.I)):
        return
    db.execute('INSERT OR IGNORE INTO queue(site,url,kind) VALUES(?,?,?)', (site, url, kind))


def sitemap_entries(body):
    if body[:2] == b'\x1f\x8b':
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            body = stream.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('decompressed sitemap exceeds 30 MiB')
    root = ElementTree.fromstring(body)
    tag = root.tag.split('}')[-1]
    if tag not in ('sitemapindex', 'urlset'):
        raise ValueError('not a sitemap')
    kind = 'sitemap' if tag == 'sitemapindex' else 'page'
    for entry in root:
        for child in entry:
            if child.tag.split('}')[-1] == 'loc' and child.text:
                yield child.text.strip(), kind


def crawl(args, db, site, config):
    run = db.execute('INSERT INTO runs(site,started_at) VALUES(?,?)', (site, now())).lastrowid
    db.commit()
    client = Client(config, args.delay, args.user_agent)
    result = 'unknown'
    try:
        maps = client.load_robots()  # Fail closed if robots cannot be retrieved.
        if args.retry_errors:
            db.execute("UPDATE queue SET status='pending',error=NULL WHERE site=? AND status='error'", (site,))
        if not args.urls_only:
            for url in maps + config['sitemaps']:
                enqueue(db, site, config, url, 'sitemap')
            for url in config['seeds']:
                enqueue(db, site, config, url, 'page')
        if args.urls:
            for url in Path(args.urls).read_text(encoding='utf-8').splitlines():
                if url.strip() and not url.startswith('#'):
                    enqueue(db, site, config, url.strip(), 'page')
        db.commit()
        count = 0
        while args.max_pages == 0 or count < args.max_pages:
            row = db.execute("SELECT url,kind FROM queue WHERE site=? AND status='pending' ORDER BY CASE kind WHEN 'sitemap' THEN 0 ELSE 1 END,rowid LIMIT 1", (site,)).fetchone()
            if row is None:
                result = 'queue_exhausted_not_proof_of_full_coverage'
                break
            url, kind = row
            count += 1
            try:
                body, content_type = client.get(url)
                if kind == 'sitemap':
                    for link, link_kind in sitemap_entries(body):
                        enqueue(db, site, config, link, link_kind)
                    state = 'done'
                else:
                    if 'html' not in content_type.lower():
                        raise ValueError('not HTML')
                    records, links = extract(body, url, site, config)
                    for record in records:
                        db.execute('INSERT OR REPLACE INTO products VALUES(?,?,?,?,?)',
                                   (record['id'], site, url, record['collected_at'], json.dumps(record, ensure_ascii=False)))
                    if not args.urls_only:
                        for link in links:
                            enqueue(db, site, config, urljoin(url, link), 'page')
                    state = 'product' if records else 'no_product_schema'
                    LOG.info('%s: %d products %s', site, len(records), url)
                db.execute('UPDATE queue SET status=?,error=NULL WHERE site=? AND url=?', (state, site, url))
            except StopSite:
                raise  # Current URL stays pending for a later run.
            except PermissionError as exc:
                db.execute("UPDATE queue SET status='disallowed',error=? WHERE site=? AND url=?", (str(exc), site, url))
            except Exception as exc:
                LOG.warning('%s: %s', url, exc)
                db.execute("UPDATE queue SET status='error',error=? WHERE site=? AND url=?", (str(exc), site, url))
            db.commit()
        else:
            result = 'page_limit_reached'
    except KeyboardInterrupt:
        result = 'interrupted'
        raise
    except Exception as exc:
        result = f'stopped: {exc}'
        LOG.error('%s %s', site, result)
    finally:
        db.execute('UPDATE runs SET ended_at=?,result=? WHERE id=?', (now(), result, run))
        db.commit()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['crawl', 'export', 'status'])
    p.add_argument('--site', default='all', choices=['all', 'cellphones', 'tgdd', 'phongvu'])
    p.add_argument('--config', default=str(Path(__file__).with_name('sites.json')))
    p.add_argument('--db', default='data/catalogue.sqlite')
    p.add_argument('--output', default='data/products.jsonl')
    p.add_argument('--max-pages', type=int, default=100, help='per site per run; 0 = no limit, includes sitemaps')
    p.add_argument('--delay', type=float, default=2.5)
    p.add_argument('--user-agent', default=BOT + '/1.0 (educational catalogue research)')
    p.add_argument('--urls', help='optional UTF-8 file of known public product URLs')
    p.add_argument('--urls-only', action='store_true', help='no discovery; use a fresh DB to avoid an existing frontier')
    p.add_argument('--retry-errors', action='store_true')
    args = p.parse_args()
    if args.max_pages < 0 or args.delay < 1:
        p.error('max-pages must be >= 0; delay must be >= 1 second')
    if args.urls_only and not args.urls:
        p.error('--urls-only requires --urls')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with database(args.db) as db:
        if args.command == 'crawl':
            configs = json.loads(Path(args.config).read_text(encoding='utf-8'))
            for site, config in configs.items():
                if args.site in ('all', site):
                    crawl(args, db, site, config)
        elif args.command == 'export':
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            query = 'SELECT payload FROM products' + (' WHERE site=?' if args.site != 'all' else '')
            params = (args.site,) if args.site != 'all' else ()
            with out.open('w', encoding='utf-8') as f:
                for (payload,) in db.execute(query, params):
                    f.write(payload + '\n')
            LOG.info('Exported %s', out)
        else:
            print('Queue:', list(db.execute('SELECT site,status,count(*) FROM queue GROUP BY site,status')))
            print('Products:', list(db.execute('SELECT site,count(*) FROM products GROUP BY site')))
            print('Recent runs:', list(db.execute('SELECT site,started_at,result FROM runs ORDER BY id DESC LIMIT 10')))


if __name__ == '__main__':
    main()
