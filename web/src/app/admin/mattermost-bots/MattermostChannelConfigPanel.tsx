"use client";

import { SelectorFormField, TextFormField } from "@/components/Field";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAgents } from "@/lib/agents/hooks";
import type { MattermostChannelConfig } from "@/lib/types";
import { SWR_KEYS } from "@/lib/swr-keys";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { CheckboxField } from "@/refresh-components/form/LabeledCheckboxField";
import { Button } from "@opal/components";
import { toast } from "@opal/layouts";
import { Form, Formik } from "formik";
import { useState } from "react";
import useSWR from "swr";
import * as Yup from "yup";

type MattermostResponseStyle = "default" | "orka_concise" | "detailed";
type MattermostResponseType = "citations" | "quotes";

interface MattermostChannelConfigFormValues {
  channel_id: string;
  channel_name: string;
  persona_id: string | null;
  response_style: MattermostResponseStyle;
  response_type: MattermostResponseType;
  include_source_previews: boolean;
  answer_only_when_sourced: boolean;
  standard_answer_category_ids: string;
  follow_up_tags: string;
  respond_tag_only: boolean;
  disabled: boolean;
}

interface MattermostChannelConfigPanelProps {
  mattermostBotId: number;
}

function valuesFromConfig(
  config?: MattermostChannelConfig
): MattermostChannelConfigFormValues {
  return {
    channel_id: config?.channel_id ?? "",
    channel_name:
      config?.channel_name ?? config?.channel_config.channel_name ?? "",
    persona_id: config?.persona?.id.toString() ?? null,
    response_style: config?.channel_config.response_style ?? "orka_concise",
    response_type: config?.channel_config.response_type ?? "citations",
    include_source_previews:
      config?.channel_config.include_source_previews ?? false,
    answer_only_when_sourced:
      config?.channel_config.answer_filters?.includes(
        "well_answered_postfilter"
      ) ?? false,
    standard_answer_category_ids:
      config?.channel_config.standard_answer_category_ids?.join(", ") ?? "",
    follow_up_tags: config?.channel_config.follow_up_tags?.join(", ") ?? "",
    respond_tag_only: config?.channel_config.respond_tag_only ?? true,
    disabled: config?.channel_config.disabled ?? false,
  };
}

function parseCommaSeparatedInts(value: string): number[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map(Number)
    .filter(Number.isInteger);
}

function parseCommaSeparatedStrings(value: string): string[] | null {
  const tags = value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return tags.length > 0 ? tags : null;
}

export function configRequestBody(
  values: MattermostChannelConfigFormValues,
  mattermostBotId: number,
  isDefault: boolean
): string {
  return JSON.stringify({
    mattermost_bot_id: mattermostBotId,
    channel_id: isDefault ? null : values.channel_id,
    channel_name: isDefault ? null : values.channel_name || null,
    persona_id: values.persona_id ? Number(values.persona_id) : null,
    respond_tag_only: values.respond_tag_only,
    response_style: values.response_style,
    response_type: values.response_type,
    include_source_previews: values.include_source_previews,
    answer_filters: values.answer_only_when_sourced
      ? ["well_answered_postfilter"]
      : [],
    standard_answer_category_ids: parseCommaSeparatedInts(
      values.standard_answer_category_ids
    ),
    follow_up_tags: parseCommaSeparatedStrings(values.follow_up_tags),
    disabled: values.disabled,
    is_default: isDefault,
  });
}

async function saveMattermostChannelConfig(
  values: MattermostChannelConfigFormValues,
  mattermostBotId: number,
  existingConfig?: MattermostChannelConfig,
  isDefault = false
): Promise<Response> {
  const isUpdate = existingConfig !== undefined;
  const url = isUpdate
    ? SWR_KEYS.mattermostChannelConfig(existingConfig.id)
    : "/api/manage/admin/mattermost-app/channel";
  return fetch(url, {
    method: isUpdate ? "PATCH" : "POST",
    headers: { "Content-Type": "application/json" },
    body: configRequestBody(values, mattermostBotId, isDefault),
  });
}

async function deleteMattermostChannelConfig(
  configId: number
): Promise<Response> {
  return fetch(SWR_KEYS.mattermostChannelConfig(configId), {
    method: "DELETE",
  });
}

export function MattermostChannelConfigPanel({
  mattermostBotId,
}: MattermostChannelConfigPanelProps) {
  const [editingConfig, setEditingConfig] = useState<
    MattermostChannelConfig | undefined
  >();
  const [isCreating, setIsCreating] = useState(false);
  const { agents } = useAgents();
  const { data, isLoading, mutate } = useSWR<MattermostChannelConfig[]>(
    SWR_KEYS.mattermostBotConfigs(mattermostBotId),
    errorHandlingFetcher
  );

  const configs = data ?? [];
  const defaultConfig = configs.find((config) => config.is_default);
  const channelConfigs = configs.filter((config) => !config.is_default);
  const activeConfig =
    editingConfig ?? (isCreating ? undefined : defaultConfig);
  const isDefaultConfig = !isCreating && activeConfig?.is_default !== false;
  const isFormVisible = isCreating || activeConfig !== undefined;
  const agentOptions = agents.map((agent) => ({
    name: agent.name,
    value: agent.id,
  }));

  const onSaved = async () => {
    setEditingConfig(undefined);
    setIsCreating(false);
    await mutate();
  };

  return (
    <Card className="p-6 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">
            Mattermost channel behaviour
          </h2>
          <p className="text-sm text-muted-foreground">
            Select the Onyx Agent per Mattermost channel and apply bounded
            response controls. The selected Agent Instructions remain the only
            base personality source.
          </p>
        </div>
        <Button
          prominence="secondary"
          onClick={() => {
            setIsCreating(true);
            setEditingConfig(undefined);
          }}
        >
          Add Channel Config
        </Button>
      </div>

      {isLoading ? (
        <div>Loading Mattermost channel configs...</div>
      ) : (
        <div className="space-y-4">
          {isFormVisible && (
            <Formik<MattermostChannelConfigFormValues>
              initialValues={valuesFromConfig(activeConfig)}
              validationSchema={Yup.object().shape({
                channel_id: isDefaultConfig
                  ? Yup.string()
                  : Yup.string().required("Mattermost channel ID is required"),
                channel_name: Yup.string(),
                persona_id: Yup.string().nullable(),
                response_style: Yup.mixed<MattermostResponseStyle>()
                  .oneOf(["default", "orka_concise", "detailed"])
                  .required(),
                response_type: Yup.mixed<MattermostResponseType>()
                  .oneOf(["citations", "quotes"])
                  .required(),
                include_source_previews: Yup.boolean().required(),
                answer_only_when_sourced: Yup.boolean().required(),
                standard_answer_category_ids: Yup.string().matches(
                  /^$|^(\s*\d+\s*)(,\s*\d+\s*)*$/,
                  "Use comma-separated standard answer category IDs"
                ),
                follow_up_tags: Yup.string(),
                respond_tag_only: Yup.boolean().required(),
                disabled: Yup.boolean().required(),
              })}
              onSubmit={async (values, formikHelpers) => {
                formikHelpers.setSubmitting(true);
                const response = await saveMattermostChannelConfig(
                  values,
                  mattermostBotId,
                  activeConfig,
                  isDefaultConfig
                );
                formikHelpers.setSubmitting(false);
                if (!response.ok) {
                  const responseJson = await response.json();
                  toast.error(
                    `Error saving Mattermost channel config - ${
                      responseJson.detail ||
                      responseJson.message ||
                      "unknown error"
                    }`
                  );
                  return;
                }
                toast.success("Mattermost channel config saved");
                await onSaved();
              }}
              enableReinitialize={true}
            >
              {({ isSubmitting }) => (
                <Form className="space-y-4 rounded-md border p-4">
                  <div className="flex items-center justify-between gap-3">
                    <h3 className="font-medium">
                      {isDefaultConfig
                        ? "Default Mattermost behaviour"
                        : activeConfig
                          ? "Edit channel behaviour"
                          : "New channel behaviour"}
                    </h3>
                    {isDefaultConfig && (
                      <Badge variant="secondary">Default</Badge>
                    )}
                  </div>

                  {!isDefaultConfig && (
                    <>
                      <TextFormField
                        name="channel_id"
                        label="Mattermost Channel ID"
                        type="text"
                      />
                      <TextFormField
                        name="channel_name"
                        label="Mattermost Channel Name"
                        type="text"
                        subtext="Optional display label. Agent routing uses the immutable Mattermost channel ID."
                      />
                    </>
                  )}

                  <SelectorFormField
                    name="persona_id"
                    label="Agent"
                    options={agentOptions}
                    includeReset
                    subtext="Base tone and instructions come from this Agent only."
                  />
                  <SelectorFormField
                    name="response_style"
                    label="Response style"
                    options={[
                      { name: "Default Agent behaviour", value: "default" },
                      { name: "Orka concise", value: "orka_concise" },
                      { name: "Detailed", value: "detailed" },
                    ]}
                  />
                  <SelectorFormField
                    name="response_type"
                    label="Sources display"
                    options={[
                      { name: "Citations", value: "citations" },
                      { name: "Quotes", value: "quotes" },
                    ]}
                  />
                  <CheckboxField
                    name="include_source_previews"
                    label="Show source previews"
                  />
                  <CheckboxField
                    name="answer_only_when_sourced"
                    label="Answer only when sources are found"
                  />
                  <TextFormField
                    name="standard_answer_category_ids"
                    label="Standard answer category IDs"
                    type="text"
                    subtext="Comma-separated managed Onyx standard answer category IDs for native Mattermost standard answers."
                  />
                  <TextFormField
                    name="follow_up_tags"
                    label="Follow-up tags"
                    type="text"
                    subtext="Optional comma-separated tags included when Mattermost users mark an answer as needing follow-up."
                  />
                  <CheckboxField
                    name="respond_tag_only"
                    label="Respond only when tagged"
                  />
                  <CheckboxField name="disabled" label="Disable this config" />

                  <div className="flex justify-end gap-2">
                    {!isDefaultConfig && (
                      <Button
                        type="button"
                        prominence="secondary"
                        onClick={() => {
                          setEditingConfig(undefined);
                          setIsCreating(false);
                        }}
                      >
                        Cancel
                      </Button>
                    )}
                    <Button type="submit" disabled={isSubmitting}>
                      Save Config
                    </Button>
                  </div>
                </Form>
              )}
            </Formik>
          )}

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Channel</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead>Style</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {channelConfigs.map((config) => (
                <TableRow key={config.id}>
                  <TableCell>
                    {config.channel_name ||
                      config.channel_config.channel_name ||
                      config.channel_id}
                  </TableCell>
                  <TableCell>
                    {config.persona?.name ?? "Default bot Agent"}
                  </TableCell>
                  <TableCell>
                    {config.channel_config.response_style === "orka_concise"
                      ? "Orka concise"
                      : config.channel_config.response_style === "detailed"
                        ? "Detailed"
                        : "Default"}
                  </TableCell>
                  <TableCell>
                    {config.channel_config.disabled ? (
                      <Badge variant="destructive">Disabled</Badge>
                    ) : (
                      <Badge variant="success">Enabled</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        prominence="secondary"
                        onClick={() => {
                          setEditingConfig(config);
                          setIsCreating(false);
                        }}
                      >
                        Edit
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={async () => {
                          const response = await deleteMattermostChannelConfig(
                            config.id
                          );
                          if (!response.ok) {
                            const responseJson = await response.json();
                            toast.error(
                              `Error deleting Mattermost channel config - ${
                                responseJson.detail ||
                                responseJson.message ||
                                "unknown error"
                              }`
                            );
                            return;
                          }
                          toast.success("Mattermost channel config deleted");
                          await mutate();
                        }}
                      >
                        Delete
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {channelConfigs.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="text-center text-muted-foreground"
                  >
                    No channel-specific configurations yet. The default
                    Mattermost behaviour applies to all channels.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </Card>
  );
}
