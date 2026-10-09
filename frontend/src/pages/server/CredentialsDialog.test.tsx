import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, renderWithProviders } from "../../test/render";
import { CredentialsDialog } from "./CredentialsDialog";

const ID = "11111111-1111-4111-8111-111111111111";
const REPLACE_LABEL = /Заменить пароль ключом панели/;

interface Recorded {
  method: string;
  path: string;
  body: unknown;
}

/** Подменный API: запоминает каждый запрос вместе с телом. */
function mockApi() {
  const calls: Recorded[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const text = await request.text();
      calls.push({
        method: request.method,
        path: new URL(request.url).pathname,
        body: text ? JSON.parse(text) : null,
      });
      return json({ id: "j1", status: "pending" }, 202);
    }),
  );
  return calls;
}

function renderDialog() {
  return renderWithProviders(<CredentialsDialog serverId={ID} open onOpenChange={() => {}} />);
}

async function fillPassword(user: ReturnType<typeof userEvent.setup>) {
  await user.clear(screen.getByLabelText("Пользователь"));
  await user.type(screen.getByLabelText("Пользователь"), "deploy");
  await user.type(screen.getByLabelText("Пароль"), "s3cret-pass");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("замена пароля ключом панели включена по умолчанию", () => {
  mockApi();
  renderDialog();
  expect(screen.getByRole("checkbox", { name: REPLACE_LABEL })).toBeChecked();
});

test("с галочкой отправляет POST /ssh-key с логином и паролем, а не PUT /credentials", async () => {
  const calls = mockApi();
  const user = userEvent.setup();
  renderDialog();
  await fillPassword(user);
  await user.click(screen.getByRole("button", { name: "Сохранить доступ" }));

  await waitFor(() => expect(calls).toHaveLength(1));
  expect(calls[0]).toEqual({
    method: "POST",
    path: `/api/v1/servers/${ID}/ssh-key`,
    body: { username: "deploy", password: "s3cret-pass" },
  });
});

test("без галочки сохраняет пароль как раньше: PUT /credentials", async () => {
  const calls = mockApi();
  const user = userEvent.setup();
  renderDialog();
  await fillPassword(user);
  await user.click(screen.getByRole("checkbox", { name: REPLACE_LABEL }));
  await user.click(screen.getByRole("button", { name: "Сохранить доступ" }));

  await waitFor(() => expect(calls).toHaveLength(1));
  expect(calls[0]).toEqual({
    method: "PUT",
    path: `/api/v1/servers/${ID}/credentials`,
    body: { auth_type: "password", username: "deploy", secret: "s3cret-pass" },
  });
});

test("в режиме SSH-ключа галочки нет и уходит PUT /credentials", async () => {
  const calls = mockApi();
  const user = userEvent.setup();
  renderDialog();
  await user.click(screen.getByRole("radio", { name: "по SSH-ключу" }));
  expect(screen.queryByRole("checkbox", { name: REPLACE_LABEL })).not.toBeInTheDocument();

  await user.type(screen.getByLabelText(/Приватный ключ/), "-----BEGIN OPENSSH PRIVATE KEY-----");
  await user.click(screen.getByRole("button", { name: "Сохранить доступ" }));
  await waitFor(() => expect(calls).toHaveLength(1));
  expect(calls[0]).toMatchObject({ method: "PUT", path: `/api/v1/servers/${ID}/credentials` });
});
