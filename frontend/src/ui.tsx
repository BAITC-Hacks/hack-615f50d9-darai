import {
  Children,
  cloneElement,
  isValidElement,
  useId,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type ReactElement,
} from "react";
import { Link } from "react-router-dom";
export function PageTitle({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-title">
      <div>
        <div className="eyebrow">{eyebrow || "РАБОЧЕЕ ПРОСТРАНСТВО"}</div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {action}
    </header>
  );
}
export function Notice({
  children,
  kind = "info",
}: {
  children: ReactNode;
  kind?: "info" | "error" | "success" | "warning";
}) {
  return (
    <div
      className={`notice ${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-mark" aria-hidden="true">
        ◇
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <span className="spinner" />
      Загружаем данные…
    </div>
  );
}
export function ErrorState({
  error,
  retry,
}: {
  error: unknown;
  retry: () => void;
}) {
  return (
    <Notice kind="error">
      <strong>Не удалось загрузить данные.</strong>
      <p>
        {error instanceof Error
          ? error.message
          : "Ошибка подключения к серверу."}
      </p>
      <button className="secondary" onClick={retry}>
        Повторить
      </button>
    </Notice>
  );
}
export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export function Back({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link className="back" to={to}>
      ← {children}
    </Link>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {Children.map(children, (child) =>
        isValidElement(child) &&
        ["input", "select", "textarea"].includes(String(child.type))
          ? cloneElement(
              child as ReactElement<{
                id: string;
                "aria-describedby"?: string;
              }>,
              { id, "aria-describedby": hint ? `${id}-hint` : undefined },
            )
          : child,
      )}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
    </div>
  );
}
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const lock = useRef(false);
  return {
    busy,
    error,
    success,
    run: async (fn: () => Promise<void>, message = "Сохранено") => {
      if (lock.current) return;
      lock.current = true;
      setBusy(true);
      setError("");
      setSuccess("");
      try {
        await fn();
        setSuccess(message);
      } catch (e) {
        setError(
          e instanceof Error ? e.message : "Не удалось выполнить действие",
        );
      } finally {
        lock.current = false;
        setBusy(false);
      }
    },
  };
}
export function ActionStatus({
  action,
}: {
  action: ReturnType<typeof useAction>;
}) {
  return (
    <>
      {action.error && <Notice kind="error">{action.error}</Notice>}
      {action.success && <Notice kind="success">{action.success}</Notice>}
    </>
  );
}
// A new request is scheduled only after the previous one settles. Cleanup aborts it.
export function useResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  key: string,
  pollMs = 0,
) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  const latest = useRef(loader);
  latest.current = loader;
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    setData(undefined);
    setLoading(true);
    setError(undefined);
    const run = async () => {
      try {
        const result = await latest.current(controller.signal);
        if (!disposed) {
          setData(result);
          setError(undefined);
        }
      } catch (e) {
        if (!disposed) setError(e);
      } finally {
        if (!disposed) {
          setLoading(false);
          if (pollMs) timer = setTimeout(run, pollMs);
        }
      }
    };
    void run();
    return () => {
      disposed = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [key, pollMs, version]);
  return {
    data,
    setData,
    error,
    loading,
    reload: () => setVersion((v) => v + 1),
  };
}
export function ResourceState({
  resource,
}: {
  resource: { loading: boolean; error: unknown; reload: () => void };
}) {
  return resource.loading ? (
    <Loading />
  ) : resource.error ? (
    <ErrorState error={resource.error} retry={resource.reload} />
  ) : null;
}
