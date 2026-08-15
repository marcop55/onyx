import { configRequestBody } from "../MattermostChannelConfigPanel";

test("serializes channel config without duplicating agent instructions", () => {
  const body = JSON.parse(
    configRequestBody(
      {
        channel_id: "channel-1",
        channel_name: "design",
        persona_id: "42",
        response_style: "orka_concise",
        respond_tag_only: true,
        disabled: false,
      },
      7,
      false
    )
  );

  expect(body).toEqual({
    mattermost_bot_id: 7,
    channel_id: "channel-1",
    channel_name: "design",
    persona_id: 42,
    respond_tag_only: true,
    response_style: "orka_concise",
    disabled: false,
    is_default: false,
  });
  expect(body).not.toHaveProperty("system_prompt");
  expect(body).not.toHaveProperty("instructions");
});

test("serializes default config without a channel-specific identity", () => {
  const body = JSON.parse(
    configRequestBody(
      {
        channel_id: "ignored-channel",
        channel_name: "ignored-name",
        persona_id: null,
        response_style: "default",
        respond_tag_only: true,
        disabled: false,
      },
      7,
      true
    )
  );

  expect(body.channel_id).toBeNull();
  expect(body.channel_name).toBeNull();
  expect(body.persona_id).toBeNull();
  expect(body.response_style).toBe("default");
  expect(body.is_default).toBe(true);
});
