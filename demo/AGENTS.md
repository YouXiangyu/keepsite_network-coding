# MemoVerse Web App Agent Guide

Updated: 2026-10-20.

## Current platform decision

- On 2026-10-18 the user explicitly required a real responsive Web App. Android APK development is a separate later phase. The five mobile design references describe the future APK; preserve their white, photo-led visual language without rendering a simulated phone in the website.
- The production Web entry must not mount PhoneFrame, MobileRuntime, fake status bars, device selectors, simulated keyboards, or phone-scoped scroll/portal contexts. Use browser-native forms, scrolling, keyboard behavior, responsive layout, and document-level dialogs.
- This decision authorizes changing the former protected Web entry, global CSS, Vite configuration and runtime validation contract. Do not add compatibility adapters around mobile hooks. Avoid unrelated changes to unused reference assets or runtime modules.
- Desktop uses a wide content area and suitable navigation; small browser viewports remain responsive. Preserve collection, category, explore, shares and profile workflows. Browser back, refresh and direct links must work.

## Authentication and account boundaries

- Anonymous visitors see only sign-in, registration, email verification and password recovery. Do not mount business pages, read legacy IndexedDB, initialize data, or fetch private images/models before the server confirms a session.
- Support Google sign-in and email/password registration with email verification. Google and email delivery are only complete after actual provider verification, not merely displaying buttons.
- The backend owns identity, initialization and access checks. Each account gets independent initial records from the explicitly approved current test template. Repeated login must never reseed or overwrite edits.
- Existing local data must remain intact; do not silently assign an unowned browser workspace to whichever user signs in first.
- Use same-origin /api requests and server-issued HttpOnly cookies. Never store Cognito access, ID or refresh tokens in localStorage, frontend source or browser-readable cookies. Mutations require the server session's CSRF token.
- Key the business component tree by authenticated user ID. Cancel in-flight requests and polling on logout/account change, reject responses from older sessions, and release WebGL/object URLs. Do not delete the old persistent workspace as part of logout.
- Backend state must match the current user ID. Files use authenticated backend URLs; all reads, updates, jobs and exports must be authorized on the server.

## Product and visual rules

- One image is one collectible. Preserve user edits and show persistence failures clearly.
- Keep the neutral white background, strong dark headings, photo-led cards and readable labels. Use responsive grids instead of fixed phone widths or fake top/bottom chrome.
- Use native input/textarea elements, explicit labels, password manager/autocomplete support, keyboard focus indicators and accessible error/loading feedback. Existing Radix dialogs may portal to the document body.
- The relief viewer permits at most 40 degrees of total deviation from the front, including diagonals. Pointer position presses that edge/corner away from the camera. A hover effect is not proof of actual model generation.
- AnyImage currently provides official MoGe2 CPU depth inference plus project relief mesh/GLB export. Do not call it a complete 360-degree object reconstruction.
- Explore and received-share initial content are test examples. New user uploads stay private unless the user explicitly shares them. Do not add check-in or wanted/experienced states.
- AWS S3 is the selected object storage. No Google Cloud Storage, paid model API, Meshy integration or APK implementation is implied by this phase.

## Verification and delivery

- Follow the root AGENTS.md and memoverse-delivery skill. Keep changes on D:, announce writes/builds/downloads first, commit completed logical units and obtain independent review for the stage.
- Run npm run check:runtime and the meaningful Web/auth tests before preview or handoff. Update the former mobile lock/contract only for the explicitly authorized Web runtime replacement, after checking the new behavior. Do not weaken checks to conceal failures.
- Verify anonymous gating; registration/verification/login/logout/expired sessions; two-account isolation; upload/edit/delete/generation/export; responsive widths 360/390, 768 and 1280/1440; native keyboard navigation; browser back/refresh; and the 40-degree relief constraint.
- In Codex Desktop, start the needed local preview and inspect it with browser tools. Keep a useful preview available for user handoff, and close temporary tabs and idle owned processes. Deploy to AWS, start/stop EC2 or merge PRs only when the user explicitly requests it; deployment uses the existing Ubuntu/EC2 workflow. Do not publish to Sites unless requested.
- Keep credentials, databases, templates containing user data, model artifacts, caches and temporary packages out of Git. Preserve still-needed data, models, credentials and exact deployment artifacts. Respect previously blocked cleanup decisions.
