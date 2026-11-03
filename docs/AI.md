# Architect

The Architect is an optional, **read-only** assistant (`core/modules/advisor/`). It explains mesh state and incidents without executing commands, changing settings, installing updates, or calling tools.

Responses use short Markdown sections, tight bullets, and code spans when useful. The UI renders that Markdown in a console with a daily budget meter and message history.

## Why read-only

Giving an LLM write access to a network control plane is a bad trade:

- it can hallucinate;
- it can be steered by prompt injection in logs or device names;
- it adds an external network dependency to local administration;
- it burns tokens when nothing is happening;
- it makes failures harder to reproduce.

All state changes stay behind normal hub APIs and quorum approvals.

## What it receives

A sanitized snapshot:

- hub names, online state, coordinator flag, platform, version;
- service names and health;
- module states;
- number of pending approvals and bad releases;
- optionally, the last 40 lines of `mesh.log`, with secret-like values redacted.

It does **not** receive:

- owner identities or IP allowlists;
- API keys or tokens;
- clipboard contents;
- file contents;
- terminal output;
- the full directory or internal quorum state.

## What it can do

- Explain a state or incident.
- Summarize the last day.
- Point out an offline hub or a failing service.
- Suggest a remediation for a human to approve.
- Draft an incident note.

It cannot run the remediation.

## Configuration

Enable **Architect** in **Control › Modules**.

| Field | Default | Notes |
|---|---|---|
| `base_url` | `https://api.openai.com/v1` | Any OpenAI-compatible `/chat/completions` endpoint |
| `model` | `gpt-4o-mini` | Provider-specific |
| `api_key` | empty | Masked in the UI; stored in `state/modules/advisor.json` |
| `max_tokens` | 4000 | 64-4000; reasoning models may need 2000+ before producing a final answer |
| `daily_requests` | 50 | 1-10000; enforced locally |
| `language` | `auto` | `auto`, `en`, or `es` |
| `include_logs` | false | Opt-in; redacted before sending |

Usage is stored in `state/modules/advisor_usage.json`.

The console renders a safe Markdown subset. The service normalizes em/en dashes to commas or hyphens, and asks the model not to use decorative separators. If a reasoning model consumes the whole budget before producing a final answer, the API returns a clear error; if the final answer itself is cut off, the API returns `truncated: true` and the UI shows a warning instead of silently ending mid-sentence.

Legacy `mesh.log` lines from older Spanish builds are translated before they enter the advisor context. The current hub/module state is always given more weight than historical log entries.

## Cost and privacy

- Prefer event-driven or on-demand calls over a constant heartbeat.
- Use one coordinator, not every hub, if you run a shared budget.
- A local model can be used through an OpenAI-compatible endpoint; quality and RAM depend on the device.
- Treat the provider as a third party that receives the sanitized snapshot.
- Do not enable logs unless you need them.
- Keep a daily request budget.

## Prompt injection

Context is wrapped as untrusted data and the system prompt forbids following instructions found inside it. That reduces risk but does not eliminate it. The decisive control is that the Architect has **no tools** and no write path.

If tool use is added later, it must be:

- read-only first;
- allowlisted;
- explicitly confirmed by the user;
- audited;
- never able to bypass the quorum for terminal, shutdown, or hub removal.

## Failure behavior

- Disabled: the page explains how to configure it.
- Missing key: `needs_config`.
- Provider error: the UI shows an error; the hub keeps working.
- Daily budget reached: HTTP 429.
- Hub restart: configuration and usage persist; no background loop.

## Future ideas

- "Explain this alert" button on the dashboard.
- Daily brief generated once, not continuously.
- Read-only Q&A over the catalogue and monitor history.
- Suggested actions with an explicit approval button.
- Local-only provider preset.

Not planned: autonomous remediation, shell access, or an always-on agent with write tools.
