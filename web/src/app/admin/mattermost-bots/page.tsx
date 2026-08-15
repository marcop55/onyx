"use client";

import { errorHandlingFetcher } from "@/lib/fetcher";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { SWR_KEYS } from "@/lib/swr-keys";
import type { MattermostBot } from "@/lib/types";
import { Button } from "@opal/components";
import { SettingsLayouts, toast } from "@opal/layouts";
import { useState } from "react";
import useSWR from "swr";
import { MattermostChannelConfigPanel } from "./MattermostChannelConfigPanel";
import { MattermostBotForm } from "./MattermostBotForm";
import { MattermostBotTable } from "./MattermostBotTable";

export default function MattermostBotsPage() {
  const route = ADMIN_ROUTES.MATTERMOST_BOTS;
  const [editingBot, setEditingBot] = useState<MattermostBot | undefined>();
  const [isCreating, setIsCreating] = useState(false);
  const { data, isLoading, mutate } = useSWR<MattermostBot[]>(
    SWR_KEYS.mattermostBots,
    errorHandlingFetcher
  );

  const mattermostBots = data ?? [];

  const onSaved = async () => {
    setEditingBot(undefined);
    setIsCreating(false);
    await mutate();
  };

  const deleteBot = async (mattermostBot: MattermostBot) => {
    const response = await fetch(SWR_KEYS.mattermostBot(mattermostBot.id), {
      method: "DELETE",
    });
    if (!response.ok) {
      const responseJson = await response.json();
      toast.error(
        `Error deleting Mattermost Bot - ${
          responseJson.detail || responseJson.message || "unknown error"
        }`
      );
      return;
    }
    toast.success("Mattermost bot deleted successfully");
    await mutate();
  };

  return (
    <SettingsLayouts.Root>
      <SettingsLayouts.Header
        icon={route.icon}
        title={route.title}
        description="Manage encrypted Mattermost bot instances without relying on environment variables as the steady-state control surface."
        rightChildren={
          <Button onClick={() => setIsCreating(true)}>
            Add Mattermost Bot
          </Button>
        }
      />
      <SettingsLayouts.Body>
        {(isCreating || editingBot) && (
          <div className="space-y-6">
            <MattermostBotForm
              existingMattermostBot={editingBot}
              onSaved={onSaved}
            />
            {editingBot && (
              <MattermostChannelConfigPanel mattermostBotId={editingBot.id} />
            )}
          </div>
        )}
        {isLoading ? (
          <div>Loading Mattermost bots...</div>
        ) : (
          <MattermostBotTable
            mattermostBots={mattermostBots}
            onDelete={deleteBot}
            onEdit={(mattermostBot) => {
              setIsCreating(false);
              setEditingBot(mattermostBot);
            }}
          />
        )}
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
