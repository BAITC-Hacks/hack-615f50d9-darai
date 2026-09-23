import { test, expect, type Page } from "@playwright/test";
import { fixtureApi } from "./fixture-api";

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Логин", { exact: true }).fill("fixture-admin");
  await page
    .getByLabel("Пароль", { exact: true })
    .fill("fixture-only-password");
  await page
    .getByRole("button", { name: "Войти в рабочее пространство" })
    .click();
  await expect(
    page.getByRole("heading", {
      name: /^(Совещания|Мой профиль)$/,
      exact: true,
    }),
  ).toBeVisible();
}
async function meeting(page: Page) {
  await page.getByRole("link", { name: "＋ Создать совещание" }).click();
  await page.getByLabel("Название совещания").fill("ТЕСТ · Қаржы және бюджет");
  await page
    .getByLabel("Дата и время", { exact: true })
    .fill("2026-09-23T14:30");
  await page
    .getByLabel("Повестка", { exact: true })
    .fill("Бюджет · Ә Ғ Қ Ң Ө Ұ Ү Һ І");
  await page.getByRole("checkbox", { name: /Әлия Қасым/ }).check();
  await page
    .getByRole("button", { name: "Создать совещание", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "ТЕСТ · Қаржы және бюджет" }),
  ).toBeVisible();
}
async function upload(page: Page) {
  await page.getByLabel("Загрузить запись совещания").setInputFiles({
    name: "synthetic.wav",
    mimeType: "audio/wav",
    buffer: Buffer.from("TEST FIXTURE ONLY"),
  });
  await page.getByRole("button", { name: "Загрузить и обработать" }).click();
}
test("контрактный путь: вход → сотрудник → встреча → проверка → задача → уведомление → экспорт", async ({
  page,
}) => {
  const state = await fixtureApi(page);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await login(page);
  await expect(page.getByText("Здесь появятся ваши совещания")).toBeVisible();
  await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
  await page.getByRole("link", { name: "＋ Добавить сотрудника" }).click();
  await page.getByLabel("ФИО", { exact: true }).fill("Ғалым Өмір — тест");
  await page.getByLabel("Должность", { exact: true }).fill("Аналитик");
  await page.getByLabel("Департамент", { exact: true }).fill("Қаржы");
  await page
    .getByRole("button", { name: "Создать сотрудника", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Ғалым Өмір — тест" }),
  ).toBeVisible();
  await page.getByLabel("Должность", { exact: true }).fill("Ведущий аналитик");
  await page
    .getByRole("button", { name: "Сохранить изменения", exact: true })
    .click();
  await expect(page.getByText("Сохранено", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Должность", { exact: true })).toHaveValue(
    "Ведущий аналитик",
  );
  await page.getByRole("link", { name: "Совещания", exact: true }).click();
  await meeting(page);
  expect(state.meetings[0].starts_at).toBe("2026-09-23T09:30:00.000Z");
  await upload(page);
  await expect(
    page.getByText("Подготовить отчёт — есеп", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  await expect(
    page.getByText("Исполнитель требует уточнения.", { exact: false }),
  ).toBeVisible();
  await page
    .getByLabel("Сопоставление SPEAKER_00")
    .selectOption(state.employees[0].id);
  await page
    .getByRole("button", { name: "Сохранить спикеров", exact: true })
    .click();
  await expect(
    page.getByText("Спикеры сохранены", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Итоги и решения")
    .fill("Сохранённые правки · Ә Ғ Қ Ң Ө Ұ Ү Һ І");
  await page
    .getByRole("button", { name: "Сохранить саммари", exact: true })
    .click();
  await expect(
    page.getByText("Саммари сохранено", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Редактировать поручение" }).click();
  await page.getByLabel("Кому поручено").selectOption(state.employees[0].id);
  await page.getByLabel("Кто поручил").selectOption(state.employees[0].id);
  await page.getByLabel("Срок исполнения", { exact: true }).fill("2026-09-25");
  await page
    .getByRole("button", { name: "Сохранить поручение", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Редактировать поручение" }),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Итоги и решения")).toHaveValue(
    "Сохранённые правки · Ә Ғ Қ Ң Ө Ұ Ү Һ І",
  );
  await expect(page.getByLabel("Сопоставление SPEAKER_00")).toHaveValue(
    state.employees[0].id,
  );
  await page.screenshot({
    path: "test-results/review-desktop.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Утвердить протокол", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Подтвердить и опубликовать" })
    .click();
  await expect(page.getByText("Протокол утверждён · v1")).toBeVisible();
  for (const format of ["DOCX", "PDF"]) {
    const downloaded = page.waitForEvent("download");
    await page
      .getByRole("button", { name: `↓ ${format}`, exact: true })
      .click();
    expect((await downloaded).suggestedFilename()).toMatch(
      new RegExp(`\\.${format.toLowerCase()}$`),
    );
  }
  await page.getByRole("link", { name: "Мои поручения", exact: true }).click();
  await expect(
    page.getByText("Подготовить отчёт — есеп", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Отметить выполненным" }).click();
  await expect(page.getByText("Выполнено", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Выполнено", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: /Уведомления/ }).click();
  await expect(
    page.getByText("Вам назначено поручение", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Прочитать все" }).click();
  await expect(page.getByText("0 непрочитанных")).toBeVisible();
  await page.reload();
  await expect(page.getByText("0 непрочитанных")).toBeVisible();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
  expect(errors).toEqual([]);
  expect(state.polls).toBe(1);
});
test("ошибка извлечения не означает отсутствие поручений", async ({ page }) => {
  await fixtureApi(page, { extractionError: true });
  await login(page);
  await meeting(page);
  await upload(page);
  await expect(
    page.getByText("Извлечение поручений не удалось.", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  await expect(
    page.getByText("Поручения не найдены", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Повторить извлечение", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Утвердить протокол", exact: true }),
  ).toBeDisabled();
});
test("ошибка загрузки и пустое состояние сохраняют правдивый статус", async ({
  page,
}) => {
  await fixtureApi(page, { uploadError: true });
  await login(page);
  await meeting(page);
  await upload(page);
  await expect(
    page.getByText("Тест: файл превышает допустимый размер"),
  ).toBeVisible();
  await expect(page.getByText("Нет записи", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Загрузить и обработать" }),
  ).toBeEnabled();
});
test("голосовой профиль: уведомление, загрузка и удаление", async ({
  page,
}) => {
  await fixtureApi(page);
  await login(page);
  await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
  await page
    .getByRole("link", { name: "Әлия Қасым — тест", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Записать с микрофона" }),
  ).toBeDisabled();
  await page.getByRole("checkbox", { name: /Сотрудник уведомлён/ }).check();
  await page.getByLabel("Или загрузите образец").setInputFiles({
    name: "voice.webm",
    mimeType: "audio/webm",
    buffer: Buffer.from("FIXTURE"),
  });
  await page
    .getByRole("button", { name: "Зарегистрировать голос", exact: true })
    .click();
  await expect(
    page.getByText("Голосовой профиль обновлён", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText("Зарегистрирован", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Удалить профиль", exact: true })
    .click();
  await page.getByRole("button", { name: "Подтвердить удаление" }).click();
  await expect(
    page.getByText("Не зарегистрирован", { exact: true }),
  ).toBeVisible();
});
test("недоступный backend: ошибка подключения и повтор", async ({ page }) => {
  await page.route("**/api/**", (route) => route.abort("connectionrefused"));
  await page.goto("/");
  await expect(
    page.getByText("Не удалось загрузить данные.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Повторить", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("heading", { name: "Совещания", exact: true }),
  ).toHaveCount(0);
});
test("роль сотрудника, узкий экран и истечение сессии", async ({ page }) => {
  const state = await fixtureApi(page, { role: "employee" });
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await expect(
    page.getByRole("link", { name: "＋ Создать совещание" }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "＋ Добавить сотрудника" }),
  ).toHaveCount(0);
  state.loggedIn = false;
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Вход в DARAI" }),
  ).toBeVisible();
});

test("устаревшая версия не утверждается, правки блокируют экспорт", async ({
  page,
}) => {
  const state = await fixtureApi(page);
  await login(page);
  await meeting(page);
  await upload(page);
  await expect(
    page.getByText("Подготовить отчёт — есеп", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  await page.getByLabel("Итоги и решения").fill("Несохранённое решение");
  await expect(
    page.getByRole("button", { name: "↓ DOCX", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Утвердить протокол", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Отменить правки" }).click();
  await page.getByRole("checkbox", { name: /Я проверил/ }).check();
  state.meetings[0].draft_revision++;
  await page
    .getByRole("button", { name: "Утвердить протокол", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Подтвердить и опубликовать" })
    .click();
  await expect(
    page.getByText("Протокол изменился после загрузки страницы"),
  ).toBeVisible();
  expect(state.meetings[0].approval_status).toBe("draft");
  await page.getByRole("button", { name: "Обновить версию протокола" }).click();
  await expect(
    page.getByText("Актуальная версия загружена. Проверьте протокол ещё раз."),
  ).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /Я проверил/ }),
  ).not.toBeChecked();
});

test.describe("запись с тестового микрофона Chromium", () => {
  test("MediaRecorder создаёт локальный образец и останавливает дорожки", async ({
    page,
  }) => {
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        // Synthetic local audio source; no physical microphone or OS permissions.
        // MediaRecorder remains the real browser implementation.
        const context = new AudioContext();
        const tone = context.createOscillator();
        const output = context.createMediaStreamDestination();
        tone.connect(output);
        tone.start();
        const stream = output.stream;
        (window as unknown as { testStream: MediaStream }).testStream = stream;
        return stream;
      };
    });
    await fixtureApi(page);
    await login(page);
    await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
    await page
      .getByRole("link", { name: "Әлия Қасым — тест", exact: true })
      .click();
    await page.getByRole("checkbox", { name: /Сотрудник уведомлён/ }).check();
    await page
      .getByRole("button", { name: "Записать с микрофона", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: /Остановить запись · [1-9]/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Зарегистрировать голос", exact: true }),
    ).toBeDisabled();
    await page.getByRole("button", { name: /Остановить запись/ }).click();
    await expect(page.getByText(/voice.webm/)).toBeVisible();
    expect(
      await page.evaluate(() =>
        (window as unknown as { testStream: MediaStream }).testStream
          .getTracks()
          .every((t) => t.readyState === "ended"),
      ),
    ).toBe(true);
    await page
      .getByRole("button", { name: "Зарегистрировать голос", exact: true })
      .click();
    await expect(
      page.getByText("Голосовой профиль обновлён", { exact: true }),
    ).toBeVisible();
  });
});
