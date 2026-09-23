export function timecode(seconds: number) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)
    .toString()
    .padStart(2, "0")}:${(s % 60).toString().padStart(2, "0")}`;
}
export function dateTime(value: string, timezone?: string) {
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: timezone,
    }).format(new Date(value));
  } catch {
    return value;
  }
}
export function day(value: string | null) {
  if (!value) return "Требует уточнения";
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(`${value}T12:00:00Z`));
}
export function isOverdue(
  deadline: string | null,
  completed: boolean,
  timezone: string,
  now = new Date(),
) {
  if (!deadline || completed) return false;
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const part = (name: string) => parts.find((p) => p.type === name)?.value;
  return deadline < `${part("year")}-${part("month")}-${part("day")}`;
}
// Convert a wall-clock value in the selected IANA zone, never in the browser's zone.
export function zonedToISO(value: string, timezone: string) {
  const target = Date.parse(`${value}:00Z`);
  if (!Number.isFinite(target))
    throw new Error("Укажите корректные дату и время.");
  const formatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  let result = target;
  for (let i = 0; i < 3; i++) {
    const p = formatter.formatToParts(new Date(result));
    const get = (t: string) => p.find((x) => x.type === t)!.value;
    const asUTC = Date.parse(
      `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}:${get("second")}Z`,
    );
    result += target - asUTC;
  }
  const parts = formatter.formatToParts(new Date(result));
  const g = (t: string) => parts.find((p) => p.type === t)!.value;
  if (
    `${g("year")}-${g("month")}-${g("day")}T${g("hour")}:${g("minute")}` !==
    value
  )
    throw new Error("Это время не существует в выбранном часовом поясе.");
  return new Date(result).toISOString();
}
