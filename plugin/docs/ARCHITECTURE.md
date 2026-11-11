# Architecture and provenance

The original Cafe24 migration engine is from [MadanChaollaPark/gmail-migration-tools](https://github.com/MadanChaollaPark/gmail-migration-tools), base commit `cee4dac053a3ad1389a457b57a40f2e90af210b8`, with the local Mailbridge prototype `05b70c6`. The earlier standalone distribution is retained under `../../legacy`; it is outside the catalog's `subdir: plugin`.

The catalog package retains source inventory, byte hashing, MIME-repair policy, duplicate-source handling, uncertain-upload reconciliation, checkpoint locks, human review and final readback. It changes authentication and upload transport. It does not claim the original engine file remains byte-identical.

## Credential ownership

`plugin.yaml` declares the Google OAuth client and exact Gmail host. Google client settings and the POP password enter only through Hermes's secret APIs. The native tool handler resolves the active profile with `plugin_data_dir`, obtains only the needed declared secrets with `agent.secret_scope.get_secret`, and launches the fixed Python entrypoint with an explicit owning `HERMES_HOME` and filtered environment.

The worker uses `credentialed_provider_request("google", ...)` from its installed plugin directory. Hermes finds its declared OAuth client, owns its grant and refresh, and attaches credentials to requests. Plugin code never opens a credential file or sees an OAuth access/refresh token. The real-Hermes smoke test exercises this path inside an installed worker and confirms an absent grant fails closed.

The plugin stores only an account identity and verification timestamp. Existing plans bind a destination address and recheck identity. Every migration API URL uses that destination explicitly rather than `users/me`, so changing the Hermes Google sign-in during a run cannot redirect later writes to the new account.

No OAuth callback listener, automatic sign-in, token store, Keychain call or model API planner is included in the catalog package. Legacy OAuth/token profiles are rejected. A browser sign-in command is shown to the human, never executed by a tool.

## Upload transport

Hermes's public request helper accepts JSON. The adapter encodes preserved RFC822 bytes as the Gmail Message `raw` field, adds required labels, and calls the documented import endpoint. This follows [Gmail messages.import](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/import) and the [Message resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages). A conservative 25-MiB per-message limit bounds this nonstreaming transport; larger source messages stop before upload.

Requests do not retry writes. A timeout after acceptance leaves a durable uncertain state; the existing engine searches and verifies destination evidence before allowing progress. Read-only requests use the engine's bounded idempotent retries. Imports disable calendar extraction and never use the deleted option. The adapter exposes no send, delete or arbitrary URL operation.

## Runtime and scope

Capture, prepare, import and verification run in detached Python workers. Parent-acquired locks are inherited before execution, preventing duplicate workers. The source and destination locks are per Hermes profile; the engine also locks its checkpoint. Operators must not run separate profiles/importers against the same destination concurrently.

The source snapshot and approval fingerprint cover the frozen source and destination. POP capture is read-only and resumable by UIDL. Capturing a complete POP listing does not prove full mailbox completeness. Folder/local archives need their own exports.

## Test lineage

The original standalone tests remain under `legacy/tests`, including its token-store and browser-flow tests. The catalog suite retains the migration-ledger behavior tests, replaces obsolete standalone credential/upload tests with Hermes-managed credential and JSON-transport tests, and retains the detached-worker/TLS-POP tests. It additionally tests the complete original engine through the new REST adapter against a fictional Gmail response service, including delayed visibility after a lost response and a zero-import rerun.

No live Google account, OAuth grant or Gmail import is used by these tests. No new license grant is asserted for the inherited source.
