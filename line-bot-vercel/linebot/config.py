"""環境変数・定数・JST（日本時間）ヘルパー。Code.gs 冒頭の定数群に相当。"""
import os
import random
import string
import time
from datetime import datetime, timedelta, timezone

CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_ANON_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite')
CRON_SECRET = os.environ.get('CRON_SECRET', '')
POLL_SECRET = os.environ.get('POLL_SECRET', '')
GITHUB_PAT = os.environ.get('GITHUB_PAT', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', '')

REMIND_HOUR = 6            # 毎朝の通知時刻（時・24時間制、JST）
REMIND_MINUTE = 0
AGENT_HOUR = 20             # 毎晩のAI進捗チェックイン時刻（JST）
TIME_LEAD_MINUTES = 10       # 時刻指定タスクの何分前にリマインドするか
TIME_CHECK_INTERVAL = 5      # 5分おきのポーリング間隔（GitHub Actions側の間隔と合わせる）
REC_NEAR_DAYS = 3            # 通知・時刻確認の対象とする「期限が近い」日数

REPLY_URL = 'https://api.line.me/v2/bot/message/reply'
PUSH_URL = 'https://api.line.me/v2/bot/message/push'

STATUS_ORDER = ['未着手', '着手中', '対応待ち', 'ペンディング']
STATUS_ICON = {'未着手': '⚪', '着手中': '🔵', '対応待ち': '🟣', 'ペンディング': '🟡'}
STATUS_LABEL_JP = {'todo': '未着手', 'doing': '着手中', 'waiting': '対応待ち', 'pending': 'ペンディング'}

AGENT_PERSONA = '\n'.join([
    'あなたは経験豊富な、やや厳しめのプロジェクトマネージャーです。',
    'ユーザーのタスク管理データ（進捗状況・期限・最終更新からの経過日数・振り返りログ・直近の完了実績・過去の会話履歴）を分析し、率直かつ具体的にコメントします。',
    '進捗が悪いタスク、期限超過、長期間放置されているタスクは遠慮なく指摘してください。順調な進捗は簡潔に認めます。',
    '一般論ではなく、渡されたデータに含まれる具体的なタスク名・経過日数・期限をもとに指摘してください。',
    'タスクの進め方や成功のさせ方について聞かれた場合は、単なる励ましや進捗評価にとどまらず、そのタスクの分野に詳しいスペシャリストとして、具体的な進め方・実務上の注意点・コツを提示してください。',
    '常に日本語で、LINEのトーク画面に収まる分量（300字程度まで）に収め、絵文字は使っても最小限にし、要点を箇条書き中心でまとめてください。',
    'LINEはMarkdown記法を装飾として表示できません。「**太字**」「*斜体*」「#見出し」「1. 番号リスト」などの記法は絶対に使わず、記号を含まない普通の文章で書いてください。箇条書きは行頭に「・」だけを使ってください。'
])

JST = timezone(timedelta(hours=9))


def now_jst():
    """現在時刻をJST（日本時間）で返す。"""
    return datetime.now(timezone.utc).astimezone(JST)


def midnight(d):
    """その日の0時0分に丸める。"""
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def today_iso():
    """今日の日付（YYYY-MM-DD、JST基準）。"""
    return now_jst().strftime('%Y-%m-%d')


def parse_date(v):
    """'YYYY-MM-DD'（区切りは-または/）文字列をJST 0時のdatetimeに変換。パース不可はNone。"""
    if not v:
        return None
    parts = str(v).strip().replace('/', '-').split('-')
    if len(parts) < 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2][:2])
        return datetime(y, m, d, tzinfo=JST)
    except ValueError:
        return None


def day_diff(today, date_obj):
    """今日との日数差（0=今日, 1=明日, -1=昨日）。todayもdate_objもJSTのdatetimeで0時丸め済み想定。"""
    return round((midnight(date_obj) - midnight(today)).total_seconds() / 86400)


def jp(iso):
    """'YYYY-MM-DD' を 'M/D' 表記に。"""
    d = parse_date(iso)
    return f'{d.month}/{d.day}' if d else iso


def jp2(d):
    """datetimeを 'M/D' 表記に。"""
    return f'{d.month}/{d.day}'


def iso_of_date(d):
    """datetimeを 'YYYY-MM-DD' に。"""
    return d.strftime('%Y-%m-%d')


_BASE36 = '0123456789abcdefghijklmnopqrstuvwxyz'


def _to_base36(n):
    if n == 0:
        return '0'
    digits = []
    while n:
        n, r = divmod(n, 36)
        digits.append(_BASE36[r])
    return ''.join(reversed(digits))


def new_id():
    """タスク等のID生成。Code.gs の Date.now().toString(36)+ランダム6文字 に相当（一意であればよい）。"""
    ms = int(time.time() * 1000)
    rand_part = ''.join(random.choice(_BASE36) for _ in range(6))
    return _to_base36(ms) + rand_part


def now_iso():
    """現在時刻をISO8601（UTC、'Z'表記）で返す。Supabaseのtimestamptz列に書き込む用。"""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def now_ms():
    """エポックミリ秒（保留状態の経過時間チェック用）。"""
    return int(time.time() * 1000)


def parse_timestamp(s):
    """SupabaseのISOタイムスタンプ文字列（updated_at/created_at）をJSTのdatetimeに変換。パース不可はNone。"""
    if not s:
        return None
    s2 = str(s).replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST)
