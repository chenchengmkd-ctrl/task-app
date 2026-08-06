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
    # 「今日は〜」のように助詞が続く場合、助詞ごと取り除かないとタイトルが「は〜」になってしまう
    m = re.search(r'(今日|明日|明後日)(?:には|までに|まで|は|に|の)?', title)
    if m:
        from datetime import timedelta
        d = config.now_jst() + timedelta(days=rel_days[m.group(1)])
        due = d.strftime('%Y-%m-%d')
        title = title.replace(m.group(0), '', 1)
    else:
        m = re.search(r'(\d{1,2})/(\d{1,2})(?:には|までに|まで|は|に|の)?', title)
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
        f'今日の日付は{config.today_iso()}（YYYY-MM-DD）です。渡された文章から、このタスク自体の締め切り・期限を表す日付表現を1つだけ探し、'
        'YYYY-MM-DD形式の1行だけで返してください。'
        '文章中には「6月30日時点の在庫」のように、タスクが扱う対象データの時点を表すだけの日付が含まれることがありますが、'
        'それらはタスクの期限ではないので無視してください。「今週中」「来週」「月末までに」のように、'
        'このタスク自体をいつまでに終わらせたいかを示す表現があれば、そちらを優先してください。'
        '期限表現が無い・特定できない場合は「なし」とだけ返してください。説明は不要です。',
        text, 20,
    )
    if not reply:
        return None
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', reply.strip())
    if not m:
        return None
    return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'


# ============ タスク追加・一覧 ============
def add_line(user_id, text, estimate=''):
    """タスクをその場でSupabaseに追加する。
    予定を決めている最中（PENDING_PLAN）なら、期限をその日にして候補にも足す。
    estimate は「30分」のような見積り時間（AIが推定して渡す）。優先順位の判断に使う。
    """
    from . import planning as planning_mod

    parsed = extract_date_time(text)
    title = parsed['title']
    due = parsed['due']
    due_time = parsed['due_time']

    plan_pending = planning_mod.get_pending_plan(user_id)
    plan_date = plan_pending.get('date') if plan_pending else ''
    if plan_pending and not due:
        due = plan_date

    if not due and re.search(r'(来週|再来週|今週中|今月中|来月|月末|週末|月曜|火曜|水曜|木曜|金曜|土曜|日曜)', title):
        ai_due = ai_parse_date(title)
        if ai_due:
            due = ai_due

    if len(title) > 20 or re.search(r'\n|・|^[0-9]+[.、)]', title):
        cleaned = cleanup_task_text(title)
        if cleaned:
            title = cleaned

    estimate = str(estimate or '').strip()
    row_id = config.new_id()
    row = {
        'id': row_id, 'title': title, 'status': 'todo', 'priority': 'mid',
        'due': due or None, 'due_time': due_time or None,
        'estimate': estimate, 'recurrence': 'none', 'note': '', 'tags': [],
        'done': False, 'deleted': False, 'from_line': True,
        'updated_at': config.now_iso(),
    }
    if not post_supabase('tasks', [row]):
        return '⚠️ タスクの追加に失敗しました。時間をおいて再度お試しください。'

    cnt = len(get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id'))
    msg = f'✅ 追加しました\n「{title}」'
    if estimate:
        msg += f'（目安 {estimate}）'

    # 予定を決めている最中なら、期限の質問はせずに候補へ追加して番号を返す
    if plan_pending:
        num = planning_mod.add_plan_candidate(user_id, row_id, title)
        msg += f'\n📅 {config.jp(due)}' + (f' {due_time}' if due_time else '')
        if num:
            msg += (f'\n\n🌙 候補に追加しました（{num}番）'
                    f'\n予定に入れるなら、{num} も一緒に番号で送ってください。')
        return msg + f'\n\n未完了: {cnt}件'

    # すぐ終わるものは「今すぐ／今日中」と促す（ダブルマトリックスの2段階目。詳細は priority.py）
    from . import priority as priority_mod
    minutes = priority_mod.parse_estimate_minutes(estimate)
    guideline = priority_mod.deadline_guideline(minutes)

    if due:
        msg += f'\n📅 {config.jp(due)}' + (f' {due_time}' if due_time else '')
        due_date = config.parse_date(due)
        if not due_time and due_date and config.day_diff(config.now_jst(), due_date) <= 2:
            set_state(f'PENDING_TIME_{user_id}', {
                'mode': 'single', 'taskId': row_id, 'title': title, 'createdAt': config.now_ms(),
            })
            msg += '\n\n⏰ 時間は何時ですか？（例：15:00、15時、未定なら「なし」）'
    elif minutes is not None and minutes <= priority_mod.DO_NOW_MINUTES:
        # 10分以内で終わるものは、期限を聞くより先に片づけたほうが早い
        msg += ('\n\n⚡ これは今すぐ片づけてしまうのが一番早いです。'
                '\n終わったら「完了」、後回しにするなら期限を送ってください（例：明日、なし）。')
        set_state(f'PENDING_DUE_{user_id}', {
            'mode': 'single', 'tasks': [{'id': row_id, 'title': title}], 'createdAt': config.now_ms(),
        })
    else:
        set_state(f'PENDING_DUE_{user_id}', {
            'mode': 'single', 'tasks': [{'id': row_id, 'title': title}], 'createdAt': config.now_ms(),
        })
        msg += '\n\n📅 期限はいつにしますか？（例：6/30、今日、なし）'
        if guideline:
            msg += f'\n（{estimate}なら {guideline} が目安です）'
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
        msg += '\n番号だけで操作できます（例：1削除、5ペンディング、3完了）。\n「1削除 5ペンディング」のようにまとめてもOKです。'
    msg = msg.strip()

    if not rec:
        return msg

    rec_msg = recurring_mod.recurring_summary_message(rec)
    return [msg, rec_msg] if app else rec_msg


LAST_LIST_VALID_HOURS = 12


def resolve_numbered_task(user_id, num):
    """直前の「一覧」で表示した番号から、実際のタスク（id・title）を引き当てる。該当なし/期限切れはNone。"""
    data = get_state(f'LAST_LIST_{user_id}')
    if not data:
        return None
    if (config.now_ms() - data.get('createdAt', 0)) / 3600000 > LAST_LIST_VALID_HOURS:
        return None
    return next((it for it in (data.get('items') or []) if it['num'] == num), None)


def set_last_list(user_id, tasks):
    """番号を1から振り直す（そのメッセージだけで完結させたいとき用）。→ [{'num','id','title'}]"""
    numbered = [{'num': i + 1, 'id': t['id'], 'title': t['title']} for i, t in enumerate(tasks)]
    if user_id:
        set_state(f'LAST_LIST_{user_id}', {'items': numbered, 'createdAt': config.now_ms()})
    return numbered


def append_to_last_list(user_id, tasks):
    """直前の一覧の番号を引き継いで、渡したタスクに番号を振る。
    すでに番号があるタスクはその番号のまま、無いものだけ続き番号を足す。
    → [{'num': n, 'id': .., 'title': ..}]
    """
    if not user_id:
        return [{'num': i + 1, 'id': t['id'], 'title': t['title']} for i, t in enumerate(tasks)]
    data = get_state(f'LAST_LIST_{user_id}') or {}
    created = data.get('createdAt') or 0
    if (config.now_ms() - created) / 3600000 > LAST_LIST_VALID_HOURS:
        data, created = {}, config.now_ms()
    items = list(data.get('items') or [])
    by_id = {it['id']: it['num'] for it in items}
    max_num = max((it['num'] for it in items), default=0)

    out = []
    for t in tasks:
        n = by_id.get(t['id'])
        if n is None:
            max_num += 1
            n = max_num
            items.append({'num': n, 'id': t['id'], 'title': t['title']})
        out.append({'num': n, 'id': t['id'], 'title': t['title']})
    set_state(f'LAST_LIST_{user_id}', {'items': items, 'createdAt': created or config.now_ms()})
    return out


# 番号だけで操作するときに使える言葉（「1削除」「5ペンディング」など）
NUMBERED_ACTIONS = {
    '削除': 'delete', '消す': 'delete', '消して': 'delete',
    '完了': 'done', 'done': 'done',
    'ペンディング': 'pending', 'ペンデイング': 'pending',
    '着手中': 'doing', '着手': 'doing',
    '対応待ち': 'waiting', '待ち': 'waiting',
    '未着手': 'todo',
}
_ACTION_LABEL = {'delete': '削除', 'done': '完了', 'pending': 'ペンディング',
                 'doing': '着手中', 'waiting': '対応待ち', 'todo': '未着手'}
_ZEN2HAN = str.maketrans('０１２３４５６７８９', '0123456789')


# 「1明日」「2を7/30」「1 15時」「3明日10時」のように、番号＋日付/時刻で期限・時間を設定する
_DATE_WORD = r'今日|明日|明後日|来週|再来週|今週末|週末|来月|月末'
_DATE_NUM = r'\d{1,2}/\d{1,2}'
_TIME_EXPR = r'\d{1,2}時(?:\d{1,2}分)?|\d{1,2}:\d{2}'
_NUM_SPEC = re.compile(
    r'(?P<num>\d+)'
    r'(?P<sep>\s*(?:番目?)?\s*(?:を|の|は|に)?\s*)'
    rf'(?P<when>{_DATE_WORD}|{_DATE_NUM})?'
    r'\s*(?:の|に|は|、)?\s*'
    rf'(?P<time>{_TIME_EXPR})?'
)


def _iter_num_specs(text):
    """「番号＋日付/時刻」の指定を順に取り出す。→ (match, 番号, 日付表現, 時刻表現)"""
    for m in _NUM_SPEC.finditer(text.translate(_ZEN2HAN)):
        when = m.group('when') or ''
        time_s = m.group('time') or ''
        if not when and not time_s:
            continue
        # 「15:00」を「1番を5:00」と読み違えないよう、数字で始まる指定は
        # 区切り（空白・助詞・「番」）が入っているときだけ番号指定として扱う。
        if (when or time_s)[0].isdigit() and m.group('sep') == '':
            continue
        yield m, int(m.group('num')), when, time_s


def _accepted_num_specs(text):
    """番号指定として受け付けてよいものだけを返す。
    「会議 1 15時」のような普通のタスク文をコマンドと誤解しないよう、
    先頭が番号で始まり、かつ指定部分と定型的な言い回しを除いた残りがほとんど無いことを条件にする。
    """
    specs = list(_iter_num_specs(text))
    if not specs or specs[0][0].start() > 1:
        return []
    rest = text.translate(_ZEN2HAN)
    for m, _, _, _ in reversed(specs):
        rest = rest[:m.start()] + rest[m.end():]
    rest = re.sub(r'(にしてください|にして|でお願いします|でお願い|お願いします|おねがいします|にリスケ|リスケ|'
                  r'に変更|変更|に設定|設定|時間|時刻|期限|から|へ|に|、|,|\s)', '', rest)
    return [] if len(rest) > 3 else specs


def looks_like_numbered_due_time(user_id, text):
    """「1 15時」「2を7/30」のような番号指定かどうか（番号が直前の一覧で実在する場合のみTrue）。
    期限・時刻の確認待ちへの返信と取り違えないための判定。
    """
    return any(resolve_numbered_task(user_id, num) for _, num, _, _ in _accepted_num_specs(text))


def _prune_pending_time(user_id, task_ids):
    """番号指定で時間が入ったぶんを、時刻確認待ちのリストから外す（全部埋まったら確認自体を終了）。"""
    key = f'PENDING_TIME_{user_id}'
    pending = get_state(key)
    if not pending or pending.get('mode') != 'batch':
        return
    remain = [t for t in (pending.get('tasks') or []) if t['id'] not in task_ids]
    if not remain:
        delete_state(key)
    elif len(remain) != len(pending.get('tasks') or []):
        set_state(key, {**pending, 'tasks': remain})


def _current_due(task_id):
    """カレンダーとの重なりを見るために、そのタスクの今の期限を取り出す。"""
    rows = get_supabase('tasks', f'id=eq.{quote(task_id)}&select=due')
    return (rows[0].get('due') or '') if rows else ''


def handle_numbered_due_time(user_id, text):
    """番号＋日付/時刻の指定を、期限・時間の設定として処理する。該当なしはNone。"""
    specs = _accepted_num_specs(text)
    if not specs:
        return None

    applied, missing, timed_ids = [], [], []
    for _, num, when, time_s in specs:
        target = resolve_numbered_task(user_id, num)
        if not target:
            missing.append(num)
            continue
        body = {'updated_at': config.now_iso()}
        due = ''
        if when:
            due = extract_date_time(when)['due'] or ai_parse_date(when) or ''
            if due:
                body['due'] = due
        time_val = parse_time_only(time_s) if time_s else ''
        if time_val:
            body['due_time'] = time_val
        if len(body) == 1:
            continue
        ok = patch_supabase('tasks', f"id=eq.{quote(target['id'])}", body)
        if ok is None:
            continue
        if time_val:
            timed_ids.append(target['id'])
        parts = [config.jp(due)] if due else []
        if time_val:
            parts.append(time_val)
        line = f"{num}. {target['title']} → {' '.join(parts)}"
        # カレンダーに予定が入っている時間帯なら、その場で知らせて空き時間を提案する
        if time_val:
            from . import gcal
            check_date = due or _current_due(target['id'])
            busy = gcal.conflict_at(check_date, time_val) if check_date else None
            if busy:
                alt = gcal.suggest_time(check_date)
                line += f'\n　⚠️ この時間は「{busy}」と重なっています'
                if alt:
                    line += f'（空いているのは {alt} ごろ）'
        applied.append(line)

    if timed_ids:
        _prune_pending_time(user_id, timed_ids)

    lines = []
    if applied:
        head = '⏰ 期限・時間を設定しました' if timed_ids else '📅 期限を変更しました'
        lines.append(head + '\n' + '\n'.join(applied))
    if missing:
        nums = '、'.join(str(n) for n in missing)
        lines.append(f'⚠️ {nums} 番に対応するタスクが見つかりませんでした。「一覧」で番号を確認してください。')
    return '\n\n'.join(lines) if lines else None


def looks_like_numbered_action(text):
    """「1完了」「2削除」のように番号＋操作語の指定かどうか。期限確認への返信と取り違えないための判定。"""
    words = '|'.join(sorted(NUMBERED_ACTIONS, key=len, reverse=True))
    return bool(re.search(r'\d+\s*(?:番目?)?\s*(?:を|の|に|で|は)?\s*(' + words + r')',
                          text.translate(_ZEN2HAN)))


def handle_numbered_actions(user_id, text):
    """「1削除」「5ペンディング」「1削除 5ペンディング」のように、番号＋操作をまとめて処理する。
    AIを使わずその場で確定させる。該当する指定が無ければNoneを返して通常のルーティングへ。
    """
    t = text.translate(_ZEN2HAN)
    words = '|'.join(sorted(NUMBERED_ACTIONS, key=len, reverse=True))
    pattern = r'(\d+)\s*(?:番目?)?\s*(?:を|の|に|で|は)?\s*(' + words + r')'
    pairs = re.findall(pattern, t)
    if not pairs:
        return handle_numbered_due_time(user_id, t)

    # 「3完了報告書を作る」のような普通のタスク文を誤って操作コマンドと解釈しないよう、
    # 指定部分と定型的な言い回しを取り除いた残りがほとんど無いときだけコマンドとして扱う。
    rest = re.sub(pattern, '', t)
    rest = re.sub(r'(でお願いします|でお願い|にしてください|しといて|しておいて|してください|お願いします|'
                  r'おねがいします|にして|して|に変更|変更|です|ます|、|,|\s)', '', rest)
    if len(rest) > 3:
        return None

    applied, missing, failed = [], [], []
    for num_str, word in pairs:
        num = int(num_str)
        target = resolve_numbered_task(user_id, num)
        if not target:
            missing.append(num)
            continue
        action = NUMBERED_ACTIONS[word]
        if action == 'delete':
            body = {'deleted': True, 'updated_at': config.now_iso()}
        elif action == 'done':
            body = {'done': True, 'completed_at': config.now_iso(), 'updated_at': config.now_iso()}
        else:
            body = {'done': False, 'status': action, 'completed_at': None, 'updated_at': config.now_iso()}
        ok = patch_supabase('tasks', f"id=eq.{quote(target['id'])}", body)
        if ok is None:
            failed.append(f"{num}. {target['title']}")
        else:
            applied.append(f"{num}. {target['title']} → {_ACTION_LABEL[action]}")

    lines = []
    if applied:
        lines.append('✅ 反映しました\n' + '\n'.join(applied))
    if failed:
        lines.append('⚠️ 反映に失敗しました\n' + '\n'.join(failed))
    if missing:
        nums = '、'.join(str(n) for n in missing)
        lines.append(f'⚠️ {nums} 番に対応するタスクが見つかりませんでした。「一覧」で番号を確認してください。')
    return '\n\n'.join(lines)


# ============ リマインド ============
def get_due_from_app():
    """アプリの期限ありの未完了タスクを取得。"""
    rows = get_supabase('tasks', 'done=eq.false&deleted=eq.false&due=not.is.null&select=id,title,status,due,due_time')
    out = []
    for r in rows:
        due = config.parse_date(r.get('due'))
        if not r.get('title') or not due:
            continue
        out.append({
            'id': r['id'], 'title': str(r['title']), 'due': due,
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


def build_reminder(user_id=None):
    """通知メッセージを組み立て（対象が無ければ空文字）。
    通常タスク: 期限が3日以内（超過含む）を、進捗状況ごとに表示（時刻が設定されていれば時刻も表示）。
    番号を振り、user_idが分かる場合は「1完了」のようにそのまま操作できるよう記憶しておく。
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
    numbered = []
    n = 0
    for st in config.STATUS_ORDER:
        arr = sorted([t for t in near if t['status'] == st], key=lambda t: t['d'])
        if not arr:
            continue
        lines = []
        for t in arr:
            n += 1
            numbered.append({'num': n, 'id': t['id'], 'title': t['title']})
            lines.append(f"{n}. {t['title']}（{due_tag(t['d'], t['due'], t['due_time'])}）")
        msg += f'\n{config.STATUS_ICON[st]} {st}\n' + '\n'.join(lines) + '\n'

    if numbered and user_id:
        set_state(f'LAST_LIST_{user_id}', {'items': numbered, 'createdAt': config.now_ms()})
        msg += '\n番号だけで操作できます（例：1完了、2ペンディング、3削除）。'
    return msg.strip()


def get_no_time_soon_tasks():
    """期限が3日以内（超過含む）なのに時刻が未設定のタスクを集める。"""
    today = config.midnight(config.now_jst())
    rows = get_supabase('tasks', 'done=eq.false&deleted=eq.false&due=not.is.null&due_time=is.null&select=id,title,due')
    out = []
    for r in rows:
        d = config.parse_date(r.get('due'))
        if d and config.day_diff(today, d) <= config.REC_NEAR_DAYS:
            out.append({'id': r['id'], 'title': r['title'], 'due': r.get('due') or ''})
    return out


def build_no_time_message(user_id, no_time):
    """期限が近いのに時刻未設定のタスクを、リマインド本文と同じ番号つきで並べる。
    番号で「1 15時」と返すだけで時間を設定できるようにする。
    """
    numbered = append_to_last_list(user_id, no_time)
    due_by_id = {t['id']: t.get('due') or '' for t in no_time}
    lines = '\n'.join(
        f"{it['num']}. {it['title']}" + (f"（{config.jp(due_by_id[it['id']])}）" if due_by_id.get(it['id']) else '')
        for it in numbered
    )
    msg = ('⏰ 期限が近いのに時間未設定のタスク\n' + lines +
           '\n\n番号と時間を送れば設定できます（例：1 15時／2は10時30分）。'
           '\n日付もまとめて変えられます（例：3 明日10時）。不要なら「なし」。')
    # カレンダーを連携していれば、今日の空き時間をヒントとして添える
    from . import gcal
    free = gcal.free_summary(config.today_iso(), from_now=True)
    if free:
        msg += '\n\n' + free
    return msg


def send_reminders(push_text_fn, get_users_fn):
    """毎朝のトリガーから呼ばれる。番号はユーザーごとに記憶する。"""
    from . import gcal
    schedule = gcal.schedule_text(config.today_iso(), '🗓 今日の予定（カレンダー）')
    for uid in get_users_fn():
        msg = build_reminder(uid)
        if msg:
            push_text_fn(uid, msg)
        if schedule:
            push_text_fn(uid, schedule)

    no_time = get_no_time_soon_tasks()
    if not no_time:
        return
    for uid in get_users_fn():
        if not get_state(f'PENDING_TIME_{uid}'):
            set_state(f'PENDING_TIME_{uid}', {'mode': 'batch', 'tasks': no_time, 'createdAt': config.now_ms()})
        push_text_fn(uid, build_no_time_message(uid, no_time))


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
    # 「1完了」「2を7/30」のような番号指定、「1,3,5」のような予定の選択は、そちらの処理に譲る
    from . import planning as planning_mod
    if (looks_like_numbered_action(t) or looks_like_numbered_due_time(user_id, t)
            or planning_mod.looks_like_plan_reply(user_id, t)):
        return None
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
            # 「今週金曜日」「来週」「月末」のような曖昧な表現は、正規表現では拾えないのでAIに判定させる
            ai_due = ai_parse_date(t)
            if not ai_due:
                return '日付を認識できませんでした。「6/30」「今日」「今週金曜」のように送ってください（設定しない場合は「なし」）。'
            parsed = {'due': ai_due, 'due_time': parsed['due_time']}
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
    # 「1完了」「1 15時」のような番号指定、「1,3,5」のような予定の選択は、そちらの処理に譲る
    from . import planning as planning_mod
    if (looks_like_numbered_action(t) or looks_like_numbered_due_time(user_id, t)
            or planning_mod.looks_like_plan_reply(user_id, t)):
        return None
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
