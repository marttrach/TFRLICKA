import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, Station, Suggestions, Task, TrainCandidate, User } from "./api";

const TOKEN_KEY = "tra-sniper-token";

function tomorrow(): string {
  const value = new Date();
  value.setDate(value.getDate() + 1);
  return value.toISOString().slice(0, 10);
}

function fiveMinutesFromNow(): string {
  const value = new Date(Date.now() + 5 * 60_000);
  value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
  return value.toISOString().slice(0, 16);
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-TW", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function AuthScreen({ onAuthenticated }: { onAuthenticated: (token: string) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api.authenticate(mode, email, password);
      onAuthenticated(result.access_token);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法完成登入");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-brand" aria-labelledby="brand-title">
        <span className="eyebrow">HUMAN-IN-THE-LOOP BOOKING</span>
        <h1 id="brand-title">TRA<span>/</span>Sniper</h1>
        <p>把台鐵訂票條件、排程與人工驗證整理在同一個本機工作台。</p>
        <div className="rail-line" aria-hidden="true"><i /><i /><i /><i /></div>
      </section>
      <section className="auth-card">
        <div className="auth-tabs" role="tablist" aria-label="會員操作">
          <button className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>登入</button>
          <button className={mode === "register" ? "active" : ""} onClick={() => setMode("register")}>建立帳號</button>
        </div>
        <form onSubmit={submit}>
          <label>電子郵件<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
          <label>密碼<input type="password" minLength={mode === "register" ? 12 : 1} value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primary" disabled={busy}>{busy ? "處理中…" : mode === "login" ? "進入工作台" : "建立並登入"}</button>
        </form>
        <p className="privacy-note">會員與任務資料只保存在本機 SQLite；訂票資料另以 Fernet 加密。</p>
      </section>
    </main>
  );
}

function OcrWorkflow({ token }: { token: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [language, setLanguage] = useState<"zh-TW" | "en">("zh-TW");
  const [preview, setPreview] = useState("");
  const [result, setResult] = useState("");
  const [details, setDetails] = useState("");
  const [copyNotice, setCopyNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!file) {
      setPreview("");
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setPreview(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file]);

  async function recognize(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError("");
    setResult("");
    setDetails("");
    setCopyNotice("");
    try {
      const response = await api.ocr(token, file, language);
      setResult(response.text);
      setDetails(`${response.width} × ${response.height} px · ${response.language}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法辨識圖片");
    } finally {
      setBusy(false);
    }
  }

  async function copyResult() {
    if (!result) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(result);
      } else {
        const helper = document.createElement("textarea");
        helper.value = result;
        helper.style.position = "fixed";
        helper.style.opacity = "0";
        document.body.appendChild(helper);
        helper.select();
        const copied = document.execCommand("copy");
        helper.remove();
        if (!copied) throw new Error("Copy command failed");
      }
      setCopyNotice("已複製到剪貼簿");
    } catch {
      setError("無法自動複製，請選取文字後手動複製");
    }
  }

  return (
    <section className="ocr-workflow">
      <div className="ocr-workflow-heading">
        <div><span>OCR</span><b>圖片辨識輸入框</b></div>
        <small>辨識、修正，再一鍵複製</small>
      </div>
      <form className="ocr-form" onSubmit={recognize}>
        <div className="ocr-controls">
          <label>選擇圖片
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              onChange={(event) => {
                const selected = event.target.files?.[0] ?? null;
                if (selected && selected.size > 8 * 1024 * 1024) {
                  setFile(null);
                  setError("圖片不可超過 8 MB");
                  event.currentTarget.value = "";
                  return;
                }
                if (selected && !["image/png", "image/jpeg", "image/webp"].includes(selected.type)) {
                  setFile(null);
                  setError("僅支援 PNG、JPEG 與 WebP 圖片");
                  event.currentTarget.value = "";
                  return;
                }
                setFile(selected);
                setResult("");
                setDetails("");
                setCopyNotice("");
                setError("");
              }}
              required
            />
          </label>
          <label>辨識語言
            <select value={language} onChange={(event) => setLanguage(event.target.value as "zh-TW" | "en")}>
              <option value="zh-TW">繁體中文＋英文</option>
              <option value="en">英文</option>
            </select>
          </label>
          <button className="primary" disabled={!file || busy}>{busy ? "辨識中…" : "開始辨識"}</button>
          <p className="ocr-hint">支援 PNG、JPEG、WebP，檔案上限 8 MB。</p>
          {error && <p className="error" role="alert">{error}</p>}
        </div>
        <div className="ocr-preview">
          {preview ? <img src={preview} alt="待辨識圖片預覽" /> : <div><b>圖片預覽</b><span>選擇圖片後會顯示在這裡</span></div>}
        </div>
        <div className="ocr-output">
          <div><b>辨識結果</b>{details && <span>{details}</span>}</div>
          <textarea value={result} onChange={(event) => { setResult(event.target.value); setCopyNotice(""); }} placeholder="辨識出的文字會顯示在這裡，也可直接修正" aria-label="OCR 辨識結果" />
          <div className="ocr-actions">
            <button type="button" className="copy-button" disabled={!result} onClick={copyResult}>複製文字</button>
            <button type="button" className="text-button" disabled={!result} onClick={() => { setResult(""); setDetails(""); setCopyNotice(""); }}>清除</button>
            {copyNotice && <span role="status">✓ {copyNotice}</span>}
          </div>
        </div>
      </form>
    </section>
  );
}

interface BookingFormState {
  identity: string;
  startStation: string;
  endStation: string;
  rideDate: string;
  trainNumber: string;
  orderType: "BY_TRAIN_NO" | "BY_TIME";
  startTime: string;
  endTime: string;
  preferReserved: boolean;
  includeTransfers: boolean;
  quantity: number;
  scheduledAt: string;
}

const defaultForm: BookingFormState = {
  identity: "",
  startStation: "1000-臺北",
  endStation: "3300-臺中",
  rideDate: tomorrow(),
  trainNumber: "",
  orderType: "BY_TRAIN_NO",
  startTime: "08:00",
  endTime: "12:00",
  preferReserved: true,
  includeTransfers: true,
  quantity: 1,
  scheduledAt: fiveMinutesFromNow(),
};

function CandidateRow({ item, onChoose }: { item: TrainCandidate; onChoose: (item: TrainCandidate) => void }) {
  return (
    <article className="candidate-row">
      <div><b>{item.train_type_name} {item.train_no}</b><span>{item.seat_type_label}（車種屬性）</span></div>
      <strong>{item.departure_time} → {item.arrival_time}</strong>
      <small>{item.duration_minutes} 分鐘</small>
      <button type="button" onClick={() => onChoose(item)}>改用此車次</button>
    </article>
  );
}

function Dashboard({ token, onLogout }: { token: string; onLogout: () => void }) {
  const [user, setUser] = useState<User | null>(null);
  const [stations, setStations] = useState<Station[]>([]);
  const [times, setTimes] = useState<string[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [form, setForm] = useState(defaultForm);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [suggestionBusy, setSuggestionBusy] = useState(false);
  const [suggestions, setSuggestions] = useState<Suggestions | null>(null);
  const [suggestionKey, setSuggestionKey] = useState("");
  const [waitingSuggestions, setWaitingSuggestions] = useState<Record<string, Suggestions>>({});

  const currentSuggestionKey = [form.startStation, form.endStation, form.rideDate, form.startTime, form.endTime, form.preferReserved, form.includeTransfers].join("|");

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await api.tasks(token));
    } catch (reason) {
      if (reason instanceof Error && /expired|signed in/i.test(reason.message)) onLogout();
    }
  }, [token, onLogout]);

  useEffect(() => {
    Promise.all([api.me(token), api.stations(), api.times(), api.tasks(token)])
      .then(([profile, stationList, timeList, taskList]) => {
        setUser(profile);
        setStations(stationList);
        setTimes(timeList);
        setTasks(taskList);
      })
      .catch(onLogout);
  }, [token, onLogout]);

  useEffect(() => {
    const timer = window.setInterval(loadTasks, 10_000);
    return () => window.clearInterval(timer);
  }, [loadTasks]);

  useEffect(() => {
    const waitingTasks = tasks.filter((task) => task.status === "waiting_human");
    if (!waitingTasks.length) return;
    void Promise.all(waitingTasks.map(async (task) => [task.id, await api.taskSuggestions(token, task.id)] as const))
      .then((entries) => setWaitingSuggestions(Object.fromEntries(entries)))
      .catch(() => undefined);
  }, [tasks, token]);

  const waiting = useMemo(() => tasks.filter((task) => task.status === "waiting_human").length, [tasks]);
  const scheduled = useMemo(() => tasks.filter((task) => task.status === "scheduled").length, [tasks]);

  async function querySuggestions(): Promise<Suggestions> {
    setSuggestionBusy(true);
    setError("");
    try {
      const result = await api.suggestions(token, {
        start_station: form.startStation,
        end_station: form.endStation,
        ride_date: form.rideDate,
        start_time: form.startTime,
        end_time: form.endTime,
        preferences: { prefer_reserved: form.preferReserved, include_transfers: form.includeTransfers },
      });
      setSuggestions(result);
      setSuggestionKey(currentSuggestionKey);
      return result;
    } finally {
      setSuggestionBusy(false);
    }
  }

  function chooseCandidate(item: TrainCandidate) {
    setForm((current) => ({ ...current, orderType: "BY_TRAIN_NO", trainNumber: item.train_no }));
    setNotice(`已選擇 ${item.train_type_name} ${item.train_no}；請確認後再建立任務。`);
  }

  async function createTask(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      let candidateSuggestions = suggestions;
      if (form.orderType === "BY_TIME" && suggestionKey !== currentSuggestionKey) {
        try {
          candidateSuggestions = await querySuggestions();
        } catch {
          candidateSuggestions = null;
        }
      }
      await api.createTask(token, {
        scheduled_at: new Date(form.scheduledAt).toISOString(),
        booking: {
          identity: form.identity,
          identity_type: "PERSON_ID",
          start_station: form.startStation,
          end_station: form.endStation,
          trip_type: "ONEWAY",
          order_type: form.orderType,
          quantity: form.quantity,
          seat_preference: "NONE",
          allow_seat_change: true,
          outbound: form.orderType === "BY_TRAIN_NO"
            ? { ride_date: form.rideDate.replaceAll("-", "/"), train_numbers: [form.trainNumber] }
            : { ride_date: form.rideDate.replaceAll("-", "/"), start_time: form.startTime, end_time: form.endTime },
          ...(form.orderType === "BY_TIME" && candidateSuggestions ? { candidate_suggestions: candidateSuggestions } : {}),
        },
      });
      setNotice(candidateSuggestions || form.orderType === "BY_TRAIN_NO"
        ? "任務已排入；到點後會轉為等待人工驗證。"
        : "任務已排入，但 TDX 暫時不可用，這次未附離線候選清單。");
      setForm((current) => ({ ...current, identity: "", trainNumber: "" }));
      await loadTasks();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法建立任務");
    } finally {
      setBusy(false);
    }
  }

  async function cancelTask(taskId: string) {
    await api.cancelTask(token, taskId);
    await loadTasks();
  }

  async function downloadConfig(taskId: string) {
    const config = await api.taskConfig(token, taskId);
    const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `tra-booking-${taskId.slice(0, 8)}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div><strong>TRA<span>/</span>Sniper</strong><small>本機訂票任務工作台</small></div>
        <div className="account"><span>{user?.email ?? "載入中…"}</span><button onClick={onLogout}>登出</button></div>
      </header>
      <main className="workspace">
        <section className="summary-row" aria-label="任務摘要">
          <article><span>排程中</span><strong>{scheduled}</strong><small>等待觸發時間</small></article>
          <article className={waiting ? "attention" : ""}><span>需要人工</span><strong>{waiting}</strong><small>驗證碼與最終確認</small></article>
          <article><span>全部任務</span><strong>{tasks.length}</strong><small>含取消與完成</small></article>
          <div className="boundary"><b>安全邊界</b><p>系統不解 CAPTCHA；到點後由你核對並送出。</p></div>
        </section>

        <div className="content-grid">
          <section className="panel booking-panel">
            <div className="panel-heading"><div><span className="step">01</span><h2>建立訂票任務</h2></div><small>依車次 · 依時段</small></div>
            <form className="booking-form" onSubmit={createTask}>
              <fieldset className="mode-switch wide">
                <legend>查詢方式</legend>
                <label><input type="radio" checked={form.orderType === "BY_TRAIN_NO"} onChange={() => setForm({ ...form, orderType: "BY_TRAIN_NO" })} /> 依車次</label>
                <label><input type="radio" checked={form.orderType === "BY_TIME"} onChange={() => setForm({ ...form, orderType: "BY_TIME" })} /> 依時段</label>
              </fieldset>
              <label className="wide">身分證字號<input value={form.identity} onChange={(e) => setForm({ ...form, identity: e.target.value })} autoComplete="off" required /></label>
              <label>出發站<select value={form.startStation} onChange={(e) => setForm({ ...form, startStation: e.target.value })}>{stations.map((station) => <option value={station.value} key={station.value}>{station.value}</option>)}</select></label>
              <button type="button" className="swap" aria-label="交換出發站與抵達站" onClick={() => setForm({ ...form, startStation: form.endStation, endStation: form.startStation })}>⇄</button>
              <label>抵達站<select value={form.endStation} onChange={(e) => setForm({ ...form, endStation: e.target.value })}>{stations.map((station) => <option value={station.value} key={station.value}>{station.value}</option>)}</select></label>
              <label>乘車日期<input type="date" value={form.rideDate} onChange={(e) => setForm({ ...form, rideDate: e.target.value })} required /></label>
              {form.orderType === "BY_TRAIN_NO" ? (
                <label>車次<input inputMode="numeric" pattern="[0-9]+" value={form.trainNumber} onChange={(e) => setForm({ ...form, trainNumber: e.target.value })} placeholder="例如 110" required /></label>
              ) : (
                <>
                  <label>開始時段<select value={form.startTime} onChange={(e) => setForm({ ...form, startTime: e.target.value })}>{times.map((value) => <option key={value}>{value}</option>)}</select></label>
                  <label>結束時段<select value={form.endTime} onChange={(e) => setForm({ ...form, endTime: e.target.value })}>{times.map((value) => <option key={value}>{value}</option>)}</select></label>
                  <div className="suggestion-options wide">
                    <label><input type="checkbox" checked={form.preferReserved} onChange={(e) => setForm({ ...form, preferReserved: e.target.checked })} /> 排序時優先對號列車</label>
                    <label><input type="checkbox" checked={form.includeTransfers} onChange={(e) => setForm({ ...form, includeTransfers: e.target.checked })} /> 包含單次轉乘建議</label>
                    <button type="button" onClick={() => void querySuggestions().catch((reason) => setError(reason instanceof Error ? reason.message : "無法取得建議"))} disabled={suggestionBusy}>{suggestionBusy ? "查詢中…" : "查詢車次建議"}</button>
                  </div>
                </>
              )}
              <label>一般座票數<select value={form.quantity} onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })}>{[1, 2, 3, 4, 5, 6].map((value) => <option key={value}>{value}</option>)}</select></label>
              <label className="wide">排程觸發時間<input type="datetime-local" value={form.scheduledAt} onChange={(e) => setForm({ ...form, scheduledAt: e.target.value })} required /></label>
              {error && <p className="error wide" role="alert">{error}</p>}
              {notice && <p className="notice wide" role="status">{notice}</p>}
              <button className="primary wide" disabled={busy}>{busy ? "建立中…" : "加入任務佇列"}</button>
            </form>
            {form.orderType === "BY_TIME" && suggestions && suggestionKey === currentSuggestionKey && (
              <section className="suggestions-panel" aria-label="車次建議">
                <div className="suggestion-warning"><b>時刻建議，不是餘位資訊</b><p>{suggestions.notice}</p></div>
                <h3>時段內對號列車</h3>
                {suggestions.primary.length ? suggestions.primary.map((item) => <CandidateRow key={`p-${item.train_no}-${item.departure_time}`} item={item} onChoose={chooseCandidate} />) : <p className="muted">這個時段沒有對號列車候選。</p>}
                <h3>其他選擇</h3>
                {suggestions.alternatives.slice(0, 8).map((item) => <CandidateRow key={`a-${item.train_no}-${item.departure_time}`} item={item} onChoose={chooseCandidate} />)}
                {suggestions.transfers.length > 0 && <h3>單次轉乘</h3>}
                {suggestions.transfers.map((item, index) => (
                  <article className="transfer-row" key={`${item.transfer_station.value}-${index}`}>
                    <b>於 {item.transfer_station.label} 轉乘 · 共 {item.duration_minutes} 分鐘</b>
                    <span>{item.first_leg.train_no} {item.first_leg.departure_time} → {item.first_leg.arrival_time}</span>
                    <span>緩衝 {item.buffer_minutes} 分鐘</span>
                    <span>{item.second_leg.train_no} {item.second_leg.departure_time} → {item.second_leg.arrival_time}</span>
                    <p>{item.notice}</p>
                  </article>
                ))}
              </section>
            )}
            <OcrWorkflow token={token} />
          </section>

          <section className="panel task-panel">
            <div className="panel-heading"><div><span className="step">02</span><h2>任務佇列</h2></div><button className="text-button" onClick={loadTasks}>重新整理</button></div>
            <div className="task-list">
              {tasks.length === 0 && <div className="empty"><b>尚無任務</b><p>建立第一個訂票條件後，狀態會顯示在這裡。</p></div>}
              {tasks.map((task) => (
                <article className="task-card" key={task.id}>
                  <div className="task-main"><span className={`status status-${task.status}`}>{task.status === "waiting_human" ? "需要人工" : task.status === "scheduled" ? "排程中" : task.status}</span><h3>{task.route}</h3><p>{task.ride_date} · {task.order_type === "BY_TRAIN_NO" ? "依車次" : "依時段"}</p></div>
                  <div className="task-time"><span>觸發</span><b>{formatDate(task.scheduled_at)}</b></div>
                  <div className="task-actions">
                    {task.status === "waiting_human" && <button className="primary compact" onClick={() => downloadConfig(task.id)}>下載設定</button>}
                    {["scheduled", "waiting_human"].includes(task.status) && <button className="danger" onClick={() => cancelTask(task.id)}>取消</button>}
                  </div>
                  {task.status === "waiting_human" && waitingSuggestions[task.id] && (
                    <div className="offline-candidates">
                      <b>離線候選清單</b>
                      {[...waitingSuggestions[task.id].primary, ...waitingSuggestions[task.id].alternatives].slice(0, 3).map((item) => <span key={`${item.train_no}-${item.departure_time}`}>{item.train_type_name} {item.train_no} · {item.departure_time}</span>)}
                      <small>僅為時刻與車種建議，不代表有座位。</small>
                    </div>
                  )}
                </article>
              ))}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? "");

  const login = useCallback((value: string) => {
    localStorage.setItem(TOKEN_KEY, value);
    setToken(value);
  }, []);

  const clearSession = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
  }, []);

  const logout = useCallback(() => {
    const currentToken = localStorage.getItem(TOKEN_KEY);
    if (!currentToken) {
      clearSession();
      return;
    }
    void api.logout(currentToken).finally(clearSession);
  }, [clearSession]);

  return token ? <Dashboard token={token} onLogout={logout} /> : <AuthScreen onAuthenticated={login} />;
}
