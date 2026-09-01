<div align="center">

# 🚆 TRA-Sniper

### 台鐵訂票任務管理與瀏覽器輔助工具

從建立行程、排程提醒到開啟官方訂票頁面，\
用一個清楚、簡潔的儀表板管理你的訂票任務。

[![License](https://img.shields.io/github/license/marttrach/TFRLICKA?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/marttrach/TFRLICKA/test.yml?branch=main&style=flat-square&label=tests)](https://github.com/marttrach/TFRLICKA/actions)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-TypeScript-149ECA?style=flat-square&logo=react&logoColor=white)](https://react.dev/)

</div>

## ✨ 功能特色

- **會員系統** — 註冊、登入與個人任務管理
- **React 儀表板** — 建立、查看及取消訂票任務
- **預約排程** — 到達指定時間後提醒使用者接手訂票
- **多種行程** — 支援單程、來回、依車次或時段查詢
- **瀏覽器輔助** — 自動開啟台鐵官方頁面並填入訂票條件
- **圖片 OCR** — 上傳圖片辨識繁體中文與英文，支援網頁及 CLI
- **資料保護** — 密碼雜湊、Token 驗證與敏感資料加密儲存
- **Docker 部署** — 一個指令啟動 API、排程器與前端介面

> TRA-Sniper 採用人工確認流程。驗證碼、reCAPTCHA 與最後訂票動作皆由使用者本人完成。

## 🚀 快速開始

```bash
git clone https://github.com/marttrach/TFRLICKA.git
cd TFRLICKA
cp .env.example .env
```

編輯 `.env`，將 `TRA_TOKEN_SECRET` 換成至少 32 字元的隨機字串，接著啟動服務：

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
python -m pip install -e .
python -m playwright install chromium
cp config.example.json booking.json
tra-sniper book booking.json
```

加入 `--submit` 可讓瀏覽器保持開啟，方便完成驗證與最後確認：

```bash
tra-sniper book booking.json --submit --wait-seconds 600
```

一般圖片文字辨識：

```bash
tra-sniper ocr image.png --language zh-TW
```

網頁上傳的圖片只在記憶體中處理，不會寫入資料庫；支援 PNG、JPEG、WebP，單檔上限 8 MB。

## 🛡️ 免責聲明

**本軟體僅供學術研究與教育用途。**

- 本專案為非官方實作，與國營臺灣鐵路股份有限公司（TRC）無任何關聯
- 使用者須自行遵守相關法規及台鐵網站規範
- 使用本軟體所產生的風險、損害或法律責任由使用者自行承擔
- 本工具不會自動破解驗證碼，也不會代替使用者完成最終訂票

## 📄 License

本專案採用 [Apache License 2.0](LICENSE)。
