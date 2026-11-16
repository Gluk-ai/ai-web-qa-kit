# ai-web-qa-kit

**Find the bugs AI-built sites and business chatbots ship with before your clients run into them.**

Sites made with Lovable, Bolt or Cursor and chatbots built on n8n go live fast and fail in predictable ways:

| What breaks | What it costs | Module |
|---|---|---|
| The bot invents a price, promises a deadline or agrees to a fake discount | Legal and reputational risk | `ai-evals/` |
| The bot says "we're not a fit" to a hesitant client | Lost lead | `ai-evals/` |
| A layout that looks fine on a laptop overlaps or scrolls sideways on an iPhone SE | Bounced visitors | `ui-playwright/` |
| A `VITE_*` secret or service key gets bundled into `dist/` | Leaked API bill or data breach | `sec-audit/` |
| Supabase tables readable by anyone holding the public anon key | Leads and customer data exposed | `sec-audit/` |

## Architecture

The design borrows one idea from [Visa's VVAH](https://github.com/visa/visa-vulnerability-agentic-harness): **deterministic steps first, agent judgement second.**

```
            deterministic (scripts, CI, exit codes)               agentic (LLM)
 ┌───────────────────────────────────────────────────────┐   ┌───────────────────────┐
 │ ui-playwright/audit_ui.py  device×language matrix → JSON│   │ skills/qa-bug-hunter  │
 │ sec-audit/  gitleaks + check_rls.py           → JSON  │──▶│ reads the reports,     │
 │ ai-evals/   promptfoo test matrix             → JSON  │   │ triages, writes bug    │
 └───────────────────────────────────────────────────────┘   │ reports + fix patches  │
                                                             └───────────────────────┘
```

Scripts measure the facts: pixel overlaps, CLS, HTTP status codes, rows returned. The agent never guesses those. It only interprets the reports, ranks severity and writes reproducible bug reports in the Kaner format: steps, expected result, actual result.

## Quick start

```bash
pip install -r requirements.txt && playwright install chromium

# 1. Layout across 10 devices × your languages
python ui-playwright/audit_ui.py --url https://your-site.com --langs /,/fr/

# 2. Secrets in git history and in the built bundle
gitleaks git . -c sec-audit/.gitleaks.toml
python sec-audit/scan_build.py dist/   # refuses an empty dist/, decodes JWT roles

# 3. What can an anonymous visitor read or write in your Supabase project?
SUPABASE_URL=... SUPABASE_ANON_KEY=... python sec-audit/check_rls.py

# 4. Chatbot quality (facts, sycophancy, lead loss, scope) - TEST COPY of the bot only, see warning below
BOT_WEBHOOK_URL=... RUN_ID=$(date +%s) npx promptfoo@latest eval -c ai-evals/promptfooconfig.yaml
```

> **Warning: point `ai-evals/` at a test copy of your bot, never at production.** The tests send
> real messages. An objection test can trigger a real lead handoff (Telegram/CRM/email) or an outgoing
> SMS/WhatsApp. Duplicate the workflow, disable lead and messaging actions, and use that webhook.

Every script exits non-zero when it finds a problem, so each one works as a CI gate. Ready-made GitHub Actions live in `.github/workflows/`.

## What each check catches

- **`audit_ui.py`**: horizontal scroll, elements past the screen edge, overlapping top-level blocks (`getBoundingClientRect`), CLS ≥ 0.1 computed web.dev-style (session windows, separately for load and for scrolling, with the shifted elements named), `scrollY` jumping backwards while scrolling down, too many font families, console errors and 4xx/5xx responses. It checks each cell at the top of the page and again after scrolling to the bottom, and saves a full-page screenshot of every cell.
- **`check_rls.py`**: anonymous reads (rated CRITICAL for tables named like `leads`, `users` or `payments`), anonymous update/insert/delete (opt-in `--write-probe TABLE`: the script creates its own probe row with the service key, checks whether it actually changed and cleans up; live rows are never touched), and a `service_role` key used where an anon key belongs. Before the live test, open **Security Advisor** in the Supabase dashboard: it catches views and `security definer` functions that bypass RLS.
- **`scan_build.py`**: wraps Gitleaks for `dist/`. An empty folder is an error, not "clean". Findings (exit 2) are kept apart from crashes (exit 1). Supabase anon JWTs are recognised as public, while `service_role` JWTs are flagged.
- **`.gitleaks.toml`**: the Gitleaks defaults plus rules for secret-looking `VITE_*` variables and n8n API keys. Use Gitleaks 8.30 or newer: older releases do not detect Anthropic keys.
- **`promptfooconfig.yaml`**: knowledge gaps, false-premise sycophancy, promises on the company's behalf, objection handling, off-topic and competitor requests, card numbers echoed back, and replies in the wrong language.

## Scope and ethics

Only test systems you own or have written permission to test. See [SECURITY.md](SECURITY.md).

## License

MIT, © 2026 Vasyl Kravets ([CorePulse](https://corepulsedev.com)). Third-party tools are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
