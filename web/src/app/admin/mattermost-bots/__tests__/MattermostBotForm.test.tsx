import { render, screen, setupUser, waitFor } from "@tests/setup/test-utils";
import { MattermostBotForm } from "../MattermostBotForm";

jest.mock("@opal/layouts", () => ({
  toast: {
    error: jest.fn(),
    success: jest.fn(),
  },
}));

afterEach(() => {
  jest.restoreAllMocks();
});

test("creates a Mattermost bot through the managed API without leaking the token", async () => {
  const user = setupUser();
  const fetchSpy = jest.spyOn(global, "fetch");
  const onSaved = jest.fn();
  fetchSpy.mockResolvedValueOnce({
    ok: true,
    json: async () => ({
      id: 1,
      name: "prod mattermost",
      url: "https://mattermost.example.com",
      enabled: true,
      token: "",
      bot_user_id: "bot-user",
      bot_username: "onyxbot",
      health_status: "ok",
      health_error: null,
    }),
  } as Response);

  render(<MattermostBotForm onSaved={onSaved} />);
  await user.type(screen.getByLabelText(/mattermost bot name/i), "prod mattermost");
  await user.type(
    screen.getByLabelText(/mattermost server url/i),
    "https://mattermost.example.com"
  );
  await user.type(screen.getByLabelText(/mattermost bot token/i), "secret-token");
  await user.click(screen.getByRole("button", { name: /create/i }));

  await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
  expect(fetchSpy).toHaveBeenCalledWith(
    "/api/manage/admin/mattermost-app/bots",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        name: "prod mattermost",
        url: "https://mattermost.example.com",
        enabled: true,
        token: "secret-token",
      }),
    })
  );
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ token: "" })));
});

test("updates Mattermost bot metadata without rotating token when the token field is blank", async () => {
  const user = setupUser();
  const fetchSpy = jest.spyOn(global, "fetch");
  fetchSpy.mockResolvedValueOnce({
    ok: true,
    json: async () => ({
      id: 7,
      name: "renamed",
      url: "https://mattermost.example.com/team",
      enabled: false,
      token: "",
      bot_user_id: "bot-user",
      bot_username: "onyxbot",
      health_status: "ok",
      health_error: null,
    }),
  } as Response);

  render(
    <MattermostBotForm
      existingMattermostBot={{
        id: 7,
        name: "existing",
        url: "https://mattermost.example.com",
        enabled: true,
        token: "",
        bot_user_id: "bot-user",
        bot_username: "onyxbot",
        health_status: "ok",
        health_error: null,
      }}
    />
  );

  await user.clear(screen.getByLabelText(/mattermost bot name/i));
  await user.type(screen.getByLabelText(/mattermost bot name/i), "renamed");
  await user.click(screen.getByLabelText(/enabled/i));
  await user.click(screen.getByRole("button", { name: /update/i }));

  await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
  expect(fetchSpy).toHaveBeenCalledWith(
    "/api/manage/admin/mattermost-app/bots/7",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({
        name: "renamed",
        url: "https://mattermost.example.com",
        enabled: false,
      }),
    })
  );
});
