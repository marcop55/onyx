import { test } from "@playwright/test";
import { MattermostBotsAdminPage } from "@tests/e2e/pages/MattermostBotsAdminPage";

test.describe("Mattermost bot parity admin acceptance", () => {
  test.use({ storageState: "admin2_auth.json" });

  test("surfaces configuration, replay, health, indexing, and fallback controls", async ({
    page,
  }) => {
    const mattermostBotsPage = new MattermostBotsAdminPage(page);
    await mattermostBotsPage.mockParityRoutes();

    await mattermostBotsPage.goto();
    await mattermostBotsPage.openExistingBotEditor();
    await mattermostBotsPage.expectConfigurationParityControls();

    await mattermostBotsPage.gotoHealth();
    await mattermostBotsPage.expectHealthParityControls();
  });

  test("keeps rejected Mattermost identity and membership validation fail-closed", async ({
    page,
  }) => {
    const mattermostBotsPage = new MattermostBotsAdminPage(page);
    await mattermostBotsPage.mockParityRoutes();

    await mattermostBotsPage.goto();
    await mattermostBotsPage.openCreateForm();
    await mattermostBotsPage.submitRejectedBot();
    await mattermostBotsPage.expectMembershipFailureToast();
  });
});
