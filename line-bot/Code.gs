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

// AIエージェントの人格（厳しめのプロマネ＋各分野のスペシャリスト）
const AGENT_PERSONA = [
  'あなたは経験豊富な、やや厳しめのプロジェクトマネージャーです。',
  'ユーザーのタスク管理データ（進捗状況・期限・最終更新からの経過日数・振り返りログ・直近の完了実績・過去の会話履歴）を分析し、率直かつ具体的にコメントします。',
  '進捗が悪いタスク、期限超過、長期間放置されているタスクは遠慮なく指摘してください。順調な進捗は簡潔に認めます。',
  '一般論ではなく、渡されたデータに含まれる具体的なタスク名・経過日数・期限をもとに指摘してください。',
  'タスクの進め方や成功のさせ方について聞かれた場合は、単なる励ましや進捗評価にとどまらず、そのタスクの分野に詳しいスペシャリストとして、具体的な進め方・実務上の注意点・コツを提示してください。',
  '常に日本語で、LINEのトーク画面に収まる分量（300字程度まで）に収め、絵文字は使っても最小限にし、要点を箇条書き中心でまとめてください。',
  'LINEはMarkdown記法を装飾として表示できません。「**太字**」「*斜体*」「#見出し」「1. 番号リスト」などの記法は絶対に使わず、記号を含まない普通の文章で書いてください。箇条書きは行頭に「・」だけを使ってください。'
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
  // 保留中の返信（サブタスク提案／期限確認／優先順位見直し案）への「はい/いいえ」等を最優先で処理
  const pendingSubtask = handlePendingSubtaskReply(userId, text);
  if (pendingSubtask !== null) return pendingSubtask;
  const pendingDue = handlePendingDueReply(userId, text);
  if (pendingDue !== null) return pendingDue;
  const pendingReprioritize = handlePendingReprioritizeReply(userId, text);
  if (pendingReprioritize !== null) return pendingReprioritize;

  if (/^(一覧|リスト|list)$/i.test(text)) return listAll(userId);
  if (/^(通知|リマインド|今日)$/i.test(text)) return buildReminder() || '📭 期限が近い（超過・今日・明日）のタスクはありません。';
  if (/^(ヘルプ|使い方|help)$/i.test(text)) return helpText();
  if (/^(相談|アドバイス)$/i.test(text)) return askAgent(userId, '最近のタスク状況について、率直な進捗評価とアドバイスをください。');
  if (/^(優先順位を整理して|優先順位を並べ替えて|並べ替えて)$/.test(text)) return proposeReprioritization_(userId);

  // 「3番を完了にして」のような番号指定を、直前の一覧を元に実際のタイトルへ変換する
  const numMatch = text.match(/^(\d+)番(目)?(を|の)?\s*([\s\S]*)$/);
  if (numMatch) {
    const resolved = resolveNumberedTask_(userId, Number(numMatch[1]));
    if (!resolved) return '番号に対応するタスクが見つかりませんでした。「一覧」で番号を確認してから、もう一度お試しください。';
    const rest = numMatch[4].trim();
    if (!rest) return `${numMatch[1]}番：「${resolved.title}」`;
    text = `「${resolved.title}」${numMatch[4]}`;
  }
  if (/^(資料一覧|資料リスト)$/.test(text)) return listMaterials_();
  const materialMatch = text.match(/^資料[:：]\s*([\s\S]+)$/);
  if (materialMatch) return addMaterialFromText_(materialMatch[1].trim());
  const ytMatch = text.match(/(https?:\/\/(?:www\.)?(?:youtube\.com\/(?:watch\?v=|shorts\/)[\w-]+|youtu\.be\/[\w-]+))/);
  if (ytMatch) return addMaterialFromYoutube_(ytMatch[1]);
  if (!text) return '空メッセージです。「ヘルプ」で使い方を表示します。';
  if (isQuestionLike(text)) return askAgent(userId, text);
  return addLine(userId, text);
}
// 疑問文・相談・タスク分解／状態変更／削除／要約／優先度／期限変更の依頼っぽい文かどうかをざっくり判定（タスク追加との区別用）
function isQuestionLike(text) {
  if (/[?？]$/.test(text)) return true;
  if (/(か|かな|かしら)[。.!！]?$/.test(text)) return true;
  if (/(完了しました|完了した|終わりました|終わった|できました|やりました)[。.!！]?$/.test(text)) return true;
  if (/(どう|教えて|大丈夫|やばい|相談|アドバイス|優先|分解|やる意味|意味ある|完了に|着手中に|対応待ちに|ペンディングに|状態を|ステータスを|削除|消して|要約|言い換え|まとめて|整理して|並べ替え|期限を|期限に|書き換えて|タイトルを|名前を)/.test(text)) return true;
  return false;
}

/* ============ タスク追加・一覧 ※すべてSupabase直接操作 ============ */
// タスクをその場でSupabaseに追加する（受信箱を介さず即反映）
function addLine(userId, text) {
  // 「今日17時に」「6/25 15:00」のような日付・時刻表現を検出して取り除く
  const parsed = extractDateTime_(text);
  let title = parsed.title;
  let due = parsed.due;
  const dueTime = parsed.dueTime;

  // 規則で日付が拾えず、それでも日付っぽい表現が残っていればAIで補って解釈する
  if (!due && /(来週|再来週|今週中|今月中|来月|月末|週末|月曜|火曜|水曜|木曜|金曜|土曜|日曜)/.test(title)) {
    const aiDue = aiParseDate_(title);
    if (aiDue) due = aiDue;
  }

  // 長い・改行を含む・箇条書きっぽい文章は、AIで簡潔なタスク名に整理する
  if (title.length > 20 || /\n|・|^[0-9]+[.、)]/.test(title)) {
    const cleaned = cleanupTaskText_(title);
    if (cleaned) title = cleaned;
  }

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
  if (due) {
    msg += `\n📅 ${jp(due)}` + (dueTime ? ' ' + dueTime : '');
  } else {
    PropertiesService.getScriptProperties().setProperty('PENDING_DUE_' + userId,
      JSON.stringify({ mode: 'single', tasks: [{ id: row.id, title }], createdAt: Date.now() }));
    msg += '\n\n📅 期限はいつにしますか？（例：6/30、今日、なし）';
  }
  msg += `\n\n未完了: ${cnt}件`;
  return msg;
}
// 未完了タスク＋定期タスクを「進捗状況ごと」に表示。番号を振り、後で「3番を〜」と指定できるよう記憶しておく
function listAll(userId) {
  const app = getAppTasksAll();
  const rec = getRecurring();
  if (!app.length && !rec.length) return '🎉 未完了のタスクはありません。';

  let msg = '📋 タスク一覧\n';
  const numbered = [];
  let n = 0;

  // 進捗状況ごとにグループ表示（番号つき）
  STATUS_ORDER.forEach(st => {
    const arr = app.filter(t => t.status === st);
    if (!arr.length) return;
    msg += '\n' + STATUS_ICON[st] + ' ' + st + '（' + arr.length + '）\n' +
      arr.map(t => {
        n++;
        numbered.push({ num: n, id: t.id, title: t.title });
        return n + '. ' + t.title + (t.due ? '（' + jp(t.due) + '）' : '');
      }).join('\n') + '\n';
  });

  // 定期タスク（番号は振らない）
  if (rec.length) {
    msg += '\n🔁 定期タスク（' + rec.length + '）\n' +
      rec.map(r => '・' + r.title + '（' + r.recurrence + (r.next ? '・次回 ' + jp2(r.next) : '') + '）').join('\n') + '\n';
  }

  if (numbered.length && userId) {
    PropertiesService.getScriptProperties().setProperty('LAST_LIST_' + userId,
      JSON.stringify({ items: numbered, createdAt: Date.now() }));
    msg += '\n番号で「3番を完了にして」のように操作できます。';
  }
  return msg.trim();
}
// アプリの未完了タスクを全部取得（状態つき・期限の有無は問わない）※Supabase参照
function getAppTasksAll() {
  const rows = getSupabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title,status,due');
  return rows.map(r => ({
    id: r.id,
    title: String(r.title),
    due: r.due || '',
    status: STATUS_LABEL_JP[r.status] || r.status || '未着手'
  }));
}
// 直前の「一覧」で表示した番号から、実際のタスク（id・title）を引き当てる。該当なし/期限切れはnull
function resolveNumberedTask_(userId, num) {
  const raw = PropertiesService.getScriptProperties().getProperty('LAST_LIST_' + userId);
  if (!raw) return null;
  let data;
  try { data = JSON.parse(raw); } catch (e) { return null; }
  if ((Date.now() - data.createdAt) / 60000 > 60) return null;
  return (data.items || []).find(it => it.num === num) || null;
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
    '・一覧 → 未完了タスクを番号つきで表示',
    '　例）3番を完了にして／3番の期限を7/1にして／3番を削除して　→ 一覧の番号でそのまま操作（1時間有効）',
    '・通知 → 今の期限リマインドを表示',
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
    '・YouTubeのURLを送る、または「資料：本文」の形式でテキストを送ると、AIコーチが要点を要約して覚え、以降の相談で参考にします',
    '・資料一覧 → 登録済みの資料を確認',
    '',
    `毎朝${REMIND_HOUR}時に期限リマインド、毎晩${AGENT_HOUR}時にAIコーチの進捗チェックイン（今日完了したタスクの集計・期限未設定タスクの確認・優先順位見直し案つき）を自動送信します。`,
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

// 長い/雑多な文章から、実際のタスク名だけを簡潔に抽出する（AI機能・失敗時はnullを返し元の文章を使う）
function cleanupTaskText_(text) {
  const reply = callGemini(
    'あなたはタスク管理アシスタントです。渡された文章から、実際にやるべきタスクの内容だけを抽出し、20文字程度までの簡潔な日本語のタスク名として1行で返してください。' +
    '説明・前置き・箇条書き記号・敬語表現は不要です。タスク名だけを返してください。',
    text, 60);
  if (!reply) return null;
  const cleaned = reply.trim().split('\n')[0].replace(/^[・\-\d.、)]+\s*/, '').trim();
  return cleaned || null;
}
// 「来週」「月末」のような曖昧な日付表現をAIでYYYY-MM-DDに変換する（失敗時はnull）
function aiParseDate_(text) {
  const reply = callGemini(
    '今日の日付は' + todayISO_() + '（YYYY-MM-DD）です。渡された文章に含まれる日付表現を、YYYY-MM-DD形式の1行だけで返してください。' +
    '日付表現が無い・特定できない場合は「なし」とだけ返してください。説明は不要です。',
    text, 20);
  if (!reply) return null;
  const m = reply.trim().match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  return m ? m[1] + '-' + m[2].padStart(2, '0') + '-' + m[3].padStart(2, '0') : null;
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
          parent_task_title: { type: 'STRING', description: '分解対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）' },
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
          task_title: { type: 'STRING', description: '状態変更の対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）' },
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
          task_title: { type: 'STRING', description: '削除対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）' }
        },
        required: ['task_title']
      }
    },
    {
      name: 'update_task_priority',
      description: 'ユーザーが「優先度を上げて」「優先順位を高くして」のように特定タスクの優先度変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。',
      parameters: {
        type: 'OBJECT',
        properties: {
          task_title: { type: 'STRING', description: '優先度変更の対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）' },
          new_priority: { type: 'STRING', enum: ['high', 'mid', 'low'], description: '変更後の優先度' }
        },
        required: ['task_title', 'new_priority']
      }
    },
    {
      name: 'update_task_due',
      description: 'ユーザーが「期限を6/30にして」のように特定タスクの期限変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。',
      parameters: {
        type: 'OBJECT',
        properties: {
          task_title: { type: 'STRING', description: '期限変更の対象タスクのタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）' },
          due: { type: 'STRING', description: 'YYYY-MM-DD形式の新しい期限' },
          due_time: { type: 'STRING', description: 'HH:MM形式の時刻（分かる場合のみ、無ければ省略）' }
        },
        required: ['task_title', 'due']
      }
    },
    {
      name: 'update_task_title',
      description: 'ユーザーが「〇〇を△△に書き換えて」「〇〇のタイトルを△△に変更して」のように、既存タスクの内容・タイトルそのものの変更を明確に依頼した場合に使う。渡されたタスク一覧の中でタイトルが一意に特定できる場合のみ使うこと。',
      parameters: {
        type: 'OBJECT',
        properties: {
          task_title: { type: 'STRING', description: '変更対象タスクの現在のタイトル（正式なタイトル。ユーザーが一部の言葉やキーワードだけで指定していても、渡されたタスク一覧から該当するものを選び、その一覧にある通りの完全なタイトルをここに入れること）' },
          new_title: { type: 'STRING', description: '変更後の新しいタイトル' }
        },
        required: ['task_title', 'new_title']
      }
    }
  ]
}];
// バッチで複数タスクへ期限を割り当てるためのツール（夜間チェックインの棚卸し返信の解釈に使用）
const TOOLS_SET_DUE_BATCH = [{
  functionDeclarations: [{
    name: 'set_due_dates',
    description: 'タスク一覧と、ユーザーの返信文をもとに、各タスクに割り当てる期限を返す。返信で触れられていないタスクは含めない。',
    parameters: {
      type: 'OBJECT',
      properties: {
        assignments: {
          type: 'ARRAY',
          items: {
            type: 'OBJECT',
            properties: {
              task_title: { type: 'STRING', description: 'タスク一覧の名称と完全一致' },
              due: { type: 'STRING', description: 'YYYY-MM-DD形式の期限' },
              due_time: { type: 'STRING', description: 'HH:MM形式の時刻（分かる場合のみ）' }
            },
            required: ['task_title', 'due']
          }
        }
      },
      required: ['assignments']
    }
  }]
}];
// 未完了タスク全体の優先順位見直し案を作るためのツール（毎晩のチェックイン／「優先順位を整理して」で使用）
const TOOLS_REPRIORITIZE = [{
  functionDeclarations: [{
    name: 'reprioritize_tasks',
    description: '未完了タスク一覧を、期限・停滞日数・状態を踏まえて見直し、優先度を変えるべきタスクだけを返す。今のままでよいタスクは含めない。',
    parameters: {
      type: 'OBJECT',
      properties: {
        assignments: {
          type: 'ARRAY',
          items: {
            type: 'OBJECT',
            properties: {
              task_title: { type: 'STRING', description: 'タスク一覧の名称と完全一致' },
              priority: { type: 'STRING', enum: ['high', 'mid', 'low'], description: '新しい優先度' }
            },
            required: ['task_title', 'priority']
          }
        }
      },
      required: ['assignments']
    }
  }]
}];
// Gemini APIを呼び出し、応答テキストを返す（失敗時はnull）。toolsを渡すとGoogle検索等も使える
function callGemini(systemPrompt, userText, maxTokens, tools) {
  const data = callGeminiRaw_(systemPrompt, userText, tools || null, maxTokens);
  if (!data) return null;
  const parts = (((data.candidates || [])[0] || {}).content || {}).parts || [];
  const textPart = parts.find(p => p.text);
  return textPart ? textPart.text : null;
}
// Gemini APIを呼び出し、レスポンス全体（functionCallを含む）を返す（失敗時はnull）
function callGeminiWithTools(systemPrompt, userText, tools, maxTokens) {
  return callGeminiRaw_(systemPrompt, userText, tools, maxTokens);
}
// userTextは文字列（通常のテキスト）、または動画等を渡す場合はparts配列（例：[{file_data:{file_uri:url}},{text:'...'}]）
function callGeminiRaw_(systemPrompt, userText, tools, maxTokens) {
  const parts = Array.isArray(userText) ? userText : [{ text: userText }];
  const payload = {
    system_instruction: { parts: [{ text: systemPrompt }] },
    contents: [{ role: 'user', parts: parts }],
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

// タスク状況（Supabase）＋定期タスク＋直近の完了実績＋過去の会話をAI用にまとめる
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

  // 直近14日で完了したタスク（傾向把握用）
  const since14 = new Date(now - 14 * 86400000).toISOString().slice(0, 10);
  const doneTasks = getSupabase('tasks',
    'done=eq.true&deleted=eq.false&updated_at=gte.' + since14 + '&select=title,updated_at&order=updated_at.desc&limit=15');
  const doneLines = doneTasks.map(t => '・' + t.title + '（' + jp2(new Date(t.updated_at)) + '完了）');

  // 直近のAIコーチとの会話（継続性・傾向の把握用）
  const convos = getSupabase('ai_log', 'select=user_text,ai_reply,created_at&order=created_at.desc&limit=6');
  const convoLines = convos.reverse().map(c =>
    '・' + jp2(new Date(c.created_at)) + ' ユーザー「' + trunc_(c.user_text, 60) + '」→ コーチ「' + trunc_(c.ai_reply, 80) + '」');

  // ユーザーが登録した参考資料（動画・テキスト）の要約
  const materials = getSupabase('materials', 'select=title,summary&order=created_at.desc&limit=10');
  const materialLines = materials.map(m => '・' + m.title + '：' + trunc_(m.summary, 150));

  return [
    '【未完了タスク一覧】', taskLines.join('\n') || 'なし',
    '', '【直近14日で完了したタスク】', doneLines.join('\n') || 'なし',
    '', '【定期タスク】', recLines.join('\n') || 'なし',
    '', '【直近の振り返りログ】', logLines.join('\n') || 'なし',
    '', '【直近のAIコーチとの会話】', convoLines.join('\n') || 'なし',
    '', '【ユーザーが登録した参考資料】', materialLines.join('\n') || 'なし'
  ].join('\n');
}
function trunc_(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }

/* ============ 参考資料（YouTube動画／テキスト）をAIコーチに学習させる ============ */
// YouTube動画のURLを渡すと、Geminiが内容を要約して「資料」として保存する
function addMaterialFromYoutube_(url) {
  const parts = [
    { file_data: { file_uri: url } },
    { text: 'この動画の内容を、後でタスクの相談・アドバイスに役立てられるよう、実務で使える要点・具体的な方法論・注意点を中心に日本語800字程度で要約してください。' +
      '1行目には「タイトル: 〇〇」の形式で20文字程度の短いタイトルだけを入れ、2行目以降に要約本文を書いてください。' }
  ];
  const reply = callGemini('あなたはタスク管理アシスタントのための資料要約担当です。', parts, 800);
  if (!reply) return '⚠️ 動画の読み込みに失敗しました。URLが正しいか確認のうえ、時間をおいて再度お試しください。';
  return saveMaterial_(reply, 'youtube', url);
}
// 貼り付けられたテキストを、後で参照しやすい形に整理して「資料」として保存する
function addMaterialFromText_(text) {
  if (!text) return '資料の内容が空です。「資料：」の後に本文も送ってください。';
  const reply = callGemini(
    'あなたはタスク管理アシスタントのための資料要約担当です。渡されたテキストの1行目に「タイトル: 〇〇」の形式で20文字程度の短いタイトルをつけてください。' +
    '2行目以降に、後でタスクの相談・アドバイスに役立てられるよう要点を日本語600字程度で整理してください。元のテキストがすでに簡潔なら、大きく書き換えずそのまま活かして構いません。',
    text, 600);
  if (!reply) return '⚠️ 資料の整理に失敗しました。時間をおいて再度お試しください。';
  return saveMaterial_(reply, 'text', null);
}
// AIの要約結果（1行目「タイトル: 〇〇」＋本文）をパースしてSupabaseに保存する
function saveMaterial_(aiText, sourceType, sourceUrl) {
  const titleMatch = aiText.match(/^タイトル[:：]\s*(.+)$/m);
  const title = titleMatch ? titleMatch[1].trim() : trunc_(aiText, 20);
  const summary = titleMatch ? aiText.replace(titleMatch[0], '').trim() : aiText;

  const ok = postSupabase('materials', [{
    title: trunc_(title, 60),
    source_type: sourceType,
    source_url: sourceUrl,
    summary: trunc_(summary, 1500),
    created_at: new Date().toISOString()
  }]);
  if (!ok) return '⚠️ 資料の保存に失敗しました。Supabaseにmaterialsテーブルがあるか確認してください。';

  return `📚 資料として覚えました\n「${title}」\n\n` + trunc_(summary, 200) +
    '\n\n以降の相談やチェックインで参考にします。「資料一覧」で登録済みの資料を確認できます。';
}
// 登録済みの資料一覧を表示する
function listMaterials_() {
  const rows = getSupabase('materials', 'select=title,source_type&order=created_at.desc&limit=20');
  if (!rows.length) return '📚 登録済みの資料はまだありません。「資料：本文」やYouTubeのURLを送ると覚えます。';
  return '📚 資料一覧\n' + rows.map(r => '・' + r.title + (r.source_type === 'youtube' ? '🎥' : '📝')).join('\n');
}
// AIコーチとのやり取りを記録する（次回以降の文脈把握のため。失敗しても致命的ではないので結果は無視する）
function logAgentInteraction_(userId, userText, aiReply) {
  postSupabase('ai_log', [{
    user_id: userId,
    user_text: trunc_(userText, 500),
    ai_reply: trunc_(aiReply, 1000),
    created_at: new Date().toISOString()
  }]);
}

// 対話型：ユーザーからの自由な相談にタスク状況を踏まえて回答（タスク分解・状態変更も可能）
function askAgent(userId, userText) {
  const context = buildAgentContext();
  const res = callGeminiWithTools(AGENT_PERSONA,
    '以下は現在のタスク状況です。この状況を踏まえて、ユーザーからの次の相談に答えてください。\n' +
    '「〜を分解して」のような依頼にはpropose_subtasksツールで提案し、' +
    '「〜を完了にして」のように状態変更が明確に依頼された場合はupdate_task_statusツールを、' +
    '「〜を削除して」のように削除が明確に依頼された場合はdelete_taskツールを、' +
    '「〜の優先度を上げて/下げて」のように優先度変更が明確に依頼された場合はupdate_task_priorityツールを、' +
    '「〜の期限を6/30にして」のように特定タスクの期限変更が明確に依頼された場合はupdate_task_dueツールを、' +
    '「〜を△△に書き換えて」のようにタスクの内容・タイトルそのものの変更が明確に依頼された場合はupdate_task_titleツールを使ってください。\n' +
    '「要約して」「言い換えて」「まとめて」のような依頼には、ツールを使わず文章で簡潔に答えてください。\n' +
    'ユーザーはタスク名を毎回全部書かず、一部の言葉やキーワードだけで指定することが多いです。「未完了タスク一覧」を見て該当するタスクが1つに絞れる場合は、正式なタイトルを補ってツールを呼び出してください。似たタスクが複数あり判断できない場合のみ、ツールを使わず候補を挙げて確認してください。\n' +
    '「直近14日で完了したタスク」や「直近の会話」も参考に、繰り返し先延ばしにしている傾向や、前回の相談からの変化があれば触れてください。\n' +
    '「ユーザーが登録した参考資料」に関連する内容があれば、一般論より優先して、その資料の内容を踏まえて具体的に助言してください。\n\n' +
    context + '\n\n【ユーザーの相談】\n' + userText,
    AGENT_TOOLS, 700);
  if (!res) return '⚠️ AIエージェントの応答取得に失敗しました。時間をおいて再度お試しください。';

  const parts = (((res.candidates || [])[0] || {}).content || {}).parts || [];
  const funcPart = parts.find(p => p.functionCall);
  const textPart = parts.find(p => p.text);
  const intro = textPart ? textPart.text : '';

  let reply;
  if (funcPart && funcPart.functionCall.name === 'propose_subtasks') reply = handleProposeSubtasks(userId, funcPart.functionCall.args, intro);
  else if (funcPart && funcPart.functionCall.name === 'update_task_status') reply = handleUpdateTaskStatus(funcPart.functionCall.args, intro);
  else if (funcPart && funcPart.functionCall.name === 'delete_task') reply = handleDeleteTask(funcPart.functionCall.args, intro);
  else if (funcPart && funcPart.functionCall.name === 'update_task_priority') reply = handleUpdateTaskPriority(funcPart.functionCall.args, intro);
  else if (funcPart && funcPart.functionCall.name === 'update_task_due') reply = handleUpdateTaskDue(funcPart.functionCall.args, intro);
  else if (funcPart && funcPart.functionCall.name === 'update_task_title') reply = handleUpdateTaskTitle(funcPart.functionCall.args, intro);
  else reply = intro || '⚠️ AIエージェントの応答取得に失敗しました。';

  logAgentInteraction_(userId, userText, reply);
  return reply;
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

// タスクの優先度変更を実行（タイトルが一意に特定できる場合のみ）
function handleUpdateTaskPriority(input, intro) {
  const title = String((input || {}).task_title || '').trim();
  const newPriority = String((input || {}).new_priority || '').trim();
  if (!title || ['high', 'mid', 'low'].indexOf(newPriority) < 0) return '⚠️ 優先度変更の内容を理解できませんでした。';

  const matches = getSupabase('tasks',
    'deleted=eq.false&title=eq.' + encodeURIComponent(title) + '&select=id,title');
  if (!matches.length) return `⚠️「${title}」に一致するタスクが見つかりませんでした。`;
  if (matches.length > 1) return `⚠️「${title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。`;

  const updated = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(matches[0].id),
    { priority: newPriority, updated_at: new Date().toISOString() });
  if (!updated) return `⚠️「${title}」の更新に失敗しました。`;

  const label = { high: '高', mid: '中', low: '低' }[newPriority];
  return (intro ? intro + '\n\n' : '') + `✅「${title}」の優先度を${label}に変更しました。`;
}

// タスクの期限変更を実行（タイトルが一意に特定できる場合のみ）
function handleUpdateTaskDue(input, intro) {
  const title = String((input || {}).task_title || '').trim();
  const due = String((input || {}).due || '').trim();
  const dueTime = String((input || {}).due_time || '').trim();
  if (!title || !due) return '⚠️ 期限変更の内容を理解できませんでした。';

  const matches = getSupabase('tasks',
    'deleted=eq.false&title=eq.' + encodeURIComponent(title) + '&select=id,title');
  if (!matches.length) return `⚠️「${title}」に一致するタスクが見つかりませんでした。`;
  if (matches.length > 1) return `⚠️「${title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。`;

  const updated = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(matches[0].id),
    { due: due, due_time: dueTime || null, updated_at: new Date().toISOString() });
  if (!updated) return `⚠️「${title}」の更新に失敗しました。`;

  return (intro ? intro + '\n\n' : '') + `✅「${title}」の期限を ${jp(due)}` + (dueTime ? ' ' + dueTime : '') + ' に変更しました。';
}

// タスクの内容（タイトル）変更を実行（タイトルが一意に特定できる場合のみ）
function handleUpdateTaskTitle(input, intro) {
  const title = String((input || {}).task_title || '').trim();
  const newTitle = String((input || {}).new_title || '').trim();
  if (!title || !newTitle) return '⚠️ 書き換え内容を理解できませんでした。';

  const matches = getSupabase('tasks',
    'deleted=eq.false&title=eq.' + encodeURIComponent(title) + '&select=id,title');
  if (!matches.length) return `⚠️「${title}」に一致するタスクが見つかりませんでした。`;
  if (matches.length > 1) return `⚠️「${title}」に一致するタスクが複数あります。アプリ側で確認・変更してください。`;

  const updated = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(matches[0].id),
    { title: newTitle, updated_at: new Date().toISOString() });
  if (!updated) return `⚠️「${title}」の更新に失敗しました。`;

  return (intro ? intro + '\n\n' : '') + `✅「${title}」を「${newTitle}」に書き換えました。`;
}

// pending中の期限確認（単発追加時／夜間の棚卸し）への返信を処理。該当なしはnullを返す
function handlePendingDueReply(userId, text) {
  const p = PropertiesService.getScriptProperties();
  const key = 'PENDING_DUE_' + userId;
  const raw = p.getProperty(key);
  if (!raw) return null;
  let pending;
  try { pending = JSON.parse(raw); } catch (e) { p.deleteProperty(key); return null; }
  if ((Date.now() - pending.createdAt) / 60000 > 360) { p.deleteProperty(key); return null; }

  const t = text.trim();
  if (/^(不要|なし|未定|スキップ|やめて|あとで)[。.!！]?$/.test(t)) {
    p.deleteProperty(key);
    return '了解です、期限は未設定のままにします。';
  }

  if (pending.mode === 'single') {
    const parsed = extractDateTime_(t);
    if (!parsed.due) {
      // 明らかに日付の返信ではなさそうな長文・複数行は、期限確認を諦めて通常ルーティングへ流す
      if (t.length > 15 || /\n/.test(t)) { p.deleteProperty(key); return null; }
      return '日付を認識できませんでした。「6/30」「今日」のように送ってください（設定しない場合は「なし」）。';
    }
    const task = pending.tasks[0];
    const updated = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(task.id),
      { due: parsed.due, due_time: parsed.dueTime || null, updated_at: new Date().toISOString() });
    p.deleteProperty(key);
    if (!updated) return `⚠️「${task.title}」の期限設定に失敗しました。`;
    return `✅「${task.title}」の期限を ${jp(parsed.due)}` + (parsed.dueTime ? ' ' + parsed.dueTime : '') + ' に設定しました。';
  }

  // batchモード：複数タスクぶんの期限をまとめてAIに割り振らせる
  const taskListText = pending.tasks.map(pt => '・' + pt.title).join('\n');
  const res = callGeminiWithTools(
    '今日の日付は' + todayISO_() + '（YYYY-MM-DD）です。ユーザーの返信文から、下記タスク一覧のうちどのタスクにどの期限を割り当てたいか読み取り、set_due_datesツールで返してください。返信で触れられていないタスクは含めないでください。',
    '【タスク一覧】\n' + taskListText + '\n\n【ユーザーの返信】\n' + t,
    TOOLS_SET_DUE_BATCH, 400);
  p.deleteProperty(key);

  const parts = res && (((res.candidates || [])[0] || {}).content || {}).parts;
  const funcPart = parts && parts.find(pp => pp.functionCall);
  const assignments = funcPart && Array.isArray(funcPart.functionCall.args.assignments) ? funcPart.functionCall.args.assignments : [];
  if (!assignments.length) {
    // 期限の話ではなさそうな長文・複数行は、諦めて通常ルーティングへ流す
    if (t.length > 15 || /\n/.test(t)) return null;
    return '反映できませんでした。個別に「〇〇の期限を6/30にして」のように送ってください。';
  }

  const applied = [];
  assignments.forEach(a => {
    const atitle = String(a.task_title || '').trim();
    const adue = String(a.due || '').trim();
    if (!atitle || !adue) return;
    const match = pending.tasks.find(pt => pt.title === atitle);
    if (!match) return;
    const ok = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(match.id),
      { due: adue, due_time: a.due_time || null, updated_at: new Date().toISOString() });
    if (ok) applied.push('・' + atitle + '（' + jp(adue) + (a.due_time ? ' ' + a.due_time : '') + '）');
  });
  if (!applied.length) return '反映できませんでした。個別に「〇〇の期限を6/30にして」のように送ってください。';
  return '✅ 期限を設定しました\n' + applied.join('\n');
}

// 未完了タスク全体の優先順位見直し案を作り、確認を求める（実際の適用は「はい」の返信を待つ）
function proposeReprioritization_(userId) {
  const tasks = getSupabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title,priority');
  if (!tasks.length) return '未完了タスクがありません。';
  const priorityMap = {};
  tasks.forEach(t => { priorityMap[t.title] = t.priority; });

  const context = buildAgentContext();
  const res = callGeminiWithTools(AGENT_PERSONA,
    '以下は現在のタスク状況です。期限・停滞日数・状態を踏まえて、優先度を変えたほうがよいタスクだけをreprioritize_tasksツールで提案してください。変更不要なタスクは含めないでください。\n\n' + context,
    TOOLS_REPRIORITIZE, 500);
  const parts = res && (((res.candidates || [])[0] || {}).content || {}).parts;
  const funcPart = parts && parts.find(p => p.functionCall);
  const assignments = funcPart && Array.isArray(funcPart.functionCall.args.assignments) ? funcPart.functionCall.args.assignments : [];
  const valid = assignments.filter(a => priorityMap[a.task_title] !== undefined && priorityMap[a.task_title] !== a.priority);
  if (!valid.length) return '🔀 今のままで問題なさそうです。優先順位の変更提案はありません。';

  PropertiesService.getScriptProperties().setProperty('PENDING_REPRIORITIZE_' + userId,
    JSON.stringify({ assignments: valid, createdAt: Date.now() }));

  const label = { high: '高', mid: '中', low: '低' };
  const lines = valid.map(a => '・' + a.task_title + '：' + label[priorityMap[a.task_title]] + '→' + label[a.priority]);
  return '🔀 優先順位の見直し案\n' + lines.join('\n') + '\n\n適用してよければ「はい」と送ってください。';
}
// pending中の優先順位見直し案への返信（「はい」「いいえ」）を処理。該当なしはnullを返す
function handlePendingReprioritizeReply(userId, text) {
  const p = PropertiesService.getScriptProperties();
  const key = 'PENDING_REPRIORITIZE_' + userId;
  const raw = p.getProperty(key);
  if (!raw) return null;
  let pending;
  try { pending = JSON.parse(raw); } catch (e) { p.deleteProperty(key); return null; }
  if ((Date.now() - pending.createdAt) / 60000 > 360) { p.deleteProperty(key); return null; }

  const t = text.trim();
  if (/^(はい|適用|うん|お願い(します)?|ok|yes)$/i.test(t)) {
    p.deleteProperty(key);
    const tasks = getSupabase('tasks', 'done=eq.false&deleted=eq.false&select=id,title');
    const idByTitle = {};
    tasks.forEach(tk => { idByTitle[tk.title] = tk.id; });
    const applied = [];
    pending.assignments.forEach(a => {
      const id = idByTitle[a.task_title];
      if (!id) return;
      const ok = patchSupabase('tasks', 'id=eq.' + encodeURIComponent(id),
        { priority: a.priority, updated_at: new Date().toISOString() });
      if (ok) applied.push('・' + a.task_title);
    });
    if (!applied.length) return '⚠️ 適用できませんでした。タスクの状態が変わっている可能性があります。';
    return '✅ 優先順位を更新しました\n' + applied.join('\n');
  }
  if (/^(いいえ|キャンセル|やめて|no)$/i.test(t)) {
    p.deleteProperty(key);
    return '🙅 優先順位の変更をキャンセルしました。';
  }
  return null;
}

// 今日完了したタスクの集計（日次レポート・進捗チェックインに含める）
function buildDailyReport_() {
  const todayStr = todayISO_();
  const doneToday = getSupabase('tasks',
    'done=eq.true&deleted=eq.false&updated_at=gte.' + todayStr + '&select=title&order=updated_at.desc');
  let msg = `✅ 今日完了したタスク（${doneToday.length}件）`;
  if (doneToday.length) msg += '\n' + doneToday.map(t => '・' + t.title).join('\n');
  return msg;
}
// テスト：今日完了したタスクの集計を確認する（LINE送信はせずログに出すだけ）
function testDailyReport() {
  Logger.log(buildDailyReport_());
}

// プッシュ型：毎晩の進捗チェックイン（毎朝のリマインドとは別に送信）
function sendAgentCheckIn() {
  const context = buildAgentContext();
  const reply = callGemini(AGENT_PERSONA,
    '以下は現在のタスク状況です。今日の進捗チェックインとして、①最優先で手をつけるべきタスク　②停滞・放置が気になる要注意タスク　③一言アドバイス、をまとめてください。' +
    '③のアドバイスは、単なる励ましではなく、そのタスクの分野に詳しいスペシャリストとしての具体的な進め方を含めてください。\n\n' + context,
    600);
  if (!reply) return;

  let msg = '🧭 進捗チェックイン\n\n' + reply;
  msg += '\n\n' + buildDailyReport_();

  // 期限未設定タスクの棚卸し（すでに確認待ちがあれば重複して聞かない）
  const p = PropertiesService.getScriptProperties();
  const noDue = getSupabase('tasks', 'done=eq.false&deleted=eq.false&due=is.null&select=id,title&limit=10');
  if (noDue.length) {
    getUsers().forEach(uid => {
      if (!p.getProperty('PENDING_DUE_' + uid)) {
        p.setProperty('PENDING_DUE_' + uid, JSON.stringify({
          mode: 'batch',
          tasks: noDue.map(t => ({ id: t.id, title: t.title })),
          createdAt: Date.now()
        }));
      }
    });
    msg += '\n\n📅 期限未設定のタスク\n' + noDue.map(t => '・' + t.title).join('\n') +
      '\n\n期限を教えてください（例：「◯◯は6/30、△△は今日」。不要なら「なし」）。';
  }

  getUsers().forEach(uid => pushText(uid, msg));

  // 優先順位の見直し提案（別メッセージ。すでに確認待ちがあれば重複して提案しない）
  getUsers().forEach(uid => {
    if (p.getProperty('PENDING_REPRIORITIZE_' + uid)) return;
    const proposal = proposeReprioritization_(uid);
    if (proposal && proposal.indexOf('変更提案はありません') === -1) pushText(uid, proposal);
  });
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
