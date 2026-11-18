# Browser diagnostics

Start with the failing repository test and its original logs, screenshot and
bounded console/request evidence. Preserve the original exit status when capture
fails. Expected HTTP errors in recovery tests are diagnostic events, not automatic
test failures. Diagnostic summaries should omit authorization headers, cookies,
response bodies and URL query strings.

For a timing/order failure, request a trace through the project's documented
switch and rerun only the affected suite. Keep the first failure visible even if
the diagnostic rerun passes. Trace recording has CPU/storage cost; a retention
limit does not eliminate collection overhead. Raw traces can contain request
headers, bodies and page content; use only synthetic flows without authenticated
sessions. Remove diagnostic artifacts through the documented retention procedure.

Use Chrome DevTools only when available and relevant: performance traces for
long tasks/layout shifts, network timing for slow resources, or heap inspection
for suspected retained objects. Discover tool capabilities and follow the active
browser skill; do not assume an installed performance skill provides a connected
DevTools server. If unavailable, continue supported checks and name the missing
measurement. Chromium profiling does not establish WebKit behaviour.

Do not add Storybook to a project merely to inspect a handful of UI states; use
it when an existing component catalogue or repeated component work justifies its
fixture/build maintenance. Production error services require separate integration,
data filtering and applicable deployment/access authority. They complement local
tests and do not prove that a repair works until the original case is reproduced.
