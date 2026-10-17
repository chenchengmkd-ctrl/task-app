# flyer-data.js について

チラシ配りアプリ（`flyer.html`）の地図データは、HTMLに直接書かず `flyer-data.js` に分けてあります。

| 中身 | 何か |
|---|---|
| `AREAS` | 配る町丁目の境界線（OpenStreetMap行政区域） |
| `POIS` | 区域内のお店・会社（Overture Maps）。各件に建物の番号が付いている |
| `BUILDINGS` | 建物の形（OpenStreetMap） |

**`flyer-data.js` は自動生成のファイルです。手で編集しないでください。**
`POIS` の建物の番号は `BUILDINGS` の並び順なので、片方だけ作り直すと番号がずれます。

## 分けてある理由

- HTMLが 465KB → 約80KB になり、毎回の転送・解析が軽くなる
- データはブラウザが保存するので、2回目以降は再取得されない
- 区域を広げてデータが増えても、HTML本体は重くならない

## 更新するとき

1. データを作り直したら、`flyer-data.js` の**内容から作った版番号**を `flyer.html` の
   `<script src="flyer-data.js?v=○○○○">` に反映する（版が変わらないと、ブラウザが古いデータを使い続ける）
2. `flyer.html` と `flyer-data.js` は**必ず同じコミットで**出す
