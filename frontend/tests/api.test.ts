import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, request, setCsrf } from "../src/api";
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  setCsrf("");
});
describe("cookie session and error transport", () => {
  it("отправляет cookie и CSRF в изменяющем запросе", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ saved: true }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);
    setCsrf("in-memory-test-token");
    await request("/meetings/fixture/summary", {
      method: "PATCH",
      body: JSON.stringify({ summary: "Тест · Қазақша" }),
    });
    const [url, options] = fetch.mock.calls[0];
    expect(url).toBe("/api/meetings/fixture/summary");
    expect(options.credentials).toBe("include");
    expect(options.headers.get("X-CSRF-Token")).toBe("in-memory-test-token");
    expect(options.headers.get("Content-Type")).toBe("application/json");
  });
  it("сохраняет код конфликта и сообщение backend", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "DRAFT_REVISION_MISMATCH",
              message: "Протокол изменился",
              details: { current_draft_revision: 7 },
            },
          }),
          { status: 409 },
        ),
      ),
    );
    await expect(
      request("/meetings/fixture/confirm", { method: "POST" }),
    ).rejects.toMatchObject({
      status: 409,
      code: "DRAFT_REVISION_MISMATCH",
      message: "Протокол изменился",
    });
  });
  it("401 завершает клиентскую сессию", async () => {
    const target = new EventTarget();
    const expire = vi.fn();
    target.addEventListener("darai:unauthorized", expire);
    vi.stubGlobal("window", target);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { code: "UNAUTHENTICATED", message: "Войдите снова" },
          }),
          { status: 401 },
        ),
      ),
    );
    await expect(request("/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(expire).toHaveBeenCalledOnce();
  });
  it("HTML от ошибочного SPA fallback не считается успешным API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<html>SPA</html>", {
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );
    await expect(request("/auth/me")).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
  it("зависший запрос завершается и освобождает polling", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (_url, options) =>
          new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const pending = expect(request("/notifications")).rejects.toMatchObject({
      code: "TIMEOUT",
    });
    await vi.advanceTimersByTimeAsync(45000);
    await pending;
    expect(vi.getTimerCount()).toBe(0);
  });
});

it("403 PASSWORD_CHANGE_REQUIRED централизованно включает смену пароля", async () => {
  const target = new EventTarget();
  const required = vi.fn();
  target.addEventListener("darai:password-required", required);
  vi.stubGlobal("window", target);
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "PASSWORD_CHANGE_REQUIRED",
              message: "Смените пароль",
            },
          }),
          { status: 403 },
        ),
      ),
  );
  await expect(request("/employees")).rejects.toMatchObject({
    code: "PASSWORD_CHANGE_REQUIRED",
  });
  expect(required).toHaveBeenCalledOnce();
});
