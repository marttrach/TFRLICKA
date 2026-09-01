# TRA-Sniper

針對國營臺灣鐵路公司「個人訂票／快速」頁面的瀏覽器自動化工具：

<https://www.trc.com.tw/tra-tip-web/tip/tip001/tip121/query>

本專案會在真正的瀏覽器工作階段中填入訂票條件，因此由官方頁面自行管理
CSRF token、action token、cookie 與表單欄位。它不重用 THSR-Sniper 的 Wicket
端點，也不嘗試破解或自動解答官方圖形驗證碼或 Google reCAPTCHA。

## 目前功能

- 身分證或護照／統一證號模式
- 單程與來回
- 依車次（最多三班）或依時段
- 一般座票數、桌型座偏好、同班車換座偏好
- 台鐵站名 autocomplete，遇到同名站時要求使用完整 `代碼-站名`
- 預設只填表、不送出
- 明確加上 `--submit` 才保留互動工作階段；使用者需完成人工驗證、核對內容並親自按「訂票」
- 設定驗證時遮罩身分證件號碼

## 初步全端移植

目前已加入原 THSR-Sniper 的三個主要產品面向，但依台鐵驗證流程縮小為安全的
human-in-the-loop 版本：

- **會員**：電子郵件註冊與登入、scrypt 密碼雜湊、HMAC Bearer token。
- **任務儲存**：SQLite 持久化；包含證件號碼的訂票 payload 使用 Fernet 加密。
- **排程器**：到點後將任務轉成 `waiting_human`，不會在背景自動解 CAPTCHA 或送單。
- **React 儀表板**：登入、建立單程依車次任務、查看狀態、取消任務、下載人工訂票設定。
- **FastAPI**：`/docs` 提供會員與任務 API 文件。

這是初步移植，儀表板目前只提供最常用的「單程、依車次」建立表單；底層 Python
模型與 CLI 已支援來回及依時段。

## 安裝

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Linux/macOS 將啟用指令改為 `source .venv/bin/activate`。

啟動本機 API：

```powershell
$env:TRA_TOKEN_SECRET = python -c "import secrets; print(secrets.token_urlsafe(48))"
tra-sniper serve
```

另一個終端啟動儀表板：

```powershell
cd frontend
pnpm install
pnpm dev
```

開啟 <http://localhost:5173>，API 文件位於 <http://localhost:8000/docs>。

也可以複製 `.env.example` 為 `.env`，設定 `TRA_TOKEN_SECRET` 後執行：

```powershell
docker compose up -d --build
```

容器版儀表板位於 <http://localhost:43123>。容器內的排程器只管理任務狀態；需要
人工驗證時，從儀表板下載設定並在具備圖形介面的電腦執行 `tra-sniper book ... --submit`。

## 使用

先複製範例，`booking.json` 已列入 `.gitignore`：

```powershell
Copy-Item config.example.json booking.json
```

編輯 `booking.json` 後驗證（輸出不會顯示完整證件號碼）：

```powershell
tra-sniper validate booking.json
```

只開啟官方頁面並填表，不送出：

```powershell
tra-sniper book booking.json --screenshot screenshots/prepared.png
```

需要實際訂票時，瀏覽器會保持開啟；請人工輸入官方圖形驗證碼、完成
reCAPTCHA、核對內容，再親自按「訂票」：

```powershell
tra-sniper book booking.json --submit --wait-seconds 600
```

`--submit` 不接受 `--headless`，也不會替使用者點擊最終訂票按鈕。

## 設定格式

`start_station`、`end_station` 建議使用官方 autocomplete 顯示的完整值，例如
`1000-臺北`。`order_type` 可為 `BY_TRAIN_NO` 或 `BY_TIME`：

```json
{
  "identity": "REPLACE_WITH_YOUR_ID",
  "start_station": "1000-臺北",
  "end_station": "3300-臺中",
  "order_type": "BY_TIME",
  "outbound": {
    "ride_date": "2026/09/15",
    "start_time": "06:00",
    "end_time": "12:00"
  }
}
```

來回票另設 `"trip_type": "ROUNDTRIP"` 並加入相同格式的 `inbound`。

## 網路與隱私界線

工具本身唯一設定的業務目標是 `www.trc.com.tw`，且本專案沒有加入遙測或分析服務。
不過，台鐵官方頁面目前自行載入 HiNet CDN 靜態資源、Google reCAPTCHA，以及 Google
Tag Manager／Analytics；因此實際瀏覽器仍會依官方頁面指示連到這些網域。工具不會把
證件號碼寫入 log；使用 `--submit` 時，表單資料會依你的明確操作送到台鐵官方網站。

請勿將 `booking.json`、瀏覽器 profile、訂票結果或截圖提交至 Git。使用者需自行遵守
台鐵規範，並負責付款、取消與未付款紀錄。

## 開發

```powershell
python -m pip install -e ".[dev]"
ruff check .
pytest
```

整合測試預設不會向官方網站送出訂票表單。

## 免責聲明

**本軟體僅供學術研究與教育用途。**

- 本專案為非官方實作，與國營臺灣鐵路股份有限公司（TRC）無任何關聯
- 使用風險由使用者自行承擔
- 使用者需自行遵守相關法規與台鐵使用規範
- 開發者不對任何使用造成的損害或法律問題負責
- 本工具旨在網頁自動化與 CLI 開發之教學與研究
