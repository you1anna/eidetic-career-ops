---
description: Deploy pushed main to work.example.com (the owner types it; local sessions only)
allowed-tools: Bash(${CLAUDE_PROJECT_DIR}/ops/local/deploy_session.py)
disable-model-invocation: true
---

The owner invoked the shared deployment workflow in `docs/deploy.md`.
Claude Code attempts the runner before this prompt reaches you. Its output:

!`${CLAUDE_PROJECT_DIR}/ops/local/deploy_session.py`

Read and follow `docs/deploy.md`, using this output. Do not invoke the runner again.
If the pre-prompt step did not run or was rejected, follow the shared failure instructions.
