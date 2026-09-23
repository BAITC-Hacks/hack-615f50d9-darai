import { test, expect, type Page } from "@playwright/test";
import { fixtureApi } from "./fixture-api";
import type { Employee, Role } from "../../src/types";
function seed(state: Awaited<ReturnType<typeof fixtureApi>>, count = 1) {
  const template = state.employees[0];
  const result: Employee[] = [];
  for (let n = 1; n <= count; n++) {
    const employee: Employee = {
      ...structuredClone(template),
      id: `10000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
      fio: `Архивируемый сотрудник ${String(n).padStart(3, "0")}`,
      has_account: false,
      user_id: null,
      can_delete: true,
    };
    state.employees.push(employee);
    result.push(employee);
  }
  return result;
}
async function openList(page: Page) {
  await page.goto("/");
  await page.getByLabel("Логин", { exact: true }).fill("fixture-admin");
  await page
    .getByLabel("Пароль", { exact: true })
    .fill("fixture-only-password");
  await page
    .getByRole("button", { name: "Войти в рабочее пространство" })
    .click();
  await page.getByRole("link", { name: "Сотрудники", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Сотрудники", exact: true }),
  ).toBeVisible();
}
const trigger = (page: Page, e: Employee) =>
  page.getByRole("button", {
    name: `Удалить сотрудника ${e.fio}`,
    exact: true,
  });
const deletes = (state: Awaited<ReturnType<typeof fixtureApi>>) =>
  state.requests.filter(
    (r) => r.method === "DELETE" && /^\/employees\/[^/]+$/.test(r.path),
  );

test("секретарь: обычный сотрудник доступен для удаления; свой, admin и secretary — нет", async ({
  page,
}) => {
  const state = await fixtureApi(page, { role: "secretary" });
  const [normal, admin, secretary] = seed(state, 3);
  state.employeeRoles[admin.id] = "admin";
  state.employeeRoles[secretary.id] = "secretary";
  await openList(page);
  await expect(trigger(page, normal)).toBeVisible();
  await expect(trigger(page, admin)).toHaveCount(0);
  await expect(trigger(page, secretary)).toHaveCount(0);
  await expect(trigger(page, state.employees[0])).toHaveCount(0);
  await page
    .getByRole("link", { name: state.employees[0].fio, exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Удалить сотрудника", exact: true }),
  ).toHaveCount(0);
});
for (const role of ["employee", "admin"] as Role[])
  test(`${role}: собственную карточку удалить нельзя`, async ({ page }) => {
    const state = await fixtureApi(page, { role });
    const [target] = seed(state);
    await openList(page);
    if (role === "employee")
      await expect(
        page.getByRole("button", { name: /Удалить сотрудника/ }),
      ).toHaveCount(0);
    else await expect(trigger(page, target)).toBeVisible();
    await page
      .getByRole("link", { name: state.employees[0].fio, exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Удалить сотрудника", exact: true }),
    ).toHaveCount(0);
  });
test("старый ответ без can_delete запрещает удаление", async ({ page }) => {
  const state = await fixtureApi(page, { legacyEmployeeResponse: true });
  const [target] = seed(state);
  await openList(page);
  await expect(
    page.getByRole("button", { name: /Удалить сотрудника/ }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: target.fio, exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Удалить сотрудника", exact: true }),
  ).toHaveCount(0);
});
test("диалог доступен с клавиатуры; Escape и Отмена не отправляют DELETE и возвращают фокус", async ({
  page,
}) => {
  const state = await fixtureApi(page, { role: "secretary" });
  const [target] = seed(state);
  await openList(page);
  await trigger(page, target).focus();
  await page.keyboard.press("Enter");
  const modal = page.getByRole("dialog", { name: "Удалить сотрудника?" });
  await expect(modal).toBeVisible();
  await page.screenshot({ path: "test-results/employee-delete-dialog.png" });
  await expect(modal).toContainText(target.fio);
  await expect(modal).toContainText("История совещаний и поручений сохранится");
  await expect(
    page.getByRole("button", { name: "Отмена", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    modal.getByRole("button", { name: "Удалить сотрудника", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(modal).toHaveCount(0);
  await expect(trigger(page, target)).toBeFocused();
  expect(page.url()).toContain("/employees");
  await trigger(page, target).click();
  await page.getByRole("button", { name: "Отмена", exact: true }).click();
  expect(deletes(state)).toHaveLength(0);
});
test("204: задержка блокирует повторные запросы; строка исчезает только после ответа", async ({
  page,
}) => {
  const state = await fixtureApi(page, {
    role: "secretary",
    deleteDelayMs: 800,
  });
  const [target] = seed(state);
  state.accounts.push({
    id: "archived-account",
    employee_id: target.id,
    employee_fio: target.fio,
    login: "archive.fixture",
    role: "employee",
    active: true,
    must_change_password: false,
    created_at: "2026-09-23T00:00:00Z",
    fixturePassword: "fixture-only",
  });
  target.user_id = "archived-account";
  target.has_account = true;
  await openList(page);
  await trigger(page, target).click();
  const confirm = page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить сотрудника", exact: true });
  await confirm.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(confirm).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Отмена", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("Удаляем сотрудника…", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: target.fio, exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: target.fio, exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("1 в справочнике", { exact: true }),
  ).toBeVisible();
  expect(deletes(state)).toHaveLength(1);
  expect(deletes(state)[0].body).toBeNull();
  expect(target.active).toBe(false);
  expect(state.accounts[0].active).toBe(false);
  expect(state.identificationExcluded).toContain(target.id);
  await expect(
    page.getByText(/удалён из активного справочника. История сохранена/),
  ).toBeVisible();
});
for (const error of [
  {
    status: 403,
    code: "FORBIDDEN",
    message: "Доступ запрещён",
    shown: "У вас нет прав на удаление этого сотрудника.",
  },
  {
    status: 409,
    code: "SELF_DELETE_FORBIDDEN",
    message: "Нельзя удалить себя",
    shown: "Нельзя удалить собственную карточку сотрудника.",
  },
  {
    status: 409,
    code: "LAST_ADMIN",
    message: "Последний администратор",
    shown: "Нельзя удалить последнего администратора системы.",
  },
  {
    status: 500,
    code: "INTERNAL_ERROR",
    message: "Не удалось выполнить удаление. Повторите позже.",
    shown: "Не удалось выполнить удаление. Повторите позже.",
  },
])
  test(`ошибка ${error.status} ${error.code}: диалог остаётся, доступен повтор`, async ({
    page,
  }) => {
    const state = await fixtureApi(page);
    const [target] = seed(state);
    state.deleteError = error;
    await openList(page);
    await trigger(page, target).click();
    const confirm = page
      .getByRole("dialog")
      .getByRole("button", { name: "Удалить сотрудника", exact: true });
    await confirm.click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByText(error.shown, { exact: true })).toBeVisible();
    await expect(confirm).toBeEnabled();
    expect(target.active).toBe(true);
    await expect(
      page.getByRole("link", { name: target.fio, exact: true }),
    ).toBeVisible();
    state.deleteError = null;
    await confirm.click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(deletes(state)).toHaveLength(2);
  });
test("404 обновляет список и объясняет недоступность без фиктивного успеха", async ({
  page,
}) => {
  const state = await fixtureApi(page);
  const [target] = seed(state);
  state.deleteError = {
    status: 404,
    code: "NOT_FOUND",
    message: "Нет сотрудника",
  };
  await openList(page);
  await trigger(page, target).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Сотрудник больше недоступен",
  );
  await expect(
    page.getByRole("link", { name: target.fio, exact: true }),
  ).toHaveCount(0);
  await expect(
    page
      .getByRole("dialog")
      .getByRole("button", { name: "Удалить сотрудника", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await expect(
    page.getByText("1 в справочнике", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/удалён из активного справочника. История сохранена/),
  ).toHaveCount(0);
});
test("последняя строка страницы: предыдущая страница и сохранённый поиск", async ({
  page,
}) => {
  const state = await fixtureApi(page, { role: "secretary" });
  const targets = seed(state, 51);
  await openList(page);
  await page.getByLabel("Поиск сотрудников").fill("Архивируемый");
  await page.getByRole("button", { name: "Найти", exact: true }).click();
  await expect(
    page.getByText("51 в справочнике", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Далее →", exact: true }).click();
  await expect(page.getByText("51–51 из 51", { exact: true })).toBeVisible();
  await trigger(page, targets[50]).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await expect(page.getByText("1–50 из 50", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Поиск сотрудников")).toHaveValue(
    "Архивируемый",
  );
  await expect(
    page.getByRole("button", { name: "← Назад", exact: true }),
  ).toBeDisabled();
  expect(new URL(page.url()).searchParams.get("q")).toBe("Архивируемый");
});
test("карточка: успех возвращает в отфильтрованный список и сохраняет сообщение", async ({
  page,
}) => {
  const state = await fixtureApi(page, { role: "secretary" });
  const [target] = seed(state);
  await openList(page);
  await page.getByLabel("Поиск сотрудников").fill("Архивируемый");
  await page.getByRole("button", { name: "Найти", exact: true }).click();
  await page.getByRole("link", { name: target.fio, exact: true }).click();
  await page
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Сотрудники", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Поиск сотрудников")).toHaveValue(
    "Архивируемый",
  );
  await expect(
    page.getByText("Сотрудники не найдены", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("0 записей", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/удалён из активного справочника. История сохранена/),
  ).toBeVisible();
});
test("последний активный сотрудник: пустой справочник и total=0", async ({
  page,
}) => {
  const state = await fixtureApi(page);
  const [target] = seed(state);
  state.user.employee = null;
  state.employees.splice(0, 1);
  await openList(page);
  await trigger(page, target).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await expect(
    page.getByText("Справочник пока пуст", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("0 в справочнике", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Далее →", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "← Назад", exact: true }),
  ).toBeDisabled();
});

test("повторный DELETE архивированного сотрудника возвращает пустой 204", async ({
  page,
}) => {
  const state = await fixtureApi(page, { role: "secretary" });
  const [target] = seed(state);
  await openList(page);
  await trigger(page, target).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const repeated = await page.evaluate(async (id) => {
    const response = await fetch(`/api/employees/${id}`, {
      method: "DELETE",
      credentials: "include",
      headers: { "X-CSRF-Token": "fixture-csrf" },
    });
    return { status: response.status, body: await response.text() };
  }, target.id);
  expect(repeated).toEqual({ status: 204, body: "" });
  expect(
    state.identificationExcluded.filter((id) => id === target.id),
  ).toHaveLength(1);
});

test("404 в карточке: сообщение остаётся до закрытия, затем открывается обновлённый список", async ({
  page,
}) => {
  const state = await fixtureApi(page, { role: "secretary" });
  const [target] = seed(state);
  await openList(page);
  await page.getByRole("link", { name: target.fio, exact: true }).click();
  state.deleteError = {
    status: 404,
    code: "NOT_FOUND",
    message: "Нет сотрудника",
  };
  await page
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить сотрудника", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Сотрудник больше недоступен",
  );
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Сотрудники", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: target.fio, exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Сотрудник больше недоступен.", { exact: true }),
  ).toBeVisible();
});
