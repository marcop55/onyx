import { render, screen, waitFor } from "@tests/setup/test-utils";
import MattermostBotHealthPage from "../page";

jest.mock("next/navigation", () => ({
  useParams: () => ({ "bot-id": "7" }),
  useRouter: () => ({ back: jest.fn(), push: jest.fn() }),
}));

afterEach(() => {
  jest.restoreAllMocks();
});

test("renders Mattermost health, delivery, channel and indexing observability", async () => {
  const fetchSpy = jest.spyOn(global, "fetch");
  fetchSpy.mockResolvedValueOnce({
    status: 200,
    ok: true,
    json: async () => ({
      bot_id: 7,
      bot_name: "prod mattermost",
      instance_id: "https://mattermost.example.com/team",
      enabled: true,
      bot_user_id: "bot-user",
      bot_username: "onyxbot",
      health_status: "ok",
      health_error: null,
      joined_channels: [
        {
          id: "channel-1",
          name: "town-square",
          display_name: "Town Square",
          bot_is_member: true,
        },
      ],
      delivery: {
        total_events: 6,
        completed_events: 4,
        in_progress_events: 1,
        replayable_events: 1,
        attachment_failure_events: 1,
        rate_limited_events: 1,
        latest_event_at: "2026-08-15T10:00:00Z",
        by_event_type: { channel_mention: 6 },
      },
      indexing: {
        connectors: [
          {
            id: 3,
            name: "mattermost history",
            status: "ACTIVE",
            last_successful_index_time: "2026-08-15T09:30:00Z",
            total_docs_indexed: 42,
            in_repeated_error_state: false,
          },
        ],
        latest_successful_index_time: "2026-08-15T09:30:00Z",
        total_docs_indexed: 42,
      },
    }),
  } as Response);

  render(<MattermostBotHealthPage />);

  await waitFor(() =>
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/manage/admin/mattermost-app/bots/7/observability"
    )
  );
  expect(await screen.findByText("prod mattermost health")).toBeInTheDocument();
  expect(
    screen.getByText("https://mattermost.example.com/team")
  ).toBeInTheDocument();
  expect(screen.getByText("Replayable")).toBeInTheDocument();
  expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(3);
  expect(screen.getByText("Town Square")).toBeInTheDocument();
  expect(screen.getByText("member")).toBeInTheDocument();
  expect(screen.getByText("Indexed documents")).toBeInTheDocument();
  expect(screen.getByText("42")).toBeInTheDocument();
});

test("renders joined-channel discovery failures separately from empty membership", async () => {
  const fetchSpy = jest.spyOn(global, "fetch");
  fetchSpy.mockResolvedValueOnce({
    status: 200,
    ok: true,
    json: async () => ({
      bot_id: 7,
      bot_name: "prod mattermost",
      instance_id: "https://mattermost.example.com/team",
      enabled: true,
      bot_user_id: "bot-user",
      bot_username: "onyxbot",
      health_status: "error",
      health_error: "Mattermost joined-channel discovery failed",
      joined_channels: [],
      delivery: {
        total_events: 0,
        completed_events: 0,
        in_progress_events: 0,
        replayable_events: 0,
        attachment_failure_events: 0,
        rate_limited_events: 0,
        latest_event_at: null,
        by_event_type: {},
      },
      indexing: {
        connectors: [],
        latest_successful_index_time: null,
        total_docs_indexed: 0,
      },
    }),
  } as Response);

  render(<MattermostBotHealthPage />);

  expect(
    await screen.findByText("Mattermost joined-channel discovery failed")
  ).toBeInTheDocument();
  expect(
    screen.getByText(
      "Joined-channel discovery failed. Check the connection health error above."
    )
  ).toBeInTheDocument();
  expect(
    screen.queryByText("No joined channels were returned by Mattermost.")
  ).not.toBeInTheDocument();
  expect(fetchSpy).toHaveBeenCalledWith(
    "/api/manage/admin/mattermost-app/bots/7/observability"
  );
});
