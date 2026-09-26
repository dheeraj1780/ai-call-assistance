# Vendored: Google Meet Media API TypeScript reference client

- Source: https://github.com/googleworkspace/meet-media-api-samples (`web/internal`, `web/types`)
- Upstream commit: `1961cee` (2026-09-11)
- License: Apache License 2.0 (see `LICENSE`), Copyright 2024 Google LLC. Original file headers kept.
- Not published on npm, hence vendored. Excluded from our ESLint rules.

## Local modifications (only to compile under this app's TypeScript settings)

1. Type-only imports marked `type` (86 specifiers in 13 files; 30 imports whose names are all
   types rewritten as `import type {...}`) because the app uses `verbatimModuleSyntax`; without it
   the browser would try to load `.d.ts` modules at runtime. No behaviour change.
2. `noUncheckedIndexedAccess` fixes:
   - `media_entries_channel_handler.ts`: `videoCsrcs[0]!` (upstream already checks `length > 0`).
   - `session_control_channel_handler.ts`: `json.resources[0]!` (upstream already checks `length > 0`).
   - `media_stats_channel_handler.ts`: `if (!resource) return;` after `resources[0]` - upstream
     would throw on an empty resource list; we ignore the message instead.
   - `media_stats_channel_handler.ts`: `as string` on the stats-type key (type-only).
3. BUILD (Bazel) files removed.

The client is used through `src/lib/meet/` only; the rest of the app never imports it. Our code
injects its own `MediaApiCommunicationProtocol` so the SDP offer goes to the CallCopilot API,
which calls `connectActiveConference` with the user's token (the token never reaches the browser).
