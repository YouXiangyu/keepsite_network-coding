# MemoVerse Web App Agent Guide

Updated: 2026-10-22.

## Product and platform

- The current product specification is `docs/项目定义与决策主文档.md`; implementation order is `docs/开发计划.md`. The October 21–22 redesign is a documentation decision, not proof that the new architecture is already implemented.
- Deliver one responsive Web application for personal movie tickets across desktop, phone and tablet browsers. After Web completion, continue the native Android phase outside `demo/`.
- Remove relief/3D, categories/tags, badges, holiday effects, social sharing, community exploration and the full offline editing model when implementing the corresponding milestone. Old screenshots and five-page navigation are no longer requirements.
- The production entry must not mount PhoneFrame, MobileRuntime, fake status bars, device selectors, simulated keyboards, or phone-scoped scroll/portal contexts. Use browser-native forms, scrolling, keyboard behavior and document-level dialogs.
- Desktop uses an appropriate wide layout; small viewports remain responsive. Browser back, refresh and direct links must work. Update obsolete runtime contracts for authorized new behavior without compatibility adapters or weakened checks.

## Authentication and account boundaries

- Anonymous visitors see only sign-in, registration, email verification and password recovery. Do not mount private business pages or open device transfers before the server confirms a session.
- Preserve Google sign-in and email/password flows. Provider verification and actual email delivery are distinct from mocked tests.
- The backend owns identity and authorization. The redesigned product starts new users with an empty ticket library; do not seed the old collection/social/model template.
- Use same-origin requests and server-issued HttpOnly cookies with CSRF protection. Never expose Cognito tokens in frontend source, localStorage or browser-readable cookies.
- Key business state and device media by account. On logout, expiry or account change, cancel requests, polling/subscriptions and peer transfers; reject stale responses and release temporary object URLs.
- Logout does not silently delete persistent media or assign it to another account. Existing user data must remain intact until an explicit migration or cleanup plan is authorized.
- A file hash identifies content; it never authorizes access. Validate ownership for metadata, jobs, device registration, signaling, media requests and transfer credentials.

## Movie-ticket workflows and media

- The flow is upload, server OCR, poster retrieval, cover display, correction, deletion and same-account device synchronization.
- Preserve the original ticket photo. Use a reliable poster match as the cover; otherwise show the original and the real processing result.
- Distinguish original/poster cloud-save status, OCR processing and local-cache availability. Confirm cloud saving only after object verification and a durable owned reference; an upload intent or URL is not proof of success.
- Store originals and posters durably in private S3. Browser files are reclaimable per-account caches; clearing them does not delete cloud records. Unsaved upload drafts do not have the same recovery guarantee. Do not introduce offline CRUD, local OCR or indefinite background transfers.
- Use valid local cache, bounded P2P/optional TURN attempts, then authorized S3 retrieval. Trigger S3 fallback on missing peers, timeouts, stalled transfers or checksum failures. Cancel redundant transfers and report cloud errors or storage limits accurately.
- User corrections and deletion take precedence over stale asynchronous results. Synchronization uses the specification's version, cursor and idempotency rules.
- Use neutral backgrounds, readable labels, native inputs, visible focus, accessible status/error feedback and responsive image-led layouts. Do not copy obsolete visual references.
- Use lightweight server-side CPU OCR and validate accuracy, process memory and latency. Do not introduce paid OCR/model APIs or a new provider by implication. Continue private S3 for media, with encryption, HTTPS and short-lived authorized access; preserve old user data during migration.
- Cloud media survives browser cache eviction. Logout is not deletion; record deletion revokes future authorization and propagates tombstones, while object/version cleanup follows the documented policy. Existing signed URLs retain their own expiry.
- The user deferred the always-available signaling/API hosting decision. Do not change EC2 shutdown policy or claim the application remains accessible while its required services are stopped.

## Verification and delivery

- Follow the root AGENTS.md and project delivery skill. Use D:, announce writes/builds/downloads first, commit completed logical units and obtain independent stage review.
- For application changes, run the runtime contract and relevant authentication, workflow, synchronization, transport and build checks. Update tests with the approved behavior; do not preserve removed features merely to satisfy old tests or weaken assertions to hide failures.
- Verify anonymous gating, session lifecycle, two-account isolation, upload/correction/deletion, task failure recovery, reconnect catch-up, cache eviction recovery, bounded direct/relay attempts and S3 fallback, browser back/refresh, keyboard access and responsive widths 360/390, 768 and 1280/1440 as each milestone lands.
- Browser automation is not evidence of physical phone/tablet acceptance. Real-device acceptance remains with the user; Android has its own later stage.
- For documentation-only changes, check consistency and local links without starting previews or running the whole application test suite.
- Keep useful requested previews available and close temporary tabs and idle owned processes. Deploy, start/stop EC2, publish elsewhere or merge PRs only when explicitly requested.
- Keep credentials, databases, user images, model artifacts, caches and temporary packages out of Git. Preserve needed rollback artifacts and respect previously blocked cleanup decisions.
