"""AIコーチ本体：対話・タスク文脈の構築・各ツールのhandle_*・保留状態（サブタスク提案／優先順位見直し）の管理。
Code.gs の askAgent/buildAgentContext/各handle*関数に相当。
"""
import re
from datetime import timedelta
from urllib.parse import quote

from . import config
from . import tasks as tasks_mod
from . import recurring as recurring_mod
from . import materials as materials_mod
from . import daily_log as daily_log_mod
from .gemini_client import call_gemini, call_gemini_with_tools, extract_function_call, AGENT_TOOLS, TOOLS_REPRIORITIZE, FORCE_ANY_TOOL
from .supabase_client import get_supabase, post_supabase, patch_supabase, get_state, set_state, delete_state


# ============ タスク状況・文脈の構築 ============
def build_agent_context():
    """タスク状況（Supabase）＋定期タスク＋直近の完了実績＋過去の会話をAI用にまとめる。"""
    from . import priority as priority_mod

    tasks = get_supabase(
        'tasks',
        'done=eq.false&deleted=eq.false&select=title,status,priority,due,estimate,updated_at&order=updated_at.asc',
    )
    now = config.now_jst()
    today_iso = config.today_iso()

    # ダブルマトリックスの順に並べて渡す（AIが優先順位を語るときの土台をこちらで作っておく）
    tasks = sorted(tasks, key=lambda t: priority_mod.sort_key(t, today_iso))
    task_lines = []
    for t in tasks:
        updated = config.parse_timestamp(t.get('updated_at'))
        days = int((now - updated).total_seconds() // 86400) if updated else 0
        due = f" 期限:{t['due']}" if t.get('due') else ''
        status = config.STATUS_LABEL_JP.get(t.get('status'), t.get('status'))
        est = f" 目安:{t['estimate']}" if t.get('estimate') else ' 目安:未設定'
        minutes = priority_mod.parse_estimate_minutes(t.get('estimate'))
        rank = priority_mod.double_matrix_rank(
            t.get('priority') or 'low', priority_mod.urgency_high(t.get('due'), today_iso), minutes)
        task_lines.append(
            f"・{t['title']}（状態:{status}{due} 優先度:{t['priority']}{est} 最終更新:{days}日前"
            f" ／ダブルマトリックス総合{rank}位:{priority_mod.rank_label(rank)}）"
        )

    rec = recurring_mod.get_recurring()
    rec_lines = []
    for r in rec:
        next_part = f"・次回{config.jp2(r['next'])}" if r['next'] else ''
        rec_lines.append(f"・{r['title']}（{recurring_mod.rec_desc(r)}{next_part}）")

    logs = get_supabase('daily_logs', 'select=log_date,content&order=log_date.desc&limit=5')
    log_lines = [f"・{l['log_date']}：{(str(l['content'])[:80] if l.get('content') else '（内容なし）')}" for l in logs]

    since14 = (now - timedelta(days=14)).strftime('%Y-%m-%d')
    done_tasks = get_supabase(
        'tasks',
        f'done=eq.true&deleted=eq.false&updated_at=gte.{since14}&select=title,updated_at&order=updated_at.desc&limit=15',
    )
    done_lines = []
    for t in done_tasks:
        d = config.parse_timestamp(t.get('updated_at'))
        done_lines.append(f"・{t['title']}（{config.jp2(d) if d else ''}完了）")

    convos = get_supabase('ai_log', 'select=user_text,ai_reply,created_at&order=created_at.desc&limit=6')
    convos = list(reversed(convos))
    convo_lines = []
    for c in convos:
        d = config.parse_timestamp(c.get('created_at'))
        convo_lines.append(
            f"・{config.jp2(d) if d else ''} ユーザー「{str(c.get('user_text', ''))[:60]}」→ コーチ「{str(c.get('ai_reply', ''))[:80]}」"
        )

    materials = get_supabase('materials', 'select=title,summary&order=created_at.desc&limit=10')
    material_lines = [f"・{m['title']}：{str(m.get('summary', ''))[:150]}" for m in materials]

    def section(title, lines):
        return ['', f'【{title}】', '\n'.join(lines) if lines else 'なし']

    parts = [priority_mod.DOUBLE_MATRIX_RULE, '',
             '【未完了タスク一覧】（すでにダブルマトリックス順に並べてある）',
             '\n'.join(task_lines) or 'なし']
    # Googleカレンダーを連携していれば、今日・明日の予定と空き時間も渡す（生活リズムを踏まえた助言のため）
    from . import gcal
    if gcal.is_enabled():
        cal_lines = []
        for label, iso in (('今日', config.today_iso()),
                           ('明日', config.iso_of_date(now + timedelta(days=1)))):
            text = gcal.schedule_text(iso, f'{label}（{config.jp(iso)}）')
            if text:
                cal_lines.append(text)
        parts += section('カレンダーの予定と空き時間（この時間帯は手が空かない前提で助言すること）',
                         ['\n'.join(cal_lines)] if cal_lines else [])
    parts += section('直近14日で完了したタスク', done_lines)
    parts += section('定期タスク', rec_lines)
    parts += section('直近の振り返りログ', log_lines)
    parts += section('直近のAIコーチとの会話', convo_lines)
    parts += section('ユーザーが登録した参考資料', material_lines)
    return '\n'.join(parts)


def log_agent_interaction(user_id, user_text, ai_reply):
    """AIコーチとのやり取りを記録する（次回以降の文脈把握のため。失敗しても致命的ではない）。"""
    post_supabase('ai_log', [{
        'user_id': user_id,
        'user_text': str(user_text)[:500],
        'ai_reply': str(ai_reply)[:1000],
        'created_at': config.now_iso(),
    }])


# ============ サブタスク分解の提案 ============
def handle_propose_subtasks(user_id, input_args, intro):
    a = input_args or {}
    parent_title = str(a.get('parent_task_title') or '').strip()
    subtasks = [str(s).strip() for s in (a.get('subtasks') or []) if str(s).strip()]
    if not parent_title or not subtasks:
        return '⚠️ 分解案の生成に失敗しました。もう一度お試しください。'

    set_state(f'PENDING_SUBTASKS_{user_id}', {
        'parentTitle': parent_title, 'subtasks': subtasks, 'createdAt': config.now_ms(),
    })

    numbered = '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(subtasks))
    return f'🧩「{parent_title}」の分解案\n{numbered}\n\n追加してよければ「はい」と送ってください（5分以内）。'


def add_subtasks_to_app(parent_title, subtasks):
    now = config.now_iso()
    rows = [{
        'id': config.new_id(), 'title': title, 'status': 'todo', 'priority': 'low', 'due': None, 'due_time': None,
        'estimate': '', 'recurrence': 'none', 'note': '', 'tags': [],
        'done': False, 'deleted': False, 'from_line': True, 'updated_at': now,
    } for title in subtasks]
    if not post_supabase('tasks', rows):
        return '⚠️ サブタスクの追加に失敗しました。'
    numbered = '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(subtasks))
    return f'✅「{parent_title}」に{len(subtasks)}件のサブタスクを追加しました\n{numbered}'


def handle_pending_subtask_reply(user_id, text):
    """pending中のサブタスク提案への返信（「はい」「いいえ」）を処理。該当なしはNoneを返す。"""
    key = f'PENDING_SUBTASKS_{user_id}'
    pending = get_state(key)
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 60000 > 5:
        delete_state(key)
        return None

    t = text.strip()
    if re.match(r'^(はい|追加|うん|お願い(します)?|ok|yes)[。.!！]*$', t, re.IGNORECASE):
        delete_state(key)
        return add_subtasks_to_app(pending['parentTitle'], pending['subtasks'])
    if re.match(r'^(いいえ|キャンセル|やめて|no)[。.!！]*$', t, re.IGNORECASE):
        delete_state(key)
        return '🙅 分解案の追加をキャンセルしました。'
    return None


# ============ カレンダーの予定を追加・変更・削除（必ず確認してから実行する） ============
_CAL_PENDING_MINUTES = 5


def _cal_pending_key(user_id):
    return f'PENDING_CALENDAR_{user_id}'


def _ask_calendar_confirm(user_id, action, summary_text, payload, intro=''):
    set_state(_cal_pending_key(user_id), {
        'action': action, 'payload': payload, 'summary': summary_text, 'createdAt': config.now_ms(),
    })
    # intro（AIが付け足すコメント）は、カレンダー確認のような機械的な操作では余計な説教や
    # 誤った既成事実（「今回で最後です」等）を混ぜてしまうことがあるため使わない
    return f'{summary_text}\n\nこの内容でよければ「はい」と送ってください（5分以内）。'


def handle_create_calendar_event(user_id, input_args, intro):
    from . import gcal
    a = input_args or {}
    title = str(a.get('title') or '').strip()
    date = str(a.get('date') or '').strip()
    start = str(a.get('start_time') or '').strip()[:5]
    end = str(a.get('end_time') or '').strip()[:5]
    if not title or not config.parse_date(date):
        return '⚠️ 予定の内容を理解できませんでした。「明日14時から16時 仕込み をカレンダーに入れて」のように送ってください。'
    if not gcal.is_enabled():
        return '⚠️ Googleカレンダーが連携されていません。'

    recurrence = str(a.get('recurrence') or '').strip()
    weekday, monthday = a.get('weekday'), a.get('monthday')
    rrule = gcal.build_rrule(recurrence, weekday, monthday) if recurrence else None
    if recurrence and not rrule:
        return '⚠️ 繰り返しの指定を理解できませんでした。毎週なら曜日、毎月なら日にちも教えてください。'
    reminder = a.get('reminder_minutes')

    when = f'{config.jp(date)} {start}〜{end}' if start else f'{config.jp(date)} 終日'
    text = f'🗓 カレンダーに追加します\n・{title}\n・{when}'
    if rrule:
        text += f'\n・繰り返し：{recurring_mod.rec_desc({"recurrence": recurrence, "weekday": weekday, "monthday": monthday})}'
    if reminder is not None:
        text += f'\n・通知：{int(reminder)}分前'
    busy = gcal.conflict_at(date, start) if start else None
    if busy:
        text += f'\n\n⚠️ この時間は「{busy}」と重なっています'
    return _ask_calendar_confirm(user_id, 'create', text, {
        'title': title, 'date': date, 'start': start, 'end': end, 'rrule': rrule, 'reminder': reminder,
    }, intro)


def handle_update_calendar_event(user_id, input_args, intro):
    from . import gcal
    a = input_args or {}
    keyword = str(a.get('keyword') or '').strip()
    date = str(a.get('date') or '').strip()
    if not keyword or not config.parse_date(date):
        return '⚠️ どの予定を変えるのか分かりませんでした。「明日のバイトを17時からにして」のように送ってください。'
    if not gcal.is_enabled():
        return '⚠️ Googleカレンダーが連携されていません。'

    ev, candidates = gcal.find_event(date, keyword)
    if not ev:
        if not candidates:
            return f'⚠️ {config.jp(date)} に予定が見つかりませんでした。'
        names = '\n'.join(f'・{gcal.event_label(c)}' for c in candidates[:5])
        return f'⚠️ どの予定か特定できませんでした。{config.jp(date)} の予定はこちらです。\n{names}\n\nどれのことか教えてください。'

    new_date = str(a.get('new_date') or '').strip() or date
    new_start = str(a.get('new_start_time') or '').strip()[:5]
    new_end = str(a.get('new_end_time') or '').strip()[:5]
    new_title = str(a.get('new_title') or '').strip()
    reminder = a.get('reminder_minutes')
    if not (new_start or new_end or new_title or new_date != date or reminder is not None):
        return '⚠️ 変更内容が分かりませんでした。「17時からにして」のように、新しい時間を教えてください。'

    after = []
    if new_title:
        after.append(f'名前：{new_title}')
    if new_date != date:
        after.append(f'日付：{config.jp(new_date)}')
    if new_start or new_end:
        after.append(f"時間：{new_start or '（そのまま）'}〜{new_end or '（自動）'}")
    if reminder is not None:
        after.append(f'通知：{int(reminder)}分前')
    text = ('🗓 カレンダーの予定を変更します\n'
            f'・変更前：{config.jp(date)} {gcal.event_label(ev)}\n'
            '・変更後：' + '／'.join(after))
    if ev.get('is_recurring'):
        text += '\n\n（この予定は繰り返しの一部です。この変更は指定した1回分だけに適用され、他の回には影響しません）'
    return _ask_calendar_confirm(user_id, 'update', text, {
        'id': ev['id'], 'date': new_date, 'start': new_start, 'end': new_end, 'title': new_title, 'reminder': reminder,
    }, intro)


def handle_delete_calendar_event(user_id, input_args, intro):
    from . import gcal
    a = input_args or {}
    keyword = str(a.get('keyword') or '').strip()
    date = str(a.get('date') or '').strip()
    if not keyword or not config.parse_date(date):
        return '⚠️ どの予定を消すのか分かりませんでした。「明日の〇〇をキャンセルして」のように送ってください。'
    if not gcal.is_enabled():
        return '⚠️ Googleカレンダーが連携されていません。'

    ev, candidates = gcal.find_event(date, keyword)
    if not ev:
        if not candidates:
            return f'⚠️ {config.jp(date)} に予定が見つかりませんでした。'
        names = '\n'.join(f'・{gcal.event_label(c)}' for c in candidates[:5])
        return f'⚠️ どの予定か特定できませんでした。{config.jp(date)} の予定はこちらです。\n{names}\n\nどれのことか教えてください。'

    text = f'🗓 カレンダーから削除します\n・{config.jp(date)} {gcal.event_label(ev)}'
    if ev.get('is_recurring'):
        text += '\n\n（この予定は繰り返しの一部です。削除されるのは指定した1回分だけで、他の回は残ります）'
    return _ask_calendar_confirm(user_id, 'delete', text, {'id': ev['id'], 'label': gcal.event_label(ev)}, intro)


def handle_pending_calendar_reply(user_id, text):
    """カレンダー操作の確認への返信（「はい」「いいえ」）を処理。該当なしはNoneを返す。"""
    from . import gcal
    key = _cal_pending_key(user_id)
    pending = get_state(key)
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 60000 > _CAL_PENDING_MINUTES:
        delete_state(key)
        return None

    t = text.strip()
    if re.match(r'^(いいえ|キャンセル|やめて|no)[。.!！]*$', t, re.IGNORECASE):
        delete_state(key)
        return '🙅 カレンダーの操作をやめました。'
    if not re.match(r'^(はい|お願い(します)?|うん|ok|yes|登録|変更|削除)[。.!！]*$', t, re.IGNORECASE):
        return None

    delete_state(key)
    p = pending.get('payload') or {}
    action = pending.get('action')
    if action == 'create':
        ok, err = gcal.create_event(p['title'], p['date'], p.get('start') or '', p.get('end') or '',
                                    p.get('rrule'), p.get('reminder'))
        done = f"✅ カレンダーに追加しました\n・{p['title']}（{config.jp(p['date'])}）"
    elif action == 'update':
        ok, err = gcal.update_event(p['id'], p['date'], p.get('start') or '', p.get('end') or '',
                                    p.get('title') or '', p.get('reminder'))
        done = '✅ カレンダーの予定を変更しました。'
    elif action == 'delete':
        ok, err = gcal.delete_event(p['id'])
        done = f"🗑️ カレンダーから削除しました\n・{p.get('label', '')}"
    else:
        return None
    return done if ok else f'⚠️ {err}'


# ============ タスク単体操作 ============
def handle_update_task_status(input_args, intro):
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    new_status = str(a.get('new_status') or '').strip()
    if not title or not new_status:
        return '⚠️ 状態変更の内容を理解できませんでした。'

    matches = tasks_mod.find_task_by_title(title)
    if not matches:
        return f'⚠️「{title}」に一致するタスクが見つかりませんでした。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。'

    body = {'updated_at': config.now_iso()}
    if new_status == 'done':
        body['done'] = True
        body['completed_at'] = config.now_iso()
    else:
        body['done'] = False
        body['status'] = new_status
        body['completed_at'] = None

    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", body)
    if updated is None:
        return f'⚠️「{title}」の更新に失敗しました。'

    label = '完了' if new_status == 'done' else config.STATUS_LABEL_JP.get(new_status, new_status)
    return f'✅「{title}」を{label}に変更しました。'


def handle_delete_task(input_args, intro):
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    if not title:
        return '⚠️ 削除対象を理解できませんでした。'

    matches = tasks_mod.find_task_by_title(title)
    if not matches:
        return f'⚠️「{title}」に一致するタスクが見つかりませんでした。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致するタスクが複数あります。アプリ側で確認・削除してください。'

    # LINEからの削除はゴミ箱へ移すだけ（取り消せる）。完全削除はアプリの「ゴミ箱」タブから行う
    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", {'deleted': True, 'updated_at': config.now_iso()})
    if updated is None:
        return f'⚠️「{title}」の削除に失敗しました。'

    return f'🗑️「{title}」をゴミ箱に移しました。\n（アプリの「🗑 ゴミ箱」タブから元に戻せます）'


def handle_update_task_priority(input_args, intro):
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    new_priority = str(a.get('new_priority') or '').strip()
    if not title or new_priority not in ('high', 'low'):
        return '⚠️ 優先度変更の内容を理解できませんでした。'

    matches = tasks_mod.find_task_by_title(title)
    if not matches:
        return f'⚠️「{title}」に一致するタスクが見つかりませんでした。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。'

    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", {'priority': new_priority, 'updated_at': config.now_iso()})
    if updated is None:
        return f'⚠️「{title}」の更新に失敗しました。'

    label = {'high': '高', 'low': '低'}[new_priority]
    return f'✅「{title}」の優先度を{label}に変更しました。'


def handle_update_task_due(input_args, intro):
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    due = str(a.get('due') or '').strip()
    due_time = str(a.get('due_time') or '').strip()
    if not title or not due:
        return '⚠️ 期限変更の内容を理解できませんでした。'

    matches = tasks_mod.find_task_by_title(title)
    if not matches:
        return f'⚠️「{title}」に一致するタスクが見つかりませんでした。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。'

    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", {
        'due': due, 'due_time': due_time or None, 'updated_at': config.now_iso(),
    })
    if updated is None:
        return f'⚠️「{title}」の更新に失敗しました。'

    time_part = f' {due_time}' if due_time else ''
    return f'✅「{title}」の期限を {config.jp(due)}{time_part} に変更しました。'


def handle_update_task_title(input_args, intro):
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    new_title = str(a.get('new_title') or '').strip()
    if not title or not new_title:
        return '⚠️ 書き換え内容を理解できませんでした。'

    matches = tasks_mod.find_task_by_title(title)
    if not matches:
        return f'⚠️「{title}」に一致するタスクが見つかりませんでした。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。'

    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", {'title': new_title, 'updated_at': config.now_iso()})
    if updated is None:
        return f'⚠️「{title}」の更新に失敗しました。'

    return f'✅「{title}」を「{new_title}」に書き換えました。'


def handle_record_reflection(input_args, intro):
    """AIが「これは振り返り報告だ」と判断した発言を、そのままdaily_logsに記録する。"""
    content = str((input_args or {}).get('content') or '').strip()
    if not content:
        return '⚠️ 振り返りの内容を理解できませんでした。'
    return daily_log_mod.add_daily_log(content)


# ============ 優先順位の見直し ============
def propose_reprioritization(user_id):
    """未完了タスク全体の優先順位見直し案を作り、確認を求める（実際の適用は「はい」の返信を待つ）。"""
    tasks = get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title,priority')
    if not tasks:
        return '未完了タスクがありません。'
    priority_map = {t['title']: t['priority'] for t in tasks}

    context = build_agent_context()
    res = call_gemini_with_tools(
        config.AGENT_PERSONA,
        '以下は現在のタスク状況です。優先度を変えたほうがよいタスクだけをreprioritize_tasksツールで提案してください。'
        '判断は必ず冒頭の「優先順位の判断基準：ダブルマトリックス」に従ってください。'
        'とくに、期限が近いだけで重要度の低いタスク（結果への影響が小さいもの）に高い優先度をつけないこと。'
        '逆に、期限が先でも結果への影響が大きいタスクは優先度を上げること。'
        '変更不要なタスクは含めないでください。\n\n' + context,
        TOOLS_REPRIORITIZE, 500,
    )
    name, args, _ = extract_function_call(res)
    assignments = (args or {}).get('assignments') if name == 'reprioritize_tasks' else []
    assignments = assignments or []
    valid = [
        a for a in assignments
        if a.get('task_title') in priority_map and priority_map[a['task_title']] != a.get('priority')
    ]
    if not valid:
        return '🔀 今のままで問題なさそうです。優先順位の変更提案はありません。'

    set_state(f'PENDING_REPRIORITIZE_{user_id}', {'assignments': valid, 'createdAt': config.now_ms()})

    label = {'high': '高', 'low': '低'}
    lines = [f"・{a['task_title']}：{label[priority_map[a['task_title']]]}→{label[a['priority']]}" for a in valid]
    return '🔀 優先順位の見直し案\n' + '\n'.join(lines) + '\n\n適用してよければ「はい」と送ってください。'


def handle_pending_reprioritize_reply(user_id, text):
    """pending中の優先順位見直し案への返信（「はい」「いいえ」）を処理。該当なしはNoneを返す。"""
    key = f'PENDING_REPRIORITIZE_{user_id}'
    pending = get_state(key)
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 60000 > 360:
        delete_state(key)
        return None

    t = text.strip()
    if re.match(r'^(はい|適用|うん|お願い(します)?|ok|yes)[。.!！]*$', t, re.IGNORECASE):
        delete_state(key)
        tasks = get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title')
        id_by_title = {tk['title']: tk['id'] for tk in tasks}
        applied = []
        for a in pending['assignments']:
            tid = id_by_title.get(a['task_title'])
            if not tid:
                continue
            ok = patch_supabase('tasks', f'id=eq.{quote(tid)}', {'priority': a['priority'], 'updated_at': config.now_iso()})
            if ok is not None:
                applied.append(f"・{a['task_title']}")
        if not applied:
            return '⚠️ 適用できませんでした。タスクの状態が変わっている可能性があります。'
        return '✅ 優先順位を更新しました\n' + '\n'.join(applied)
    if re.match(r'^(いいえ|キャンセル|やめて|no)[。.!！]*$', t, re.IGNORECASE):
        delete_state(key)
        return '🙅 優先順位の変更をキャンセルしました。'
    return None


# ============ 対話本体 ============
def ask_agent(user_id, user_text):
    """ユーザーからの自由な相談にタスク状況を踏まえて回答（タスク分解・状態変更等も可能）。"""
    context = build_agent_context()
    prompt = (
        '以下は現在のタスク状況です。この状況を踏まえて、ユーザーからの次の相談に答えてください。\n'
        '「〜を分解して」のような依頼にはpropose_subtasksツールで提案し、'
        '「〜を完了にして」のように状態変更が明確に依頼された場合はupdate_task_statusツールを、'
        '「〜を削除して」のように削除が明確に依頼された場合はdelete_taskツールを、'
        '「〜の優先度を上げて/下げて」のように優先度変更が明確に依頼された場合はupdate_task_priorityツールを、'
        '「〜の期限を6/30にして」のように特定タスクの期限変更が明確に依頼された場合はupdate_task_dueツールを、'
        '「〜を△△に書き換えて」のようにタスクの内容・タイトルそのものの変更が明確に依頼された場合はupdate_task_titleツールを、'
        '「資料を〜に直して」のように登録済みの参考資料の修正が明確に依頼された場合はupdate_materialツールを、'
        '「毎週〇曜日に〜する」「毎月〇日に〜」「毎日〜」のように繰り返しタスク（定期タスク）の新規登録が明確に依頼された場合はcreate_recurring_taskツールを、'
        '「定期タスク」一覧にあるものの周期・曜日・日にち・リマインド時刻・タイトルの変更が依頼された場合はupdate_recurring_taskツールを、'
        '「定期タスク」一覧にあるものの削除・停止が依頼された場合はdelete_recurring_taskツールを、'
        '「タスクの時間取れませんでした」「今日は忙しかった」のように、依頼や質問ではなくその日の状況・気持ちの報告・感想を述べている場合はrecord_reflectionツールを、\n'
        '「カレンダーに入れて」「予定を登録して」のようにGoogleカレンダーへの予定登録が依頼された場合はcreate_calendar_eventツールを、'
        '「明日のバイトを17時からにして」のようにカレンダーにある予定の変更が依頼された場合はupdate_calendar_eventツールを、'
        '「明日の〇〇をキャンセルして」のようにカレンダーの予定の削除が依頼された場合はdelete_calendar_eventツールを使ってください。\n'
        f'今日の日付は{config.today_iso()}です。日付は必ずYYYY-MM-DD形式に直してツールに渡してください。\n'
        'カレンダーの予定（時間が決まっている用事）と、タスク（やること）は別物です。'
        'ユーザーが「カレンダー」「予定」と言っている場合はカレンダー側のツールを、それ以外はタスク側のツールを使ってください。\n'
        '上記のどれにも明確に当てはまらない場合、つまりユーザーが単に新しいやること・予定を追加したいだけの場合はadd_taskツールを使ってください。'
        '複数行・複数項目のメモでも、それが同じ1つの作業についての補足（誰宛か・何を・いつまでに・数量など）を書き並べているだけなら、それは1つのタスクなのでadd_taskで1件にまとめてください。'
        'add_tasksを使ってよいのは、行ごとに完全に独立していて別々に完了報告できるやること（例：全く別の相手への全く別の連絡と、別の買い物）が複数書かれている場合だけです。'
        '迷ったら分割せずadd_taskを使ってください。'
        '（悪い例1：「株式譲渡契約書の作成／誠さんから山下慎太郎／誠さんから株式会社薙刀／両方作成／100株全部」→ 2人への契約書作成という1つの依頼なのでadd_taskで1件にする（宛先はタイトルやメモに両方書き込む）。'
        '悪い例2：「21提出／22、23に出してもらうようにするシフト」→「22日・23日ぶんのシフトを21日までに提出してもらう」という1つの依頼なのでadd_taskで1件にする。）\n'
        '判断に迷う場合のデフォルトはadd_taskです（誤って追加してもユーザーは後から番号で簡単に削除・修正できるので、扱いに迷ったらまず追加を優先してください）。\n'
        '「要約して」「言い換えて」「まとめて」「相談」のように、何かを実行するのではなく文章で答えるだけでよい場合はreply_with_textツールを使ってください。\n'
        '【重要・最優先】ユーザーが次にやるべき具体的な作業を1つでも述べている場合は、それが業務の背景説明や既存タスクへの言及を伴っていても、'
        '必ずadd_task（複数ならadd_tasks）で実際にタスクとして追加してください。'
        '追加せずに、進捗評価・助言・状況コメントだけを返して終わることは禁止です'
        '（例：「1尾あたりの原価を報告」→報告というタスクをadd_taskで追加する。「関連タスクの状況はこうです」で終わらせて追加しないのは誤りです）。\n'
        '【重要】update_task_status／delete_task／update_task_priority／update_task_due／update_task_titleは、'
        'ユーザーが「完了にして」「削除して」「優先度を上げて」「期限を〜にして」「〜に書き換えて」のように、'
        '既存タスクへの明確な変更・削除の依頼をした場合にのみ使ってください。'
        'そうした変更の言葉が無く、単に新しい作業内容を述べているだけなら、既存タスクと名前が似ていても更新系ツールは使わず、必ずadd_task／add_tasksを使ってください'
        '（名前が似ているというだけでユーザーに聞き返すのではなく、新規タスクとして追加するのがデフォルトです）。\n'
        '【重要】上記のいずれの操作ツールも呼ばずに「登録しました」「追加しました」「削除しました」「完了しました」のように、'
        '何かを実行済みであるかのように答えることは絶対にしないでください。必ず対応するツールを実際に呼び出してから、その結果として報告してください。\n'
        'ユーザーはタスク名を毎回全部書かず、一部の言葉やキーワードだけで指定することが多いです。「未完了タスク一覧」を見て該当するタスクが1つに絞れる場合は、'
        '正式なタイトルを補ってツールを呼び出してください。'
        '明確な変更・削除の依頼であるにもかかわらず似たタスクが複数あり対象を1つに絞れない場合のみ、操作系ツールは呼ばずreply_with_textツールで候補を挙げて確認してください。\n'
        '「直近14日で完了したタスク」や「直近の会話」も参考に、繰り返し先延ばしにしている傾向や、前回の相談からの変化があれば触れてください。\n'
        '「ユーザーが登録した参考資料」に関連する内容があれば、一般論より優先して、その資料の内容を踏まえて具体的に助言してください。\n\n'
        + context + '\n\n【ユーザーの相談】\n' + user_text
    )
    # ツールを呼ばず文章だけで済ませる（＝実行せずに「やりました」と言うだけになりかねない）ことができないよう、
    # 必ず何らかのツール（操作系 or reply_with_text）を呼ぶよう強制する
    res = call_gemini_with_tools(config.AGENT_PERSONA, prompt, AGENT_TOOLS, 900, FORCE_ANY_TOOL)
    if not res:
        # タスク追加もAI経由になったため、AIが一時的に使えないだけで入力を取りこぼさないよう、
        # ひとまずタスクとして追加しておく（操作の依頼だった場合も番号指定等で後から直せる）
        fallback = tasks_mod.add_line(user_id, user_text)
        return f'⚠️ AIコーチが一時的に応答できなかったため、ひとまずタスクとして追加しました。\n\n{fallback}'

    name, args, intro = extract_function_call(res)

    if name == 'propose_subtasks':
        reply = handle_propose_subtasks(user_id, args, intro)
    elif name == 'update_task_status':
        reply = handle_update_task_status(args, intro)
    elif name == 'delete_task':
        reply = handle_delete_task(args, intro)
    elif name == 'update_task_priority':
        reply = handle_update_task_priority(args, intro)
    elif name == 'update_task_due':
        reply = handle_update_task_due(args, intro)
    elif name == 'update_task_title':
        reply = handle_update_task_title(args, intro)
    elif name == 'update_material':
        reply = materials_mod.handle_update_material(args, intro)
    elif name == 'create_recurring_task':
        reply = recurring_mod.handle_create_recurring_task(args, intro)
    elif name == 'update_recurring_task':
        reply = recurring_mod.handle_update_recurring_task(args, intro)
    elif name == 'delete_recurring_task':
        reply = recurring_mod.handle_delete_recurring_task(args, intro)
    elif name == 'record_reflection':
        reply = handle_record_reflection(args, intro)
    elif name == 'create_calendar_event':
        reply = handle_create_calendar_event(user_id, args, intro)
    elif name == 'update_calendar_event':
        reply = handle_update_calendar_event(user_id, args, intro)
    elif name == 'delete_calendar_event':
        reply = handle_delete_calendar_event(user_id, args, intro)
    elif name == 'add_task':
        reply = tasks_mod.add_line(user_id, user_text, (args or {}).get('estimate') or '')
    elif name == 'add_tasks':
        items = []
        for it in (args or {}).get('items') or []:
            # 旧形式（文字列の配列）で返ってくる場合にも備えておく
            if isinstance(it, dict):
                text_part, est = str(it.get('text') or '').strip(), str(it.get('estimate') or '').strip()
            else:
                text_part, est = str(it).strip(), ''
            if text_part:
                items.append((text_part, est))
        if not items:
            reply = tasks_mod.add_line(user_id, user_text)
        else:
            reply = '\n\n'.join(tasks_mod.add_line(user_id, t, e) for t, e in items)
    elif name == 'reply_with_text':
        reply = str((args or {}).get('message') or '').strip() or (intro or '⚠️ AIエージェントの応答取得に失敗しました。')
    else:
        # FORCE_ANY_TOOLにより通常はここに来ないはずだが、モデルが万一ツール名を認識できない形で
        # 返してきた場合の保険。ツールを呼ばずに実行済みを名乗っていないかも念のため確認する。
        reply = intro or '⚠️ AIエージェントの応答取得に失敗しました。'
        if _claims_completed_action(reply):
            print('agent hallucinated completion without a tool call:', reply[:200])
            reply = ('⚠️ 操作が実行できませんでした（AIが実行せずに完了したかのように返答してしまいました）。'
                     'お手数ですが、対象・日時などをもう少し具体的にして、もう一度送ってください。')

    # ツールを呼ばず「対象を明確にして」のように聞き返した場合、次の返信を新規タスク追加と
    # 誤解しないよう、続きの返信として受け取る準備をしておく（例：「両方です」が定期タスクの新規追加になる不具合対策）
    clarify_key = f'PENDING_AI_CLARIFY_{user_id}'
    if _looks_like_clarifying_question(reply):
        set_state(clarify_key, {'createdAt': config.now_ms()})
    else:
        delete_state(clarify_key)

    log_agent_interaction(user_id, user_text, reply)
    return reply


_ACTION_CLAIM_PATTERN = re.compile(
    r'(登録しました|登録を完了しました|追加しました|削除しました|変更しました|更新しました|'
    r'設定しました|反映しました|作成しました|キャンセルしました)'
)


def _claims_completed_action(reply):
    """ツールを呼んでいないのに『登録しました』等の実行済み口調で答えていないかを見る（AIの虚偽報告対策）。
    「完了しました」単体は、タスク状況の要約など無害な文にも出てくるため対象から外している。
    """
    return bool(_ACTION_CLAIM_PATTERN.search(reply))


def _looks_like_clarifying_question(reply):
    """AIコーチの返信が「対象を教えてください」のような聞き返しかどうかを判定する。"""
    tail = reply.strip()[-60:]
    return bool(re.search(r'(？|でしょうか|どちら|教えてください|選んでください|お知らせください)', tail))


_CLARIFY_PENDING_MINUTES = 5


def handle_pending_ai_clarify(user_id, text):
    """AIコーチの聞き返しへの返信を、続きの相談としてもう一度AIに渡す。該当なしはNone。"""
    key = f'PENDING_AI_CLARIFY_{user_id}'
    pending = get_state(key)
    if not pending:
        return None
    if (config.now_ms() - pending.get('createdAt', 0)) / 60000 > _CLARIFY_PENDING_MINUTES:
        delete_state(key)
        return None
    delete_state(key)
    return ask_agent(user_id, text)


# ============ 毎晩の進捗チェックイン・週次レポート（Cronから呼ばれる） ============
def send_agent_checkin(push_text_fn, get_users_fn):
    """プッシュ型：毎晩の進捗チェックイン。日次レポートは番号つきなので別メッセージで送る。"""
    from . import reports as reports_mod

    context = build_agent_context()
    reply = call_gemini(
        config.AGENT_PERSONA,
        '以下は現在のタスク状況です。今日の進捗チェックインとして、①最優先で手をつけるべきタスク　②停滞・放置が気になる要注意タスク　③一言アドバイス、をまとめてください。'
        '③のアドバイスは、単なる励ましではなく、そのタスクの分野に詳しいスペシャリストとしての具体的な進め方を含めてください。\n\n' + context,
        600,
    )

    # 日次レポートは番号を振るため、ユーザーごとに組み立てて送る
    for uid in get_users_fn():
        if reply:
            push_text_fn(uid, '🧭 進捗チェックイン\n\n' + reply)
        push_text_fn(uid, reports_mod.build_daily_report(uid))

    # 期限未設定タスクの棚卸し。以前は全部並べていたが、毎晩同じ顔ぶれが長々と続いて読み飛ばされていたため、
    # 件数だけ伝えてアプリでまとめて設定してもらう形に変えた。
    # ペンディング状態のタスクは、期限をあえて決めずに保留しているものなので対象から除く。
    no_due = get_supabase(
        'tasks',
        'done=eq.false&deleted=eq.false&due=is.null&status=neq.pending&select=id',
    )
    if no_due:
        msg = (f'📅 期限が未設定のタスクが{len(no_due)}件あります。\n'
               'アプリを開いてまとめて決めてしまいましょう。\n' + config.APP_URL)
        for uid in get_users_fn():
            push_text_fn(uid, msg)

    # 優先順位の見直し提案（別メッセージ。すでに確認待ちがあれば重複して提案しない）
    for uid in get_users_fn():
        if get_state(f'PENDING_REPRIORITIZE_{uid}'):
            continue
        proposal = propose_reprioritization(uid)
        if proposal and '変更提案はありません' not in proposal:
            push_text_fn(uid, proposal)


def send_weekly_report(push_text_fn, get_users_fn):
    """毎週日曜21時：その週の完了実績・ペース・積み残しを送る。番号はユーザーごとに記憶する。"""
    from . import reports as reports_mod
    for uid in get_users_fn():
        push_text_fn(uid, reports_mod.build_weekly_report(uid))
