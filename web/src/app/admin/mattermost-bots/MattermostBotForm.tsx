"use client";

import { TextFormField } from "@/components/Field";
import type { MattermostBot } from "@/lib/types";
import { CheckboxField } from "@/refresh-components/form/LabeledCheckboxField";
import { Button } from "@opal/components";
import { toast } from "@opal/layouts";
import { Form, Formik } from "formik";
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

const MATTERMOST_BOTS_URL = "/api/manage/admin/mattermost-app/bots";

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
  const initialValues: MattermostBotFormValues = {
    name: existingMattermostBot?.name ?? "",
    url: existingMattermostBot?.url ?? "",
    token: "",
    enabled: existingMattermostBot?.enabled ?? true,
  };

  return (
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
        const response = await saveMattermostBot(values, existingMattermostBot);
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
          <TextFormField name="name" label="Mattermost Bot Name" type="text" />
          <TextFormField name="url" label="Mattermost Server URL" type="url" />
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
  );
}
