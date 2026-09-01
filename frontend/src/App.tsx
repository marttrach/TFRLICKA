import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, Station, Task, User } from "./api";

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
          <label>密碼<input type="password" minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
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
  quantity: number;
  scheduledAt: string;
}

const defaultForm: BookingFormState = {
  identity: "",
  startStation: "1000-臺北",
  endStation: "3300-臺中",
  rideDate: tomorrow(),
  trainNumber: "",
  quantity: 1,
  scheduledAt: fiveMinutesFromNow(),
};

function Dashboard({ token, onLogout }: { token: string; onLogout: () => void }) {
  const [user, setUser] = useState<User | null>(null);
  const [stations, setStations] = useState<Station[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [form, setForm] = useState(defaultForm);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await api.tasks(token));
    } catch (reason) {
      if (reason instanceof Error && /expired|signed in/i.test(reason.message)) onLogout();
    }
  }, [token, onLogout]);

  useEffect(() => {
    Promise.all([api.me(token), api.stations(), api.tasks(token)])
      .then(([profile, stationList, taskList]) => {
        setUser(profile);
        setStations(stationList);
        setTasks(taskList);
      })
      .catch(onLogout);
  }, [token, onLogout]);

  useEffect(() => {
    const timer = window.setInterval(loadTasks, 10_000);
    return () => window.clearInterval(timer);
  }, [loadTasks]);

  const waiting = useMemo(() => tasks.filter((task) => task.status === "waiting_human").length, [tasks]);
  const scheduled = useMemo(() => tasks.filter((task) => task.status === "scheduled").length, [tasks]);

  async function createTask(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.createTask(token, {
        scheduled_at: new Date(form.scheduledAt).toISOString(),
        booking: {
          identity: form.identity,
          identity_type: "PERSON_ID",
          start_station: form.startStation,
          end_station: form.endStation,
          trip_type: "ONEWAY",
          order_type: "BY_TRAIN_NO",
          quantity: form.quantity,
          seat_preference: "NONE",
          allow_seat_change: true,
          outbound: {
            ride_date: form.rideDate.replaceAll("-", "/"),
            train_numbers: [form.trainNumber],
          },
        },
      });
      setNotice("任務已排入；到點後會轉為等待人工驗證。");
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
            <div className="panel-heading"><div><span className="step">01</span><h2>建立訂票任務</h2></div><small>快速訂票 · 依車次</small></div>
            <form className="booking-form" onSubmit={createTask}>
              <label className="wide">身分證字號<input value={form.identity} onChange={(e) => setForm({ ...form, identity: e.target.value })} autoComplete="off" required /></label>
              <label>出發站<select value={form.startStation} onChange={(e) => setForm({ ...form, startStation: e.target.value })}>{stations.map((station) => <option value={station.value} key={station.value}>{station.value}</option>)}</select></label>
              <button type="button" className="swap" aria-label="交換出發站與抵達站" onClick={() => setForm({ ...form, startStation: form.endStation, endStation: form.startStation })}>⇄</button>
              <label>抵達站<select value={form.endStation} onChange={(e) => setForm({ ...form, endStation: e.target.value })}>{stations.map((station) => <option value={station.value} key={station.value}>{station.value}</option>)}</select></label>
              <label>乘車日期<input type="date" value={form.rideDate} onChange={(e) => setForm({ ...form, rideDate: e.target.value })} required /></label>
              <label>車次<input inputMode="numeric" pattern="[0-9]+" value={form.trainNumber} onChange={(e) => setForm({ ...form, trainNumber: e.target.value })} placeholder="例如 110" required /></label>
              <label>一般座票數<select value={form.quantity} onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })}>{[1, 2, 3, 4, 5, 6].map((value) => <option key={value}>{value}</option>)}</select></label>
              <label className="wide">排程觸發時間<input type="datetime-local" value={form.scheduledAt} onChange={(e) => setForm({ ...form, scheduledAt: e.target.value })} required /></label>
              {error && <p className="error wide" role="alert">{error}</p>}
              {notice && <p className="notice wide" role="status">{notice}</p>}
              <button className="primary wide" disabled={busy}>{busy ? "建立中…" : "加入任務佇列"}</button>
            </form>
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

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
  }, []);

  return token ? <Dashboard token={token} onLogout={logout} /> : <AuthScreen onAuthenticated={login} />;
}
