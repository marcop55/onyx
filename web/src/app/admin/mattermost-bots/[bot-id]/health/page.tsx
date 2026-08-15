"use client";

import { errorHandlingFetcher } from "@/lib/fetcher";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { SWR_KEYS } from "@/lib/swr-keys";
import type { MattermostObservabilitySnapshot } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Text } from "@opal/components";
import { SettingsLayouts } from "@opal/layouts";
import { useParams } from "next/navigation";
import useSWR from "swr";

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border border-border bg-background p-3">
      <Text as="p" font="secondary-body" color="text-03">
        {label}
      </Text>
      <Text as="p" font="heading-h3" color="text-05">
        {String(value)}
      </Text>
    </div>
  );
}

export default function MattermostBotHealthPage() {
  const params = useParams<{ "bot-id": string }>();
  const botId = Number(params["bot-id"]);
  const route = ADMIN_ROUTES.MATTERMOST_BOTS;
  const { data, isLoading } = useSWR<MattermostObservabilitySnapshot>(
    Number.isFinite(botId) ? SWR_KEYS.mattermostBotObservability(botId) : null,
    errorHandlingFetcher
  );

  if (isLoading || !data) {
    return (
      <SettingsLayouts.Root>
        <SettingsLayouts.Header
          icon={route.icon}
          title="Mattermost health"
          description="Loading Mattermost adapter observability..."
          backButton
        />
      </SettingsLayouts.Root>
    );
  }

  return (
    <SettingsLayouts.Root>
      <SettingsLayouts.Header
        icon={route.icon}
        title={`${data.bot_name} health`}
        description="Connection health, delivery/replay state, attachment failures, joined channels and indexing freshness."
        backButton
      />
      <SettingsLayouts.Body>
        <div className="grid gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Connection</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3 md:grid-cols-3">
              <Stat label="Instance" value={data.instance_id} />
              <Stat label="Bot username" value={data.bot_username} />
              <div className="rounded-md border border-border bg-background p-3">
                <Text as="p" font="secondary-body" color="text-03">
                  Health
                </Text>
                <Badge
                  variant={
                    data.health_status === "ok" ? "success" : "secondary"
                  }
                >
                  {data.health_status}
                </Badge>
                {data.health_error && (
                  <Text as="p" font="secondary-body" color="text-03">
                    {data.health_error}
                  </Text>
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Delivery and replay</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3 md:grid-cols-3">
              <Stat label="Recent events" value={data.delivery.total_events} />
              <Stat label="Completed" value={data.delivery.completed_events} />
              <Stat
                label="In progress"
                value={data.delivery.in_progress_events}
              />
              <Stat
                label="Replayable"
                value={data.delivery.replayable_events}
              />
              <Stat
                label="Attachment failures"
                value={data.delivery.attachment_failure_events}
              />
              <Stat
                label="Rate limited"
                value={data.delivery.rate_limited_events}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Joined channels</CardTitle>
            </CardHeader>
            <CardContent>
              {data.joined_channels.length === 0 ? (
                <Text as="p" font="main-ui-body" color="text-03">
                  {data.health_error
                    ? "Joined-channel discovery failed. Check the connection health error above."
                    : "No joined channels were returned by Mattermost."}
                </Text>
              ) : (
                <div className="grid gap-2">
                  {data.joined_channels.map((channel) => (
                    <div
                      className="flex items-center justify-between rounded-md border border-border p-3"
                      key={channel.id}
                    >
                      <Text as="span" font="main-ui-action" color="text-05">
                        {channel.display_name || channel.name}
                      </Text>
                      <Badge
                        variant={
                          channel.bot_is_member ? "success" : "destructive"
                        }
                      >
                        {channel.bot_is_member ? "member" : "not a member"}
                      </Badge>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Indexing freshness</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3 md:grid-cols-3">
              <Stat
                label="Indexed documents"
                value={data.indexing.total_docs_indexed}
              />
              <Stat
                label="History connectors"
                value={data.indexing.connectors.length}
              />
              <Stat
                label="Latest success"
                value={data.indexing.latest_successful_index_time ?? "never"}
              />
            </CardContent>
          </Card>
        </div>
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
