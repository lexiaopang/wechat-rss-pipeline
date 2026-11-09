import unittest
import xml.etree.ElementTree as ET
from aggregate import merge, render

NOW = 1800000000
S = {'id': 'test', 'name': '测试公众号', 'url': 'https://example.com/feed'}

def feed(count=1, suffix=''):
    return ('<rss><channel>' + ''.join('<item><title>Article %d</title><link>https://example.com/%d%s</link><description>正文</description></item>' % (i, i, suffix) for i in range(count)) + '</channel></rss>').encode()

class AggregationTests(unittest.TestCase):
    def test_fetch_parse_content(self):
        state, report = merge({}, [S], lambda s: feed(), NOW)
        root = ET.fromstring(render(state))
        self.assertTrue(report['sources'][0]['ok'])
        self.assertEqual(root.findtext('.//description[1]'), '公众号与备用网站 RSS 聚合；保留至少14天；正文取自源 RSS')
        self.assertEqual(root.findtext('.//item/description'), '正文')
        self.assertEqual(root.findtext('.//item/source'), '测试公众号')

    def test_stable_guid_and_cross_run_dedupe(self):
        state, _ = merge({}, [S], lambda s: feed(suffix='?utm_source=one'), NOW)
        again, _ = merge(state, [S], lambda s: feed(suffix='?utm_source=two'), NOW + 86400)
        self.assertEqual(len(again['articles']), 1)
        self.assertEqual(state, again)

    def test_source_failure_preserves_articles(self):
        state, _ = merge({}, [S], lambda s: feed(), NOW)
        def broken(s):
            raise TimeoutError('simulated source timeout')
        again, report = merge(state, [S], broken, NOW + 86400)
        self.assertEqual(state, again)
        self.assertFalse(report['sources'][0]['ok'])

    def test_missed_run_recovery_and_retention(self):
        state, _ = merge({}, [S], lambda s: feed(), NOW)
        again, _ = merge(state, [S], lambda s: feed(5), NOW + 8 * 86400)
        self.assertEqual(len(again['articles']), 5)
        self.assertIn(next(iter(state['articles'])), again['articles'])

    def test_peak_10000_without_truncation(self):
        state, _ = merge({}, [S], lambda s: feed(10000), NOW)
        self.assertEqual(len(ET.fromstring(render(state)).findall('.//item')), 10000)
        again, _ = merge(state, [S], lambda s: feed(10000), NOW + 3600)
        self.assertEqual(len(again['articles']), 10000)

    def test_wechat_tracking_dedup(self):
        from aggregate import canonical
        self.assertEqual(canonical('https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=foo&scene=2'), canonical('https://mp.weixin.qq.com/s?mid=123&__biz=abc&idx=1&sn=bar'))

    def test_expired_article_not_reintroduced(self):
        state, _ = merge({}, [S], lambda s: feed(), NOW)
        expired, _ = merge(state, [], now=NOW + 15 * 86400)
        self.assertEqual(len(expired['articles']), 0)
        again, _ = merge(expired, [S], lambda s: feed(), NOW + 16 * 86400)
        self.assertEqual(len(again['articles']), 0)

    def test_fallback_only_after_primary_failure(self):
        from unittest.mock import patch
        from aggregate import fetch
        source = dict(S, fallback_url='https://backup.example/feed')
        with patch('aggregate.fetch_url', return_value=feed()) as loader:
            fetch(source)
            self.assertEqual(loader.call_count, 1)
        with patch('aggregate.fetch_url', side_effect=[TimeoutError('primary'), TimeoutError('primary'), feed()]) as loader:
            data, used, error = fetch(source)
            self.assertEqual(used['url'], source['fallback_url'])
            self.assertIn('primary', error)
            self.assertEqual(loader.call_count, 3)

    def test_malformed_source_isolated(self):
        other = dict(S, id='bad')
        state, report = merge({}, [S, other], lambda s: b'broken' if s['id']=='bad' else feed(), NOW)
        self.assertEqual(len(state['articles']), 1)
        self.assertEqual(sum(r['ok'] for r in report['sources']), 1)

if __name__ == '__main__':
    unittest.main()
