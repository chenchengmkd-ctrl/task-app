"""Gemini API呼び出しとFunction Callingツール定義。Code.gs の callGemini*/AGENT_TOOLS等に相当。"""
import json

import requests

from . import config


def call_gemini_raw(system_prompt, user_text, tools=None, max_tokens=800):
    """user_text は文字列、または動画等を渡す場合はparts配列（例：[{'file_data':{'file_uri':url}},{'text':'...'}]）。"""
    parts = user_text if isinstance(user_text, list) else [{'text': user_text}]
    payload = {
        'system_instruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'role': 'user', 'parts': parts}],
        'generationConfig': {'maxOutputTokens': max_tokens or 800},
    }
    if tools:
        payload['tools'] = tools
    url = (
        f'https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}'
        f':generateContent?key={config.GEMINI_API_KEY}'
    )
    try:
        res = requests.post(url, headers={'Content-Type': 'application/json'}, data=json.dumps(payload), timeout=55)
    except requests.RequestException as e:
        print('Gemini API request failed', e)
        return None
    if res.status_code != 200:
        print('Gemini API error', res.status_code, res.text)
        return None
    return res.json()


def call_gemini(system_prompt, user_text, max_tokens=800, tools=None):
    """応答テキストを返す（失敗時はNone）。"""
    data = call_gemini_raw(system_prompt, user_text, tools, max_tokens)
    if not data:
        return None
    parts = (((data.get('candidates') or [{}])[0]).get('content') or {}).get('parts') or []
    for p in parts:
        if p.get('text'):
            return p['text']
    return None


def call_gemini_with_tools(system_prompt, user_text, tools, max_tokens=800):
    """レスポンス全体（functionCallを含む）を返す（失敗時はNone）。"""
    return call_gemini_raw(system_prompt, user_text, tools, max_tokens)


def extract_function_call(res):
    """レスポンスから (function_name, function_args, intro_text) を取り出す。functionCallが無ければ function_name は None。"""
    if not res:
        return None, None, None
    parts = (((res.get('candidates') or [{}])[0]).get('content') or {}).get('parts') or []
    func_part = next((p for p in parts if p.get('functionCall')), None)
    text_part = next((p for p in parts if p.get('text')), None)
    intro = text_part['text'] if text_part else ''
    if func_part:
        fc = func_part['functionCall']
        return fc.get('name'), (fc.get('args') or {}), intro
    return None, None, intro


# ============ AIエージェントが使えるツール（Gemini Function Calling形式） ============
AGENT_TOOLS = [{
    'functionDeclarations': [
        {
            'name': 'propose_subtasks',
            'description': 'ユーザーが指定したタスクをより小さいサブタスクに分解して提案する。実際の追加はユーザーの確認後に行われるため、ここでは提案するだけでよい。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'parent_task_title': {'type': 'STRING', 'description': '分解対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）'},
                    'subtasks': {'type': 'ARRAY', 'items': {'type': 'STRING'}, 'description': '3〜5個程度の、具体的で実行しやすいサブタスクのタイトル'},
                },
                'required': ['parent_task_title', 'subtasks'],
            },
        },
        {
            'name': 'update_task_status',
            'description': 'ユーザーが「完了にして」「着手中にして」のように状態変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。どのタスクか曖昧な場合はツールを呼ばず、文章で確認すること。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'task_title': {'type': 'STRING', 'description': '状態変更の対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）'},
                    'new_status': {'type': 'STRING', 'enum': ['todo', 'doing', 'waiting', 'pending', 'done'], 'description': '変更後の状態。完了にする場合は done'},
                },
                'required': ['task_title', 'new_status'],
            },
        },
        {
            'name': 'delete_task',
            'description': 'ユーザーが「削除して」「消して」のようにタスクの削除を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。どのタスクか曖昧な場合はツールを呼ばず、文章で確認すること。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'task_title': {'type': 'STRING', 'description': '削除対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）'},
                },
                'required': ['task_title'],
            },
        },
        {
            'name': 'update_task_priority',
            'description': 'ユーザーが「優先度を上げて」「優先順位を高くして」のように特定タスクの優先度変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'task_title': {'type': 'STRING', 'description': '優先度変更の対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）'},
                    'new_priority': {'type': 'STRING', 'enum': ['high', 'mid', 'low'], 'description': '変更後の優先度'},
                },
                'required': ['task_title', 'new_priority'],
            },
        },
        {
            'name': 'update_task_due',
            'description': 'ユーザーが「期限を6/30にして」のように特定タスクの期限変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'task_title': {'type': 'STRING', 'description': '期限変更の対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）'},
                    'due': {'type': 'STRING', 'description': 'YYYY-MM-DD形式の新しい期限'},
                    'due_time': {'type': 'STRING', 'description': 'HH:MM形式の時刻（分かる場合のみ、無ければ省略）'},
                },
                'required': ['task_title', 'due'],
            },
        },
        {
            'name': 'update_task_title',
            'description': 'ユーザーが「〇〇を△△に書き換えて」「〇〇のタイトルを△△に変更して」のように、既存タスクの内容・タイトルそのものの変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'task_title': {'type': 'STRING', 'description': '変更対象タスクの現在のタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）'},
                    'new_title': {'type': 'STRING', 'description': '変更後の新しいタイトル'},
                },
                'required': ['task_title', 'new_title'],
            },
        },
        {
            'name': 'create_recurring_task',
            'description': 'ユーザーが「毎週〇曜日に〜する」「毎月〇日に〜」「毎日〜」のように、繰り返し発生するタスク（定期タスク）の新規登録を明確に依頼した場合に使う。通常の単発タスク追加とは別物で、こちらは繰り返しの予定を新しく作る場合のみ使うこと。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'title': {'type': 'STRING', 'description': '定期タスクのタイトル'},
                    'recurrence': {'type': 'STRING', 'enum': ['daily', 'weekly', 'monthly'], 'description': '繰り返しの周期'},
                    'weekday': {'type': 'NUMBER', 'description': 'recurrenceがweeklyの場合の曜日（0=日曜〜6=土曜）。weekly以外では省略'},
                    'monthday': {'type': 'STRING', 'description': 'recurrenceがmonthlyの場合の日にち（1〜31の数字、または月末なら文字列"last"）。monthly以外では省略'},
                    'remind_time': {'type': 'STRING', 'description': 'リマインドしてほしい時刻（HH:MM形式）。ユーザーが時刻を言っていなければ省略する'},
                    'start_date': {'type': 'STRING', 'description': '開始日（YYYY-MM-DD形式）。ユーザーが言っていなければ省略する（省略時は今日から開始）'},
                },
                'required': ['title', 'recurrence'],
            },
        },
        {
            'name': 'record_reflection',
            'description': 'ユーザーが「タスクの時間取れませんでした」「今日は忙しかった」のように、タスクの追加やコマンドではなく、その日の状況・出来事・気持ちを報告・感想として述べた場合に使う。振り返りログとして記録する。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'content': {'type': 'STRING', 'description': 'ユーザーが述べた状況・気持ちの内容（ユーザーの発言をもとに簡潔にまとめる）'},
                },
                'required': ['content'],
            },
        },
        {
            'name': 'update_material',
            'description': 'ユーザーが「〇〇の資料を直して」「資料の2番をもっと具体的にして」のように、タスクではなく登録済みの参考資料（資料一覧にあるもの）の内容修正・書き直しを明確に依頼した場合に使う。ユーザーが「資料」という言葉で言及している場合はこちらを優先すること。',
            'parameters': {
                'type': 'OBJECT',
                'properties': {
                    'material_title': {'type': 'STRING', 'description': '修正対象の資料の正式なタイトル（ユーザーが一部の言葉やキーワードだけで指定していても、「ユーザーが登録した参考資料」一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）'},
                    'instruction': {'type': 'STRING', 'description': 'ユーザーが伝えた修正内容・指示をそのまま入れる'},
                },
                'required': ['material_title', 'instruction'],
            },
        },
    ],
}]

# バッチで複数タスクへ期限を割り当てるためのツール（夜間チェックインの棚卸し返信の解釈に使用）
TOOLS_SET_DUE_BATCH = [{
    'functionDeclarations': [{
        'name': 'set_due_dates',
        'description': 'タスク一覧と、ユーザーの返信文をもとに、各タスクに割り当てる期限を返す。返信で触れられていないタスクは含めない。',
        'parameters': {
            'type': 'OBJECT',
            'properties': {
                'assignments': {
                    'type': 'ARRAY',
                    'items': {
                        'type': 'OBJECT',
                        'properties': {
                            'task_title': {'type': 'STRING', 'description': 'タスク一覧の名称と完全一致'},
                            'due': {'type': 'STRING', 'description': 'YYYY-MM-DD形式の期限'},
                            'due_time': {'type': 'STRING', 'description': 'HH:MM形式の時刻（分かる場合のみ）'},
                        },
                        'required': ['task_title', 'due'],
                    },
                },
            },
            'required': ['assignments'],
        },
    }],
}]

# バッチで複数タスクへ時刻を割り当てるためのツール（期限が近いのに時刻未設定タスクの棚卸し返信の解釈に使用）
TOOLS_SET_TIME_BATCH = [{
    'functionDeclarations': [{
        'name': 'set_times',
        'description': 'タスク一覧と、ユーザーの返信文をもとに、各タスクに割り当てる時刻を返す。返信で触れられていないタスクは含めない。',
        'parameters': {
            'type': 'OBJECT',
            'properties': {
                'assignments': {
                    'type': 'ARRAY',
                    'items': {
                        'type': 'OBJECT',
                        'properties': {
                            'task_title': {'type': 'STRING', 'description': 'タスク一覧の名称と完全一致'},
                            'due_time': {'type': 'STRING', 'description': 'HH:MM形式の時刻'},
                        },
                        'required': ['task_title', 'due_time'],
                    },
                },
            },
            'required': ['assignments'],
        },
    }],
}]

# 未完了タスク全体の優先順位見直し案を作るためのツール（毎晩のチェックイン／「優先順位を整理して」で使用）
TOOLS_REPRIORITIZE = [{
    'functionDeclarations': [{
        'name': 'reprioritize_tasks',
        'description': '未完了タスク一覧を、期限・停滞日数・状態を踏まえて見直し、優先度を変えるべきタスクだけを返す。今のままでよいタスクは含めない。',
        'parameters': {
            'type': 'OBJECT',
            'properties': {
                'assignments': {
                    'type': 'ARRAY',
                    'items': {
                        'type': 'OBJECT',
                        'properties': {
                            'task_title': {'type': 'STRING', 'description': 'タスク一覧の名称と完全一致'},
                            'priority': {'type': 'STRING', 'enum': ['high', 'mid', 'low'], 'description': '新しい優先度'},
                        },
                        'required': ['task_title', 'priority'],
                    },
                },
            },
            'required': ['assignments'],
        },
    }],
}]
