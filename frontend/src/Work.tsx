import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import {
  ActionStatus,
  Badge,
  Empty,
  PageTitle,
  ResourceState,
  useAction,
  useResource,
} from "./ui";
import { Pagination } from "./Employees";
import { TaskCard } from "./Tasks";
import { dateTime } from "./utils";
export function MyTasks() {
  const [filter, setFilter] = useState("all");
  const [offset, setOffset] = useState(0);
  const r = useResource(
    (signal) => api.tasks(filter, offset, signal),
    `${filter}:${offset}`,
  );
  return (
    <>
      <PageTitle
        title="Мои поручения"
        description="Подтверждённые решения совещаний, за исполнение которых вы отвечаете."
      />
      <div className="toolbar">
        <label htmlFor="task-filter">Показывать</label>
        <select
          id="task-filter"
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setOffset(0);
          }}
        >
          <option value="all">Все поручения</option>
          <option value="in_progress">В работе</option>
          <option value="overdue">Просроченные</option>
          <option value="completed">Выполненные</option>
        </select>
        <button className="text" onClick={r.reload}>
          Обновить
        </button>
      </div>
      <ResourceState resource={r} />
      {r.data && (
        <>
          {r.data.items.length ? (
            r.data.items.map((t) => (
              <div key={t.id}>
                <div style={{ margin: "18px 0 9px" }}>
                  <Link to={`/meetings/${t.meeting_id}`}>
                    <small>{t.meeting_title} ↗</small>
                  </Link>
                </div>
                <TaskCard
                  task={t}
                  canExecute
                  onSaved={async () => {
                    r.setData(await api.tasks(filter, offset));
                  }}
                />
              </div>
            ))
          ) : (
            <section className="panel">
              <Empty
                title={
                  filter === "all"
                    ? "Поручений пока нет"
                    : "Нет поручений с этим статусом"
                }
              >
                Поручения появляются после утверждения протокола секретарём.
              </Empty>
            </section>
          )}
          <Pagination
            offset={offset}
            total={r.data.total}
            onChange={setOffset}
          />
        </>
      )}
    </>
  );
}
const eventLabels: Record<string, string> = {
  meeting_invitation: "Приглашение",
  protocol_ready: "Протокол готов",
  processing_failed: "Ошибка обработки",
  task_assigned: "Назначение",
  deadline_soon: "Скоро срок",
  task_overdue: "Просрочка",
};
export function Notifications() {
  const [unread, setUnread] = useState(false);
  const [offset, setOffset] = useState(0);
  const r = useResource(
    (signal) => api.notifications(unread, offset, signal),
    `${unread}:${offset}`,
    30000,
  );
  const action = useAction();
  async function refresh() {
    r.setData(await api.notifications(unread, offset));
  }
  return (
    <>
      <PageTitle
        title="Уведомления"
        description="Приглашения, новые поручения и напоминания о сроках. Обновляются каждые 30 секунд."
        action={
          <button
            className="secondary"
            disabled={action.busy || !r.data?.unread_count}
            onClick={() =>
              void action.run(async () => {
                await api.readAll();
                await refresh();
              }, "Все уведомления прочитаны")
            }
          >
            Прочитать все
          </button>
        }
      />
      <div className="toolbar">
        <label className="check">
          <input
            type="checkbox"
            checked={unread}
            onChange={(e) => {
              setUnread(e.target.checked);
              setOffset(0);
            }}
          />
          Только непрочитанные
        </label>
        {r.data && <Badge>{r.data.unread_count} непрочитанных</Badge>}
        <button className="text" onClick={r.reload}>
          Обновить
        </button>
      </div>
      <ActionStatus action={action} />
      <ResourceState resource={r} />
      {r.data && (
        <section className="panel flush">
          {r.data.items.length ? (
            r.data.items.map((n) => (
              <article
                key={n.id}
                className={`notification ${n.read_at ? "" : "unread"}`}
              >
                <div>
                  <div className="toolbar" style={{ marginBottom: 8 }}>
                    <Badge
                      tone={
                        n.event_type === "task_overdue" ||
                        n.event_type === "processing_failed"
                          ? "error"
                          : n.event_type === "deadline_soon"
                            ? "warning"
                            : "neutral"
                      }
                    >
                      {eventLabels[n.event_type] || n.event_type}
                    </Badge>
                    <small>{dateTime(n.created_at)}</small>
                    {!n.read_at && <small>● Непрочитано</small>}
                  </div>
                  <h3>{n.title}</h3>
                  <p>{n.message}</p>
                  {n.meeting_id ? (
                    <Link
                      to={`/meetings/${n.meeting_id}${n.task_id ? `#task-${n.task_id}` : ""}`}
                    >
                      Открыть {n.task_id ? "поручение" : "совещание"} →
                    </Link>
                  ) : n.task_id ? (
                    <Link to="/tasks">Мои поручения →</Link>
                  ) : null}
                </div>
                <button
                  className="text"
                  style={{ alignSelf: "center", whiteSpace: "nowrap" }}
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(
                      async () => {
                        await api.readNotification(n.id, !n.read_at);
                        await refresh();
                      },
                      n.read_at
                        ? "Отмечено непрочитанным"
                        : "Отмечено прочитанным",
                    )
                  }
                >
                  {n.read_at ? "Не прочитано" : "Прочитано ✓"}
                </button>
              </article>
            ))
          ) : (
            <Empty title={unread ? "Всё прочитано" : "Уведомлений пока нет"}>
              Здесь будут приглашения на встречи и изменения по вашим
              поручениям.
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
