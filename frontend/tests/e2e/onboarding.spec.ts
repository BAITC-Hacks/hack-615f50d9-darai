import { test, expect, type Page } from "@playwright/test";
import { fixtureApi } from "./fixture-api";
async function login(
  page: Page,
  login = "fixture-admin",
  password = "fixture-only-password",
) {
  await page.goto("/");
  await page.getByLabel("Логин", { exact: true }).fill(login);
  await page.getByLabel("Пароль", { exact: true }).fill(password);
  await page
    .getByRole("button", { name: "Войти в рабочее пространство" })
    .click();
}
async function create(page: Page) {
  await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
  await page.getByRole("link", { name: "＋ Добавить сотрудника" }).click();
  await page.getByLabel("ФИО", { exact: true }).fill("Өмір Әли — тест");
  await page.getByLabel("Должность", { exact: true }).fill("Аналитик");
  await page.getByLabel("Департамент", { exact: true }).fill("Қаржы");
  await page
    .getByRole("checkbox", { name: "Создать учётную запись", exact: true })
    .check();
  await page.getByLabel("Логин сотрудника").fill("omir.test");
  await page
    .getByRole("button", { name: "Создать сотрудника", exact: true })
    .click();
}
async function changePassword(page: Page) {
  await expect(
    page.getByRole("heading", { name: "Задайте новый пароль" }),
  ).toBeVisible();
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await page
    .getByLabel("Текущий временный пароль")
    .fill("Temporary-fixture-123");
  await page
    .getByLabel("Новый пароль", { exact: true })
    .fill("Personal-fixture-123");
  await page.getByLabel("Повторите новый пароль").fill("Personal-fixture-123");
  await page
    .getByRole("button", { name: "Сменить пароль", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Мой профиль", exact: true }),
  ).toBeVisible();
}
async function sample(page: Page) {
  await page.getByRole("checkbox", { name: /Сотрудник уведомлён/ }).check();
  await page
    .getByLabel("Или загрузите образец")
    .setInputFiles({
      name: "test.wav",
      mimeType: "audio/wav",
      buffer: Buffer.from("SYNTHETIC FIXTURE"),
    });
}
test("доступ → обязательная смена → свой профиль → голос → отказ замены → сброс", async ({
  page,
}) => {
  const state = await fixtureApi(page);
  await login(page);
  await create(page);
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByLabel("Временный пароль", { exact: true }),
  ).toHaveValue("Temporary-fixture-123");
  await expect(page.getByLabel("Выданный логин")).toHaveValue("omir.test");
  expect(state.accounts[0].role).toBe("employee");
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Записать с микрофона" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await login(page, "omir.test", "Temporary-fixture-123");
  await page.reload();
  await changePassword(page);
  await page.getByRole("button", { name: "Зарегистрировать позже" }).click();
  await expect(
    page.getByText("Вы можете работать без голосового профиля.", {
      exact: false,
    }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Мои поручения", exact: true }).click();
  await page.getByRole("link", { name: "Мой профиль", exact: true }).click();
  await sample(page);
  await expect(page.getByLabel("Прослушать голосовой образец")).toBeVisible();
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
  state.qualityError = true;
  await sample(page);
  await page
    .getByRole("button", { name: "Перерегистрировать голос", exact: true })
    .click();
  await expect(page.getByText(/Прочитайте текст целиком/)).toBeVisible();
  await expect(
    page.getByText("Зарегистрирован", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await login(page);
  await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
  await page
    .getByRole("link", { name: "Өмір Әли — тест", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Сбросить пароль", exact: true })
    .click();
  await page.getByRole("button", { name: "Подтвердить сброс пароля" }).click();
  await expect(
    page.getByLabel("Временный пароль", { exact: true }),
  ).toHaveValue("Reset-fixture-456");
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  expect(state.accounts[0].must_change_password).toBe(true);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  await page.screenshot({
    path: "test-results/onboarding-access.png",
    fullPage: true,
  });
});
test("сбой выдачи аккаунта повторяет только выдачу, без дубликата сотрудника", async ({
  page,
}) => {
  const state = await fixtureApi(page);
  state.failAccountOnce = true;
  await login(page);
  await create(page);
  await expect(
    page.getByText("Этот логин уже занят", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Повторить выдачу доступа" }).click();
  await expect(
    page.getByLabel("Временный пароль", { exact: true }),
  ).toBeVisible();
  expect(state.employees).toHaveLength(2);
  expect(
    state.requests.filter(
      (r) => r.path === "/employees" && r.method === "POST",
    ),
  ).toHaveLength(1);
  expect(
    state.requests.filter((r) => r.path === "/users" && r.method === "POST"),
  ).toHaveLength(2);
});
test("запрет микрофона объяснён, загрузка остаётся доступна", async ({
  page,
}) => {
  await fixtureApi(page, { role: "employee" });
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException("denied", "NotAllowedError");
    };
  });
  await login(page);
  await page.getByRole("checkbox", { name: /Сотрудник уведомлён/ }).check();
  await page.getByRole("button", { name: "Записать с микрофона" }).click();
  await expect(page.getByText(/Доступ к микрофону запрещён/)).toBeVisible();
  await expect(page.getByLabel("Или загрузите образец")).toBeEnabled();
});
