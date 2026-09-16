# チラシ配り管理（flyer.html）セットアップ

チラシ配り（飛び込み営業）を最大3人で分担するための地図アプリです。
「どの建物を回ったか」を全員でリアルタイムに共有し、重複訪問を防ぎます。

既存のタスク管理アプリと同じ **GitHub Pages ＋ 同じ Supabase** をそのまま使うので、
新しいアカウントを作る必要はありません。**費用は一切かかりません。**

| 使うもの | 何のため | 料金 |
|---|---|---|
| OpenStreetMap + Leaflet | 地図の表示 | 無料 |
| 国土地理院タイル | 淡色地図・航空写真 | 無料 |
| Supabase（今使っているもの） | ピンの保存・3人での共有 | 無料枠内 |
| GitHub Pages（今使っているもの） | 公開・HTTPS（GPSに必須） | 無料 |

**あなたの作業は STEP 1 と STEP 2 だけ**です。

---

## STEP 1. Supabase にテーブルを2つ作る（1回だけ）

1. [Supabase](https://supabase.com/dashboard) を開き、今使っているプロジェクト（`xldkfkhgazpugfuscpqt`）を選ぶ
2. 左メニューの **SQL Editor**（`</>` のアイコン）を開く
3. **New query** を押し、下をまるごと貼り付けて **RUN**（右下の緑ボタン）を押す

```sql
create table if not exists flyer_pins (
  id text primary key,
  lat double precision not null,
  lng double precision not null,
  status text not null default 'todo',
  label text default '',
  note text default '',
  author text default '',
  deleted boolean default false,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_flyer_pins_updated on flyer_pins(updated_at);

create table if not exists flyer_settings (
  id text primary key,
  data jsonb default '{}'::jsonb,
  updated_at timestamptz default now()
);

alter table flyer_pins disable row level security;
alter table flyer_settings disable row level security;

-- 他のメンバーの画面に即座に反映させるための設定
do $$ begin
  alter publication supabase_realtime add table flyer_pins;
exception when others then null; end $$;
do $$ begin
  alter publication supabase_realtime add table flyer_settings;
exception when others then null; end $$;
```

4. 「Success. No rows returned」と出れば完了です。

---

## STEP 2. 公開する

このリポジトリに `flyer.html` を push するだけです。1〜2分で公開されます。

**https://chenchengmkd-ctrl.github.io/task-app/flyer.html**

このURLを、チラシ配りをする3人のスマホでブックマーク（ホーム画面に追加）してください。
タスク管理アプリの左上からも「📍 チラシ配り」で開けます。

> **GPSを使うのでHTTPSが必須です。** GitHub Pages は最初からHTTPSなのでそのまま使えます。

---

## 最初に一度だけやること（アプリ内で）

誰か1人がやれば、全員に共有されます。

1. アプリを開くと **名前** を聞かれるので入れる（ログインはありません。端末ごとに1回だけ）
2. 続いて **設定** が開くので、**お店の位置** を決める
   - お店にいるなら「**現在地にする**」がいちばん簡単
   - お店にいないなら「**地図で選ぶ**」→ 地図を動かして中心をお店に合わせ「ここに決定」
3. **配る範囲** を選ぶ（徒歩5分＝400m／10分＝800m／15分＝1.2km）

これで地図はそのエリアだけに固定され、範囲外にはスクロールできなくなります。

---

## 使い方

### ピンを立てる（歩きながら）

1. 右下の青い「**＋ピン**」を押す → 追加モードになります
2. 下のバーで状況（**配布済み** など）を選ぶ
3. あとは **建物をタップするだけ** でどんどんピンが立ちます
4. 間違えたら下のバーの「**取消**」で直前の1件を取り消せます
5. 終わったら「**やめる**」で追加モードを抜けます

メモを書きながら立てたいときは、**地図を長押し**してください（編集画面が一緒に開きます）。

### ピンを直す

立っているピンをタップすると下から編集画面が出ます。

- **状況**：配布済み／未訪問／要再訪／配布不可 の4つ。押した瞬間に保存され、他のメンバーにも反映されます
- **建物名・部屋番号**：「〇〇ハイツ 101〜305」など
- **メモ**：「管理人さんに許可もらった」「ポスト共用部のみ」など
- 最終更新の欄に **誰が・いつ** 触ったかが出ます

### 進捗を見る

- 画面上のチップに **配布済み / 未訪問 / 要再訪 / 配布不可** の件数が出ます
- その下の緑のバーが **配布済みの割合** です
- チップを押すと、その種類のピンだけ地図から隠す／出すの切り替えができます
- 右上の **☰** でピンの一覧。「地図で見る」でその場所へ飛べます

### 現在位置

右下の **◎** ボタンで、自分の青い点に地図を寄せます。
初回はブラウザから位置情報の許可を聞かれるので「許可」を押してください。

### 次のラウンドを始める

右上の **⚙** →「**すべてのピンを「未訪問」に戻す**」。
建物のピンはそのまま残り、状況だけリセットされるので、2回目以降の配布がすぐ始められます。

---

## 3人での共有について

- 誰かがピンを追加・変更すると、**他の人の画面に数秒で反映**されます
- 反映されないときでも、**12秒ごとに自動で取り直し**ます（画面を閉じて開き直しても最新になります）
- 同じピンを2人が同時に触った場合は、**あとから変更したほうが残ります**
- 電波が悪いところでピンを立てても消えません。画面右上が「**未送信 ○件**」になり、電波が戻ったら自動で送信されます

---

## こんなときは

| 症状 | 対処 |
|---|---|
| 右上に「**準備が必要**」と出る | STEP 1 のSQLがまだです。実行してから画面を再読み込みしてください |
| 右上に「**オフライン**」と出る | 電波が届いていません。記録は端末に残るので、そのまま続けて大丈夫です |
| 現在地が出ない | ブラウザの設定で位置情報が拒否されています。iPhoneは「設定 → Safari → 位置情報」、Androidは「Chrome → サイトの設定 → 位置情報」を許可に |
| 地図が見づらい | ⚙ →「地図の種類」を切り替えてください。**淡色地図**は文字が読みやすく、**航空写真**は建物の形で判断できます |
| エリアを広げたい／お店を移転した | ⚙ から「配る範囲」「お店の位置」をいつでも変更できます（全員に共有されます） |
| ピンを消したい | ピンをタップ →「削除」 |
