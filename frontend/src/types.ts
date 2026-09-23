export type ID = string;
export type Role = "admin" | "secretary" | "employee";
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export interface EmployeeRef {
  id: ID;
  fio: string;
  position: string;
  department: string;
}
export interface AccountInfo {
  id: ID;
  login: string;
  role: Role;
  active: boolean;
  must_change_password: boolean;
  employee_id: ID | null;
  employee_fio: string | null;
  created_at: string;
}
export interface User {
  id: ID;
  login: string;
  role: Role;
  employee: EmployeeRef | null;
  must_change_password: boolean;
}
export interface VoiceProfile {
  status: "none" | "ok" | "needs_review" | "incompatible";
  quality_status: string | null;
  speech_seconds: number | null;
  created_at: string | null;
  consent_at: string | null;
}
export interface Employee extends EmployeeRef {
  /** Old servers omit this field: the UI must deny deletion by default. */
  can_delete?: boolean;
  active: boolean;
  has_account: boolean;
  user_id: ID | null;
  voice_profile: VoiceProfile;
}
export interface Participant {
  employee_id: ID;
  fio: string;
  position: string;
  department: string;
  has_account: boolean;
  voice_status: VoiceProfile["status"];
}
export interface UserRef {
  id: ID;
  login: string;
  employee_id: ID | null;
  fio: string | null;
}
export interface RecordingStatus {
  id: ID;
  meeting_id: ID;
  processing_status: "processing" | "done" | "error";
  stage: string;
  error_code: string | null;
  error_message: string | null;
  generation: number;
  original_filename: string;
  duration_seconds: number | null;
  languages: string[];
  extraction: {
    status: "not_started" | "ok" | "error";
    error_code: string | null;
    error_message: string | null;
  };
  audio_url: string | null;
  created_at: string;
  updated_at: string;
}
export interface Speaker {
  label: string;
  proposed_employee_id: ID | null;
  confirmed_employee_id: ID | null;
  manually_set: boolean;
  similarity: number | null;
  second_similarity: number | null;
  review_required: boolean;
  review_reasons: string[];
  clean_speech_seconds: number;
  utterance_count: number;
}
export interface Utterance {
  id: number;
  speaker_label: string | null;
  employee_id: ID | null;
  start: number;
  end: number;
  text: string;
  language: string | null;
  uncertain: boolean;
  uncertain_reasons: string[];
}
export interface Task {
  id: ID;
  meeting_id: ID;
  meeting_title: string;
  from: string | null;
  from_speaker_label: string | null;
  from_fio: string | null;
  to: ID | null;
  to_fio: string | null;
  task: string;
  deadline: string | null;
  deadline_source: string | null;
  deadline_at: string | null;
  evidence: string | null;
  source_utterance_ids: number[];
  confidence: number | null;
  status: "draft" | "confirmed";
  execution_status: "in_progress" | "completed";
  completed_at: string | null;
  overdue: boolean;
  needs_review: boolean;
  review_reasons: string[];
  origin: "llm" | "manual";
  created_at: string;
  updated_at: string;
}
export interface MeetingListItem {
  meeting_url?: string | null;
  id: ID;
  title: string;
  starts_at: string;
  timezone: string;
  organizer: UserRef;
  secretary: UserRef;
  approval_status: "draft" | "confirmed";
  protocol_version: number;
  participant_count: number;
  recording: RecordingStatus | null;
  can_edit: boolean;
}
export interface Meeting extends MeetingListItem {
  agenda: string;
  draft_revision: number;
  confirmed_at: string | null;
  confirmed_by: ID | null;
  participants: Participant[];
  permissions: {
    can_edit: boolean;
    can_confirm: boolean;
    can_export_draft: boolean;
  };
  content_visible: boolean;
  speakers: Speaker[];
  utterances: Utterance[];
  summary: string | null;
  summary_edited: boolean;
  tasks: Task[];
}
export interface Notification {
  id: ID;
  event_type: string;
  title: string;
  message: string;
  meeting_id: ID | null;
  task_id: ID | null;
  created_at: string;
  read_at: string | null;
}
export interface NotificationsPage extends Page<Notification> {
  unread_count: number;
}
export type TaskInput = Pick<
  Task,
  | "task"
  | "from"
  | "to"
  | "deadline"
  | "deadline_source"
  | "evidence"
  | "source_utterance_ids"
>;
export type EmployeeInput = Pick<Employee, "fio" | "position" | "department">;
export interface MeetingInput {
  title: string;
  starts_at: string;
  timezone: string;
  agenda: string;
  participant_ids: ID[];
  secretary_id: null;
}
