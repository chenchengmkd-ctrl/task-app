"""単一のWSGIアプリとして全エンドポイントをまとめたもの。
Vercelは requirements.txt しか無いPythonプロジェクトで /api/*.py が複数「handler」を
定義していると自動検出に失敗することがあるため、確実に動く単一エントリポイント方式
（ルート直下の app.py・トップレベル変数 app）に統一している。
URLパス自体は従来通り /api/webhook・/api/cron_* のまま（vercel.jsonのcrons設定もこのまま）。
"""
import json
from urllib.parse import parse_qs

from linebot import config
from linebot.router import handle_event
from linebot.tasks import send_reminders, check_timed_reminders
from linebot.recurring import check_recurring_reminders
from linebot.daily_log import send_daily_log_prompt, check_daily_log_followup
from linebot.agent import send_agent_checkin, send_weekly_report
from linebot.heartbeat import push_heartbeat_commit
from linebot.line_client import push_text, get_users


def _respond(start_response, status, body):
    payload = json.dumps(body).encode('utf-8')
    start_response(status, [('Content-Type', 'application/json')])
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


def app(environ, start_response):
    path = environ.get('PATH_INFO', '')
    method = environ.get('REQUEST_METHOD', 'GET')

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
