# 訂票流程重整設計

日期：2026-09-05
狀態：§1～§4 已實作；§5 改為自動準備、暫停並通知一次（2026-09-05）

## 背景

使用者建立了一個「1180-竹北 → 2200-大甲、2026/09/25、依時段」的任務，結果是訂票失敗，
而且從任務卡片上看不出來這個任務到底在訂什麼車。追查後發現三層互相獨立的問題：

1. CDP 連不上（已於 `db6fc30` 修復，不在本設計範圍）。
2. 官方訂票頁改版，`_prepare_form` 幾乎每一個 selector 都已失效。
3. 產品流程本身：任務可以在「還沒決定要搭哪一班車」的狀態下建立，
   卡片不顯示車次，沒有刪除鍵，訂票失敗後任務就死掉不再重試。

本設計處理 2 與 3。

## 已驗證的事實

以下全部來自 2026-09-05 對 `https://www.trc.com.tw/tra-tip-web/tip/tip001/tip121/query`
的實際 DOM 探測（只讀取，未送出任何請求）。

### 分頁與網址

「依車次／依時段」「單程／雙行程」是四個分頁連結，**各自是不同網址**，
不是同一頁上的 radio：

| 分頁 | 網址 |
| --- | --- |
| 依車次單程訂票 | `/tip001/tip121/query` |
| 依時段單程訂票 | `/tip001/tip122/tripOne/byTime` |

`orderType` 與 `tripType` 現在是 **hidden input**，值由所在網址決定。
現行程式碼對它們呼叫 `.check()`，必定拋出 `Not a checkbox or radio button`。

### 表單控制項對照

| `automation.py` 現行寫法 | 官方頁面實際 |
| --- | --- |
| `input[name='orderType'][value=…]`＋`.check()` | hidden input，改由網址決定 |
| `input[name='tripType'][value=…]`＋`.check()` | hidden input，改由網址決定 |
| `#startStation` ＋ `ul.ui-autocomplete` | `select#startStation0`，option value 為站碼 |
| `#endStation` ＋ `ul.ui-autocomplete` | `select#endStation0` |
| `#rideDate1`＋`.fill()` | `select#rideDate0`，option value 為 `YYYY/MM/DD`，僅開放約 30 天 |
| `#normalQty`＋`.fill()` | `select#normalQty0` |
| `input[name='…seatPref'][value=…]`＋`.check()` | `select#seatPref0`（`NONE` / `TABLE`） |
| `#chgSeat1`＋`.set_checked()` | `select#chgSeat0`（`true` / `false`） |
| `#startOrEndTime{n}` radio | 依車次分頁上不存在 |
| 索引 `suffix = leg_index + 1` | name 為 0-based（`ticketOrderParamList[0]`） |

車次欄位：`ticketOrderParamList[0].trainNoList[0]`（id 為 `trainNo1`）、
`[1]`、`[2]`。注意 name 是 0-based 而 id 是 1-based，兩者不一致。

站碼對應：官方 option 形如 `value="1180"` / text `"1180-竹北"`，
而 `api.py` 的 `POPULAR_STATIONS` 存的是 `"1180-竹北"`。
取 `value.split("-")[0]` 即得站碼，無歧義。

### 車種

只存在於**依時段**分頁：

```
select name='ticketOrderParamList[0].trainTypeList[0]'
  ALL=全部  11=自強(3000)  1=太魯閣  2=普悠瑪  3=自強  4=莒光  5=復興
```

依車次分頁沒有這個欄位——車次號碼本身即決定車種。
**因此「同時指定車次與車種」在官方表單上無法表達，兩者只能二選一。**

### 驗證機制

查詢表單上有 `g-recaptcha-response`、`action-token`、`action-name=submit_form`、
`isSecondVerify=true`。後續探測看到「因 v3 驗證未通過，請輸入驗證碼」、
`#codeimg` 與 `#verifyCode`，表示驗證可能在送出查詢前要求人工完成。
原先「有位才出驗證碼」的前提撤回；目前也無法由這段提示判定失敗原因、
sidecar 每次是否遇到驗證，或持久 profile 是否有效。

## 決策

| 議題 | 決定 | 理由 |
| --- | --- | --- |
| 車種語意 | 只作為 tra-sniper 建議清單的篩選器 | 官方二選一；使用者要求 VNC 必須落在指定車次，因此任務一律走依車次 |
| 任務建立 | 必須先選定車次才能加入佇列 | 同上；VNC 打開時看到的必須是使用者自己挑過的車 |
| 人工交接 | 到點自動準備，遇驗證或需要確認送出時暫停，只通知一次 | 使用者接受的修訂方向 |
| 驗證碼 | 一律停在人工那一步，不辨識、不繞過 | 專案既有立場；本設計不改變 |

### 「自動送出」的界線

這版不自動按送出。是否顯示驗證與是否有位分開看待；未確認送出是否直接成立訂位，
且沒有經確認的無位回應之前，先完成自動準備與人工交接。

## 非目標

- 不辨識、不代解、不繞過任何驗證。
- 不實作雙行程（ROUNDTRIP）。現行 UI 寫死 ONEWAY（`App.tsx:588`），
  model 保留 ROUNDTRIP 定義與驗證，但 automation 遇到即明確拒絕。
- 不碰餘票預測。系統仍然無法在送出前得知任何車次是否有位。

---

## §1 填表層重寫（`automation.py`）

任務一律走依車次單程，`BOOKING_URL` 固定為 `/tip001/tip121/query`。

改寫 `_prepare_form` 與 `_fill_leg`，全數改用上表的新 selector：

- 站別：`select_option(value=站碼)`，站碼由 `"1180-竹北".split("-")[0]` 取得。
- 日期：`select_option("#rideDate0", value="YYYY/MM/DD")`。官方僅開放約 30 天，
  **日期不在 option 中時必須拋出明確錯誤**，訊息含官方可選範圍，不得靜默失敗。
- 車次：`#trainNo1` 及 `trainNoList[1]`、`[2]`。
- 張數／座位偏好／換座：對應三個 `select`。
- `orderType` / `tripType`：不互動。

刪除 `choose_station_suggestion()`、`_select_station()` 及 `tests/test_station_selection.py`：
自動完成選單已不存在，這些是死程式碼。

`TripType.ROUNDTRIP` 或 `OrderType.BY_TIME` 進到 automation 時拋出明確的
`NotImplementedError`，訊息說明只支援依車次單程。

## §2 車種篩選（純前端）

在建議清單上加車種篩選。選項**由 TDX 實際回傳的 `train_type_name` 動態產生**，
不寫死官方那份清單——避免列出當天根本沒有的車種。

純 client-side 過濾已取得的候選陣列，不改 model、不改 API、不新增請求。

## §3 必須選定車次才能建任務

「依車次／依時段」從**任務模式**降級為**搜尋方式**：

- 使用者以時段＋車種查詢候選，從清單挑一班。
- 未選定車次前，「加入任務佇列」保持 disabled，並說明原因。
- 送出的 `order_type` 恆為 `BY_TRAIN_NO`。

model 的 `BY_TIME` 保留（驗證邏輯與既有測試仍在使用），僅 UI 不再產生該類任務。

## §4 卡片資訊與刪除鍵

### 顯示

任務需記住使用者挑中那一班的展示資訊：`train_no`、`train_type_name`、
`departure_time`、`arrival_time`。建立任務時一併存入，`TaskResponse` 增加這些欄位。

任務卡片改為顯示：`自強(3000) 123 · 08:30 → 11:45 · 2026/09/25`。
VNC 畫面標題使用同一組資料，確保兩邊一致。

### 刪除

新增 `DELETE /tasks/{task_id}`，沿用 `travelers` / `profile` 既有模式
（`storage.delete_task` ＋ 204 回應）。

守則：該任務正在進行 booking session 時回 409 拒絕；其餘狀態一律可刪。
刪除為硬刪除，連同任務 payload 一併移除。

## §5 自動準備、暫停並通知一次

### 現況

先前只在排程到期時改狀態並通知，沒有開頁。現在由排程啟動既有 booking session，
與使用者手動開頁共用同一把鎖及同一條填表流程。

### 新行為

任務到期 → 取得瀏覽器鎖 → 暫停排程 → 開依車次頁並填表 → 通知人工接手。
若會員登入先需要人工操作，就在登入頁通知；後續訂票表單不再通知第二次。

| 結果 | 動作 |
| --- | --- |
| 需要人工登入、驗證或確認送出 | 保留頁面，session 只嘗試發送一次 `task.waiting_human` |
| 瀏覽器忙碌 | 等下一個 `poll_interval`，不通知、不開第二個頁面 |
| 填表失敗 | 停止、記錄錯誤並發送訂票結果事件 |
| 純提醒模式 | 到點通知一次並暫停，不開頁面 |

### 判讀不出來時必須停止

**未知回應一律不得預設為「沒位」。** 本版沒有餘票判讀器，不自動重送。
填表 selector 不符合就失敗；人工操作後沒有可識別結果則逾時停止。
通知傳既有任務連結；本人登入後開啟驗證畫面，API 接回原 session，不另開頁面。
Webhook 失敗只記錄日誌；排程重啟也不會重新認領已暫停的任務。

### 停止條件

- 取得訂位代碼
- 超過 session 的 15 分鐘期限或 `monitor_until`（取較早者）
- 使用者取消
- 填表失敗或等待人工結果逾時

成功、失敗、逾時、取消都不自動恢復；需要再試時由本人建立新任務。
訂票結束事件與人工接手通知各有一次機會，不將「只通知一次」誤解為省略結果。

### 併發限制

`BookingSessionManager` 規定同時只有一個瀏覽器 session——sidecar 只有一顆瀏覽器，
這是實體限制而非政策。巡檢必須競爭同一把鎖；兩個任務撞在同一輪時，
後者等下一個設定間隔，不並行開第二個瀏覽器。
取消／逾時先發出停止訊號；直到 worker 關閉 context 才釋放鎖。
每次 session 仍使用新 context，不保留跨任務會員登入。

## 測試策略

- **§1**：對照本文件記錄的官方 DOM 結構，以本地 HTML fixture 驗證填表選擇器。
  不對正式站送出任何請求。
- **§2**：純函式測試——給定候選清單與車種，回傳正確子集，且選項由資料產生。
- **§3**：表單在未選車次時不得送出。
- **§4**：`DELETE` 的擁有者隔離（不能刪別人的任務）、session 進行中回 409。
- **§5**：單次與週期任務自動準備、填妥前不通知、兩次交接只通知一次、
  重複 tick／排程重啟不重開、原 session 接回、取消與逾時釋鎖、準備失敗停止。
  以假的 automator 驅動，不對正式站送出訂票。

## 文件變更

README 與前端同步說明「自動準備、人工接手一次」，移除「有位才通知」與
「自動巡檢至訂到」的承諾。取消後恢復監控已移除。

## 待第一次實跑確認

1. 無餘票時官方回應的實際文字／DOM 結構。
2. 沒有圖形驗證時，送出是否可能直接成立訂位。
3. 送出後是否會離開 `tip121/query` 進到另一個網址（影響巡檢如何判斷結果）。

在上述行為確認之前，保留人工送出，不啟用無位自動續查。
