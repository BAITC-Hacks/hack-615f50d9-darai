import { test, expect, type Page } from "@playwright/test";
import { fixtureApi } from "./fixture-api";
async function setup(page: Page, mode = "ok") {
  const state = await fixtureApi(page);
  await page.addInitScript((mode) => {
    const w = window as any;
    w.captureStreams = [];
    w.captureContexts = [];
    w.recorderStarts = 0;
    const stream = () => {
      const c = new AudioContext();
      w.captureContexts.push(c);
      const d = c.createMediaStreamDestination(),
        o = c.createOscillator();
      o.connect(d);
      o.start();
      w.captureStreams.push(d.stream);
      return d.stream;
    };
    Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
      value: async () => {
        if (mode === "denied")
          throw new DOMException("denied", "NotAllowedError");
        return stream();
      },
    });
    Object.defineProperty(navigator.mediaDevices, "getDisplayMedia", {
      value: async () => (mode === "silent" ? new MediaStream() : stream()),
    });
    class Recorder extends EventTarget {
      static isTypeSupported() {
        return true;
      }
      state = "inactive";
      mimeType = "audio/webm;codecs=opus";
      ondataavailable: any;
      onstop: any;
      onerror: any;
      timer: any;
      constructor(public stream: MediaStream) {
        super();
        w.recorderStream = stream;
      }
      start() {
        this.state = "recording";
        w.recorderStarts++;
        this.timer = setInterval(
          () =>
            this.ondataavailable?.({
              data: new Blob(["chunk-0123456789"], { type: this.mimeType }),
            }),
          200,
        );
      }
      stop() {
        if (this.state === "inactive") return;
        this.state = "inactive";
        clearInterval(this.timer);
        setTimeout(() => {
          this.ondataavailable?.({
            data: new Blob(["FINAL"], { type: this.mimeType }),
          });
          this.onstop?.();
        }, 30);
      }
    }
    if (mode !== "real-recorder") w.MediaRecorder = Recorder;
  }, mode);
  await page.goto("/");
  await page.getByLabel("Логин", { exact: true }).fill("fixture-admin");
  await page
    .getByLabel("Пароль", { exact: true })
    .fill("fixture-only-password");
  await page
    .getByRole("button", { name: "Войти в рабочее пространство" })
    .click();
  await page.getByRole("link", { name: "＋ Создать совещание" }).click();
  await page.getByLabel("Название совещания").fill("Запись — Ә Ғ Қ");
  await page
    .getByLabel("Дата и время", { exact: true })
    .fill("2026-09-23T14:30");
  await page.getByRole("checkbox", { name: /Әлия Қасым/ }).check();
  await page
    .getByRole("button", { name: "Создать совещание", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Записать сейчас", exact: true })
    .click();
  return state;
}
test("последовательные бинарные части, финальный чанк, snapshot replacement, финальная карточка", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "Подготовить источник" }).click();
  await page
    .getByRole("button", { name: "Начать запись", exact: true })
    .click();
  await expect(page.getByText("Алғашқы мәтін", { exact: false })).toBeVisible();
  await expect(
    page.getByText("Жаңартылған мәтін", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("Алғашқы мәтін", { exact: false })).toHaveCount(
    0,
  );
  await page.screenshot({
    path: "test-results/live-recording.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Завершить запись" }).click();
  await expect(
    page.getByText("Әлия, дайындаңыз есеп. Подготовьте отчёт до пятницы.", { exact: true }),
  ).toBeVisible({ timeout: 12000 });
  expect(
    Buffer.concat(state.liveChunks.map((c) => c.bytes)).toString(),
  ).toMatch(/FINAL$/);
  expect(
    state.liveChunks.every(
      (c, i) =>
        c.sequence === i &&
        c.bytes.length <= 8 &&
        c.mime === "audio/webm;codecs=opus",
    ),
  ).toBeTruthy();
  expect(state.requests.filter((r) => r.path.endsWith("/finish"))).toHaveLength(
    1,
  );
  expect(
    await page.evaluate(() => ({
      starts: (window as any).recorderStarts,
      ended: (window as any).captureStreams.every((s: MediaStream) =>
        s.getTracks().every((t) => t.readyState === "ended"),
      ),
    })),
  ).toEqual({ starts: 1, ended: true });
});
test("сбой сети сохраняет байты для retry; preview error не мешает аудио", async ({
  page,
}) => {
  const state = await setup(page);
  state.liveFailures = 1;
  state.livePreviewError = true;
  await page.getByRole("button", { name: "Подготовить источник" }).click();
  await page
    .getByRole("button", { name: "Начать запись", exact: true })
    .click();
  await expect(page.getByText("Тест: временный сбой отправки")).toBeVisible();
  await page.getByRole("button", { name: "Завершить запись" }).click();
  await expect(
    page.getByRole("link", { name: "Скачать резервную запись" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Повторить отправку" }).click();
  await expect(
    page.getByText("Әлия, дайындаңыз есеп. Подготовьте отчёт до пятницы.", { exact: true }),
  ).toBeVisible({ timeout: 12000 });
  expect(
    Buffer.concat(state.liveChunks.map((c) => c.bytes)).toString(),
  ).toMatch(/FINAL$/);
});
for (const mode of ["denied", "silent"])
  test(`ошибка источника ${mode}`, async ({ page }) => {
    const state = await setup(page, mode);
    if (mode === "silent")
      await page.getByLabel("Режим записи").selectOption("display");
    await page.getByRole("button", { name: "Подготовить источник" }).click();
    await expect(
      page.getByText(
        mode === "denied"
          ? /Доступ к микрофону или захвату запрещён/
          : /Источник не передал звук/,
      ),
    ).toBeVisible();
    expect(state.requests.filter((r) => r.path.endsWith("/live"))).toHaveLength(
      0,
    );
    await expect(page.getByText("Идёт запись", { exact: false })).toHaveCount(
      0,
    );
  });
test("онлайн со смешиванием, отмена, остановка tracks", async ({ page }) => {
  const state = await setup(page);
  await page.getByLabel("Режим записи").selectOption("display");
  await page.getByLabel("Добавить мой микрофон").check();
  await page.getByRole("button", { name: "Подготовить источник" }).click();
  await page
    .getByRole("button", { name: "Начать запись", exact: true })
    .click();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Отменить запись" }).click();
  await expect(
    page.getByRole("button", { name: "Подготовить источник" }),
  ).toBeVisible();
  expect(state.liveState).toBe("cancelled");
  expect(state.requests.filter((r) => r.path.endsWith("/finish"))).toHaveLength(
    0,
  );
  expect(
    await page.evaluate(() =>
      (window as any).captureStreams.every((s: MediaStream) =>
        s.getTracks().every((t) => t.readyState === "ended"),
      ),
    ),
  ).toBeTruthy();
});

test("настоящий MediaRecorder на искусственном аудио и завершение источника", async ({
  page,
}) => {
  const state = await setup(page, "real-recorder");
  await page.getByRole("button", { name: "Подготовить источник" }).click();
  await page
    .getByRole("button", { name: "Начать запись", exact: true })
    .click();
  await expect(page.getByText("Идёт запись", { exact: false })).toBeVisible();
  await expect
    .poll(() => state.liveChunks.length, { timeout: 10000 })
    .toBeGreaterThan(0);
  await page.evaluate(() => {
    const track = (window as any).captureStreams[0].getAudioTracks()[0];
    track.dispatchEvent(new Event("ended"));
  });
  await expect(
    page.getByText("Әлия, дайындаңыз есеп. Подготовьте отчёт до пятницы.", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  expect(state.liveChunks.length).toBeGreaterThan(0);
});
test("выход со страницы освобождает потоки и контекст", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Подготовить источник" }).click();
  await page
    .getByRole("button", { name: "Начать запись", exact: true })
    .click();
  await expect(page.getByText("Идёт запись", { exact: false })).toBeVisible();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Сотрудники", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      (window as any).captureStreams.every((s: MediaStream) =>
        s.getTracks().every((t) => t.readyState === "ended"),
      ),
    ),
  ).toBeTruthy();
});

test("язык и профиль сохраняются до начала записи", async ({ page }) => {
  const state = await setup(page);
  await page.getByLabel("Язык совещания", { exact: true }).selectOption("kk");
  await page.getByLabel("Режим распознавания", { exact: true }).selectOption("refined");
  await expect(page.getByRole("button", { name: "Подготовить источник" })).toBeDisabled();
  await page.getByRole("button", { name: "Сохранить настройки распознавания" }).click();
  await expect(page.getByRole("button", { name: "Подготовить источник" })).toBeEnabled();
  expect(state.meetings[0].asr_language).toBe("kk");
  expect(state.meetings[0].asr_profile).toBe("refined");
  await page.reload();
  await expect(page.getByLabel("Язык совещания", { exact: true })).toHaveValue("kk");
  await page.screenshot({ path: "test-results/speech-settings.png", fullPage: true });
});
