"use client";

import { TextFormField } from "@/components/Field";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import type { MattermostBot, MattermostChannelConfig } from "@/lib/types";
import { CheckboxField } from "@/refresh-components/form/LabeledCheckboxField";
import { Button } from "@opal/components";
import { toast } from "@opal/layouts";
import { Form, Formik } from "formik";
import useSWR from "swr";
import * as Yup from "yup";

interface MattermostBotFormValues {
  name: string;
  url: string;
  token: string;
  enabled: boolean;
}

interface MattermostBotFormProps {
  existingMattermostBot?: MattermostBot;
  onSaved?: (mattermostBot: MattermostBot) => void;
}

interface MattermostChannelConfigFormValues {
  channel_id: string;
  channel_enabled: boolean;
}

const MATTERMOST_BOTS_URL = "/api/manage/admin/mattermost-app/bots";
const MATTERMOST_CHANNEL_CONFIGS_URL =
  "/api/manage/admin/mattermost-app/channel";

function requestBody(
  values: MattermostBotFormValues,
  isUpdate: boolean
): string {
  return JSON.stringify({
    name: values.name,
    url: values.url,
    enabled: values.enabled,
    token: isUpdate && !values.token ? undefined : values.token,
  });
}

async function saveMattermostBot(
  values: MattermostBotFormValues,
  existingMattermostBot?: MattermostBot
): Promise<Response> {
  const isUpdate = existingMattermostBot !== undefined;
  const url = isUpdate
    ? `${MATTERMOST_BOTS_URL}/${existingMattermostBot.id}`
    : MATTERMOST_BOTS_URL;
  return fetch(url, {
    method: isUpdate ? "PATCH" : "POST",
    headers: { "Content-Type": "application/json" },
    body: requestBody(values, isUpdate),
  });
}

export function MattermostBotForm({
  existingMattermostBot,
  onSaved,
}: MattermostBotFormProps) {
  const isUpdate = existingMattermostBot !== undefined;
  const channelConfigKey = existingMattermostBot
    ? SWR_KEYS.mattermostChannelConfigs(existingMattermostBot.id)
    : null;
  const { data: channelConfigs, mutate: mutateChannelConfigs } = useSWR<
    MattermostChannelConfig[]
  >(channelConfigKey, errorHandlingFetcher);
  const initialValues: MattermostBotFormValues = {
    name: existingMattermostBot?.name ?? "",
    url: existingMattermostBot?.url ?? "",
    token: "",
    enabled: existingMattermostBot?.enabled ?? true,
  };

  return (
    <div className="w-full space-y-6">
      <Formik<MattermostBotFormValues>
        initialValues={initialValues}
        validationSchema={Yup.object().shape({
          name: Yup.string().required(),
          url: Yup.string().url().required(),
          token: isUpdate ? Yup.string().optional() : Yup.string().required(),
          enabled: Yup.boolean().required(),
        })}
        onSubmit={async (values, formikHelpers) => {
          formikHelpers.setSubmitting(true);
          const response = await saveMattermostBot(
            values,
            existingMattermostBot
          );
          formikHelpers.setSubmitting(false);
          if (!response.ok) {
            const responseJson = await response.json();
            toast.error(
              `Error ${isUpdate ? "updating" : "creating"} Mattermost Bot - ${
                responseJson.detail || responseJson.message || "unknown error"
              }`
            );
            return;
          }
          const responseJson = (await response.json()) as MattermostBot;
          toast.success(
            isUpdate
              ? "Successfully updated Mattermost Bot!"
              : "Successfully created Mattermost Bot!"
          );
          onSaved?.(responseJson);
        }}
        enableReinitialize={true}
      >
        {({ isSubmitting, values }) => (
          <Form className="w-full space-y-4">
            <TextFormField
              name="name"
              label="Mattermost Bot Name"
              type="text"
            />
            <TextFormField
              name="url"
              label="Mattermost Server URL"
              type="url"
            />
            <TextFormField
              name="token"
              label={isUpdate ? "Rotate Bot Token" : "Mattermost Bot Token"}
              type="password"
              subtext={
                isUpdate
                  ? "Leave blank to keep the currently stored encrypted token."
                  : "Token is validated, encrypted, and never returned after save."
              }
            />
            <CheckboxField name="enabled" label="Enabled" />
            <div className="flex justify-end w-full">
              <Button
                disabled={
                  isSubmitting ||
                  !values.name ||
                  !values.url ||
                  (!isUpdate && !values.token)
                }
                type="submit"
              >
                {isUpdate ? "Update" : "Create"}
              </Button>
            </div>
          </Form>
        )}
      </Formik>
      {existingMattermostBot && (
        <div className="space-y-4 rounded border border-border p-4">
          <div>
            <h3 className="font-semibold">Private answer channels</h3>
            <p className="text-sm text-muted-foreground">
              Answers in these Mattermost channel IDs are sent as native
              ephemeral posts to the requester.
            </p>
          </div>
          <Formik<MattermostChannelConfigFormValues>
            initialValues={{ channel_id: "", channel_enabled: true }}
            validationSchema={Yup.object().shape({
              channel_id: Yup.string().required(),
              channel_enabled: Yup.boolean().required(),
            })}
            onSubmit={async (values, formikHelpers) => {
              formikHelpers.setSubmitting(true);
              const response = await fetch(MATTERMOST_CHANNEL_CONFIGS_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  mattermost_bot_id: existingMattermostBot.id,
                  channel_id: values.channel_id,
                  is_ephemeral: true,
                  enabled: values.channel_enabled,
                }),
              });
              formikHelpers.setSubmitting(false);
              if (!response.ok) {
                const responseJson = await response.json();
                toast.error(
                  `Error adding private answer channel - ${
                    responseJson.detail ||
                    responseJson.message ||
                    "unknown error"
                  }`
                );
                return;
              }
              formikHelpers.resetForm();
              toast.success("Private answer channel added");
              await mutateChannelConfigs();
            }}
          >
            {({ isSubmitting, values }) => (
              <Form className="space-y-3">
                <TextFormField
                  name="channel_id"
                  label="Mattermost Channel ID"
                  type="text"
                  subtext="Use the stable Mattermost channel ID, not a display name."
                />
                <CheckboxField
                  name="channel_enabled"
                  label="Private channel enabled"
                />
                <Button
                  disabled={isSubmitting || !values.channel_id}
                  type="submit"
                >
                  Add private answer channel
                </Button>
              </Form>
            )}
          </Formik>
          <div className="space-y-2">
            {(channelConfigs ?? []).map((channelConfig) => (
              <div
                className="flex items-center justify-between rounded border border-border p-3"
                key={channelConfig.id}
              >
                <div>
                  <div className="font-mono text-sm">
                    {channelConfig.channel_id}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {channelConfig.enabled ? "Enabled" : "Disabled"} private
                    answers
                  </div>
                </div>
                <Button
                  onClick={async () => {
                    const response = await fetch(
                      SWR_KEYS.mattermostChannelConfig(channelConfig.id),
                      { method: "DELETE" }
                    );
                    if (!response.ok) {
                      const responseJson = await response.json();
                      toast.error(
                        `Error removing private answer channel - ${
                          responseJson.detail ||
                          responseJson.message ||
                          "unknown error"
                        }`
                      );
                      return;
                    }
                    toast.success("Private answer channel removed");
                    await mutateChannelConfigs();
                  }}
                  size="sm"
                  variant="danger"
                >
                  Remove
                </Button>
              </div>
            ))}
            {(channelConfigs ?? []).length === 0 && (
              <div className="text-sm text-muted-foreground">
                No private answer channels configured.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
