# Privacy

This document describes what the application stores, why, for how long, who can access it and
how it is deleted. It is an engineering description, **not legal advice or a certification**
(e.g. of DPDP Act compliance) — REQUIRES LEGAL REVIEW before real customer data is processed.

Transcripts and extracted customer information **are personal data**. Not storing audio does
not remove privacy obligations.

## What is stored

| Data | Why | Retention | Deletion |
|---|---|---|---|
| Raw call audio | **Never stored or recorded**, for every provider (Plivo, Teams, Google Meet, mock). Audio is held only in bounded in-memory queues on its way to speech-to-text and then dropped. No provider recording feature is used. No database column can hold binary data (a test asserts this). | none | n/a |
| Live-only calls (Teams by default; Google Meet if chosen) | Transcript, AI notes and cards are kept **in server memory for the duration of the call** (so a refreshed screen can resume) and never written to the database. The call record, status, outcome, action items and manually written notes are kept. For Teams this follows Microsoft's Graph terms: media-derived data may not be persisted unless the bot first called `updateRecordingStatus` successfully (which requires Teams policy-based recording). | until the call's live session closes | automatic |
| Partial (interim) transcripts | Shown live in the browser only; never written to the database. | none | n/a |
| Final transcript segments (speaker, text, timing, confidence; a person's correction keeps the STT text as `original_text`) | Live transcript, copilot, post-call summary | `expires_at` = creation + company `transcript_retention_days` (default **30**, 1–365, set in Settings) | Hourly retention job deletes expired rows (and cascades with the call/contact) |
| Live copilot cards (may quote the customer) | Assist the salesperson during the call | Same as transcript (`expires_at`) | Retention job |
| Structured call notes (requirement, objection, budget, …) | Business record of the conversation; human-reviewed | Until deleted by a user (they lose the link to the purged transcript segment) | Delete note / call / contact |
| Call summary & follow-up drafts | Post-call record | Until deleted with the call/contact | Cascade |
| Contacts, notes, action items, timeline | CRM | Until deleted | Contact delete cascades to calls, transcripts, notes, drafts, timeline |
| Knowledge documents | Company knowledge retrieval | Extracted text chunks + embeddings until deleted; **original files are not stored** | Delete document (chunks cascade) |
| Google OAuth refresh token | Calendar access; Google Meet (per salesperson) | Until disconnect; encrypted at rest (Fernet) | Disconnect (token revoked at Google, best effort) |
| Microsoft delegated tokens (Teams messaging, per salesperson) | Teams chat messages | Until disconnect; encrypted at rest | Disconnect |
| Provider credentials (Plivo auth token, WhatsApp token, …) | Integrations | Until the admin removes the integration; encrypted at rest, write-only in the UI | Disconnect integration |
| AI usage records | Cost control | Until deleted with the company | Cascade |
| Audit log | Security | Until deleted with the company (append-only for the app) | Operator action |
| Application logs | Operations | Host log retention | Never contain passwords, tokens, transcript text or request bodies; DB errors hide bound parameters |

## Access

- Everything tenant-owned is protected by application checks **and** PostgreSQL Row-Level
  Security; users only see their own company's data.
- The retention job is the only cross-tenant access to transcripts, through a policy that allows
  deleting **only expired rows**.

## Third parties (when real providers are configured)

Audio/text may be processed by the telephony provider (Plivo; optionally Microsoft Teams / Google
Meet), the STT provider (Google Cloud Speech-to-Text), the LLM provider (Anthropic or Google Gemini)
and the embedding provider (Voyage or Gemini). Their data residency, retention and training
policies must be reviewed and documented before production use — REQUIRES PROVIDER CONFIRMATION /
REQUIRES LEGAL REVIEW. The default configuration uses only local mock providers.

## Not yet implemented (FUTURE)

- Customer calling consent capture and a configurable call-processing announcement.
- Data subject request tooling (export/erase per individual beyond deleting the contact).
- Per-company data residency options.
