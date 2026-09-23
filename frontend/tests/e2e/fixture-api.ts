// Synthetic contract fixture. Imported only by Playwright; never bundled into production.
import type { Page as BrowserPage } from "@playwright/test";
import type {
  AccountInfo,
  Employee,
  Meeting,
  Notification,
  RecordingStatus,
  Task,
  User,
} from "../../src/types";
const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
export async function fixtureApi(
  page: BrowserPage,
  options: {
    extractionError?: boolean;
    role?: User["role"];
    uploadError?: boolean;
  } = {},
) {
  let user: User = {
    id: id(1),
    login: "fixture-admin",
    must_change_password: false,
    role: options.role || "admin",
    employee: {
      id: id(2),
      fio: "Әлия Қасым — тест",
      position: "Секретарь",
      department: "Тестовый отдел",
    },
  };
  const employees: Employee[] = [
    {
      ...user.employee!,
      active: true,
      has_account: true,
      user_id: user.id,
      voice_profile: {
        status: "none",
        quality_status: null,
        speech_seconds: null,
        created_at: null,
        consent_at: null,
      },
    },
  ];
  const adminUser = user;
  let csrf = "fixture-csrf";
  const accounts: Array<AccountInfo & { fixturePassword: string }> = [];
  const state = {
    accounts,
    failAccountOnce: false,
    qualityError: false,
    get user() {
      return user;
    },
    loggedIn: false,
    employees,
    meetings: [] as Meeting[],
    notifications: [] as Notification[],
    requests: [] as { path: string; method: string; body: unknown }[],
    polls: 0,
  };
  const paged = (items: unknown[], params: URLSearchParams) => ({
    items: items.slice(
      Number(params.get("offset") || 0),
      Number(params.get("offset") || 0) + Number(params.get("limit") || 50),
    ),
    total: items.length,
    limit: Number(params.get("limit") || 50),
    offset: Number(params.get("offset") || 0),
  });
  function notify(event_type: string, m: Meeting, task?: Task) {
    state.notifications.push({
      id: id(500 + state.notifications.length),
      event_type,
      title:
        event_type === "meeting_invitation"
          ? "Приглашение на совещание"
          : "Вам назначено поручение",
      message: task?.task || m.title,
      meeting_id: m.id,
      task_id: task?.id || null,
      created_at: "2026-09-23T10:00:00Z",
      read_at: null,
    });
  }
  function complete(m: Meeting) {
    m.recording!.processing_status = "done";
    m.recording!.stage = "done";
    m.recording!.audio_url = `/meetings/${m.id}/recordings/${m.recording!.id}/audio`;
    m.recording!.extraction = {
      status: options.extractionError ? "error" : "ok",
      error_code: options.extractionError ? "LLM_INVALID_RESPONSE" : null,
      error_message: options.extractionError
        ? "Тестовая ошибка локального извлечения"
        : null,
    };
    m.summary = options.extractionError
      ? null
      : "Тестовое саммари: бюджет и жоспар";
    m.speakers = [
      {
        label: "SPEAKER_00",
        proposed_employee_id: id(2),
        confirmed_employee_id: null,
        manually_set: false,
        similarity: 0.54,
        second_similarity: null,
        review_required: true,
        review_reasons: ["below_threshold"],
        clean_speech_seconds: 20,
        utterance_count: 1,
      },
    ];
    m.utterances = [
      {
        id: 1,
        speaker_label: "SPEAKER_00",
        employee_id: null,
        start: 0,
        end: 1,
        text: "Әлия, дайындаңыз есеп. Подготовьте отчёт до пятницы.",
        language: "kk",
        uncertain: true,
        uncertain_reasons: ["ambiguous_speaker"],
      },
    ];
    m.tasks = options.extractionError
      ? []
      : [
          {
            id: id(100),
            meeting_id: m.id,
            meeting_title: m.title,
            from: "SPEAKER_00",
            from_speaker_label: "SPEAKER_00",
            from_fio: null,
            to: null,
            to_fio: null,
            task: "Подготовить отчёт — есеп",
            deadline: null,
            deadline_source: "до пятницы",
            deadline_at: null,
            evidence: m.utterances[0].text,
            source_utterance_ids: [1],
            confidence: 0.7,
            status: "draft",
            execution_status: "in_progress",
            completed_at: null,
            overdue: false,
            needs_review: true,
            review_reasons: ["missing_assignee", "missing_deadline"],
            origin: "llm",
            created_at: "2026-09-23T10:00:00Z",
            updated_at: "2026-09-23T10:00:00Z",
          },
        ];
    m.draft_revision++;
  }
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.slice(4);
    const method = req.method();
    let body: any = null;
    if (req.headers()["content-type"]?.includes("application/json"))
      body = req.postDataJSON();
    state.requests.push({ path, method, body });
    const ok = (data: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    const fail = (status: number, code: string, message: string) =>
      ok({ error: { code, message, details: null } }, status);
    if (path === "/auth/login") {
      if (body.login === "fixture-admin") user = adminUser;
      else {
        const account = accounts.find(
          (a) => a.login === body.login && a.fixturePassword === body.password,
        );
        if (!account)
          return fail(401, "INVALID_CREDENTIALS", "Неверный логин или пароль");
        user = {
          id: account.id,
          login: account.login,
          role: account.role,
          must_change_password: account.must_change_password,
          employee: employees.find((e) => e.id === account.employee_id)!,
        };
      }
      state.loggedIn = true;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: {
          "Set-Cookie": "darai_session=fixture; HttpOnly; Path=/; SameSite=Lax",
        },
        body: JSON.stringify({ user, csrf_token: csrf }),
      });
    }
    if (!state.loggedIn)
      return fail(401, "UNAUTHENTICATED", "Тестовая сессия завершена");
    if (method !== "GET" && req.headers()["x-csrf-token"] !== csrf)
      return fail(403, "CSRF_FAILED", "Нет CSRF заголовка");
    if (path === "/auth/me") return ok({ ...user, csrf_token: csrf });
    if (path === "/auth/logout") {
      state.loggedIn = false;
      return route.fulfill({ status: 204 });
    }
    if (path === "/auth/change-password") {
      const account = accounts.find((a) => a.id === user.id)!;
      if (!account || body.current_password !== account.fixturePassword)
        return fail(422, "INVALID_CURRENT_PASSWORD", "Неверный текущий пароль");
      account.fixturePassword = body.new_password;
      account.must_change_password = false;
      user = { ...user, must_change_password: false };
      csrf = "rotated-fixture-csrf";
      return ok({ user, csrf_token: csrf });
    }
    if (user.must_change_password)
      return fail(
        403,
        "PASSWORD_CHANGE_REQUIRED",
        "Необходимо сменить временный пароль",
      );
    if (path === "/users" && method === "GET")
      return ok(
        paged(
          accounts.map(({ fixturePassword, ...a }) => a),
          url.searchParams,
        ),
      );
    if (path === "/users" && method === "POST") {
      if (state.failAccountOnce) {
        state.failAccountOnce = false;
        return fail(409, "LOGIN_TAKEN", "Этот логин уже занят");
      }
      const employee = employees.find((e) => e.id === body.employee_id)!;
      if (employee.has_account)
        return fail(409, "EMPLOYEE_HAS_ACCOUNT", "Аккаунт уже существует");
      const account: AccountInfo & { fixturePassword: string } = {
        id: id(700 + accounts.length),
        login: body.login,
        role: body.role,
        active: true,
        must_change_password: true,
        employee_id: employee.id,
        employee_fio: employee.fio,
        created_at: "2026-09-23T10:00:00Z",
        fixturePassword: "Temporary-fixture-123",
      };
      accounts.push(account);
      employee.has_account = true;
      employee.user_id = account.id;
      const { fixturePassword, ...out } = account;
      return ok({ ...out, temporary_password: fixturePassword }, 201);
    }
    if (path.startsWith("/users/") && path.endsWith("/reset-password")) {
      const account = accounts.find(
        (a) => path === `/users/${a.id}/reset-password`,
      )!;
      account.must_change_password = true;
      account.fixturePassword = "Reset-fixture-456";
      const { fixturePassword, ...out } = account;
      return ok({ ...out, temporary_password: fixturePassword });
    }
    if (path === "/employees/me")
      return ok({
        ...employees.find((e) => e.id === user.employee?.id),
        login: user.login,
      });
    if (path === "/employees" && method === "GET")
      return ok(
        paged(
          employees.filter(
            (e) =>
              !url.searchParams.get("q") ||
              `${e.fio} ${e.position} ${e.department}`.includes(
                url.searchParams.get("q")!,
              ),
          ),
          url.searchParams,
        ),
      );
    if (path === "/employees" && method === "POST") {
      const e: Employee = {
        ...body,
        id: id(employees.length + 10),
        active: true,
        has_account: false,
        user_id: null,
        voice_profile: {
          status: "none",
          quality_status: null,
          speech_seconds: null,
          created_at: null,
          consent_at: null,
        },
      };
      employees.push(e);
      return ok(e, 201);
    }
    const employee = employees.find(
      (e) =>
        path === `/employees/${e.id}` || path === `/employees/${e.id}/voice`,
    );
    if (employee) {
      if (path.endsWith("/voice")) {
        if (method === "DELETE") {
          employee.voice_profile.status = "none";
          return route.fulfill({ status: 204 });
        }
        if (!req.postDataBuffer()?.includes(Buffer.from('name="consent"')))
          return fail(422, "VALIDATION_ERROR", "consent обязателен");
        if (state.qualityError)
          return ok(
            {
              error: {
                code: "VOICE_QUALITY_REJECTED",
                message: "Слишком мало чистой речи",
                details: { reasons: ["too_short"] },
              },
            },
            422,
          );
        employee.voice_profile = {
          status: "ok",
          quality_status: "ok",
          speech_seconds: 24,
          created_at: "2026-09-23T10:00:00Z",
          consent_at: "2026-09-23T10:00:00Z",
        };
        return ok({ ...employee.voice_profile, reasons: [] }, 201);
      }
      if (method === "PATCH") Object.assign(employee, body);
      return ok(employee);
    }
    if (path === "/meetings" && method === "POST") {
      const m: Meeting = {
        id: id(50 + state.meetings.length),
        ...body,
        organizer: {
          id: user.id,
          login: user.login,
          employee_id: id(2),
          fio: user.employee!.fio,
        },
        secretary: {
          id: user.id,
          login: user.login,
          employee_id: id(2),
          fio: user.employee!.fio,
        },
        approval_status: "draft",
        protocol_version: 0,
        participant_count: body.participant_ids.length,
        recording: null,
        can_edit: true,
        draft_revision: 0,
        confirmed_at: null,
        confirmed_by: null,
        participants: employees
          .filter((e) => body.participant_ids.includes(e.id))
          .map((e) => ({
            employee_id: e.id,
            fio: e.fio,
            position: e.position,
            department: e.department,
            has_account: e.has_account,
            voice_status: e.voice_profile.status,
          })),
        permissions: {
          can_edit: true,
          can_confirm: true,
          can_export_draft: true,
        },
        content_visible: true,
        speakers: [],
        utterances: [],
        summary: null,
        summary_edited: false,
        tasks: [],
      };
      state.meetings.push(m);
      notify("meeting_invitation", m);
      return ok(m, 201);
    }
    if (path === "/meetings")
      return ok(paged(state.meetings, url.searchParams));
    const m = state.meetings.find((m) => path.startsWith(`/meetings/${m.id}`));
    if (m) {
      const suffix = path.slice(`/meetings/${m.id}`.length);
      if (!suffix) return ok(m);
      if (suffix === "/recordings" && method === "POST") {
        if (options.uploadError)
          return fail(
            413,
            "FILE_TOO_LARGE",
            "Тест: файл превышает допустимый размер",
          );
        if (!req.postDataBuffer()?.includes(Buffer.from('name="file"')))
          return fail(422, "VALIDATION_ERROR", "file обязателен");
        m.recording = {
          id: id(80),
          meeting_id: m.id,
          processing_status: "processing",
          stage: "transcribing",
          error_code: null,
          error_message: null,
          generation: 1,
          original_filename: "synthetic.wav",
          duration_seconds: 1,
          languages: ["ru", "kk"],
          extraction: {
            status: "not_started",
            error_code: null,
            error_message: null,
          },
          audio_url: null,
          created_at: "2026-09-23T10:00:00Z",
          updated_at: "2026-09-23T10:00:00Z",
        };
        return ok(m.recording, 202);
      }
      if (suffix.endsWith("/audio")) {
        const wav = Buffer.alloc(32044);
        wav.write("RIFF");
        wav.writeUInt32LE(32036, 4);
        wav.write("WAVEfmt ", 8);
        wav.writeUInt32LE(16, 16);
        wav.writeUInt16LE(1, 20);
        wav.writeUInt16LE(1, 22);
        wav.writeUInt32LE(16000, 24);
        wav.writeUInt32LE(32000, 28);
        wav.writeUInt16LE(2, 32);
        wav.writeUInt16LE(16, 34);
        wav.write("data", 36);
        wav.writeUInt32LE(32000, 40);
        return route.fulfill({ contentType: "audio/wav", body: wav });
      }
      if (suffix.startsWith("/recordings/")) {
        state.polls++;
        complete(m);
        return ok(m.recording);
      }
      if (suffix === "/summary") {
        m.summary = body.summary;
        m.summary_edited = true;
        m.draft_revision++;
        return ok({
          summary: m.summary,
          summary_edited: true,
          draft_revision: m.draft_revision,
        });
      }
      if (suffix === "/speakers") {
        for (const s of m.speakers) {
          if (Object.hasOwn(body, s.label)) {
            s.confirmed_employee_id = body[s.label];
            s.manually_set = true;
            s.review_required = false;
            for (const u of m.utterances)
              if (u.speaker_label === s.label) u.employee_id = body[s.label];
          }
        }
        m.draft_revision++;
        return ok({ speakers: m.speakers, draft_revision: m.draft_revision });
      }
      if (suffix === "/confirm") {
        if (body.draft_revision !== m.draft_revision)
          return fail(
            409,
            "DRAFT_REVISION_MISMATCH",
            "Протокол изменился после загрузки страницы",
          );
        if (
          !body.acknowledge_incomplete &&
          (m.tasks.some((t) => !t.to || !t.deadline) ||
            m.speakers.some((s) => !s.confirmed_employee_id) ||
            m.recording?.extraction.status === "error")
        )
          return fail(
            409,
            "CONFIRMATION_REQUIRES_ACKNOWLEDGEMENT",
            "Есть незаполненные поля",
          );
        m.approval_status = "confirmed";
        m.protocol_version = 1;
        for (const t of m.tasks) {
          t.status = "confirmed";
          if (t.to) notify("task_assigned", m, t);
        }
        return ok(m);
      }
      if (suffix === "/export")
        return route.fulfill({
          contentType:
            url.searchParams.get("fmt") === "pdf"
              ? "application/pdf"
              : "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          headers: {
            "Content-Disposition": `attachment; filename="fixture.${url.searchParams.get("fmt")}"`,
          },
          body: Buffer.from(
            "SYNTHETIC TEST DOWNLOAD — not a real generated document",
          ),
        });
    }
    const task = state.meetings
      .flatMap((m) => m.tasks)
      .find((t) => path === `/tasks/${t.id}`);
    if (task && method === "PATCH") {
      Object.assign(task, body);
      task.to_fio = employees.find((e) => e.id === task.to)?.fio || null;
      task.from_fio = employees.find((e) => e.id === task.from)?.fio || null;
      task.needs_review = false;
      state.meetings.find((m) => m.id === task.meeting_id)!.draft_revision++;
      return ok(task);
    }
    if (path === "/tasks") {
      let tasks = state.meetings
        .flatMap((m) => m.tasks)
        .filter((t) => t.status === "confirmed" && t.to === user.employee?.id);
      if (url.searchParams.get("execution_status"))
        tasks = tasks.filter(
          (t) =>
            t.execution_status === url.searchParams.get("execution_status"),
        );
      if (url.searchParams.get("overdue") === "true")
        tasks = tasks.filter((t) => t.overdue);
      return ok(paged(tasks, url.searchParams));
    }
    if (path === "/notifications") {
      const items = state.notifications.filter(
        (n) => url.searchParams.get("unread_only") !== "true" || !n.read_at,
      );
      return ok({
        ...paged(items, url.searchParams),
        unread_count: state.notifications.filter((n) => !n.read_at).length,
      });
    }
    if (path === "/notifications/read-all") {
      state.notifications.forEach((n) => (n.read_at = "2026-09-23T12:00:00Z"));
      return ok({ updated: state.notifications.length });
    }
    const n = state.notifications.find(
      (n) => path === `/notifications/${n.id}`,
    );
    if (n) {
      n.read_at = body.read ? "2026-09-23T12:00:00Z" : null;
      return ok(n);
    }
    return fail(
      501,
      "FIXTURE_NOT_IMPLEMENTED",
      `Тестовый маршрут не реализован: ${method} ${path}`,
    );
  });
  return state;
}
