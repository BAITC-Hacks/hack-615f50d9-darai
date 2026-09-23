import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { liveApi, type LiveSnapshot } from "./live-api";
import { Field, Notice } from "./ui";
import { timecode } from "./utils";

// Independent of employee voice enrollment. Blobs are ordered parts of ONE container.
export function MeetingLiveRecorder({
  id,
  disabled,
  refresh,
  onActive,
}: {
  id: string;
  disabled: boolean;
  refresh: () => Promise<void>;
  onActive: (v: boolean) => void;
}) {
  const [source, setSource] = useState<"microphone" | "display">("microphone");
  const [mic, setMic] = useState(false);
  const [phase, setPhase] = useState("idle");
  const [error, setError] = useState("");
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [level, setLevel] = useState(0);
  const [backup, setBackup] = useState("");
  const [pending, setPending] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  const streams = useRef<MediaStream[]>([]),
    context = useRef<AudioContext | null>(null),
    recorder = useRef<MediaRecorder | null>(null);
  const session = useRef(""),
    chunks = useRef<Blob[]>([]),
    next = useRef(0),
    max = useRef(5242880),
    total = useRef(0);
  const alive = useRef(true),
    lock = useRef(false),
    stopped = useRef(false),
    stopping = useRef(false),
    sending = useRef(false),
    failed = useRef(false);
  const abort = useRef(new AbortController()),
    timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined),
    meter = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const cancelled = useRef(false);
  const pollDelay = useRef(2000);
  const beganRef = useRef(0),
    finishSent = useRef(false);
  const finishRef = useRef<() => void>(() => {}),
    refreshRef = useRef(refresh),
    activeRef = useRef(onActive),
    url = useRef("");
  refreshRef.current = refresh;
  activeRef.current = onActive;
  const busy = [
    "preparing",
    "ready",
    "starting",
    "recording",
    "sending",
    "finalizing",
    "failed",
  ].includes(phase);
  function release() {
    clearInterval(meter.current);
    for (const stream of streams.current)
      for (const track of stream.getTracks()) {
        track.onended = null;
        track.stop();
      }
    streams.current = [];
    void context.current?.close().catch(() => {});
    context.current = null;
  }
  function localCopy() {
    if (!alive.current || !chunks.current.length) return;
    URL.revokeObjectURL(url.current);
    url.current = URL.createObjectURL(
      new Blob(chunks.current, {
        type: recorder.current?.mimeType || "audio/webm",
      }),
    );
    setBackup(url.current);
  }
  useEffect(() => {
    alive.current = true;
    abort.current = new AbortController();
    return () => {
      alive.current = false;
      abort.current.abort();
      clearTimeout(timer.current);
      release();
      const r = recorder.current;
      if (r && r.state !== "inactive") r.stop();
      URL.revokeObjectURL(url.current);
    };
  }, []);
  useEffect(() => {
    if (!busy) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    const navigate = (e: MouseEvent) => {
      const link = (e.target as Element).closest("a");
      if (
        link &&
        !link.hasAttribute("download") &&
        link.target !== "_blank" &&
        !link.getAttribute("href")?.startsWith("#") &&
        !window.confirm(
          "Выйти со страницы? Несохранённая запись будет потеряна.",
        )
      ) {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", warn);
    document.addEventListener("click", navigate, true);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("click", navigate, true);
    };
  }, [busy]);
  useEffect(() => {
    activeRef.current(busy);
  }, [busy]);
  async function prepare() {
    if (lock.current) return;
    lock.current = true;
    setPhase("preparing");
    setError("");
    try {
      if (!navigator.mediaDevices || !window.MediaRecorder)
        throw new Error(
          "Запись не поддерживается. Используйте HTTPS/localhost и современный браузер или загрузите файл.",
        );
      const stream =
        source === "display"
          ? await navigator.mediaDevices.getDisplayMedia({
              video: true,
              audio: true,
            })
          : await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!alive.current) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      streams.current.push(stream);
      if (!stream.getAudioTracks().length)
        throw new Error(
          "Источник не передал звук. Выберите вкладку с включённым «Поделиться аудио» или режим «В помещении».",
        );
      if (source === "display" && mic) {
        const extra = await navigator.mediaDevices.getUserMedia({
          audio: true,
        });
        if (!alive.current) {
          extra.getTracks().forEach((t) => t.stop());
          return;
        }
        streams.current.push(extra);
      }
      const ctx = new AudioContext();
      context.current = ctx;
      await ctx.resume();
      if (!alive.current) {
        release();
        return;
      }
      const destination = ctx.createMediaStreamDestination(),
        analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      for (const s of streams.current) {
        const node = ctx.createMediaStreamSource(
          new MediaStream(s.getAudioTracks()),
        );
        node.connect(destination);
        node.connect(analyser);
      }
      // No connection to ctx.destination: captured audio never plays on speakers.
      streams.current.push(destination.stream);
      const mime = [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/mp4",
        "audio/ogg;codecs=opus",
      ].find((t) => MediaRecorder.isTypeSupported(t));
      if (!mime)
        throw new Error(
          "Браузер не поддерживает подходящий аудиоформат. Загрузите файл.",
        );
      recorder.current = new MediaRecorder(destination.stream, {
        mimeType: mime,
      });
      const samples = new Uint8Array(analyser.frequencyBinCount);
      meter.current = setInterval(() => {
        if (beganRef.current)
          setSeconds((Date.now() - beganRef.current) / 1000);
        analyser.getByteTimeDomainData(samples);
        setLevel(
          Math.min(
            100,
            Math.sqrt(
              samples.reduce((n, v) => n + (v - 128) ** 2, 0) / samples.length,
            ) * 3,
          ),
        );
      }, 150);
      for (const s of streams.current)
        for (const t of s.getTracks())
          t.onended = () => {
            if (recorder.current?.state === "recording") finishRef.current();
            else {
              release();
              setPhase("idle");
              setError("Источник отключён. Подготовьте его заново.");
            }
          };
      setPhase("ready");
    } catch (e) {
      release();
      setPhase("idle");
      setError(
        e instanceof DOMException && e.name === "NotAllowedError"
          ? "Доступ к микрофону или захвату запрещён. Разрешите доступ в браузере и повторите."
          : e instanceof DOMException && e.name === "NotFoundError"
            ? "Микрофон не найден. Подключите устройство или загрузите файл."
            : e instanceof Error
              ? e.message
              : "Не удалось подготовить источник.",
      );
    } finally {
      lock.current = false;
    }
  }
  async function poll() {
    if (!alive.current || !session.current) return;
    try {
      const s = await liveApi.snapshot(
        id,
        session.current,
        abort.current.signal,
      );
      if (!alive.current || cancelled.current) return;
      setSnapshot(s);
      if (s.state === "done") {
        setPhase("done");
        activeRef.current(false);
        await refreshRef.current();
        return;
      }
      if (s.state === "error" || s.state === "cancelled") {
        setError(
          s.error?.message ||
            "Сессия остановлена сервером. Скачайте резервную запись.",
        );
        if (recorder.current?.state === "recording") recorder.current.stop();
        release();
        setPhase("failed");
        failed.current = true;
        localCopy();
        return;
      }
    } catch (e) {
      if (!alive.current || cancelled.current) return;
      setError(
        e instanceof Error
          ? `Предварительный текст недоступен: ${e.message}`
          : "Не удалось обновить текст.",
      );
    }
    timer.current = setTimeout(() => void poll(), pollDelay.current);
  }
  async function drain() {
    if (sending.current || failed.current || !session.current || !alive.current)
      return;
    sending.current = true;
    try {
      while (!cancelled.current && next.current < chunks.current.length) {
        const n = next.current,
          b = chunks.current[n];
        try {
          const ack = await liveApi.chunk(
            id,
            session.current,
            n,
            b,
            recorder.current!.mimeType,
            abort.current.signal,
          );
          if (ack.accepted_sequence !== n || ack.next_sequence !== n + 1)
            throw new Error("Некорректное подтверждение части записи.");
          next.current++;
        } catch (e) {
          if (
            e instanceof ApiError &&
            e.code === "CHUNK_OUT_OF_ORDER" &&
            e.details?.next_sequence === n + 1
          ) {
            next.current++;
          } else throw e;
        }
        if (!alive.current) return;
        setPending(chunks.current.length - next.current);
      }
      if (!cancelled.current && stopped.current && !finishSent.current) {
        if (!chunks.current.length)
          throw new Error(
            "Запись пуста. Отмените её и запишите более длинный фрагмент.",
          );
        await liveApi.finish(
          id,
          session.current,
          next.current - 1,
          abort.current.signal,
        );
        finishSent.current = true;
        if (alive.current) setPhase("finalizing");
      }
    } catch (e) {
      if (alive.current && !cancelled.current) {
        if (
          e instanceof ApiError &&
          ["CHUNKS_MISSING", "CHUNK_OUT_OF_ORDER"].includes(e.code)
        ) {
          const expected = e.details?.next_sequence;
          if (
            typeof expected === "number" &&
            Number.isInteger(expected) &&
            expected >= 0 &&
            expected <= chunks.current.length
          )
            next.current = expected;
        }
        failed.current = true;
        setError(
          e instanceof ApiError && e.code === "CHUNK_CONFLICT"
            ? "Конфликт содержимого записи. Скачайте резервную копию."
            : e instanceof Error
              ? e.message
              : "Ошибка отправки",
        );
        if (stopped.current) setPhase("failed");
        localCopy();
      }
    } finally {
      sending.current = false;
    }
  }
  async function start() {
    if (lock.current || phase !== "ready") return;
    lock.current = true;
    setPhase("starting");
    setError("");
    try {
      const r = recorder.current!;
      const result = await liveApi.start(
        id,
        source,
        r.mimeType,
        abort.current.signal,
      );
      if (!alive.current) {
        void liveApi.cancel(id, result.session_id).catch(() => {});
        return;
      }
      cancelled.current = false;
      session.current = result.session_id;
      pollDelay.current = Math.max(
        1000,
        Number.isFinite(result.poll_after_ms) ? result.poll_after_ms : 2000,
      );
      max.current = result.max_chunk_bytes;
      if (
        !Number.isSafeInteger(max.current) ||
        max.current <= 0 ||
        result.next_sequence !== 0
      )
        throw new Error("Некорректные параметры сессии записи.");
      chunks.current = [];
      next.current = 0;
      total.current = 0;
      stopped.current = false;
      stopping.current = false;
      failed.current = false;
      const began = Date.now();
      beganRef.current = began;
      finishSent.current = false;
      r.ondataavailable = (e) => {
        if (!e.data.size) return;
        for (let offset = 0; offset < e.data.size; offset += max.current)
          chunks.current.push(
            e.data.slice(offset, offset + max.current, r.mimeType),
          );
        total.current += e.data.size;
        if (alive.current) {
          setPending(chunks.current.length - next.current);
          setSeconds((Date.now() - began) / 1000);
        }
        if (total.current >= 128 * 1024 * 1024) {
          setError(
            "Достигнут предел памяти 128 МБ. Запись остановлена; дождитесь отправки или скачайте копию.",
          );
          finishRef.current();
        }
        void drain();
      };
      r.onstop = () => {
        stopped.current = true;
        release();
        if (alive.current) {
          setSeconds((Date.now() - began) / 1000);
          localCopy();
          setPhase(failed.current ? "failed" : "sending");
          void drain();
        }
      };
      r.onerror = () => {
        setError(
          "Браузер остановил запись с ошибкой. Сохраните резервную копию.",
        );
        finishRef.current();
      };
      if (!r.stream.getAudioTracks().some((t) => t.readyState === "live"))
        throw new Error("Источник отключён. Подготовьте запись заново.");
      r.start(2000);
      if (r.state !== "recording")
        throw new Error("Браузер не запустил запись.");
      setPhase("recording");
      void poll();
    } catch (e) {
      release();
      setPhase("failed");
      setError(e instanceof Error ? e.message : "Не удалось начать запись");
    } finally {
      lock.current = false;
    }
  }
  function finish() {
    if (stopping.current) return;
    stopping.current = true;
    setPhase("sending");
    const r = recorder.current;
    if (r && r.state !== "inactive") r.stop();
  }
  finishRef.current = finish;
  async function cancel() {
    if (
      lock.current ||
      !window.confirm("Отменить запись? Несохранённая запись будет потеряна.")
    )
      return;
    lock.current = true;
    setCancelling(true);
    try {
      if (session.current) await liveApi.cancel(id, session.current);
      cancelled.current = true;
      abort.current.abort();
      abort.current = new AbortController();
      clearTimeout(timer.current);
      const r = recorder.current;
      if (r) {
        r.ondataavailable = null;
        r.onstop = null;
        if (r.state !== "inactive") r.stop();
      }
      release();
      chunks.current = [];
      beganRef.current = 0;
      setSeconds(0);
      setPending(0);
      setLevel(0);
      session.current = "";
      URL.revokeObjectURL(url.current);
      setBackup("");
      setSnapshot(null);
      setPhase("idle");
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось отменить запись");
    } finally {
      lock.current = false;
      setCancelling(false);
    }
  }
  return (
    <div className="recording-box">
      <Notice>
        Перед записью уведомите всех участников. При закрытии страницы
        несохранённые данные могут быть потеряны.
      </Notice>
      <Field label="Режим записи">
        <select
          disabled={busy}
          value={source}
          onChange={(e) => setSource(e.target.value as typeof source)}
        >
          <option value="microphone">В помещении</option>
          <option value="display">Онлайн-встреча</option>
        </select>
      </Field>
      {source === "display" && (
        <label className="check">
          <input
            type="checkbox"
            disabled={busy}
            checked={mic}
            onChange={(e) => setMic(e.target.checked)}
          />
          Добавить мой микрофон
        </label>
      )}
      <progress aria-label="Уровень звука" max={100} value={level} />
      <p role="status">
        {phase === "recording"
          ? "Идёт запись"
          : phase === "ready"
            ? "Источник готов. Проверьте уровень звука."
            : phase === "sending"
              ? "Отправляем запись…"
              : phase === "finalizing"
                ? "Финальная обработка…"
                : phase === "done"
                  ? "Запись сохранена и обработана"
                  : phase === "preparing"
                    ? "Ожидаем разрешение источника…"
                    : phase === "starting"
                      ? "Создаём сессию…"
                      : ""}{" "}
        · {timecode(seconds)}
      </p>
      <div className="form-actions">
        {phase === "idle" && (
          <button disabled={disabled} onClick={() => void prepare()}>
            Подготовить источник
          </button>
        )}
        {phase === "ready" && (
          <button disabled={disabled} onClick={() => void start()}>
            Начать запись
          </button>
        )}
        {phase === "recording" && (
          <button disabled={cancelling} onClick={finish}>
            Завершить запись
          </button>
        )}
        {busy && phase !== "preparing" && phase !== "starting" && (
          <button
            className="secondary"
            disabled={cancelling}
            onClick={() => void cancel()}
          >
            {cancelling ? "Отменяем запись…" : "Отменить запись"}
          </button>
        )}
        {failed.current && session.current && (
          <button
            onClick={() => {
              failed.current = false;
              setError("");
              if (stopped.current) setPhase("sending");
              void drain();
            }}
          >
            Повторить отправку
          </button>
        )}
      </div>
      {pending > 0 && <p>Частей ожидают подтверждения: {pending}</p>}
      {error && <Notice kind="error">{error}</Notice>}
      {backup && (
        <a
          href={backup}
          download={`meeting-${id}.${recorder.current?.mimeType.includes("mp4") ? "m4a" : recorder.current?.mimeType.includes("ogg") ? "ogg" : "webm"}`}
        >
          Скачать резервную запись
        </a>
      )}
      {snapshot && (
        <div>
          <h3>Предварительный транскрипт</h3>
          <p>
            Реплики могут измениться. Обработано:{" "}
            {timecode(snapshot.processed_until_seconds)} · записано:{" "}
            {timecode(seconds)}
          </p>
          {snapshot.preview_error && (
            <Notice kind="warning">
              {snapshot.preview_error.message} Сохранение аудио продолжается.
            </Notice>
          )}
          {snapshot.utterances.length === 0 && (
            <p>Ожидаем распознавание речи…</p>
          )}
          {snapshot.utterances.map((u) => (
            <p key={u.id}>
              <span className="timestamp">{timecode(u.start)}</span>{" "}
              <strong>{u.speaker_label ?? "Говорящий не определён"}</strong>:{" "}
              {u.text}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
