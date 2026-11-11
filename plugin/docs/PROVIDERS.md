# Provider research and supported ingestion

38 catalog entries: 18 previously discussed and 20 additions across 11 countries.

Country means service origin/market, not data residency. The existing Zoho entry is retained from the earlier list; it does not count toward the new developed-country requirement. Company size is not asserted: this includes independent hosts, regional ISPs and large telecom operators.

Every entry has official protocol evidence checked on 2026-11-11. No provider has been authenticated or live-migrated in this build. A source preset is a documented configuration/playbook over the shared POP engine, not bespoke tested integration code. IMAP-capable services use secure POP or EML ingestion in this release.

## Added twenty

| Provider | Country | IMAP status | Evidence and limits |
|---|---|---|---|
| Freeola | United Kingdom | plan_restricted | One Address is described as POP; EmailPro offers IMAP. Confirm exact product and secure endpoint. [Official source](https://freeola.com/support/freeola-faqs.html) |
| Andrews & Arnold (AAISP) | United Kingdom | supported | Official guidance supports POP3 on 995 and IMAP on 993. [Official source](https://support.aa.net.uk/Email_Settings) |
| Fasthosts | United Kingdom | supported | Provider confirms POP, IMAP and SMTP settings persist across its platform upgrade; use assigned mailbox setup details. [Official source](https://www.fasthosts.co.uk/blog/introducing-our-brand-new-mail-platform/) |
| Aruba domain email | Italy | plan_restricted | IMAP is included or available as an add-on depending on package. Do not describe all Aruba mail as POP-only. [Official source](https://guide.aruba.it/hosting-e-domini/email/gestione-email/caratteristiche-acquisto-gestione/acquisto-email-e-servizi-aggiuntivi) |
| Register.it | Italy | supported | Official help explicitly supports both POP and IMAP. This plugin handles ordinary email; no legal-preservation claims for PEC. [Official source](https://www.register.it/assistenza/pop3-imap/) |
| OVHcloud MX Plan | France | supported | POP on TLS 995 is documented; confirm region and product. IMAP is also available. [Official source](https://docs.ovhcloud.com/en/guides/web-cloud/email-and-collaborative-solutions/mx-plan/landing-page-mx-plan) |
| Gandi Mail | France | supported | Standard settings document both POP and IMAP. Confirm account password and secure ports. [Official source](https://docs.gandi.net/en/gandimail/standard_email_settings/) |
| Orange / Wanadoo mail | France | supported | POP/IMAP must be enabled in mailbox settings. Confirm the current mail-application password requirements. [Official source](https://assistance.orange.fr/oid/964290) |
| IONOS Mail | Germany | supported | POP3 and IMAP documented. Regional endpoints differ; Hosted Exchange is a separate configuration. [Official source](https://www.ionos.com/help/index.php?id=2490) |
| STRATO | Germany | supported | Documents POP3 over TLS on 995 and IMAP support. [Official source](https://www.strato.de/faq/mail/e-mailserver-adressen-ports-ssl-tls/) |
| freenet Mail | Germany | supported | Documents encrypted POP3 on 995; IMAP is also available. Enable external access in settings. [Official source](https://kundenservice.freenet.de/faq/e-mail-client-mail/bei-freenet-mail-verwendete-ports/) |
| TransIP | Netherlands | supported | Official settings support POP3 and IMAP. Confirm credentials and current connection settings. [Official source](https://www.transip.eu/knowledgebase/309-the-email-settings-at-transip) |
| Yourhosting | Netherlands | supported | Documents POP3 on 995 and IMAP on 993. Confirm migrated legacy brands use this platform. [Official source](https://www.yourhosting.nl/kennisbank/e-mail/gegevens-om-mail-in-te-stellen/) |
| Proximus / Skynet | Belgium | supported | Official support offers POP3 and IMAP configuration. Use the account-specific setup guide. [Official source](https://www.proximus.be/support/en/id_smartphone-email-messages/personal/support/smartphone-configuration/smartphone-email-messages.html) |
| Bell / Sympatico | Canada | supported | Official support documents POP and IMAP; consumer and Bellnet business servers differ. [Official source](https://support.bell.ca/Internet/Email/How-to-use-Bell-Mail?step=5) |
| Spark Xtra Mail | New Zealand | supported | Official guide documents POP3 on 995 and IMAP; Spark Business Mail has different endpoints. [Official source](https://www.spark.co.nz/help/get-more/xtra/xtra-mail-setup) |
| OptusNet | Australia | supported | Official help recommends IMAP and also documents POP. Confirm a secure POP endpoint; never downgrade to plaintext. [Official source](https://optus.com.au/support/answer/using_an_email_application_with_optusnet_email_1434) |
| The Messaging Company | Australia | supported | Provides both protocols; recommends IMAP. Confirm migrated account and current secure POP settings. [Official source](https://support.themessaging.co/hc/en-au/articles/7879726922639-Mail-server-settings) |
| Chunghwa Telecom HiNet | Taiwan | supported | Official help explicitly supports POP3 and IMAP with encrypted ports. Host depends on the account domain (MS1–MS82, MSA and other products). [Official source](https://lib.webmail.hinet.net/statement/F-soft-1.htm) |
| Juno | United States | not_documented | Official client setup explicitly directs POP instead of IMAP and specifies 995. This is POP-directed documentation, not a universal proof that IMAP is absent. [Official source](https://help.juno.com/support/email/tbird.html) |

## Previously discussed eighteen

| Provider | Country | IMAP status | Evidence and limits |
|---|---|---|---|
| Cafe24 | South Korea | unsupported | Different Cafe24 products have different hosts. Capture only through verified TLS/STARTTLS; use folder EML exports for missing mail. [Official source](https://help.cafe24.com/docs/business-tools/webmail/webmail-setup-and-integration-guide/) |
| Hiworks / Gabia | South Korea | unsupported | Official help describes POP-only client access; account settings must enable it. [Official source](https://customer.gabia.com/faq/detail/3460/20100) |
| NAVER WORKS | South Korea | supported | POP and IMAP access depend on administrator and plan settings. IMAP is available; do not market as POP-only. [Official source](https://help.worksmobile.com/en/use-guides/mail/settings/pop3-imap-smtp/) |
| Zoho Mail | India | supported | Regional server and plan eligibility vary. Earlier requested provider retained; India is not counted toward the additional developed-country requirement. [Official source](https://www.zoho.com/mail/help/pop-access.html) |
| Fastmail | Australia | supported | Confirm eligible plan and app password; Basic plans exclude external-client access. Regional endpoints may differ. [Official source](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports) |
| Migadu | Switzerland | supported | Both POP and IMAP supported. Use mailbox credentials rather than administrative API credentials. [Official source](https://www.migadu.com/guides/) |
| MXroute | United States | supported | IMAP, POP3 and SMTP supported. Use the assigned account server, not a guessed MX record. [Official source](https://docs.mxroute.com/docs/presales/faq-extended.html) |
| mailbox.org | Germany | supported | TLS POP on 995; application password may be needed with two-factor authentication. [Official source](https://kb.mailbox.org/en/business/e-mail/e-mail-configuration/) |
| Runbox | Norway | supported | POP and IMAP are available. Confirm current encrypted connection settings. [Official source](https://help.runbox.com/pop/) |
| Purelymail | United States | supported | Supports POP and IMAP. Confirm the POP hostname in account documentation. [Official source](https://support.purelymail.com/support/solutions/articles/159000430778-server-settings-imap-smtp-and-pop3) |
| BiNDup / WebLiFE | Japan | unsupported | Official setup requires unencrypted POP. Direct capture is intentionally disabled; obtain EML exports or confirmed secure access from the operator. [Official source](https://bindup.jp/support/09/0906/09090600m/) |
| MINC Internet | Japan | unsupported | POP-only; documented 365-day retention and 1 GB account quota. Older mail may exist only on computers. [Official source](https://support.minc.ne.jp/faq/mail/) |
| Kamome Internet | Japan | unsupported | POP-only; documents SSL on 995 and 100 MB mailbox quota. Username is provider-issued account. [Official source](https://www.kamome.or.jp/member/mail/) |
| Itscom | Japan | unsupported | Official FAQ explicitly rules out IMAP. Obtain the account-specific secure POP settings. [Official source](https://faq.itscom.net/faq/show/12640?site_domain=default) |
| Sano Cable TV | Japan | unsupported | October 2026 support notice says POP3 only and no current IMAP plans. Confirm sctv.jp versus mirun.sctv.jp account settings. [Official source](https://www.sctv.jp/internet/support/support_top.html) |
| Marinet / Miyagi Cable TV | Japan | unsupported | 2026 migration notice rules out IMAP; SSL POP on 995 is documented. Use full email address as username. [Official source](https://www.m-marinet.co.jp/maintenance/detail.php?id=351) |
| GMO Tokutoku BB | Japan | unsupported | Basic/additional ISP mail is POP-only; server retention is 180 days. Large parent company, not a small independent host. [Official source](https://help.gmobb.jp/faq/faq_detail.html?id=1021204) |
| Dynadot Email Free | United States | plan_restricted | Free plan provides POP-only remote access; Pro adds IMAP and SMTP. This is a plan restriction, not proof of old software. [Official source](https://www.dynadot.com/it/help/question/36) |

## Meaning of labels

- `unsupported`: cited provider guidance says no IMAP for the named service; Cafe24 is scoped to the existing project's POP/webmail source.
- `plan_restricted`: the provider offers IMAP in other plans/products. Verify the actual subscription.
- `supported`: IMAP is documented; these must never be marketed as POP-only.
- `not_documented`: inspected primary source directs POP but does not establish global IMAP absence.
- `export_only`: direct capture is disabled because inspected guidance requires plaintext. BiNDup currently falls in this category.

A documentation date records this research, not the provider's last system update. All captured inventories still need mailbox-specific completeness checks.
