# Mattermost adapter release for Onyx v4.5.6

## Decision

Use a release-derived backend image for the private PoC. Do not deploy the fork `main` backend.

The fork `main` branch reports `edge-12`. It is not based on Onyx v4.5.6. Mixing that backend with the current v4.5.6 web, model, and database contract is not approved.

The approved candidate starts from the immutable upstream v4.5.6 tag. It adds only the reviewed Mattermost adapter series and one release-specific migration-parent fix.

## Provenance

| Item | Immutable value |
| --- | --- |
| Upstream tag | `v4.5.6` |
| Upstream base commit | `90917b8ecd0677a16c0dc46386352148fd772136` |
| Current deployed backend digest | `sha256:53704e5ffa0272cdf93d0644cb37842ec7b1282a4f56524b8de468d39b74e139` |
| Adapter source tip | `4c6a3a8f8bf2abf2104937037f4223ab86b0e1c0` |
| Release candidate source commit | `ef8f9a13de` |
| Release issue | `marcop55/onyx#18` |

The adapter series is:

```text
974fab88a7  docs: define Mattermost adapter contract
48b503366a  feat: persist Mattermost thread mappings
fcd2fb9d6b  feat: add Mattermost event listener
8f76dd7e8c  feat: route Mattermost events to Onyx chat
d0df094c00  feat: stream Mattermost answers with citations
9de1df76ff  feat: package Mattermost bot service
93dfdd179d  test: validate Mattermost adapter end to end
4c6a3a8f8b  style: format Mattermost streaming files
```

The release branch changes the initial adapter migration parent from edge-only revision `3350a25df58e` to v4.5.6 head `f57f35403f6c`, then applies the additive release chain:

```text
f57f35403f6c -> a14eb2f1d9c0 -> b72c9e4f1a2d -> c83d1e7a4b6f
```

`uv run alembic heads` must report exactly one head, `c83d1e7a4b6f`. Before review and again from the final image, prove disposable upgrade from `f57f35403f6c` through the full chain, downgrade all the way to `f57f35403f6c`, and re-upgrade to `c83d1e7a4b6f`.

## Superseded candidate image (do not deploy)

The values below document an earlier build only. They are not approved deployment inputs. Rebuild after exact-head independent approval and merge, then replace this section with the actual reviewed source commit, immutable local image ID, and labels.

| Item | Value |
| --- | --- |
| Local tag | `orka/onyx-backend:v4.5.6-mattermost-ef8f9a13de` |
| OCI version | `v4.5.6+mattermost.ef8f9a13de` |
| Platform | `linux/arm64` |
| Local image ID | `sha256:e2a7a59cc529069a1d063d1737da25b7895c985eb479d945fe300d4fab33c6ef` |
| Local content digest | `orka/onyx-backend@sha256:e2a7a59cc529069a1d063d1737da25b7895c985eb479d945fe300d4fab33c6ef` |
| Private registry digest | Not published. Record after an approved registry push. |

Build command:

```bash
docker buildx build \
  --platform linux/arm64 \
  --target runtime \
  --load \
  --provenance=false \
  --build-arg ONYX_VERSION=v4.5.6+mattermost.ef8f9a13de \
  --label org.opencontainers.image.source=https://github.com/marcop55/onyx \
  --label org.opencontainers.image.revision=ef8f9a13de \
  --label org.opencontainers.image.version=v4.5.6+mattermost.ef8f9a13de \
  --label io.orka.onyx.base-revision=90917b8ecd0677a16c0dc46386352148fd772136 \
  --label io.orka.onyx.adapter-tip=4c6a3a8f8bf2abf2104937037f4223ab86b0e1c0 \
  -t orka/onyx-backend:v4.5.6-mattermost-ef8f9a13de \
  backend
```

The tag is deterministic for this reviewed source. The local image ID is the immutable deployment reference on this host. Push to an approved private registry before multi-host use, then record the registry digest. The Dockerfile pins base images and Python requirement hashes. Debian package repositories are not snapshot-pinned, so this is not a bit-for-bit reproducible build claim.

## Compatibility evidence

Current working-tree evidence:

- `uv run pytest -q backend/tests/unit/onyx/onyxbot/mattermost`: 67 passed, 1 warning.
- `uv run alembic heads` from `backend/`: exactly one head, `c83d1e7a4b6f`.
- Disposable PostgreSQL 15.2 full-chain test passed: upgraded through `f57f35403f6c -> a14eb2f1d9c0 -> b72c9e4f1a2d -> c83d1e7a4b6f`, preserved the legacy mapping across additive upgrades, downgraded all the way to `f57f35403f6c`, verified adapter tables and `chat_message.external_idempotency_key` were removed, and re-upgraded to `c83d1e7a4b6f`: 1 passed, 2 warnings.
- Final-image repetition of this proof remains a release gate and must be recorded here before deployment.

Superseded earlier-candidate evidence, retained only for provenance:

- `uv run pytest -q backend/tests/unit/onyx/onyxbot/mattermost`: 57 passed.
- `uv run alembic heads`: one head, `a14eb2f1d9c0`.
- A disposable PostgreSQL 15.2 database upgraded to v4.5.6 head `f57f35403f6c`.
- The adapter migration upgraded it to `a14eb2f1d9c0`.
- The adapter table had 10 columns, three foreign keys, one primary key, two unique constraints, and the lookup index.
- Downgrade to `f57f35403f6c` removed only the adapter table.
- Re-upgrade restored the adapter head.
- The disposable database had no persistent volume and was removed after verification.
- The historical v4.5.6 migration path logged one non-fatal Redis connection warning. Migration completion and schema checks passed.

- The candidate image imported `onyx.onyxbot.mattermost.run` and reported version `v4.5.6+mattermost.ef8f9a13de`.
- The candidate image reported one Alembic head, `a14eb2f1d9c0`.
- The candidate image upgraded a separate disposable PostgreSQL 15.2 database to `a14eb2f1d9c0`; the adapter table had 10 columns.
- The image-migration database had no persistent volume and was removed after verification.

- Credential-independent integration tests passed: 6 passed with PostgreSQL 15.2, Redis 7.4, PostgreSQL file storage, and vector services disabled.
- The integration migration test downgraded to v4.5.6 head `f57f35403f6c` and upgraded back to adapter head.

The integration command used only disposable local services:

```bash
INTEGRATION_TESTS_MODE=true \
DISABLE_VECTOR_DB=true \
FILE_STORE_BACKEND=postgres \
USER_AUTH_SECRET=test-only-not-for-deployment \
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 \
POSTGRES_USER=postgres POSTGRES_PASSWORD=password POSTGRES_DB=postgres \
REDIS_HOST=127.0.0.1 REDIS_PORT=56379 \
uv run pytest -q backend/tests/integration/tests/mattermost_bot
```

The disposable PostgreSQL and Redis containers were removed after the test.

## Deployment gate

Do not deploy this image as a backend-only mix. When the adapter is enabled, `api_server`, `background`, and `mattermost_bot` must use the same reviewed release-derived backend image. Keep `web_server` and both model servers on standard v4.5.6 images.

Before deployment:

1. Review and approve the release-backport PR.
2. Record the final image ID or private-registry digest.
3. Validate Orka Compose with the opt-in adapter profile.
4. Take a coordinated database and object-data backup.
5. Test restore in an isolated project.
6. Record current container IDs, image digests, health, and restart counts.
7. Confirm the current v4.5.6 backend image remains present locally.
8. Obtain explicit cutover approval.

The live project `orka-issue2-persist-c174d899` must not change during release qualification.

## Rollback

Rollback does not recreate or delete any volume. The standard v4.5.6 backend must never start while PostgreSQL remains stamped at an adapter-only revision.

1. Require a verified backup and successful disposable restore rehearsal.
2. Stop `mattermost_bot`, `api_server`, and `background` to quiesce writers.
3. While the reviewed custom image is still selected, run `alembic downgrade f57f35403f6c` through the complete reverse chain and verify the current revision is exactly `f57f35403f6c`.
4. Only then set `api_server` and `background` to the prior immutable image `sha256:53704e5ffa0272cdf93d0644cb37842ec7b1282a4f56524b8de468d39b74e139` and recreate only those stateless services.
5. Verify API health, direct web-to-API calls, browser signup/login, indexing, search, retained volume identity, and stable restart counts.
6. Preserve the custom image and migration evidence for forward recovery. Rotate a Mattermost token only when a live token had reached an untrusted runtime.

Keep the production bot account, token, and live Mattermost tests outside this release issue. Live disposable-bot evidence remains required by `marcop55/onyx#8`.
