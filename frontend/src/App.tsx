import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, MemberProfile, Station, Suggestions, Task, TrainCandidate, Traveler, User } from "./api";

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
  travelerId: number | null;
  startCounty: string;
  endCounty: string;
  startStation: string;
  endStation: string;
  mode: "monitor_only" | "book_when_available";
  startNow: boolean;
  pollIntervalMinutes: number;
  monitorUntil: string;
  rideDate: string;
  trainNumber: string;
  // How you find the train, not how the task books it: a task always books a
  // specific train number, because that is the only way the booking screen can
  // open on the train you picked.
  searchMode: "DIRECT" | "BY_TIME";
  // Set when the number came from the suggestion list, so the card can name the
  // train instead of just its number. Cleared when the number is typed by hand.
  chosenTrain: TrainCandidate | null;
  trainType: string;
  startTime: string;
  endTime: string;
  preferReserved: boolean;
  includeTransfers: boolean;
  quantity: number;
  scheduledAt: string;
}

const defaultForm: BookingFormState = {
  travelerId: null,
  startCounty: "臺北市",
  endCounty: "臺中市",
  startStation: "1000-臺北",
  endStation: "3300-臺中",
  mode: "book_when_available",
  startNow: true,
  pollIntervalMinutes: 5,
  monitorUntil: "",
  rideDate: tomorrow(),
  trainNumber: "",
  searchMode: "BY_TIME",
  chosenTrain: null,
  trainType: "ALL",
  startTime: "08:00",
  endTime: "12:00",
  preferReserved: true,
  includeTransfers: true,
  quantity: 1,
  scheduledAt: fiveMinutesFromNow(),
};

function TravelerPanel({
  token,
  travelers,
  onChanged,
}: {
  token: string;
  travelers: Traveler[];
  onChanged: () => void;
}) {
  const [editingId, setEditingId] = useState<number | null>(null);
  const [label, setLabel] = useState("");
  const [identity, setIdentity] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function reset() {
    setEditingId(null);
    setLabel("");
    setIdentity("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (editingId === null) {
        await api.createTraveler(token, { label, identity });
      } else {
        await api.updateTraveler(token, editingId, { label, identity });
      }
      reset();
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法儲存常用資料");
    } finally {
      setBusy(false);
    }
  }

  async function remove(traveler: Traveler) {
    if (!window.confirm(`確定刪除「${traveler.label}」？`)) return;
    setBusy(true);
    setError("");
    try {
      await api.deleteTraveler(token, traveler.id);
      if (editingId === traveler.id) reset();
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法刪除常用資料");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel profile-panel">
      <div className="panel-heading"><div><span className="step">01</span><h2>常用乘車人</h2></div><small>加密保存</small></div>
      {travelers.length > 0 && (
        <ul className="traveler-list">
          {travelers.map((traveler) => (
            <li key={traveler.id}>
              <div><strong>{traveler.label}</strong><small>{maskIdentity(traveler.identity)}</small></div>
              <div className="traveler-actions">
                <button type="button" className="compact" disabled={busy} onClick={() => {
                  setEditingId(traveler.id);
                  setLabel(traveler.label);
                  setIdentity(traveler.identity);
                }}>編輯</button>
                <button type="button" className="danger compact" disabled={busy} onClick={() => remove(traveler)}>刪除</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <form className="profile-form" onSubmit={submit}>
        <label>名稱<input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="例如：我自己、老婆、媽" maxLength={32} required /></label>
        <label>身分證字號<input value={identity} onChange={(event) => setIdentity(event.target.value.toUpperCase())} autoComplete="off" maxLength={32} required /></label>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="profile-actions">
          <button className="primary" disabled={busy}>{busy ? "處理中…" : editingId === null ? "新增乘車人" : "儲存修改"}</button>
          {editingId !== null && <button type="button" disabled={busy} onClick={reset}>取消編輯</button>}
        </div>
      </form>
    </section>
  );
}

// Status is never conveyed by colour alone: each one carries its own words.
const STATUS_TEXT: Record<string, string> = {
  scheduled: "尚未啟動",
  monitoring: "監控中",
  waiting_human: "待你完成驗證",
  completed: "已訂位",
  cancelled: "已取消",
  failed: "訂票失敗",
  timeout: "逾時未完成",
  expired: "監控已截止",
  ended: "本輪已結束",
};

// The four statuses browser_session.FINISHED_STATUSES can end a session with.
const FINISHED_STATUSES = ["completed", "failed", "timeout", "cancelled", "expired", "ended"];

interface BookingSession {
  taskId: string;
  route: string;
  trainLabel: string | null;
  sessionToken: string;
  status: string;
  bookingCode: string | null;
  message: string;
}

// noVNC 1.0 (the jammy package the sidecar installs) ships no index.html, so the
// client file has to be named. It also builds its WebSocket URL from the site
// root rather than from the page, so the socket path has to be spelled out or it
// would ask for /websockify and miss the token-prefixed nginx location.
// resize=scale is what makes it usable on a phone: the remote screen is
// 1280x1024 and would otherwise have to be panned around.
function viewerUrl(sessionToken: string): string {
  const params = new URLSearchParams({
    autoconnect: "1",
    reconnect: "1",
    resize: "scale",
    path: `booking-session/${sessionToken}/websockify`,
  });
  return `/booking-session/${sessionToken}/vnc.html?${params}`;
}

// Exactly one obvious button per state, so nobody has to guess.
const PRIMARY_ACTION: Record<string, { label: string; kind: "session" | "result" | "cancel" }> = {
  scheduled: { label: "立即開啟訂票頁", kind: "session" },
  monitoring: { label: "立即開啟訂票頁", kind: "session" },
  waiting_human: { label: "開啟驗證畫面", kind: "session" },
  completed: { label: "查看訂位結果", kind: "result" },
  failed: { label: "查看訂位結果", kind: "result" },
  timeout: { label: "查看訂位結果", kind: "result" },
};

function nextCheckText(task: Task): string {
  if (task.status === "waiting_human") return "等待你接手，已暫停";
  if (!task.next_check_at) return "不再查詢";
  if (["completed", "cancelled", "expired", "failed", "timeout"].includes(task.status)) {
    return "不再查詢";
  }
  return formatDate(task.next_check_at);
}

const OTHER_COUNTY = "其他";

function countyOf(station: Station): string {
  return station.county || OTHER_COUNTY;
}

// With no county data every station lands in one "其他" group, and the picker
// falls back to the flat list. The toggle then switches between two identical
// lists, so hide it rather than offer a control that does nothing.
function countyGroupingAvailable(stations: Station[]): boolean {
  return stations.some((item) => item.county);
}

function StationPicker({
  legend,
  stations,
  county,
  station,
  searchAll,
  onChange,
}: {
  legend: string;
  stations: Station[];
  county: string;
  station: string;
  searchAll: boolean;
  onChange: (next: { county: string; station: string }) => void;
}) {
  // Counties come from the station data, so a county with no TRA station can
  // never appear as an empty choice.
  const counties = useMemo(() => {
    const seen = new Map<string, number>();
    stations.forEach((item) => seen.set(countyOf(item), (seen.get(countyOf(item)) ?? 0) + 1));
    return [...seen.keys()].sort((a, b) => a.localeCompare(b, "zh-Hant"));
  }, [stations]);

  const inCounty = useMemo(
    () => stations.filter((item) => countyOf(item) === county),
    [stations, county],
  );

  if (searchAll || !countyGroupingAvailable(stations)) {
    return (
      <fieldset className="station-picker">
        <legend>{legend}</legend>
        <label className="wide">車站
          <select
            value={station}
            onChange={(event) => {
              const picked = stations.find((item) => item.value === event.target.value);
              onChange({ county: picked ? countyOf(picked) : county, station: event.target.value });
            }}
            required
          >
            <option value="">請選擇車站</option>
            {stations.map((item) => (
              <option value={item.value} key={item.value}>{item.label}（{countyOf(item)}）</option>
            ))}
          </select>
        </label>
      </fieldset>
    );
  }

  return (
    <fieldset className="station-picker">
      <legend>{legend}</legend>
      <label>縣市
        <select
          value={county}
          onChange={(event) => {
            const nextCounty = event.target.value;
            // Never keep a station that does not belong to the new county:
            // a silently wrong station would be booked without the user seeing.
            const stillValid = stations.some(
              (item) => item.value === station && countyOf(item) === nextCounty,
            );
            onChange({ county: nextCounty, station: stillValid ? station : "" });
          }}
        >
          {counties.map((name) => <option value={name} key={name}>{name}</option>)}
        </select>
      </label>
      <label>車站
        <select
          value={station}
          onChange={(event) => onChange({ county, station: event.target.value })}
          required
        >
          <option value="">請選擇車站</option>
          {inCounty.map((item) => (
            <option value={item.value} key={item.value}>{item.label}</option>
          ))}
        </select>
      </label>
    </fieldset>
  );
}

function maskIdentity(value: string): string {
  return value.length <= 3 ? "***" : `${value.slice(0, 1)}****${value.slice(-3)}`;
}

function MemberProfilePanel({
  token,
  profile,
  onSaved,
}: {
  token: string;
  profile: MemberProfile;
  onSaved: (profile: MemberProfile) => void;
}) {
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
      setNotice("台鐵會員帳密已清除，常用乘車人不受影響");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法清除會員帳密");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="panel profile-panel">
      <summary className="panel-heading">台鐵會員資料（訂票不使用）</summary>
      <form className="profile-form" onSubmit={save}>
        <label>台鐵會員帳號<input value={account} onChange={(event) => setAccount(event.target.value)} autoComplete="username" placeholder="身分證號或會員編號" /></label>
        <label>台鐵會員密碼<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" placeholder={profile.has_member_password ? "已保存；不修改請留空" : "選填"} /></label>
        <p className="privacy-note">已保存的帳密仍加密存放在 NAS，但訂票不會使用。系統直接填寫訂票表單；後續付款等操作請至台鐵官網自行辦理。</p>
        {error && <p className="error" role="alert">{error}</p>}
        {notice && <p className="notice" role="status">{notice}</p>}
        <div className="profile-actions">
          <button className="primary" disabled={busy}>{busy ? "處理中…" : "儲存常用資料"}</button>
          {profile.has_member_password && <button type="button" className="danger" disabled={busy} onClick={clearLogin}>清除台鐵帳密</button>}
        </div>
      </form>
    </details>
  );
}

function trainLabel(item: TrainCandidate): string {
  return `${item.train_type_name} ${item.train_no} · ${item.departure_time} → ${item.arrival_time}`;
}

function CandidateRow({ item, onChoose }: { item: TrainCandidate; onChoose: (item: TrainCandidate) => void }) {
  return (
    <article className="candidate-row">
      <div><b>{item.train_type_name} {item.train_no}</b><span>{item.seat_type_label}（車種屬性）</span></div>
      <strong>{item.departure_time} → {item.arrival_time}</strong>
      <small>{item.duration_minutes} 分鐘</small>
      <button type="button" className="primary" onClick={() => onChoose(item)}>② 改用此車次</button>
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
  const [travelers, setTravelers] = useState<Traveler[]>([]);
  const [searchAllStations, setSearchAllStations] = useState(false);
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
  const [booking, setBooking] = useState<BookingSession | null>(null);

  // Train types come from what TDX actually returned, never a hard-coded list:
  // offering a type with no trains that day is worse than not offering it.
  const trainTypes = useMemo(() => {
    if (!suggestions) return [] as string[];
    const names = [...suggestions.primary, ...suggestions.alternatives].map((i) => i.train_type_name);
    return [...new Set(names)].sort();
  }, [suggestions]);
  const filterByType = (items: TrainCandidate[]) =>
    form.trainType === "ALL" ? items : items.filter((i) => i.train_type_name === form.trainType);

  const currentSuggestionKey = [form.startStation, form.endStation, form.rideDate, form.startTime, form.endTime, form.preferReserved, form.includeTransfers].join("|");

  useEffect(() => {
    // Only invalidate a timetable selection, not a manually entered number.
    setNotice("");
    setForm((current) => current.chosenTrain
      ? { ...current, trainNumber: "", chosenTrain: null } : current);
  }, [currentSuggestionKey]);

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await api.tasks(token));
    } catch (reason) {
      if (reason instanceof Error && /expired|signed in/i.test(reason.message)) onLogout();
    }
  }, [token, onLogout]);

  useEffect(() => {
    Promise.all([api.me(token), api.profile(token), api.stations(), api.times(), api.tasks(token), api.travelers(token)])
      .then(([account, savedProfile, stationList, timeList, taskList, travelerList]) => {
        setUser(account);
        setProfile(savedProfile);
        setTravelers(travelerList);
        setForm((current) => ({
          ...current,
          travelerId: travelerList[0]?.id ?? null,
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

  // While the person solves the CAPTCHA in the frame, poll for the outcome so
  // the booking code appears without them hunting for a button.
  useEffect(() => {
    if (!booking || FINISHED_STATUSES.includes(booking.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const result = await api.bookingResult(token, booking.taskId, booking.sessionToken);
        setBooking((current) => current && current.taskId === result.task_id
          ? { ...current, status: result.status, bookingCode: result.booking_code, message: result.message }
          : current);
        if (FINISHED_STATUSES.includes(result.status)) await loadTasks();
      } catch {
        // A dropped poll is not worth interrupting the person mid-verification.
      }
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [booking, token, loadTasks]);

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
    setForm((current) => ({ ...current, trainNumber: item.train_no, chosenTrain: item }));
    setNotice(`已鎖定 ${trainLabel(item)}；確認後即可加入任務佇列。`);
  }

  async function createTask(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (form.chosenTrain && suggestionKey !== currentSuggestionKey) {
        throw new Error("查詢條件已變更，請重新查詢並選擇車次。");
      }
      await api.createTask(token, {
        // "Start now" sends no time at all: stamping this clock and having the
        // server compare it to its own only ever measured the gap between them.
        scheduled_at: form.startNow ? null : new Date(form.scheduledAt).toISOString(),
        traveler_id: form.travelerId,
        mode: form.mode,
        train_label: form.chosenTrain ? trainLabel(form.chosenTrain) : `車次 ${form.trainNumber}`,
        poll_interval_seconds: form.pollIntervalMinutes * 60,
        monitor_until: form.monitorUntil ? new Date(form.monitorUntil).toISOString() : null,
        booking: {
          identity_type: "PERSON_ID",
          start_station: form.startStation,
          end_station: form.endStation,
          trip_type: "ONEWAY",
          // Always by train number. 依時段 is how you search, not how you book.
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
      setNotice("任務已排入；每個間隔會自動備好訂票頁並通知你接手，直到訂到或超過截止時間。通知不代表有位。");
      setForm((current) => ({ ...current, trainNumber: "", chosenTrain: null }));
      await loadTasks();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法建立任務");
    } finally {
      setBusy(false);
    }
  }

  async function runPrimaryAction(task: Task) {
    const action = PRIMARY_ACTION[task.status];
    if (!action) return;
    setError("");
    try {
      if (action.kind === "session") {
        const session = await api.startBookingSession(token, task.id);
        setNotice(session.notice);
        setBooking({
          taskId: task.id,
          route: task.route,
          trainLabel: task.train_label,
          // session_url is "/booking-session/<token>/"; both the noVNC URL and
          // the DELETE that releases the lock are built from the token.
          sessionToken: session.session_url.split("/")[2] ?? "",
          status: "waiting_verification",
          bookingCode: null,
          message: "",
        });
      } else {
        const result = await api.bookingResult(token, task.id);
        setNotice(result.booking_code
          ? `訂位代碼 ${result.booking_code}`
          : `狀態：${STATUS_TEXT[result.status] ?? result.status}${result.message ? `／${result.message}` : ""}`);
      }
      await loadTasks();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法完成操作");
    }
  }

  async function cancelTask(taskId: string) {
    await api.cancelTask(token, taskId);
    await loadTasks();
  }

  async function deleteTask(taskId: string) {
    if (!window.confirm("刪除這個任務？監控會停止，訂票設定也會一併移除。")) return;
    try {
      await api.deleteTask(token, taskId);
      await loadTasks();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "無法刪除任務");
    }
  }

  async function closeBooking(session: BookingSession) {
    // Closing before the official result is in means abandoning the attempt, so
    // release the browser instead of leaving it holding the single session lock.
    if (!FINISHED_STATUSES.includes(session.status)) {
      await api.cancelBookingSession(token, session.sessionToken).catch(() => undefined);
    }
    setBooking(null);
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
        </section>
        <section className="summary-row" aria-label="任務摘要">
          <article><span>排程中</span><strong>{scheduled}</strong><small>等待觸發時間</small></article>
          <article className={waiting ? "attention" : ""}><span>需要人工</span><strong>{waiting}</strong><small>驗證碼與最終確認</small></article>
          <article><span>全部任務</span><strong>{tasks.length}</strong><small>含取消與完成</small></article>
          <div className="boundary"><b>驗證協助</b><p>若官方要求驗證，可放大畫面、重新產生，或請可信任家人協助；最後仍由你確認送出。</p></div>
        </section>

        <div className="content-grid">
          <div className="left-column">
          <TravelerPanel token={token} travelers={travelers} onChanged={async () => {
            const refreshed = await api.travelers(token);
            setTravelers(refreshed);
            setForm((current) => ({
              ...current,
              travelerId: refreshed.some((t) => t.id === current.travelerId)
                ? current.travelerId
                : refreshed[0]?.id ?? null,
            }));
          }} />
          {profile && <MemberProfilePanel token={token} profile={profile} onSaved={setProfile} />}
          <section className="panel booking-panel">
            <div className="panel-heading"><div><span className="step">03</span><h2>建立訂票任務</h2></div><small>依車次 · 依時段</small></div>
            <form className="booking-form" onSubmit={createTask}>
              <fieldset className="mode-switch wide">
                <legend>怎麼找車次</legend>
                <label><input type="radio" checked={form.searchMode === "BY_TIME"} onChange={() => setForm({ ...form, searchMode: "BY_TIME" })} /> 用時段與車種找</label>
                <label><input type="radio" checked={form.searchMode === "DIRECT"} onChange={() => setForm({ ...form, searchMode: "DIRECT" })} /> 直接輸入車次</label>
              </fieldset>
              <label className="wide">乘車人
                {travelers.length === 0
                  ? <span className="empty-hint">請先在上方新增一位常用乘車人</span>
                  : <select value={form.travelerId ?? ""} onChange={(e) => setForm({ ...form, travelerId: Number(e.target.value) })} required>
                      {travelers.map((traveler) => <option value={traveler.id} key={traveler.id}>{traveler.label}（{maskIdentity(traveler.identity)}）</option>)}
                    </select>}
              </label>
              <p className="wide muted">直接以乘車人資料訂票，不先登入台鐵會員。</p>
              <div className="wide station-block">
                {countyGroupingAvailable(stations) && (
                  <label className="search-all-toggle">
                    <input type="checkbox" checked={searchAllStations} onChange={(e) => setSearchAllStations(e.target.checked)} />
                    搜尋全部車站（熟悉站名時不必先選縣市）
                  </label>
                )}
                <StationPicker
                  legend="出發"
                  stations={stations}
                  county={form.startCounty}
                  station={form.startStation}
                  searchAll={searchAllStations}
                  onChange={({ county, station }) => setForm({ ...form, startCounty: county, startStation: station })}
                />
                <button
                  type="button"
                  className="swap wide"
                  aria-label="交換出發與抵達"
                  onClick={() => setForm({
                    ...form,
                    startCounty: form.endCounty,
                    startStation: form.endStation,
                    endCounty: form.startCounty,
                    endStation: form.startStation,
                  })}
                >⇄ 交換出發／抵達</button>
                <StationPicker
                  legend="抵達"
                  stations={stations}
                  county={form.endCounty}
                  station={form.endStation}
                  searchAll={searchAllStations}
                  onChange={({ county, station }) => setForm({ ...form, endCounty: county, endStation: station })}
                />
              </div>
              <label>乘車日期<input type="date" value={form.rideDate} onChange={(e) => setForm({ ...form, rideDate: e.target.value })} required /></label>
              {form.searchMode === "DIRECT" ? (
                <label>車次<input inputMode="numeric" pattern="[0-9]+" value={form.trainNumber} onChange={(e) => setForm({ ...form, trainNumber: e.target.value, chosenTrain: null })} placeholder="例如 110" required /></label>
              ) : (
                <>
                  <label>開始時段<select value={form.startTime} onChange={(e) => setForm({ ...form, startTime: e.target.value })}>{times.map((value) => <option key={value}>{value}</option>)}</select></label>
                  <label>結束時段<select value={form.endTime} onChange={(e) => setForm({ ...form, endTime: e.target.value })}>{times.map((value) => <option key={value}>{value}</option>)}</select></label>
                  <label>車種
                    <select value={form.trainType} onChange={(e) => setForm({ ...form, trainType: e.target.value })}>
                      <option value="ALL">全部車種</option>
                      {trainTypes.map((name) => <option key={name} value={name}>{name}</option>)}
                    </select>
                  </label>
                  <div className="suggestion-options wide">
                    <label><input type="checkbox" checked={form.preferReserved} onChange={(e) => setForm({ ...form, preferReserved: e.target.checked })} /> 排序時優先對號列車</label>
                    <label><input type="checkbox" checked={form.includeTransfers} onChange={(e) => setForm({ ...form, includeTransfers: e.target.checked })} /> 包含單次轉乘建議</label>
                  </div>
                  <button
                    type="button"
                    className="primary wide"
                    disabled={suggestionBusy}
                    onClick={() => void querySuggestions().catch((reason) => setError(
                      reason instanceof TypeError ? "無法連線至服務，請確認網路後重試。"
                        : reason instanceof Error ? reason.message : "查詢失敗，請稍後重試。"
                    ))}
                  >{suggestionBusy ? "查詢中…" : "① 查詢車次建議"}</button>
                </>
              )}
              <label>一般座票數<select value={form.quantity} onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })}>{[1, 2, 3, 4, 5, 6].map((value) => <option key={value}>{value}</option>)}</select></label>
              <fieldset className="mode-switch wide">
                <legend>執行模式</legend>
                <label><input type="radio" checked={form.mode === "monitor_only"} onChange={() => setForm({ ...form, mode: "monitor_only" })} /> 只監控，到點提醒我</label>
                <label><input type="radio" checked={form.mode === "book_when_available"} onChange={() => setForm({ ...form, mode: "book_when_available" })} /> 到點就準備訂票頁</label>
              </fieldset>
              <fieldset className="mode-switch wide">
                <legend>開始時間</legend>
                <label><input type="radio" checked={form.startNow} onChange={() => setForm({ ...form, startNow: true })} /> 立即開始</label>
                <label><input type="radio" checked={!form.startNow} onChange={() => setForm({ ...form, startNow: false })} /> 指定時間</label>
              </fieldset>
              {!form.startNow && (
                <label className="wide">開始監控時間<input type="datetime-local" value={form.scheduledAt} onChange={(e) => setForm({ ...form, scheduledAt: e.target.value })} required /></label>
              )}
              <label>重試間隔
                <select value={form.pollIntervalMinutes} onChange={(e) => setForm({ ...form, pollIntervalMinutes: Number(e.target.value) })}>
                  {[1, 3, 5, 10, 15, 30, 60].map((minutes) => (
                    <option value={minutes} key={minutes}>{minutes} 分鐘</option>
                  ))}
                </select>
              </label>
              <label>監控截止時間<input type="datetime-local" value={form.monitorUntil} onChange={(e) => setForm({ ...form, monitorUntil: e.target.value })} /></label>
              <p className="wide privacy-note">
                系統<strong>無法得知任何車次是否有位</strong>：台鐵餘票沒有可用的官方開放資料來源。
                每個間隔自動備好訂票頁並通知你接手，每輪最多等你 15 分鐘。驗證與送出一律由你本人完成。沒完成就等下一輪，直到訂到、你取消，或超過監控截止時間。
              </p>
              {error && <p className="error wide" role="alert">{error}</p>}
              {notice && <p className="notice wide" role="status">{notice}</p>}
              {form.trainNumber
                ? <p className="notice wide" role="status">將鎖定 <b>{form.chosenTrain ? trainLabel(form.chosenTrain) : `車次 ${form.trainNumber}`}</b>；訂票畫面打開時就是這一班。</p>
                : <p className="wide muted">{form.searchMode === "BY_TIME"
                    ? "還沒選車次。按上面「① 查詢車次建議」，再從清單按「② 改用此車次」。訂票畫面必須打開在你挑的那一班車上。"
                    : "還沒填車次。填入車次號碼即可建立任務。"}</p>}
              <button className="primary wide" disabled={busy || !form.trainNumber}>{busy ? "建立中…" : "加入任務佇列"}</button>
            </form>
            {form.searchMode === "BY_TIME" && suggestions && suggestionKey !== currentSuggestionKey && (
              <p className="notice" role="status">查詢條件已變更，請再按一次「① 查詢車次建議」。</p>
            )}
            {form.searchMode === "BY_TIME" && suggestions && suggestionKey === currentSuggestionKey && (
              <section className="suggestions-panel" aria-label="車次建議">
                <div className="suggestion-warning"><b>時刻建議，不是餘位資訊</b><p>{suggestions.notice}</p></div>
                <h3>時段內對號列車</h3>
                {filterByType(suggestions.primary).length ? filterByType(suggestions.primary).map((item) => <CandidateRow key={`p-${item.train_no}-${item.departure_time}`} item={item} onChoose={chooseCandidate} />) : <p className="muted">這個時段沒有對號列車候選。</p>}
                <h3>其他選擇</h3>
                {filterByType(suggestions.alternatives).slice(0, 8).map((item) => <CandidateRow key={`a-${item.train_no}-${item.departure_time}`} item={item} onChoose={chooseCandidate} />)}
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
            <div className="panel-heading"><div><span className="step">04</span><h2>任務佇列</h2></div><button className="text-button" onClick={loadTasks}>重新整理</button></div>
            <div className="task-list">
              {tasks.length === 0 && <div className="empty"><b>尚無任務</b><p>建立第一個訂票條件後，狀態會顯示在這裡。</p></div>}
              {tasks.map((task) => (
                <article id={`task-${task.id}`} className={`task-card${task.id === linkedTaskId ? " task-card-linked" : ""}`} key={task.id}>
                  <div className="task-main">
                    <span className={`status status-${task.status}`}>{STATUS_TEXT[task.status] ?? task.status}</span>
                    <h3>{task.route}</h3>
                    <p className="task-train">{task.train_label ?? "未指定車次"}</p>
                    <p>{task.ride_date} · {task.mode === "monitor_only" ? "到點提醒一次" : "每輪自動備頁，等你接手"}</p>
                    {task.booking_code && <p className="booking-code">訂位代碼 <b>{task.booking_code}</b></p>}
                  </div>
                  <dl className="task-time">
                    <div><dt>開始監控</dt><dd>{formatDate(task.monitor_start_at)}</dd></div>
                    <div><dt>上次查詢</dt><dd>{task.last_checked_at ? formatDate(task.last_checked_at) : "尚未查詢"}</dd></div>
                    <div><dt>下次查詢</dt><dd>{nextCheckText(task)}</dd></div>
                    <div><dt>餘票資料</dt><dd className="unknown">未提供</dd></div>
                  </dl>
                  <div className="task-actions">
                    <button className="primary" onClick={() => runPrimaryAction(task)} disabled={!PRIMARY_ACTION[task.status]}>
                      {PRIMARY_ACTION[task.status]?.label ?? "無可用操作"}
                    </button>
                    <details className="task-details">
                      <summary>查看詳情</summary>
                      <p>查詢間隔：每 {Math.round(task.poll_interval_seconds / 60)} 分鐘</p>
                      <p>監控截止：{task.monitor_until ? formatDate(task.monitor_until) : task.mode === "monitor_only" ? "未設定（提醒一次）" : "未設定（直到訂到或取消）"}</p>
                      <p className="unknown">{task.availability_note}</p>
                      {task.last_error && <p className="error">最近錯誤：{task.last_error}</p>}
                      {task.status === "waiting_human" && <button className="compact" title="檔案可能包含已保存的台鐵會員登入資料，使用後請妥善刪除" onClick={() => downloadConfig(task.id)}>下載訂票設定</button>}
                      {["scheduled", "monitoring", "waiting_human"].includes(task.status) && <button className="danger" onClick={() => cancelTask(task.id)}>停止並取消任務</button>}
                      <button className="danger" onClick={() => deleteTask(task.id)}>刪除任務</button>
                    </details>
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
      {booking && <BookingScreen session={booking} onClose={() => void closeBooking(booking)} />}
    </div>
  );
}

function BookingScreen({ session, onClose }: { session: BookingSession; onClose: () => void }) {
  const finished = FINISHED_STATUSES.includes(session.status);
  return (
    <div className="booking-screen" role="dialog" aria-modal="true" aria-label="訂票驗證畫面">
      <header>
        <div>
          <strong>{session.trainLabel ?? session.route}</strong>
          <small>{finished ? "已結束" : `${session.route}｜請在下方畫面完成官方驗證，然後自行按下訂票`}</small>
        </div>
        <button onClick={onClose}>{finished ? "關閉" : "放棄這次訂票"}</button>
      </header>
      {finished ? (
        <div className={`booking-outcome ${session.status}`}>
          <b>{STATUS_TEXT[session.status] ?? session.status}</b>
          {session.bookingCode && <p className="booking-code">訂位代碼 {session.bookingCode}</p>}
          {session.message && <p>{session.message}</p>}
          {session.bookingCode && <p className="boundary-note">請至台鐵官網或超商於期限內完成付款取票。</p>}
        </div>
      ) : (
        <iframe title="台鐵訂票畫面" src={viewerUrl(session.sessionToken)} allow="clipboard-write" />
      )}
      <footer>驗證碼與送出都由你本人完成；系統只負責把已填好的畫面送到你面前，並在拿到訂位代碼後記錄結果。</footer>
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
