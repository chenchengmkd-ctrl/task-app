"""優先順位の判断ロジック（ダブルマトリックス）。

木下勝寿『時間最短化、成果最大化の法則』法則3「優先順位のダブルマトリックスの法則」を
実際の並び替え・助言に組み込むためのモジュール。考え方は次の2段階。

1段階目（重要度×緊急度）……緊急度より「重要度」を優先する
    重要度＝結果に対して影響度の大きなもの／緊急度＝今すぐ対処を求められるもの
    重要高×緊急高→1、重要高×緊急低→2、重要低×緊急高→3、重要低×緊急低→4（やらない検討）
    緊急度を優先すると「忙しいが成果は低い人」になるため、重要度を先に見る。

2段階目（1段階目の順位×かかる時間）……すぐ終わるものを先にやる
    重要度＆緊急度が低くても、短時間で終わるものは先に片づけたほうが成果は上がる。
    （こなせる案件数が増える／「発生→タスク管理→実行」が「発生→実行」になり管理の手間が減る／
      次の仕事が生まれ全体最適になる／記憶が鮮明なうちにやるので精度が高い）

完了時間の目安（本文より）
    10分以内→今すぐ、30分以内→今日中、1時間以内→明日中、1日以内→2週間以内でやる日を決める、
    それ以上かかりすぐやる価値がないもの→「やらない」と決める
"""
import re

# 「短時間」と見なす上限（分）。本の目安では1時間以内なら明日中には片づける範囲。
SHORT_TASK_MINUTES = 60
# この時間以内で終わるものは、順位に関わらず「今すぐやる」と促す。
DO_NOW_MINUTES = 10

# AIへ渡す共通の判断基準。プロンプトの言い回しを1か所に集約しておく。
DOUBLE_MATRIX_RULE = (
    '【優先順位の判断基準：ダブルマトリックス】\n'
    '1段階目（重要度×緊急度）：緊急度より「重要度」を優先する。'
    '重要度とは結果への影響度が大きいこと、緊急度とは今すぐ対処を求められること。'
    '重要度高×緊急度高＞重要度高×緊急度低＞重要度低×緊急度高＞重要度低×緊急度低（最後はやらないことも検討する）。'
    '緊急度を優先すると「忙しいが成果は低い」状態になるため、重要度を先に見ること。\n'
    '2段階目（1段階目の順位×かかる時間）：重要度・緊急度が低くても、すぐ終わるものは先に片づける。'
    '短時間で終わるものを先にやるほうが、こなせる件数が増え、タスク管理の手間が減り、'
    '記憶が鮮明なうちに実行できるので精度も上がる。\n'
    '完了時間の目安：10分以内→今すぐ、30分以内→今日中、1時間以内→明日中、'
    '1日以内→2週間以内でやる日を決める、それ以上かかりすぐやる価値がないもの→「やらない」と決める。'
)


_UNIT_MINUTES = {'分': 1, 'ふん': 1, 'min': 1, 'm': 1,
                 '時間': 60, 'h': 60, 'hr': 60,
                 '日': 480, 'd': 480}          # 1日＝実働8時間として換算


def parse_estimate_minutes(text):
    """「30分」「1時間半」「2h」「半日」などの見積り表記を分に直す。読めなければNone。"""
    if not text:
        return None
    s = str(text).strip().translate(str.maketrans('０１２３４５６７８９．', '0123456789.'))
    if re.search(r'半日', s):
        return 240
    # 「終日」「一日」は数字が無いのでここで拾う。「1日」は下の数字＋単位の解析に任せる
    # （ここで拾うと「21日」の中の「1日」に誤って一致してしまうため）
    if re.search(r'終日|一日', s):
        return 480

    total = 0
    found = False
    for num, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(時間|分|ふん|日|hours?|hrs?|h|mins?|m|d)', s, re.IGNORECASE):
        key = unit.lower()
        if key.startswith('hour') or key.startswith('hr'):
            key = 'h'
        elif key.startswith('min'):
            key = 'min'
        mult = _UNIT_MINUTES.get(unit) or _UNIT_MINUTES.get(key)
        if not mult:
            continue
        total += float(num) * mult
        found = True
    # 「1時間半」の“半”は30分として足す
    if found and re.search(r'時間半', s):
        total += 30
    if found:
        return int(total)
    # 単位なしの数字だけ（「30」など）は分とみなす
    only = re.fullmatch(r'\s*(\d+)\s*', s)
    return int(only.group(1)) if only else None


def urgency_high(due_iso, today_iso, soon_days=3):
    """緊急度が高いか。期限切れ〜soon_days日以内なら高、それ以外・期限なしは低。"""
    if not due_iso:
        return False
    from . import config
    d, today = config.parse_date(due_iso), config.parse_date(today_iso)
    if not d or not today:
        return False
    return (d - today).days <= soon_days


def first_matrix_rank(priority, is_urgent):
    """1段階目（重要度×緊急度）の順位。1が最優先、4は「やらない」検討。"""
    important = priority == 'high'
    if important:
        return 1 if is_urgent else 2
    return 3 if is_urgent else 4


def double_matrix_rank(priority, is_urgent, minutes):
    """2段階目まで含めた総合順位（1〜4）。図表5の並びに対応する。

    重要度＆緊急度が高い＝1段階目が1・2、低い＝3・4。かかる時間はSHORT_TASK_MINUTESで短長を分ける。
    見積り未設定は「長い」と決めつけず、短時間側の恩恵だけ与えないよう長時間扱いにしておく。
    """
    first = first_matrix_rank(priority, is_urgent)
    high_value = first <= 2
    is_short = minutes is not None and minutes <= SHORT_TASK_MINUTES
    if is_short:
        return 1 if high_value else 2
    return 3 if high_value else 4


def sort_key(task, today_iso):
    """タスク一覧をダブルマトリックス順に並べるためのキー。

    task は title/priority/due/estimate を持つ辞書。総合順位→1段階目の順位→かかる時間の短い順→期限の近い順。
    """
    minutes = parse_estimate_minutes(task.get('estimate'))
    is_urgent = urgency_high(task.get('due'), today_iso)
    priority = task.get('priority') or 'mid'
    first = first_matrix_rank(priority, is_urgent)
    total = double_matrix_rank(priority, is_urgent, minutes)
    return (total, first, minutes if minutes is not None else 10 ** 6, task.get('due') or '9999-99-99')


def deadline_guideline(minutes):
    """完了時間の目安から「いつまでにやるか」の指針を返す。見積り不明はNone。"""
    if minutes is None:
        return None
    if minutes <= DO_NOW_MINUTES:
        return '今すぐ'
    if minutes <= 30:
        return '今日中'
    if minutes <= 60:
        return '明日中'
    if minutes <= 480:
        return '2週間以内（やる日を決める）'
    return None


def rank_label(total_rank):
    """総合順位を人が読める短い説明にする。"""
    return {
        1: '最優先（重要＆すぐ終わる）',
        2: '先に片づける（すぐ終わる）',
        3: '腰を据えてやる（重要だが時間がかかる）',
        4: 'やらない検討（重要度も低く時間もかかる）',
    }.get(total_rank, '')
