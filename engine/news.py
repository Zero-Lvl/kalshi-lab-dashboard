"""Public RSS headlines and publisher summaries; relevance is not trading advice."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request
from public_http import open_public
import xml.etree.ElementTree as ET

FEEDS = (
    ('BBC News', 'https://feeds.bbci.co.uk/news/rss.xml'),
    ('NYT Business', 'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml'),
    ('Federal Reserve', 'https://www.federalreserve.gov/feeds/press_all.xml'),
    ('NOAA / National Weather Service', 'https://www.weather.gov/rss_page.php?site_name=nws'),
)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _fetch_bytes(url):
    with open_public(Request(url, headers={'User-Agent': 'KalshiLab/0.1 (public RSS research)'}), timeout=8) as response:
        return response.read(1_048_577)


def _canonical_url(value):
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return ''
        host = parts.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        port = parts.port
        if port and (parts.scheme.lower(), port) not in (('http', 80), ('https', 443)):
            host += ':' + str(port)
        query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid', 'mc_cid', 'mc_eid', 'ocid'})
        return urlunsplit((parts.scheme.lower(), host, parts.path or '/', urlencode(query), ''))
    except ValueError:
        return ''


def _text(value):
    return ' '.join(unescape(re.sub(r'<[^>]*>', ' ', value or '')).split())


def _child_text(item, names):
    for name in names:
        for child in item:
            if child.tag.rsplit('}', 1)[-1] == name and child.text:
                return ''.join(child.itertext()).strip()
    return ''


def _published(value):
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (TypeError, ValueError, OverflowError):
            return None


def collect_news(markets, now, fetch_bytes=None):
    """Collect feed summaries; fetch_bytes(url) may inject a byte transport."""
    fetch = fetch_bytes or _fetch_bytes
    report = {'articles': [], 'matches': [], 'errors': [], 'feeds_ok': 0}
    seen = set()
    for source, url in FEEDS:
        try:
            payload = fetch(url)
            if len(payload) > 1_048_576:
                raise ValueError('feed exceeds 1 MiB limit')
            root = ET.fromstring(payload)
            if root.tag.rsplit('}',1)[-1] not in ('rss','feed','RDF'):
                raise ValueError('not an RSS/Atom feed')
        except Exception as exc:
            report['errors'].append({'source': source, 'url': url, 'error': str(exc)})
            continue
        report['feeds_ok'] += 1
        for item in root.iter():
            if item.tag.rsplit('}', 1)[-1] not in ('item', 'entry'):
                continue
            published = _published(_child_text(item, ('pubDate', 'published', 'date', 'updated')))
            if published is None or published.tzinfo is None or not now - timedelta(hours=72) <= published <= now + timedelta(minutes=5):
                continue
            raw_link = _child_text(item, ('link',))
            for child in item:
                if child.tag.rsplit('}', 1)[-1] == 'link' and child.get('rel', 'alternate') == 'alternate':
                    raw_link = child.get('href', raw_link)
                    break
            link = _canonical_url(raw_link)
            if not link or link in seen:
                continue
            seen.add(link)
            report['articles'].append({
                'id': hashlib.sha256(link.encode()).hexdigest(),
                'title': _text(_child_text(item, ('title',))), 'url': link,
                'published_at': _iso(published),
                'observed_at': _iso(now), 'source': source,
                'summary': _text(_child_text(item, ('description', 'summary'))),
            })
    # Relevance only: never turn a headline into a probability adjustment.
    aliases = {
        'fed': ('federal reserve', 'fomc', 'fed', 'interest rates'),
        'cpi': ('consumer price index', 'inflation', 'cpi'),
        'bitcoin': ('bitcoin', 'btc'),
        'sp500': ('s&p 500', 's&p500', 'sp500'),
    }
    stop = set('will would could should when what which how does did the and this that with from than above below over under yes no price prices company temperature maximum minimum high low win game points forecast recorded market according resolves november december january february march april may june july august september october'.split())
    def has(text, term):
        return re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text) is not None
    for market in markets:
        text = ' '.join(str(market.get(k,'')) for k in ('title','subtitle','rules_primary','event_title')).lower()
        ticker = market.get('ticker','')
        groups = [terms for name, terms in aliases.items()
                  if any(has(text, term) for term in terms)
                  or (name == 'fed' and ticker.startswith('KXFED'))
                  or (name == 'cpi' and ticker.startswith('KXCPI'))
                  or (name == 'bitcoin' and ticker.startswith('KXBTC'))
                  or (name == 'sp500' and ticker.startswith('KXINX'))]
        weather = ticker.startswith('KXHIGHNY')
        words = set(re.findall(r'[a-z]{4,}', market.get('title','').lower())) - stop
        matches = []
        for article in report['articles']:
            headline = (article['title']+' '+article['summary']).lower()
            terms = sorted(set(term for group in groups for term in group if has(headline,term)))
            if weather:
                city = [term for term in ('new york','nyc','central park') if has(headline,term)]
                context = [term for term in ('weather','temperature','forecast','heat','cold','storm') if has(headline,term)]
                terms = city+context if city and context else []
            elif not terms:
                terms = sorted(word for word in words if has(headline,word))
                if len(terms) < 2:
                    terms = []
            if terms:
                matches.append({'ticker': ticker, 'article_id': article['id'],
                                'score': len(terms), 'matched_terms': terms,
                                'kind': 'keyword_relevance_only'})
        report['matches'].extend(sorted(matches, key=lambda m: (-m['score'],m['article_id']))[:5])
    return report
