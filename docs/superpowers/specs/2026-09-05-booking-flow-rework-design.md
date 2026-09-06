# 訂票流程重整設計

日期：2026-09-05
狀態：§1～§5 已實作。§5 為每輪自動備頁＋人工接手的重試迴圈（2026-09-05）

## 背景

使用者建立了一個「1180-竹北 → 2200-大甲、2026/09/25、依時段」的任務，結果是訂票失敗，
而且從任務卡片上看不出來這個任務到底在訂什麼車。追查後發現三層互相獨立的問題：

1. CDP 連不上（已於 `db6fc30` 修復，不在本設計範圍）。
2. 官方訂票頁改版，`_prepare_form` 幾乎每一個 selector 都已失效。
3. 產品流程本身：任務可以在「還沒決定要搭哪一班車」的狀態下建立，
   卡片不顯示車次，沒有刪除鍵，訂票失敗後任務就死掉不再重試。

本設計處理 2 與 3。

## 已驗證的事實

以下全部來自對 `https://www.trc.com.tw/tra-tip-web/tip/tip001/tip121/query`
的實際 DOM 探測（只讀取，未送出任何請求）。

> **2026-09-06 更正。** 2026-09-05 那份探測是錯的：它記下的 `select#startStation0`
> 這一整組 0 結尾 `<select>` 在頁面上**一個都不存在**，照著改的 `_prepare_form`
> 每一輪都以 `Locator.select_option: Timeout ... waiting for locator("#startStation0")`
> 收場。諷刺的是那張表的「現行程式碼」欄本來才是對的——這次 rework 是拿一份
> 假的探測去砍掉一份能動的程式。下表已依實際 HTML、jQuery validator 規則與
> `tip_station_autocomplete.js` 原始碼重寫。

### 分頁與網址

「依車次／依時段」「單程／來回」**都是同一頁 `/tip001/tip121/query` 上的 radio**，
不是四個網址。今天的預設值剛好就是依車次＋單程，所以不設定也可能會過——
正因如此才必須明確設定，否則官方哪天改了預設值會靜默填錯表。

### 表單控制項對照（實際 DOM）

| 欄位 | selector | 型別與寫法 |
| --- | --- | --- |
| 身分類別 | `input[name='custIdTypeEnum'][value='PERSON_ID']` | radio，`.check()` |
| 身分證號 | `#pid` | text，`.fill()` |
| 單程 | `input[name='tripType'][value='ONEWAY']` | radio，`.check()` |
| 依車次 | `input[name='orderType'][value='BY_TRAIN_NO']` | radio，`.check()` |
| 出發／抵達站 | `#startStation` / `#endStation` | text + jQuery-UI autocomplete |
| 一般座票數 | `#normalQty` | text（旁邊是 -/+ 按鈕），`.fill()` |
| 搭乘日期 | `input[name='ticketOrderParamList[0].rideDate']`（`#rideDate1`） | text datepicker，`YYYY/MM/DD` |
| 車次 | `input[name='ticketOrderParamList[0].trainNoList[0..2]']` | text，`.fill()` |
| 座位偏好 | `input[name='…seatPref'][value='NONE'\|'TABLE']` | radio，`.check()` |
| 可接受其他座位 | `input[name='…chgSeat']`（`#chgSeat1`） | checkbox，`.set_checked()` |
| 驗證碼 | `#verifyCode`（name `g-recaptcha-response`） | **不碰** |
| 送出 | `input[type=submit][value='訂票']` | **不碰，由本人按** |

站別欄位不是下拉選單。`tip_station_autocomplete.js` 的 `searchAutoArray()`
在欄位失焦時，把使用者打的字換成 `availableTags` 裡對應的整串標籤
（例如 `1180-竹北`），**比對不到就把欄位清空**。所以填入我們存的整串標籤再
blur，然後回讀 `input_value()`：空的就代表官方站表變了，要當錯誤丟出來，
不能讓它帶著空站別去訂一張不知道去哪的票。

日期沒有 option 清單可讀，只有 jQuery validator 的
`{ required: true, dateISO: true, checkDateTime: … }`。可訂範圍由官方判斷，
本地不再自行驗證。

車次欄位：name 是 `ticketOrderParamList[0].trainNoList[0..2]`，
id 卻是 `trainNoList1..3`——0-based 與 1-based 混用，一律以 name 定位。

站別對應：`api.py` 的 `POPULAR_STATIONS` 存 `"1180-竹北"`，
與官方 `availableTags` 同一種格式，整串直接填入即可，不需拆出站碼。

### 車種

**2026-09-06 更正：車種與車次在同一頁，不是二選一。**

```
input[type=checkbox] name='ticketOrderParamList[0].trainTypeList'
  11=自強(3000)  1=太魯閣  2=普悠瑪  3=自強  4=莒光  5=復興
```

是 checkbox 群組，與車次欄位並存。既然任務一律指定確切車次，車次號碼本身
就決定了車種，勾不勾在功能上沒有差別，因此**現行實作仍不勾選**；
車種依舊只當作建議清單的篩選器。這裡記下來是因為原本「官方只讓二選一」
的理由是錯的，將來若要做依時段訂票，這個欄位是可用的。

### 驗證機制

查詢表單上有 `g-recaptcha-response`、`action-token`、`action-name=submit_form`、
`isSecondVerify=true`。後續探測看到「因 v3 驗證未通過，請輸入驗證碼」、
`#codeimg` 與 `#verifyCode`，表示驗證可能在送出查詢前要求人工完成。
原先「有位才出驗證碼」的前提撤回；目前也無法由這段提示判定失敗原因、
sidecar 每次是否遇到驗證，或持久 profile 是否有效。

## 決策

| 議題 | 決定 | 理由 |
| --- | --- | --- |
| 車種語意 | 只作為 tra-sniper 建議清單的篩選器 | 指定了確切車次，車種即已決定（原記的「官方二選一」有誤，見上）|
| 任務建立 | 必須先選定車次才能加入佇列 | 同上；VNC 打開時看到的必須是使用者自己挑過的車 |
| 人工交接 | 每輪自動備頁並通知一次，人工完成驗證與送出 | 官方在 v3 分數不足時要求圖形驗證碼，自動送出等同代解 |
| 沒訂成 | 排入下一輪，直到訂到／取消／超過截止時間 | 使用者明確要求；自動送出的部分不做 |
| 驗證碼 | 一律停在人工那一步，不辨識、不繞過 | 專案既有立場；本設計不改變 |

### 「自動送出」的界線

不自動按送出。探測確認官方在 reCAPTCHA v3 分數不足時，會在查詢表單上直接顯示
圖形驗證碼（`#codeimg`、`#verifyCode`，並標示「因 v3 驗證未通過，請輸入驗證碼」），
而自動化瀏覽器的分數一律不足。因此每一輪都會遇到驗證碼，自動送出等同代解，不做。
餘票也只有送出後才知道，所以「無位自動續查」無法在不代解的前提下實作。

## 非目標

- 不辨識、不代解、不繞過任何驗證。
- 不實作雙行程（ROUNDTRIP）。現行 UI 寫死 ONEWAY（`App.tsx:588`），
  model 保留 ROUNDTRIP 定義與驗證，但 automation 遇到即明確拒絕。
- 不碰餘票預測。系統仍然無法在送出前得知任何車次是否有位。

---

## §1 填表層重寫（`automation.py`）

任務一律走依車次單程，`BOOKING_URL` 固定為 `/tip001/tip121/query`。

`_prepare_form` 與 `_fill_leg` 依上表填寫：

- 站別：`fill("1180-竹北")` → `blur()` → 回讀，空值即報錯。
- 日期：`fill("YYYY/MM/DD")`，範圍交給官方判斷。
- 車次：三個欄位一律用 `ticketOrderParamList[0].trainNoList[n]` 這個 name 定位。
- 張數：`fill()`；座位偏好：radio `.check()`；換座：checkbox `.set_checked()`。
- `orderType` / `tripType`：明確 `.check()`，不依賴預設值。

`station_code()` 隨之刪除：站碼不再單獨使用，整串標籤才是官方要的值。

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

守則：**一律可刪**。任務上還開著的 session 會先被停止，而不是擋下刪除
（2026-09-06 更正：原本的 409 讓「worker 卡住／VNC 沒斷線」的任務永遠刪不掉，
而且畫面上沒有任何東西可以關來解除）。刪除為硬刪除，連同任務 payload 一併移除。

## §5 每輪自動備頁，人工接手

### 現況

先前只在排程到期時改狀態並通知，沒有開頁。現在由排程啟動既有 booking session，
與使用者手動開頁共用同一把鎖及同一條填表流程。

### 新行為

任務到期 → 取得瀏覽器鎖 → 暫停排程 → 開依車次頁並填表 → 通知人工接手。
訂票不再先登入台鐵會員，直接用乘車人資料填表。舊任務的 `member_login` 也忽略，
避免會員登入與訂票各要求一次驗證；官方仍可能要求再次驗證，不能保證只需一次。
這一輪沒訂成，就在 `poll_interval_seconds` 後排入下一輪，直到訂到或視窗關閉。

| 結果 | 動作 |
| --- | --- |
| 需要人工驗證或確認送出 | 保留頁面，session 只嘗試發送一次 `task.waiting_human` |
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
- 超過 `monitor_until`（未設定則不限，靠訂到或取消停止）
- 使用者取消
- 準備階段失敗（未交接）

單輪的等待上限仍是 session 的 15 分鐘，逾時只結束該輪，不結束任務。

已交接過的失敗與逾時會恢復監控；成功與使用者取消終止。
**未交接就失敗不恢復**（`BookingSession.handed_off`）：連表都填不了代表官方版面變了，
重試只會對著壞掉的頁面空轉，這是 §5 的安全閥。
訂票結束事件與人工接手通知各有一次機會，不將「只通知一次」誤解為省略結果。

### 併發限制

`BookingSessionManager` 規定同時只有一個瀏覽器 session——sidecar 只有一顆瀏覽器，
這是實體限制而非政策。巡檢必須競爭同一把鎖；兩個任務撞在同一輪時，
後者等下一個設定間隔，不並行開第二個瀏覽器。
取消／逾時先發出停止訊號並記錄時間；直到 worker 關閉 context 且所有 VNC 連線關閉才釋放鎖。
VNC WebSocket 經 API 持續檢查 session，nginx 只將靜態資源轉給 sidecar，拒絕其他路徑的 Upgrade。
停止 60 秒後仍未清理就嘗試重啟專用 sidecar 瀏覽器；確認新瀏覽器就緒才結束卡住的工作。
無法確認恢復時保留鎖，不能只放行下一個任務。背景工作遲到的失敗不得覆寫取消或完成。
前端以 `X-Booking-Session` 查詢本輪結果；本輪已結束則移除舊 iframe，不跟隨下一輪的 session。
每次 session 重用 sidecar 既有視窗並清 cookie，不保留跨任務資料：另開 context 會多一個
Xvfb 上沒有 WM 能給焦點的視窗，人在 VNC 看得到卻打不進去。sidecar 因此改跑 matchbox WM。
「關閉畫面」只結束本輪並排入下一輪；只有任務卡的取消才是本人喊停。

## 測試策略

- **§1**：對照本文件記錄的官方 DOM 結構，以本地 HTML fixture 驗證填表選擇器。
  不對正式站送出任何請求。
- **§2**：純函式測試——給定候選清單與車種，回傳正確子集，且選項由資料產生。
- **§3**：表單在未選車次時不得送出。
- **§4**：`DELETE` 的擁有者隔離（不能刪別人的任務）、session 進行中會被停止且刪除仍成功。
- **§5**：單次與週期任務自動準備、填妥前不通知、兩次交接只通知一次、
  重複 tick／排程重啟不重開、原 session 接回、取消與逾時釋鎖、準備失敗停止。
  另加重試迴圈：逾時與失敗排入下一輪、訂位代碼與取消終止、視窗關閉後不恢復、
  未交接的失敗不重試，以及 `reap()` 在寬限期後強制回收卡住的瀏覽器名額。
  以假的 automator 驅動，不對正式站送出訂票。

## 文件變更

README 與前端同步說明「自動準備、人工接手一次」，移除「有位才通知」與
「自動巡檢至訂到」的承諾。取消後恢復監控已移除。

## 待第一次實跑確認

1. 無餘票時官方回應的實際文字／DOM 結構。
2. 圖形驗證碼是否真的每一輪都出現，或 v3 分數偶爾會過。
3. 送出後是否會離開 `tip121/query` 進到另一個網址。

在上述行為確認之前，保留人工送出，不啟用無位自動續查。

已知取捨：逾時重試有極小的重複訂位風險——若本人已按下訂票但官方結果未被辨識，
下一輪仍會備頁。接手前先確認官方訂位紀錄。
