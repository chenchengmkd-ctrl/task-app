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


# かつては正規表現でキーワードを見て「AIに渡すか／そのままタスク追加するか」を振り分けていたが、
# 新しい言い回しや機能が増えるたびにキーワード漏れで誤動作する問題が繰り返し起きていたため廃止。
# 現在は下のフォールバック（route_command末尾）で必ずAI（agent_mod.ask_agent）に渡し、
# タスク追加自体もAIが持つ add_task ツールとして扱う。判定はAI自身に任せる。


def route_command(user_id, text):
    # 「タスク ○○」「タスク：○○」のように明示的に接頭辞がついている場合は、
    # 振り返り催促の返信待ちなど他の保留状態が残っていても、確実にタスク追加として扱う（最優先）。
    explicit_task = re.match(r'^タスク[\s　:：]+([\s\S]+)$', text)
    if explicit_task:
        return tasks_mod.add_line(user_id, explicit_task.group(1).strip())

    # シフト表の読み取り結果への「はい／いいえ」。直前に写真を送って始まった確認なので、
    # 他の保留中の確認より先に見る（16日ぶんのカレンダー登録がかかっているため）
    from . import shift as shift_mod
    pending_shift = shift_mod.handle_pending_reply(user_id, text)
    if pending_shift is not None:
        return pending_shift
    # 保留中の返信（サブタスク提案／期限確認／時刻確認）への「はい/いいえ」等を最優先で処理
    pending_subtask = agent_mod.handle_pending_subtask_reply(user_id, text)
    if pending_subtask is not None:
        return pending_subtask
    pending_due = tasks_mod.handle_pending_due_reply(user_id, text)
    if pending_due is not None:
        return pending_due
    pending_time = tasks_mod.handle_pending_time_reply(user_id, text)
    if pending_time is not None:
        return pending_time
    pending_calendar = agent_mod.handle_pending_calendar_reply(user_id, text)
    if pending_calendar is not None:
        return pending_calendar
    # レシート写真の読み取り結果への「登録／取消／2削除」
    from . import receipt as receipt_mod
    pending_receipt = receipt_mod.handle_pending_reply(user_id, text)
    if pending_receipt is not None:
        return pending_receipt
    # 「1,3,5」のような番号だけの返信は、その日にやることの選択として受け取る
    pending_plan = planning_mod.handle_pending_plan_reply(user_id, text)
    if pending_plan is not None:
        return pending_plan

    if re.match(r'^(一覧|リスト|list)$', text, re.IGNORECASE):
        return tasks_mod.list_all(user_id)
    if re.match(r'^(通知|リマインド|今日)$', text, re.IGNORECASE):
        return tasks_mod.build_reminder(user_id) or '📭 期限が近い（超過〜3日以内）のタスクはありません。'
    if re.match(r'^(ヘルプ|使い方|help|マニュアル)$', text, re.IGNORECASE):
        return help_text()
    if re.match(r'^(日報|日次レポート|今日のレポート)$', text):
        return reports_mod.build_daily_report(user_id)
    if re.match(r'^(週報|週次レポート|今週のレポート)$', text):
        return reports_mod.build_weekly_report(user_id)
    if re.match(r'^(今日の予定を決める|今日決める|今日のタスクを決める)$', text):
        return planning_mod.plan_today_on_demand(user_id)
    if re.match(r'^(明日の予定を決める|明日決める|明日のタスクを決める)$', text):
        return planning_mod.plan_tomorrow_on_demand(user_id)
    if re.match(r'^(明日の予定|予定を決める|予定決め)$', text):
        return planning_mod.plan_prompt_on_demand(user_id)
    if re.match(r'^(今日の予定|予定|予定確認)$', text):
        return planning_mod.show_plan(user_id)
    if re.match(r'^(カレンダー|空き時間|スケジュール)$', text):
        from . import gcal
        return gcal.calendar_command()
    # 財務管理アプリ（oneburg-finance）の売上・損益。データは同じSupabaseのbirdmen_kvを読み書きする
    if re.match(r'^(収支|売上|財務|今日の収支|今日の売上)$', text):
        from . import finance as finance_mod
        return finance_mod.build_daily_finance_report()
    if re.match(r'^(今月の収支|今月の売上|月次収支|当月収支)$', text):
        from . import finance as finance_mod
        return finance_mod.build_month_finance_report()
    if re.match(r'^(入力ヘルプ|財務ヘルプ|収支ヘルプ)$', text):
        from . import finance as finance_mod
        return finance_mod.finance_help()
    # Square（レジ）。「スクエア」で確認、「スクエア取込」でアプリの売上に反映。日付を付ければその日
    square_cmd = re.match(r'^(?:スクエア|Square|square|レジ)\s*(取込|取り込み|反映)?\s*((?:\d{1,2})[/月]\d{1,2}日?)?$', text)
    if square_cmd:
        from . import square as square_mod
        from . import finance as finance_mod
        date_iso = finance_mod.date_token_to_iso(square_cmd.group(2)) if square_cmd.group(2) else None
        if square_cmd.group(1):
            return square_mod.import_day(user_id, date_iso)
        return square_mod.build_summary(date_iso)
    # 出数（商品ごとの個数）。「出数」で当日、「出数 8/5」で日付指定、「出数取込 2026-07」で月まとめて取り込み
    items_backfill = re.match(r'^(?:出数|明細)\s*(?:取込|取り込み)\s*(\d{4})[-/年](\d{1,2})月?$', text)
    if items_backfill:
        from . import square as square_mod
        r = square_mod.backfill_sales_detail(f'{items_backfill.group(1)}-{int(items_backfill.group(2)):02d}')
        return f'✅ {r["month"]} の出数を取り込みました\n営業{r["days"]}日 ／ 客数 {r["customers"]}組 ／ 売上 {r["total"]:,}円'
    items_cmd = re.match(r'^(?:出数|売れ筋|明細)\s*((?:\d{1,2})[/月]\d{1,2}日?)?$', text)
    if items_cmd:
        from . import square as square_mod
        from . import finance as finance_mod
        date_iso = finance_mod.date_token_to_iso(items_cmd.group(1)) if items_cmd.group(1) else None
        return square_mod.build_items_report(date_iso)
    # 週次CF予想（翌週の入金・支出の見積もり）。日曜夜の自動送信を待たずに確認する
    if re.match(r'^(?:週次CF|週次cf|来週のCF|来週の資金繰り|週次資金繰り)$', text):
        from . import weekly_cf
        return weekly_cf.build_weekly_cf()
    # 週次実績（今週ここまでの売上・客数・鰻/ご飯の使用量・人件費・支出）。土曜17時の自動送信を待たずに確認する
    if re.match(r'^(?:週次実績|今週の実績|週の実績)$', text):
        from . import weekly_actual
        return weekly_actual.build_weekly_actual_report()
    # 資金繰り予想（現金累計・次回金曜振込の見込み）を毎日の自動通知を待たずに確認する
    cashflow_cmd = re.match(r'^(?:資金繰り予想|資金繰り|振込予定)\s*((?:\d{1,2})[/月]\d{1,2}日?)?$', text)
    if cashflow_cmd:
        from . import square as square_mod
        from . import finance as finance_mod
        date_iso = finance_mod.date_token_to_iso(cashflow_cmd.group(1)) if cashflow_cmd.group(1) else None
        return square_mod.build_cashflow_report(date_iso)
    # 「売上 52000」「食材 お米 1000」「シフト 都丸 10:00 14:30」「取り消し」など。
    # 該当しなければNoneが返るので、そのまま下のタスク系ルーティングへ流れる
    from . import finance as finance_mod
    finance_input = finance_mod.handle_finance_input(user_id, text)
    if finance_input is not None:
        return finance_input
    if re.match(r'^(相談|アドバイス)$', text):
        return agent_mod.ask_agent(user_id, '最近のタスク状況について、率直な進捗評価とアドバイスをください。')

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

    if re.match(r'^(定期タスク|定期タスク一覧|定期一覧|定期)$', text):
        from . import recurring as recurring_mod
        return recurring_mod.recurring_list_message()
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
    # AIコーチが「対象を明確にして」と聞き返した直後なら、自由文はタスク追加ではなく続きの返答として扱う
    pending_clarify = agent_mod.handle_pending_ai_clarify(user_id, text)
    if pending_clarify is not None:
        return pending_clarify
    # ここまでのどのパターンにも当てはまらない自由文は、すべてAIに渡して判断させる
    # （タスク追加そのものも add_task ツールとしてAIが扱う。詳細は agent.py の ask_agent を参照）
    return agent_mod.ask_agent(user_id, text)


def _handle_image_message(user_id, message_id):
    """送られてきた写真を、シフト表なら勤務表として、それ以外はレシートとして読み取る。→ 返信文"""
    from . import receipt as receipt_mod
    from . import shift as shift_mod

    # シフト表かどうかの判定だけ先に済ませる。レシート側の処理には手を触れず、
    # シフト表と分かったときだけそちらへ回す。
    try:
        image_bytes, mime = receipt_mod.fetch_image(message_id)
        if image_bytes and len(image_bytes) <= shift_mod.MAX_IMAGE_BYTES \
                and shift_mod.looks_like_shift_table(image_bytes, mime):
            return shift_mod.handle_image(user_id, image_bytes, mime)
    except Exception as e:
        # 判定に失敗しただけならレシートとして読み進める（従来どおりの動きに戻す）
        print('shift detect error:', e)

    try:
        return receipt_mod.handle_image(user_id, message_id)
    except Exception as e:
        # 読み取りに失敗しても黙って終わらせない（返信が無いと送れたのかが分からないため）
        print('receipt handle_image error:', e)
        return 'レシートの読み取りでエラーが出ました。もう一度送ってみてください。'


def handle_event(ev):
    """LINEのWebhookイベント1件を処理する。"""
    if ev.get('type') != 'message':
        return
    message = ev.get('message') or {}
    msg_type = message.get('type')
    user_id = (ev.get('source') or {}).get('userId')

    # 写真はレシートかシフト表のどちらか。まず種類を見分けてから読み取る。
    # どちらもこの時点では保存せず、内容を見せて確認してもらってから登録する。
    if msg_type == 'image':
        remember_user(user_id)
        reply = _handle_image_message(user_id, message.get('id'))
        reply_text(ev.get('replyToken'), reply)
        return

    if msg_type != 'text':
        return
    remember_user(user_id)
    reply = route_command(user_id, message['text'].strip())
    reply_text(ev.get('replyToken'), reply)


def help_text():
    lines = [
        '📖 使い方',
        '（見やすいマニュアルはこちら → https://chenchengmkd-ctrl.github.io/task-app/guide.html ）', '',
        '・文章を送る → タスク追加',
        '　例）牛乳を買う',
        '　例）牛乳を買う 6/25  ← 末尾に日付で期限つき',
        '　例）会議の準備 6/25 15:00  ← 日付＋時刻もつけられる',
        '　例）今日17時に小室くんにチラシの件伝える  ← 自然な文中の日付・時刻もOK',
        '　（今日／明日／明後日、HH時MM分／HH時／HH:MM に対応）',
        '　※ 何かの返信待ち（振り返りの催促直後など）と誤認識されて意図と違う扱いになった場合は、',
        '　　先頭に「タスク 」をつけて送ると必ずタスク追加として扱われます（例：タスク 牛乳を買う 6/25）',
        '・一覧 → 未完了タスクを番号つきで表示',
        '　番号だけで操作できます（一覧・通知の番号は12時間有効）',
        '　例）1削除／5ペンディング／3完了／2着手中／4対応待ち',
        '　例）1削除 5ペンディング　→ まとめて指定もOK',
        '　（「削除」はゴミ箱へ移すだけです。完全に消すのはアプリの「🗑 ゴミ箱」タブから）',
        '　例）1明日／2を7/30　→ 番号で期限だけ変更（リスケ）',
        '　例）1 15時／2は10時30分　→ 番号で時間を設定',
        '　例）3 明日10時　→ 番号で期限と時間をまとめて設定',
        '　例）3番のタイトルを〜に書き換えて　→ 細かい指示は「3番の〜」と続けて',
        '・通知 → 今の期限リマインドを番号つきで表示（そのまま「1完了」などで操作可）',
        '・日報 → 今日の完了件数・今日が期限のもの・期限切れ・ペースを表示',
        '・週報 → 直近1週間の完了件数とペース、前週比、積み残しを表示',
        '　（どちらも番号つきなので、その場で「1完了」「1明日」と返せます）',
        '・今日決める → 必ず今日ぶんのやる候補を番号つきで表示（「1,3,5」と番号を返して確定）',
        '・明日決める → 明日ぶんのやる候補を同じ形で表示',
        f'　（「予定を決める」だけだと、{planning_mod.PLAN_ROLLOVER_HOUR}時より前はその日ぶん、'
        f'{planning_mod.PLAN_ROLLOVER_HOUR}時以降は翌日ぶんになります）',
        '・今日の予定 → 今日やると決めたぶんのうち、まだ終わっていないものを番号つきで表示',
        '　（そのまま「1完了」「1明日」のように番号で操作できます）',
        '・カレンダー → Googleカレンダーの今日・明日の予定と空き時間を表示',
        '　例）明日14時から16時 仕込み をカレンダーに入れて　→ 予定を追加（「はい」で確定）',
        '　例）毎週月曜10時に定例MTG をカレンダーに入れて　→ 繰り返しの予定を追加',
        '　例）明日のバイトを17時からに変更して　→ 予定の時間を変更',
        '　例）〇〇の通知を15分前にして　→ その予定の通知タイミングを変更',
        '　例）31日の神田をキャンセルして　→ 予定を削除',
        '　（カレンダーの追加・変更・削除は、実行前に必ず確認が入ります。繰り返しの予定は指定した1回分だけが変更・削除されます）',
        '・相談・アドバイス → AIコーチに進捗評価を聞く',
        '　（「〜どう？」のような疑問文もAIコーチが拾って回答します）',
        '・AIコーチにはタスクの分解・状態変更・削除・優先度変更・期限変更も頼めます',
        '　例）会議資料の準備を分解して　→ 提案が来たら「はい」で追加',
        '　例）牛乳を買うを完了にして　→ その場で状態を変更',
        '　例）牛乳を買うを削除して　→ ゴミ箱へ移動（アプリの「🗑 ゴミ箱」から元に戻せます）',
        '　例）牛乳を買うの重要度を上げて　→ その場で重要度を変更（高／低の2段階）',
        '　例）牛乳を買うの期限を7/1にして　→ その場で期限を変更',
        '　例）牛乳を買うを牛乳とパンを買うに書き換えて　→ その場でタスク内容を変更',
        '　（対象タスクはタイトルを全部書かなくてもOK。「うなぎの方完了にして」のようにキーワードだけでも、候補が1つに絞れればAIが判断します）',
        '　例）このタスクやる意味ある？　→ 率直な意見を返します',
        '　例）要約して／言い換えて　→ タスク状況を簡潔にまとめて返します',
        '・長い文章や箇条書きを送ると、AIが要点だけのタスク名に整理して追加します',
        '・期限を指定せずに追加すると、その場で期限を聞かれます（不要なら「なし」）',
        '・今日／明日／明後日の期限で時刻を指定しなかった場合、その場で時刻も聞かれます（不要なら「なし」）',
        '・期限が未設定のタスクは、毎晩の日報のあとに「何件あるか」だけお知らせします'
        '（まとめて決めるのはアプリのほうが速いためです）',
        '・YouTubeのURLを送る、または「資料：本文」の形式でテキストを送ると、AIコーチが要点を要約して覚え、以降の相談で参考にします',
        '・資料一覧 → 登録済みの資料を確認',
        '　例）〇〇の資料を△△に直して　→ 資料の内容をAIが書き直し（理解の相違があった時など）',
        '・振り返り：本文　の形式で送ると、その日の一言メモとして記録します',
        '　例）振り返り：今日はうなぎの仕込みが予定より早く終わった',
        '　（週次レポートや相談時にAIコーチが参考にします）',
        '',
        '💰 財務管理（株式会社ワンバーグ）',
        '・収支／売上 → 今日の売上・費用・当日損益と、当月の累計を表示',
        '・今月の収支 → 当月の売上・費用（カテゴリ別内訳つき）・損益を表示',
        '・週次実績 → 今週（月〜土）の売上・客数・鰻/ご飯の使用量・人件費・支出をまとめて表示',
        '・まとめて入力するときはアプリが速いです（16時の通知のリンクからその日が開きます）',
        '・1件だけならLINEで直接どうぞ（金額は税抜。売上だけ税込）',
        '　例）売上 52000',
        '　例）食材 お米 1000／食材 肉のハナマサ お米 1000',
        '　例）備品 シモジマ タレ瓶 500／経費 ATM手数料 110',
        '　例）シフト 都丸 10:00 14:30',
        '　例）8/5 売上 52000　← 先頭に日付で別の日',
        '　例）複数行をまとめて送っても登録できます',
        '・取り消し → 直前の入力を1件戻す',
        '・テンプレ → 記入用のひな形（LINEだけで済ませたいとき）',
        '・入力ヘルプ → 書き方の一覧',
        '',
        '・定期タスク → 登録済みの繰り返し予定を表示',
        '・定期タスク（毎日／毎週〇曜／毎月〇日）はLINEで登録・変更・削除できます',
        '　例）毎週月曜19時に週報を出すのを定期タスクにして　→ その場で登録',
        '　例）毎月末日に家賃を振り込むのを定期タスクとして登録して',
        '　例）週報を出すのリマインド時間を20時にして　→ 時刻だけ変更',
        '　例）家賃振込を毎月25日に変更して　→ 周期・日にちを変更',
        '　例）家賃振込を削除して　→ 定期タスクを削除',
        '　（登録済みの定期タスクは「一覧」の中で別メッセージとして表示されます。ボードへの自動追加はされません）',
        '　※ 定期タスクの自動リマインドはいまは停止中です。登録・確認・変更・削除はこれまでどおりできます',
        '',
        '⏰ 自動で届くもの（「明日何をやるか・今日何をやるか」を決めることだけに絞っています）',
        f'・夕方{planning_mod.PLAN_PROMPT_AT[0]}時：明日やることの候補（番号で選んで確定）',
        f'・朝{planning_mod.PLAN_MORNING_AT[0]}時：決めたぶんを「今日やること」として送る',
        '　（前日に決めていなければ、この時間に今日ぶんの候補が出ます）',
        f'・夜{planning_mod.PLAN_REVIEW_AT[0]}時：その日の答え合わせ（明日ぶんが未定ならここで一緒に促します）',
        f'・毎晩{config.AGENT_HOUR}時：日報',
        '・毎週日曜21時：週報',
        '・毎日16時：その日の入力リマインド（未入力の項目＋その日を開くリンク）',
        '・毎晩22時：その日の収支（売上・費用・損益と当月累計）',
        '・毎週土曜17時：週次実績（売上・客数・鰻/ご飯の使用量・人件費・支出）',
        f'・時刻つきタスクは開始{config.TIME_LEAD_MINUTES}分前にもリマインド',
        '',
        '（毎朝の期限リマインド・振り返りの催促・定期タスクの通知は、'
        '同じ顔ぶれが並ぶだけで読み飛ばすようになっていたため廃止しました。'
        '「通知」「振り返り」「定期タスク」と送れば今でも見られます）',
    ]
    return '\n'.join(lines)
