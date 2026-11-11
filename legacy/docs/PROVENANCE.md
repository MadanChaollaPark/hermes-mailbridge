# Source and compatibility

- Existing engine repository: https://github.com/MadanChaollaPark/gmail-migration-tools
- Original migration-engine base: `cee4dac053a3ad1389a457b57a40f2e90af210b8`.
- Reused local Mailbridge prototype: commit `05b70c6`, from the prior Cafe24 migration work.
- `gmail_eml_migration_v2.py` is preserved byte-for-byte from that prototype. Its tests, pilot, profile format, OAuth identity binding, deduplication, uncertain-write handling, MIME repair policy and final reconciliation are retained.
- New: native Hermes manifest and registration, process adapter, catalog, local installer, plugin skill, approval binding, worker orchestration and plugin tests.
- Existing dashboard change: refuse plugin-owned runtime directories so dashboard recovery cannot take over active plugin workers.
- Hermes compatibility source: https://github.com/NousResearch/hermes-agent/tree/78abaafcccec20a546c94e8734a70e38c08a6a69
- Used native `register(ctx)`, `ctx.register_tool`, `ctx.register_skill`, and `plugins.plugin_storage.plugin_data_dir`. No patches to Hermes core.

Catalog evidence records protocol documentation, not account-level integration testing or the age of server software. Country labels describe provider origin/market and make no data-residency claim. Original prototype documents under `docs/` describe earlier work; current plugin behavior and validation are described in the root README and VALIDATION.json.

Operational findings: local inventory validation must check file bytes as well as names; failed readback must invalidate cached completion; detached workers need inherited locks before spawning; captured POP mail cannot establish full-mailbox completeness. Regression tests cover these cases.

This is the historical standalone snapshot. It is not the catalog package; see ../../plugin for the Hermes-managed credential implementation. No new license grant is asserted for the reused source.

Installed-Mac validation: Hermes v0.23.1, commit `3b4215785882`, with a live ChatGPT/openai-codex tool loop and 83 passing offline tests. Dependency declarations now use bounded compatible ranges so Hermes can retain its own pinned Google library versions. No Hermes core changes. No live Gmail import or provider login.

Version 0.2.0 adds native browser Gmail connection and status tools. The selected account is verified through Gmail getProfile before the preserved engine stores its bound grant. Connection is separate from inventory approval and imports. Offline tests cover mismatch, incomplete scopes, interruption, secret redaction, runtime library propagation and the selected-account plan path. The original Gmail writer remains unchanged.

Runtime finding: replacing plugin files and running doctor does not update the long-lived gateway registry. Disable/enable refreshed it; a new normal-tools chat then invoked the two native Gmail tools for the exact prompt "connect gmail". The Google consent callback is awaiting user interaction; a real grant has not been claimed.
