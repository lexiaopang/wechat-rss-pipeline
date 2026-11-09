"""Dependency-free RSS/Atom merger. Persistent state must be committed between runs."""
import argparse
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UTC = dt.timezone.utc
CONTENT = 'http://purl.org/rss/1.0/modules/content/'
ET.register_namespace('content', CONTENT)


def timestamp(value):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).timestamp()


def canonical(url):
    p = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    # WeChat's identity is stable across share/tracking variants.
    if p.hostname == 'mp.weixin.qq.com' and dict(pairs).get('__biz') and dict(pairs).get('mid'):
        query = {k: v for k, v in pairs if k in ('__biz', 'mid', 'idx')}
        query.setdefault('idx', '1')
        return urllib.parse.urlunsplit(('https', 'mp.weixin.qq.com', '/s', urllib.parse.urlencode(sorted(query.items())), ''))
    pairs = [(k, v) for k, v in pairs if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urllib.parse.urlencode(sorted(pairs)), ''))


def parse_feed(data, source, now):
    root = ET.fromstring(data)
    output = []
    for item in root.iter():
        if item.tag.split('}')[-1] not in ('item', 'entry'):
            continue
        fields = {}
        link = ''
        for child in item:
            key = child.tag.split('}')[-1]
            fields.setdefault(key, ''.join(child.itertext()).strip())
            if key == 'link' and child.attrib.get('rel', 'alternate') == 'alternate':
                link = child.attrib.get('href') or fields[key]
        link = urllib.parse.urljoin(source['url'], link) if link else ''
        identity = canonical(link) if link else source['id'] + ':' + (fields.get('guid') or fields.get('id') or '')
        if not link and identity == source['id'] + ':':
            continue
        guid = 'urn:sha256:' + hashlib.sha256(identity.encode()).hexdigest()
        published = timestamp(fields.get('pubDate') or fields.get('published') or fields.get('date') or fields.get('updated'))
        output.append(dict(guid=guid, title=fields.get('title', 'Untitled'), link=link,
                           source=source['name'], source_url=source['url'],
                           published=published if published is not None else now,
                           content=fields.get('encoded') or fields.get('content') or fields.get('description') or fields.get('summary', ''),
                           first_seen=now))
    return output


def fetch_url(source):
    request = urllib.request.Request(source['url'], headers={'User-Agent': 'WechatRSSPipeline/1.0', 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('Feed exceeds 20 MiB')
    return data


def fetch(source):
    errors = []
    for attempt in range(2):
        try:
            data = fetch_url(source)
            if not parse_feed(data, source, dt.datetime.now(UTC).timestamp()):
                raise ValueError('No usable articles')
            return data
        except Exception as exc:
            errors.append(str(exc))
    if source.get('fallback_url'):
        backup = dict(source, url=source['fallback_url'])
        try:
            data = fetch_url(backup)
            if not parse_feed(data, backup, dt.datetime.now(UTC).timestamp()):
                raise ValueError('No usable backup articles')
            return (data, backup, '; '.join(errors))
        except Exception as exc:
            errors.append('fallback: ' + str(exc))
    raise RuntimeError('; '.join(errors))


def merge(state, sources, loader=fetch, now=None, retention_days=14):
    now = now if now is not None else dt.datetime.now(UTC).timestamp()
    records = dict(state.get('articles', {}))
    report = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        jobs = {executor.submit(loader, s): s for s in sources if s.get('enabled', True)}
        for future in concurrent.futures.as_completed(jobs):
            source = jobs[future]
            try:
                result = future.result()
                used = source
                primary_error = None
                if isinstance(result, tuple):
                    result, used, primary_error = result
                entries = parse_feed(result, used, now)
                if not entries:
                    raise ValueError('No usable articles in source')
                for entry in entries:
                    previous = records.get(entry['guid'])
                    if previous:
                        entry['first_seen'] = previous['first_seen']
                        entry['link'] = previous['link']
                        entry['published'] = previous['published']
                        entry['content'] = entry['content'] or previous['content']
                    records[entry['guid']] = entry
                report.append({'source': source['id'], 'name': source['name'], 'ok': True, 'articles': len(entries), 'fallback': used['url'] != source['url'], 'primary_error': primary_error})
            except Exception as exc:
                report.append({'source': source['id'], 'name': source['name'], 'ok': False, 'error': str(exc)})
    # Keep newly discovered older articles too: a delayed run must not discard its backlog.
    cutoff = now - retention_days * 86400
    records = {k: v for k, v in records.items() if max(v['published'], v['first_seen']) >= cutoff}
    return {'version': 1, 'articles': records}, {'sources': sorted(report, key=lambda r: r['source']), 'articles': len(records), 'generated_at': now}


def render(state):
    root = ET.Element('rss', version='2.0')
    channel = ET.SubElement(root, 'channel')
    for key, value in [('title', '公众号文章聚合'), ('link', 'https://mp.weixin.qq.com/'), ('description', '公众号与备用网站 RSS 聚合；保留至少14天；正文取自源 RSS')]:
        ET.SubElement(channel, key).text = value
    # Oldest first for consumers that cap a batch. No truncation of peak-day backlog.
    for article in sorted(state['articles'].values(), key=lambda a: (a['published'], a['guid'])):
        item = ET.SubElement(channel, 'item')
        for key in ('title', 'link'):
            ET.SubElement(item, key).text = article[key]
        ET.SubElement(item, 'guid', isPermaLink='false').text = article['guid']
        ET.SubElement(item, 'pubDate').text = email.utils.format_datetime(dt.datetime.fromtimestamp(article['published'], UTC), usegmt=True)
        ET.SubElement(item, 'source', url=article['source_url']).text = article['source']
        ET.SubElement(item, 'description').text = article['content']
        ET.SubElement(item, '{' + CONTENT + '}encoded').text = article['content']
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temporary = handle.name
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sources', default='sources.json')
    parser.add_argument('--state', default='state.json')
    parser.add_argument('--output', default='public/feed.xml')
    args = parser.parse_args()
    sources = json.loads(Path(args.sources).read_text())
    if not sources:
        raise SystemExit('No RSS sources configured')
    old = json.loads(Path(args.state).read_text()) if Path(args.state).exists() else {}
    state, report = merge(old, sources)
    atomic('public/status.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
    if not any(r['ok'] for r in report['sources']):
        raise SystemExit('All sources failed; previous state and feed preserved')
    atomic(args.state, json.dumps(state, ensure_ascii=False, sort_keys=True).encode())
    atomic(args.output, render(state))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
