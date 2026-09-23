import { useEffect, useRef, useState, type FormEvent } from "react";
import { ActionStatus, Field, Notice, useAction } from "./ui";
import type { Role } from "./types";

export function AccountFields({
  login,
  role,
  onLogin,
  onRole,
  disabled = false,
}: {
  login: string;
  role: Role;
  onLogin: (v: string) => void;
  onRole: (v: Role) => void;
  disabled?: boolean;
}) {
  return (
    <>
      <Field
        label="Логин сотрудника"
        hint="3–64 символа: латинские буквы, цифры, точка, дефис, подчёркивание."
      >
        <input
          required
          pattern="[A-Za-z0-9._-]{3,64}"
          minLength={3}
          maxLength={64}
          autoComplete="off"
          value={login}
          disabled={disabled}
          onChange={(e) => onLogin(e.target.value)}
        />
      </Field>
      <Field label="Роль учётной записи">
        <select
          value={role}
          disabled={disabled}
          onChange={(e) => onRole(e.target.value as Role)}
        >
          <option value="employee">Сотрудник</option>
          <option value="secretary">Секретарь</option>
          <option value="admin">Администратор</option>
        </select>
      </Field>
    </>
  );
}

// The password exists only in this mounted dialog. No credentials enter router/global state.
export function AccessDialog({
  issue,
  onIssued,
  onClose,
  reset = false,
}: {
  issue: () => Promise<{ login: string; password: string; userId: string }>;
  onIssued: (userId: string) => void;
  onClose: () => void;
  reset?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [credentials, setCredentials] = useState<{
    login: string;
    password: string;
  } | null>(null);
  const action = useAction();
  const copy = useAction();
  const started = useRef(false);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    dialog.current?.showModal();
    if (!started.current) {
      started.current = true;
      void issueNow();
    }
    return () => {
      active.current = false;
    };
  }, []);
  async function issueNow() {
    await action.run(async () => {
      const result = await issue();
      if (active.current) {
        setCredentials(result);
        onIssued(result.userId);
      }
    }, "");
  }
  function close() {
    if (action.busy) return;
    setCredentials(null);
    dialog.current?.close();
    onClose();
  }
  return (
    <dialog
      ref={dialog}
      className="access-dialog"
      aria-labelledby="access-title"
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
    >
      <h2 id="access-title">{reset ? "Сброс пароля" : "Доступ сотрудника"}</h2>
      {action.busy && (
        <Notice>Выдаём временный пароль… Не закрывайте страницу.</Notice>
      )}
      <ActionStatus action={action} />
      {credentials ? (
        <>
          <Notice kind="warning">
            Передайте эти данные сотруднику. После закрытия временный пароль
            нельзя посмотреть повторно. При необходимости администратор может
            сбросить пароль.
          </Notice>
          <Field label="Выданный логин">
            <input readOnly autoComplete="off" value={credentials.login} />
          </Field>
          <Field label="Временный пароль">
            <input readOnly autoComplete="off" value={credentials.password} />
          </Field>
          <p>При первом входе сотрудник должен задать собственный пароль.</p>
          <button
            className="secondary"
            onClick={() =>
              void copy.run(async () => {
                if (!navigator.clipboard)
                  throw new Error(
                    "Копирование недоступно. Выделите и скопируйте данные вручную.",
                  );
                await navigator.clipboard.writeText(
                  `Логин: ${credentials.login}\nВременный пароль: ${credentials.password}`,
                );
              }, "Данные скопированы. Передайте их сотруднику.")
            }
          >
            Скопировать данные
          </button>
          <ActionStatus action={copy} />
        </>
      ) : (
        action.error && (
          <>
            <p>
              Сотрудник уже создан. Повторится только выдача доступа или сброс
              пароля.
            </p>
            <button disabled={action.busy} onClick={() => void issueNow()}>
              Повторить выдачу доступа
            </button>
          </>
        )
      )}
      <div className="form-actions">
        <button className="secondary" disabled={action.busy} onClick={close}>
          Закрыть
        </button>
      </div>
    </dialog>
  );
}

export function PasswordChange({
  change,
  onLogout,
}: {
  change: (current: string, next: string) => Promise<void>;
  onLogout: () => Promise<void>;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const action = useAction();
  const logout = useAction();
  function submit(e: FormEvent) {
    e.preventDefault();
    void action.run(async () => {
      if (next !== repeat) throw new Error("Новые пароли не совпадают.");
      if (next === current)
        throw new Error("Новый пароль должен отличаться от временного.");
      await change(current, next);
      setCurrent("");
      setNext("");
      setRepeat("");
    }, "");
  }
  return (
    <section className="login-form" style={{ minHeight: "100vh" }}>
      <div>
        <div className="eyebrow">ЗАЩИТА УЧЁТНОЙ ЗАПИСИ</div>
        <h1>Задайте новый пароль</h1>
        <p>
          Вы вошли с временным паролем. Чтобы открыть рабочие разделы, замените
          его своим. Голос не используется для входа.
        </p>
        <form onSubmit={submit}>
          <fieldset
            disabled={action.busy || logout.busy}
            style={{ border: 0, padding: 0 }}
          >
            <Field label="Текущий временный пароль">
              <input
                type="password"
                required
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </Field>
            <Field label="Новый пароль" hint="Не менее 10 символов.">
              <input
                type="password"
                required
                minLength={10}
                maxLength={256}
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            </Field>
            <Field label="Повторите новый пароль">
              <input
                type="password"
                required
                autoComplete="new-password"
                value={repeat}
                onChange={(e) => setRepeat(e.target.value)}
              />
            </Field>
            <ActionStatus action={action} />
            <button type="submit">
              {action.busy ? "Сохраняем…" : "Сменить пароль"}
            </button>
          </fieldset>
        </form>
        <button
          className="text"
          disabled={action.busy || logout.busy}
          onClick={() => void logout.run(onLogout, "")}
        >
          Выйти
        </button>
        <ActionStatus action={logout} />
      </div>
    </section>
  );
}
