# Deliver a page without repeating setup

[Documentation index](README.md) · [Repository home](../README.md)

Use for pages and explanatory diagrams belonging to work-ops. This is delivery guidance, not
a second release procedure; [operations](../ops/OPERATIONS.md#deploying) owns deployment.

## Decide the destination before the design

Default to a route on `https://work.example.com`. A request for a “new URL” does not
by itself request a separate host, repository or hosting provider. State the intended URL and
public/private audience in the first update. Ask only if a consequential choice is unresolved.
Check the existing route and hosting configuration before provisioning anything.

For a public page beside this private workspace, inspect both edge Access rules and tunnel
origin token validation. Public-page approval does not authorise exposing the workspace or APIs.
Scope the page and its assets explicitly; verify the private routes still require sign-in.
Current access evidence belongs in the delivery ledger, not a permanent claim in this guide.

For a move, reuse the finished files and interactions. Check route/trailing-slash behavior,
asset and download paths, canonical/Open Graph URLs and any URL embedded in a downloadable brief.
Do not restart design generation or install a framework for a static-page relocation.

## Reuse verification before writing another harness

Locate the page's existing tests in `tests/ui/` and route assertions in `workspace/test_api.py`.
Use [focused checks](commands.md#focused-development-checks) while editing and retain the
required final release gate. Reuse the pinned Playwright/browser setup in
[local development](../ops/LOCAL.md); do not install another browser toolchain by default.

The internal map already has a reusable smoke module, `tests/ui/system-map.mjs`.
The career/ops delivery record locates its
source and tested journey matrix. Check current files before assuming a historical path is current.

For an interactive public page, cover:

- Each stage and perspective; keyboard/focus state; real links and downloads.
- Desktop and phone layouts, overflow, console errors and unexpected network requests.
- Fictional fixtures only; no connections to private data for a pitch demonstration.
- Anonymous hosted access and continued protection of private routes after routing changes.

Capture after functional assertions, reuse the same preview process, inspect the resulting
images and stop the owned process at handoff. A clipped browser-tool screenshot is not proof
of page overflow: compare its capture dimensions with the page viewport before changing CSS.
Do not silence runtime errors or weaken CSP to accommodate screenshot instrumentation.

## Keep the next session small

Read required entry points once per session; retain their conclusions. For further investigation,
use `rg` to locate the relevant section and read that section, rather than repeatedly dumping
whole runbooks, skills or tool catalogues. Rediscover a tool schema only when needed or changed.

Keep one delivery ledger with source location, destination, audience, deployed revision/version,
checks actually run and remaining limits. Link it from the existing plan index; do not create
parallel handover reports. Record working capability, proposed capability and demonstrated
usefulness separately.

If separate Sites hosting is explicitly chosen, follow its current skill rather than copying its
API workflow here. Reuse the persisted project ID. Obtain short-lived push credentials near
source publication; if expired, renew for that same project. Never store credentials in a ledger.
