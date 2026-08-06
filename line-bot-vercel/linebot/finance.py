"""財務管理アプリ（oneburg-finance）の売上・損益をLINEに通知する。

データは同じSupabaseプロジェクトの birdmen_kv テーブル（key/value）に入っている。
Webアプリ側（oneburg-finance/src/utils/storage.ts）が書いた JSON をそのまま読むだけで、
このボットからは書き込まない（通知専用）。

金額の扱いはWebアプリ側と揃えてある：
- LineItem.amount は常に税込
- 売上 = 現金売上 + 法人入金 + 個人入金（現金→法人の内部振替 toBank は二重計上になるので含めない）
- 費用 = 全バケットの引出明細をカテゴリ別に集計
- 損益 = 売上 − 費用
"""
from urllib.parse import quote

from . import config
from .supabase_client import get_supabase

TABLE = 'birdmen_kv'
PREFIX = 'birdmen:'

CATEGORY_LABEL = {
    'ingredient': '食材',
    'supplies': '備品',
    'labor': '人件費',
    'rent': '家賃',
    'utility': '光熱費',
    'other': 'その他',
}

APP_URL = 'https://oneburg-finance.vercel.app'


def _report(date_iso):
    """指定日の残高報告を返す。無ければNone。"""
    key = f'{PREFIX}report:{date_iso}'
    rows = get_supabase(TABLE, f'key=eq.{quote(key)}&select=value')
    return rows[0].get('value') if rows else None


def _month_reports(month):
    """指定月（YYYY-MM）の残高報告を日付順で返す。"""
    like = f'{PREFIX}report:{month}'
    rows = get_supabase(TABLE, f'key=like.{quote(like)}*&select=key,value')
    reports = [r.get('value') for r in rows if r.get('value')]
    return sorted(reports, key=lambda r: r.get('date', ''))


def _sum_items(items):
    return sum(int(i.get('amount') or 0) for i in (items or []))


def _revenue(report):
    cash = report.get('cash') or {}
    corp = report.get('corp') or {}
    pers = report.get('pers') or {}
    return (
        int(cash.get('sales') or 0)
        + _sum_items(corp.get('deposits'))
        + _sum_items(pers.get('deposits'))
    )


def _expense_by_category(report):
    totals = {k: 0 for k in CATEGORY_LABEL}
    for bucket in ('corp', 'pers', 'cash'):
        day = report.get(bucket) or {}
        for item in (day.get('withdraws') or []):
            cat = item.get('category') or 'other'
            totals[cat] = totals.get(cat, 0) + int(item.get('amount') or 0)
    return totals


def _totals(report):
    revenue = _revenue(report)
    by_cat = _expense_by_category(report)
    expense = sum(by_cat.values())
    return revenue, expense, revenue - expense, by_cat


def _yen(n):
    return f'{n:,}円'


def _signed(n):
    return f'{"+" if n >= 0 else "-"}{abs(n):,}円'


def build_daily_finance_report(date_iso=None):
    """当日の売上・費用・損益と、当月の累計をまとめたテキストを返す。"""
    date_iso = date_iso or config.today_iso()
    month = date_iso[:7]

    report = _report(date_iso)
    lines = [f'📊 {config.jp(date_iso)} の収支']

    if not report:
        lines.append('')
        lines.append('この日の入力はまだありません。')
    else:
        revenue, expense, profit, by_cat = _totals(report)
        lines.append('')
        lines.append(f'売上　{_yen(revenue)}')
        lines.append(f'費用　{_yen(expense)}')
        breakdown = [f'{CATEGORY_LABEL[k]} {v:,}' for k, v in by_cat.items() if v]
        if breakdown:
            lines.append('　（' + '／'.join(breakdown) + '）')
        lines.append(f'当日損益　{_signed(profit)}')

    reports = _month_reports(month)
    if reports:
        m_rev = m_exp = 0
        for r in reports:
            rev, exp, _, _ = _totals(r)
            m_rev += rev
            m_exp += exp
        lines.append('')
        lines.append(f'▼ {int(month[5:7])}月の累計（{len(reports)}日分）')
        lines.append(f'売上　{_yen(m_rev)}')
        lines.append(f'費用　{_yen(m_exp)}')
        lines.append(f'損益　{_signed(m_rev - m_exp)}')

    lines.append('')
    lines.append(APP_URL)
    return '\n'.join(lines)


def build_month_finance_report(month=None):
    """当月の累計と、費用のカテゴリ別内訳を返す。"""
    month = month or config.today_iso()[:7]
    reports = _month_reports(month)
    if not reports:
        return f'📊 {int(month[5:7])}月の入力はまだありません。\n{APP_URL}'

    m_rev = m_exp = 0
    totals = {k: 0 for k in CATEGORY_LABEL}
    for r in reports:
        rev, exp, _, by_cat = _totals(r)
        m_rev += rev
        m_exp += exp
        for k, v in by_cat.items():
            totals[k] = totals.get(k, 0) + v

    lines = [f'📊 {int(month[5:7])}月の収支（{len(reports)}日分）', '']
    lines.append(f'売上　{_yen(m_rev)}')
    lines.append(f'費用　{_yen(m_exp)}')
    for k, label in CATEGORY_LABEL.items():
        if totals.get(k):
            lines.append(f'　{label}　{totals[k]:,}円')
    lines.append('')
    lines.append(f'損益　{_signed(m_rev - m_exp)}')
    lines.append('')
    lines.append(APP_URL)
    return '\n'.join(lines)


def send_finance_report(push_text, get_users):
    """cronから呼ぶ。当日の収支を全ユーザーにpushする。"""
    body = build_daily_finance_report()
    for uid in get_users():
        push_text(uid, body)
