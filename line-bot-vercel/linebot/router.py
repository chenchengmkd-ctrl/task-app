"""コマンドルーティング（意図判定）とWebhookイベント処理。Code.gs の routeCommand/isQuestionLike/handleEvent/helpText に相当。"""
import re

from . import config
from . import agent as agent_mod
from . import tasks as tasks_mod
from . import materials as materials_mod
from . import daily_log as daily_log_mod
from . import reports as reports_mod
from . import planning as planning_mod
from .line_client import remember_user, reply_text


def is_question_like(text):
    """疑問文・相談・タスク分解／状態変更／削除／要約／優先度／期限変更／報告の依頼っぽい文かどうかをざっくり判定（タスク追加との区別用）。"""
    if re.search(r'[?？]$', text):
        return True
    if re.search(r'(か|かな|かしら)[。.!！]?$', text):
        return True
    if re.search(
        r'(完了しました|完了した|終わりました|終わった|できました|やりました|'
        r'できませんでした|できなかった|間に合わなかった|間に合いませんでした|取れませんでした|取れなかった|'
        r'難しかった|大変だった|疲れました|疲れた)[。.!！]?$',
        text,
    ):
        return True
    # 「〇〇ペンディングでお願いします」「〇〇完了に変更」のように、状態名＋依頼表現なら状態変更の依頼とみなす。
    # 状態名だけ（例：「ペンディング案件の整理」）はタスク追加のままにしたいので、依頼表現とセットのときだけ拾う。
    if re.search(r'(完了|着手中|対応待ち|ペンディング|未着手)[にでへは]?\s*(して|しといて|しておいて|お願い|おねがい|変更|にし)', text):
        return True
    if re.search(
        r'(どう|教えて|大丈夫|やばい|相談|アドバイス|優先|分解|やる意味|意味ある|完了に|着手中に|対応待ちに|'
        r'ペンディングに|状態を|ステータスを|削除|消して|要約|言い換え|まとめて|整理して|並べ替え|期限を|期限に|'
        r'書き換えて|タイトルを|名前を|修正して|直して|定期)',
        text,
    ):
        return True
    return False


def route_command(user_id, text):
    # 保留中の返信（サブタスク提案／期限確認／時刻確認／優先順位見直し案）への「はい/いいえ」等を最優先で処理
    pending_subtask = agent_mod.handle_pending_subtask_reply(user_id, text)
    if pending_subtask is not None:
        return pending_subtask
    pending_due = tasks_mod.handle_pending_due_reply(user_id, text)
    if pending_due is not None:
        return pending_due
    pending_time = tasks_mod.handle_pending_time_reply(user_id, text)
    if pending_time is not None:
        return pending_time
    pending_reprioritize = agent_mod.handle_pending_reprioritize_reply(user_id, text)
    if pending_reprioritize is not None:
        return pending_reprioritize
    # 「1,3,5」のような番号だけの返信は、その日にやることの選択として受け取る
    pending_plan = planning_mod.handle_pending_plan_reply(user_id, text)
    if pending_plan is not None:
        return pending_plan

    if re.match(r'^(一覧|リスト|list)$', text, re.IGNORECASE):
        return tasks_mod.list_all(user_id)
    if re.match(r'^(通知|リマインド|今日)$', text, re.IGNORECASE):
        return tasks_mod.build_reminder(user_id) or '📭 期限が近い（超過〜3日以内）のタスクはありません。'
    if re.match(r'^(ヘルプ|使い方|help)$', text, re.IGNORECASE):
        return help_text()
    if re.match(r'^(日報|日次レポート|今日のレポート)$', text):
        return reports_mod.build_daily_report(user_id)
    if re.match(r'^(週報|週次レポート|今週のレポート)$', text):
        return reports_mod.build_weekly_report(user_id)
    if re.match(r'^(明日の予定|予定を決める|予定決め)$', text):
        return planning_mod.plan_prompt_on_demand(user_id)
    if re.match(r'^(今日の予定|予定|予定確認)$', text):
        return planning_mod.show_plan(user_id)
    if re.match(r'^(相談|アドバイス)$', text):
        return agent_mod.ask_agent(user_id, '最近のタスク状況について、率直な進捗評価とアドバイスをください。')
    if re.match(r'^(優先順位を整理して|優先順位を並べ替えて|並べ替えて)$', text):
        return agent_mod.propose_reprioritization(user_id)

    # 「1削除」「5ペンディング」「1削除 5ペンディング」のように、番号＋操作だけの指定はその場で確定させる
    numbered = tasks_mod.handle_numbered_actions(user_id, text)
    if numbered is not None:
        return numbered

    # 「3番の期限を7/1にして」のような番号指定を、直前の一覧を元に実際のタイトルへ変換してAIに渡す
    num_match = re.match(r'^(\d+)番(目)?(を|の)?\s*([\s\S]*)$', text)
    if num_match:
        resolved = tasks_mod.resolve_numbered_task(user_id, int(num_match.group(1)))
        if not resolved:
            return '番号に対応するタスクが見つかりませんでした。「一覧」で番号を確認してから、もう一度お試しください。'
        rest = num_match.group(4).strip()
        if not rest:
            return f"{num_match.group(1)}番：「{resolved['title']}」"
        text = f"「{resolved['title']}」{num_match.group(4)}"

    if re.match(r'^(定期タスク一覧|定期一覧)$', text):
        return tasks_mod.list_all(user_id)
    if re.match(r'^(資料一覧|資料リスト|資料)$', text):
        return materials_mod.list_materials()
    # リッチメニューの「振り返り」ボタン等。次に送られた文章を振り返りとして記録する
    if re.match(r'^(振り返り|ふりかえり)$', text):
        return daily_log_mod.start_reflection_prompt()
    material_match = re.match(r'^資料[:：]\s*([\s\S]+)$', text)
    if material_match:
        return materials_mod.add_material_from_text(material_match.group(1).strip())
    log_match = re.match(r'^振り返り[:：]\s*([\s\S]+)$', text)
    if log_match:
        return daily_log_mod.add_daily_log(log_match.group(1).strip())
    yt_match = re.search(
        r'(https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)[\w-]+|youtu\.be/[\w-]+))', text,
    )
    if yt_match:
        return materials_mod.add_material_from_youtube(yt_match.group(1))
    if not text:
        return '空メッセージです。「ヘルプ」で使い方を表示します。'
    # 「毎日／毎週◯曜／毎月◯日」で始まる文章は定期タスクの依頼とみなし、AIコーチ（create_recurring_task）に回す
    if re.match(r'^毎(日|週|月)', text):
        return agent_mod.ask_agent(user_id, text)
    # 振り返りの催促直後なら、自由文はタスクではなく振り返りとして記録する
    pending_log = daily_log_mod.handle_pending_log_reply(text)
    if pending_log is not None:
        return pending_log
    if is_question_like(text):
        return agent_mod.ask_agent(user_id, text)
    return tasks_mod.add_line(user_id, text)


def handle_event(ev):
    """LINEのWebhookイベント1件を処理する。"""
    if ev.get('type') != 'message' or (ev.get('message') or {}).get('type') != 'text':
        return
    user_id = (ev.get('source') or {}).get('userId')
    remember_user(user_id)
    reply = route_command(user_id, ev['message']['text'].strip())
    reply_text(ev.get('replyToken'), reply)


def help_text():
    lines = [
        '📖 使い方', '',
        '・文章を送る → タスク追加',
        '　例）牛乳を買う',
        '　例）牛乳を買う 6/25  ← 末尾に日付で期限つき',
        '　例）会議の準備 6/25 15:00  ← 日付＋時刻もつけられる',
        '　例）今日17時に小室くんにチラシの件伝える  ← 自然な文中の日付・時刻もOK',
        '　（今日／明日／明後日、HH時MM分／HH時／HH:MM に対応）',
        '・一覧 → 未完了タスクを番号つきで表示',
        '　番号だけで操作できます（一覧・通知の番号は12時間有効）',
        '　例）1削除／5ペンディング／3完了／2着手中／4対応待ち',
        '　例）1削除 5ペンディング　→ まとめて指定もOK',
        '　例）1明日／2を7/30　→ 番号で期限だけ変更（リスケ）',
        '　例）1 15時／2は10時30分　→ 番号で時間を設定',
        '　例）3 明日10時　→ 番号で期限と時間をまとめて設定',
        '　例）3番のタイトルを〜に書き換えて　→ 細かい指示は「3番の〜」と続けて',
        '・通知 → 今の期限リマインドを番号つきで表示（そのまま「1完了」などで操作可）',
        '・日報 → 今日の完了件数・今日が期限のもの・期限切れ・ペースを表示',
        '・週報 → 直近1週間の完了件数とペース、前週比、積み残しを表示',
        '　（どちらも番号つきなので、その場で「1完了」「1明日」と返せます）',
        '・予定を決める（＝明日の予定）→ やる候補を番号つきで表示（「1,3,5」と番号を返して確定）',
        '　（20時より前に送るとその日ぶん、20時以降に送ると翌日ぶんの予定になります）',
        '・今日の予定 → 決めたぶんが今どこまで進んでいるかを表示',
        '・相談・アドバイス → AIコーチに進捗評価を聞く',
        '　（「〜どう？」のような疑問文もAIコーチが拾って回答します）',
        '・AIコーチにはタスクの分解・状態変更・削除・優先度変更・期限変更も頼めます',
        '　例）会議資料の準備を分解して　→ 提案が来たら「はい」で追加',
        '　例）牛乳を買うを完了にして　→ その場で状態を変更',
        '　例）牛乳を買うを削除して　→ その場で削除',
        '　例）牛乳を買うの優先度を上げて　→ その場で優先度を変更',
        '　例）牛乳を買うの期限を7/1にして　→ その場で期限を変更',
        '　例）牛乳を買うを牛乳とパンを買うに書き換えて　→ その場でタスク内容を変更',
        '　（対象タスクはタイトルを全部書かなくてもOK。「うなぎの方完了にして」のようにキーワードだけでも、候補が1つに絞れればAIが判断します）',
        '　例）このタスクやる意味ある？　→ 率直な意見を返します',
        '　例）要約して／言い換えて　→ タスク状況を簡潔にまとめて返します',
        '・優先順位を整理して　→ AIが全体を見直して並べ替え案を提示（「はい」で適用）',
        '・長い文章や箇条書きを送ると、AIが要点だけのタスク名に整理して追加します',
        '・期限を指定せずに追加すると、その場で期限を聞かれます（不要なら「なし」）',
        '・今日／明日／明後日の期限で時刻を指定しなかった場合、その場で時刻も聞かれます（不要なら「なし」）',
        '・期限が3日以内に迫っているのに時刻が未設定のタスクは、毎朝の通知とは別メッセージで番号つきでまとめて聞かれます'
        '（「1 15時」のように番号と時間を返すだけで設定できます）',
        '・期限が未設定のタスクも、毎晩の進捗チェックインで番号つきでまとめて聞かれます（「1明日」で設定）',
        '・YouTubeのURLを送る、または「資料：本文」の形式でテキストを送ると、AIコーチが要点を要約して覚え、以降の相談で参考にします',
        '・資料一覧 → 登録済みの資料を確認',
        '　例）〇〇の資料を△△に直して　→ 資料の内容をAIが書き直し（理解の相違があった時など）',
        '・振り返り：本文　の形式で送ると、その日の一言メモとして記録します',
        '　例）振り返り：今日はうなぎの仕込みが予定より早く終わった',
        '　（週次レポートや相談時にAIコーチが参考にします）',
        '・定期タスク（毎日／毎週〇曜／毎月〇日）はアプリの「🔁 定期タスク」タブ、またはLINEで登録できます',
        '　例）毎週月曜19時に週報を出すのを定期タスクにして　→ その場で登録',
        '　例）毎月末日に家賃を振り込むのを定期タスクとして登録して',
        '　（登録済みの定期タスクは「一覧」の中で別メッセージとして表示されます。ボードへの自動追加はされず、'
        '指定した時刻＝未指定なら毎朝の通知と同じ時刻に、そのつどLINEでリマインドします）',
        '',
        '⏰ 自動で届くもの',
        f'・毎朝{config.REMIND_HOUR}時：期限リマインド（番号つき）',
        f'・毎晩{config.AGENT_HOUR}時：AIコーチの進捗チェックイン＋日報',
        '・毎晩22:30：その日決めた予定の答え合わせ（できていないものはそのままリマインド）',
        '・毎晩23:30：明日やることの洗い出し（番号で選んで確定）',
        '　決まらない場合は 0:30／6:30／8:00 に催促（朝8時が最終期限）',
        '・毎晩24時：振り返りの催促（1時間たっても届かなければもう一度だけ催促）',
        '・毎週日曜21時：週報',
        f'・時刻つきタスクは開始{config.TIME_LEAD_MINUTES}分前にもリマインド',
        '・定期タスクは設定時刻（未設定なら毎朝の通知と同じ時刻）にリマインド',
    ]
    return '\n'.join(lines)
