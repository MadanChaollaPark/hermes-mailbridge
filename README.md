# Mailbridge for Hermes

Rescue legacy email through a resumable migration workflow: secure POP capture, frozen EML inventory, deduplication, Gmail import, uncertain-write reconciliation and independent destination verification.

**38 documented provider profiles. 12 Hermes tools.** The 20 new profiles span 11 countries; POP-only services, plan restrictions and IMAP-capable providers are labeled separately. The profiles are researched configurations, not 38 live-tested integrations.

The installable **v0.3.0 plugin is in [`plugin/`](plugin/README.md)**. It uses Hermes-managed OAuth and profile-scoped secrets; no plugin-owned Google token store, browser consent worker or credential-file access. The catalog submission pins this directory, not the repository root.

```sh
git clone https://github.com/MadanChaollaPark/hermes-mailbridge.git
cd hermes-mailbridge/plugin
python3 install.py
hermes plugins enable mailbridge
```

Try: “Use Mailbridge to run the synthetic recovery demo, verify it, and report what is covered.” The expected result is 14 source rows represented by 12 distinct local messages. This uses no real email account.

[Setup and disclosures](plugin/README.md) · [Provider research](plugin/docs/PROVIDERS.md) · [Validation](plugin/VALIDATION.json) · [Architecture and provenance](plugin/docs/ARCHITECTURE.md)

[`legacy/`](legacy/) preserves the earlier Cafe24-derived standalone implementation for provenance and regression comparison. **It is not the catalog distribution.** Its local OAuth flow and token store are replaced in `plugin/`, and old credential-bearing profiles are rejected by the catalog version. Existing standalone installations are not silently upgraded.

The new transport is tested with fictional mail and mocked Gmail responses, plus real local TLS POP and real Hermes registration/credential routing. A live Gmail grant and import are not claimed. The catalog entry remains subject to maintainer review.
