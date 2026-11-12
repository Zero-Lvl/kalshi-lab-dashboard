import hashlib
import importlib
import unittest
from datetime import datetime, timezone

NOW = datetime(2026, 11, 11, 18, tzinfo=timezone.utc)


def news_module():
    try:
        return importlib.import_module('news')
    except ModuleNotFoundError:
        return None


def rss(items):
    return ('<rss><channel>' + ''.join(
        '<item><title>' + title + '</title><link>' + url + '</link>'
        + ('<pubDate>' + date + '</pubDate>' if date else '')
        + '<description>Publisher summary only</description></item>'
        for title, url, date in items) + '</channel></rss>').encode()


class NewsTests(unittest.TestCase):
    def test_topical_matches_not_generic_overlap_or_direction_claims(self):
        payload = rss([
            ('Federal Reserve divided over interest rates', 'https://example.org/fed', 'Wed, 11 Nov 2026 17:00:00 GMT'),
            ('New York temperature forecast warmer weather', 'https://example.org/weather', 'Wed, 11 Nov 2026 17:00:00 GMT'),
            ('New York baseball club wins a game', 'https://example.org/sport', 'Wed, 11 Nov 2026 17:00:00 GMT'),
            ('Will the company raise its prices?', 'https://example.org/other', 'Wed, 11 Nov 2026 17:00:00 GMT'),
        ])
        markets = [
            {'ticker': 'KXFEDDECISION-X', 'title': 'Will the Fed raise interest rates?'},
            {'ticker': 'KXHIGHNY-X', 'title': 'Will the maximum temperature be >66°?', 'rules_primary': 'New York City (CLINYC)'},
            {'ticker': 'OTHER-X', 'title': 'Will a company win?'},
        ]
        report = news_module().collect_news(markets, NOW, lambda url: payload)
        titles = {a['id']: a['title'] for a in report['articles']}
        pairs = {(m['ticker'], titles[m['article_id']]) for m in report['matches']}
        self.assertIn(('KXFEDDECISION-X', 'Federal Reserve divided over interest rates'), pairs)
        self.assertIn(('KXHIGHNY-X', 'New York temperature forecast warmer weather'), pairs)
        self.assertNotIn(('KXHIGHNY-X', 'New York baseball club wins a game'), pairs)
        self.assertFalse(any(t == 'OTHER-X' for t, _ in pairs))
        self.assertTrue(all(m['kind'] == 'keyword_relevance_only' and m['matched_terms'] for m in report['matches']))

    def test_bad_feed_is_visible_without_discarding_other_feeds(self):
        n = news_module()
        good = rss([('Real headline', 'https://example.org/good', 'Wed, 11 Nov 2026 17:00:00 GMT')])
        def fetch(url):
            return b'<broken' if url == n.FEEDS[0][1] else good
        result = n.collect_news([], NOW, fetch)
        self.assertEqual(result['feeds_ok'], len(n.FEEDS)-1)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(len(result['articles']), 1)
        large = n.collect_news([], NOW, lambda url: b'x'*1_048_577)
        self.assertEqual(len(large['errors']), len(n.FEEDS))

    def test_rss_bom_returns_attributed_summary_with_stable_id(self):
        news = news_module()
        self.assertIsNotNone(news, 'news module must exist')
        payload = b'\xef\xbb\xbf' + rss([('Fed update', 'https://example.org/story', 'Wed, 11 Nov 2026 17:00:00 GMT')])
        report = news.collect_news([], NOW, lambda url: payload)
        article = report['articles'][0]
        self.assertEqual(article['id'], hashlib.sha256(b'https://example.org/story').hexdigest())
        self.assertEqual(article['title'], 'Fed update')
        self.assertEqual(article['summary'], 'Publisher summary only')
        self.assertEqual(article['published_at'], '2026-11-11T17:00:00Z')
        self.assertEqual(article['observed_at'], '2026-11-11T18:00:00Z')
        self.assertTrue(article['source'])
        self.assertEqual(report['feeds_ok'], len(news.FEEDS))
        self.assertEqual(report['errors'], [])

    def test_canonical_url_dedup_removes_tracking_and_rejects_unsafe_links(self):
        payload = rss([
            ('First', 'HTTPS://Example.org/story?utm_source=rss&amp;b=2&amp;a=1#top', 'Wed, 11 Nov 2026 17:00:00 GMT'),
            ('Duplicate', 'https://example.org/story?a=1&amp;b=2&amp;fbclid=track', 'Wed, 11 Nov 2026 17:00:00 GMT'),
            ('Unsafe', 'javascript:alert(1)', 'Wed, 11 Nov 2026 17:00:00 GMT'),
            ('Relative', '/relative', 'Wed, 11 Nov 2026 17:00:00 GMT'),
        ])
        articles = news_module().collect_news([], NOW, lambda url: payload)['articles']
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['url'], 'https://example.org/story?a=1&b=2')
        self.assertEqual(articles[0]['id'], hashlib.sha256(articles[0]['url'].encode()).hexdigest())

    def test_atom_namespace_iso_encoding_and_html_summary(self):
        payload = '''<?xml version="1.0" encoding="ISO-8859-1"?>
        <feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Café CPI</title>
        <link rel="self" href="https://example.org/feed"/>
        <link rel="alternate" href="https://example.org/cpi"/>
        <published>2026-11-11T12:00:00-05:00</published>
        <summary type="html">&lt;p&gt;Inflation &amp;amp; prices&lt;/p&gt;</summary>
        <content>FULL ARTICLE MUST NOT BE STORED</content></entry></feed>'''.encode('iso-8859-1')
        result = news_module().collect_news([], NOW, lambda url: payload)
        self.assertEqual(len(result['articles']), 1)
        article = result['articles'][0]
        self.assertEqual(article['title'], 'Café CPI')
        self.assertEqual(article['url'], 'https://example.org/cpi')
        self.assertEqual(article['summary'], 'Inflation & prices')
        self.assertEqual(article['published_at'], '2026-11-11T17:00:00Z')
        self.assertNotIn('FULL ARTICLE', str(result))

    def test_only_dated_last_72_hours_and_at_most_five_minutes_future(self):
        payload = rss([
            ('Fresh', 'https://example.org/fresh', 'Wed, 11 Nov 2026 18:05:00 GMT'),
            ('Boundary', 'https://example.org/boundary', 'Sun, 08 Nov 2026 18:00:00 GMT'),
            ('Stale', 'https://example.org/stale', 'Sun, 08 Nov 2026 17:59:59 GMT'),
            ('Future', 'https://example.org/future', 'Wed, 11 Nov 2026 18:05:01 GMT'),
            ('Missing', 'https://example.org/missing', None),
            ('Invalid', 'https://example.org/invalid', 'not a date'),
        ])
        result = news_module().collect_news([], NOW, lambda url: payload)
        self.assertEqual({a['title'] for a in result['articles']}, {'Fresh', 'Boundary'})


if __name__ == '__main__':
    unittest.main()
