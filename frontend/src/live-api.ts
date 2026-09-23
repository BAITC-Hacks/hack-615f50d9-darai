import { request } from "./api";
export interface LiveSnapshot {
  session_id: string;
  state: "recording" | "finalizing" | "done" | "error" | "cancelled";
  next_sequence: number;
  received_bytes: number;
  processed_until_seconds: number;
  received_audio_seconds?: number | null;
  lag_seconds?: number | null;
  revision: number;
  preview_status: "waiting" | "processing" | "ready" | "unavailable";
  preview_error: { code: string; message: string } | null;
  utterances: {
    id: string;
    start: number;
    end: number;
    speaker_label: string | null;
    text: string;
    is_final: boolean;
  }[];
  draft_tasks: unknown[];
  recording_id: string | null;
  error: { code: string; message: string } | null;
}
const base = (id: string, session?: string) =>
  `/meetings/${encodeURIComponent(id)}/live${session ? `/${encodeURIComponent(session)}` : ""}`;
const post = (body?: unknown): RequestInit => ({
  method: "POST",
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export const liveApi = {
  speechSettings: (
    id: string,
    asr_language: "auto" | "ru" | "kk",
    asr_profile: "standard" | "refined",
  ) =>
    request(`/meetings/${encodeURIComponent(id)}/speech-settings`, {
      method: "PATCH",
      body: JSON.stringify({ asr_language, asr_profile }),
    }),
  start: (
    id: string,
    source: "microphone" | "display",
    mime_type: string,
    signal: AbortSignal,
  ) =>
    request<{
      session_id: string;
      next_sequence: number;
      poll_after_ms: number;
      max_chunk_bytes: number;
    }>(base(id), { ...post({ source, mime_type }), signal }),
  chunk: (
    id: string,
    s: string,
    n: number,
    blob: Blob,
    mime: string,
    signal: AbortSignal,
  ) =>
    request<{ accepted_sequence: number; next_sequence: number }>(
      `${base(id, s)}/chunks/${n}`,
      { method: "PUT", body: blob, headers: { "Content-Type": mime }, signal },
    ),
  snapshot: (id: string, s: string, signal: AbortSignal) =>
    request<LiveSnapshot>(base(id, s), { signal }),
  finish: (id: string, s: string, last_sequence: number, signal: AbortSignal) =>
    request(`${base(id, s)}/finish`, { ...post({ last_sequence }), signal }),
  cancel: (id: string, s: string) =>
    request<void>(`${base(id, s)}/cancel`, post()),
  link: (id: string, meeting_url: string | null) =>
    request<{ meeting_url: string | null }>(
      `/meetings/${encodeURIComponent(id)}/link`,
      { method: "PATCH", body: JSON.stringify({ meeting_url }) },
    ),
};
