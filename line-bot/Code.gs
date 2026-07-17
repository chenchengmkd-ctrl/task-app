/**
 * LINE連携 — ①期限リマインド通知 ＋ ②チャットでタスク追加
 * Google Apps Script + LINE Messaging API
 *
 * ・タスク／定期タスクはSupabase（tasks／recurringテーブル）を直接参照する
 * ・LINEで追加したタスクも、その場でSupabaseの tasks テーブルに直接書き込む（受信箱や取込は無し）
 * ・毎朝、期限が「期限超過/今日/明日」のタスクをまとめてLINEに自動通知
 *
 * LINEトークでの使い方:
 *   牛乳を買う           → タスク追加
 *   牛乳を買う 6/25       → 期限つきで追加（末尾に M/D）
 *   一覧                 → 未完了タスクの一覧
 *   〇〇を完了にして／削除して → AIコーチが状態変更・削除
 *   通知                 → 今の期限リマインドを表示
 *   ヘルプ               → 使い方
 */

// ===== ① 自分の値を入れる =========================================
const CHANNEL_ACCESS_TOKEN = 'ここにチャネルアクセストークンを貼り付け';
const SUPABASE_URL         = 'https://xldkfkhgazpugfuscpqt.supabase.co';
const SUPABASE_ANON_KEY    = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhsZGtma2hnYXpwdWdmdXNjcHF0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM4NDg3MzgsImV4cCI6MjA5OTQyNDczOH0.C3_TYQI8R3HeXYWTzca9erUMjpTWm2sneB7hk5Bre8Y';
const GEMINI_API_KEY       = 'ここにGemini APIキーを貼り付け';  // https://aistudio.google.com/apikey で発行（無料）
const GEMINI_MODEL         = 'gemini-3.1-flash-lite';
const REMIND_HOUR   = 8;            // 毎朝の通知時刻（時・24時間制）
const REMIND_MINUTE = 0;            // 通知時刻（分）。nearMinuteで±15分ほどに絞る
const REC_LEAD_DAYS = 4;            // 定期タスクは「何日前」から通知するか
const AGENT_HOUR = 20;              // 毎晩のAI進捗チェックイン時刻（毎朝のリマインドとは別）
const TIME_LEAD_MINUTES = 10;       // 時刻指定タスクの何分前にリマインドするか
const TIME_CHECK_INTERVAL = 5;      // 何分おきにチェックするか（installTimedReminderTriggerと合わせる）
// =================================================================

const REPLY_URL = 'https://api.line.me/v2/bot/message/reply';
const PUSH_URL  = 'https://api.line.me/v2/bot/message/push';

// 進捗状況の表示順とアイコン
const STATUS_ORDER = ['未着手', '着手中', '対応待ち', 'ペンディング'];
const STATUS_ICON  = { '未着手': '⚪', '着手中': '🔵', '対応待ち': '🟣', 'ペンディング': '🟡' };
// Supabase（英語status）→ 表示用日本語
const STATUS_LABEL_JP = { todo: '未着手', doing: '着手中', waiting: '対応待ち', pending: 'ペンディング' };

// AIエージェントの人格（厳しめのプロマネ）
const AGENT_PERSONA = [
  'あなたは経験豊富な、やや厳しめのプロジェクトマネージャーです。',
  'ユーザーのタスク管理データ（進捗状況・期限・最終更新からの経過日数・振り返りログ）を分析し、率直かつ具体的にコメントします。',
  '進捗が悪いタスク、期限超過、長期間放置されているタスクは遠慮なく指摘してください。順調な進捗は簡潔に認めます。',
  '一般論ではなく、渡されたデータに含まれる具体的なタスク名・経過日数・期限をもとに指摘してください。',
  '常に日本語で、LINEのトーク画面に収まる分量（300字程度まで）に収め、絵文字は使っても最小限にし、要点を箇条書き中心でまとめてください。'
].join('\n');

/* ============ Webhook 入口 ============ */
function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    (body.events || []).forEach(handleEvent);
  } catch (err) {
    console.error(err);
  }
  return ContentService.createTextOutput(JSON.stringify({ ok: true }))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleEvent(ev) {
  if (ev.type !== 'message' || ev.message.type !== 'text') return;
  const userId = ev.source.userId;
  rememberUser(userId);                       // 通知用にユーザーを記憶
  const reply = routeCommand(userId, ev.message.text.trim());
  replyText(ev.replyToken, reply);
}

function routeCommand(userId, text) {
  // 保留中のサブタスク提案への「はい/いいえ」を最優先で処理
  const pendingReply = handlePendingSubtaskReply(userId, text);
  if (pendingReply !== null) return pendingReply;

  if (/^(一覧|リスト|list)$/i.test(text)) return listAll();
  if (/^(通知|リマインド|今日)$/i.test(text)) return buildReminder() || '📭 期限が近い（超過・今日・明日）のタスクはありません。';
  if (/^(ヘルプ|使い方|help)$/i.test(text)) return helpText();
  if (/^(相談|アドバイス)$/i.test(text)) return askAgent(userId, '最近のタスク状況について、率直な進捗評価とアドバイスをください。');
  if (!text) return '空メッセージです。「ヘルプ」で使い方を表示します。';
  if (isQuestionLike(text)) return askAgent(userId, text);
  return addLine(text);
}
// 疑問文・相談・タスク分解／状態変更／削除の依頼っぽい文かどうかをざっくり判定（タスク追加との区別用）
function isQuestionLike(text) {
  if (/[?？]$/.test(text)) return true;
  if (/(か|かな|かしら)[。.!！]?$/.test(text)) return true;
  if (/(どう|教えて|大丈夫|やばい|相談|アドバイス|優先|分解|やる意味|意味ある|完了に|着手中に|対応待ちに|ペンディングに|状態を|ステータスを|削除して|消して)/.test(text)) return true;
  return false;
}

/* ============ タスク追加・一覧 ※すべてSupabase直接操作 ============ */
// タスクをその場でSupabaseに追加する（受信箱を介さず即反映）
function addLine(text) {
  // 「今日17時に」「6/25 15:00」のような日付・時刻表現を検出して取り除く
  const parsed = extractDateTime_(text);
  const title = parsed.title;
  const due = parsed.due, dueTime = parsed.dueTime;

  const row = {
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    title, status: 'todo', priority: 'mid', due: due || null, due_time: dueTime || null,
    estimate: '', recurrence: 'none', note: '', tags: [],
    done: false, deleted: false, from_line: true,
    updated_at: new Date().toISOString()
  };
  if (!postSupabase('tasks', [row])) return '⚠️ タスクの追加に失敗しました。時間をおいて再度お試しください。';

  const cnt = getSupabase('tasks', 'done=eq.false&deleted=eq.false&select=id').length;
  let msg = `✅ 追加しました\n「${title}」`;
  if (due) msg += `\n📅 ${jp(due)}` + (dueTime ? ' ' + dueTime : '');
  msg += `\n\n未完了: ${cnt}件`;
  return msg;
}
// 未完了タスク＋定期タスクを「進捗状況ごと」に表示
function listAll() {
  const app = getAppTasksAll();
  const rec = getRecurring();
  if (!app.length && !rec.length) return '🎉 未完了のタスクはありません。';

  let msg = '📋 タスク一覧\n';

  // 進捗状況ごとにグループ表示
  STATUS_ORDER.forEach(st => {
    const arr = app.filter(t => t.status === st);
    if (!arr.length) return;
    msg += '\n' + STATUS_ICON[st] + ' ' + st + '（' + arr.length + '）\n' +
      arr.map(t => '・' + t.title + (t.due ? '（' + jp(t.due) + '）' : '')).join('\n') + '\n';
  });

  // 定期タスク
  if (rec.length) {
    msg += '\n🔁 定期タスク（' + rec.length + '）\n' +
      rec.map(r => '・' + r.title + '（' + r.recurrence + (r.next ? '・次回 ' + jp2(r.next) : '') + '）').join('\n') + '\n';
  }
  return msg.trim();
}
// アプリの未完了タスクを全部取得（状態つき・期限の有無は問わない）※Supabase参照
function getAppTasksAll() {
  const rows = getSupabase('tasks', 'done=eq.false&deleted=eq.false&select=title,status,due');
  return rows.map(r => ({
    title: String(r.title),
    due: r.due || '',
    status: STATUS_LABEL_JP[r.status] || r.status || '未着手'
  }));
}
// 定期タスクを取得 ※Supabase参照
function getRecurring() {
  const rows = getSupabase('recurring', 'select=title,recurrence,next_date');
  return rows.map(r => ({
    title: String(r.title),
    recurrence: String(r.recurrence || ''),
    next: parseDate(r.next_date)
  }));
}

/* ============ リマインド ============ */
// アプリの期限ありの未完了タスクを取得 ※Supabase参照
function getDueFromApp() {
  const rows = getSupabase('tasks', 'done=eq.false&deleted=eq.false&due=not.is.null&select=title,status,due');
  const out = [];
  rows.forEach(r => {
    const due = parseDate(r.due);
    if (!r.title || !due) return;
    out.push({ title: String(r.title), due, status: STATUS_LABEL_JP[r.status] || r.status || '未着手' });
  });
  return out;
}
// 通知メッセージを組み立て（対象が無ければ空文字）
// ・通常タスク: 期限が「超過/今日/明日」を、進捗状況ごとに表示
// ・定期タスク: 次回予定が REC_LEAD_DAYS 日以内を表示
function buildReminder() {
  const today = midnight(new Date());

  // 期限が近い通常タスク（状態つき）を集める
  const near = [];
  getDueFromApp().forEach(t => {
    const d = dayDiff(today, t.due);
    if (d <= 1) near.push({ title: t.title, due: t.due, status: t.status, d });
  });

  // もうすぐの定期タスク（3〜4日前から）
  const recSoon = getRecurring().filter(r => {
    if (!r.next) return false;
    const d = dayDiff(today, r.next);
    return d >= 0 && d <= REC_LEAD_DAYS;
  }).sort((a, b) => dayDiff(today, a.next) - dayDiff(today, b.next));

  if (!near.length && !recSoon.length) return '';

  let msg = `🔔 タスクのリマインド (${today.getMonth() + 1}/${today.getDate()})\n`;

  // 進捗状況ごとに表示（各行に期日も）
  STATUS_ORDER.forEach(st => {
    const arr = near.filter(t => t.status === st).sort((a, b) => a.d - b.d);
    if (!arr.length) return;
    msg += '\n' + STATUS_ICON[st] + ' ' + st + '\n' +
      arr.map(t => '・' + t.title + '（' + dueTag(t.d, t.due) + '）').join('\n') + '\n';
  });

  // 定期タスク
  if (recSoon.length) {
    msg += '\n🔁 定期タスク（' + REC_LEAD_DAYS + '日以内）\n' +
      recSoon.map(r => '・' + r.title + '（' + r.recurrence + '・' + dueTag(dayDiff(today, r.next), r.next) + '）').join('\n') + '\n';
  }
  return msg.trim();
}
// 今日との日数差（0=今日, 1=明日, -1=昨日）
function dayDiff(today, dateObj) {
  return Math.round((midnight(dateObj) - today) / 86400000);
}
// 期日を「今日/明日/N日後/N日超過 M/D」の形に
function dueTag(d, dateObj) {
  const md = (dateObj.getMonth() + 1) + '/' + dateObj.getDate();
  if (d < 0) return '⚠' + (-d) + '日超過 ' + md;
  if (d === 0) return '今日 ' + md;
  if (d === 1) return '明日 ' + md;
  return d + '日後 ' + md;
}
// 毎朝のトリガーから呼ばれる
function sendReminders() {
  const msg = buildReminder();
  if (!msg) return;
  getUsers().forEach(uid => pushText(uid, msg));
}

/* ============ 時刻指定タスクの直前リマインド ============ */
// 今日の日付（YYYY-MM-DD、スクリプトのタイムゾーン基準）
function todayISO_() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
// 「YYYY-MM-DD」＋「HH:MM」からDateを作る
function dateTimeFromISO_(dateISO, hhmm) {
  const [y, m, d] = dateISO.split('-').map(Number);
  const [hh, mm] = hhmm.split(':').map(Number);
  return new Date(y, m - 1, d, hh, mm);
}
// 今日が期日で、時刻が設定されている未完了タスクを集める（アプリ分＋LINE分）
function getTimedTasksToday_() {
  const todayStr = todayISO_();
  const out = [];
  getSupabase('tasks',
    'done=eq.false&deleted=eq.false&due=eq.' + todayStr + '&due_time=not.is.null&select=id,title,due_time'
  ).forEach(r => {
    if (r.due_time) out.push({ id: r.id, title: r.title, dueTime: String(r.due_time).slice(0, 5) });
  });
  return out;
}
// 時刻の TIME_LEAD_MINUTES 分前になったタスクをLINEに通知（重複送信は防ぐ）
function checkTimedReminders() {
  const todayStr = todayISO_();
  const now = new Date();
  const items = getTimedTasksToday_();
  if (!items.length) return;

  const propKey = 'TIME_REMINDED_' + todayStr;
  const p = PropertiesService.getScriptProperties();
  let reminded = [];
  try { reminded = JSON.parse(p.getProperty(propKey) || '[]'); } catch (e) {}

  const due = [];
  items.forEach(it => {
    if (reminded.indexOf(it.id) >= 0) return;
    const target = dateTimeFromISO_(todayStr, it.dueTime);
    const diffMin = (target - now) / 60000;
    // 「TIME_LEAD_MINUTES分前」の到来を、チェック間隔ぶんの幅で捕まえる
    if (diffMin <= TIME_LEAD_MINUTES && diffMin > TIME_LEAD_MINUTES - TIME_CHECK_INTERVAL) {
      due.push(it);
      reminded.push(it.id);
    }
  });
  if (!due.length) return;

  const msg = `⏰ もうすぐ開始（${TIME_LEAD_MINUTES}分後）\n` +
    due.map(it => '・' + it.title + '（' + it.dueTime + '〜）').join('\n');
  getUsers().forEach(uid => pushText(uid, msg));
  p.setProperty(propKey, JSON.stringify(reminded));
  cleanupOldTimeReminderProps_(todayStr);
}
// 過去の日付のリマインド済み記録を消す（プロパティが増え続けないように）
function cleanupOldTimeReminderProps_(todayStr) {
  const p = PropertiesService.getScriptProperties();
  const all = p.getProperties();
  Object.keys(all).forEach(k => {
    if (k.indexOf('TIME_REMINDED_') === 0 && k !== 'TIME_REMINDED_' + todayStr) p.deleteProperty(k);
  });
}
// 数分おきの自動チェック・トリガーを作成
function installTimedReminderTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'checkTimedReminders') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('checkTimedReminders').timeBased().everyMinutes(TIME_CHECK_INTERVAL).create();
}
// テスト：今すぐ時刻指定タスクをチェックする
function testTimedReminders() {
  checkTimedReminders();
  Logger.log('時刻指定リマインドをチェックしました（対象があれば届きます）');
}

/* ============ ユーザー記憶（通知先） ============ */
function rememberUser(userId) {
  if (!userId) return;
  const p = PropertiesService.getScriptProperties();
  let ids = [];
  try { ids = JSON.parse(p.getProperty('USER_IDS') || '[]'); } catch (e) {}
  if (ids.indexOf(userId) < 0) { ids.push(userId); p.setProperty('USER_IDS', JSON.stringify(ids)); }
}
function getUsers() {
  try { return JSON.parse(PropertiesService.getScriptProperties().getProperty('USER_IDS') || '[]'); }
  catch (e) { return []; }
}

/* ============ LINE送信 ============ */
function replyText(token, text) {
  UrlFetchApp.fetch(REPLY_URL, {
    method: 'post', contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + CHANNEL_ACCESS_TOKEN },
    payload: JSON.stringify({ replyToken: token, messages: [{ type: 'text', text: text }] }),
    muteHttpExceptions: true
  });
}
function pushText(to, text) {
  UrlFetchApp.fetch(PUSH_URL, {
    method: 'post', contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + CHANNEL_ACCESS_TOKEN },
    payload: JSON.stringify({ to: to, messages: [{ type: 'text', text: text }] }),
    muteHttpExceptions: true
  });
}

/* ============ 補助 ============ */
function helpText() {
  return [
    '📖 使い方',
    '',
    '・文章を送る → タスク追加',
    '　例）牛乳を買う',
    '　例）牛乳を買う 6/25  ← 末尾に日付で期限つき',
    '　例）会議の準備 6/25 15:00  ← 日付＋時刻もつけられる',
    '　例）今日17時に小室くんにチラシの件伝える  ← 自然な文中の日付・時刻もOK',
    '　（今日／明日／明後日、HH時MM分／HH時／HH:MM に対応）',
    '・一覧 → 未完了タスクをまとめて表示',
    '・通知 → 今の期限リマインドを表示',
    '・相談・アドバイス → AIコーチに進捗評価を聞く',
    '　（「〜どう？」のような疑問文もAIコーチが拾って回答します）',
    '・AIコーチにはタスクの分解・状態変更・削除も頼めます',
    '　例）会議資料の準備を分解して　→ 提案が来たら「はい」で追加',
    '　例）牛乳を買うを完了にして　→ その場で状態を変更',
    '　例）牛乳を買うを削除して　→ その場で削除',
    '　例）このタスクやる意味ある？　→ 率直な意見を返します',
    '',
    `毎朝${REMIND_HOUR}時に期限リマインド、毎晩${AGENT_HOUR}時にAIコーチの進捗チェックインを自動送信します。`,
    `時刻つきタスクは、開始${TIME_LEAD_MINUTES}分前にも別途リマインドします。`
  ].join('\n');
}
function parseDate(v) {
  if (!v) return null;
  if (Object.prototype.toString.call(v) === '[object Date]') return midnight(v);
  const m = String(v).trim().match(/^(\d{4})[-\/](\d{1,2})[-\/](\d{1,2})/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
function midnight(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
function toISOThisYear(mm, dd) {
  const y = new Date().getFullYear(), p = n => String(n).padStart(2, '0');
  return y + '-' + p(mm) + '-' + p(dd);
}
function jp(iso) { const d = parseDate(iso); return d ? (d.getMonth() + 1) + '/' + d.getDate() : iso; }
function jp2(d) { return (d.getMonth() + 1) + '/' + d.getDate(); }

// テキストの中から日付・時刻の表現を検出して取り除く。
// 対応: 今日／明日／明後日、M/D、HH時MM分／HH時／HH:MM（文中どこにあってもよい）
function extractDateTime_(text) {
  let title = text, due = '', dueTime = '';

  // 日付：相対表現（今日/明日/明後日）を優先、なければ M/D
  const REL_DAYS = { '今日': 0, '明日': 1, '明後日': 2 };
  let dateMatch = title.match(/(今日|明日|明後日)(に|の)?/);
  if (dateMatch) {
    const d = new Date();
    d.setDate(d.getDate() + REL_DAYS[dateMatch[1]]);
    due = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    title = title.replace(dateMatch[0], '');
  } else {
    dateMatch = title.match(/(\d{1,2})\/(\d{1,2})(に|の)?/);
    if (dateMatch) {
      due = toISOThisYear(dateMatch[1], dateMatch[2]);
      title = title.replace(dateMatch[0], '');
    }
  }

  // 時刻：HH時MM分 → HH時 → HH:MM の順に1つだけ検出
  let timeMatch = title.match(/(\d{1,2})時(\d{1,2})分(に|の)?/);
  if (timeMatch) {
    dueTime = String(timeMatch[1]).padStart(2, '0') + ':' + String(timeMatch[2]).padStart(2, '0');
    title = title.replace(timeMatch[0], '');
  } else {
    timeMatch = title.match(/(\d{1,2})時(に|の)?/);
    if (timeMatch) {
      dueTime = String(timeMatch[1]).padStart(2, '0') + ':00';
      title = title.replace(timeMatch[0], '');
    } else {
      timeMatch = title.match(/(\d{1,2}):(\d{2})(に|の)?/);
      if (timeMatch) {
        dueTime = String(timeMatch[1]).padStart(2, '0') + ':' + String(timeMatch[2]).padStart(2, '0');
        title = title.replace(timeMatch[0], '');
      }
    }
  }

  title = title.replace(/^[、,\s]+/, '').replace(/[、,\s]+$/, '').replace(/\s{2,}/g, ' ').trim();
  return { title, due, dueTime };
}

/* ============ Supabase ヘルパー ============ */
function getSupabase(table, params) {
  const url = SUPABASE_URL + '/rest/v1/' + table + '?' + params;
  const res = UrlFetchApp.fetch(url, {
    headers: {
      'apikey': SUPABASE_ANON_KEY,
      'Authorization': 'Bearer ' + SUPABASE_ANON_KEY,
      'Accept': 'application/json'
    },
    muteHttpExceptions: true
  });
  if (res.getResponseCode() !== 200) {
    console.error('Supabase error', res.getResponseCode(), res.getContentText());
    return [];
  }
  return JSON.parse(res.getContentText());
}
// Supabaseへ新規行を挿入
function postSupabase(table, rows) {
  const res = UrlFetchApp.fetch(SUPABASE_URL + '/rest/v1/' + table, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'apikey': SUPABASE_ANON_KEY,
      'Authorization': 'Bearer ' + SUPABASE_ANON_KEY,
      'Prefer': 'return=minimal'
    },
    payload: JSON.stringify(rows),
    muteHttpExceptions: true
  });
  if (res.getResponseCode() >= 300) {
    console.error('Supabase insert error', res.getResponseCode(), res.getContentText());
    return false;
  }
  return true;
}
// Supabaseの行を更新（filterQueryは「id=eq.xxx」のようなPostgRESTのクエリ文字列）
function patchSupabase(table, filterQuery, body) {
  const res = UrlFetchApp.fetch(SUPABASE_URL + '/rest/v1/' + table + '?' + filterQuery, {
    method: 'patch',
    contentType: 'application/json',
    headers: {
      'apikey': SUPABASE_ANON_KEY,
      'Authorization': 'Bearer ' + SUPABASE_ANON_KEY,
      'Prefer': 'return=representation'
    },
    payload: JSON.stringify(body),
    muteHttpExceptions: true
  });
  if (res.getResponseCode() >= 300) {
    console.error('Supabase update error', res.getResponseCode(), res.getContentText());
    return null;
  }
  try { return JSON.parse(res.getContentText()); } catch (e) { return null; }
}

/* ============ AIエージェント（進捗コーチ） ※Gemini API（無料枠）使用 ============ */
// AIエージェントが使えるツール（タスク分解の提案／状態変更）— Gemini Function Calling形式
const AGENT_TOOLS = [{
  functionDeclarations: [
    {
      name: 'propose_subtasks',
      description: 'ユーザーが指定したタスクをより小さいサブタスクに分解して提案する。実際の追加はユーザーの確認後に行われるため、ここでは提案するだけでよい。',
      parameters: {
        type: 'OBJECT',
        properties: {
          parent_task_title: { type: 'STRING', description: '分解対象タスクのタイトル（タスク一覧の名称と完全一致）' },
          subtasks: { type: 'ARRAY', items: { type: 'STRING' }, description: '3〜5個程度の、具体的で実行しやすいサブタスクのタイトル' }
        },
        required: ['parent_task_title', 'subtasks']
      }
    },
    {
      name: 'update_task_status',
      description: 'ユーザーが「完了にして」「着手中にして」のように状態変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。どのタスクか曖昧な場合はツールを呼ばず、文章で確認すること。',
      parameters: {
        type: 'OBJECT',
        properties: {
          task_title: { type: 'STRING', description: '状態変更の対象タスクのタイトル（タスク一覧の名称と完全一致）' },
          new_status: { type: 'STRING', enum: ['todo', 'doing', 'waiting', 'pending', 'done'], description: '変更後の状態。完了にする場合は done' }
        },
        required: ['task_title', 'new_status']
      }
    },
    {
      name: 'delete_task',
      description: 'ユーザーが「削除して」「消して」のようにタスクの削除を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。どのタスクか曖昧な場合はツールを呼ばず、文章で確認すること。',
      parameters: {
        type: 'OBJECT',
        properties: {
          task_title: { type: 'STRING', description: '削除対象タスクのタイトル（タスク一覧の名称と完全一致）' }
        },
        required: ['task_title']
      }
    }
  ]
}];
// Gemini APIを呼び出し、応答テキストを返す（失敗時はnull）
function callGemini(systemPrompt, userText, maxTokens) {
  const data = callGeminiRaw_(systemPrompt, userText, null, maxTokens);
  if (!data) return null;
  const parts = (((data.candidates || [])[0] || {}).content || {}).parts || [];
  const textPart = parts.find(p => p.text);
  return textPart ? textPart.text : null;
}
// Gemini APIを呼び出し、レスポンス全体（functionCallを含む）を返す（失敗時はnull）
function callGeminiWithTools(systemPrompt, userText, tools, maxTokens) {
  return callGeminiRaw_(systemPrompt, userText, tools, maxTokens);
}
function callGeminiRaw_(systemPrompt, userText, tools, maxTokens) {
  const payload = {
    system_instruction: { parts: [{ text: systemPrompt }] },
    contents: [{ role: 'user', parts: [{ text: userText }] }],
    generationConfig: { maxOutputTokens: maxTokens || 800 }
  };
  if (tools) payload.tools = tools;
  const url = 'https://generativelanguage.googleapis.com/v1beta/models/' + GEMINI_MODEL + ':generateContent?key=' + GEMINI_API_KEY;
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
  if (res.getResponseCode() !== 200) {
    console.error('Gemini API error', res.getResponseCode(), res.getContentText());
    return null;
  }
  return JSON.parse(res.getContentText());
}

// タスク状況（Supabase）＋定期タスク（シート）＋直近の振り返りログをAI用にまとめる
function buildAgentContext() {
  const tasks = getSupabase('tasks',
    'done=eq.false&deleted=eq.false&select=title,status,priority,due,updated_at&order=updated_at.asc');
  const now = Date.now();

  const taskLines = tasks.map(t => {
    const days = Math.floor((now - new Date(t.updated_at).getTime()) / 86400000);
    const due = t.due ? ' 期限:' + t.due : '';
    const status = STATUS_LABEL_JP[t.status] || t.status;
    return '・' + t.title + '（状態:' + status + due + ' 優先度:' + t.priority + ' 最終更新:' + days + '日前）';
  });

  const rec = getRecurring();
  const recLines = rec.map(r => '・' + r.title + '（' + r.recurrence + (r.next ? '・次回' + jp2(r.next) : '') + '）');

  const logs = getSupabase('daily_logs', 'select=log_date,note&order=log_date.desc&limit=5');
  const logLines = logs.map(l => '・' + l.log_date + '：' + (l.note ? String(l.note).slice(0, 80) : '（内容なし）'));

  return [
    '【未完了タスク一覧】', taskLines.join('\n') || 'なし',
    '', '【定期タスク】', recLines.join('\n') || 'なし',
    '', '【直近の振り返りログ】', logLines.join('\n') || 'なし'
  ].join('\n');
}

// 対話型：ユーザーからの自由な相談にタスク状況を踏まえて回答（タスク分解・状態変更も可能）
function askAgent(userId, userText) {
  const context = buildAgentContext();
  const res = callGeminiWithTools(AGENT_PERSONA,
    '以下は現在のタスク状況です。この状況を踏まえて、ユーザーからの次の相談に答えてください。\n' +
    '「〜を分解して」のような依頼にはpropose_subtasksツールで提案し、' +
    '「〜を完了にして」のように状態変更が明確に依頼された場合はupdate_task_statusツールを、' +
    '「〜を削除して」のように削除が明確に依頼された場合はdelete_taskツールを使ってください。\n\n' +
    context + '\n\n【ユーザーの相談】\n' + userText,
    AGENT_TOOLS, 700);
  if (!res) return '⚠️ AIエージェントの応答取得に失敗しました。時間をおいて再度お試しください。';

  const parts = (((res.candidates || [])[0] || {}).content || {}).parts || [];
  const funcPart = parts.find(p => p.functionCall);
  const textPart = parts.find(p => p.text);
  const intro = textPart ? textPart.text : '';

  if (funcPart && funcPart.functionCall.name === 'propose_subtasks') return handleProposeSubtasks(userId, funcPart.functionCall.args, intro);
  if (funcPart && funcPart.functionCall.name === 'update_task_status') return handleUpdateTaskStatus(funcPart.functionCall.args, intro);
  if (funcPart && funcPart.functionCall.name === 'delete_task') return handleDeleteTask(funcPart.functionCall.args, intro);
  return intro || '⚠️ AIエージェントの応答取得に失敗しました。';
}

// タスク分解の提案を保存し、確認を求める（実際の追加は「はい」の返信を待つ）
function handleProposeSubtasks(userId, input, intro) {
  const parentTitle = String((input || {}).parent_task_title || '').trim();
  const subtasks = Array.isArray((input || {}).subtasks)
    ? input.subtasks.map(s => String(s).trim()).filter(Boolean) : [];
  if (!parentTitle || !subtasks.length) return '⚠️ 分解案の生成に失敗しました。もう一度お試しください。';

  PropertiesService.getScriptProperties().setProperty('PENDING_SUBTASKS_' + userId,
    JSON.stringify({ parentTitle, subtasks, createdAt: Date.now() }));

  return (intro ? intro + '\n\n' : '') +
    `🧩「${parentTitle}」の分解案\n` + subtasks.map((s, i) => (i + 1) + '. ' + s).join('\n') +
    '\n\n追加してよければ「はい」と送ってください（5分以内）。';
}
// 「はい」の返信を受けて、保留中のサブタスク提案を実際にSupabaseへ追加
function addSubtasksToApp_(parentTitle, subtasks) {
  const now = new Date().toISOString();
  const rows = subtasks.map(title => ({
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    title, status: 'todo', priority: 'mid', due: null, due_time: null,
    estimate: '', recurrence: 'none', note: '', tags: [],
    done: false, deleted: false, from_line: true,
    updated_at: now
  }));
  if (!postSupabase('tasks', rows)) return '⚠️ サブタスクの追加に失敗しました。';
  return `✅「${parentTitle}」に${subtasks.length}件のサブタスクを追加しました\n` +
    subtasks.map((s, i) => (i + 1) + '. ' + s).join('\n');
}
// pending中のサブタスク提案への返信（「はい」「いいえ」）を処理。該当なしはnullを返す
function handlePendingSubtaskReply(userId, text) {
  const p = PropertiesService.getScriptProperties();
  const key = 'PENDING_SUBTASKS_' + userId;
  const raw = p.getProperty(key);
  if (!raw) return null;
  let pending;
  try { pending = JSON.parse(raw); } catch (e) { p.deleteProperty(key); return null; }
  if ((Date.now() - pending.createdAt) / 60000 > 5) { p.deleteProperty(key); return null; }

  const t = text.trim();
  if (/^(はい|追加|うん|お願い(します)?|ok|yes)$/i.test(t)) {
    p.deleteProperty(key);
    return addSubtasksToApp_(pending.parentTitle, pending.subtasks);
  }
  if (/^(いいえ|キャンセル|やめて|no)$/i.test(t)) {
    p.deleteProperty(key);
    return '🙅 分解案の追加をキャンセルしました。';
  }
  return null; // 「はい/いいえ」以外はそのまま通常ルーティングへ
}

// タスクの状態変更を実行（タイトルが一意に特定できる場合のみ）
function handleUpdateTaskStatus(input, intro) {
  const title = String((input || {}).task_title || '').trim();
  const newStatus = String((input || {}).new_status || '').trim();
  if (!title || !newStatus) return '⚠️ 状態変更の内容を理解できませんでした。';

  const matches = getSupabase('tasks',
    'deleted=eq.false&title=eq.' + encodeURIComponent(title) + '&select=id,title');
  if (!matches.length) return `⚠️「${title}」に一致するタスクが見つかりませんでした。`;
  if (matches.length > 1) return `⚠️「${title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。`;

  const body = { updated_at: new Date().toISOString() };
  if (newStatus === 'done') { body.done = true; } else { body.done = false; body.status = newStatus; }

  const updated = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(matches[0].id), body);
  if (!updated) return `⚠️「${title}」の更新に失敗しました。`;

  const label = newStatus === 'done' ? '完了' : (STATUS_LABEL_JP[newStatus] || newStatus);
  return (intro ? intro + '\n\n' : '') + `✅「${title}」を${label}に変更しました。`;
}

// タスクの削除を実行（タイトルが一意に特定できる場合のみ。ソフトデリート＝deleted=trueにする）
function handleDeleteTask(input, intro) {
  const title = String((input || {}).task_title || '').trim();
  if (!title) return '⚠️ 削除対象を理解できませんでした。';

  const matches = getSupabase('tasks',
    'deleted=eq.false&title=eq.' + encodeURIComponent(title) + '&select=id,title');
  if (!matches.length) return `⚠️「${title}」に一致するタスクが見つかりませんでした。`;
  if (matches.length > 1) return `⚠️「${title}」に一致するタスクが複数あります。アプリ側で確認・削除してください。`;

  const updated = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(matches[0].id),
    { deleted: true, updated_at: new Date().toISOString() });
  if (!updated) return `⚠️「${title}」の削除に失敗しました。`;

  return (intro ? intro + '\n\n' : '') + `🗑️「${title}」を削除しました。`;
}

// プッシュ型：毎晩の進捗チェックイン（毎朝のリマインドとは別に送信）
function sendAgentCheckIn() {
  const context = buildAgentContext();
  const reply = callGemini(AGENT_PERSONA,
    '以下は現在のタスク状況です。今日の進捗チェックインとして、①最優先で手をつけるべきタスク　②停滞・放置が気になる要注意タスク　③一言アドバイス、をまとめてください。\n\n' + context,
    600);
  if (!reply) return;
  getUsers().forEach(uid => pushText(uid, '🧭 進捗チェックイン\n\n' + reply));
}
// 毎晩の自動チェックイン・トリガーを作成
function installAgentTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'sendAgentCheckIn') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendAgentCheckIn').timeBased()
    .atHour(AGENT_HOUR).nearMinute(0).everyDays(1).create();
}
// テスト：今すぐ進捗チェックインを送る
function testAgentCheckIn() {
  if (!getUsers().length) { Logger.log('ユーザー未登録：先にLINEからBotへメッセージを送ってください。'); return; }
  sendAgentCheckIn();
  Logger.log('進捗チェックインを送信しました');
}

/* ============ 週次レポート ============ */
function sendWeeklyReport() {
  const today = new Date();
  const weekAgo = new Date(today);
  weekAgo.setDate(weekAgo.getDate() - 7);
  const since = weekAgo.toISOString().slice(0, 10);

  // 先週完了したタスク（updated_at >= 7日前 AND done=true）
  const doneTasks = getSupabase('tasks',
    'done=eq.true&deleted=eq.false&updated_at=gte.' + since + '&select=title,updated_at&order=updated_at.desc');

  // 先週の日次ログ
  const logs = getSupabase('daily_logs',
    'log_date=gte.' + since + '&select=log_date,content&order=log_date.desc');

  const m = today.getMonth() + 1, d = today.getDate();
  let msg = `📊 週次レポート（${m}/${d}）\n`;

  // 完了タスク
  msg += `\n✅ 先週の完了タスク（${doneTasks.length}件）`;
  if (doneTasks.length) {
    msg += '\n' + doneTasks.map(t => '・' + t.title).join('\n');
  } else {
    msg += '\nなし';
  }

  // 日次ログ
  msg += `\n\n📓 振り返りログ（${logs.length}件）`;
  if (logs.length) {
    msg += '\n' + logs.map(l => {
      const ld = new Date(l.log_date);
      const label = (ld.getMonth() + 1) + '/' + ld.getDate();
      const excerpt = l.content ? l.content.slice(0, 60).replace(/\n/g, ' ') + (l.content.length > 60 ? '…' : '') : '（内容なし）';
      return label + '：' + excerpt;
    }).join('\n');
  } else {
    msg += '\nなし';
  }

  if (doneTasks.length === 0 && logs.length === 0) {
    msg += '\n\n今週もお疲れ様でした！来週も頑張りましょう💪';
  } else {
    msg += '\n\n今週もお疲れ様でした！';
  }

  getUsers().forEach(uid => pushText(uid, msg));
}

/* ============ セットアップ（1回だけ実行）============ */
// 毎朝の自動通知トリガーを作成
function installDailyTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'sendReminders') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendReminders').timeBased()
    .atHour(REMIND_HOUR).nearMinute(REMIND_MINUTE).everyDays(1).create();
}
// 毎週月曜 9時に週次レポートを送るトリガーを作成
function installWeeklyTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'sendWeeklyReport') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendWeeklyReport').timeBased()
    .onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(9).create();
}
// テスト：今すぐ自分に通知を送る（先にLINEから1回メッセージを送っておくこと）
function testReminder() {
  if (!getUsers().length) { Logger.log('ユーザー未登録：先にLINEからBotへメッセージを送ってください。'); return; }
  sendReminders();
  Logger.log('送信しました（対象があれば届きます）');
}
// テスト：今すぐ週次レポートを送る
function testWeeklyReport() {
  if (!getUsers().length) { Logger.log('ユーザー未登録：先にLINEからBotへメッセージを送ってください。'); return; }
  sendWeeklyReport();
  Logger.log('週次レポートを送信しました');
}
