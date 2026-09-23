import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import type { Participant, Speaker, Task, TaskInput, Utterance } from "./types";
import { ActionStatus, Badge, Field, Notice, useAction } from "./ui";
import { day, timecode } from "./utils";
export function TaskCard({
  task,
  editable = false,
  participants = [],
  speakers = [],
  utterances = [],
  onSaved,
  onSeek,
  onDirty,
  canExecute = false,
}: {
  task: Task;
  editable?: boolean;
  participants?: Participant[];
  speakers?: Speaker[];
  utterances?: Utterance[];
  onSaved?: () => Promise<void>;
  onSeek?: (seconds: number) => void;
  onDirty?: (key: string, dirty: boolean) => void;
  canExecute?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const action = useAction();
  const sources = utterances.filter((u) =>
    task.source_utterance_ids.includes(u.id),
  );
  return (
    <article className="task-card" id={`task-${task.id}`}>
      <div className="task-top">
        <div>
          <h3>{task.task}</h3>
          <div className="task-meta">
            <span>
              Поручил: {task.from_fio || task.from || "Требует уточнения"}
            </span>
            <span>Исполнитель: {task.to_fio || "Требует уточнения"}</span>
            <span>Срок: {day(task.deadline)}</span>
          </div>
        </div>
        <Badge
          tone={
            task.status === "draft"
              ? "warning"
              : task.execution_status === "completed"
                ? "success"
                : task.overdue
                  ? "error"
                  : "info"
          }
        >
          {task.status === "draft"
            ? "Черновик"
            : task.execution_status === "completed"
              ? "Выполнено"
              : task.overdue
                ? "Просрочено"
                : "В работе"}
        </Badge>
      </div>
      {task.deadline_source && (
        <small>Срок в записи: «{task.deadline_source}»</small>
      )}
      {(!task.to || !task.deadline || task.needs_review) && (
        <Notice kind="warning">
          {!task.to ? "Исполнитель требует уточнения. " : ""}
          {!task.deadline ? "Срок требует уточнения. " : ""}
          {task.needs_review
            ? "Проверьте содержание и источник поручения."
            : ""}
        </Notice>
      )}
      {task.evidence ? (
        <blockquote className="quote">«{task.evidence}»</blockquote>
      ) : (
        <p>
          <small>Подтверждающая цитата не указана.</small>
        </p>
      )}
      <div className="toolbar">
        {onSeek ? (
          sources.map((u) => (
            <button
              type="button"
              className="timestamp"
              key={u.id}
              onClick={() => onSeek(u.start)}
            >
              ▶ {timecode(u.start)} — фрагмент
            </button>
          ))
        ) : (
          <Link to={`/meetings/${task.meeting_id}#task-${task.id}`}>
            Открыть в протоколе →
          </Link>
        )}
        {task.confidence !== null && (
          <small>
            Оценка модели: {task.confidence.toFixed(2)} · не гарантия точности
          </small>
        )}
      </div>
      {editable && !editing && (
        <div className="form-actions">
          <button className="secondary" onClick={() => setEditing(true)}>
            Редактировать поручение
          </button>
          <button className="text" onClick={() => setRemoving(true)}>
            Удалить
          </button>
        </div>
      )}
      {editing && (
        <TaskForm
          initial={task}
          meetingId={task.meeting_id}
          participants={participants}
          speakers={speakers}
          utterances={utterances}
          onDirty={(dirty) => onDirty?.(task.id, dirty)}
          onSaved={async () => {
            await onSaved?.();
            setEditing(false);
            onDirty?.(task.id, false);
          }}
          onCancel={() => {
            setEditing(false);
            onDirty?.(task.id, false);
          }}
        />
      )}
      {removing && (
        <Notice kind="warning">
          Удалить это поручение из черновика?
          <div className="form-actions">
            <button
              className="danger"
              disabled={action.busy}
              onClick={() =>
                void action.run(async () => {
                  await api.deleteTask(task.id);
                  await onSaved?.();
                  setRemoving(false);
                }, "Поручение удалено")
              }
            >
              Подтвердить удаление
            </button>
            <button className="secondary" onClick={() => setRemoving(false)}>
              Отмена
            </button>
          </div>
        </Notice>
      )}
      {canExecute && task.status === "confirmed" && (
        <button
          className="secondary"
          disabled={action.busy}
          onClick={() =>
            void action.run(async () => {
              await api.execute(
                task.id,
                task.execution_status === "completed"
                  ? "in_progress"
                  : "completed",
              );
              await onSaved?.();
            }, "Статус выполнения сохранён")
          }
        >
          {task.execution_status === "completed"
            ? "Вернуть в работу"
            : "Отметить выполненным"}
        </button>
      )}
      <ActionStatus action={action} />
    </article>
  );
}
export function TaskForm({
  initial,
  meetingId,
  participants,
  speakers,
  utterances,
  onSaved,
  onCancel,
  onDirty,
}: {
  initial?: Task;
  meetingId: string;
  participants: Participant[];
  speakers: Speaker[];
  utterances: Utterance[];
  onSaved: () => Promise<void>;
  onCancel: () => void;
  onDirty?: (dirty: boolean) => void;
}) {
  const [form, setForm] = useState<TaskInput>({
    task: initial?.task || "",
    from: initial?.from || null,
    to: initial?.to || null,
    deadline: initial?.deadline || null,
    deadline_source: initial?.deadline_source || null,
    evidence: initial?.evidence || null,
    source_utterance_ids: initial?.source_utterance_ids || [],
  });
  const action = useAction();
  function update<K extends keyof TaskInput>(key: K, value: TaskInput[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    onDirty?.(true);
  }
  function submit(e: FormEvent) {
    e.preventDefault();
    void action.run(async () => {
      await api.saveTask(meetingId, initial?.id, form);
      onDirty?.(false);
      await onSaved();
    });
  }
  return (
    <form className="task-editor" onSubmit={submit}>
      <fieldset
        disabled={action.busy}
        style={{ border: 0, padding: 0, margin: 0 }}
      >
        <Field label="Что нужно сделать">
          <textarea
            required
            value={form.task}
            onChange={(e) => update("task", e.target.value)}
          />
        </Field>
        <div className="grid-2">
          <Field label="Кто поручил">
            <select
              value={form.from || ""}
              onChange={(e) => update("from", e.target.value || null)}
            >
              <option value="">Требует уточнения</option>
              {participants.map((p) => (
                <option key={p.employee_id} value={p.employee_id}>
                  {p.fio}
                </option>
              ))}
              {speakers.map((s) => (
                <option key={s.label} value={s.label}>
                  {s.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Кому поручено">
            <select
              value={form.to || ""}
              onChange={(e) => update("to", e.target.value || null)}
            >
              <option value="">Требует уточнения</option>
              {participants.map((p) => (
                <option key={p.employee_id} value={p.employee_id}>
                  {p.fio}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Срок исполнения">
            <input
              type="date"
              value={form.deadline || ""}
              onChange={(e) => update("deadline", e.target.value || null)}
            />
          </Field>
          <Field label="Исходная формулировка срока">
            <input
              value={form.deadline_source || ""}
              onChange={(e) =>
                update("deadline_source", e.target.value || null)
              }
            />
          </Field>
        </div>
        <Field label="Подтверждающая цитата">
          <textarea
            value={form.evidence || ""}
            onChange={(e) => update("evidence", e.target.value || null)}
          />
        </Field>
        <Field
          label="Реплики-источники"
          hint="Удерживайте Ctrl / ⌘ для выбора нескольких реплик."
        >
          <select
            multiple
            value={form.source_utterance_ids.map(String)}
            onChange={(e) =>
              update(
                "source_utterance_ids",
                Array.from(e.target.selectedOptions, (o) => Number(o.value)),
              )
            }
          >
            {utterances.map((u) => (
              <option key={u.id} value={u.id}>
                {timecode(u.start)} · {u.text}
              </option>
            ))}
          </select>
        </Field>
        <ActionStatus action={action} />
        <div className="form-actions">
          <button>{action.busy ? "Сохраняем…" : "Сохранить поручение"}</button>
          <button type="button" className="secondary" onClick={onCancel}>
            Отмена
          </button>
        </div>
      </fieldset>
    </form>
  );
}
