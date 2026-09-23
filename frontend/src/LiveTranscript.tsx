import { useLayoutEffect, useRef, useState } from "react";
import type { LiveSnapshot } from "./live-api";
import { Notice } from "./ui";
import { timecode } from "./utils";

export function LiveTranscript({
  snapshot,
  recording,
  finalizing,
  pollError,
}: {
  snapshot: LiveSnapshot | null;
  recording: boolean;
  finalizing: boolean;
  pollError: string;
}) {
  const list = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [unread, setUnread] = useState(false);
  const utterances = snapshot?.utterances ?? [];
  const signature = JSON.stringify(utterances);
  useLayoutEffect(() => {
    if (!list.current) return;
    if (following.current) {
      list.current.scrollTop = list.current.scrollHeight;
      setUnread(false);
    } else setUnread(true);
  }, [signature]);
  const lag = snapshot?.lag_seconds;
  const received = snapshot?.received_audio_seconds;
  const measured = (n: unknown): n is number =>
    typeof n === "number" && Number.isFinite(n) && n >= 0;
  const status = snapshot?.preview_status ?? "waiting";
  return (
    <section className="live-preview" aria-label="Предварительный транскрипт">
      <h3>Предварительный транскрипт</h3>
      <p className="live-metrics">
        Распознано:{" "}
        <strong>{timecode(snapshot?.processed_until_seconds ?? 0)}</strong>
        {measured(received) && (
          <>
            {" "}
            · Получено сервером: <strong>{timecode(received)}</strong>
          </>
        )}
      </p>
      <p>
        {measured(lag)
          ? lag >= 10
            ? `Распознавание отстаёт на ${Math.round(lag)} секунд; ${recording ? "аудио сохраняется" : "аудио передаётся или обрабатывается"}`
            : `Серверная задержка: ${Math.round(lag)} сек.`
          : "Серверная задержка: нет данных"}
      </p>
      <p role="status">
        {finalizing
          ? "Уточняем полный транскрипт, спикеров и поручения"
          : status === "unavailable"
            ? "Предварительное распознавание временно недоступно"
            : status === "processing"
              ? utterances.length
                ? "Распознаём следующий фрагмент"
                : "Распознаём первый фрагмент"
              : status === "waiting"
                ? utterances.length
                  ? "Ожидаем следующий фрагмент речи"
                  : "Ожидаем достаточно речи для распознавания"
                : "Предварительный текст обновлён"}
      </p>
      {(status === "unavailable" || snapshot?.preview_error) && (
        <Notice kind="warning">
          {snapshot?.preview_error?.message ||
            "Не удалось получить предварительный текст."}{" "}
          {recording
            ? "Запись аудио продолжается"
            : "Полученное аудио сохранено для обработки."}
        </Notice>
      )}
      {pollError && (
        <Notice kind="warning">
          {pollError} Показан последний полученный текст. Повторим обновление
          автоматически.
        </Notice>
      )}
      <p>
        <small>
          Весь текст предварительный, включая стабильные реплики. Последняя
          фраза может пересматриваться. Это не утверждённый протокол.
        </small>
      </p>
      <div
        ref={list}
        className="live-transcript-scroll"
        role="region"
        aria-label="Реплики предварительного транскрипта"
        tabIndex={0}
        onScroll={() => {
          const el = list.current!;
          following.current =
            el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
          if (following.current) setUnread(false);
        }}
      >
        <ol className="live-utterances">
          {utterances.map((u) => (
            <li
              key={u.id}
              data-utterance-id={u.id}
              className={u.is_final ? "preview-stable" : "preview-changing"}
            >
              <div>
                <span className="timestamp">{timecode(u.start)}</span>{" "}
                <strong>{u.speaker_label ?? "Говорящий не определён"}</strong>
              </div>
              <p>{u.text}</p>
              <small>
                {u.is_final ? "Стабильная часть preview" : "Фраза уточняется"}
              </small>
            </li>
          ))}
        </ol>
      </div>
      {unread && (
        <button
          className="secondary"
          onClick={() => {
            following.current = true;
            if (list.current)
              list.current.scrollTop = list.current.scrollHeight;
            setUnread(false);
          }}
        >
          Новые реплики ↓
        </button>
      )}
    </section>
  );
}
