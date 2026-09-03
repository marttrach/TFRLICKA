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
2. 輸入乘車日期、起訖站與車次條件
3. 建立立即或預約訂票任務
4. 任務就緒後下載設定並開啟台鐵官方訂票頁面
5. 完成人工驗證、確認資料並自行送出訂票

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

只有 `TRA_WEBHOOK_URL` 與 `TRA_WEBHOOK_SECRET` 都存在時才會啟用。請求以
`X-TRA-Signature: sha256=<hex>` 傳送 HMAC-SHA256 簽章；簽章原文是收到的原始
JSON bytes。通知只包含任務編號、日期、路線、最多三筆時刻候選與任務連結，
不會傳送身分證或台鐵會員帳密。通知失敗只會寫入日誌，不會阻止任務進入
「需要人工」狀態。

## 🛡️ 免責聲明

**本軟體僅供學術研究與教育用途。**

- 本專案為非官方實作，與國營臺灣鐵路股份有限公司（TRC）無任何關聯
- 使用者須自行遵守相關法規及台鐵網站規範
- 使用本軟體所產生的風險、損害或法律責任由使用者自行承擔

## 📄 License

本專案採用 [Apache License 2.0](LICENSE)。
