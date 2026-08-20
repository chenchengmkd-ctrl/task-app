"""「明日何をやるか」「今日何をやるか」を決めるための仕組み。このボットの中心。

1日の流れ（すべて5分おきのポーリング → /api/cron_poll から呼ばれる）
・夕方17:00 …… 翌日の候補を出して「どれをやるか」聞く（前日のうちに決めきる）
・朝 7:00 …… 決めたぶんを「今日やること」として送る。前日に決めていなければ、その場で今日ぶんを決めてもらう
・夜21:00 …… その日の答え合わせ。明日ぶんが未定ならこのメッセージの末尾で促す（通知の数は増やさない）

候補は長くなりすぎると読まれないので上限をかけ、あふれたぶんはアプリへ誘導する。
"""
import re
from datetime import timedelta

from . import config
from .supabase_client import get_supabase, get_state, set_state, delete_state

PLAN_PROMPT_AT = (17, 0)      # 前日の夕方：翌日やることを決める
PLAN_MORNING_AT = (7, 0)      # 朝：今日やることを送る（未定ならその場で決める）
PLAN_REVIEW_AT = (21, 0)      # 夜：その日の答え合わせ
PLAN_ROLLOVER_HOUR = 17       # この時刻以降は「翌日ぶんの予定」を扱う（＝夕方の問いかけと合わせる）
PLAN_MAX_CANDIDATES = 12      # LINEで一目で選べる件数。あふれたぶんはアプリで見てもらう
PENDING_VALID_HOURS = 18      # 夕方に聞いて翌朝に返事が来ても受け取れる長さ


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
    """今この瞬間に扱っている「予定の日」。夕方（17時以降）は翌日ぶん、それ以外はその日ぶんを指す。"""
    d = now + timedelta(days=1) if now.hour >= PLAN_ROLLOVER_HOUR else now
    return config.iso_of_date(d)


# ============ 候補の洗い出し ============
def _candidates(plan_date_iso):
    """未完了タスクを全部、期限切れ → その日が期限 → それ以降 → 期限なし に分けて返す。
    各グループの中は「優先順位のダブルマトリックス」順（すぐ終わるものが先）に並べる。
    """
    from . import priority as priority_mod

    rows = get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title,status,due,priority,estimate')
    overdue, on_day, later, no_due = [], [], [], []
    for r in rows:
        item = {'id': r['id'], 'title': str(r['title']),
                'status': config.STATUS_LABEL_JP.get(r.get('status'), r.get('status') or '未着手'),
                'due': r.get('due') or '',
                'priority': r.get('priority') or 'low',
                'estimate': r.get('estimate') or ''}
        if not item['due']:
            no_due.append(item)
        elif item['due'] < plan_date_iso:
            overdue.append(item)
        elif item['due'] == plan_date_iso:
            on_day.append(item)
        else:
            later.append(item)
    for arr in (overdue, on_day, later, no_due):
        arr.sort(key=lambda t: priority_mod.sort_key(t, plan_date_iso))
    return overdue, on_day, later, no_due


def _pick_balanced(groups, cap):
    """各グループから順ぐりに1件ずつ取って上限まで埋める。→ 選ばれたタスクのidの集合
    先頭から単純に切ると、期限切れが溜まっているときにそれだけで埋まってしまい、
    「明日が期限」のタスクが1件も出てこなくなるため。
    空いたグループのぶんは残りのグループに回る。
    """
    picked, idx = [], [0] * len(groups)
    while len(picked) < cap:
        moved = False
        for gi, g in enumerate(groups):
            if idx[gi] >= len(g):
                continue
            picked.append(g[idx[gi]]['id'])
            idx[gi] += 1
            moved = True
            if len(picked) >= cap:
                break
        if not moved:
            break
    return set(picked)


def build_plan_prompt(user_id, plan_date_iso):
    """候補を番号つきで並べたメッセージと、番号→タスクの対応を作る。→ (本文, items) 候補なしは (None, [])
    番号はこのメッセージで1から振り直す（他のメッセージの番号とはズレるが、その場で選びやすくするため）。
    """
    from .tasks import set_last_list

    overdue, on_day, later, no_due = _candidates(plan_date_iso)
    all_candidates = overdue + on_day + later + no_due
    # 表示は上から順に読めるよう、選び直したあとで元の並び（グループ順）に戻す
    picked_ids = _pick_balanced([overdue, on_day, later, no_due], PLAN_MAX_CANDIDATES)
    ordered = [t for t in all_candidates if t['id'] in picked_ids]
    if not ordered:
        return None, []

    numbered = set_last_list(user_id, ordered)
    num_by_id = {it['id']: it['num'] for it in numbered}

    def block(title, arr):
        arr = [t for t in arr if t['id'] in num_by_id]
        if not arr:
            return ''
        lines = []
        for t in arr:
            due_part = f"（{config.jp(t['due'])}）" if t['due'] else ''
            est_part = f"［{t['estimate']}］" if t.get('estimate') else ''
            lines.append(f"{num_by_id[t['id']]}. {t['title']}{due_part}{est_part}")
        return f'\n{title}\n' + '\n'.join(lines) + '\n'

    d = config.parse_date(plan_date_iso)
    label = '今日' if plan_date_iso == config.today_iso() else '明日'
    msg = f'🌙 {label}（{config.jp2(d)}）やることを決めましょう\n'
    # Googleカレンダーを連携していれば、その日の空き時間を先に見せる（詰め込みすぎ防止）
    from . import gcal
    free = gcal.free_summary(plan_date_iso)
    if free:
        msg += '\n' + free + '\n'
    msg += block('🔴 期限切れ', overdue)
    msg += block(f'📅 {label}が期限', on_day)
    msg += block('⏳ 期限はこの先', later)
    msg += block('⚪ 期限なし', no_due)

    rest = len(all_candidates) - len(ordered)
    if rest:
        # 全部並べると読む気が失せるので、上位だけ出して残りは件数だけ伝える
        msg += f'\n…ほか{rest}件。全部見て並べ替えるならアプリが早いです\n{config.APP_URL}\n'

    msg += ('\n（重要度が高い順・すぐ終わる順に並べてあります。上から選ぶほど成果につながりやすいです）'
            '\n\nやる番号を送ってください（例：1,3,5）。'
            '\n全部なら「全部」、決めないなら「なし」。'
            '\nここで新しいタスクを送れば、そのまま候補に足せます。')
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
            delete_state(_pending_key(uid))
            set_state(_plan_key(uid, plan_date), {'items': [], 'decidedAt': config.now_iso(), 'source': 'empty'})
            continue
        set_state(_pending_key(uid), {'date': plan_date, 'items': items, 'createdAt': config.now_ms()})
        push_text_fn(uid, msg)


def _ask_now(user_id, plan_date, empty_message):
    """自動の時刻を待たずに、その場で候補を出して選んでもらう。"""
    msg, items = build_plan_prompt(user_id, plan_date)
    if not msg:
        return empty_message
    set_state(_sent_key(user_id, plan_date, 'prompt'), True)
    set_state(_pending_key(user_id), {'date': plan_date, 'items': items, 'createdAt': config.now_ms()})
    return msg


def plan_prompt_on_demand(user_id):
    """「予定を決める」。夕方以降なら明日ぶん、それより前ならその日ぶんを扱う。"""
    return _ask_now(user_id, plan_date_for(config.now_jst()), '🌙 やる候補のタスクはありません。')


def plan_today_on_demand(user_id):
    """「今日決める」。時間帯に関係なく必ず今日ぶん。"""
    return _ask_now(user_id, config.today_iso(), '🌙 今日やる候補のタスクはありません。')


def plan_tomorrow_on_demand(user_id):
    """「明日決める」。時間帯に関係なく必ず明日ぶん。"""
    tomorrow = config.iso_of_date(config.now_jst() + timedelta(days=1))
    return _ask_now(user_id, tomorrow, '🌙 明日やる候補のタスクはありません。')


# ============ 朝：今日やることを渡す ============
def build_today_list(user_id, items, date_iso):
    """決めたぶんのうち、まだ終わっていないものを番号つきで並べる。全部終わっていればNone。"""
    from .tasks import set_last_list

    ids = ','.join(str(it['id']) for it in items)
    rows = get_supabase('tasks', f'id=in.({ids})&select=id,title,done,deleted,estimate') if ids else []
    by_id = {r['id']: r for r in rows}

    live = []
    for it in items:
        r = by_id.get(it['id'])
        if not r or r.get('deleted') or r.get('done'):
            continue
        live.append({'id': it['id'], 'title': str(r.get('title') or it['title']),
                     'estimate': r.get('estimate') or ''})
    if not live:
        return None

    numbered = set_last_list(user_id, live)
    est_by_id = {t['id']: t['estimate'] for t in live}
    lines = '\n'.join(
        f"{it['num']}. {it['title']}" + (f"［{est_by_id[it['id']]}］" if est_by_id.get(it['id']) else '')
        for it in numbered
    )
    d = config.parse_date(date_iso)
    label = '今日' if date_iso == config.today_iso() else config.jp2(d)
    msg = f'☀️ {label}やること（{len(live)}件）\n{lines}'
    finished = len(items) - len(live)
    if finished:
        msg += f'\n\n（決めた{len(items)}件のうち{finished}件は完了済みです）'
    msg += ('\n\n終わったら番号で送ってください（例：1完了）。'
            f'\n夜{PLAN_REVIEW_AT[0]}時に答え合わせします。')
    return msg


def send_plan_morning(push_text_fn, get_users_fn, now):
    """朝：前日に決めたぶんを「今日やること」として渡す。
    決めていなければ、その場で今日ぶんを決めてもらう（＝朝が最後の決めどき）。
    """
    date_iso = config.iso_of_date(now)
    for uid in get_users_fn():
        if get_state(_sent_key(uid, date_iso, 'morning')):
            continue
        set_state(_sent_key(uid, date_iso, 'morning'), True)

        plan = get_state(_plan_key(uid, date_iso))
        if plan is None:
            msg, items = build_plan_prompt(uid, date_iso)
            if not msg:
                continue
            set_state(_sent_key(uid, date_iso, 'prompt'), True)
            set_state(_pending_key(uid), {'date': date_iso, 'items': items, 'createdAt': config.now_ms()})
            push_text_fn(uid, '☀️ 今日やることがまだ決まっていません。今のうちに決めましょう。')
            push_text_fn(uid, msg)
            continue

        items = plan.get('items') or []
        if not items:
            continue          # 「なし」で決めた日は、朝も静かにしておく
        msg = build_today_list(uid, items, date_iso)
        if msg:
            push_text_fn(uid, msg)


def show_plan(user_id):
    """「今日の予定」で、今日やると決めたぶんをその場で確認する。"""
    date_iso = config.today_iso()
    plan = get_state(_plan_key(user_id, date_iso))
    if plan is None:
        return ('📋 今日やることはまだ決まっていません。\n'
                '→ 「今日決める」と送れば、今すぐ候補が出ます。\n'
                f'→ 自動では前日の{PLAN_PROMPT_AT[0]}時に翌日ぶんをお聞きします。')
    if not (plan.get('items') or []):
        return '📋 今日は「予定なし」で決めています。決め直すなら「今日決める」と送ってください。'
    return build_today_list(user_id, plan['items'], date_iso) or '🎉 今日やると決めたぶんは全部終わりました！'


# ============ 予定への返信 ============
# 「6/30」のような日付の返信と取り違えないよう、区切りはカンマ類と空白だけに絞る
_ONLY_NUMBERS = re.compile(r'^[\d\s,、，と]+$')
_ZEN2HAN = str.maketrans('０１２３４５６７８９', '0123456789')


def get_pending_plan(user_id):
    """予定を選んでもらっている最中かどうか。最中ならその状態を返す。"""
    pending = get_state(_pending_key(user_id))
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 3600000 > PENDING_VALID_HOURS:
        return None
    return pending


def looks_like_plan_reply(user_id, text):
    """予定の選択（「1,3,5」）らしい返信かどうか。期限・時刻の確認待ちより優先させるための判定。"""
    if not get_pending_plan(user_id):
        return False
    return bool(_ONLY_NUMBERS.match(text.strip().translate(_ZEN2HAN)))


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
        when = '今日' if plan_date == config.today_iso() else config.jp2(config.parse_date(plan_date))
        return f'了解です。{when}やることは決めずにおきます。'

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
    msg = f'✅ {config.jp2(d)}にやること（{len(chosen)}件）\n{body}\n'
    if plan_date != config.today_iso():
        msg += f'\n当日の朝{PLAN_MORNING_AT[0]}時にこのリストを送ります。'
    msg += f'\n当日の夜{PLAN_REVIEW_AT[0]}時に、できたかどうか確認します。'
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
        return (f'📋 {config.jp2(d)}にやることは決まっていませんでした。\n\n'
                '→ いま決めるなら「今日決める」と送ってください（候補が番号つきで出ます）。\n'
                f'→ 自動では毎日{PLAN_PROMPT_AT[0]}時に翌日ぶんをお聞きします。')
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
    """夜の答え合わせ。明日ぶんが未定ならこのメッセージの末尾で促す（通知そのものは増やさない）。"""
    date_iso = config.iso_of_date(now)
    tomorrow_iso = config.iso_of_date(now + timedelta(days=1))
    for uid in get_users_fn():
        if get_state(_sent_key(uid, date_iso, 'review')):
            continue
        set_state(_sent_key(uid, date_iso, 'review'), True)
        msg = build_plan_review(uid, date_iso)
        if get_state(_plan_key(uid, tomorrow_iso)) is None:
            msg = (msg or f'📋 {config.jp2(now)}の答え合わせ')
            msg += ('\n\n──\n🌙 明日やることがまだ決まっていません。'
                    '\n「明日決める」と送れば候補が出ます。')
        if msg:
            push_text_fn(uid, msg)


# ============ 5分おきのポーリングから呼ばれる入口 ============
def check_daily_plan(push_text_fn, get_users_fn):
    now = config.now_jst()
    if _due_now(now, PLAN_MORNING_AT):
        send_plan_morning(push_text_fn, get_users_fn, now)
    if _due_now(now, PLAN_PROMPT_AT):
        send_plan_prompt(push_text_fn, get_users_fn, now)
    if _due_now(now, PLAN_REVIEW_AT):
        send_plan_review(push_text_fn, get_users_fn, now)
