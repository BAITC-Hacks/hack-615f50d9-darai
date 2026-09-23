import { MeetingLiveRecorder } from "./MeetingLiveRecorder";
import { liveApi } from "./live-api";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, apiPath, downloadExport, upload } from "./api";
import type { Meeting, MeetingInput, RecordingStatus } from "./types";
import { useUser } from "./App";
import {
  ActionStatus,
  Back,
  Badge,
  Empty,
  Field,
  Notice,
  PageTitle,
  ResourceState,
  useAction,
  useResource,
} from "./ui";
import { Pagination } from "./Employees";
import { dateTime, timecode, zonedToISO } from "./utils";
import { TaskCard, TaskForm } from "./Tasks";
export const stages: Record<string, string> = {
  queued: "В очереди",
  normalizing: "Подготовка аудио",
  transcribing: "Распознавание речи",
  diarizing: "Разделение спикеров",
  aligning: "Выравнивание реплик",
  identifying: "Сопоставление голосов",
  extracting: "Извлечение поручений",
  done: "Обработка завершена",
};
function ProcessingBadge({ recording }: { recording: RecordingStatus | null }) {
  return (
    <Badge
      tone={
        !recording
          ? "neutral"
          : recording.processing_status === "error"
            ? "error"
            : recording.processing_status === "done"
              ? "success"
              : "info"
      }
    >
      {!recording
        ? "Нет записи"
        : recording.processing_status === "error"
          ? "Ошибка обработки"
          : stages[recording.stage] || recording.stage}
    </Badge>
  );
}
export function Meetings() {
  const user = useUser();
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const r = useResource(
    (signal) => api.meetings(search, offset, signal),
    `${search}:${offset}`,
  );
  return (
    <>
      <PageTitle
        title="Совещания"
        description="От повестки до утверждённого протокола. Все решения — в одном месте."
        action={
          user.role !== "employee" && (
            <Link className="button" to="/meetings/new">
              ＋ Создать совещание
            </Link>
          )
        }
      />
      {r.data && (
        <div className="stats">
          <div className="stat">
            <span className="stat-label">Совещания</span>
            <span className="stat-value">{r.data.total}</span>
            <small>Доступные вам встречи</small>
          </div>
          <div className="stat">
            <span className="stat-label">На проверке</span>
            <span className="stat-value">
              {
                r.data.items.filter(
                  (m) =>
                    m.approval_status === "draft" &&
                    m.recording?.processing_status === "done",
                ).length
              }
            </span>
            <small>На текущей странице</small>
          </div>
          <div className="stat">
            <span className="stat-label">Утверждены</span>
            <span className="stat-value">
              {
                r.data.items.filter((m) => m.approval_status === "confirmed")
                  .length
              }
            </span>
            <small>На текущей странице</small>
          </div>
        </div>
      )}
      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          setSearch(q);
          setOffset(0);
        }}
      >
        <input
          aria-label="Поиск совещаний"
          placeholder="Найти совещание по названию"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button className="secondary">Найти</button>
        <button type="button" className="text" onClick={r.reload}>
          Обновить список
        </button>
      </form>
      <ResourceState resource={r} />
      {r.data && (
        <section className="panel flush">
          <div className="panel-head">
            <div>
              <h2>Все совещания</h2>
              <p>Сначала создайте встречу, затем загрузите запись</p>
            </div>
            <Badge>{r.data.total} встреч</Badge>
          </div>
          {r.data.items.length ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Совещание</th>
                    <th>Дата и время</th>
                    <th>Обработка</th>
                    <th>Протокол</th>
                  </tr>
                </thead>
                <tbody>
                  {r.data.items.map((m) => (
                    <tr key={m.id}>
                      <td>
                        <Link to={`/meetings/${m.id}`}>
                          <strong>{m.title}</strong>
                        </Link>
                        <small>
                          {m.participant_count} участников ·{" "}
                          {m.organizer.fio || m.organizer.login}
                        </small>
                      </td>
                      <td>
                        {dateTime(m.starts_at, m.timezone)}
                        <small>{m.timezone}</small>
                      </td>
                      <td>
                        <ProcessingBadge recording={m.recording} />
                      </td>
                      <td>
                        <Badge
                          tone={
                            m.approval_status === "confirmed"
                              ? "success"
                              : "warning"
                          }
                        >
                          {m.approval_status === "confirmed"
                            ? "Утверждён"
                            : "Черновик"}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title={
                search
                  ? "Совещания не найдены"
                  : "Здесь появятся ваши совещания"
              }
            >
              {search
                ? "Измените поисковый запрос."
                : "Создайте встречу с повесткой и участниками. Запись можно загрузить позже."}
            </Empty>
          )}
          <Pagination
            offset={offset}
            total={r.data.total}
            onChange={setOffset}
          />
        </section>
      )}
    </>
  );
}
export function NewMeeting() {
  const user = useUser();
  const navigate = useNavigate();
  const employees = useResource(
    (signal) => api.allEmployees(signal),
    "participants",
  );
  const action = useAction();
  const [form, setForm] = useState({
    title: "",
    starts_at: "",
    timezone: "Asia/Almaty",
    agenda: "",
    participant_ids: [] as string[],
  });
  const [search, setSearch] = useState("");
  if (user.role === "employee")
    return (
      <Notice kind="error">
        Создавать встречи может секретарь или администратор.
      </Notice>
    );
  function submit(e: FormEvent) {
    e.preventDefault();
    void action.run(async () => {
      const input: MeetingInput = {
        ...form,
        starts_at: zonedToISO(form.starts_at, form.timezone),
        secretary_id: null,
      };
      const m = await api.createMeeting(input);
      navigate(`/meetings/${m.id}`);
    }, "");
  }
  return (
    <>
      <Back to="/meetings">Совещания</Back>
      <PageTitle
        title="Новое совещание"
        description="Укажите повестку и участников. После создания встречи можно загрузить запись."
      />
      <form onSubmit={submit}>
        <div className="grid-2">
          <section className="panel">
            <h2>О встрече</h2>
            <Field label="Название совещания">
              <input
                required
                maxLength={255}
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                placeholder="Например, еженедельная планёрка"
              />
            </Field>
            <Field
              label="Дата и время"
              hint="В выбранном часовом поясе встречи."
            >
              <input
                type="datetime-local"
                required
                value={form.starts_at}
                onChange={(e) =>
                  setForm({ ...form, starts_at: e.target.value })
                }
              />
            </Field>
            <Field label="Часовой пояс (IANA)">
              <input
                required
                list="timezones"
                value={form.timezone}
                onChange={(e) => setForm({ ...form, timezone: e.target.value })}
              />
              <datalist id="timezones">
                <option value="Asia/Almaty" />
                <option value="Asia/Aqtau" />
                <option value="Asia/Aqtobe" />
                <option value="Europe/Moscow" />
                <option value="UTC" />
              </datalist>
            </Field>
            <Field label="Повестка">
              <textarea
                rows={5}
                value={form.agenda}
                onChange={(e) => setForm({ ...form, agenda: e.target.value })}
                placeholder="Темы и вопросы для обсуждения"
              />
            </Field>
          </section>
          <section className="panel">
            <h2>
              Участники <Badge>{form.participant_ids.length} / 200</Badge>
            </h2>
            <ResourceState resource={employees} />
            {employees.data && (
              <>
                <Field label="Поиск участника">
                  <input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="ФИО или департамент"
                  />
                </Field>
                <div className="participant-list">
                  {employees.data
                    .filter((e) =>
                      `${e.fio} ${e.department}`
                        .toLowerCase()
                        .includes(search.toLowerCase()),
                    )
                    .map((e) => (
                      <label className="check" key={e.id}>
                        <input
                          type="checkbox"
                          checked={form.participant_ids.includes(e.id)}
                          disabled={
                            !form.participant_ids.includes(e.id) &&
                            form.participant_ids.length >= 200
                          }
                          onChange={(event) =>
                            setForm({
                              ...form,
                              participant_ids: event.target.checked
                                ? [...form.participant_ids, e.id]
                                : form.participant_ids.filter(
                                    (id) => id !== e.id,
                                  ),
                            })
                          }
                        />
                        <span>
                          {e.fio}
                          <small style={{ display: "block" }}>
                            {e.department} ·{" "}
                            {e.has_account
                              ? "Получит приглашение"
                              : "Без учётной записи"}
                          </small>
                        </span>
                      </label>
                    ))}
                  {employees.data.length === 0 && (
                    <p>
                      Справочник пуст. Администратор может добавить сотрудников.
                    </p>
                  )}
                </div>
              </>
            )}
            <Notice>
              Приглашения появятся в CRM у участников с учётными записями. Перед
              записью уведомите участников об обработке речи.
            </Notice>
            <p>
              <small>
                Организатор и секретарь: {user.employee?.fio || user.login}
              </small>
            </p>
          </section>
        </div>
        <ActionStatus action={action} />
        <button
          disabled={action.busy || employees.loading || !!employees.error}
        >
          {action.busy ? "Создаём встречу…" : "Создать совещание"}
        </button>
      </form>
    </>
  );
}
export function MeetingDetail() {
  const { id } = useParams();
  const r = useResource((signal) => api.meeting(id!, signal), id!);
  const refresh = useCallback(async () => {
    r.setData(await api.meeting(id!));
  }, [id, r.setData]);
  return (
    <>
      <Back to="/meetings">Совещания</Back>
      <ResourceState resource={r} />
      {r.data && (
        <MeetingContent key={r.data.id} meeting={r.data} refresh={refresh} />
      )}
    </>
  );
}
function MeetingContent({
  meeting: m,
  refresh,
}: {
  meeting: Meeting;
  refresh: () => Promise<void>;
}) {
  const user = useUser();
  const audio = useRef<HTMLAudioElement>(null);
  const [audioError, setAudioError] = useState("");
  const [summary, setSummary] = useState(m.summary || "");
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [dirty, setDirty] = useState<Record<string, boolean>>({});
  const [adding, setAdding] = useState(false);
  const [ack, setAck] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [includeTranscript, setIncludeTranscript] = useState(false);
  const action = useAction();
  const exportAction = useAction();
  const editable =
    m.permissions.can_edit &&
    m.approval_status === "draft" &&
    m.recording?.processing_status === "done";
  const hasDirty = Object.values(dirty).some(Boolean);
  const markDirty = useCallback(
    (key: string, value: boolean) => setDirty((d) => ({ ...d, [key]: value })),
    [],
  );
  useEffect(() => {
    if (!dirty.summary) setSummary(m.summary || "");
  }, [m.summary, dirty.summary]);
  useEffect(() => {
    if (!hasDirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasDirty]);
  useEffect(() => {
    if (!window.location.hash) return;
    const timer = setTimeout(
      () =>
        document
          .getElementById(window.location.hash.slice(1))
          ?.scrollIntoView(),
      100,
    );
    return () => clearTimeout(timer);
  }, [m.id, m.tasks.length]);
  async function seek(seconds: number) {
    setAudioError("");
    const player = audio.current;
    if (!player) {
      setAudioError("Аудиозапись пока недоступна.");
      return;
    }
    try {
      player.currentTime = seconds;
      await player.play();
      player.scrollIntoView({ block: "center", behavior: "smooth" });
    } catch {
      setAudioError(
        "Не удалось воспроизвести фрагмент. Проверьте доступность записи и нажмите Play.",
      );
    }
  }
  const unresolved =
    m.tasks.some((t) => !t.to || !t.deadline) ||
    m.speakers.some((s) => !s.confirmed_employee_id) ||
    m.recording?.extraction.status === "error";
  return (
    <>
      <PageTitle
        eyebrow="КАРТОЧКА СОВЕЩАНИЯ"
        title={m.title}
        description={`${dateTime(m.starts_at, m.timezone)} · ${m.timezone}`}
        action={
          <Badge
            tone={m.approval_status === "confirmed" ? "success" : "warning"}
          >
            {m.approval_status === "confirmed"
              ? `Протокол утверждён · v${m.protocol_version}`
              : "Протокол: черновик"}
          </Badge>
        }
      />
      {hasDirty && (
        <Notice kind="warning">
          Есть несохранённые изменения. Сохраните или отмените их перед
          утверждением, экспортом и переходом на другую страницу.
        </Notice>
      )}
      <div className="grid-detail">
        <div>
          <section className="panel">
            <h2>Повестка</h2>
            <p className="summary">{m.agenda || "Повестка не указана"}</p>
            <div className="task-meta">
              <span>Организатор: {m.organizer.fio || m.organizer.login}</span>
              <span>Секретарь: {m.secretary.fio || m.secretary.login}</span>
            </div>
          </section>
          <RecordingPanel meeting={m} refresh={refresh} dirty={hasDirty} />
          {m.content_visible && m.recording?.audio_url && (
            <section className="panel">
              <h2>Аудиозапись</h2>
              <audio
                ref={audio}
                controls
                preload="metadata"
                style={{ width: "100%" }}
                src={apiPath(m.recording.audio_url)}
                onError={() =>
                  setAudioError(
                    "Запись недоступна. Обновите страницу или повторите позже.",
                  )
                }
              />
              {audioError && (
                <Notice kind="error">
                  {audioError}
                  <button
                    className="text"
                    onClick={() => {
                      setAudioError("");
                      audio.current?.load();
                    }}
                  >
                    Повторить загрузку аудио
                  </button>
                </Notice>
              )}
            </section>
          )}
          {!m.content_visible ? (
            <Notice>
              Протокол находится на проверке. Транскрипт и поручения станут
              доступны после утверждения.
            </Notice>
          ) : (
            <>
              <section className="panel">
                <h2>
                  Транскрипт <Badge>{m.utterances.length} реплик</Badge>
                </h2>
                {m.utterances.length ? (
                  <div className="transcript">
                    {m.utterances.map((u) => (
                      <div className="utterance" key={u.id}>
                        <button
                          className="timestamp"
                          disabled={!m.recording?.audio_url}
                          onClick={() => void seek(u.start)}
                          aria-label={`Воспроизвести фрагмент ${timecode(u.start)}`}
                        >
                          {timecode(u.start)}
                        </button>
                        <div>
                          <strong>
                            {m.participants.find(
                              (p) => p.employee_id === u.employee_id,
                            )?.fio ||
                              u.speaker_label ||
                              "Неизвестный спикер"}
                          </strong>
                          {u.uncertain && (
                            <>
                              {" "}
                              <Badge tone="warning">
                                Спикер требует проверки
                              </Badge>
                            </>
                          )}
                          <p>{u.text}</p>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty title="Транскрипт пока не готов">
                    {m.recording?.processing_status === "processing"
                      ? "Дождитесь завершения обработки."
                      : "Загрузите запись и запустите обработку."}
                  </Empty>
                )}
              </section>
              <section className="panel">
                <h2>Саммари совещания</h2>
                {editable ? (
                  <>
                    <Field label="Итоги и решения">
                      <textarea
                        rows={6}
                        disabled={action.busy}
                        value={summary}
                        onChange={(e) => {
                          setSummary(e.target.value);
                          markDirty(
                            "summary",
                            e.target.value !== (m.summary || ""),
                          );
                        }}
                      />
                    </Field>
                    <div className="form-actions">
                      <button
                        disabled={action.busy || !dirty.summary}
                        onClick={() =>
                          void action.run(async () => {
                            await api.summary(m.id, summary);
                            markDirty("summary", false);
                            await refresh();
                          }, "Саммари сохранено")
                        }
                      >
                        Сохранить саммари
                      </button>
                      {dirty.summary && (
                        <button
                          className="secondary"
                          onClick={() => {
                            setSummary(m.summary || "");
                            markDirty("summary", false);
                          }}
                        >
                          Отменить правки
                        </button>
                      )}
                    </div>
                  </>
                ) : (
                  <p className="summary">
                    {m.summary || "Саммари пока не подготовлено"}
                  </p>
                )}
              </section>
            </>
          )}
        </div>
        <aside>
          <section className="panel">
            <h2>
              Участники <Badge>{m.participants.length}</Badge>
            </h2>
            {m.participants.length ? (
              m.participants.map((p) => (
                <div
                  className="cell-person"
                  style={{ margin: "16px 0" }}
                  key={p.employee_id}
                >
                  <span className="avatar">{p.fio.slice(0, 1)}</span>
                  <div>
                    <Link to={`/employees/${p.employee_id}`}>{p.fio}</Link>
                    <small style={{ display: "block" }}>{p.position}</small>
                  </div>
                </div>
              ))
            ) : (
              <p>Участники не выбраны</p>
            )}
          </section>
          {m.content_visible && (
            <section className="panel">
              <h2>Проверка спикеров</h2>
              <p>
                <small>
                  Сходство голоса — мера сравнения, а не вероятность. Проверьте
                  предложения по записи.
                </small>
              </p>
              {m.speakers.length ? (
                m.speakers.map((s) => (
                  <div className="speaker-row" key={s.label}>
                    <label>
                      <strong>{s.label}</strong>
                      <select
                        aria-label={`Сопоставление ${s.label}`}
                        value={
                          Object.hasOwn(mapping, s.label)
                            ? mapping[s.label] || ""
                            : s.confirmed_employee_id || ""
                        }
                        disabled={!editable || action.busy}
                        onChange={(e) => {
                          setMapping({
                            ...mapping,
                            [s.label]: e.target.value || null,
                          });
                          markDirty("speakers", true);
                        }}
                      >
                        <option value="">Неизвестный</option>
                        {m.participants.map((p) => (
                          <option value={p.employee_id} key={p.employee_id}>
                            {p.fio}
                          </option>
                        ))}
                      </select>
                    </label>
                    <small>
                      Предложение:{" "}
                      {m.participants.find(
                        (p) => p.employee_id === s.proposed_employee_id,
                      )?.fio || "Нет кандидата"}
                      {s.similarity !== null
                        ? ` · сходство ${s.similarity.toFixed(2)}`
                        : ""}
                    </small>
                    {s.review_required && (
                      <Badge tone="warning">Требует проверки</Badge>
                    )}
                  </div>
                ))
              ) : (
                <p>Спикеры появятся после обработки</p>
              )}
              {editable && (
                <div className="form-actions">
                  <button
                    disabled={action.busy || !dirty.speakers}
                    onClick={() =>
                      void action.run(async () => {
                        await api.speakers(m.id, mapping);
                        setMapping({});
                        markDirty("speakers", false);
                        await refresh();
                      }, "Спикеры сохранены")
                    }
                  >
                    Сохранить спикеров
                  </button>
                  {dirty.speakers && (
                    <button
                      className="secondary"
                      onClick={() => {
                        setMapping({});
                        markDirty("speakers", false);
                      }}
                    >
                      Отмена
                    </button>
                  )}
                </div>
              )}
            </section>
          )}
          <section className="panel">
            <h2>Экспорт протокола</h2>
            <p>
              <small>
                {m.approval_status === "confirmed"
                  ? "Документ содержит утверждённые решения."
                  : "Черновик будет явно обозначен в документе."}
              </small>
            </p>
            <label className="check">
              <input
                type="checkbox"
                checked={includeTranscript}
                onChange={(e) => setIncludeTranscript(e.target.checked)}
              />
              Включить транскрипт
            </label>
            <div className="form-actions">
              {(["docx", "pdf"] as const).map((fmt) => (
                <button
                  className="secondary"
                  key={fmt}
                  disabled={
                    hasDirty ||
                    exportAction.busy ||
                    (!m.permissions.can_export_draft &&
                      m.approval_status !== "confirmed")
                  }
                  onClick={() =>
                    void exportAction.run(
                      () => downloadExport(m.id, fmt, includeTranscript),
                      "Документ скачан",
                    )
                  }
                >
                  ↓ {fmt.toUpperCase()}
                </button>
              ))}
            </div>
            <ActionStatus action={exportAction} />
          </section>
        </aside>
      </div>
      {m.content_visible && (
        <section>
          <PageTitle
            eyebrow="РЕШЕНИЯ И ОТВЕТСТВЕННОСТЬ"
            title="Поручения"
            description="Проверьте автора, исполнителя, срок и подтверждающую реплику."
            action={
              editable &&
              !adding && (
                <button
                  className="secondary"
                  onClick={() => {
                    setAdding(true);
                    markDirty("new-task", true);
                  }}
                >
                  ＋ Добавить поручение
                </button>
              )
            }
          />
          {m.recording?.extraction.status === "error" && (
            <Notice kind="error">
              <strong>Извлечение поручений не удалось.</strong>
              <p>
                {m.recording.extraction.error_message ||
                  "Локальная модель не смогла подготовить результат. Повторите извлечение или добавьте поручения вручную."}
              </p>
            </Notice>
          )}
          {m.tasks.map((t) => (
            <TaskCard
              key={t.id}
              task={t}
              editable={!!editable}
              participants={m.participants}
              speakers={m.speakers}
              utterances={m.utterances}
              onSeek={
                m.recording?.audio_url
                  ? (seconds) => void seek(seconds)
                  : undefined
              }
              onSaved={refresh}
              onDirty={markDirty}
              canExecute={
                t.status === "confirmed" &&
                (user.role === "admin" ||
                  m.permissions.can_edit ||
                  user.employee?.id === t.to)
              }
            />
          ))}
          {adding && (
            <section className="panel">
              <h2>Новое поручение</h2>
              <TaskForm
                meetingId={m.id}
                participants={m.participants}
                speakers={m.speakers}
                utterances={m.utterances}
                onDirty={(v) => markDirty("new-task", v)}
                onCancel={() => {
                  setAdding(false);
                  markDirty("new-task", false);
                }}
                onSaved={async () => {
                  await refresh();
                  setAdding(false);
                  markDirty("new-task", false);
                }}
              />
            </section>
          )}
          {!m.tasks.length && m.recording?.extraction.status !== "error" && (
            <Empty
              title={
                m.recording?.extraction.status === "ok"
                  ? "Поручения не найдены"
                  : "Поручения пока не подготовлены"
              }
            >
              {editable
                ? "Проверьте транскрипт и при необходимости добавьте поручение вручную."
                : "После обработки здесь появятся предложения для проверки."}
            </Empty>
          )}
        </section>
      )}
      <ActionStatus action={action} />
      {action.error && (
        <button
          className="secondary"
          disabled={hasDirty || action.busy}
          onClick={() =>
            void action.run(async () => {
              await refresh();
              setConfirmOpen(false);
              setAck(false);
            }, "Актуальная версия загружена. Проверьте протокол ещё раз.")
          }
        >
          Обновить версию протокола
        </button>
      )}
      {m.permissions.can_confirm && m.approval_status === "draft" && (
        <section className="panel">
          <h2>Утверждение протокола</h2>
          <p>
            После утверждения исполнители получат уведомления. Редактирование
            протокола и замена записи будут закрыты.
          </p>
          {unresolved && (
            <label className="check">
              <input
                type="checkbox"
                checked={ack}
                onChange={(e) => setAck(e.target.checked)}
              />
              Я проверил(а) неуточнённые поля, неизвестных спикеров и ошибки
              извлечения и подтверждаю протокол с этими ограничениями. Без
              исполнителя уведомление не создаётся; без срока напоминание не
              планируется.
            </label>
          )}
          {!confirmOpen ? (
            <button
              disabled={
                hasDirty ||
                action.busy ||
                m.recording?.processing_status !== "done" ||
                (!!unresolved && !ack)
              }
              onClick={() => setConfirmOpen(true)}
            >
              Утвердить протокол
            </button>
          ) : (
            <Notice kind="warning">
              Утвердить проверенную версию {m.draft_revision} и опубликовать
              поручения?
              <div className="form-actions">
                <button
                  disabled={hasDirty || action.busy || (!!unresolved && !ack)}
                  onClick={() =>
                    void action.run(async () => {
                      await api.confirm(m.id, m.draft_revision, ack);
                      setConfirmOpen(false);
                      await refresh();
                    }, "Протокол утверждён")
                  }
                >
                  Подтвердить и опубликовать
                </button>
                <button
                  className="secondary"
                  onClick={() => setConfirmOpen(false)}
                >
                  Вернуться к проверке
                </button>
              </div>
            </Notice>
          )}
        </section>
      )}
    </>
  );
}
function RecordingPanel({
  meeting: m,
  refresh,
  dirty,
}: {
  meeting: Meeting;
  refresh: () => Promise<void>;
  dirty: boolean;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [inputMode, setInputMode] = useState("upload");
  const [capturing, setCapturing] = useState(false);
  const [meetingUrl, setMeetingUrl] = useState(m.meeting_url || "");
  const [replaceLive, setReplaceLive] = useState(false);
  const [replace, setReplace] = useState(false);
  const [progress, setProgress] = useState(0);
  const [pollError, setPollError] = useState("");
  const [pollAttempt, setPollAttempt] = useState(0);
  const [live, setLive] = useState(m.recording);
  const action = useAction();
  const controller = useRef<AbortController | null>(null);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  useEffect(() => setLive(m.recording), [m.recording]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (
      !m.recording ||
      m.recording.processing_status !== "processing" ||
      !m.permissions.can_edit
    )
      return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let disposed = false;
    async function poll() {
      try {
        const r = await api.recording(m.id, m.recording!.id, abort.signal);
        if (disposed) return;
        setLive(r);
        setPollError("");
        if (r.processing_status !== "processing") {
          await refreshRef.current();
          return;
        }
      } catch (e) {
        if (!disposed)
          setPollError(
            e instanceof Error ? e.message : "Не удалось обновить обработку",
          );
      }
      if (!disposed) timer = setTimeout(poll, 3000);
    }
    timer = setTimeout(poll, 3000);
    return () => {
      disposed = true;
      clearTimeout(timer);
      abort.abort();
    };
  }, [
    m.id,
    m.recording?.id,
    m.recording?.processing_status,
    m.permissions.can_edit,
    pollAttempt,
  ]);
  return (
    <section className="panel">
      <div className="task-top">
        <h2>Запись и обработка</h2>
        <ProcessingBadge recording={live} />
      </div>
      {live && (
        <>
          <p>
            <small>
              {live.original_filename} · попытка {live.generation}
              {live.duration_seconds !== null
                ? ` · ${timecode(live.duration_seconds)}`
                : ""}
            </small>
          </p>
          {live.processing_status === "processing" && (
            <div className="steps">
              {Object.entries(stages).map(([key, label]) => (
                <span key={key} className={live.stage === key ? "current" : ""}>
                  {label}
                </span>
              ))}
            </div>
          )}
          {live.processing_status === "error" && (
            <Notice kind="error">
              <strong>
                Обработка остановлена: {stages[live.stage] || live.stage}.
              </strong>
              <p>{live.error_message || "Не удалось обработать запись."}</p>
              {live.error_code && <small>Код: {live.error_code}</small>}
            </Notice>
          )}
          {live.extraction.status === "error" && (
            <Notice kind="error">
              Извлечение не удалось. Транскрипт доступен для ручной проверки.
            </Notice>
          )}
        </>
      )}
      {pollError && (
        <Notice kind="error">
          {pollError}
          <button
            className="secondary"
            onClick={() => setPollAttempt((v) => v + 1)}
          >
            Повторить обновление
          </button>
        </Notice>
      )}
      {m.permissions.can_edit && m.approval_status === "draft" && (
        <>
          <Field label="Ссылка на онлайн-встречу (необязательно)">
            <input
              type="url"
              value={meetingUrl}
              onChange={(e) => setMeetingUrl(e.target.value)}
              placeholder="https://…"
            />
          </Field>
          <div className="form-actions">
            <button
              className="secondary"
              disabled={action.busy}
              onClick={() =>
                void action.run(async () => {
                  const value = meetingUrl.trim();
                  if (value && !/^https?:\/\//i.test(value))
                    throw new Error("Укажите ссылку http:// или https://");
                  await liveApi.link(m.id, value || null);
                  await refresh();
                }, "Ссылка сохранена")
              }
            >
              Сохранить ссылку
            </button>
            {/^(https?):\/\//i.test(meetingUrl.trim()) && (
              <a
                href={meetingUrl.trim()}
                target="_blank"
                rel="noopener noreferrer"
              >
                Открыть внешнюю встречу
              </a>
            )}
          </div>
          <div className="form-actions" aria-label="Способ добавления записи">
            <button
              className="secondary"
              aria-pressed={inputMode === "record"}
              disabled={capturing || action.busy}
              onClick={() => setInputMode("record")}
            >
              Записать сейчас
            </button>
            <button
              className="secondary"
              aria-pressed={inputMode === "upload"}
              disabled={capturing || action.busy}
              onClick={() => setInputMode("upload")}
            >
              Загрузить файл
            </button>
          </div>
          {inputMode === "record" && (
            <>
              {live && (
                <label className="check">
                  <input
                    type="checkbox"
                    disabled={capturing}
                    checked={replaceLive}
                    onChange={(e) => setReplaceLive(e.target.checked)}
                  />
                  Заменить запись и предыдущий черновой протокол
                </label>
              )}
              <MeetingLiveRecorder
                id={m.id}
                disabled={
                  dirty ||
                  live?.processing_status === "processing" ||
                  (!!live && !replaceLive)
                }
                refresh={refresh}
                onActive={setCapturing}
              />
            </>
          )}

          {live &&
            (live.processing_status === "error" ||
              live.extraction.status === "error") && (
              <button
                className="secondary"
                disabled={action.busy || dirty || capturing}
                onClick={() =>
                  void action.run(async () => {
                    await api.retry(m.id, live.id);
                    await refresh();
                  }, "Повторная обработка запущена")
                }
              >
                Повторить{" "}
                {live.extraction.status === "error"
                  ? "извлечение"
                  : "обработку"}
              </button>
            )}
          {inputMode === "upload" &&
            live?.processing_status !== "processing" && (
              <div className="recording-box" style={{ marginTop: 16 }}>
                <Field
                  label={
                    live ? "Заменить запись" : "Загрузить запись совещания"
                  }
                  hint="Аудио или видео с аудиодорожкой. Ограничения размера и длительности проверяет сервер."
                >
                  <input
                    type="file"
                    accept="audio/*,video/*,.m4a,.mkv"
                    disabled={action.busy}
                    onChange={(e) => {
                      setFile(e.target.files?.[0] || null);
                      setReplace(false);
                    }}
                  />
                </Field>
                {file && (
                  <p className="filesize">
                    {file.name} · {(file.size / 1024 / 1024).toFixed(1)} МБ
                  </p>
                )}
                {live && file && (
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={replace}
                      onChange={(e) => setReplace(e.target.checked)}
                    />
                    Заменить запись и удалить предыдущий транскрипт, саммари,
                    спикеров и все черновые поручения.
                  </label>
                )}
                <button
                  disabled={
                    !file || action.busy || dirty || (!!live && !replace)
                  }
                  onClick={() =>
                    void action.run(async () => {
                      if (!file) return;
                      setProgress(0);
                      controller.current = new AbortController();
                      await upload<RecordingStatus>(
                        `/meetings/${m.id}/recordings`,
                        file,
                        setProgress,
                        controller.current.signal,
                      );
                      setFile(null);
                      await refresh();
                    }, "Запись загружена. Обработка выполняется локально.")
                  }
                >
                  {action.busy ? "Загружаем запись…" : "Загрузить и обработать"}
                </button>
                {action.busy && (
                  <>
                    <progress
                      className="progress"
                      value={progress}
                      max={100}
                      aria-label="Загрузка записи"
                    />
                    <p role="status">
                      <small>
                        {progress < 100
                          ? `Загружено ${progress}%`
                          : "Файл передан. Сервер проверяет запись…"}
                      </small>
                    </p>
                  </>
                )}
              </div>
            )}
        </>
      )}
      <ActionStatus action={action} />
    </section>
  );
}
