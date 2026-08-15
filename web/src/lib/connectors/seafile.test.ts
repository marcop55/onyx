import { credentialTemplates } from "@/lib/connectors/credentials";
import { connectorConfigs } from "@/lib/connectors/connectors";
import { SourceCategory } from "@/lib/search/interfaces";
import { getSourceMetadata } from "@/lib/sources";
import { ValidSources } from "@/lib/types";

describe("Seafile managed connector metadata", () => {
  it("exposes Seafile as an Onyx-managed storage connector with write-only token fields", () => {
    const metadata = getSourceMetadata(ValidSources.Seafile);

    expect(metadata.displayName).toBe("Seafile");
    expect(metadata.category).toBe(SourceCategory.Storage);
    expect(credentialTemplates[ValidSources.Seafile]).toEqual({
      seafile_api_token: "",
    });
    expect(connectorConfigs[ValidSources.Seafile].values).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ name: "base_url", label: "Server URL" }),
        expect.objectContaining({
          name: "library_ids",
          label: "Library IDs",
          optional: false,
          type: "list",
        }),
        expect.objectContaining({
          name: "adopt_existing_ingestion_api",
          type: "checkbox",
        }),
        expect.objectContaining({
          name: "ingestion_api_document_id_mappings",
          optional: false,
          type: "list",
        }),
      ])
    );
  });

  it("does not expose mutable library name selection", () => {
    const fieldNames = connectorConfigs[ValidSources.Seafile].values.map(
      (field) => field.name
    );

    expect(fieldNames).not.toContain("library_names");
  });
});
