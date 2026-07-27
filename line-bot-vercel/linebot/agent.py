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
from .gemini_client import call_gemini, call_gemini_with_tools, extract_function_call, AGENT_TOOLS, TOOLS_REPRIORITIZE
from .supabase_client import get_supabase, post_supabase, patch_supabase, get_state, set_state, delete_state


# ============ タスク状況・文脈の構築 ============
def build_agent_context():
    """タスク状況（Supabase）＋定期タスク＋直近の完了実績＋過去の会話をAI用にまとめる。"""
    tasks = get_supabase('tasks', 'done=eq.false&deleted=eq.false&select=title,status,priority,due,updated_at&order=updated_at.asc')
    now = config.now_jst()

    task_lines = []
    for t in tasks:
        updated = config.parse_timestamp(t.get('updated_at'))
        days = int((now - updated).total_seconds() // 86400) if updated else 0
        due = f" 期限:{t['due']}" if t.get('due') else ''
        status = config.STATUS_LABEL_JP.get(t.get('status'), t.get('status'))
        task_lines.append(f"・{t['title']}（状態:{status}{due} 優先度:{t['priority']} 最終更新:{days}日前）")

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

    parts = ['【未完了タスク一覧】', '\n'.join(task_lines) or 'なし']
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

    prefix = f'{intro}\n\n' if intro else ''
    numbered = '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(subtasks))
    return f'{prefix}🧩「{parent_title}」の分解案\n{numbered}\n\n追加してよければ「はい」と送ってください（5分以内）。'


def add_subtasks_to_app(parent_title, subtasks):
    now = config.now_iso()
    rows = [{
        'id': config.new_id(), 'title': title, 'status': 'todo', 'priority': 'mid', 'due': None, 'due_time': None,
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
    if re.match(r'^(はい|追加|うん|お願い(します)?|ok|yes)$', t, re.IGNORECASE):
        delete_state(key)
        return add_subtasks_to_app(pending['parentTitle'], pending['subtasks'])
    if re.match(r'^(いいえ|キャンセル|やめて|no)$', t, re.IGNORECASE):
        delete_state(key)
        return '🙅 分解案の追加をキャンセルしました。'
    return None


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
    else:
        body['done'] = False
        body['status'] = new_status

    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", body)
    if updated is None:
        return f'⚠️「{title}」の更新に失敗しました。'

    label = '完了' if new_status == 'done' else config.STATUS_LABEL_JP.get(new_status, new_status)
    prefix = f'{intro}\n\n' if intro else ''
    return f'{prefix}✅「{title}」を{label}に変更しました。'


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

    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", {'deleted': True, 'updated_at': config.now_iso()})
    if updated is None:
        return f'⚠️「{title}」の削除に失敗しました。'

    prefix = f'{intro}\n\n' if intro else ''
    return f'{prefix}🗑️「{title}」を削除しました。'


def handle_update_task_priority(input_args, intro):
    a = input_args or {}
    title = str(a.get('task_title') or '').strip()
    new_priority = str(a.get('new_priority') or '').strip()
    if not title or new_priority not in ('high', 'mid', 'low'):
        return '⚠️ 優先度変更の内容を理解できませんでした。'

    matches = tasks_mod.find_task_by_title(title)
    if not matches:
        return f'⚠️「{title}」に一致するタスクが見つかりませんでした。'
    if len(matches) > 1:
        return f'⚠️「{title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。'

    updated = patch_supabase('tasks', f"id=eq.{quote(matches[0]['id'])}", {'priority': new_priority, 'updated_at': config.now_iso()})
    if updated is None:
        return f'⚠️「{title}」の更新に失敗しました。'

    label = {'high': '高', 'mid': '中', 'low': '低'}[new_priority]
    prefix = f'{intro}\n\n' if intro else ''
    return f'{prefix}✅「{title}」の優先度を{label}に変更しました。'


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

    prefix = f'{intro}\n\n' if intro else ''
    time_part = f' {due_time}' if due_time else ''
    return f'{prefix}✅「{title}」の期限を {config.jp(due)}{time_part} に変更しました。'


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

    prefix = f'{intro}\n\n' if intro else ''
    return f'{prefix}✅「{title}」を「{new_title}」に書き換えました。'


def handle_record_reflection(input_args, intro):
    """AIが「これは振り返り報告だ」と判断した発言を、そのままdaily_logsに記録する。"""
    content = str((input_args or {}).get('content') or '').strip()
    if not content:
        return '⚠️ 振り返りの内容を理解できませんでした。'
    prefix = f'{intro}\n\n' if intro else ''
    return prefix + daily_log_mod.add_daily_log(content)


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
        '以下は現在のタスク状況です。期限・停滞日数・状態を踏まえて、優先度を変えたほうがよいタスクだけをreprioritize_tasksツールで提案してください。'
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

    label = {'high': '高', 'mid': '中', 'low': '低'}
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
    if re.match(r'^(はい|適用|うん|お願い(します)?|ok|yes)$', t, re.IGNORECASE):
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
    if re.match(r'^(いいえ|キャンセル|やめて|no)$', t, re.IGNORECASE):
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
        '「タスクの時間取れませんでした」「今日は忙しかった」のように、依頼や質問ではなくその日の状況・気持ちの報告・感想を述べている場合はrecord_reflectionツールを使ってください。\n'
        '「要約して」「言い換えて」「まとめて」のような依頼には、ツールを使わず文章で簡潔に答えてください。\n'
        'ユーザーはタスク名を毎回全部書かず、一部の言葉やキーワードだけで指定することが多いです。「未完了タスク一覧」を見て該当するタスクが1つに絞れる場合は、'
        '正式なタイトルを補ってツールを呼び出してください。似たタスクが複数あり判断できない場合のみ、ツールを使わず候補を挙げて確認してください。\n'
        '「直近14日で完了したタスク」や「直近の会話」も参考に、繰り返し先延ばしにしている傾向や、前回の相談からの変化があれば触れてください。\n'
        '「ユーザーが登録した参考資料」に関連する内容があれば、一般論より優先して、その資料の内容を踏まえて具体的に助言してください。\n\n'
        + context + '\n\n【ユーザーの相談】\n' + user_text
    )
    res = call_gemini_with_tools(config.AGENT_PERSONA, prompt, AGENT_TOOLS, 700)
    if not res:
        return '⚠️ AIエージェントの応答取得に失敗しました。時間をおいて再度お試しください。'

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
    elif name == 'record_reflection':
        reply = handle_record_reflection(args, intro)
    else:
        reply = intro or '⚠️ AIエージェントの応答取得に失敗しました。'

    log_agent_interaction(user_id, user_text, reply)
    return reply


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

    # 期限未設定タスクの棚卸し（すでに確認待ちがあれば重複して聞かない）
    no_due = get_supabase('tasks', 'done=eq.false&deleted=eq.false&due=is.null&select=id,title&limit=10')
    if no_due:
        msg = '📅 期限未設定のタスク\n' + '\n'.join(f"・{t['title']}" for t in no_due) + \
            '\n\n期限を教えてください（例：「◯◯は6/30、△△は今日」。不要なら「なし」）。'
        for uid in get_users_fn():
            if get_state(f'PENDING_DUE_{uid}'):
                continue
            set_state(f'PENDING_DUE_{uid}', {
                'mode': 'batch',
                'tasks': [{'id': t['id'], 'title': t['title']} for t in no_due],
                'createdAt': config.now_ms(),
            })
            push_text_fn(uid, msg)

    # 優先順位の見直し提案（別メッセージ。すでに確認待ちがあれば重複して提案しない）
    for uid in get_users_fn():
        if get_state(f'PENDING_REPRIORITIZE_{uid}'):
            continue
        proposal = propose_reprioritization(uid)
        if proposal and '変更提案はありません' not in proposal:
            push_text_fn(uid, proposal)


def send_weekly_report(push_text_fn, get_users_fn):
    """毎週月曜9時：先週の完了実績・ペース・積み残しを送る。番号はユーザーごとに記憶する。"""
    from . import reports as reports_mod
    for uid in get_users_fn():
        push_text_fn(uid, reports_mod.build_weekly_report(uid))
