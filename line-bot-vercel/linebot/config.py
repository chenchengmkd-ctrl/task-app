"""環境変数・定数・JST（日本時間）ヘルパー。Code.gs 冒頭の定数群に相当。"""
import os
import random
import re
import string
import time
from datetime import datetime, timedelta, timezone


def _env(name, default=''):
    """環境変数を読む。値の前後だけでなく途中の改行・空白も取り除く。
    トークンやAPIキーは貼り付け時に改行や空白が紛れ込みやすく、そのままだと
    HTTPヘッダに載せた時点でエラーになったり認証に失敗したりするため。
    """
    return re.sub(r'\s+', '', os.environ.get(name, default) or '')


def _raw_env(name, default=''):
    """前後の空白だけを落として読む。JSONのように途中の空白に意味がある値はこちらを使う。"""
    return (os.environ.get(name, default) or '').strip()


CHANNEL_ACCESS_TOKEN = _env('CHANNEL_ACCESS_TOKEN')
SUPABASE_URL = _env('SUPABASE_URL')
SUPABASE_ANON_KEY = _env('SUPABASE_ANON_KEY')
GEMINI_API_KEY = _env('GEMINI_API_KEY')
GEMINI_MODEL = _env('GEMINI_MODEL', 'gemini-3.1-flash-lite') or 'gemini-3.1-flash-lite'
CRON_SECRET = _env('CRON_SECRET')
POLL_SECRET = _env('POLL_SECRET')
GITHUB_PAT = _env('GITHUB_PAT')
GITHUB_REPO = _env('GITHUB_REPO')
# Googleカレンダー連携（サービスアカウントのJSONキーと、読みに行くカレンダーID＝ふつうはGmailアドレス）
GOOGLE_SERVICE_ACCOUNT_JSON = _raw_env('GOOGLE_SERVICE_ACCOUNT_JSON')
GOOGLE_CALENDAR_ID = _env('GOOGLE_CALENDAR_ID')
# Square（レジアプリ）連携。アクセストークンはSquare開発者ダッシュボードで発行する。
# 店舗が1つだけならLOCATION_IDは省略可（起動時に自動で取得する）
SQUARE_ACCESS_TOKEN = _env('SQUARE_ACCESS_TOKEN')
SQUARE_LOCATION_ID = _env('SQUARE_LOCATION_ID')

APP_URL = 'https://chenchengmkd-ctrl.github.io/task-app/'   # タスク管理アプリ（LINEで羅列せずアプリに誘導する時に使う）

DAY_START = '07:00'          # 「空き時間」を数えはじめる時刻
DAY_END = '23:00'            # 「空き時間」を数え終える時刻
MIN_FREE_MINUTES = 30        # これより短い空きは数えない
TASK_SLOT_MINUTES = 60       # 空き時間から「何件くらい入るか」を見積もるときの1件あたりの目安

REMIND_HOUR = 6            # 毎朝の通知時刻（時・24時間制、JST）
REMIND_MINUTE = 0
AGENT_HOUR = 20             # 毎晩のAI進捗チェックイン時刻（JST）
TIME_LEAD_MINUTES = 10       # 時刻指定タスクの何分前にリマインドするか
# ポーリングが遅れて上の時刻を過ぎてしまった場合に、何分後まで遅れて知らせるか。
# GitHub Actionsの定期実行は数分〜十数分ずれることがあるため、これが無いと取りこぼす。
TIME_LATE_MINUTES = 45
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
