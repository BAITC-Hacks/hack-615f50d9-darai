import {
  createContext,
  useContext,
  useEffect,
  useState,
  type FormEvent,
} from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { api, ApiError, setCsrf } from "./api";
import type { User } from "./types";
import {
  ActionStatus,
  ErrorState,
  Loading,
  useAction,
  useResource,
} from "./ui";
import { Employees, EmployeeDetail } from "./Employees";
import { Meetings, NewMeeting, MeetingDetail } from "./Meetings";
import { MyTasks, Notifications } from "./Work";
const AuthContext = createContext<User | null>(null);
export function useUser() {
  return useContext(AuthContext)!;
}
const roles = {
  admin: "Администратор",
  secretary: "Секретарь",
  employee: "Сотрудник",
};
function Brand() {
  return (
    <Link to="/meetings" className="brand">
      <span className="brand-mark">д</span>
      <span>
        <strong>DARAI</strong>
        <small>СОВЕЩАНИЯ И РЕШЕНИЯ</small>
      </span>
    </Link>
  );
}
function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const action = useAction();
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  function submit(e: FormEvent) {
    e.preventDefault();
    void action.run(async () => {
      const r = await api.login(login, password);
      setCsrf(r.csrf_token);
      setPassword("");
      onLogin(r.user);
    }, "");
  }
  return (
    <div className="login">
      <section className="login-story">
        <Brand />
        <h1>
          От обсуждения —<br />к решению.
        </h1>
        <p>
          Совещания, сотрудники и поручения в едином рабочем пространстве вашей
          организации.
        </p>
        <footer>
          <span className="dot" />
          Локальная обработка · Закрытый контур
        </footer>
      </section>
      <section className="login-form">
        <div>
          <div className="eyebrow">ДОБРО ПОЖАЛОВАТЬ</div>
          <h1>Вход в DARAI</h1>
          <p>Используйте локальную учётную запись, выданную администратором.</p>
          <form onSubmit={submit}>
            <label className="field">
              <span>Логин</span>
              <input
                autoComplete="username"
                required
                value={login}
                onChange={(e) => setLogin(e.target.value)}
                autoFocus
              />
            </label>
            <label className="field">
              <span>Пароль</span>
              <input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            <ActionStatus action={action} />
            <button type="submit" disabled={action.busy}>
              {action.busy ? "Входим…" : "Войти в рабочее пространство →"}
            </button>
          </form>
          <p style={{ marginTop: 24, fontSize: 11 }}>
            Голосовой профиль используется для распознавания участников
            совещаний. Для входа нужен пароль.
          </p>
        </div>
      </section>
    </div>
  );
}
function Shell({ user, onLogout }: { user: User; onLogout: () => void }) {
  const action = useAction();
  const location = useLocation();
  const unread = useResource(
    (signal) => api.notifications(true, 0, signal, 1),
    "unread",
    30000,
  );
  useEffect(() => {
    document.querySelector<HTMLElement>("#main")?.focus();
  }, [location.pathname]);
  return (
    <AuthContext.Provider value={user}>
      <a href="#main" className="skip">
        Перейти к содержимому
      </a>
      <div className="shell">
        <aside className="sidebar">
          <Brand />
          <div className="nav-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
          <nav aria-label="Основная навигация">
            <NavLink to="/meetings">
              <span className="nav-icon" aria-hidden>
                ▦
              </span>
              Совещания
            </NavLink>
            <NavLink to="/employees">
              <span className="nav-icon" aria-hidden>
                ♙
              </span>
              Сотрудники
            </NavLink>
            <NavLink to="/tasks">
              <span className="nav-icon" aria-hidden>
                ☑
              </span>
              Мои поручения
            </NavLink>
            <NavLink to="/notifications">
              <span className="nav-icon" aria-hidden>
                ♧
              </span>
              Уведомления
              {(unread.data?.unread_count ?? 0) > 0 && (
                <span className="nav-count">{unread.data?.unread_count}</span>
              )}
            </NavLink>
          </nav>
          <div className="sidebar-footer">
            <span className="dot" />
            Закрытый контур
            <br />
            <span>Данные остаются в организации</span>
            {!!unread.error && <div>Уведомления временно недоступны</div>}
          </div>
        </aside>
        <div className="workspace">
          <header className="topbar">
            <span className="topbar-title">
              Внутренняя CRM / Рабочее пространство
            </span>
            <div className="user">
              <span className="avatar">
                {(user.employee?.fio || user.login)
                  .slice(0, 2)
                  .toLocaleUpperCase("ru")}
              </span>
              <div>
                <div className="user-name">
                  {user.employee?.fio || user.login}
                </div>
                <div className="user-role">{roles[user.role]}</div>
              </div>
              <button
                className="text"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await api.logout();
                    setCsrf("");
                    onLogout();
                  }, "")
                }
              >
                Выйти
              </button>
            </div>
          </header>
          <main id="main" className="content" tabIndex={-1}>
            <ActionStatus action={action} />
            <Routes>
              <Route path="/" element={<Navigate to="/meetings" replace />} />
              <Route path="/meetings" element={<Meetings />} />
              <Route path="/meetings/new" element={<NewMeeting />} />
              <Route path="/meetings/:id" element={<MeetingDetail />} />
              <Route path="/employees" element={<Employees />} />
              <Route path="/employees/new" element={<EmployeeDetail />} />
              <Route path="/employees/:id" element={<EmployeeDetail />} />
              <Route path="/tasks" element={<MyTasks />} />
              <Route path="/notifications" element={<Notifications />} />
              <Route
                path="*"
                element={
                  <div className="panel">
                    <h1>Страница не найдена</h1>
                    <Link to="/meetings">Перейти к совещаниям</Link>
                  </div>
                }
              />
            </Routes>
          </main>
        </div>
      </div>
    </AuthContext.Provider>
  );
}
export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    api
      .me(controller.signal)
      .then((u) => {
        setCsrf(u.csrf_token);
        setUser(u);
      })
      .catch((e) => {
        if (
          e.name !== "AbortError" &&
          !(e instanceof ApiError && e.status === 401)
        )
          setError(e);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => {
    const expire = () => {
      setUser(null);
      setCsrf("");
    };
    window.addEventListener("darai:unauthorized", expire);
    return () => window.removeEventListener("darai:unauthorized", expire);
  }, []);
  return (
    <BrowserRouter>
      {loading ? (
        <Loading />
      ) : error ? (
        <div className="login-form" style={{ minHeight: "100vh" }}>
          <div>
            <h1>DARAI</h1>
            <ErrorState error={error} retry={() => setAttempt((a) => a + 1)} />
          </div>
        </div>
      ) : user ? (
        <Shell user={user} onLogout={() => setUser(null)} />
      ) : (
        <Login onLogin={setUser} />
      )}
    </BrowserRouter>
  );
}
