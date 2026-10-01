<div align="center">

# 🚆 好搭車

### 清楚易用的台鐵訂票任務與 AI 助理工具

從查班次、排程提醒到送出官方訂票連結，\
用儀表板或你的 AI 助理（Hermes 等 MCP client）管理訂票任務。

[![License](https://img.shields.io/github/license/marttrach/TFRLICKA?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/marttrach/TFRLICKA/test.yml?branch=main&style=flat-square&label=tests)](https://github.com/marttrach/TFRLICKA/actions)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-TypeScript-149ECA?style=flat-square&logo=react&logoColor=white)](https://react.dev/)

</div>

## ✨ 功能特色

- **會員系統** — 註冊、登入與個人任務管理
- **常用資料** — 加密保存乘車人資料；訂票直接填表，不先登入台鐵會員
- **無障礙介面** — 大字、高對比、全中文提示與清楚的操作步驟
- **React 儀表板** — 建立、查看及取消訂票任務
- **預約排程** — 到達指定時間後，每個間隔提醒一次並附官方訂票連結
- **Webhook 通知** — 提醒可送到 n8n、Hermes 等服務，轉發到 Telegram 讓人直接點開
- **AI 助理 (MCP)** — 查車站、查班次、開監控、拿訂票連結、回報訂到，都能交給 agent
- **多種行程** — 支援單程、來回、依車次或時段查詢
- **時刻建議** — 透過 TDX 排序對號、非對號、鄰近時段與單次轉乘選項
- **離線候選** — 建立任務時加密保存候選，等待人工處理時不必重新查詢
- **官方導訂連結** — 透過 [TDX MCP 服務](https://github.com/tdxmotc/MCP) 開啟已填好日期、起訖站、車次與張數的台鐵訂票頁
- **資料保護** — 登入節流、可撤銷 Token 與敏感資料加密儲存
- **Docker 部署** — 一個指令啟動 API、排程器與前端介面

> TDX 不提供台鐵即時餘位。畫面中的對號／非對號是車種屬性，所有候選皆為時刻建議，
> 不代表有位或可訂；轉乘方案需要分開購買兩張車票。

## 🚀 快速開始

```bash
git clone https://github.com/marttrach/TFRLICKA.git
cd TFRLICKA
cp .env.example .env
```

編輯 `.env`，將 `TRA_TOKEN_SECRET` 換成至少 32 字元的隨機字串，接著啟動服務：

若要使用完整車站與時刻建議，再填入 TDX 的 `TDX_CLIENT_ID`、
`TDX_CLIENT_SECRET`；未設定時 API 仍可使用熱門站與依車次模式。

同一組金鑰必須在 TDX 會員中心開通「臺鐵訂票導訂」，才能產生官方訂票連結：
它向 [TDX MCP 服務](https://github.com/tdxmotc/MCP) 取得有時效的連結，在你自己的
瀏覽器開啟已帶入日期、起訖站、車次與張數的台鐵訂票頁。身分證字號不會送給 TDX，
由你在官方頁面輸入、通過驗證並送出；訂到後按「我訂到了」記錄電腦代碼，提醒就會停止。

映像檔（API、前端）由 GitHub Actions 建置，推送到 Docker Hub 並保留
GHCR 副本。在 `.env` 或 Portainer 設定 `DOCKERHUB_USER` 為你的 Docker Hub 帳號：

```bash
docker compose pull
docker compose up -d
```

CI 需要 repository secrets `DOCKERHUB_USERNAME` 與 `DOCKERHUB_TOKEN`（Read & Write）
才會推送 Docker Hub；未設定時只推送 GHCR。另設 `PORTAINER_WEBHOOK_URL` 時，兩個映像檔
全部推送完成後才觸發 Portainer 重新部署，避免拉到舊的 `:latest`。

服務啟動後：

| 服務 | 網址 |
| --- | --- |
| 儀表板 | <http://localhost:43124> |
| API 文件 | <http://localhost:48100/docs> |
| AI 助理 MCP | <http://localhost:48100/mcp> |

## 🎯 使用流程

1. 建立會員並登入儀表板（或讓 AI 助理代你操作，見下方「AI 助理」）
2. 先選縣市、再選車站，設定乘車日期與車次／時段條件
3. 選擇執行模式與提醒間隔，開始監控
4. 到點後每個間隔收到一次提醒（儀表板、webhook 或 AI 助理），附官方訂票連結
5. 點開連結，在台鐵官方頁面輸入身分證、通過驗證並自行按下訂票
6. 訂到後按「我訂到了」輸入電腦代碼，任務完成、提醒停止

驗證（reCAPTCHA／圖形驗證碼）在你自己的瀏覽器裡完成，系統不辨識、不代填、不送出。
「常用乘車人」仍可保存，但官方連結不會帶入身分證；需要時可用瀏覽器的自動填入。

## 🤖 AI 助理（Hermes 等 MCP client）

API 內建 MCP 端點 `/mcp`（Streamable HTTP），讓 agent 走完整個流程，只把最後
「輸入身分證與驗證碼、按訂票」留給你：

| 工具 | 用途 |
|---|---|
| `find_stations` | 站名轉站碼，例如「板橋」→ `1020-板橋` |
| `search_trains` | 查某日兩站間的班次（TDX 時刻表，不含餘位） |
| `create_booking_task` | 開始監控 1～3 個車次，每個間隔發提醒 |
| `list_tasks` | 列出任務、狀態與 `booking_url` |
| `get_booking_link` | 立即取一條新的官方訂票連結，傳給你點開 |
| `report_booked` | 記錄你訂到的電腦代碼，結束提醒 |
| `cancel_task` | 停止任務 |

在 Portainer 設定：

```dotenv
TRA_AGENT_TOKEN=至少-32-字元的隨機字串
TRA_AGENT_EMAIL=你在儀表板註冊的-email
```

agent 以 `TRA_AGENT_EMAIL` 這個帳號的身分操作，`TRA_AGENT_TOKEN` 未設定時 `/mcp`
一律回 401。在 Hermes 的 MCP 設定加入這個 HTTP server：

- 網址：`http://你的NAS:48100/mcp`（或經前端的 `http://你的NAS:43124/api/mcp`）
- 標頭：`Authorization: Bearer <TRA_AGENT_TOKEN>`

同樣的設定也適用 Claude Code：
`claude mcp add --transport http tra-sniper http://你的NAS:48100/mcp --header "Authorization: Bearer <TRA_AGENT_TOKEN>"`

## 📱 手機操作

介面以手機直向為主：單欄排版、任務以卡片呈現、觸控目標至少 44 × 44 px、
輸入欄位 16px（避免 iOS 自動放大），並支援瀏海與底部安全區域。
已實測 360 / 390 / 430 CSS px 三種寬度**不產生整頁水平捲動**。

車站採兩級選擇：**先選縣市，再選該縣市的車站**。縣市清單由 TDX 車站資料的
`LocationCity` 欄位產生（缺漏時退回站址前綴），不依站名猜測，因此沒有台鐵車站
的縣市不會出現。熟悉站名者可勾選「搜尋全部車站」跳過兩級選單。改選縣市時，
若原車站不屬於新縣市會被清除，不會偷偷保留錯誤值。

## ⏱️ 週期監控

任務有三個獨立的時間概念，**不要混為一談**：

| 欄位 | 意義 | 預設 |
|---|---|---|
| `scheduled_at`／`monitor_start_at` | 何時**開始**提醒 | 立即 |
| `poll_interval_seconds` | 沒回報訂到時，多久再提醒一次 | 300 秒（5 分鐘） |
| `monitor_until` | 提醒**截止**時間 | 未設定＝直到回報訂到或取消 |

執行模式：`monitor_only`（到點提醒一次）或 `book_when_available`（每個間隔提醒一次）。

> **提醒代表「現在可以去訂」，不代表有位。** 台鐵沒有可用的餘位開放資料，
> 任務回應中的 `availability` 恆為 `unknown`；有沒有位只有你按下訂票後才知道。

- 同一任務不會同時被兩個排程處理（資料庫層 compare-and-swap，重啟後依然有效）
- 回報訂到、取消或超過監控截止後立即停止
- 提醒送不出去只記錄日誌，下一個間隔會再提醒
- 修改日期、站點或查詢時段後，從時刻建議選取的車次會解除鎖定，必須重新選擇

## 🧰 CLI

```bash
python -m pip install -e .
tra-sniper serve              # 啟動 API 與排程器
tra-sniper validate booking.json
tra-sniper ocr screenshot.png
```

### 開發測試

執行 `pytest`。測試只使用本機假資料，不會向台鐵或 TDX 送出請求。

## 🔔 任務就緒通知

如需接到 n8n 或其他 HTTP webhook，設定以下三個環境變數：

```dotenv
TRA_WEBHOOK_URL=https://你的-webhook-網址
TRA_WEBHOOK_SECRET=至少-32-字元的獨立隨機密鑰
TRA_PUBLIC_URL=http://你的NAS:43124
```

只有 `TRA_WEBHOOK_URL` 與 `TRA_WEBHOOK_SECRET` 都存在時才會啟用。

### 認證方式

請求以 **Header Auth** 認證，`TRA_WEBHOOK_SECRET` 的值原樣放在 `X-TRA-Token`
標頭送出（不加 `Bearer ` 或 `sha256=` 前綴），對應 n8n Webhook 節點的
Header Auth credential。

因為 token 是持有即可用的憑證，發送端有兩道保護：

- **URL 必須是 HTTPS**（loopback 位址除外），否則拒絕送出並記錄錯誤，
  避免 token 以明文上線
- **不跟隨 HTTP 轉址**，避免 3xx 把帶著 token 的請求轉發到其他主機

### 事件

| 事件 | 觸發時機 |
|---|---|
| `task.waiting_human` | 每次提醒（每個間隔一次；純提醒模式只有一次） |
| `task.booking_result` | 回報訂到時 |

`task.booking_result` 的 `status` 為 `completed`，並附上 `booking_code`。

通知只包含任務編號、日期、路線、狀態、訂位代碼、最多三筆時刻候選與任務連結，
**不會傳送身分證、台鐵會員帳密，也不會傳送 token 本身**。
通知失敗只會寫入日誌，不會改變任務狀態。

設定了 TDX 金鑰時，`task.waiting_human` 會多一個 `booking_url`，可直接轉發到
Telegram 等通訊軟體讓人點開：它不需登入，點下去才向 TDX 取一條新的官方訂票連結並
轉址過去（TDX 連結只有幾分鐘效期，所以不直接放進通知）。連結以
`TRA_TOKEN_SECRET` 簽章、只在任務進行中有效，網址為
`TRA_PUBLIC_URL` 加上 `/api/tasks/<id>/booking-link/open`，所以 `TRA_PUBLIC_URL`
必須是收訊裝置連得到的位址。拿到連結的人都能開啟，請只轉給訂票的本人。

## 🛡️ 免責聲明

**本軟體僅供學術研究與教育用途。**

- 本專案為非官方實作，與國營臺灣鐵路股份有限公司（TRC）無任何關聯
- 使用者須自行遵守相關法規及台鐵網站規範
- 使用本軟體所產生的風險、損害或法律責任由使用者自行承擔

## 📄 License

本專案採用 [Apache License 2.0](LICENSE)。
