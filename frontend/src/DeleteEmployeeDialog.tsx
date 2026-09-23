import { useEffect, useId, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { Employee } from "./types";
import { Notice } from "./ui";

export function DeleteEmployeeDialog({
  employee,
  onClose,
  onDeleted,
  onUnavailable,
}: {
  employee: Employee;
  onClose: () => void;
  onDeleted: () => void;
  onUnavailable: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const locked = useRef(false);
  const alive = useRef(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState(false);
  const titleId = useId();
  const descriptionId = useId();
  useEffect(() => {
    alive.current = true;
    if (!opener.current)
      opener.current = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    element?.showModal();
    cancel.current?.focus();
    return () => {
      alive.current = false;
      element?.close();
      requestAnimationFrame(() => {
        // React StrictMode can reopen the same dialog after effect cleanup.
        if (element?.isConnected && element.open) return;
        const target = opener.current?.isConnected
          ? opener.current
          : document.getElementById("main");
        target?.focus();
      });
    };
  }, []);
  function close() {
    if (!locked.current) onClose();
  }
  async function remove() {
    if (locked.current || unavailable) return;
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      await api.deleteEmployee(employee.id);
      if (alive.current) onDeleted();
    } catch (e) {
      if (!alive.current) return;
      if (e instanceof ApiError && e.status === 404) {
        setUnavailable(true);
        setError(
          "Сотрудник больше недоступен. Возможно, его уже удалили или изменились ваши права.",
        );
        onUnavailable();
      } else {
        const messages: Record<string, string> = {
          FORBIDDEN: "У вас нет прав на удаление этого сотрудника.",
          SELF_DELETE_FORBIDDEN:
            "Нельзя удалить собственную карточку сотрудника.",
          LAST_ADMIN: "Нельзя удалить последнего администратора системы.",
        };
        setError(
          e instanceof ApiError
            ? messages[e.code] || e.message
            : e instanceof Error
              ? e.message
              : "Не удалось удалить сотрудника. Попробуйте ещё раз.",
        );
      }
    } finally {
      locked.current = false;
      if (alive.current) setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="access-dialog"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      aria-busy={busy}
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
    >
      <h2 id={titleId}>Удалить сотрудника?</h2>
      <p id={descriptionId}>
        <strong>{employee.fio}</strong>
        <br />
        Сотрудник исчезнет из активного справочника и потеряет доступ к системе.
        История совещаний и поручений сохранится.
      </p>
      {error && <Notice kind="error">{error}</Notice>}
      {busy && <Notice>Удаляем сотрудника…</Notice>}
      <div className="form-actions">
        <button
          ref={cancel}
          type="button"
          className="secondary"
          disabled={busy}
          onClick={close}
        >
          {unavailable ? "Закрыть" : "Отмена"}
        </button>
        <button
          type="button"
          className="danger"
          disabled={busy || unavailable}
          onClick={() => void remove()}
        >
          Удалить сотрудника
        </button>
      </div>
    </dialog>
  );
}
