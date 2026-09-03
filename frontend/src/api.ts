export interface User {
  id: number;
  email: string;
  created_at: string;
}

export interface Station {
  value: string;
  label: string;
}

export interface Task {
  id: string;
  status: "scheduled" | "waiting_human" | "cancelled" | "completed" | "failed";
  scheduled_at: string;
  route: string;
  ride_date: string;
  order_type: string;
  created_at: string;
  updated_at: string;
  last_error: string | null;
}

export interface MemberProfile {
  identity: string;
  member_account: string;
  has_member_password: boolean;
  updated_at: string | null;
}

export interface TrainCandidate {
  train_no: string;
  train_type_name: string;
  departure_time: string;
  arrival_time: string;
  duration_minutes: number;
  is_reserved_type: boolean;
  seat_type_label: string;
  note: string;
  in_requested_window: boolean;
}

export interface TransferSuggestion {
  transfer_station: Station;
  departure_time: string;
  arrival_time: string;
  duration_minutes: number;
  buffer_minutes: number;
  first_leg: TrainCandidate;
  second_leg: TrainCandidate;
  notice: string;
}

export interface Suggestions {
  primary: TrainCandidate[];
  alternatives: TrainCandidate[];
  transfers: TransferSuggestion[];
  availability_known: false;
  notice?: string;
}

const BASE_URL = import.meta.env.VITE_API_URL ?? "/api";

async function request<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${BASE_URL}${path}`, { ...options, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Keep the status-based message when the server did not return JSON.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  authenticate(mode: "login" | "register", email: string, password: string) {
    return request<{ access_token: string }>(`/auth/${mode}`, {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
  },
  me(token: string) {
    return request<User>("/auth/me", {}, token);
  },
  profile(token: string) {
    return request<MemberProfile>("/profile", {}, token);
  },
  saveProfile(token: string, payload: { identity: string; member_account: string; member_password: string }) {
    return request<MemberProfile>("/profile", { method: "PUT", body: JSON.stringify(payload) }, token);
  },
  deleteProfile(token: string) {
    return request<void>("/profile", { method: "DELETE" }, token);
  },
  clearMemberLogin(token: string) {
    return request<void>("/profile/member-login", { method: "DELETE" }, token);
  },
  logout(token: string) {
    return request<void>("/auth/logout", { method: "POST" }, token);
  },
  stations() {
    return request<Station[]>("/stations");
  },
  times() {
    return request<string[]>("/times");
  },
  suggestions(token: string, payload: unknown) {
    return request<Suggestions>("/suggestions", { method: "POST", body: JSON.stringify(payload) }, token);
  },
  tasks(token: string) {
    return request<Task[]>("/tasks", {}, token);
  },
  createTask(token: string, payload: unknown) {
    return request<Task>("/tasks", { method: "POST", body: JSON.stringify(payload) }, token);
  },
  cancelTask(token: string, taskId: string) {
    return request<void>(`/tasks/${taskId}/cancel`, { method: "POST" }, token);
  },
  taskConfig(token: string, taskId: string) {
    return request<Record<string, unknown>>(`/tasks/${taskId}/config`, {}, token);
  },
  taskSuggestions(token: string, taskId: string) {
    return request<Suggestions>(`/tasks/${taskId}/suggestions`, {}, token);
  },
};
