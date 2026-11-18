# Visual comparisons

Use the repository's visual runner, not a second screenshot framework. Inspect
its help/configuration before running it. Baselines belong to the project;
screenshots containing real records or private browser sessions do not.

1. Use synthetic fixtures with a fixed clock, locale, timezone, viewport, fonts
   and browser/platform. Wait for the intended UI state and loaded fonts rather
   than an arbitrary sleep. Disable animations for capture. Avoid masking the
   actual feature under test.
2. Check baseline environment metadata before comparing. A missing reference or
   incompatible environment is unavailable verification, never a passing test.
   Do not regenerate baselines automatically to hide that mismatch.
3. Compare stable components or representative viewports. Keep behavioural
   assertions for content, interactions and data; pixel agreement cannot prove
   that a design is correct or usable.
4. On failure inspect expected, actual and difference images. Trace the change
   to an intended requirement or fix the regression. Update only reviewed,
   intended references with the explicit project update command. Record why
   each changed state is expected; then rerun normal comparison without update.
5. When introducing a comparator or guard, deliberately change a visible style
   or known pixels and show failure, restore it and show success. Repeat stable
   captures to check for rendering noise. Do not increase tolerances solely to
   hide inconsistent results.

Keep failure outputs private and bounded. Store baselines in version control
with their environment metadata; retain diagnostic artifacts separately. Use
the project's removal/recovery procedure rather than deleting shared temp roots.

Playwright Test provides `toHaveScreenshot`; a custom `playwright-core` harness
does not gain this assertion merely by installing the core browser package.
Reuse the project's comparator rather than migrating its runner by default.
