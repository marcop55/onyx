import { expect, type Page } from "@playwright/test";

const MATTERMOST_BOTS_URL = "**/api/manage/admin/mattermost-app/bots";
const MATTERMOST_CHANNEL_URL = "**/api/manage/admin/mattermost-app/channel**";
const MATTERMOST_BOT_CONFIG_URL =
  "**/api/manage/admin/mattermost-app/bots/7/config";
const MATTERMOST_OBSERVABILITY_URL =
  "**/api/manage/admin/mattermost-app/bots/7/observability";

export class MattermostBotsAdminPage {
  constructor(private readonly page: Page) {}

  async mockParityRoutes(): Promise<void> {
    await this.page.route("**/api/settings", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ notifications: [] }),
      });
    });

    await this.page.route("**/api/llm/provider", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          providers: [
            {
              name: "openai",
              provider: "openai",
              model_configurations: [
                { name: "gpt-4o", is_visible: true, is_default: true },
              ],
            },
          ],
          default_text: "gpt-4o",
          default_vision: "gpt-4o",
        }),
      });
    });

    await this.page.route(MATTERMOST_BOTS_URL, async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 403,
          contentType: "application/json",
          body: JSON.stringify({
            detail:
              "Mattermost identity validation failed: bot and sender membership must be current",
          }),
        });
        return;
      }

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 7,
            name: "OneQode Mattermost",
            url: "https://mattermost.example.test",
            enabled: true,
            bot_user_id: "bot-user-1",
            bot_username: "orka",
            health_status: "ok",
            health_error: null,
          },
        ]),
      });
    });

    await this.page.route(MATTERMOST_CHANNEL_URL, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 71,
            mattermost_bot_id: 7,
            channel_id: null,
            channel_name: null,
            enabled: true,
            is_default: true,
            is_ephemeral: false,
            persona: { id: 456, name: "Mattermost Agent" },
            channel_config: {
              response_style: "orka_concise",
              response_type: "citations",
              respond_tag_only: true,
              include_source_previews: true,
              answer_filters: ["well_answered_postfilter"],
              standard_answer_category_ids: [12],
              follow_up_tags: ["needs-follow-up"],
              disabled: false,
            },
          },
          {
            id: 72,
            mattermost_bot_id: 7,
            channel_id: "private-channel-1",
            channel_name: "exec-private",
            enabled: true,
            is_default: false,
            is_ephemeral: true,
            persona: null,
            channel_config: { channel_name: "exec-private" },
          },
        ]),
      });
    });

    await this.page.route(MATTERMOST_BOT_CONFIG_URL, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 71,
            mattermost_bot_id: 7,
            channel_id: null,
            channel_name: null,
            enabled: true,
            is_default: true,
            is_ephemeral: false,
            persona: { id: 456, name: "Mattermost Agent" },
            channel_config: {
              response_style: "orka_concise",
              response_type: "citations",
              respond_tag_only: true,
              include_source_previews: true,
              answer_filters: ["well_answered_postfilter"],
              standard_answer_category_ids: [12],
              follow_up_tags: ["needs-follow-up"],
              disabled: false,
            },
          },
        ]),
      });
    });

    await this.page.route(MATTERMOST_OBSERVABILITY_URL, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          bot_id: 7,
          bot_name: "OneQode Mattermost",
          instance_id: "https://mattermost.example.test",
          bot_username: "orka",
          health_status: "ok",
          health_error: null,
          delivery: {
            total_events: 9,
            completed_events: 8,
            in_progress_events: 1,
            replayable_events: 2,
            attachment_failure_events: 1,
            rate_limited_events: 0,
          },
          joined_channels: [
            {
              id: "channel-1",
              name: "oneqode",
              display_name: "OneQode",
              bot_is_member: true,
            },
          ],
          indexing: {
            total_docs_indexed: 42,
            latest_successful_index_time: "2026-08-15T19:00:00Z",
            connectors: [{ id: 33, name: "Mattermost history" }],
          },
        }),
      });
    });

    await this.page.route("**/api/persona", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 456,
            name: "Mattermost Agent",
            description: "Agent used for Mattermost parity coverage",
            tools: [],
            starter_messages: null,
            document_sets: [],
            is_public: false,
            is_listed: true,
            display_priority: null,
            is_featured: false,
            builtin_persona: false,
            labels: [],
            owner: null,
            owner_group: null,
            user_permission: "OWNER",
          },
        ]),
      });
    });
  }

  async goto(): Promise<void> {
    await this.page.goto("/admin/mattermost-bots");
    await this.page.waitForURL("**/admin/mattermost-bots");
  }

  async gotoHealth(): Promise<void> {
    await this.page.goto("/admin/mattermost-bots/7/health");
    await this.page.waitForURL("**/admin/mattermost-bots/7/health");
  }

  async openExistingBotEditor(): Promise<void> {
    await this.page
      .getByRole("row", { name: /OneQode Mattermost/ })
      .getByRole("button", { name: "Edit" })
      .click();
  }

  async openCreateForm(): Promise<void> {
    await this.page.getByRole("button", { name: "Add Mattermost Bot" }).click();
  }

  async submitRejectedBot(): Promise<void> {
    await this.page.getByLabel("Mattermost Bot Name").fill("Rejected bot");
    await this.page
      .getByLabel("Mattermost Server URL")
      .fill("https://mattermost.example.test");
    await this.page.getByLabel("Mattermost Bot Token").fill("test-token");
    await this.page.getByRole("button", { name: "Create" }).click();
  }

  async expectConfigurationParityControls(): Promise<void> {
    await expect(this.page.getByText("Mattermost Integration")).toBeVisible();
    await expect(this.page.getByText("OneQode Mattermost")).toBeVisible();
    await expect(this.page.getByText("Enabled").first()).toBeVisible();
    await expect(this.page.getByText("ok", { exact: true })).toBeVisible();
    await expect(this.page.getByText("Private answer channels")).toBeVisible();
    await expect(this.page.getByText("private-channel-1")).toBeVisible();
    await expect(
      this.page.getByText("Mattermost channel behaviour")
    ).toBeVisible();
    await expect(
      this.page.getByRole("heading", { name: "Default Mattermost behaviour" })
    ).toBeVisible();
    await expect(this.page.getByText("Response style")).toBeVisible();
    await expect(this.page.getByText("Sources display")).toBeVisible();
    await expect(
      this.page.getByText("Answer only when sources are found")
    ).toBeVisible();
    await expect(
      this.page.getByText("Standard answer category IDs", { exact: true })
    ).toBeVisible();
    await expect(
      this.page.getByText("Follow-up tags", { exact: true })
    ).toBeVisible();
    await expect(this.page.getByText("Respond only when tagged")).toBeVisible();
  }

  async expectHealthParityControls(): Promise<void> {
    await expect(
      this.page.getByText("Connection", { exact: true })
    ).toBeVisible();
    await expect(
      this.page.getByText("Delivery and replay", { exact: true })
    ).toBeVisible();
    await expect(
      this.page.getByText("Replayable", { exact: true })
    ).toBeVisible();
    await expect(
      this.page.getByText("Attachment failures", { exact: true })
    ).toBeVisible();
    await expect(
      this.page.getByText("Joined channels", { exact: true })
    ).toBeVisible();
    await expect(this.page.getByText("OneQode", { exact: true })).toBeVisible();
    await expect(
      this.page.getByText("Indexing freshness", { exact: true })
    ).toBeVisible();
    await expect(
      this.page.getByText("Indexed documents", { exact: true })
    ).toBeVisible();
  }

  async expectMembershipFailureToast(): Promise<void> {
    await expect(
      this.page
        .getByTestId("toast-container")
        .getByText(/bot and sender membership must be current/)
    ).toBeVisible();
  }
}
