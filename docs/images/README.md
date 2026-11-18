# User-guide captures

[Documentation index](../README.md) · [User guide](../../USER-GUIDE.md)

Synthetic fixture captures (`1300 × 900` desktop Chromium) generated from `tools/make_fixture.py`:

- `this-week.png` — **This week** view before contact.
- `pipeline.png` — **Pipeline** view with active conversations and the recorded-results line.

To regenerate locally:

```sh
WO_GUIDE_CAPTURE_DIR=/tmp/work-ops-guide-captures node tests/ui/user-guide.mjs
cp /tmp/work-ops-guide-captures/this-week.png /tmp/work-ops-guide-captures/pipeline.png docs/images/
```
