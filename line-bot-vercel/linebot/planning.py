"""明日やることを決める仕組み。
・毎晩23:30 …… 翌日の候補タスクを番号つきで洗い出して「どれをやるか」聞く
・決まらない場合 …… 0:30 / 6:30 / 8:00 に催促（8時が最終期限）
・毎晩22:30 …… その日の予定の答え合わせ（完了したか報告し、未完了はそのままリマインド）
いずれも5分おきのポーリング（GitHub Actions → /api/cron_poll）から呼ばれる。
"""
import re
from datetime import timedelta

from . import config
from .supabase_client import get_supabase, get_state, set_state, delete_state

PLAN_PROMPT_AT = (23, 30)                     # 翌日の予定を聞く時刻
PLAN_NUDGE_AT = [(0, 30), (6, 30), (8, 0)]    # 決まっていないときの催促時刻（最後が期限）
PLAN_DEADLINE = PLAN_NUDGE_AT[-1]
PLAN_REVIEW_AT = (22, 30)                     # その日の答え合わせ（23時前）
PLAN_ROLLOVER_HOUR = 20                       # この時刻以降は「翌日ぶんの予定」を扱う
PLAN_MAX_CANDIDATES = 15
PENDING_VALID_HOURS = 12


def _plan_key(uid, date_iso):
    return f'PLAN_{uid}_{date_iso}'


def _sent_key(uid, date_iso, tag):
    return f'PLAN_SENT_{uid}_{date_iso}_{tag}'


def _pending_key(uid):
    return f'PENDING_PLAN_{uid}'


def _due_now(now, hm, window_min=60):
    """指定時刻を過ぎてから window_min 分以内かどうか。
    ポーリングが多少遅れても取りこぼさず、かつ何時間も後に暴発しないようにするための判定。
    """
    target = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    elapsed = (now - target).total_seconds() / 60
    return 0 <= elapsed < window_min


def plan_date_for(now):
    """今この瞬間に扱っている「予定の日」。夜（20時以降）は翌日ぶん、それ以外はその日ぶんを指す。"""
    d = now + timedelta(days=1) if now.hour >= PLAN_ROLLOVER_HOUR else now
    return config.iso_of_date(d)


# ============ 候補の洗い出し ============
def _candidates(plan_date_iso):
    """その日にやる候補。期限切れ → その日が期限 → それ以降・期限なし の順に並べる。"""
    rows = get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title,status,due')
    overdue, on_day, later = [], [], []
    for r in rows:
        item = {'id': r['id'], 'title': str(r['title']),
                'status': config.STATUS_LABEL_JP.get(r.get('status'), r.get('status') or '未着手'),
                'due': r.get('due') or ''}
        if item['due'] and item['due'] < plan_date_iso:
            overdue.append(item)
        elif item['due'] == plan_date_iso:
            on_day.append(item)
        else:
            later.append(item)
    overdue.sort(key=lambda t: t['due'])
    later.sort(key=lambda t: t['due'] or '9999-99-99')
    return overdue, on_day, later


def build_plan_prompt(user_id, plan_date_iso):
    """翌日の候補を番号つきで並べたメッセージと、番号→タスクの対応を作る。→ (本文, items) 候補なしは (None, [])"""
    from .tasks import append_to_last_list

    overdue, on_day, later = _candidates(plan_date_iso)
    room = PLAN_MAX_CANDIDATES - len(overdue) - len(on_day)
    later = later[:max(room, 0)]
    ordered = overdue + on_day + later
    if not ordered:
        return None, []

    numbered = append_to_last_list(user_id, ordered)
    num_by_id = {it['id']: it['num'] for it in numbered}

    def block(title, arr):
        if not arr:
            return ''
        lines = []
        for t in arr:
            due_part = f"（{config.jp(t['due'])}）" if t['due'] else '（期限なし）'
            lines.append(f"{num_by_id[t['id']]}. {t['title']}{due_part}")
        return f'\n{title}\n' + '\n'.join(lines) + '\n'

    d = config.parse_date(plan_date_iso)
    label = '今日' if plan_date_iso == config.today_iso() else '明日'
    msg = f'🌙 {label}（{config.jp2(d)}）やることを決めましょう\n'
    msg += block('🔴 期限切れ', overdue)
    msg += block(f'📅 {label}が期限', on_day)
    msg += block('⏳ その他の候補', later)
    msg += ('\nやる番号を送ってください（例：1,3,5）。'
            '\n全部なら「全部」、決めないなら「なし」。'
            f'\n⏰ 朝{PLAN_DEADLINE[0]}時までに決めてください。')
    return msg, [{'num': num_by_id[t['id']], 'id': t['id'], 'title': t['title']} for t in ordered]


def send_plan_prompt(push_text_fn, get_users_fn, now):
    plan_date = plan_date_for(now)
    for uid in get_users_fn():
        if get_state(_sent_key(uid, plan_date, 'prompt')):
            continue
        msg, items = build_plan_prompt(uid, plan_date)
        set_state(_sent_key(uid, plan_date, 'prompt'), True)
        if not msg:
            push_text_fn(uid, '🌙 明日やる候補のタスクはありません。ゆっくり休んでください。')
            set_state(_plan_key(uid, plan_date), {'items': [], 'decidedAt': config.now_iso(), 'source': 'empty'})
            continue
        set_state(_pending_key(uid), {'date': plan_date, 'items': items, 'createdAt': config.now_ms()})
        push_text_fn(uid, msg)


def plan_prompt_on_demand(user_id):
    """「明日の予定」と送られたときに、その場で候補を出して選んでもらう（23:30を待たずに決められる）。"""
    now = config.now_jst()
    plan_date = plan_date_for(now)
    msg, items = build_plan_prompt(user_id, plan_date)
    if not msg:
        return '🌙 やる候補のタスクはありません。'
    set_state(_sent_key(user_id, plan_date, 'prompt'), True)
    set_state(_pending_key(user_id), {'date': plan_date, 'items': items, 'createdAt': config.now_ms()})
    return msg


def show_plan(user_id):
    """「今日の予定」で、決めたぶんの進み具合をその場で確認する。"""
    msg = build_plan_review(user_id, config.today_iso())
    if msg:
        return msg
    return '📋 今日は「予定なし」で決めています。決め直すなら「明日の予定」と送ってください。'


def send_plan_nudge(push_text_fn, get_users_fn, now, hm):
    """まだ決まっていなければ催促する。8時（最終期限）だけ文面を変える。"""
    plan_date = plan_date_for(now)
    tag = f'nudge{hm[0]:02d}{hm[1]:02d}'
    is_last = hm == PLAN_DEADLINE
    for uid in get_users_fn():
        if not get_state(_sent_key(uid, plan_date, 'prompt')):
            continue          # そもそも聞いていない日は催促しない
        if get_state(_plan_key(uid, plan_date)):
            continue          # もう決まっている
        if get_state(_sent_key(uid, plan_date, tag)):
            continue
        set_state(_sent_key(uid, plan_date, tag), True)
        if is_last:
            msg = (f'⚠️ 朝{PLAN_DEADLINE[0]}時になりました。今日やることがまだ決まっていません。\n'
                   '番号だけでもいいので送ってください（例：1,3,5）。'
                   '\n候補をもう一度見るなら「一覧」または「通知」。')
        else:
            msg = ('🌙 今日やることがまだ決まっていません。\n'
                   f'番号を送ってください（例：1,3,5）。朝{PLAN_DEADLINE[0]}時までにお願いします。')
        push_text_fn(uid, msg)


# ============ 予定への返信 ============
_ONLY_NUMBERS = re.compile(r'^[\d\s,、，\.。・/／と]+$')
_ZEN2HAN = str.maketrans('０１２３４５６７８９', '0123456789')


def handle_pending_plan_reply(user_id, text):
    """「1,3,5」のような番号だけの返信を、その日の予定として確定する。該当なしはNone。"""
    key = _pending_key(user_id)
    pending = get_state(key)
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 3600000 > PENDING_VALID_HOURS:
        delete_state(key)
        return None

    t = text.strip().translate(_ZEN2HAN)
    plan_date = pending.get('date')
    items = pending.get('items') or []

    if re.match(r'^(なし|未定|決めない|スキップ|やめて|あとで)[。.!！]?$', t):
        delete_state(key)
        set_state(_plan_key(user_id, plan_date), {'items': [], 'decidedAt': config.now_iso(), 'source': 'none'})
        return '了解です。今日やることは決めずにおきます。'

    missing = []
    if re.match(r'^(全部|ぜんぶ|すべて|全て)[。.!！]?$', t):
        chosen = items
    else:
        if not _ONLY_NUMBERS.match(t):
            return None
        nums = [int(n) for n in re.findall(r'\d+', t)]
        if not nums:
            return None
        by_num = {it['num']: it for it in items}
        chosen = [by_num[n] for n in nums if n in by_num]
        missing = [n for n in nums if n not in by_num]
        if not chosen:
            return (f"⚠️ {'、'.join(str(n) for n in missing)} 番が候補の中に見つかりませんでした。"
                    'もう一度、表示された番号で送ってください。')

    delete_state(key)
    set_state(_plan_key(user_id, plan_date), {
        'items': [{'id': c['id'], 'title': c['title']} for c in chosen],
        'decidedAt': config.now_iso(), 'source': 'reply',
    })
    d = config.parse_date(plan_date)
    body = '\n'.join(f"{c['num']}. {c['title']}" for c in chosen)
    msg = (f'✅ {config.jp2(d)}にやること（{len(chosen)}件）\n{body}\n\n'
           f'当日の{PLAN_REVIEW_AT[0]}:{PLAN_REVIEW_AT[1]:02d}に、できたかどうか確認します。')
    if missing:
        msg += f"\n\n⚠️ {'、'.join(str(n) for n in missing)} 番は候補になかったので入れていません。"
    return msg


# ============ 答え合わせ ============
def build_plan_review(user_id, date_iso):
    """その日の予定が実際にどうなったかをまとめる。予定が無い日はNone。"""
    from .tasks import append_to_last_list

    plan = get_state(_plan_key(user_id, date_iso))
    d = config.parse_date(date_iso)
    if not plan:
        return (f'📋 {config.jp2(d)}にやることは決まっていませんでした。\n'
                f'このあと{PLAN_PROMPT_AT[0]}:{PLAN_PROMPT_AT[1]:02d}に明日のぶんを聞きます。')
    items = plan.get('items') or []
    if not items:
        return None

    ids = ','.join(str(it['id']) for it in items)
    rows = get_supabase('tasks', f'id=in.({ids})&select=id,title,done,deleted,status')
    by_id = {r['id']: r for r in rows}

    done, undone, gone = [], [], []
    for it in items:
        r = by_id.get(it['id'])
        if not r or r.get('deleted'):
            gone.append(it)
        elif r.get('done'):
            done.append(it)
        else:
            undone.append({'id': it['id'], 'title': str(r.get('title') or it['title'])})

    total = len(done) + len(undone)
    msg = f'📋 {config.jp2(d)}の予定の答え合わせ\n'
    msg += f'\n✅ 完了：{len(done)}件 / 予定{total}件'
    if done:
        msg += '\n' + '\n'.join(f"・{c['title']}" for c in done)

    if undone:
        numbered = append_to_last_list(user_id, undone)
        msg += f'\n\n❌ できていない：{len(undone)}件\n'
        msg += '\n'.join(f"{it['num']}. {it['title']}" for it in numbered)
        msg += ('\n\n→ 終わっているなら番号で（例：1完了）'
                '\n→ 今日やらないなら日付を（例：1明日）')
    elif total:
        msg += '\n\n🎉 予定していたぶんは全部終わりました！'

    if gone:
        msg += f'\n\n（削除されたもの：{len(gone)}件）'
    return msg


def send_plan_review(push_text_fn, get_users_fn, now):
    date_iso = config.iso_of_date(now)
    for uid in get_users_fn():
        if get_state(_sent_key(uid, date_iso, 'review')):
            continue
        set_state(_sent_key(uid, date_iso, 'review'), True)
        msg = build_plan_review(uid, date_iso)
        if msg:
            push_text_fn(uid, msg)


# ============ 5分おきのポーリングから呼ばれる入口 ============
def check_daily_plan(push_text_fn, get_users_fn):
    now = config.now_jst()
    if _due_now(now, PLAN_REVIEW_AT):
        send_plan_review(push_text_fn, get_users_fn, now)
    if _due_now(now, PLAN_PROMPT_AT):
        send_plan_prompt(push_text_fn, get_users_fn, now)
    for hm in PLAN_NUDGE_AT:
        if _due_now(now, hm):
            send_plan_nudge(push_text_fn, get_users_fn, now, hm)
