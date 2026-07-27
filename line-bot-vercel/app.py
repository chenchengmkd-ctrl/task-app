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
from linebot.tasks import send_reminders, check_timed_reminders
from linebot.recurring import check_recurring_reminders
from linebot.daily_log import send_daily_log_prompt, check_daily_log_followup
from linebot.agent import send_agent_checkin, send_weekly_report
from linebot.heartbeat import push_heartbeat_commit
from linebot.richmenu import setup_rich_menu
from linebot.gemini_client import call_gemini
from linebot.line_client import push_text, get_users

# デプロイが反映されたかを /api/health で確認するための版数。コードを直すたびに上げる。
APP_VERSION = 5


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
        (check_recurring_reminders, 'check_recurring_reminders'),
        (check_timed_reminders, 'check_timed_reminders'),
        (check_daily_log_followup, 'check_daily_log_followup'),
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
        },
    }
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


def app(environ, start_response):
    path = environ.get('PATH_INFO', '')
    method = environ.get('REQUEST_METHOD', 'GET')

    if path == '/api/setup_richmenu':
        if method == 'OPTIONS':
            return _respond(start_response, '204 No Content', {}, cors=True)
        if method == 'POST':
            return _handle_setup_richmenu(environ, start_response)
    if path == '/api/health' and method == 'GET':
        return _handle_health(environ, start_response)
    if path == '/api/webhook' and method == 'POST':
        return _handle_webhook(environ, start_response)
    if path == '/api/cron_morning_reminder' and method == 'GET':
        return _run_cron(environ, start_response, send_reminders, 'send_reminders')
    if path == '/api/cron_agent_checkin' and method == 'GET':
        return _run_cron(environ, start_response, send_agent_checkin, 'send_agent_checkin')
    if path == '/api/cron_daily_log_prompt' and method == 'GET':
        return _run_cron(environ, start_response, send_daily_log_prompt, 'send_daily_log_prompt')
    if path == '/api/cron_weekly_report' and method == 'GET':
        return _run_weekly_report(environ, start_response)
    if path == '/api/cron_poll' and method == 'GET':
        return _run_poll(environ, start_response)

    return _respond(start_response, '404 Not Found', {'error': 'not found'})
