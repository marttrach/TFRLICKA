<div align="center">

# 🚆 好搭車

### 清楚易用的台鐵訂票任務與瀏覽器輔助工具

從建立行程、排程提醒到開啟官方訂票頁面，\
用一個清楚、簡潔的儀表板管理你的訂票任務。

[![License](https://img.shields.io/github/license/marttrach/TFRLICKA?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/marttrach/TFRLICKA/test.yml?branch=main&style=flat-square&label=tests)](https://github.com/marttrach/TFRLICKA/actions)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-TypeScript-149ECA?style=flat-square&logo=react&logoColor=white)](https://react.dev/)

</div>

## ✨ 功能特色

- **會員系統** — 註冊、登入與個人任務管理
- **常用資料** — 加密保存身分證與台鐵會員登入資料，減少重複輸入
- **無障礙介面** — 大字、高對比、全中文提示與清楚的操作步驟
- **React 儀表板** — 建立、查看及取消訂票任務
- **預約排程** — 到達指定時間後提醒使用者接手訂票
- **Webhook 通知** — 任務就緒時可通知 n8n 等服務並直接開啟該任務
- **多種行程** — 支援單程、來回、依車次或時段查詢
- **時刻建議** — 透過 TDX 排序對號、非對號、鄰近時段與單次轉乘選項
- **離線候選** — 建立任務時加密保存候選，等待人工處理時不必重新查詢
- **瀏覽器輔助** — 自動開啟台鐵官方頁面並填入訂票條件
- **可替換驗證介面** — 預留 `manual`、本機 `mock` 與未來核准 `official_api` 供應器
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

```bash
docker compose pull
docker compose up -d
```

服務啟動後：

| 服務 | 網址 |
| --- | --- |
| 儀表板 | <http://localhost:43124> |
| API 文件 | <http://localhost:48100/docs> |

## 🎯 使用流程

1. 建立會員並登入儀表板
2. 新增一位或多位「常用乘車人」（每位一組名稱與身分證，加密保存）
3. 先選縣市、再選車站，設定乘車日期與車次／時段條件
4. 選擇執行模式與查詢間隔，開始監控
5. 任務就緒後開啟訂票畫面，完成官方驗證並自行送出訂票

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
| `scheduled_at`／`monitor_start_at` | 何時**開始**監控 | 立即 |
| `poll_interval_seconds` | 瀏覽器忙碌時，多久重試準備頁面 | 300 秒（5 分鐘） |
| `monitor_until` | 監控**截止**時間 | 未設定＝單次執行 |

執行模式：`monitor_only`（到點提醒一次）或 `book_when_available`（到點自動開頁填表）。
兩者都在交接後暫停，不會每個間隔重新通知。

> **通知代表需要你接手，不代表有位。** 任務回應中的 `availability` 恆為 `unknown`。
> 會員登入需要人工操作，或訂票表單填妥時，保留同一個瀏覽器頁面，
> 每次 session 只嘗試發送一次 `task.waiting_human` 通知。
> 開啟通知中的任務連結並登入，再按「開啟驗證畫面」即可接回原頁面。
> 即使沒有顯示驗證碼，送出仍由本人確認操作；自動判讀無位後重送尚未啟用。

防重複與退避規則：

- 同一任務不會同時執行兩次查詢（資料庫層 compare-and-swap，重啟後依然有效）
- 訂票 session 進行中會離開可輪詢狀態，**不會每 5 分鐘再開一個瀏覽器**
- 瀏覽器忙碌時等下一個設定間隔；頁面準備失敗則停止並記錄錯誤
- 訂位成功、使用者取消、監控截止後立即停止
- 頁面保留最長 15 分鐘，且不超過 `monitor_until`；逾時後關閉頁面
- `failed`／`timeout`／`cancelled` **都不自動恢復監控**；需要再試時自行建立新任務
- 取消或逾時先停止 session，等原瀏覽器 context 關閉後才開放下一個任務
- 通知失敗只記錄日誌，不重開頁面或重複通知；結束時另發一次訂票結果事件

## 🧰 CLI 模式

安裝 Python 3.11 以上版本後：

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
cp config.example.json booking.json
tra-sniper book booking.json
```

加入 `--submit` 可讓瀏覽器保持開啟，方便完成驗證與最後確認：

```bash
tra-sniper book booking.json --submit --wait-seconds 600
```

若台鐵要求 CAPTCHA 或 reCAPTCHA，程式會停下來等待使用者完成官方驗證，
不會自動辨識、送出或繞過驗證。可使用瀏覽器縮放、官方重新產生／語音播放，
或請可信任家人協助。

`TRA_VERIFICATION_PROVIDER` 正式環境維持 `manual`。`mock` 有強制 localhost
限制，只用來測試未來驗證交接流程；`official_api` 已保留穩定介面，但在取得
台鐵核准的端點、認證與回應格式以前會明確回報尚未設定。

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
| `task.waiting_human` | 頁面可供人工接手時一次；純提醒模式則為排程到期時一次 |
| `task.booking_result` | 訂票 session 結束 |

`task.booking_result` 的 `status` 可能是 `completed`、`failed`、`timeout`、
`cancelled` 四者之一。

通知只包含任務編號、日期、路線、狀態、訂位代碼、最多三筆時刻候選與任務連結，
**不會傳送身分證、台鐵會員帳密，也不會傳送 token 本身或訂票 session 連結**。
通知失敗只會寫入日誌，不會改變任務狀態，也不會重跑訂票。

## 🛡️ 免責聲明

**本軟體僅供學術研究與教育用途。**

- 本專案為非官方實作，與國營臺灣鐵路股份有限公司（TRC）無任何關聯
- 使用者須自行遵守相關法規及台鐵網站規範
- 使用本軟體所產生的風險、損害或法律責任由使用者自行承擔

## 📄 License

本專案採用 [Apache License 2.0](LICENSE)。
