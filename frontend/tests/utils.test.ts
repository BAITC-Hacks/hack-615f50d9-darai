import { describe, it, expect } from "vitest";
import { zonedToISO, isOverdue, timecode } from "../src/utils";
import { apiPath } from "../src/api";
describe("время встречи и срок", () => {
  it("не использует часовой пояс браузера вместо Asia/Almaty", () => {
    expect(zonedToISO("2026-09-23T14:30", "Asia/Almaty")).toBe(
      "2026-09-23T09:30:00.000Z",
    );
  });
  it("срок истекает в конце дня встречи", () => {
    expect(
      isOverdue(
        "2026-09-23",
        false,
        "Asia/Almaty",
        new Date("2026-09-23T18:59:59Z"),
      ),
    ).toBe(false);
    expect(
      isOverdue(
        "2026-09-23",
        false,
        "Asia/Almaty",
        new Date("2026-09-23T19:00:00Z"),
      ),
    ).toBe(true);
    expect(
      isOverdue(
        "2026-09-23",
        true,
        "Asia/Almaty",
        new Date("2026-09-24T00:00:00Z"),
      ),
    ).toBe(false);
  });
  it("не выдумывает просрочку при неизвестном сроке", () =>
    expect(isOverdue(null, false, "UTC")).toBe(false));
  it("отклоняет несуществующее время при переходе на летнее время", () =>
    expect(() => zonedToISO("2026-03-29T02:30", "Europe/Berlin")).toThrow());
  it("форматирует аудиометки", () => expect(timecode(125.8)).toBe("02:05"));
});
describe("ресурсы внутри контура", () => {
  it("добавляет /api к маршруту аудио", () =>
    expect(apiPath("/meetings/1/recordings/2/audio")).toBe(
      "/api/meetings/1/recordings/2/audio",
    ));
  it.each([
    "https://example.com/a",
    "//example.com/a",
    "/\\example.com",
    "/../file",
  ])("отклоняет внешний или некорректный URL %s", (path) =>
    expect(() => apiPath(path)).toThrow(),
  );
});
