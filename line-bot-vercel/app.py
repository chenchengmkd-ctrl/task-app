"""単一のWSGIアプリとして全エンドポイントをまとめたもの。
Vercelは requirements.txt しか無いPythonプロジェクトで /api/*.py が複数「handler」を
定義していると自動検出に失敗することがあるため、確実に動く単一エントリポイント方式
（ルート直下の app.py・トップレベル変数 app）に統一している。
URLパス自体は従来通り /api/webhook・/api/cron_* のまま（vercel.jsonのcrons設定もこのまま）。
"""
import json
from urllib.parse import parse_qs

import requests

from linebot import config
from linebot.router import handle_event
from linebot.tasks import check_timed_reminders
from linebot.recurring import check_recurring_reminders
from linebot.planning import check_daily_plan
from linebot.agent import send_agent_checkin, send_weekly_report
from linebot.heartbeat import push_heartbeat_commit
from linebot.richmenu import setup_rich_menu
from linebot.gemini_client import call_gemini
from linebot.reports import build_daily_report, build_weekly_report
from linebot.finance import (
    send_finance_report, send_finance_reminder,
    build_daily_finance_report, build_month_finance_report, build_finance_reminder,
)
from linebot.line_client import push_text, get_users
# 起動時に読み込んでおく（構文エラーがあれば /api/health が落ちてデプロイ事故に気づける）
from linebot import receipt as receipt_mod
from linebot import weekly_cf as weekly_cf_mod

# デプロイが反映されたかを /api/health で確認するための版数。コードを直すたびに上げる。
APP_VERSION = 61


def _respond(start_response, status, body, cors=False):
    payload = json.dumps(body).encode('utf-8')
    headers = [('Content-Type', 'application/json')]
    if cors:
        headers += [
            ('Access-Control-Allow-Origin', '*'),
            ('Access-Control-Allow-Headers', 'Content-Type'),
            ('Access-Control-Allow-Methods', 'POST, OPTIONS'),
        ]
    start_response(status, headers)
    return [payload]


def _has_cron_secret(environ):
    auth = environ.get('HTTP_AUTHORIZATION', '')
    return bool(config.CRON_SECRET) and auth == f'Bearer {config.CRON_SECRET}'


def _has_poll_secret(environ):
    qs = parse_qs(environ.get('QUERY_STRING', ''))
    key = (qs.get('key') or [None])[0]
    return bool(config.POLL_SECRET) and key == config.POLL_SECRET


def _handle_webhook(environ, start_response):
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    raw_body = environ['wsgi.input'].read(length) if length else b'{}'
    try:
        body = json.loads(raw_body.decode('utf-8'))
        for ev in body.get('events', []):
            try:
                handle_event(ev)
            except Exception as e:  # 1件の処理失敗で他のイベントを止めない
                print('handle_event error:', e)
    except Exception as e:
        print('webhook error:', e)
    return _respond(start_response, '200 OK', {'ok': True})


def _run_cron(environ, start_response, fn, name):
    if not _has_cron_secret(environ):
        return _respond(start_response, '401 Unauthorized', {'error': 'unauthorized'})
    try:
        fn(push_text, get_users)
    except Exception as e:
        print(f'{name} error:', e)
    return _respond(start_response, '200 OK', {'ok': True})


def _run_weekly_report(environ, start_response):
    if not _has_cron_secret(environ):
        return _respond(start_response, '401 Unauthorized', {'error': 'unauthorized'})
    try:
        send_weekly_report(push_text, get_users)
    except Exception as e:
        print('send_weekly_report error:', e)
    try:
        push_heartbeat_commit()
    except Exception as e:
        print('heartbeat error:', e)
    return _respond(start_response, '200 OK', {'ok': True})


def _run_poll(environ, start_response):
    if not _has_poll_secret(environ):
        return _respond(start_response, '401 Unauthorized', {'error': 'unauthorized'})
    for fn, name in (
        # 定期タスクの通知は停止中。呼び続けているのは、次回予定日だけ静かに進めるため
        # （recurring.PUSH_REMINDERS を True に戻せば通知が復活する）。
        (check_recurring_reminders, 'check_recurring_reminders'),
        (check_timed_reminders, 'check_timed_reminders'),
        (check_daily_plan, 'check_daily_plan'),
    ):
        try:
            fn(push_text, get_users)
        except Exception as e:
            print(f'{name} error:', e)
    return _respond(start_response, '200 OK', {'ok': True})


def _handle_health(environ, start_response):
    """設定診断用。秘密の値そのものは返さず、「設定されているか」「余計な文字が混ざっていないか」だけを返す。
    実際にLINEのAPIを叩いて、チャネルアクセストークンが有効かどうかも確認する。
    """
    token = config.CHANNEL_ACCESS_TOKEN
    gkey = config.GEMINI_API_KEY

    def describe(v):
        return {
            'set': bool(v),
            'length': len(v),
            'has_whitespace_or_newline': any(c.isspace() for c in v),
            'looks_truncated_or_extra': ("'" in v or ';' in v),
        }

    info = {
        'version': APP_VERSION,
        'env': {
            'CHANNEL_ACCESS_TOKEN': describe(token),
            'GEMINI_API_KEY': describe(gkey),
            'GEMINI_MODEL': config.GEMINI_MODEL,
            'SUPABASE_URL': bool(config.SUPABASE_URL),
            'SUPABASE_ANON_KEY': bool(config.SUPABASE_ANON_KEY),
            'CRON_SECRET': bool(config.CRON_SECRET),
            'POLL_SECRET': bool(config.POLL_SECRET),
            'GITHUB_PAT': bool(config.GITHUB_PAT),
            'GOOGLE_SERVICE_ACCOUNT_JSON': bool(config.GOOGLE_SERVICE_ACCOUNT_JSON),
            'GOOGLE_CALENDAR_ID': config.GOOGLE_CALENDAR_ID,
            'SQUARE_ACCESS_TOKEN': describe(config.SQUARE_ACCESS_TOKEN),
            'SQUARE_LOCATION_ID': bool(config.SQUARE_LOCATION_ID),
        },
    }
    # Googleカレンダーが読めるか（共有設定・カレンダーIDの確認用）
    try:
        from linebot import gcal
        if gcal.is_enabled():
            events = gcal.get_events(config.today_iso())
            info['calendar_check'] = {
                'enabled': True, 'today_events': len(events),
                'sample': [e['summary'] for e in events[:3]],
                'free': gcal.free_summary(config.today_iso()),
            }
        else:
            # 連携前でも、必要なライブラリが入っているかだけは確かめておく
            from google.oauth2 import service_account  # noqa: F401
            info['calendar_check'] = {'enabled': False, 'library': 'ok'}
    except Exception as e:
        info['calendar_check'] = {'enabled': True, 'error': repr(e)}
    # Squareのトークンが有効か・店舗が見えるか（売上は取り込まず読むだけ）
    try:
        from linebot import square as square_mod
        info['square_check'] = square_mod.diagnose()
    except Exception as e:
        info['square_check'] = {'enabled': True, 'ok': False, 'error': repr(e)}
    # Geminiに実際に短い問い合わせをして、キー・モデル名が有効か確かめる
    try:
        reply = call_gemini('返答は必ず日本語で。', 'テスト。「OK」とだけ返してください。', 20)
        info['gemini_check'] = {'ok': reply is not None, 'reply': (reply or '')[:100]}
    except Exception as e:
        info['gemini_check'] = {'ok': False, 'error': str(e)}
    # LINEのAPIにトークンの有効性を問い合わせる（メッセージは送らない）
    try:
        res = requests.get(
            'https://api.line.me/v2/bot/info',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        info['line_token_check'] = {'status': res.status_code, 'body': res.text[:300]}
    except Exception as e:
        info['line_token_check'] = {'error': str(e)}
    return _respond(start_response, '200 OK', info)


def _handle_preview(environ, start_response):
    """レポートの中身をLINEに送らずに確認する（動作確認用）。POLL_SECRETで保護。"""
    if not _has_poll_secret(environ):
        return _respond(start_response, '401 Unauthorized', {'error': 'unauthorized'})
    qs = parse_qs(environ.get('QUERY_STRING', ''))
    kind = (qs.get('kind') or ['daily'])[0]
    try:
        if kind == 'parse':
            # 日付・時刻の読み取り結果だけを確認する（保存はしない）。?text=... に文面を入れる
            from linebot.tasks import extract_date_time
            body = repr(extract_date_time((qs.get('text') or [''])[0]))
        elif kind in ('plan', 'plan_tomorrow'):
            # user_id を渡さないので番号の記憶もされず、LINEにも何も送られない（純粋な下書き確認）
            from linebot import config as cfg
            from linebot.planning import build_plan_prompt
            from datetime import timedelta as _td
            date_iso = cfg.today_iso() if kind == 'plan' else cfg.iso_of_date(cfg.now_jst() + _td(days=1))
            body = build_plan_prompt(None, date_iso)[0] or '（候補になるタスクがありません）'
        elif kind == 'weekly':
            body = build_weekly_report(None)
        elif kind == 'square_detail':
            # Squareの出数・客数が取れているか確認する（保存もLINE送信もしない）。?date=YYYY-MM-DD
            from linebot import square as square_mod
            from linebot import config as cfg
            date_iso = (qs.get('date') or [cfg.today_iso()])[0]
            body = json.dumps(square_mod.sales_detail(date_iso), ensure_ascii=False)
        elif kind == 'finance':
            body = build_daily_finance_report()
        elif kind == 'finance_month':
            body = build_month_finance_report()
        elif kind == 'finance_reminder':
            body = build_finance_reminder()
        else:
            body = build_daily_report(None)
    except Exception as e:
        return _respond(start_response, '500 Internal Server Error', {'error': repr(e)})
    return _respond(start_response, '200 OK', {'kind': kind, 'text': body})


def _handle_preview_shift(environ, start_response):
    """シフト表の画像を読み取った結果だけを返す（カレンダーには一切書き込まない）。動作確認用。
    body: {"image": "data:image/png;base64,..."}
    """
    if not _has_poll_secret(environ):
        return _respond(start_response, '401 Unauthorized', {'error': 'unauthorized'}, cors=True)
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    raw = environ['wsgi.input'].read(length) if length else b'{}'
    try:
        data_url = (json.loads(raw.decode('utf-8')) or {}).get('image') or ''
    except Exception as e:
        return _respond(start_response, '400 Bad Request', {'error': f'bad json: {e}'}, cors=True)
    if ',' not in data_url:
        return _respond(start_response, '400 Bad Request', {'error': 'image missing'}, cors=True)

    import base64
    header, b64 = data_url.split(',', 1)
    mime = 'image/png' if 'png' in header else 'image/jpeg'
    try:
        image_bytes = base64.b64decode(b64)
    except Exception as e:
        return _respond(start_response, '400 Bad Request', {'error': f'base64: {e}'}, cors=True)

    from linebot import shift as shift_mod
    try:
        detected = shift_mod.looks_like_shift_table(image_bytes, mime)
        days = shift_mod.read_shift(image_bytes, mime)
        preview = shift_mod._format_pending(days) if days else ''
    except Exception as e:
        return _respond(start_response, '500 Internal Server Error', {'error': repr(e)}, cors=True)
    return _respond(start_response, '200 OK',
                    {'detected_as_shift': detected, 'days': days, 'preview': preview}, cors=True)


def _handle_setup_richmenu(environ, start_response):
    """ブラウザのCanvasで作った画像を受け取り、LINEのリッチメニューとして登録する（セットアップ時に1回だけ叩く）。"""
    if not _has_poll_secret(environ):
        return _respond(start_response, '401 Unauthorized', {'error': 'unauthorized'}, cors=True)
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    raw = environ['wsgi.input'].read(length) if length else b'{}'
    try:
        body = json.loads(raw.decode('utf-8'))
    except Exception as e:
        return _respond(start_response, '400 Bad Request', {'error': f'bad json: {e}'}, cors=True)
    try:
        result = setup_rich_menu(body.get('image'))
    except Exception as e:
        return _respond(start_response, '500 Internal Server Error', {'error': str(e)}, cors=True)
    return _respond(start_response, '200 OK', result, cors=True)


def _handle_preview_receipt(environ, start_response):
    """レシート画像（data URL または生base64）を渡すと、読み取り結果だけ返す。保存もLINE送信もしない。動作確認用。"""
    if not _has_poll_secret(environ):
        return _respond(start_response, '401 Unauthorized', {'error': 'unauthorized'}, cors=True)
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    raw = environ['wsgi.input'].read(length) if length else b'{}'
    try:
        body = json.loads(raw.decode('utf-8'))
    except Exception as e:
        return _respond(start_response, '400 Bad Request', {'error': f'bad json: {e}'}, cors=True)

    image = body.get('image') or ''
    mime = 'image/jpeg'
    if ',' in image and image.startswith('data:'):
        header, image = image.split(',', 1)
        mime = header[5:].split(';')[0] or mime
    try:
        import base64 as _b64
        image_bytes = _b64.b64decode(image)
    except Exception as e:
        return _respond(start_response, '400 Bad Request', {'error': f'bad image: {e}'}, cors=True)

    try:
        parsed = receipt_mod.read_receipt(image_bytes, mime)
    except Exception as e:
        return _respond(start_response, '500 Internal Server Error', {'error': repr(e)}, cors=True)
    return _respond(start_response, '200 OK', {'bytes': len(image_bytes), 'mime': mime, 'parsed': parsed}, cors=True)


def app(environ, start_response):
    path = environ.get('PATH_INFO', '')
    method = environ.get('REQUEST_METHOD', 'GET')

    if path == '/api/preview_receipt':
        if method == 'OPTIONS':
            return _respond(start_response, '204 No Content', {}, cors=True)
        if method == 'POST':
            return _handle_preview_receipt(environ, start_response)

    if path == '/api/preview_shift':
        if method == 'OPTIONS':
            return _respond(start_response, '204 No Content', {}, cors=True)
        if method == 'POST':
            return _handle_preview_shift(environ, start_response)
    if path == '/api/setup_richmenu':
        if method == 'OPTIONS':
            return _respond(start_response, '204 No Content', {}, cors=True)
        if method == 'POST':
            return _handle_setup_richmenu(environ, start_response)
    if path == '/api/health' and method == 'GET':
        return _handle_health(environ, start_response)
    if path == '/api/preview_report' and method == 'GET':
        return _handle_preview(environ, start_response)
    if path == '/api/webhook' and method == 'POST':
        return _handle_webhook(environ, start_response)
    # 毎朝のタスクリマインド（/api/cron_morning_reminder）と振り返りの催促（/api/cron_daily_log_prompt）は廃止。
    # 「明日やることを決める」「今日やることを渡す」だけに絞るため（planning.py を参照）。
    if path == '/api/cron_agent_checkin' and method == 'GET':
        return _run_cron(environ, start_response, send_agent_checkin, 'send_agent_checkin')
    if path == '/api/cron_weekly_report' and method == 'GET':
        return _run_weekly_report(environ, start_response)
    if path == '/api/cron_finance_report' and method == 'GET':
        return _run_cron(environ, start_response, send_finance_report, 'send_finance_report')
    if path == '/api/cron_square_sync' and method == 'GET':
        from linebot.square import sync_daily
        return _run_cron(environ, start_response, sync_daily, 'square_sync')
    if path == '/api/cron_weekly_cf' and method == 'GET':
        from linebot.weekly_cf import send_weekly_cf
        return _run_cron(environ, start_response, send_weekly_cf, 'weekly_cf')
    if path == '/api/cron_finance_reminder' and method == 'GET':
        return _run_cron(environ, start_response, send_finance_reminder, 'send_finance_reminder')
    if path == '/api/cron_poll' and method == 'GET':
        return _run_poll(environ, start_response)

    return _respond(start_response, '404 Not Found', {'error': 'not found'})
