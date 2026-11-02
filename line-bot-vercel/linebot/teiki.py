"""定期報告アプリ（teiki.html）の送信記録と、期限までに送れていないときのLINE通知。

- アプリで資金報告を「LINEで送る」と、その日付を `teiki:sent:YYYY-MM-DD` に記録する
- 通知の設定（期限の時刻・オン/オフ）は `teiki:cfg` に置く。アプリの設定画面から変える
- 5分おきのポーリングで、期限を過ぎても今日の記録がなければ1回だけLINEで知らせる
"""
import re

from . import config
from .supabase_client import delete_state, get_state, set_state

CFG_KEY = 'teiki:cfg'
APP_URL = 'https://chenchengmkd-ctrl.github.io/task-app/teiki.html'
DEFAULT_CFG = {'enabled': False, 'deadline': '17:00'}
# 期限を過ぎてからこの分数のうちに見つけたときだけ知らせる（夜中に急に届かないように）
NOTIFY_WINDOW_MIN = 120


def _sent_key(date_iso):
    return f'teiki:sent:{date_iso}'


def _valid_date(d):
    return isinstance(d, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d) is not None


def _parse_hm(s):
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', str(s or ''))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def get_cfg():
    cfg = dict(DEFAULT_CFG)
    cfg.update(get_state(CFG_KEY) or {})
    return cfg


def handle(body):
    """アプリからの呼び出し。op: sent / unsent / config / status"""
    op = body.get('op')
    if op in ('sent', 'unsent'):
        d = body.get('date')
        if not _valid_date(d):
            return {'ok': False, 'error': 'date がありません'}
        if op == 'sent':
            ok = set_state(_sent_key(d), {'at': config.now_jst().isoformat()})
        else:
            ok = delete_state(_sent_key(d))
        return {'ok': bool(ok)}
    if op == 'config':
        cfg = get_cfg()
        if 'enabled' in body:
            cfg['enabled'] = bool(body.get('enabled'))
        if 'deadline' in body:
            if not _parse_hm(body.get('deadline')):
                return {'ok': False, 'error': '時刻の形が違います（例 17:00）'}
            cfg['deadline'] = body['deadline']
        ok = set_state(CFG_KEY, cfg)
        return {'ok': bool(ok), 'cfg': cfg}
    # status
    today = config.today_iso()
    return {'ok': True, 'cfg': get_cfg(), 'today': today, 'sent_today': bool(get_state(_sent_key(today)))}


def check_teiki_deadline(push_text_fn, get_users_fn):
    """5分おきのポーリングから呼ばれる。期限を過ぎても今日の資金報告が送られていなければ1回だけ知らせる。"""
    cfg = get_cfg()
    if not cfg.get('enabled'):
        return
    hm = _parse_hm(cfg.get('deadline'))
    if not hm:
        return
    now = config.now_jst()
    target = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    elapsed = (now - target).total_seconds() / 60
    if not (0 <= elapsed < NOTIFY_WINDOW_MIN):
        return
    today = config.today_iso()
    if get_state(_sent_key(today)):
        return
    key = f'TEIKI_REMIND_{today}'
    if get_state(key):
        return
    set_state(key, True)
    text = (f'⏰ {hm[0]}:{hm[1]:02d}を過ぎましたが、今日（{now.month}/{now.day}）の資金報告がまだ送られていません。\n'
            f'定期報告アプリから送ってください。\n{APP_URL}')
    for uid in get_users_fn():
        push_text_fn(uid, text)
