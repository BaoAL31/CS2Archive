"""Overlay vs raw long-form POV performance report (YouTube Data API v3, read-only).

Classification:
  * FACEIT POVs -> joined to .pipeline/{run}.json (youtube_dir *_overlay suffix).
  * HLTV POVs   -> title marker ("W/ Inputs + Util Overlay", "Utility Cams", ...)
                   or description marker ("Utility trajectory clips ...").
  * Videos with neither marker and no pipeline join are treated as raw.

Metrics: raw views/likes, views-per-day since publish, and a
release-window-normalised index (views / median views of every channel
long-form upload published within +-7 days) that cancels channel growth.

Usage:
  python scripts/misc/yt_overlay_perf.py [--refresh] [--json out.json]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import io
import json
import os
import re
import statistics
import sys
from difflib import SequenceMatcher

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKEN = os.path.join(ROOT, 'token_youtube.json')
ANALYTICS_TOKEN = os.path.join(ROOT, 'token_youtube_analytics.json')
CACHE = os.path.join(ROOT, '.tmp', 'yt_uploads.json')
ANALYTICS_CACHE = os.path.join(ROOT, '.tmp', 'yt_analytics.json')
STUDIO_DEFAULT = os.path.join(ROOT, 'exports', 'studio',
                              'content-2025-09-24_2026-09-24', 'Table data.csv')
ANALYTICS_METRICS = ('views,estimatedMinutesWatched,averageViewDuration,'
                     'averageViewPercentage,subscribersGained')

TOV = re.compile(r'W/.*(Util|Input)|Utility Cams|Input \+ Utility|Util Cams|Util Overlay', re.I)
DOV = re.compile(r'Utility trajectory clips|utility trajectory', re.I)
TKB = re.compile(r'W/ ?(Inputs|Input \+)', re.I)
DKB = re.compile(r'keyboard & mouse input overlay', re.I)
FACEIT = re.compile(r'faceit\.com/en/cs2/room/([0-9a-z-]{30,45})')
POV = re.compile(r'(\d+\.\d+ (Rating|ELO))|FACEIT CS2 POV|\|\s*\d+\.\d+|\d+ ELO vs', re.I)


def secs(iso: str) -> int:
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso)
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def service():
    c = Credentials.from_authorized_user_file(TOKEN)
    if c.expired and c.refresh_token:
        c.refresh(Request())
        open(TOKEN, 'w').write(c.to_json())
    return build('youtube', 'v3', credentials=c)


def analytics_service():
    c = Credentials.from_authorized_user_file(ANALYTICS_TOKEN)
    if c.expired and c.refresh_token:
        c.refresh(Request())
        open(ANALYTICS_TOKEN, 'w').write(c.to_json())
    return build('youtubeAnalytics', 'v2', credentials=c)


def fetch_analytics(ids: list[str], start='2026-05-01', end=None) -> dict:
    """Per-video analytics for lifetime of the channel. Returns {video_id: row}."""
    import datetime as _dt
    end = end or _dt.date.today().isoformat()
    yt = analytics_service()
    out = {}
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 180):
        chunk = ids[i:i + 180]
        r = yt.reports().query(
            ids='channel==MINE', startDate=start, endDate=end,
            metrics=ANALYTICS_METRICS, dimensions='video',
            filters='video==' + ','.join(chunk), maxResults=200).execute()
        cols = [h['name'] for h in r.get('columnHeaders', [])]
        for row in r.get('rows', []):
            d = dict(zip(cols, row))
            out[d['video']] = d
    return out


def probe_impressions() -> None:
    """Check whether impression/CTR metrics exist for this channel."""
    import datetime as _dt
    yt = analytics_service()
    for metrics in ('impressions,impressionClickThroughRate',
                    'cardImpressions,cardClickRate'):
        try:
            yt.reports().query(
                ids='channel==MINE', startDate='2026-08-01',
                endDate=_dt.date.today().isoformat(), metrics=metrics,
                dimensions='video', maxResults=10).execute()
            print(f'  analytics metric OK: {metrics}')
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            m = re.search(r'"message":\s*"([^"]+)', msg)
            print(f'  analytics metric FAILED: {metrics} -> {m.group(1) if m else msg[:120]}')


def fetch_retention(ids: list[str]) -> dict:
    """Retention curve per video: {video_id: [(ratio, watch_ratio, rel_perf), ...]}"""
    import datetime as _dt
    yt = analytics_service()
    out = {}
    for n, vid in enumerate(ids, 1):
        try:
            r = yt.reports().query(
                ids='channel==MINE', startDate='2026-05-01',
                endDate=_dt.date.today().isoformat(),
                metrics='audienceWatchRatio,relativeRetentionPerformance',
                dimensions='elapsedVideoTimeRatio',
                filters=f'video=={vid}', maxResults=200).execute()
        except Exception:  # noqa: BLE001
            continue
        pts = [(row[0], row[1], row[2]) for row in r.get('rows', [])]
        if pts:
            out[vid] = pts
        if n % 20 == 0:
            print(f'   retention {n}/{len(ids)}')
    return out


STUDIO_COLS = {
    'Thumbnail impressions': 'impressions',
    'Thumbnail click-through rate (%)': 'ctr',
    'Stayed to watch (%)': 'stayed_pct',
    'Engaged views': 'engaged_views',
    'Average percentage viewed (%)': 'studio_avp',
    'Unique viewers': 'unique_viewers',
    'New viewers': 'new_viewers',
    'Returning viewers': 'returning_viewers',
    'Watch time (hours)': 'studio_watch_h',
    'Views': 'studio_views',
}


def load_studio(path: str) -> dict:
    """YouTube Studio 'Table data.csv' -> {video_id: {metric: float}}."""
    import csv as _csv
    out = {}
    rows = list(_csv.reader(open(path, encoding='utf-8-sig')))
    idx = {name: i for i, name in enumerate(rows[0])}
    for row in rows[1:]:
        vid = row[0].strip() if row else ''
        if not vid or vid == 'Total' or len(vid) < 6:
            continue
        rec = {'title': row[idx['Video title']]}
        for col, key in STUDIO_COLS.items():
            if col not in idx:
                continue
            raw = row[idx[col]].strip() if idx[col] < len(row) else ''
            raw = raw.replace('%', '').replace(',', '')
            try:
                rec[key] = float(raw) if raw != '' else None
            except ValueError:
                rec[key] = None
        out[vid] = rec
    return out


def fetch_uploads() -> list[dict]:
    """All channel uploads with snippet/statistics."""
    """All channel uploads with snippet/statistics."""
    y = service()
    pl = y.channels().list(part='contentDetails', mine=True).execute()[
        'items'][0]['contentDetails']['relatedPlaylists']['uploads']
    ids, tok = [], None
    while True:
        r = y.playlistItems().list(part='contentDetails', playlistId=pl,
                                   maxResults=50, pageToken=tok).execute()
        ids += [i['contentDetails']['videoId'] for i in r.get('items', [])]
        tok = r.get('nextPageToken')
        if not tok:
            break
    items = []
    for i in range(0, len(ids), 50):
        r = y.videos().list(part='snippet,statistics,contentDetails,status',
                            id=','.join(ids[i:i + 50])).execute()
        items += r.get('items', [])
    return [{
        'id': v['id'], 'title': v['snippet']['title'],
        'description': v['snippet'].get('description', ''),
        'publishedAt': v['snippet']['publishedAt'],
        'duration': v['contentDetails']['duration'],
        'privacy': v['status']['privacyStatus'],
        'views': int(v.get('statistics', {}).get('viewCount', 0)),
        'likes': int(v.get('statistics', {}).get('likeCount', 0) or 0),
        'comments': int(v.get('statistics', {}).get('commentCount', 0) or 0),
    } for v in items]


def pipeline_runs() -> dict:
    """run_id -> {overlay: bool, uid: faceit match id}"""
    out = {}
    for p in glob.glob(os.path.join(ROOT, '.pipeline', '*.json')):
        try:
            d = json.load(open(p, encoding='utf-8')).get('data', {})
        except Exception:
            continue
        yd = d.get('youtube_dir', '')
        if not yd:
            continue
        n = os.path.basename(yd)
        m = re.match(r'^(1-[0-9a-f-]{36})', n)
        out[n] = {'overlay': n.endswith('_overlay'), 'uid': m.group(1) if m else None}
    return out


def classify(videos: list[dict]) -> list[dict]:
    runs = pipeline_runs()
    by_uid = collections.defaultdict(list)
    for info in runs.values():
        if info['uid']:
            by_uid[info['uid']].append(info)
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for v in videos:
        if v['privacy'] != 'public' or secs(v['duration']) < 480:
            continue
        fm = FACEIT.search(v['description'])
        desc_mk = bool(DOV.search(v['description']))
        title_mk = bool(TOV.search(v['title']))
        product, variant, source = 'other', None, None
        if fm:
            product = 'FACEIT POV'
            infos = by_uid.get(fm.group(1))
            if infos:
                variant = 'overlay' if infos[0]['overlay'] else 'raw'
                source = 'pipeline'
        elif POV.search(v['title']):
            product = 'HLTV POV'
            variant = 'overlay' if (title_mk or desc_mk) else 'raw'
            source = 'title' if title_mk else ('description' if desc_mk else 'no marker')
        pub = dt.datetime.fromisoformat(v['publishedAt'].replace('Z', '+00:00'))
        rows.append({
            'id': v['id'], 'title': v['title'], 'pub': v['publishedAt'],
            'age': max((now - pub).total_seconds() / 86400, 0.5),
            'views': v['views'], 'likes': v['likes'], 'comments': v['comments'],
            'product': product, 'variant': variant, 'source': source,
            'keyboard': bool(TKB.search(v['title'])) or bool(DKB.search(v['description'])),
        })
    for r in rows:
        r['vpd'] = r['views'] / r['age']
    # release-window normalisation
    dts = {r['id']: dt.datetime.fromisoformat(r['pub'].replace('Z', '+00:00')) for r in rows}
    for r in rows:
        win = [o['views'] for o in rows
               if o['id'] != r['id'] and abs((dts[o['id']] - dts[r['id']]).days) <= 7]
        r['rel'] = r['views'] / statistics.median(win) if win else None
    return rows


def med(xs):
    return statistics.median(xs) if xs else 0.0


def show(label, group):
    if not group:
        print(f'  {label:22s} n=0')
        return
    line = (f'  {label:22s} n={len(group):3d}  median_views={med([r["views"] for r in group]):7.0f}'
            f'  median_views/day={med([r["vpd"] for r in group]):6.2f}'
            f'  median_rel={med([r["rel"] for r in group]):.2f}'
            f'  mean_rel={statistics.mean([r["rel"] for r in group]):.2f}'
            f'  median_likes={med([r["likes"] for r in group]):.0f}')
    print(line)
    a = [r for r in group if r.get('avp') is not None]
    if a:
        print(f'  {"":22s} n={len(a):3d}  median_avg_view_pct={med([r["avp"] for r in a]):5.1f}%'
              f'  median_avd={med([r["avd"] for r in a]):6.0f}s'
              f'  median_watch_min={med([r["watch_min"] for r in a]):7.1f}'
              f'  median_subs={med([r["subs"] for r in a]):.1f}')
        tot = sum(r['watch_min'] for r in a)
        print(f'  {"":22s} total_watch_hours={tot / 60:.1f}')


def mannwhitney(a, b):
    """two-sided normal-approximation p-value (scipy if present, else builtin)"""
    if not a or not b:
        return 1.0
    try:
        from scipy.stats import mannwhitneyu
        return float(mannwhitneyu(a, b, alternative='two-sided').pvalue)
    except Exception:
        pass
    import math
    comb = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    i, rank1 = 0, 0.0
    while i < len(comb):
        j = i
        while j + 1 < len(comb) and comb[j + 1][0] == comb[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            if comb[k][1] == 0:
                rank1 += avg
        i = j + 1
    n1, n2 = len(a), len(b)
    u = rank1 - n1 * (n1 + 1) / 2
    sd = (n1 * n2 * (n1 + n2 + 1) / 12) ** .5
    z = (u - n1 * n2 / 2) / sd if sd else 0
    return math.erfc(abs(z) / 2 ** .5)


def compare(label, groups, metric='rel'):
    """groups: dict name->list"""
    names = list(groups)
    if len(names) != 2:
        return
    a = [r[metric] for r in groups[names[0]] if r.get(metric) is not None]
    b = [r[metric] for r in groups[names[1]] if r.get(metric) is not None]
    p = mannwhitney(a, b)
    print(f'  {label}: {names[0]} vs {names[1]} on {metric} -> p={p:.4f}'
          f'  (medians {med(a):.2f} vs {med(b):.2f})')


def find_pairs(rows, threshold=0.78):
    """Same-match HLTV raw/overlay re-uploads."""
    h = [r for r in rows if r['product'] == 'HLTV POV' and r['variant']]
    def norm(t):
        t = re.sub(r'\s*\|?\s*W/.*$|\s*\|?\s*Inputs? \+.*$|\s*\|?\s*Utility Cams.*$', '', t, flags=re.I)
        t = re.sub(r'\b\d\.\d{2}\b|\bRating\b|\b\d+-\d+\b', '', t)
        return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', t.lower()).split())
    for r in h:
        r['_n'] = norm(r['title'])
    used, pairs = set(), []
    for i, a in enumerate(h):
        if i in used or a['variant'] != 'raw':
            continue
        best, bj = 0, None
        for j, b in enumerate(h):
            if j == i or j in used or b['variant'] != 'overlay':
                continue
            s = SequenceMatcher(None, a['_n'], b['_n']).ratio()
            if s > best:
                best, bj = s, j
        if bj is not None and best >= threshold:
            pairs.append((a, h[bj], best))
            used |= {i, bj}
    return pairs


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true', help='re-fetch from the API')
    ap.add_argument('--json', help='write the classified rows to this path')
    ap.add_argument('--analytics', action='store_true',
                    help='fetch + merge YouTube Analytics rows (watch time, retention)')
    ap.add_argument('--probe-analytics', action='store_true',
                    help='check which analytics metrics this channel exposes')
    ap.add_argument('--retention', action='store_true',
                    help='fetch + compare audience-retention curves by variant')
    ap.add_argument('--studio', nargs='?', const=STUDIO_DEFAULT, default=None,
                    help='merge a YouTube Studio "Table data.csv" export '
                         '(thumbnail impressions + CTR, stayed-to-watch)')
    args = ap.parse_args()

    if args.probe_analytics:
        probe_impressions()
        return 0

    if args.refresh or not os.path.exists(CACHE):
        vids = fetch_uploads()
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump(vids, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    else:
        vids = json.load(open(CACHE, encoding='utf-8'))
    rows = classify(vids)

    if args.analytics:
        if args.refresh or not os.path.exists(ANALYTICS_CACHE):
            an = fetch_analytics([r['id'] for r in rows])
            os.makedirs(os.path.dirname(ANALYTICS_CACHE), exist_ok=True)
            json.dump(an, open(ANALYTICS_CACHE, 'w', encoding='utf-8'))
        else:
            an = json.load(open(ANALYTICS_CACHE, encoding='utf-8'))
        hit = 0
        for r in rows:
            a = an.get(r['id'])
            if not a:
                continue
            hit += 1
            r['an_views'] = a.get('views', r['views'])
            r['watch_min'] = a.get('estimatedMinutesWatched')
            r['avd'] = a.get('averageViewDuration')
            r['avp'] = a.get('averageViewPercentage')
            r['subs'] = a.get('subscribersGained')
        print(f'analytics rows merged: {hit}/{len(rows)}')

    if args.retention:
        cache = os.path.join(ROOT, '.tmp', 'yt_retention.json')
        sel = [r for r in rows if r['product'] == 'HLTV POV' and r['variant'] and r['views'] >= 30]
        if args.refresh or not os.path.exists(cache):
            ret = fetch_retention([r['id'] for r in sel])
            json.dump(ret, open(cache, 'w', encoding='utf-8'))
        else:
            ret = json.load(open(cache, encoding='utf-8'))
        print(f'retention curves: {len(ret)}/{len(sel)} HLTV POV videos (>=30 views)')
        buckets = [i / 20 for i in range(21)]
        curves = {}
        for label in ('raw', 'overlay'):
            groups = {b: [] for b in buckets}
            for r in sel:
                if r['variant'] != label or r['id'] not in ret:
                    continue
                for ratio, wr, _rp in ret[r['id']]:
                    b = round(ratio * 20) / 20
                    if b in groups:
                        groups[b].append(wr)
            curves[label] = {b: (med(v) if v else None) for b, v in groups.items()}
        print(f'  n(raw)={sum(1 for r in sel if r["variant"] == "raw" and r["id"] in ret)}'
              f'  n(overlay)={sum(1 for r in sel if r["variant"] == "overlay" and r["id"] in ret)}')
        print('  point   raw%   overlay%')
        for b in buckets:
            a, c = curves['raw'][b], curves['overlay'][b]
            fa = f'{a * 100:6.1f}' if a is not None else '     -'
            fc = f'{c * 100:6.1f}' if c is not None else '     -'
            print(f'  {b:5.2f}  {fa}  {fc}')
        return 0

    print(f'classified long-form public uploads: {len(rows)}')

    if args.studio:
        st = load_studio(args.studio)
        hit = 0
        for r in rows:
            s = st.get(r['id'])
            if not s:
                continue
            hit += 1
            r.update({k: v for k, v in s.items() if k != 'title'})
        print(f'studio rows merged: {hit}/{len(rows)}  ({args.studio})')
    print('counts:', dict(collections.Counter(
        f'{r["product"]}/{r["variant"]}' for r in rows)))

    povs = [r for r in rows if r['variant'] and r['product'].endswith('POV')]
    print('\n== ALL POV long-form ==')
    show('raw', [r for r in povs if r['variant'] == 'raw'])
    show('overlay', [r for r in povs if r['variant'] == 'overlay'])

    for prod in ('HLTV POV', 'FACEIT POV'):
        g = [r for r in povs if r['product'] == prod]
        print(f'\n== {prod} ==')
        show('raw', [r for r in g if r['variant'] == 'raw'])
        show('overlay', [r for r in g if r['variant'] == 'overlay'])

    print('\n== HLTV overlay POVs by overlay type (release-normalised) ==')
    ov = [r for r in povs if r['product'] == 'HLTV POV' and r['variant'] == 'overlay' and r['rel']]
    show('util-cam only', [r for r in ov if not r['keyboard']])
    show('util + keyboard', [r for r in ov if r['keyboard']])

    print('\n== per-month medians (median views | median rel) ==')
    for m in sorted({r['pub'][:7] for r in povs}):
        g = [r for r in povs if r['pub'][:7] == m]
        raw = [r for r in g if r['variant'] == 'raw']
        ovl = [r for r in g if r['variant'] == 'overlay']
        f = lambda x: (f'{med([r["views"] for r in x]):6.0f} | {med([r["rel"] for r in x]):.2f}'
                       if x else '     - |    -')
        print(f'  {m}   raw n={len(raw):3d} {f(raw)}    overlay n={len(ovl):3d} {f(ovl)}')

    for prod in ('HLTV POV', 'FACEIT POV'):
        pairs = find_pairs([r for r in rows if r['product'] == prod])
        if not pairs:
            continue
        print(f'\n== same-match raw vs overlay re-uploads ({prod}) ==')
        for a, b, s in sorted(pairs, key=lambda x: abs(
                (dt.datetime.fromisoformat(x[1]['pub'].replace('Z', '+00:00'))
                 - dt.datetime.fromisoformat(x[0]['pub'].replace('Z', '+00:00'))).days)):
            gap = abs((dt.datetime.fromisoformat(b['pub'].replace('Z', '+00:00'))
                       - dt.datetime.fromisoformat(a['pub'].replace('Z', '+00:00'))).days)
            print(f'  gap {gap:3d}d  raw {a["views"]:6d}  overlay {b["views"]:6d}'
                  f'  ratio {b["views"] / a["views"] if a["views"] else 0:.2f}  {a["title"][:52]}')

    print('\n== top 12 POV by views ==')
    for r in sorted(povs, key=lambda x: -x['views'])[:12]:
        avp = f"  ret{r['avp']:5.1f}%" if r.get('avp') is not None else ''
        ctr = f"  ctr{r['ctr']:4.2f}%" if r.get('ctr') is not None else ''
        print(f'  {r["views"]:6d}  {r["variant"]:8s} {r["pub"][:10]}{avp}{ctr}  {r["title"][:48]}')

    if args.studio:
        raw = [r for r in povs if r['variant'] == 'raw']
        ovl = [r for r in povs if r['variant'] == 'overlay']
        print('\n== thumbnail / acquisition metrics (Studio export) ==')
        for m, lbl in (('ctr', 'thumbnail CTR %'), ('impressions', 'thumbnail impressions'),
                       ('stayed_pct', 'stayed to watch %'), ('engaged_views', 'engaged views'),
                       ('studio_watch_h', 'watch time (h)')):
            a = [r[m] for r in raw if r.get(m) is not None]
            b = [r[m] for r in ovl if r.get(m) is not None]
            if not a or not b:
                continue
            print(f'  {lbl:24s} raw med={med(a):9.2f} (n={len(a):3d})   '
                  f'overlay med={med(b):9.2f} (n={len(b):3d})   '
                  f'p={mannwhitney(a, b):.4f}')
        hraw = [r for r in raw if r['product'] == 'HLTV POV']
        hovl = [r for r in ovl if r['product'] == 'HLTV POV']
        print('  -- HLTV POV only --')
        for m, lbl in (('ctr', 'thumbnail CTR %'), ('stayed_pct', 'stayed to watch %')):
            a = [r[m] for r in hraw if r.get(m) is not None]
            b = [r[m] for r in hovl if r.get(m) is not None]
            if a and b:
                print(f'  {lbl:24s} raw med={med(a):9.2f} (n={len(a):3d})   '
                      f'overlay med={med(b):9.2f} (n={len(b):3d})   '
                      f'p={mannwhitney(a, b):.4f}')
        top = sorted([r for r in povs if r.get('ctr')],
                     key=lambda x: -x['ctr'])[:10]
        print('  -- best CTR overall --')
        for r in top:
            print(f'   ctr {r["ctr"]:5.2f}%  imp {r["impressions"]:7.0f}  {r["views"]:5d}v  '
                  f'{r["variant"]:8s} {r["title"][:46]}')

    if args.analytics:
        raw = [r for r in povs if r['variant'] == 'raw']
        ovl = [r for r in povs if r['variant'] == 'overlay']
        print('\n== retention / watch-time tests (Analytics API) ==')
        for m in ('avp', 'avd', 'watch_min'):
            compare('ALL POV', {'raw': raw, 'overlay': ovl}, m)
        hraw = [r for r in raw if r['product'] == 'HLTV POV']
        hovl = [r for r in ovl if r['product'] == 'HLTV POV']
        for m in ('avp', 'avd', 'watch_min'):
            compare('HLTV POV', {'raw': hraw, 'overlay': hovl}, m)
        kov = [r for r in hovl if r['keyboard']]
        uov = [r for r in hovl if not r['keyboard']]
        for m in ('avp', 'watch_min'):
            compare('overlay util-only vs +keyboard', {'util-only': uov, 'keyboard': kov}, m)

        print('\n== same-match pairs: retention detail ==')
        for a, b, s in sorted(find_pairs(rows), key=lambda x: -x[0]['views']):
            fa = lambda r: (f"ret {r['avp']:4.1f}%  avd {r['avd']:4.0f}s  watch {r['watch_min']:6.1f}m"
                            if r.get('avp') is not None else 'no analytics')
            print(f'  {a["title"][:44]}\n     raw  {a["views"]:6d}v  {fa(a)}\n'
                  f'     ovl  {b["views"]:6d}v  {fa(b)}')

    if args.json:
        json.dump(rows, open(args.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('\nwrote', args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
