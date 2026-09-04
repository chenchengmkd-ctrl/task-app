# 思考トレース（study.html）セットアップ

特定の人物の発信（YouTube・記事・X）を取り込んで、その人の考え方で壁打ち・訓練・深掘りができる Web アプリです。
既存の「タスク管理アプリ」と同じ GitHub Pages サイト・同じ Supabase・同じ Vercel をそのまま使います。

あなたの作業は **STEP 1（Supabase で3テーブル作成）だけ** です。あとはこのリポジトリに push すれば公開されます。

---

## STEP 1. Supabase にテーブルを3つ作る（1回だけ）

1. [Supabase](https://supabase.com/dashboard) を開き、今使っているプロジェクト（`xldkfkhgazpugfuscpqt`）を選ぶ
2. 左メニューの **SQL Editor**（`</>` のアイコン）を開く
3. **New query** を押し、下をまるごと貼り付けて **RUN**（右下の緑ボタン）を押す

```sql
create table if not exists mentors (
  id text primary key,
  name text not null,
  note text default '',
  profile text default '',
  profile_updated_at timestamptz,
  created_at timestamptz default now()
);

create table if not exists mentor_sources (
  id text primary key,
  mentor_id text not null,
  kind text not null default 'youtube',
  url text,
  title text default '',
  status text default 'pending',
  transcript text default '',
  summary text default '',
  quotes jsonb default '[]'::jsonb,
  meta jsonb default '{}'::jsonb,
  error text default '',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_mentor_sources_mentor on mentor_sources(mentor_id);

create table if not exists mentor_chats (
  id text primary key,
  mentor_id text not null,
  mode text not null default 'spar',
  role text not null,
  content text not null,
  created_at timestamptz default now()
);
create index if not exists idx_mentor_chats_mentor on mentor_chats(mentor_id, mode, created_at);
```

4. 「Success. No rows returned」と出れば完了です。

> もし後で画面に「permission denied」や「row-level security」というエラーが出た場合は、
> SQL Editor でもう一度 New query を開き、次を RUN してください（他のテーブルと同じ運用に揃えます）:
> ```sql
> alter table mentors disable row level security;
> alter table mentor_sources disable row level security;
> alter table mentor_chats disable row level security;
> ```

---

## STEP 2. 公開する

このリポジトリに変更を push するだけです。

- `study.html` … GitHub Pages が自動で公開 → **https://chenchengmkd-ctrl.github.io/task-app/study.html**
- `line-bot-vercel/` のAPI追加分 … Vercel が自動でデプロイ

反映まで1〜2分。タスク管理アプリの画面の左上「タスク管理へ」の逆で、思考トレース側にも「タスク管理へ」リンクがあります。ブックマーク推奨。

---

## STEP 3.（任意）動画の文字起こしを高精度モデルにする

既定では LINE ボットと同じ Gemini モデル（`GEMINI_MODEL`）を使います。動画の書き起こし精度を上げたい場合：

1. Vercel のプロジェクト設定 → **Settings → Environment Variables**
2. `MENTOR_GEMINI_MODEL` を追加し、動画対応のより強いモデル名を入れる
3. **Redeploy**（環境変数は再デプロイしないと反映されません）

無料枠で使える範囲のモデル名にしてください（請求先アカウントが要るモデルは使わない方針）。

---

## STEP 4.（任意）APIに簡単な鍵をかける

`study.html` の URL は誰でも開けます（タスク管理アプリと同じ）。中身は自分の Supabase なので他人には見えませんが、
AI 呼び出し（無料枠の Gemini）を他人に叩かれたくない場合：

1. Vercel の環境変数に `MENTOR_APP_TOKEN` を追加（適当な長い文字列）→ Redeploy
2. `study.html` の先頭付近 `const APP_TOKEN = '';` に同じ文字列を入れて push

---

## 使い方

| タブ | できること |
|---|---|
| **ソース** | YouTube URL / 記事 URL / X・テキストを取り込む。全文（逐語）＋逐語引用＋要約の3層で保存。全文は「手直し」で誤変換を修正可 |
| **思考プロファイル** | 取り込んだソースを横断して、判断基準・思考の型・口ぐせをまとめる。ソースを足したら「再生成」 |
| **壁打ち** | 3モード切替。壁打ち＝その人になりきって応答／訓練＝自分の考えとの差分を指摘／深掘り＝取り込んだ内容だけを引用つきで回答 |
| **人物設定** | 人物の追加・改名・削除。上部のプルダウンでいつでも切り替え |

### 注意点

- **X**：API が無料で使えないため、ポスト本文を「X・その他テキスト」で手動コピペしてください
- **YouTube**：字幕が無くても音声から書き起こします。1時間の動画で数分かかります（画面を開いたままに）。非公開・限定公開・年齢制限・ライブ配信は取り込めません
- **記事**：ログインや JavaScript が必要なページは本文を取得できないことがあります。その場合は本文をコピーして「テキスト」で貼り付け
- 自動の書き起こしは固有名詞・専門用語を間違えます。「手直し」で直すほど、以降の壁打ちの精度が上がります
- 一度に何本もまとめて取り込むと、無料枠の1分あたり回数制限で止まります。1本ずつどうぞ（止まっても「続きから取り込む」で再開できます）


---

## （応用）会議の議事録から自動で育てる

会議で毎回助言してくれる人（上司・顧問など）がいるなら、その人の指摘を
議事録から自動で取り込めます。

1. 「人物設定」タブでその人を登録（または既存の人物を選ぶ）
2. **メモ欄に `#会議` と書く**（他の言葉と一緒でもOK。例:「毎回指摘してくれる上司 #会議」）

以降、議事録ツールで会議を取り込むたびに、その人の指摘・助言・考え方だけが
抽出されて「ソース」に追加され、思考プロファイルが自動で更新されます。
会議が5〜10本たまると、その人の「考え方の型」がかなり掴めるようになります。

会議を取り込む側の手順は `MEETINGS_SETUP.md` を参照。
