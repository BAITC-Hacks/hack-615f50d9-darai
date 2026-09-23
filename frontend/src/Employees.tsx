import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, upload } from "./api";
import type { Employee, EmployeeInput, Role } from "./types";
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
import { AccountFields, AccessDialog } from "./Access";
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
  const [wantAccount, setWantAccount] = useState(false);
  const [login, setLogin] = useState("");
  const [role, setRole] = useState<Role>("employee");
  const [issuing, setIssuing] = useState(false);
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
      if (!employee) {
        if (wantAccount) setIssuing(true);
        else navigate(`/employees/${saved.id}`, { replace: true });
      }
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
            {!employee && (
              <>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={wantAccount}
                    disabled={action.busy}
                    onChange={(e) => setWantAccount(e.target.checked)}
                  />
                  Создать учётную запись
                </label>
                {wantAccount && (
                  <AccountFields
                    login={login}
                    role={role}
                    onLogin={setLogin}
                    onRole={setRole}
                    disabled={action.busy}
                  />
                )}
              </>
            )}
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
        {employee && (
          <div>
            {user.role === "admin" && (
              <AccountPanel employee={employee} onUpdate={onUpdate} />
            )}
            <VoicePanel employee={employee} onUpdate={onUpdate} />
          </div>
        )}
        {issuing && employee && (
          <AccessDialog
            issue={() => api.issueAccount(employee.id, login, role)}
            onIssued={(userId) =>
              onUpdate({ ...employee, has_account: true, user_id: userId })
            }
            onClose={() => {
              setIssuing(false);
              navigate(`/employees/${employee.id}`, { replace: true });
            }}
          />
        )}
      </div>
    </>
  );
}
export function VoicePanel({
  employee,
  onUpdate,
}: {
  employee: Employee;
  onUpdate: (e: Employee) => void;
}) {
  const user = useUser();
  const allowed = user.employee?.id === employee.id;
  const [file, setFile] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);
  const [recording, setRecording] = useState(false);
  const [progress, setProgress] = useState(0);
  const [remove, setRemove] = useState(false);
  const [quality, setQuality] = useState("");
  const [preview, setPreview] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [deferred, setDeferred] = useState(false);
  useEffect(() => {
    setPreviewError("");
    if (!file) {
      setPreview("");
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  const action = useAction();
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function enroll() {
    if (!file || !consent) return;
    setProgress(0);
    controller.current = new AbortController();
    let result: { quality_status: string; reasons: string[] };
    try {
      result = await upload<{ quality_status: string; reasons: string[] }>(
        `/employees/${employee.id}/voice`,
        file,
        setProgress,
        controller.current.signal,
        true,
      );
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.code === "VOICE_QUALITY_REJECTED"
      ) {
        const reasons = error.details?.reasons as string[] | undefined;
        const tips: Record<string, string> = {
          too_short:
            "Прочитайте текст целиком: нужно 20–30 секунд чистой речи.",
          silence: "Проверьте микрофон и говорите отчётливо в тихом месте.",
          clipping: "Отодвиньтесь от микрофона или уменьшите его усиление.",
          inconsistent_voice: "В образце должен говорить только один человек.",
        };
        throw new Error(
          [
            error.message,
            ...(reasons || []).map(
              (r) => tips[r] || "Запишите чистую речь без посторонних звуков.",
            ),
          ].join(" "),
        );
      }
      if (error instanceof ApiError && error.code === "AUDIO_INVALID")
        throw new Error(
          "Не удалось прочитать аудио. Загрузите WAV, MP3, M4A или WebM с аудиодорожкой.",
        );
      throw error;
    }
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
          {employee.voice_profile.status === "none" && (
            <>
              <button
                className="text"
                disabled={recording || action.busy}
                onClick={() => setDeferred((v) => !v)}
              >
                {deferred
                  ? "Перейти к регистрации голоса"
                  : "Зарегистрировать позже"}
              </button>
              {deferred && (
                <Notice>
                  Вы можете работать без голосового профиля. Вернитесь в «Мой
                  профиль», когда будете готовы зарегистрировать голос.
                </Notice>
              )}
            </>
          )}
          <p className="reading-text">
            Текст для чтения: «Меня зовут {employee.fio}. Я участвую в рабочих
            совещаниях нашей команды. Сегодня мы обсуждаем планы, распределяем
            задачи и согласовываем сроки. Я говорю спокойно и отчётливо.
            Әріптестермен бірге жұмыс жоспарын талқылаймыз. Әр тапсырманың
            мақсаты мен орындалу мерзімін нақтылаймыз.»
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
          {preview && (
            <div>
              <label>
                Прослушайте образец перед отправкой
                <audio
                  className="voice-preview"
                  aria-label="Прослушать голосовой образец"
                  controls
                  src={preview}
                  onError={() =>
                    setPreviewError(
                      "Браузер не может воспроизвести этот формат. Выберите WAV, MP3 или запишите новый образец.",
                    )
                  }
                />
              </label>
              {previewError && <Notice kind="warning">{previewError}</Notice>}
              <button
                className="text"
                disabled={recording || action.busy}
                onClick={() => setFile(null)}
              >
                Удалить образец и записать заново
              </button>
            </div>
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
          Сотрудник регистрирует голос самостоятельно: выдайте ему доступ и
          предложите открыть «Мой профиль». Не записывайте свой голос вместо
          голоса сотрудника.
        </Notice>
      )}
    </section>
  );
}

function AccountPanel({
  employee,
  onUpdate,
}: {
  employee: Employee;
  onUpdate: (e: Employee) => void;
}) {
  const currentUser = useUser();
  const [login, setLogin] = useState("");
  const [role, setRole] = useState<Role>("employee");
  const [issue, setIssue] = useState(false);
  const [reset, setReset] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const r = useResource(
    (signal) =>
      employee.user_id
        ? api.account(employee.user_id, signal)
        : Promise.resolve(null),
    employee.user_id || "none",
  );
  return (
    <section className="panel">
      <h2>Доступ в CRM</h2>
      <Badge tone={employee.has_account ? "success" : "neutral"}>
        {employee.has_account ? "Учётная запись создана" : "Доступ не выдан"}
      </Badge>
      {employee.has_account ? (
        <>
          <ResourceState resource={r} />
          {r.data && (
            <p>
              Логин: {r.data.login}
              <br />
              Состояние: {r.data.active ? "Активна" : "Отключена"}
              <br />
              {r.data.must_change_password
                ? "Ожидается смена временного пароля"
                : "Пароль установлен сотрудником"}
            </p>
          )}
          <button
            className="secondary"
            disabled={!employee.user_id || employee.user_id === currentUser.id}
            onClick={() => setConfirm(true)}
          >
            Сбросить пароль
          </button>
          {employee.user_id === currentUser.id && (
            <p>
              <small>
                Сброс этой учётной записи завершит вашу сессию и закроет
                одноразовый пароль. Для её сброса обратитесь к другому
                администратору.
              </small>
            </p>
          )}
          {confirm && (
            <Notice kind="warning">
              Все текущие сессии сотрудника завершатся. Будет выдан новый
              временный пароль.
              <div className="form-actions">
                <button
                  onClick={() => {
                    setConfirm(false);
                    setReset(true);
                  }}
                >
                  Подтвердить сброс пароля
                </button>
                <button className="secondary" onClick={() => setConfirm(false)}>
                  Отмена
                </button>
              </div>
            </Notice>
          )}
        </>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setIssue(true);
          }}
        >
          <AccountFields
            login={login}
            role={role}
            onLogin={setLogin}
            onRole={setRole}
          />
          <button>Создать учётную запись</button>
        </form>
      )}
      {(issue || reset) && (
        <AccessDialog
          reset={reset}
          issue={() =>
            reset
              ? api.resetPassword(employee.user_id!)
              : api.issueAccount(employee.id, login, role)
          }
          onIssued={(userId) => {
            onUpdate({ ...employee, has_account: true, user_id: userId });
            r.reload();
          }}
          onClose={() => {
            setIssue(false);
            setReset(false);
          }}
        />
      )}
    </section>
  );
}
export function MyProfile() {
  const user = useUser();
  const r = useResource((signal) => api.myProfile(signal), "my-profile");
  return (
    <>
      <PageTitle
        title="Мой профиль"
        description="Ваши данные и голосовой профиль для участия в совещаниях."
      />
      {!user.employee ? (
        <Notice>У этой учётной записи нет профиля сотрудника.</Notice>
      ) : (
        <>
          <ResourceState resource={r} />
          {r.data && (
            <div className="grid-2">
              <section className="panel">
                <h2>{r.data.fio}</h2>
                <p>{r.data.position}</p>
                <p>{r.data.department}</p>
                <p>Логин: {r.data.login}</p>
                {r.data.voice_profile.status === "none" && (
                  <Notice>
                    Зарегистрируйте голос, чтобы система могла предложить ваше
                    имя в записи совещания. Это можно сделать позже.
                  </Notice>
                )}
              </section>
              <VoicePanel
                employee={r.data}
                onUpdate={(e) => r.setData({ ...e, login: r.data!.login })}
              />
            </div>
          )}
        </>
      )}
    </>
  );
}
