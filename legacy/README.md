# Mailbridge for Hermes

A standalone Hermes plugin built around the existing Cafe24 migration engine. **One shared toolset, 38 provider profiles, 12 workflow tools.** Providers share POP/EML transport and the migration ledger; provider differences live in documented profiles instead of duplicated importers.

The catalog retains 18 previously discussed providers and adds 20 across 11 countries. [Provider evidence and limitations](docs/PROVIDERS.md) clearly distinguish POP-only services, plan restrictions, IMAP support and uncertainty. These are documentation-backed profiles, not 38 live-tested integrations. Some are large telecom services rather than small independent hosts.

## Install

Requires Python 3.11+ on macOS or Linux and a recent [Hermes Agent](https://github.com/NousResearch/hermes-agent) supporting native plugins. Windows needs WSL. Tested against Hermes v0.23.1 (`3b4215785882`) on this Mac.

Unpack this directory, then run from it:

```sh
python3 install.py
hermes plugins enable mailbridge
hermes plugins doctor mailbridge
```

The installer refuses to overwrite an existing installation. For a non-default profile, use `python3 install.py --hermes-home /absolute/profile-home` and the same `HERMES_HOME` for the Hermes commands. Enabling installs the Python requirements declared in `plugin.yaml`; use Hermes package management to resolve dependencies; do not modify its managed runtime with pip. Start a new Hermes session so its tool list includes Mailbridge. After manually replacing installed plugin code, disable and re-enable Mailbridge to refresh the running gateway, then start a new conversation; `/reload` refreshes environment variables only.

Code is installed under `$HERMES_HOME/plugins/mailbridge`. Private captures, SQLite state, grants and reports live under `$HERMES_HOME/plugin-data/mailbridge`, surviving code updates. Move old code aside before reinstalling; preserve plugin data and original-engine profile/checkpoint directories.

## Connect your Gmail account

In a new Hermes conversation, select Mailbridge and say **connect gmail**.
Mailbridge opens Google's account chooser. Choose the Gmail or Workspace address
you want, review the requested Gmail insert/read/label permissions, and return to
Hermes. Ask it to check the returned connection ID. It reports **connected** only
after Google account verification and private credential storage succeed.

You may say **connect Gmail as person@example.com** to require an exact account
match. Choosing another account then fails without saving its credentials. With
no email supplied, the browser choice determines the destination; your ChatGPT
login and old demo profile do not select it. Connecting never imports messages,
approves a migration, provisions accounts, or changes DNS.

The two native tools are `mailbridge_gmail_connect` and
`mailbridge_gmail_connection_status`. They do not use Hermes's hosted connector.
Sign-in runs in a detached process with a ten-minute callback deadline. Repeated
connect calls while it is pending reuse the same listener. Every connect response provides a clickable Google sign-in link. A browser-launch acknowledgment does not prove a visible window. If the browser does not open, use that link. Browser consent
must be completed by the user. A callback page alone is not success; check the
verified status in Hermes.

A local Desktop OAuth client must be configured once. Set an absolute client
path in `$HERMES_HOME/plugin-data/mailbridge/gmail-settings.json`:

```json
{"oauth_client":"/absolute/private/oauth-client.json","testing":true}
```

Alternatively pass its path as `oauth_client` to the connect tool. Do not put
client secrets or tokens in chat. The configured demo app is External/Testing:
Google may block accounts that its owner has not added as test users. This is
an app eligibility restriction, not absence of Gmail support. Workspace admin
policy may also restrict access. A new account choice uses a new connection ID;
previous plans and destination bindings are left intact.

Credentials use macOS Keychain, or an owner-only file on systems without macOS
Keychain. Connection status returns only identity, timestamps, and progress.

After connection, prepare the selected export without hand-writing a profile:

```json
{"tool":"mailbridge_plan","arguments":{"mode":"gmail","provider":"cafe24","gmail_connection":"RETURNED_CONNECTION_ID","source":"/absolute/EML-export","source_mailbox":"legacy@example.com","label_prefix":"Mailbridge Archive"}}
```

The generated profile binds the selected Gmail identity and preserves its token
binding. Source inventory and local import approval still occur separately.

## Try it without an account

Ask Hermes:

> Use Mailbridge to run the synthetic recovery demo. Prepare it, start it, wait for it to settle, verify it, and show the report. Explain what the verification covers.

Or call the tools with these arguments in sequence, polling status after each operation:

```json
{"tool":"mailbridge_plan","arguments":{"mode":"demo","scenario":"recovery"}}
{"tool":"mailbridge_status","arguments":{"tid":"RETURNED_JOB_ID"}}
{"tool":"mailbridge_start","arguments":{"tid":"RETURNED_JOB_ID"}}
{"tool":"mailbridge_verify","arguments":{"tid":"RETURNED_JOB_ID"}}
{"tool":"mailbridge_report","arguments":{"tid":"RETURNED_JOB_ID"}}
```

Expected: 14 source files, 12 distinct messages, 14 verified rows. The simulated lost response is reconciled without a second upload. This is a **synthetic local rehearsal**, not Gmail.

## Configure a real POP source

First consult `mailbridge_providers` and confirm the exact product, POP hostname, encrypted port and account eligibility in its official documentation. Hostnames are not guessed from MX records. IMAP-capable providers use their POP interface or EML exports in this release; direct IMAP capture is not implemented. BiNDup is EML-export-only because the inspected setup guide requires plaintext POP.

In your local terminal, register a source; the password is only referenced by environment-variable name:

```sh
python3 "$HOME/.hermes/plugins/mailbridge/mailbridge_plugin.py" source-add \
  --provider cafe24 --connection-id old-mail \
  --host YOUR_CONFIRMED_POP_HOST --username YOUR_MAILBOX \
  --password-env MAILBRIDGE_SECRET_OLD_MAIL --security tls --port 995
```

For a custom profile pass `--state /absolute/profile-home/plugin-data/mailbridge` **before** `source-add`. TLS certificate validation is mandatory. STARTTLS uses `--security starttls --port 110` only where the provider documents it. No plaintext fallback exists.

Set the dedicated secret in the environment that launches Hermes. To avoid entering it into shell history:

```sh
# bash or zsh; getpass hides input. Start Hermes from this same terminal.
export MAILBRIDGE_SECRET_OLD_MAIL="$(python3 -c 'import getpass; print(getpass.getpass("POP password: "))')"
hermes
```

Do not paste credentials into chat. Hermes may also supply environment variables from your private profile configuration. Tokens for Gmail remain in the original engine's Keychain/file token store. The plugin does not return credentials, message bodies or subjects to the model.

Ask Hermes to inspect `old-mail`, capture up to 200 messages, and poll the returned capture job ID. Repeat bounded captures as needed; UIDLs and hashes prevent downloading already captured messages again. Capture never sends `DELE`.

Then prepare `{"mode":"local","provider":"cafe24","connection_id":"old-mail"}`, start, verify and report. That rehearses the captured files locally. Do not keep capturing into a source after freezing its migration inventory; new or changed files invalidate the plan. Use a separate capture/export directory for a later batch.

**POP scope:** usually only messages still exposed in the server Inbox. Sent folders, webmail folders, expired server mail and computer-only archives need separate exports. EML folder structure preserves the source folder mapping. PST/MBOX parsing, contacts and calendars are not provided.

## Import to Gmail

1. Prepare private EML exports or complete the POP capture. Use a separate small source/profile for a pilot; the original engine does not have a per-message pilot limit.
2. Copy [examples/profile.example.json](examples/profile.example.json), set absolute source/state/OAuth paths and the destination, then follow [the original engine instructions](docs/USAGE.md). The OAuth Desktop client and consent are configured locally. On Linux choose the documented private file token store instead of macOS Keychain.
3. Authorize the destination locally with the original command, using the interpreter where requirements are installed:

   ```sh
   python3 gmail_eml_migration_v2.py authorize --profile /absolute/private/profile.json
   ```

4. Call `mailbridge_plan` with `{"provider":"cafe24","mode":"gmail","profile":"/absolute/private/profile.json"}`. Replace provider as appropriate. `connection_id` is only for local rehearsals; a Gmail profile binds its own source directory, which can be the private capture directory.
5. Poll status. Review the frozen count, destination and capture scope. Run the exact `approval_command` returned by status in your local terminal. It asks you to type the destination and inventory fingerprint. This grants one frozen plan; there is no approval tool or `approved=true` argument.
6. Ask Hermes to start, monitor, verify and report. Resume the same job after interruption. Never start another importer for the same mailbox. Revoking a grant prevents future starts; it cannot recall an upload already in progress.

The local approval step is an operator guardrail, **not an OS sandbox against an agent with arbitrary terminal/file access**. The bundled migration skill tells Hermes to leave approval to the human operator.

`accounted_complete` can include messages retained only locally. `destination_complete` requires verified Gmail coverage. Gmail verification checks IDs, selected headers, dates and labels, not universal byte-for-byte equivalence. Local rehearsal compares exact EML bytes and folder mappings.

## Tools and execution

| Tool | Purpose |
|---|---|
| `mailbridge_gmail_connect` | Open local Google sign-in and account selection; no migration writes |
| `mailbridge_gmail_connection_status` | Check verified account identity and connection progress |
| `mailbridge_providers` | Search profiles by provider, country, POP-only/plan restriction |
| `mailbridge_inspect` | Check registered configuration and credential availability without network access |
| `mailbridge_capture` | Bounded, resumable secure POP capture |
| `mailbridge_plan` | Freeze demo, EML rehearsal or Gmail inventory |
| `mailbridge_start` | Start/resume a prepared plan; Gmail requires its local grant |
| `mailbridge_status` | Read progress, scope and migration outcome |
| `mailbridge_pause` | Pause local copying between batches |
| `mailbridge_reconcile` | Recheck local uncertain writes or Gmail terminal evidence |
| `mailbridge_verify` | Independent destination readback without uploads |
| `mailbridge_report` | Save a redacted private JSON report |

Long operations run in detached processes and survive a tool call or Hermes exit. Workers serialize per task, source connection and destination within a profile. The original engine also locks its checkpoint. Different Hermes profiles or external migration programs can bypass those profile-level locks; operators must not run them against the same destination concurrently. A job's `status=complete` means its operation finished; check `migration.status` and `migration.destination_complete` for the migration result.

Gmail uncertainty reconciliation belongs to the original authorized run/resume path. The read-only reconcile tool never retries uploads. Gmail pause is not immediate cancellation; mailbox operations settle first. Provider errors are reduced to their exception type to avoid leaking private content.

The preserved standalone dashboard is documented in [MAILBRIDGE_ORIGINAL.md](docs/MAILBRIDGE_ORIGINAL.md). Use a separate runtime directory; it refuses a plugin-owned runtime. This plugin is not a hosted service and does not provision mailboxes, delete source mail or change DNS.

## Development and evidence

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
# In an environment with Hermes dependencies installed:
python3 tests/hermes_smoke.py --hermes-source /absolute/hermes-agent
```

Tests use fictional messages and mocked Gmail APIs; the POP integration test uses a real local TLS socket and requires `openssl`. See [VALIDATION.json](VALIDATION.json), [the Hermes smoke result](docs/HERMES_SMOKE.json), and [provenance](docs/PROVENANCE.md). No live provider account or Gmail migration was performed during this build.
