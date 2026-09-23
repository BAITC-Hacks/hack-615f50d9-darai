import type {
  AccountInfo,
  Role,
  Employee,
  EmployeeInput,
  Meeting,
  MeetingInput,
  MeetingListItem,
  NotificationsPage,
  Page,
  RecordingStatus,
  Task,
  TaskInput,
  User,
} from "./types";
let csrfToken = "";
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: Record<string, unknown>,
  ) {
    super(message);
  }
}
export function setCsrf(value: string) {
  csrfToken = value;
}
export function apiPath(path: string) {
  if (
    !path.startsWith("/") ||
    path.startsWith("//") ||
    path.includes("://") ||
    path.includes("\\") ||
    path.split("/").includes("..")
  )
    throw new Error("Некорректный адрес ресурса.");
  return `/api${path}`;
}
function failure(status: number, body: unknown) {
  const error = (
    body as {
      error?: {
        code: string;
        message: string;
        details?: Record<string, unknown>;
      };
    }
  )?.error;
  const result = new ApiError(
    status,
    error?.code || "CONNECTION_ERROR",
    error?.message ||
      (status === 413
        ? "Файл слишком большой. Выберите файл меньшего размера."
        : status === 401
          ? "Сессия завершена. Войдите снова."
          : `Сервер недоступен или вернул ошибку (${status}). Попробуйте ещё раз.`),
    error?.details,
  );
  if (status === 401 && result.code !== "INVALID_CREDENTIALS")
    window.dispatchEvent(new Event("darai:unauthorized"));
  if (result.code === "PASSWORD_CHANGE_REQUIRED")
    window.dispatchEvent(new Event("darai:password-required"));
  return result;
}
export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (typeof options.body === "string" && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET" && path !== "/auth/login")
    headers.set("X-CSRF-Token", csrfToken);
  const controller = new AbortController();
  const abort = () => controller.abort();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 45000);
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  try {
    const response = await fetch(apiPath(path), {
      ...options,
      signal: controller.signal,
      headers,
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw failure(response.status, body);
    }
    if (response.status === 204) return undefined as T;
    if (!response.headers.get("content-type")?.includes("application/json"))
      throw new ApiError(
        response.status,
        "INVALID_RESPONSE",
        "Сервер вернул неожиданный ответ. Проверьте настройку /api.",
      );
    return (await response.json()) as T;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    if (timedOut)
      throw new ApiError(
        0,
        "TIMEOUT",
        "Сервер не ответил вовремя. Обновите данные перед повтором действия.",
      );
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(
      0,
      "NETWORK_ERROR",
      "Не удалось подключиться к серверу. Проверьте подключение и повторите попытку.",
    );
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}
const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
});
const query = (values: Record<string, string | number | boolean | undefined>) =>
  new URLSearchParams(
    Object.entries(values)
      .filter(([, v]) => v !== undefined && v !== "")
      .map(([k, v]) => [k, String(v)]),
  ).toString();
export const api = {
  changePassword: (current_password: string, new_password: string) =>
    request<{ user: User; csrf_token: string }>(
      "/auth/change-password",
      json("POST", { current_password, new_password }),
    ),
  issueAccount: async (employee_id: string, login: string, role: Role) => {
    const r = await request<AccountInfo & { temporary_password: string }>(
      "/users",
      json("POST", { employee_id, login, role }),
    );
    return { userId: r.id, login: r.login, password: r.temporary_password };
  },
  resetPassword: async (id: string) => {
    const r = await request<AccountInfo & { temporary_password: string }>(
      `/users/${encodeURIComponent(id)}/reset-password`,
      json("POST"),
    );
    return { userId: r.id, login: r.login, password: r.temporary_password };
  },
  account: async (id: string, signal?: AbortSignal) => {
    for (let offset = 0; ; offset += 200) {
      const p = await request<Page<AccountInfo>>(
        `/users?limit=200&offset=${offset}`,
        { signal },
      );
      const found = p.items.find((u) => u.id === id);
      if (found) return found;
      if (offset + p.items.length >= p.total || !p.items.length) return null;
    }
  },
  myProfile: (signal?: AbortSignal) =>
    request<Employee & { login: string }>("/employees/me", { signal }),
  me: (signal?: AbortSignal) =>
    request<User & { csrf_token: string }>("/auth/me", { signal }),
  login: (login: string, password: string) =>
    request<{ user: User; csrf_token: string }>(
      "/auth/login",
      json("POST", { login, password }),
    ),
  logout: () => request<void>("/auth/logout", json("POST")),
  employees: (q = "", offset = 0, signal?: AbortSignal) =>
    request<Page<Employee>>(`/employees?${query({ q, offset, limit: 50 })}`, {
      signal,
    }),
  allEmployees: async (signal?: AbortSignal) => {
    const result: Employee[] = [];
    for (let offset = 0; ; offset += 200) {
      const p = await request<Page<Employee>>(
        `/employees?limit=200&offset=${offset}`,
        { signal },
      );
      result.push(...p.items);
      if (result.length >= p.total || !p.items.length) return result;
    }
  },
  employee: (id: string, signal?: AbortSignal) =>
    request<Employee>(`/employees/${encodeURIComponent(id)}`, { signal }),
  saveEmployee: (id: string | undefined, body: EmployeeInput) =>
    request<Employee>(
      id ? `/employees/${encodeURIComponent(id)}` : "/employees",
      json(id ? "PATCH" : "POST", body),
    ),
  deleteEmployee: (id: string) =>
    request<void>(`/employees/${encodeURIComponent(id)}`, json("DELETE")),
  deleteVoice: (id: string) =>
    request<void>(`/employees/${id}/voice`, json("DELETE")),
  meetings: (q = "", offset = 0, signal?: AbortSignal) =>
    request<Page<MeetingListItem>>(
      `/meetings?${query({ q, offset, limit: 50 })}`,
      { signal },
    ),
  meeting: (id: string, signal?: AbortSignal) =>
    request<Meeting>(`/meetings/${encodeURIComponent(id)}`, { signal }),
  createMeeting: (body: MeetingInput) =>
    request<Meeting>("/meetings", json("POST", body)),
  recording: (meetingId: string, id: string, signal?: AbortSignal) =>
    request<RecordingStatus>(`/meetings/${meetingId}/recordings/${id}`, {
      signal,
    }),
  retry: (meetingId: string, id: string) =>
    request<RecordingStatus>(
      `/meetings/${meetingId}/recordings/${id}/retry`,
      json("POST"),
    ),
  speakers: (id: string, body: Record<string, string | null>) =>
    request(`/meetings/${id}/speakers`, json("PATCH", body)),
  summary: (id: string, summary: string) =>
    request(`/meetings/${id}/summary`, json("PATCH", { summary })),
  saveTask: (meetingId: string, id: string | undefined, body: TaskInput) =>
    request<Task>(
      id ? `/tasks/${id}` : `/meetings/${meetingId}/tasks`,
      json(id ? "PATCH" : "POST", body),
    ),
  deleteTask: (id: string) => request<void>(`/tasks/${id}`, json("DELETE")),
  execute: (id: string, execution_status: Task["execution_status"]) =>
    request<Task>(`/tasks/${id}`, json("PATCH", { execution_status })),
  confirm: (
    id: string,
    draft_revision: number,
    acknowledge_incomplete: boolean,
  ) =>
    request<Meeting>(
      `/meetings/${id}/confirm`,
      json("POST", { draft_revision, acknowledge_incomplete }),
    ),
  tasks: (filter: string, offset = 0, signal?: AbortSignal) =>
    request<Page<Task>>(
      `/tasks?${query({ assignee: "me", execution_status: filter === "in_progress" || filter === "completed" ? filter : undefined, overdue: filter === "overdue" ? true : undefined, limit: 50, offset })}`,
      { signal },
    ),
  notifications: (
    unread_only = false,
    offset = 0,
    signal?: AbortSignal,
    limit = 50,
  ) =>
    request<NotificationsPage>(
      `/notifications?${query({ unread_only, offset, limit })}`,
      { signal },
    ),
  readNotification: (id: string, read: boolean) =>
    request(`/notifications/${id}`, json("PATCH", { read })),
  readAll: () => request("/notifications/read-all", json("POST")),
};
export function upload<T>(
  path: string,
  file: File,
  progress: (percent: number) => void,
  signal?: AbortSignal,
  consent = false,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const cleanup = () => signal?.removeEventListener("abort", abort);
    xhr.open("POST", apiPath(path));
    xhr.withCredentials = true;
    xhr.timeout = 600000;
    xhr.setRequestHeader("X-CSRF-Token", csrfToken);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) progress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      cleanup();
      let data: unknown;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        reject(failure(xhr.status, null));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as T);
      else reject(failure(xhr.status, data));
    };
    xhr.onerror = () => {
      cleanup();
      reject(
        new Error(
          "Загрузка прервана: сервер недоступен. Проверьте соединение и повторите.",
        ),
      );
    };
    xhr.ontimeout = () => {
      cleanup();
      reject(
        new Error(
          "Сервер не ответил вовремя. Обновите карточку перед повторной загрузкой.",
        ),
      );
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Загрузка отменена", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) {
      reject(new DOMException("Загрузка отменена", "AbortError"));
      return;
    }
    const form = new FormData();
    form.append("file", file);
    if (consent) form.append("consent", "true");
    xhr.send(form);
  });
}
export async function downloadExport(
  id: string,
  fmt: "docx" | "pdf",
  includeTranscript: boolean,
) {
  let response: Response;
  try {
    response = await fetch(
      apiPath(
        `/meetings/${id}/export?fmt=${fmt}&include_transcript=${includeTranscript}`,
      ),
      { credentials: "include", cache: "no-store" },
    );
  } catch {
    throw new Error("Не удалось подключиться к серверу экспорта.");
  }
  if (!response.ok)
    throw failure(response.status, await response.json().catch(() => null));
  const type = response.headers.get("content-type") || "";
  if (
    !type.includes(
      fmt === "pdf"
        ? "application/pdf"
        : "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
  )
    throw new Error("Сервер не вернул документ.");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `DARAI-${id}.${fmt}`;
  const disposition = response.headers.get("content-disposition") || "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1];
  const plain = /filename="([^"]+)"/i.exec(disposition)?.[1];
  try {
    const filename = encoded ? decodeURIComponent(encoded) : plain;
    if (filename?.toLowerCase().endsWith(`.${fmt}`))
      a.download = filename.replace(/[\\/\u0000-\u001f]/g, "_");
  } catch {
    // Malformed header: retain the safe default filename.
  }
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
