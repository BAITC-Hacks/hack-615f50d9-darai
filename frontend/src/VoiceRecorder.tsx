import { useEffect, useRef, useState } from "react";
import { Notice } from "./ui";
export function VoiceRecorder({
  onFile,
  disabled,
  onRecordingChange,
}: {
  onFile: (file: File) => void;
  disabled: boolean;
  onRecordingChange: (active: boolean) => void;
}) {
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const alive = useRef(true);
  const [recording, setRecording] = useState(false);
  const [starting, setStarting] = useState(false);
  const startingLock = useRef(false);
  const requestId = useRef(0);
  const [error, setError] = useState("");
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      requestId.current++;
      if (recorder.current?.state === "recording") recorder.current.stop();
      stream.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);
  useEffect(() => {
    if (!recording) return;
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [recording]);
  useEffect(() => {
    if (seconds >= 60 && recorder.current?.state === "recording")
      recorder.current.stop();
  }, [seconds]);
  async function start() {
    if (startingLock.current) return;
    startingLock.current = true;
    const currentRequest = ++requestId.current;
    setStarting(true);
    onRecordingChange(true);
    setError("");
    try {
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder)
        throw new Error(
          "Запись микрофона доступна на localhost или по HTTPS в поддерживаемом браузере. Загрузите готовый образец.",
        );
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!alive.current || currentRequest !== requestId.current) {
        media.getTracks().forEach((t) => t.stop());
        return;
      }
      stream.current = media;
      const mime = [
        "audio/webm;codecs=opus",
        "audio/mp4",
        "audio/ogg;codecs=opus",
      ].find((t) => MediaRecorder.isTypeSupported(t));
      const r = new MediaRecorder(media, mime ? { mimeType: mime } : undefined);
      recorder.current = r;
      const chunks: Blob[] = [];
      let failed = false;
      r.ondataavailable = (e) => {
        if (e.data.size) chunks.push(e.data);
      };
      r.onstop = () => {
        media.getTracks().forEach((t) => t.stop());
        if (alive.current) {
          setRecording(false);
          onRecordingChange(false);
          if (failed) return;
          const type = r.mimeType.split(";")[0];
          onFile(
            new File(
              chunks,
              `voice.${type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm"}`,
              { type },
            ),
          );
        }
      };
      r.onerror = () => {
        failed = true;
        media.getTracks().forEach((t) => t.stop());
        if (!alive.current) return;
        setError("Не удалось записать звук. Попробуйте загрузить файл.");
        setRecording(false);
        onRecordingChange(false);
      };
      r.start();
      setSeconds(0);
      setRecording(true);
    } catch (e) {
      if (currentRequest !== requestId.current) return;
      stream.current?.getTracks().forEach((t) => t.stop());
      onRecordingChange(false);
      if (alive.current)
        setError(
          e instanceof DOMException && e.name === "NotAllowedError"
            ? "Доступ к микрофону запрещён. Разрешите его в браузере или загрузите образец."
            : e instanceof DOMException && e.name === "NotFoundError"
              ? "Микрофон не найден. Подключите его или загрузите образец."
              : e instanceof DOMException && e.name === "NotSupportedError"
                ? "Формат записи не поддерживается браузером. Загрузите WAV, MP3 или M4A."
                : e instanceof Error
                  ? e.message
                  : "Нет доступа к микрофону.",
        );
    } finally {
      if (currentRequest === requestId.current) {
        startingLock.current = false;
        if (alive.current) setStarting(false);
      }
    }
  }
  return (
    <div>
      {error && <Notice kind="error">{error}</Notice>}
      <button
        type="button"
        className={recording ? "danger" : "secondary"}
        disabled={!recording && (disabled || starting)}
        onClick={() => (recording ? recorder.current?.stop() : void start())}
      >
        {recording
          ? `■ Остановить запись · ${seconds} с`
          : starting
            ? "Ожидаем доступ к микрофону…"
            : "Записать с микрофона"}
      </button>
      {starting && (
        <button
          className="secondary"
          type="button"
          onClick={() => {
            requestId.current++;
            startingLock.current = false;
            setStarting(false);
            onRecordingChange(false);
          }}
        >
          Отменить ожидание микрофона
        </button>
      )}
      {recording && (
        <p role="status">
          Микрофон включён. Прочитайте несколько предложений обычным голосом.
        </p>
      )}
    </div>
  );
}
