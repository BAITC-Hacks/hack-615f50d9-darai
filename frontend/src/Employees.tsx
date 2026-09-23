import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, upload } from "./api";
import type { Employee, EmployeeInput } from "./types";
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
import { VoiceRecorder } from "./VoiceRecorder";
import { dateTime } from "./utils";
export const voiceLabels = {
  none: "Не зарегистрирован",
  ok: "Зарегистрирован",
  needs_review: "Нужна проверка",
  incompatible: "Нужна перерегистрация",
};
export function Pagination({
  offset,
  total,
  onChange,
}: {
  offset: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  return (
    <div className="toolbar" style={{ padding: "16px 22px", margin: 0 }}>
      <small>
        {total
          ? `${offset + 1}–${Math.min(offset + 50, total)} из ${total}`
          : "0 записей"}
      </small>
      <button
        className="secondary"
        disabled={!offset}
        onClick={() => onChange(Math.max(0, offset - 50))}
      >
        ← Назад
      </button>
      <button
        className="secondary"
        disabled={offset + 50 >= total}
        onClick={() => onChange(offset + 50)}
      >
        Далее →
      </button>
    </div>
  );
}
export function Employees() {
  const user = useUser();
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const r = useResource(
    (signal) => api.employees(search, offset, signal),
    `${search}:${offset}`,
  );
  return (
    <>
      <PageTitle
        title="Сотрудники"
        description="Команда организации и голосовые профили участников совещаний."
        action={
          user.role === "admin" && (
            <Link className="button" to="/employees/new">
              ＋ Добавить сотрудника
            </Link>
          )
        }
      />
      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          setSearch(q);
          setOffset(0);
        }}
      >
        <input
          aria-label="Поиск сотрудников"
          placeholder="Поиск по имени, должности, департаменту"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button className="secondary">Найти</button>
      </form>
      <ResourceState resource={r} />
      {r.data && (
        <section className="panel flush">
          <div className="panel-head">
            <h2>Справочник сотрудников</h2>
            <Badge>{r.data.total} в справочнике</Badge>
          </div>
          {r.data.items.length ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Сотрудник</th>
                    <th>Департамент</th>
                    <th>Голосовой профиль</th>
                    <th>Учётная запись</th>
                  </tr>
                </thead>
                <tbody>
                  {r.data.items.map((e) => (
                    <tr key={e.id}>
                      <td>
                        <div className="cell-person">
                          <span className="avatar">{e.fio.slice(0, 1)}</span>
                          <div>
                            <Link to={`/employees/${e.id}`}>
                              <strong>{e.fio}</strong>
                            </Link>
                            <small>{e.position}</small>
                          </div>
                        </div>
                      </td>
                      <td>{e.department}</td>
                      <td>
                        <Badge
                          tone={
                            e.voice_profile.status === "ok"
                              ? "success"
                              : e.voice_profile.status === "none"
                                ? "neutral"
                                : "warning"
                          }
                        >
                          {voiceLabels[e.voice_profile.status]}
                        </Badge>
                      </td>
                      <td>{e.has_account ? "Есть доступ" : "Без аккаунта"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title={search ? "Сотрудники не найдены" : "Справочник пока пуст"}
            >
              {search
                ? "Попробуйте изменить поисковый запрос."
                : "Добавьте сотрудников, чтобы приглашать их на совещания."}
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
export function EmployeeDetail() {
  const { id } = useParams();
  const user = useUser();
  const r = useResource(
    (signal) => (id ? api.employee(id, signal) : Promise.resolve(null)),
    id || "new",
  );
  if (!id && user.role !== "admin")
    return (
      <Notice kind="error">
        Создавать сотрудников может только администратор.
      </Notice>
    );
  return (
    <>
      <Back to="/employees">Сотрудники</Back>
      <ResourceState resource={r} />
      {!r.loading && !r.error && (
        <EmployeeForm
          key={id || "new"}
          employee={r.data || null}
          onUpdate={(e) => r.setData(e)}
        />
      )}
    </>
  );
}
function EmployeeForm({
  employee,
  onUpdate,
}: {
  employee: Employee | null;
  onUpdate: (e: Employee) => void;
}) {
  const user = useUser();
  const navigate = useNavigate();
  const action = useAction();
  const [form, setForm] = useState<EmployeeInput>({
    fio: employee?.fio || "",
    position: employee?.position || "",
    department: employee?.department || "",
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    void action.run(async () => {
      const saved = await api.saveEmployee(employee?.id, form);
      onUpdate(saved);
      if (!employee) navigate(`/employees/${saved.id}`, { replace: true });
    });
  }
  return (
    <>
      <PageTitle
        title={employee?.fio || "Новый сотрудник"}
        description={
          employee
            ? `${employee.position} · ${employee.department}`
            : "Добавьте сотрудника в справочник организации."
        }
      />
      <div className="grid-2">
        <section className="panel">
          <h2>Основные сведения</h2>
          <form onSubmit={submit}>
            {(
              [
                ["fio", "ФИО"],
                ["position", "Должность"],
                ["department", "Департамент"],
              ] as const
            ).map(([key, label]) => (
              <Field key={key} label={label}>
                <input
                  required
                  maxLength={255}
                  value={form[key]}
                  disabled={user.role !== "admin" || action.busy}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                />
              </Field>
            ))}
            <ActionStatus action={action} />
            {user.role === "admin" && (
              <button disabled={action.busy}>
                {action.busy
                  ? "Сохраняем…"
                  : employee
                    ? "Сохранить изменения"
                    : "Создать сотрудника"}
              </button>
            )}
          </form>
          {employee && !employee.has_account && (
            <Notice>
              У сотрудника нет учётной записи. Участие в совещаниях доступно,
              персональные уведомления появятся после создания аккаунта
              администратором.
            </Notice>
          )}
        </section>
        {employee && <VoicePanel employee={employee} onUpdate={onUpdate} />}
      </div>
    </>
  );
}
function VoicePanel({
  employee,
  onUpdate,
}: {
  employee: Employee;
  onUpdate: (e: Employee) => void;
}) {
  const user = useUser();
  const allowed = user.role === "admin" || user.employee?.id === employee.id;
  const [file, setFile] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);
  const [recording, setRecording] = useState(false);
  const [progress, setProgress] = useState(0);
  const [remove, setRemove] = useState(false);
  const [quality, setQuality] = useState("");
  const action = useAction();
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function enroll() {
    if (!file || !consent) return;
    setProgress(0);
    controller.current = new AbortController();
    const result = await upload<{ quality_status: string; reasons: string[] }>(
      `/employees/${employee.id}/voice`,
      file,
      setProgress,
      controller.current.signal,
      true,
    );
    setQuality(
      result.quality_status === "needs_review"
        ? "Профиль сохранён с замечаниями к качеству. Рекомендуется записать образец заново."
        : "",
    );
    onUpdate(await api.employee(employee.id));
    setFile(null);
    setConsent(false);
  }
  return (
    <section className="panel">
      <h2>Голосовой профиль</h2>
      <Badge
        tone={employee.voice_profile.status === "ok" ? "success" : "warning"}
      >
        {voiceLabels[employee.voice_profile.status]}
      </Badge>
      <p style={{ marginTop: 15, fontSize: 12 }}>
        Образец нужен для сопоставления голоса с участником совещания. Это не
        способ входа. Запись обрабатывается локально; исходный образец удаляется
        после построения профиля.
      </p>
      {employee.voice_profile.created_at && (
        <p>
          <small>
            Обновлён {dateTime(employee.voice_profile.created_at)} · Чистой
            речи: {employee.voice_profile.speech_seconds?.toFixed(1)} с
          </small>
        </p>
      )}
      {quality && <Notice kind="warning">{quality}</Notice>}
      {allowed ? (
        <>
          <p style={{ fontSize: 12 }}>
            Запишите 20–30 секунд спокойной речи одного человека, без музыки и
            посторонних голосов.
          </p>
          <label className="check">
            <input
              type="checkbox"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
              disabled={action.busy || recording}
            />
            Сотрудник уведомлён и согласен на обработку голосового профиля.
          </label>
          <VoiceRecorder
            disabled={action.busy || !consent}
            onFile={setFile}
            onRecordingChange={setRecording}
          />
          <Field label="Или загрузите образец">
            <input
              type="file"
              accept="audio/*,.webm,.m4a"
              disabled={action.busy || recording}
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </Field>
          {file && (
            <p className="filesize">
              {file.name} · {(file.size / 1024 / 1024).toFixed(1)} МБ
            </p>
          )}
          {action.busy && (
            <progress
              aria-label="Загрузка образца"
              value={progress}
              max={100}
            />
          )}
          <ActionStatus action={action} />
          <div className="form-actions">
            <button
              disabled={!file || !consent || action.busy || recording}
              onClick={() =>
                void action.run(enroll, "Голосовой профиль обновлён")
              }
            >
              {action.busy
                ? "Обрабатываем образец…"
                : employee.voice_profile.status === "none"
                  ? "Зарегистрировать голос"
                  : "Перерегистрировать голос"}
            </button>
            {employee.voice_profile.status !== "none" && (
              <button
                className="danger"
                disabled={action.busy || recording}
                onClick={() => setRemove(true)}
              >
                Удалить профиль
              </button>
            )}
          </div>
          {remove && (
            <Notice kind="warning">
              Удалить голосовой профиль? Для повторного распознавания
              понадобится новый образец.
              <div className="form-actions">
                <button
                  className="danger"
                  disabled={action.busy || recording}
                  onClick={() =>
                    void action.run(async () => {
                      await api.deleteVoice(employee.id);
                      onUpdate(await api.employee(employee.id));
                      setRemove(false);
                    }, "Голосовой профиль удалён")
                  }
                >
                  Подтвердить удаление
                </button>
                <button className="secondary" onClick={() => setRemove(false)}>
                  Отмена
                </button>
              </div>
            </Notice>
          )}
        </>
      ) : (
        <Notice>
          Управлять профилем может сам сотрудник или администратор.
        </Notice>
      )}
    </section>
  );
}
