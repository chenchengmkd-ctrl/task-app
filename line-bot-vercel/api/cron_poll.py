"""5分おき：時刻指定タスクの直前リマインド・定期タスクのリマインド・振り返りの1時間後再催促。
GitHub Actionsから ?key=POLL_SECRET を付けて呼ばれる（Vercel Hobbyは日次Cronしか使えないため）。
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from linebot import config
from linebot.tasks import check_timed_reminders
from linebot.recurring import check_recurring_reminders
from linebot.daily_log import check_daily_log_followup
from linebot.line_client import push_text, get_users


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        key = (query.get('key') or [None])[0]
        if not config.POLL_SECRET or key != config.POLL_SECRET:
            self.send_response(401)
            self.end_headers()
            return

        for fn, name in (
            (check_recurring_reminders, 'check_recurring_reminders'),
            (check_timed_reminders, 'check_timed_reminders'),
            (check_daily_log_followup, 'check_daily_log_followup'),
        ):
            try:
                fn(push_text, get_users)
            except Exception as e:
                print(f'{name} error:', e)

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok": true}')
