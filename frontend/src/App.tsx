import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, MemberProfile, Station, Suggestions, Task, TrainCandidate, User } from "./api";

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
        <img className="auth-logo" src="/favicon.svg" alt="" />
        <span className="eyebrow">台鐵訂票小幫手</span>
        <h1 id="brand-title">好搭車</h1>
        <p>把常用資料、車次選擇、排程提醒整理在同一個簡單好讀的畫面。</p>
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
  useSavedMemberLogin: boolean;
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
  useSavedMemberLogin: false,
};

function MemberProfilePanel({
  token,
  profile,
  onSaved,
}: {
  token: string;
  profile: MemberProfile;
  onSaved: (profile: MemberProfile) => void;
}) {
  const [identity, setIdentity] = useState(profile.identity);
  const [account, setAccount] = useState(profile.member_account);
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const updated = await api.saveProfile(token, {
        identity,
        member_account: account,
        member_password: password,
      });
      onSaved(updated);
      setPassword("");
      setNotice("常用資料已加密保存");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法儲存會員資料");
    } finally {
      setBusy(false);
    }
  }

  async function clearLogin() {
    setBusy(true);
    setError("");
    try {
      await api.clearMemberLogin(token);
      setAccount("");
      setPassword("");
      onSaved({ ...profile, member_account: "", has_member_password: false });
      setNotice("台鐵會員帳密已清除，身分證常用資料仍保留");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法清除會員帳密");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel profile-panel">
      <div className="panel-heading"><div><span className="step">01</span><h2>我的常用資料</h2></div><small>加密保存</small></div>
      <form className="profile-form" onSubmit={save}>
        <label>身分證字號<input value={identity} onChange={(event) => setIdentity(event.target.value.toUpperCase())} autoComplete="off" required /></label>
        <label>台鐵會員帳號<input value={account} onChange={(event) => setAccount(event.target.value)} autoComplete="username" placeholder="身分證號或會員編號" /></label>
        <label>台鐵會員密碼<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" placeholder={profile.has_member_password ? "已保存；不修改請留空" : "選填"} /></label>
        <p className="privacy-note">帳密只會以 Fernet 加密存放在你的 NAS。官方若要求驗證，仍需由本人或可信任家人完成。</p>
        {error && <p className="error" role="alert">{error}</p>}
        {notice && <p className="notice" role="status">{notice}</p>}
        <div className="profile-actions">
          <button className="primary" disabled={busy}>{busy ? "處理中…" : "儲存常用資料"}</button>
          {profile.has_member_password && <button type="button" className="danger" disabled={busy} onClick={clearLogin}>清除台鐵帳密</button>}
        </div>
      </form>
    </section>
  );
}

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
  const linkedTaskId = useMemo(() => {
    const match = window.location.pathname.match(/^\/tasks\/([^/]+)$/);
    return match ? decodeURIComponent(match[1]) : "";
  }, []);
  const [user, setUser] = useState<User | null>(null);
  const [profile, setProfile] = useState<MemberProfile | null>(null);
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
    Promise.all([api.me(token), api.profile(token), api.stations(), api.times(), api.tasks(token)])
      .then(([account, savedProfile, stationList, timeList, taskList]) => {
        setUser(account);
        setProfile(savedProfile);
        setForm((current) => ({
          ...current,
          identity: savedProfile.identity,
          useSavedMemberLogin: savedProfile.has_member_password,
        }));
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

  useEffect(() => {
    if (!linkedTaskId || !tasks.some((task) => task.id === linkedTaskId)) return;
    document.getElementById(`task-${linkedTaskId}`)?.scrollIntoView({ block: "center" });
  }, [linkedTaskId, tasks]);

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
        use_saved_member_login: form.useSavedMemberLogin,
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
      setForm((current) => ({ ...current, identity: profile?.identity ?? "", trainNumber: "" }));
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
        <div className="brand-lockup"><img src="/favicon.svg" alt="" /><strong>好搭車</strong><small>台鐵訂票小幫手</small></div>
        <div className="account"><span>{user?.email ?? "載入中…"}</span><button onClick={() => document.body.classList.toggle("large-text")}>大字模式</button><button onClick={onLogout}>登出</button></div>
      </header>
      <main className="workspace">
        <section className="welcome-card">
          <div><span>簡單三步驟</span><h1>選車次、排時間、到點完成驗證</h1><p>常用資料只要設定一次，之後每次訂票會自動帶入。</p></div>
          <a href="https://www.trc.com.tw/tra-tip-web/tip/tip008/tip811/memberLogin" target="_blank" rel="noreferrer">開啟台鐵會員登入</a>
        </section>
        <section className="summary-row" aria-label="任務摘要">
          <article><span>排程中</span><strong>{scheduled}</strong><small>等待觸發時間</small></article>
          <article className={waiting ? "attention" : ""}><span>需要人工</span><strong>{waiting}</strong><small>驗證碼與最終確認</small></article>
          <article><span>全部任務</span><strong>{tasks.length}</strong><small>含取消與完成</small></article>
          <div className="boundary"><b>驗證協助</b><p>若官方要求驗證，可放大畫面、重新產生，或請可信任家人協助；最後仍由你確認送出。</p></div>
        </section>

        <div className="content-grid">
          <div className="left-column">
          {profile && <MemberProfilePanel token={token} profile={profile} onSaved={(saved) => {
            setProfile(saved);
            setForm((current) => ({ ...current, identity: saved.identity, useSavedMemberLogin: saved.has_member_password }));
          }} />}
          <section className="panel booking-panel">
            <div className="panel-heading"><div><span className="step">02</span><h2>建立訂票任務</h2></div><small>依車次 · 依時段</small></div>
            <form className="booking-form" onSubmit={createTask}>
              <fieldset className="mode-switch wide">
                <legend>查詢方式</legend>
                <label><input type="radio" checked={form.orderType === "BY_TRAIN_NO"} onChange={() => setForm({ ...form, orderType: "BY_TRAIN_NO" })} /> 依車次</label>
                <label><input type="radio" checked={form.orderType === "BY_TIME"} onChange={() => setForm({ ...form, orderType: "BY_TIME" })} /> 依時段</label>
              </fieldset>
              <label className="wide">身分證字號<input value={form.identity} onChange={(e) => setForm({ ...form, identity: e.target.value })} autoComplete="off" required /></label>
              {profile?.has_member_password && (
                <label className="member-login-option wide"><input type="checkbox" checked={form.useSavedMemberLogin} onChange={(event) => setForm({ ...form, useSavedMemberLogin: event.target.checked })} /> 購票前先帶入已保存的台鐵會員帳密</label>
              )}
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
          </section>
          </div>

          <section className="panel task-panel">
            <div className="panel-heading"><div><span className="step">03</span><h2>任務佇列</h2></div><button className="text-button" onClick={loadTasks}>重新整理</button></div>
            <div className="task-list">
              {tasks.length === 0 && <div className="empty"><b>尚無任務</b><p>建立第一個訂票條件後，狀態會顯示在這裡。</p></div>}
              {tasks.map((task) => (
                <article id={`task-${task.id}`} className={`task-card${task.id === linkedTaskId ? " task-card-linked" : ""}`} key={task.id}>
                  <div className="task-main"><span className={`status status-${task.status}`}>{task.status === "waiting_human" ? "需要人工" : task.status === "scheduled" ? "排程中" : task.status}</span><h3>{task.route}</h3><p>{task.ride_date} · {task.order_type === "BY_TRAIN_NO" ? "依車次" : "依時段"}</p></div>
                  <div className="task-time"><span>觸發</span><b>{formatDate(task.scheduled_at)}</b></div>
                  <div className="task-actions">
                    {task.status === "waiting_human" && <button className="primary compact" title="檔案可能包含已保存的台鐵會員登入資料，使用後請妥善刪除" onClick={() => downloadConfig(task.id)}>下載訂票設定</button>}
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
