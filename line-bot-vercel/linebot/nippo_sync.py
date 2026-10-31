"""日報アプリ（nippo.html）の端末間同期。

iPadで書き始めた日報を、スマホでも続けて編集・送信できるようにするため、
日報の中身を bot_state に置いて端末どうしで共有する。

- 日報1日分 = キー `nippo:r:YYYY-MM-DD`、値は日報そのもの（`_u` に最終更新時刻ms）
  削除した日は `{"date":..., "_del": true, "_u": ...}` を残して他の端末にも消えたことを伝える
- 常連リスト・名前一覧・よく使う言葉・表示設定 = キー `nippo:shared`

競合は「`_u` が新しい方が勝つ」。同じ日報を2台で同時に触った場合は、後から保存した方になる。
"""
import json
from urllib.parse import quote

import requests

from . import config
from .supabase_client import _headers, get_supabase

PREFIX = 'nippo:r:'
SHARED_KEY = 'nippo:shared'


def _u(v):
    try:
        return int((v or {}).get('_u') or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _upsert(rows):
    if not rows:
        return True
    url = f'{config.SUPABASE_URL}/rest/v1/bot_state?on_conflict=key'
    res = requests.post(
        url,
        headers={**_headers('resolution=merge-duplicates,return=minimal'), 'Content-Type': 'application/json'},
        data=json.dumps(rows),
        timeout=20,
    )
    if res.status_code >= 300:
        print('nippo_sync upsert error', res.status_code, res.text)
        return False
    return True


def _existing(keys):
    if not keys:
        return {}
    inlist = ','.join('"' + k.replace('"', '') + '"' for k in keys)
    rows = get_supabase('bot_state', f'key=in.({quote(inlist)})&select=key,value')
    return {r['key']: r.get('value') for r in rows}


def push(reports, shared):
    """端末の変更を保存する。サーバー側の方が新しいものは保存せず、そのまま返して端末に取り込ませる。"""
    reports = reports or {}
    keys = [PREFIX + d for d in reports if isinstance(d, str) and len(d) == 10]
    if shared:
        keys.append(SHARED_KEY)
    cur = _existing(keys)
    rows, newer = [], {}
    for d, rep in reports.items():
        if not isinstance(rep, dict) or not isinstance(d, str) or len(d) != 10:
            continue
        old = cur.get(PREFIX + d)
        if old and _u(old) > _u(rep):
            newer[d] = old
            continue
        rows.append({'key': PREFIX + d, 'value': rep})
    newer_shared = None
    if isinstance(shared, dict):
        old = cur.get(SHARED_KEY)
        if old and _u(old) > _u(shared):
            newer_shared = old
        else:
            rows.append({'key': SHARED_KEY, 'value': shared})
    ok = _upsert(rows)
    if not ok:
        return {'ok': False, 'error': '保存できませんでした'}
    return {'ok': True, 'saved': len(rows), 'reports': newer, 'shared': newer_shared}


def pull(since=0):
    """`since`（ms）より後に更新された日報と、共有データを返す。since=0なら全部。"""
    try:
        since = int(since or 0)
    except (TypeError, ValueError):
        since = 0
    params = f'key=like.{quote(PREFIX)}*&select=key,value&order=key.asc'
    if since:
        params += f'&value->>_u=gt.{since}'
    rows = get_supabase('bot_state', params)
    reports = {r['key'][len(PREFIX):]: r.get('value') for r in rows if r.get('value')}
    shared_rows = get_supabase('bot_state', f'key=eq.{quote(SHARED_KEY)}&select=value')
    shared = shared_rows[0].get('value') if shared_rows else None
    return {'ok': True, 'reports': reports, 'shared': shared}
