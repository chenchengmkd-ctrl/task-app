"""タスク追加・一覧・期限リマインド・期限/時刻の保留確認フロー。Code.gs の該当セクションに相当。
すべてSupabaseの tasks テーブルを直接操作する（受信箱や取込は無し）。
"""
import re
from urllib.parse import quote

from . import config
from .gemini_client import call_gemini, call_gemini_with_tools, TOOLS_SET_DUE_BATCH, TOOLS_SET_TIME_BATCH, extract_function_call
from .supabase_client import get_supabase, post_supabase, patch_supabase, get_state, set_state, delete_state


# ============ 日付・時刻の抽出 ============
def extract_date_time(text):
    """テキストの中から日付・時刻の表現を検出して取り除く。
    対応: 今日／明日／明後日、M/D、HH時MM分／HH時／HH:MM（文中どこにあってもよい）。
    """
    title = text
    due = ''
    due_time = ''

    rel_days = {'今日': 0, '明日': 1, '明後日': 2}
    m = re.search(r'(今日|明日|明後日)(に|の)?', title)
    if m:
        from datetime import timedelta
        d = config.now_jst() + timedelta(days=rel_days[m.group(1)])
        due = d.strftime('%Y-%m-%d')
        title = title.replace(m.group(0), '', 1)
    else:
        m = re.search(r'(\d{1,2})/(\d{1,2})(に|の)?', title)
        if m:
            y = config.now_jst().year
            due = f'{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}'
            title = title.replace(m.group(0), '', 1)

    m = re.search(r'(\d{1,2})時(\d{1,2})分(に|の)?', title)
    if m:
        due_time = f'{int(m.group(1)):02d}:{int(m.group(2)):02d}'
        title = title.replace(m.group(0), '', 1)
    else:
        m = re.search(r'(\d{1,2})時(に|の)?', title)
        if m:
            due_time = f'{int(m.group(1)):02d}:00'
            title = title.replace(m.group(0), '', 1)
        else:
            m = re.search(r'(\d{1,2}):(\d{2})(に|の)?', title)
            if m:
                due_time = f'{int(m.group(1)):02d}:{int(m.group(2)):02d}'
                title = title.replace(m.group(0), '', 1)

    title = re.sub(r'^[、,\s]+', '', title)
    title = re.sub(r'[、,\s]+$', '', title)
    title = re.sub(r'\s{2,}', ' ', title).strip()
    return {'title': title, 'due': due, 'due_time': due_time}


def parse_time_only(t):
    """テキストから時刻表現だけを抜き出す（日付の有無は問わない）。"""
    return extract_date_time(t)['due_time'] or ''


def looks_like_date_reply(t):
    """日付の返信らしさを判定（数字や日付関連の言葉を含むか）。含まなければ別の話題の返信とみなしてよい。"""
    return bool(re.search(r'[0-9０-９]|今日|明日|明後日|来週|再来週|今週|来月|月末|週末|月曜|火曜|水曜|木曜|金曜|土曜|日曜', t))


def looks_like_time_reply(t):
    """時刻の返信らしさを判定。"""
    return bool(re.search(r'[0-9０-９]|時|分', t))


# ============ AIによる整形 ============
def cleanup_task_text(text):
    """長い/雑多な文章から、実際のタスク名だけを簡潔に抽出する（失敗時はNone）。"""
    reply = call_gemini(
        'あなたはタスク管理アシスタントです。渡された文章から、実際にやるべきタスクの内容だけを抽出し、20文字程度までの簡潔な日本語のタスク名として1行で返してください。'
        '説明・前置き・箇条書き記号・敬語表現は不要です。タスク名だけを返してください。',
        text, 60,
    )
    if not reply:
        return None
    cleaned = reply.strip().split('\n')[0]
    cleaned = re.sub(r'^[・\-\d.、)]+\s*', '', cleaned).strip()
    return cleaned or None


def ai_parse_date(text):
    """「来週」「月末」のような曖昧な日付表現をAIでYYYY-MM-DDに変換する（失敗時はNone）。"""
    reply = call_gemini(
        f'今日の日付は{config.today_iso()}（YYYY-MM-DD）です。渡された文章に含まれる日付表現を、YYYY-MM-DD形式の1行だけで返してください。'
        '日付表現が無い・特定できない場合は「なし」とだけ返してください。説明は不要です。',
        text, 20,
    )
    if not reply:
        return None
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', reply.strip())
    if not m:
        return None
    return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'


# ============ タスク追加・一覧 ============
def add_line(user_id, text):
    """タスクをその場でSupabaseに追加する。"""
    parsed = extract_date_time(text)
    title = parsed['title']
    due = parsed['due']
    due_time = parsed['due_time']

    if not due and re.search(r'(来週|再来週|今週中|今月中|来月|月末|週末|月曜|火曜|水曜|木曜|金曜|土曜|日曜)', title):
        ai_due = ai_parse_date(title)
        if ai_due:
            due = ai_due

    if len(title) > 20 or re.search(r'\n|・|^[0-9]+[.、)]', title):
        cleaned = cleanup_task_text(title)
        if cleaned:
            title = cleaned

    row_id = config.new_id()
    row = {
        'id': row_id, 'title': title, 'status': 'todo', 'priority': 'mid',
        'due': due or None, 'due_time': due_time or None,
        'estimate': '', 'recurrence': 'none', 'note': '', 'tags': [],
        'done': False, 'deleted': False, 'from_line': True,
        'updated_at': config.now_iso(),
    }
    if not post_supabase('tasks', [row]):
        return '⚠️ タスクの追加に失敗しました。時間をおいて再度お試しください。'

    cnt = len(get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id'))
    msg = f'✅ 追加しました\n「{title}」'
    if due:
        msg += f'\n📅 {config.jp(due)}' + (f' {due_time}' if due_time else '')
        due_date = config.parse_date(due)
        if not due_time and due_date and config.day_diff(config.now_jst(), due_date) <= 2:
            set_state(f'PENDING_TIME_{user_id}', {
                'mode': 'single', 'taskId': row_id, 'title': title, 'createdAt': config.now_ms(),
            })
            msg += '\n\n⏰ 時間は何時ですか？（例：15:00、15時、未定なら「なし」）'
    else:
        set_state(f'PENDING_DUE_{user_id}', {
            'mode': 'single', 'tasks': [{'id': row_id, 'title': title}], 'createdAt': config.now_ms(),
        })
        msg += '\n\n📅 期限はいつにしますか？（例：6/30、今日、なし）'
    msg += f'\n\n未完了: {cnt}件'
    return msg


def get_app_tasks_all():
    """アプリの未完了タスクを全部取得（状態つき・期限の有無は問わない）。"""
    rows = get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title,status,due')
    out = []
    for r in rows:
        out.append({
            'id': r['id'], 'title': str(r['title']), 'due': r.get('due') or '',
            'status': config.STATUS_LABEL_JP.get(r.get('status'), r.get('status') or '未着手'),
        })
    return out


def list_all(user_id):
    """未完了タスクを「進捗状況ごと」に表示。番号を振り、後で「3番を〜」と指定できるよう記憶しておく。
    定期タスク（テンプレート）は別メッセージで表示する。
    """
    from . import recurring as recurring_mod

    app = get_app_tasks_all()
    rec = recurring_mod.get_recurring()
    if not app and not rec:
        return '🎉 未完了のタスクはありません。'

    msg = '📋 タスク一覧\n' if app else '🎉 未完了のタスクはありません。'
    numbered = []
    n = 0

    for st in config.STATUS_ORDER:
        arr = [t for t in app if t['status'] == st]
        if not arr:
            continue
        lines = []
        for t in arr:
            n += 1
            numbered.append({'num': n, 'id': t['id'], 'title': t['title']})
            due_part = f"（{config.jp(t['due'])}）" if t['due'] else ''
            lines.append(f"{n}. {t['title']}{due_part}")
        msg += f"\n{config.STATUS_ICON[st]} {st}（{len(arr)}）\n" + '\n'.join(lines) + '\n'

    if numbered and user_id:
        set_state(f'LAST_LIST_{user_id}', {'items': numbered, 'createdAt': config.now_ms()})
        msg += '\n番号で「3番を完了にして」のように操作できます。'
    msg = msg.strip()

    if not rec:
        return msg

    rec_msg = recurring_mod.recurring_summary_message(rec)
    return [msg, rec_msg] if app else rec_msg


def resolve_numbered_task(user_id, num):
    """直前の「一覧」で表示した番号から、実際のタスク（id・title）を引き当てる。該当なし/期限切れはNone。"""
    data = get_state(f'LAST_LIST_{user_id}')
    if not data:
        return None
    if (config.now_ms() - data.get('createdAt', 0)) / 60000 > 60:
        return None
    return next((it for it in (data.get('items') or []) if it['num'] == num), None)


# ============ リマインド ============
def get_due_from_app():
    """アプリの期限ありの未完了タスクを取得。"""
    rows = get_supabase('tasks', 'done=eq.false&deleted=eq.false&due=not.is.null&select=title,status,due,due_time')
    out = []
    for r in rows:
        due = config.parse_date(r.get('due'))
        if not r.get('title') or not due:
            continue
        out.append({
            'title': str(r['title']), 'due': due,
            'status': config.STATUS_LABEL_JP.get(r.get('status'), r.get('status') or '未着手'),
            'due_time': str(r['due_time'])[:5] if r.get('due_time') else '',
        })
    return out


def due_tag(d, date_obj, due_time=''):
    """期日を「今日/明日/N日後/N日超過 M/D」の形に（時刻があれば末尾に付ける）。"""
    md = f'{date_obj.month}/{date_obj.day}'
    time_part = f' {due_time}' if due_time else ''
    if d < 0:
        return f'⚠{-d}日超過 {md}{time_part}'
    if d == 0:
        return f'今日 {md}{time_part}'
    if d == 1:
        return f'明日 {md}{time_part}'
    return f'{d}日後 {md}{time_part}'


def build_reminder():
    """通知メッセージを組み立て（対象が無ければ空文字）。
    通常タスク: 期限が3日以内（超過含む）を、進捗状況ごとに表示（時刻が設定されていれば時刻も表示）。
    ※定期タスクのリマインドは recurring.py の check_recurring_reminders() で別メッセージとして送るため、ここには含めない。
    """
    today = config.midnight(config.now_jst())

    near = []
    for t in get_due_from_app():
        d = config.day_diff(today, t['due'])
        if d <= config.REC_NEAR_DAYS:
            near.append({**t, 'd': d})

    if not near:
        return ''

    msg = f'🔔 タスクのリマインド ({today.month}/{today.day})\n'
    for st in config.STATUS_ORDER:
        arr = sorted([t for t in near if t['status'] == st], key=lambda t: t['d'])
        if not arr:
            continue
        lines = [f"・{t['title']}（{due_tag(t['d'], t['due'], t['due_time'])}）" for t in arr]
        msg += f'\n{config.STATUS_ICON[st]} {st}\n' + '\n'.join(lines) + '\n'
    return msg.strip()


def get_no_time_soon_tasks():
    """期限が3日以内（超過含む）なのに時刻が未設定のタスクを集める。"""
    today = config.midnight(config.now_jst())
    rows = get_supabase('tasks', 'done=eq.false&deleted=eq.false&due=not.is.null&due_time=is.null&select=id,title,due')
    out = []
    for r in rows:
        d = config.parse_date(r.get('due'))
        if d and config.day_diff(today, d) <= config.REC_NEAR_DAYS:
            out.append({'id': r['id'], 'title': r['title']})
    return out


def send_reminders(push_text_fn, get_users_fn):
    """毎朝のトリガーから呼ばれる。"""
    msg = build_reminder()
    if msg:
        for uid in get_users_fn():
            push_text_fn(uid, msg)

    no_time = get_no_time_soon_tasks()
    if not no_time:
        return
    for uid in get_users_fn():
        if not get_state(f'PENDING_TIME_{uid}'):
            set_state(f'PENDING_TIME_{uid}', {'mode': 'batch', 'tasks': no_time, 'createdAt': config.now_ms()})
    time_msg = '⏰ 期限が近いのに時間未設定のタスク\n' + '\n'.join(f"・{t['title']}" for t in no_time) + \
        '\n\n時間を教えてください（例：「〇〇は15時、△△は10時」。不要なら「なし」）。'
    for uid in get_users_fn():
        push_text_fn(uid, time_msg)


# ============ 時刻指定タスクの直前リマインド ============
def get_timed_tasks_today():
    """今日が期日で、時刻が設定されている未完了タスクを集める。"""
    today_str = config.today_iso()
    rows = get_supabase(
        'tasks',
        f'done=eq.false&deleted=eq.false&due=eq.{today_str}&due_time=not.is.null&select=id,title,due_time',
    )
    return [
        {'id': r['id'], 'title': r['title'], 'due_time': str(r['due_time'])[:5]}
        for r in rows if r.get('due_time')
    ]


def check_timed_reminders(push_text_fn, get_users_fn):
    """時刻の TIME_LEAD_MINUTES 分前になったタスクをLINEに通知（重複送信は防ぐ）。"""
    today_str = config.today_iso()
    now = config.now_jst()
    items = get_timed_tasks_today()
    if not items:
        return

    prop_key = f'TIME_REMINDED_{today_str}'
    reminded = get_state(prop_key) or []

    due = []
    for it in items:
        if it['id'] in reminded:
            continue
        hh, mm = it['due_time'].split(':')
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        diff_min = (target - now).total_seconds() / 60
        if diff_min <= config.TIME_LEAD_MINUTES and diff_min > config.TIME_LEAD_MINUTES - config.TIME_CHECK_INTERVAL:
            due.append(it)
            reminded.append(it['id'])

    if not due:
        return

    msg = f'⏰ もうすぐ開始（{config.TIME_LEAD_MINUTES}分後）\n' + '\n'.join(f"・{it['title']}（{it['due_time']}〜）" for it in due)
    for uid in get_users_fn():
        push_text_fn(uid, msg)
    set_state(prop_key, reminded)


# ============ タスク単体操作（AIツールから呼ばれる） ============
def find_task_by_title(title):
    return get_supabase('tasks', f'deleted=eq.false&title=eq.{quote(title)}&select=id,title')


# ============ 保留中の期限確認への返信 ============
def convert_pending_task_to_recurring(task):
    """「定期タスクでお願いします」等の返信を受けて、直前に追加した単発タスクを定期タスクへ変換する
    （タスクのタイトルに「毎月25日」のような周期が含まれている前提でAIに読み取らせる）。
    """
    from .gemini_client import AGENT_TOOLS
    from .recurring import handle_create_recurring_task

    create_tool_decl = next(f for f in AGENT_TOOLS[0]['functionDeclarations'] if f['name'] == 'create_recurring_task')
    tool = [{'functionDeclarations': [create_tool_decl]}]
    res = call_gemini_with_tools(
        'あなたはタスク管理アシスタントです。渡されたタスクのタイトルには「毎月25日」「毎週月曜」のような繰り返しの予定が含まれています。'
        'その内容を読み取り、create_recurring_taskツールで定期タスクとして登録してください。周期を表す言葉を除いた実際の作業内容をtitleにしてください。',
        'タスク：' + task['title'],
        tool, 300,
    )
    name, args, _ = extract_function_call(res)
    if name != 'create_recurring_task':
        return f"⚠️「{task['title']}」を定期タスクとして解釈できませんでした。「毎週月曜19時に〇〇する を定期タスクにして」のように、周期を具体的に送ってください。"

    reply = handle_create_recurring_task(args, '')
    patch_supabase('tasks', f"id=eq.{quote(task['id'])}", {'deleted': True, 'updated_at': config.now_iso()})
    return reply + f"\n\n（先ほど追加した単発タスク「{task['title']}」は削除し、定期タスクに置き換えました）"


def handle_pending_due_reply(user_id, text):
    """pending中の期限確認（単発追加時／夜間の棚卸し）への返信を処理。該当なしはNoneを返す。"""
    key = f'PENDING_DUE_{user_id}'
    pending = get_state(key)
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 60000 > 360:
        delete_state(key)
        return None

    t = text.strip()
    if re.match(r'^(不要|なし|未定|スキップ|やめて|あとで)[。.!！]?$', t):
        delete_state(key)
        return '了解です、期限は未設定のままにします。'

    if pending.get('mode') == 'single':
        if '定期' in t:
            delete_state(key)
            return convert_pending_task_to_recurring(pending['tasks'][0])
        parsed = extract_date_time(t)
        if not parsed['due']:
            if not looks_like_date_reply(t):
                delete_state(key)
                return None
            return '日付を認識できませんでした。「6/30」「今日」のように送ってください（設定しない場合は「なし」）。'
        task = pending['tasks'][0]
        updated = patch_supabase('tasks', f"id=eq.{quote(task['id'])}", {
            'due': parsed['due'], 'due_time': parsed['due_time'] or None, 'updated_at': config.now_iso(),
        })
        delete_state(key)
        if updated is None:
            return f"⚠️「{task['title']}」の期限設定に失敗しました。"
        time_part = f" {parsed['due_time']}" if parsed['due_time'] else ''
        return f"✅「{task['title']}」の期限を {config.jp(parsed['due'])}{time_part} に設定しました。"

    # batchモード：複数タスクぶんの期限をまとめてAIに割り振らせる
    task_list_text = '\n'.join(f"・{pt['title']}" for pt in pending['tasks'])
    res = call_gemini_with_tools(
        f'今日の日付は{config.today_iso()}（YYYY-MM-DD）です。ユーザーの返信文から、下記タスク一覧のうちどのタスクにどの期限を割り当てたいか読み取り、'
        'set_due_datesツールで返してください。返信で触れられていないタスクは含めないでください。',
        f'【タスク一覧】\n{task_list_text}\n\n【ユーザーの返信】\n{t}',
        TOOLS_SET_DUE_BATCH, 400,
    )
    delete_state(key)
    name, args, _ = extract_function_call(res)
    assignments = (args or {}).get('assignments') if name == 'set_due_dates' else []
    assignments = assignments or []
    if not assignments:
        if not looks_like_date_reply(t):
            return None
        return '反映できませんでした。個別に「〇〇の期限を6/30にして」のように送ってください。'

    applied = []
    for a in assignments:
        atitle = str(a.get('task_title') or '').strip()
        adue = str(a.get('due') or '').strip()
        if not atitle or not adue:
            continue
        match = next((pt for pt in pending['tasks'] if pt['title'] == atitle), None)
        if not match:
            continue
        ok = patch_supabase('tasks', f"id=eq.{quote(match['id'])}", {
            'due': adue, 'due_time': a.get('due_time') or None, 'updated_at': config.now_iso(),
        })
        if ok is not None:
            time_part = f" {a['due_time']}" if a.get('due_time') else ''
            applied.append(f'・{atitle}（{config.jp(adue)}{time_part}）')
    if not applied:
        return '反映できませんでした。個別に「〇〇の期限を6/30にして」のように送ってください。'
    return '✅ 期限を設定しました\n' + '\n'.join(applied)


def handle_pending_time_reply(user_id, text):
    """pending中の時刻確認（近日中のタスク追加時／期限が近いのに時刻未設定のタスクの棚卸し）への返信を処理。該当なしはNoneを返す。"""
    key = f'PENDING_TIME_{user_id}'
    pending = get_state(key)
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 60000 > 360:
        delete_state(key)
        return None

    t = text.strip()
    if re.match(r'^(不要|なし|未定|スキップ|やめて|あとで)[。.!！]?$', t):
        delete_state(key)
        return '了解です、時間は未設定のままにします。'

    if pending.get('mode') == 'single':
        time_val = parse_time_only(t)
        if not time_val:
            if not looks_like_time_reply(t):
                delete_state(key)
                return None
            return '時間を認識できませんでした。「15:00」「15時」のように送ってください（設定しない場合は「なし」）。'
        updated = patch_supabase('tasks', f"id=eq.{quote(pending['taskId'])}", {
            'due_time': time_val, 'updated_at': config.now_iso(),
        })
        delete_state(key)
        if updated is None:
            return f"⚠️「{pending['title']}」の時間設定に失敗しました。"
        return f"✅「{pending['title']}」の時間を {time_val} に設定しました。"

    # batchモード：複数タスクぶんの時刻をまとめてAIに割り振らせる
    task_list_text = '\n'.join(f"・{pt['title']}" for pt in pending['tasks'])
    res = call_gemini_with_tools(
        'ユーザーの返信文から、下記タスク一覧のうちどのタスクに何時のリマインドを割り当てたいか読み取り、set_timesツールで返してください。'
        '返信で触れられていないタスクは含めないでください。',
        f'【タスク一覧】\n{task_list_text}\n\n【ユーザーの返信】\n{t}',
        TOOLS_SET_TIME_BATCH, 300,
    )
    delete_state(key)
    name, args, _ = extract_function_call(res)
    assignments = (args or {}).get('assignments') if name == 'set_times' else []
    assignments = assignments or []
    if not assignments:
        if not looks_like_time_reply(t):
            return None
        return '反映できませんでした。個別に「〇〇の時間を15時にして」のように送ってください。'

    applied = []
    for a in assignments:
        atitle = str(a.get('task_title') or '').strip()
        atime = str(a.get('due_time') or '').strip()
        if not atitle or not atime:
            continue
        match = next((pt for pt in pending['tasks'] if pt['title'] == atitle), None)
        if not match:
            continue
        ok = patch_supabase('tasks', f"id=eq.{quote(match['id'])}", {'due_time': atime, 'updated_at': config.now_iso()})
        if ok is not None:
            applied.append(f'・{atitle}（{atime}）')
    if not applied:
        return '反映できませんでした。個別に「〇〇の時間を15時にして」のように送ってください。'
    return '✅ 時間を設定しました\n' + '\n'.join(applied)
