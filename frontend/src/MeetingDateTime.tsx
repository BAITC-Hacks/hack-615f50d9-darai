import { useId, useRef, useState, type KeyboardEvent } from "react";

const pad = (n: number) => String(n).padStart(2, "0");
const dateKey = (d: Date) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDay = (value: string) => {
  const [y, m, d] = value.split("-").map(Number);
  return new Date(y, m - 1, d, 12);
};
const fullDate = (d: Date) =>
  d.toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "long",
    year: "numeric",
  });

export function MeetingDateTime({
  value,
  onChange,
  timezone,
}: {
  value: string;
  onChange: (value: string) => void;
  timezone: string;
}) {
  const id = useId(),
    titleId = useId(),
    hintId = useId();
  const dialog = useRef<HTMLDialogElement>(null),
    trigger = useRef<HTMLButtonElement>(null),
    container = useRef<HTMLDivElement>(null);
  const [day, setDay] = useState("");
  const [clock, setClock] = useState("09:00");
  const [month, setMonth] = useState(new Date());
  const [open, setOpen] = useState(false);
  function localNow() {
    try {
      const parts = new Intl.DateTimeFormat("sv-SE", {
        timeZone: timezone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      }).formatToParts(new Date());
      const part = (type: string) => parts.find((p) => p.type === type)?.value;
      return `${part("year")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}`;
    } catch {
      const now = new Date();
      return `${dateKey(now)}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    }
  }
  const today = localNow().slice(0, 10);
  function close() {
    dialog.current?.close();
    setOpen(false);
    trigger.current?.focus();
  }
  function show() {
    const initial = value || localNow();
    setDay(initial.slice(0, 10));
    setClock(initial.slice(11, 16));
    setMonth(parseDay(initial.slice(0, 10)));
    setOpen(true);
    dialog.current?.showModal();
    requestAnimationFrame(() => {
      const el = dialog.current,
        anchor = container.current;
      if (!el || !anchor) return;
      const rect = anchor.getBoundingClientRect();
      el.style.left = `${Math.max(16, Math.min(rect.left, window.innerWidth - el.offsetWidth - 16))}px`;
      el.style.top = `${Math.max(16, Math.min(rect.bottom + 8, window.innerHeight - el.offsetHeight - 16))}px`;
      el.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus();
    });
  }
  function moveMonth(delta: number) {
    setMonth(new Date(month.getFullYear(), month.getMonth() + delta, 1, 12));
  }
  function keyDay(e: KeyboardEvent<HTMLButtonElement>, date: Date) {
    let next = new Date(date);
    const offset: Record<string, number> = {
      ArrowLeft: -1,
      ArrowRight: 1,
      ArrowUp: -7,
      ArrowDown: 7,
    };
    if (e.key in offset) next.setDate(next.getDate() + offset[e.key]);
    else if (e.key === "Home")
      next.setDate(next.getDate() - ((next.getDay() + 6) % 7));
    else if (e.key === "End")
      next.setDate(next.getDate() + 6 - ((next.getDay() + 6) % 7));
    else return;
    e.preventDefault();
    setDay(dateKey(next));
    setMonth(next);
    requestAnimationFrame(() =>
      dialog.current
        ?.querySelector<HTMLButtonElement>(`[data-date="${dateKey(next)}"]`)
        ?.focus(),
    );
  }
  const start = new Date(month.getFullYear(), month.getMonth(), 1, 12);
  start.setDate(1 - ((start.getDay() + 6) % 7));
  const days = Array.from({ length: 42 }, (_, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    return d;
  });
  return (
    <div className="field">
      <label htmlFor={id}>Дата и время</label>
      <div className="meeting-date-input" ref={container}>
        <input
          id={id}
          type="datetime-local"
          required
          value={value}
          aria-describedby={hintId}
          onChange={(e) => onChange(e.target.value)}
          onClick={show}
          onKeyDown={(e) => {
            if (e.altKey && e.key === "ArrowDown") {
              e.preventDefault();
              show();
            }
          }}
        />
        <button
          type="button"
          className="calendar-trigger"
          ref={trigger}
          aria-label="Открыть календарь"
          aria-haspopup="dialog"
          aria-expanded={open}
          onClick={show}
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            aria-hidden="true"
          >
            <rect x="3" y="5" width="18" height="16" rx="3" />
            <path d="M7 3v4m10-4v4M3 11h18M7 15h2m3 0h2m3 0h1M7 18h2m3 0h2" />
          </svg>
        </button>
      </div>
      <small id={hintId}>В выбранном часовом поясе встречи.</small>
      <dialog
        ref={dialog}
        className="meeting-calendar"
        aria-labelledby={titleId}
        onCancel={(e) => {
          e.preventDefault();
          close();
        }}
        onClick={(e) => {
          if (e.target === e.currentTarget) {
            const r = e.currentTarget.getBoundingClientRect();
            if (
              e.clientX < r.left ||
              e.clientX > r.right ||
              e.clientY < r.top ||
              e.clientY > r.bottom
            )
              close();
          }
        }}
      >
        <div className="calendar-heading">
          <div>
            <small>ДАТА СОВЕЩАНИЯ</small>
            <h3 id={titleId}>Выберите дату и время</h3>
          </div>
          <button
            type="button"
            className="calendar-icon-button"
            aria-label="Закрыть календарь"
            onClick={close}
          >
            ×
          </button>
        </div>
        <div className="calendar-month">
          <strong aria-live="polite">
            {month.toLocaleDateString("ru-RU", {
              month: "long",
              year: "numeric",
            })}
          </strong>
          <div>
            <button
              type="button"
              className="calendar-icon-button"
              aria-label="Предыдущий месяц"
              onClick={() => moveMonth(-1)}
            >
              ‹
            </button>
            <button
              type="button"
              className="calendar-icon-button"
              aria-label="Следующий месяц"
              onClick={() => moveMonth(1)}
            >
              ›
            </button>
          </div>
        </div>
        <div className="calendar-weekdays" aria-hidden="true">
          {["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((d) => (
            <span key={d}>{d}</span>
          ))}
        </div>
        <div className="calendar-days" role="group" aria-label="Дни месяца">
          {days.map((d) => {
            const key = dateKey(d);
            return (
              <button
                type="button"
                key={key}
                data-date={key}
                aria-label={fullDate(d)}
                aria-pressed={key === day}
                aria-current={key === today ? "date" : undefined}
                tabIndex={
                  key === day ||
                  (!days.some((x) => dateKey(x) === day) && d.getDate() === 1)
                    ? 0
                    : -1
                }
                className={`${d.getMonth() !== month.getMonth() ? "outside-month " : ""}${key === today ? "is-today" : ""}`}
                onClick={() => {
                  setDay(key);
                  setMonth(d);
                }}
                onKeyDown={(e) => keyDay(e, d)}
              >
                {d.getDate()}
              </button>
            );
          })}
        </div>
        <div className="calendar-time">
          <div>
            <strong>Время начала</strong>
            <small>24-часовой формат</small>
          </div>
          <div className="calendar-clock">
            <select
              aria-label="Часы начала"
              value={clock.slice(0, 2)}
              onChange={(e) => setClock(`${e.target.value}:${clock.slice(3)}`)}
            >
              {Array.from({ length: 24 }, (_, i) => (
                <option key={i}>{pad(i)}</option>
              ))}
            </select>
            <span>:</span>
            <select
              aria-label="Минуты начала"
              value={clock.slice(3)}
              onChange={(e) =>
                setClock(`${clock.slice(0, 2)}:${e.target.value}`)
              }
            >
              {Array.from({ length: 60 }, (_, i) => (
                <option key={i}>{pad(i)}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="calendar-footer">
          <button
            type="button"
            className="calendar-today"
            onClick={() => {
              setDay(today);
              setMonth(parseDay(today));
            }}
          >
            Сегодня
          </button>
          <button
            type="button"
            onClick={() => {
              onChange(`${day}T${clock}`);
              close();
            }}
          >
            Выбрать дату
          </button>
        </div>
      </dialog>
    </div>
  );
}
