# Third-party notices

This repository does **not** vendor third-party source code. It calls the following tools as external dependencies, each under its own license:

| Tool | Used in | License |
|---|---|---|
| [Playwright for Python](https://github.com/microsoft/playwright-python) | `ui-playwright/` | Apache-2.0 |
| [Gitleaks](https://github.com/gitleaks/gitleaks) | `sec-audit/` | MIT |
| [Promptfoo](https://github.com/promptfoo/promptfoo) | `ai-evals/` | MIT |

Ideas, not code:
- **Visa Vulnerability Agentic Harness (VVAH)**, Apache-2.0. We borrow the split between deterministic stages and agentic stages, and the practice of shipping operating instructions for agents (`AGENTS.md`, `CLAUDE.md`).
- **OWASP Top 10 for LLM Applications**, CC BY-SA 4.0. Used as the taxonomy for AI-chat checks.
- **Kaner, Falk, Nguyen, *Testing Computer Software*.** Its test design techniques and bug report format.

If you add code or prompt sets copied from another project, keep its original copyright header and add a row to this file.
