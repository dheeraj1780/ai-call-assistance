# Privacy

This document describes what the application stores, why, for how long, who can access it and
how it is deleted. It is an engineering description, **not legal advice or a certification**
(e.g. of DPDP Act compliance) — REQUIRES LEGAL REVIEW before real customer data is processed.

Transcripts and extracted customer information **are personal data**. Not storing audio does
not remove privacy obligations.

## What is stored

| Data | Why | Retention | Deletion |
|---|---|---|---|
| Raw call audio | **Never stored.** Audio chunks are passed from the provider stream to the STT stream in memory and dropped. No database column can hold binary data (a test asserts this). | none | n/a |
| Partial (interim) transcripts | Shown live in the browser only; never written to the database. | none | n/a |
| Final transcript segments (speaker, text, timing, confidence) | Live transcript, copilot, post-call summary | `expires_at` = creation + company `transcript_retention_days` (default **30**, 1–365, set in Settings) | Hourly retention job deletes expired rows (and cascades with the call/contact) |
| Live copilot cards (may quote the customer) | Assist the salesperson during the call | Same as transcript (`expires_at`) | Retention job |
| Structured call notes (requirement, objection, budget, …) | Business record of the conversation; human-reviewed | Until deleted by a user (they lose the link to the purged transcript segment) | Delete note / call / contact |
| Call summary & follow-up drafts | Post-call record | Until deleted with the call/contact | Cascade |
| Contacts, notes, action items, timeline | CRM | Until deleted | Contact delete cascades to calls, transcripts, notes, drafts, timeline |
| Knowledge documents | Company knowledge retrieval | Extracted text chunks + embeddings until deleted; **original files are not stored** | Delete document (chunks cascade) |
| Google OAuth refresh token | Calendar access | Until disconnect; encrypted at rest (Fernet) | Disconnect (token revoked at Google, best effort) |
| AI usage records | Cost control | Until deleted with the company | Cascade |
| Audit log | Security | Until deleted with the company (append-only for the app) | Operator action |
| Application logs | Operations | Host log retention | Never contain passwords, tokens, transcript text or request bodies; DB errors hide bound parameters |

## Access

- Everything tenant-owned is protected by application checks **and** PostgreSQL Row-Level
  Security; users only see their own company's data.
- The retention job is the only cross-tenant access to transcripts, through a policy that allows
  deleting **only expired rows**.

## Third parties (when real providers are configured)

Audio/text may be processed by the telephony provider, the STT provider, the LLM provider
(Anthropic) and the embedding provider (Voyage). Their data residency, retention and training
policies must be reviewed and documented before production use — REQUIRES PROVIDER CONFIRMATION /
REQUIRES LEGAL REVIEW. The default configuration uses only local mock providers.

## Not yet implemented (FUTURE)

- Customer calling consent capture and a configurable call-processing announcement.
- Data subject request tooling (export/erase per individual beyond deleting the contact).
- Per-company data residency options.
