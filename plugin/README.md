# Mailbridge

A Hermes plugin for rescuing legacy mail through secure POP capture or EML exports and moving it into Gmail with a durable ledger and independent readback.

One shared engine, **38 documented provider profiles and 12 workflow tools**. Provider-specific behavior lives in a source profile; import, deduplication and verification logic is shared. [The provider catalog](docs/PROVIDERS.md) includes 20 additions across 11 countries and clearly labels POP-only services, paid-plan restrictions, IMAP support and uncertain documentation. None of the 38 provider accounts has been live-tested in this build.

## Install and try

Requires Hermes **0.23.1+**, Python 3.11+, and macOS or Linux. Windows needs WSL. The plugin declares no additional Python dependencies.

Clone this repository and run from its `plugin` directory:

```sh
python3 install.py
hermes plugins enable mailbridge
hermes plugins doctor mailbridge
```

The installer refuses to replace an existing installation. For another profile use `--hermes-home /absolute/profile-home` and set the same `HERMES_HOME` for subsequent Hermes commands. Start a new Hermes conversation after enabling. Code lives in the profile's `plugins/mailbridge`; captures, nonsecret account bindings, grants, checkpoints and reports live in `plugin-data/mailbridge` and survive code updates.

Ask Hermes:

> Use Mailbridge to run the synthetic recovery demo. Prepare it, start it, wait, verify it and report what the result proves.

Expected: **14 source files, 12 unique messages, 14 verified rows**, including recovery from a simulated lost response without a duplicate upload. The demo is local and fictional, not a Gmail migration.

## Connect Gmail through Hermes

1. Enable Gmail API for your Google Cloud Desktop OAuth client. Configure its ID and secret through **Hermes Settings → Plugins → Mailbridge**. The fields map to `MAILBRIDGE_GOOGLE_CLIENT_ID` and `MAILBRIDGE_GOOGLE_CLIENT_SECRET`; Hermes stores them through its own secret route. Never paste secrets or tokens into chat.
2. Run this yourself in a local terminal for the selected Hermes profile:

   ```sh
   hermes auth add mailbridge:google
   ```

3. Ask Mailbridge to connect Gmail, optionally naming the exact desired address. It calls Gmail's profile endpoint through Hermes, verifies the selected account and stores only a nonsecret account binding. A different account is refused when an exact address was requested. Without a grant, it returns the sign-in command; it does not launch an unattended browser flow.

Hermes owns browser consent, grant storage, refresh and logout. The plugin declares `oauth: google` and sends Gmail requests exclusively with `credentialed_provider_request`. It never receives the access or refresh token. Use `hermes auth status mailbridge:google` or `hermes auth logout mailbridge:google` to manage the grant. Google testing-mode user eligibility and Workspace admin policy still apply.

Changing the signed-in Google account does not retarget an existing plan: every migration request names its frozen destination address in the Gmail API URL, and the engine verifies the current account before writing. One active Google grant is used per Hermes profile; use separate profiles for separate concurrent destinations.

## Capture a POP source

Use `mailbridge_providers` to check the exact provider and plan, then verify its current TLS hostname and port in the cited official documentation. IMAP-capable providers use POP or EML here; direct IMAP capture is not implemented. BiNDup is EML-export-only because the inspected documentation requires plaintext POP.

Configure `MAILBRIDGE_POP_PASSWORD` in the plugin's Hermes settings. This is the password or app password for the source selected in that profile. Register its nonsecret settings locally:

```sh
python3 mailbridge_plugin.py --state /absolute/profile-home/plugin-data/mailbridge source-add \
  --provider cafe24 --connection-id old-mail \
  --host YOUR_CONFIRMED_POP_HOST --username YOUR_MAILBOX \
  --security tls --port 995
```

No password is accepted on the command line or stored in the source record. The declared secret is resolved with `agent.secret_scope.get_secret`, respecting the active Hermes profile. Only that scoped source password is forwarded to its capture worker. Use a separate profile when another source needs a different simultaneous credential. For documented STARTTLS servers use `--security starttls --port 110`; there is no plaintext fallback.

Ask Hermes to inspect `old-mail`, capture a bounded batch (default 200), then poll the job ID. Subsequent captures resume by UIDL and verify existing file hashes. No `DELE` command is issued. Captured EML files are under `plugin-data/mailbridge/captures/old-mail/Inbox`.

POP covers only messages the server still exposes, usually Inbox. Sent folders, other webmail folders, expired server mail and computer-only archives require separate EML exports. A successful POP capture cannot establish complete mailbox coverage. Do not add captured messages to a source after freezing its inventory; use a new source/plan for later mail.

## Plan, approve, migrate, verify

A local rehearsal can use `{"mode":"local","provider":"cafe24","connection_id":"old-mail"}` or an absolute EML directory as `source`.

For Gmail, first verify the Hermes account and then prepare:

```json
{"mode":"gmail","provider":"cafe24","gmail_connection":"RETURNED_CONNECTION_ID","source":"/absolute/EML-export","source_mailbox":"legacy@example.com","label_prefix":"Mailbridge Archive"}
```

Alternatively use an operator-created [profile](examples/profile.example.json) with `auth_provider: google` and `token_store: {type: hermes}`. Legacy profiles containing `oauth_client` or a file/Keychain token store are rejected before credential access. Prepare and inventory perform no Gmail writes.

Review the frozen count, destination and scope. Run the exact `approval_command` returned by status in your local terminal and type the destination and fingerprint. Approval applies only to this frozen plan; source changes invalidate it. Hermes then starts the worker, polls progress, verifies and saves a report. A pilot requires a separate small EML source and profile.

The operator approval is a workflow guardrail, not an OS sandbox against arbitrary file/terminal access. The bundled skill instructs the agent not to forge or execute approval. Revoking permission prevents future starts; it cannot undo an in-flight import.

Workers survive tool calls and agent restarts. Locks serialize each task, source and destination within one Hermes profile, plus the engine's checkpoint lock. Do not run another profile or external importer against the same destination concurrently. Resume the same job after interruption. Ambiguous uploads are reconciled using Gmail evidence, never blindly retried.

The Hermes adapter uses base64url JSON imports and limits each prepared message to **25 MiB**. Larger messages stop before upload and remain unchanged locally. This conservative transport limit is lower than Gmail's overall API limit; streaming/resumable media upload is not implemented here. UTF-8 bytes are preserved in the encoded request. Mail is imported, not sent to recipients; calendar extraction is disabled.

`accounted_complete` may include messages retained only locally. `destination_complete` requires verified destination coverage. Gmail readback checks IDs, selected headers, dates and labels; it does not prove universal raw-byte equality. Local rehearsal verifies exact EML bytes and folder mappings. A completed worker operation can still report `migration.status=needs_review`; inspect the migration result.

## Tools

| Tool | Purpose |
|---|---|
| `mailbridge_gmail_connect` | Verify the Hermes-managed Google account or return human sign-in instructions |
| `mailbridge_gmail_connection_status` | List nonsecret account bindings and verification timestamps |
| `mailbridge_providers` | Find source-provider evidence and protocol limitations |
| `mailbridge_inspect` | Inspect configured source/secret availability without a network call |
| `mailbridge_capture` | Start bounded, resumable TLS POP capture |
| `mailbridge_plan` | Freeze demo, local EML or Gmail inventory |
| `mailbridge_start` | Start/resume an approved frozen plan |
| `mailbridge_status` | Read progress, coverage and scope |
| `mailbridge_pause` | Pause local copying between batches; Gmail operations settle first |
| `mailbridge_reconcile` | Investigate uncertain local writes or re-read Gmail terminal evidence |
| `mailbridge_verify` | Re-read destination evidence without uploads |
| `mailbridge_report` | Save a private redacted JSON report |

## Catalog disclosures

- **Network:** user-selected POP servers over TLS/STARTTLS; `gmail.googleapis.com` through Hermes. Hermes alone calls Google's authorization/token endpoints. No telemetry, analytics, model API calls or self-updater.
- **External reads:** operator-selected EML directories and nonsecret migration-profile JSON. The plugin does not read `.env`, Hermes `auth.json`, credential JSON, a browser profile or Keychain.
- **Processes:** fixed Python subprocess argument arrays, no shell evaluation. Long-running capture/import workers inherit only selected runtime settings, an explicitly bound Hermes home and declared profile-scoped client/source secrets. No Google tokens are passed. It does not spawn Hermes or inherit YOLO/approval flags.
- **Persistence:** EML captures, SQLite ledger, nonsecret source/account bindings, inventory approvals and JSON reports in the profile's private plugin-data directory. Hermes owns credentials and OAuth grants.
- **Writes:** private local evidence and, after approval, Gmail imports/labels. No source deletion, outgoing mail, contacts, calendars, mailbox provisioning or DNS changes. No PST/MBOX parsing or Microsoft 365 destination.
- **Compatibility:** public `ctx.register_tool`, `ctx.register_skill`, `plugin_data_dir`, `get_secret` and `credentialed_provider_request`. No Hermes core patches or Desktop overrides. There is no plugin UI requiring screenshots.

## Validation

See [VALIDATION.json](VALIDATION.json) for current evidence and [architecture/provenance](docs/ARCHITECTURE.md) for the changes from the Cafe24 engine. Tests cover deterministic migration behavior, real loopback TLS POP, actual Hermes registration and credential routing, and mocked Gmail imports including lost responses. No live Google grant or live mailbox import was performed for v0.3.0.

Run tests with a Hermes checkout and its test/runtime dependencies available:

```sh
PYTHONPATH=/absolute/hermes-agent python3 -m unittest discover -s tests -v
python3 tests/hermes_smoke.py --hermes-source /absolute/hermes-agent
hermes plugins validate /absolute/hermes-mailbridge/plugin --install-deps
```
