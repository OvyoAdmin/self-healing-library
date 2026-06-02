
# HealingSelenium

**Dynamic wrapper around SeleniumLibrary with self‑healing locators & safety nets**

> Works as a drop‑in replacement around [Robot Framework](https://robotframework.org/) SeleniumLibrary: you keep using the same Selenium keywords, while `Click Element` and `Input Text` gain self‑healing powers.

---

## ✨ Why this library?
UI tests often become flaky when page structure changes and locators break. **HealingSelenium** automatically recovers from broken locators by asking a local LLM (via **Ollama**) for a better selector, validating it against the live page, **persisting** the healed choice, and (optionally) **rewriting your source files**. By default it **fails the test after healing**, so the team notices and commits the change—flakiness is reduced without hiding real issues.

---

## 🚀 Highlights

- **Drop‑in wrapper**: Import this library **once** in Robot and continue using **all SeleniumLibrary keywords**; only `Click Element` and `Input Text` are overridden to add healing.
- **LLM‑assisted healing (Ollama)**: When a coded locator fails, the library:
  1. asks an Ollama model for an alternative selector using the **current page HTML**,
  2. **validates** the suggestion in the live DOM (visibility/uniqueness are configurable),
  3. **persists** the healed selector with timestamp & history,
  4. **optionally rewrites** your repo to replace old locator literals, and
  5. **intentionally fails** the test so the change is reviewed and committed.
- **Audit‑ready**: Snapshots go to `healed_locators.json`; a detailed event stream is appended to `healed_locators_history.jsonl` (heal + rewrite events).
- **Failure artifacts**: Captures **screenshots** (and optionally **HTML**) on failures or bad suggestions to speed up triage.
- **Safety nets**: Convenience keywords to ensure a browser is open (`Ensure Browser Open`, `Open Browser If Needed`) and to query browser state.
- **Runtime toggles**: Turn healing and auto‑rewrite **on/off** at runtime from your test cases.

**Library scope**: `GLOBAL`  •  **Version**: `2.0.0`

---

## 🔧 Requirements

- **Python** 3.8+
- **Robot Framework** and **SeleniumLibrary**
- **Selenium WebDriver** for your browser (e.g., ChromeDriver)
- **Ollama** running locally (default `http://localhost:11434`) with a model available (default `llama3`)
- Python package **`requests`** (used to call Ollama)

> The library is a single file (`HealingSelenium.py`). Place it in your project and import as a Robot library (see Quick Start).

---

## 📦 Installation

1. Install dependencies (example):

```bash
pip install robotframework SeleniumLibrary requests
```

2. Ensure **Ollama** is up and your model is pulled (e.g., `ollama pull llama3`) and the server is running on `http://localhost:11434`.

3. Add `HealingSelenium.py` to your repository (e.g., under `resources/` or project root).

---

## 🏁 Quick Start

```robot
*** Settings ***
Library    HealingSelenium.py    sl_run_on_failure=Capture Page Screenshot
Suite Setup       Open Browser    ${BASE_URL}    ${BROWSER}
Suite Teardown    Close All Browsers

*** Test Cases ***
Heals-Rewrites-Then-Fails
    Click Element    xpath=//button[@id='login']    # wrong on purpose
```

**What happens?**
- If the locator fails, the library asks Ollama for a new selector, validates it, persists to `healed_locators.json`, optionally rewrites your code (if enabled), then **fails the test by design** so you review/commit the change.

> Use `Test Ollama Connection` early in a suite to sanity‑check your local LLM endpoint.

---

## ⚙️ Configuration

You can configure via **library arguments** and/or **environment variables**. Arguments override env vars.

### Ollama / Healing switches
- `OLLAMA_BASE_URL` (default `http://localhost:11434`)
- `OLLAMA_MODEL` (default `llama3`)
- `HEALING_AUTOHEAL` (default `true`) → auto‑heal when a locator fails
- `HEALING_AUTOREWRITE` (default `false`) → auto‑rewrite source files when a heal occurs

### Validation knobs
- `HEALING_REQUIRE_VISIBLE` (default `false`)
- `HEALING_REQUIRE_UNIQUE` (default `false`)
- `HEALING_VALIDATE_RETRIES` (default `1`)
- `HEALING_VALIDATE_RETRY_INTERVAL_MS` (default `150`)

### Failure artifacts
- `HEALING_CAPTURE_ON_FAIL` (default `true`)
- `HEALING_SAVE_HTML_ON_FAIL` (default `false`)
- `HEALING_ARTIFACT_DIR` (default `healing_screens` under Robot `${OUTPUT DIR}`)

### Browser defaults (safety nets)
- `HEALING_DEFAULT_URL` (default `about:blank`)
- `HEALING_DEFAULT_BROWSER` (default `chrome`)

### Fail policy
- `HEALING_FAIL_ON_HEAL` (default `true`) → fail whenever a heal occurred
- `HEALING_FAIL_AFTER_REWRITE` (default `true`) → fail after successful auto‑rewrite changed file(s)

### Rewrite controls
- `HEALING_REWRITE_ROOT` (default: current working directory)
- `HEALING_REWRITE_GLOBS` (defaults include: `**/*.robot, **/*.resource, **/*.py, **/*.json, **/*.yaml, **/*.yml, **/*.txt`)
- `HEALING_REWRITE_EXCLUDE` (defaults include: `healed_locators.json, *_history.jsonl, .git/**, venv/**, .venv/**, node_modules/**, __pycache__/**`)
- `HEALING_REWRITE_DRY_RUN` (default `false`)
- `HEALING_REWRITE_MAX_BYTES` (default `2097152`)

### SeleniumLibrary passthrough (library args)
- `sl_timeout` (e.g., `10 seconds`)
- `sl_implicit_wait` (e.g., `0.5 seconds`)
- `sl_run_on_failure` (e.g., `Capture Page Screenshot` or `Nothing`)

Example import with arguments:

```robot
*** Settings ***
Library    HealingSelenium.py    sl_timeout=10 seconds    sl_run_on_failure=Capture Page Screenshot
```

---

## 🧠 How healing works (under the hood)

1. **Attempt the action**: Call SeleniumLibrary’s original keyword (click/input).
2. On **failure**: Optionally capture screenshot/HTML.
3. **Known heal?** Check `healed_locators.json` for a current entry and revalidate it.
4. **Ask Ollama**: If no valid heal exists, send a prompt with the **current page HTML** + the broken locator.
5. **Normalize & validate**: Convert the suggestion to a Selenium‑friendly locator and verify it exists in DOM (and meets `visible/unique` rules with retries).
6. **Persist**: Save as the new `current` with `updated_at`, pushing any previous value into `history`. Also append an audit row to `healed_locators_history.jsonl`.
7. **Auto‑rewrite (optional)**: Search the codebase and replace literal occurrences of the old locator (supports quotes and whitespace‑bound matches; makes a `.bak.<timestamp>` backup per changed file).
8. **Fail on purpose**: Enforce fail policy so the change is surfaced and reviewed.

---

## 📚 Public Keywords

### Overrides (healing‑aware)
- `Click Element`
- `Input Text`

### Utilities
- `Validate Locator` → returns `{ ok, locator, count }` after normalization/validation
- `Highlight Healings` → prints current + history of healed locators
- `Test Ollama Connection` → pings Ollama API and logs status/preview

### Runtime toggles
- `Enable Auto Healing` / `Disable Auto Healing` / `Set Auto Healing` / `Get Auto Healing Status`
- `Enable Auto Rewrite` / `Disable Auto Rewrite` / `Set Auto Rewrite` / `Get Auto Rewrite Status`

### Safety nets
- `Ensure Browser Open` → open a browser if none exists (uses defaults)
- `Open Browser If Needed` → alias of Ensure Browser Open
- `Is Browser Open` → boolean
- `Get Browser Count` → integer

#### Examples
```robot
# Turn healing ON/OFF at runtime
Enable Auto Healing
Disable Auto Rewrite

# Validate a locator quickly
${res}=    Validate Locator    //button[@id="login"]
Log    ${res}[ok] ${res}[locator] ${res}[count]

# Ensure a session exists before interacting
Ensure Browser Open    url=${BASE_URL}    browser=${BROWSER}
```

---

## 🗂️ Persistence & file format

- **`healed_locators.json`** (snapshot with history)
  ```json
  {
    "xpath=//button[@id='login']": {
      "current": {
        "type": "css",
        "locator": "css:#login",
        "updated_at": "2025-01-01T12:34:56"
      },
      "history": [
        { "type": "xpath", "locator": "xpath=//button[@id='login']", "updated_at": "2024-12-10T09:00:00" }
      ]
    }
  }
  ```

- **`healed_locators_history.jsonl`** (append‑only audit stream)
  ```json
  {"ts":"2025-01-01T12:34:56","event":"heal","old_locator":"xpath=//button[@id='login']","model":"llama3","source":"ollama","new":{"type":"css","locator":"css:#login"}}
  {"ts":"2025-01-01T12:34:57","event":"rewrite","old_locator":"xpath=//button[@id='login']","model":"llama3","source":"ollama","rewrite":{"files_changed":1,"occurrences_replaced":2,"changed_files":[{"path":"tests/login.robot","replacements":2}]}}
  ```

> The loader upgrades older/legacy formats automatically.

---

## 🛠️ Auto‑rewrite details

- Searches files under `HEALING_REWRITE_ROOT` matching `HEALING_REWRITE_GLOBS` and not matching `HEALING_REWRITE_EXCLUDE`.
- Replaces **quoted** occurrences first (preserving original quotes), then whitespace‑bound matches in `.robot/.resource/.txt` files.
- Skips large files (`HEALING_REWRITE_MAX_BYTES`) and writes a timestamped `.bak` backup for changed files.
- Emits a summary: files changed, occurrences replaced, and per‑file counts.

> Use `HEALING_REWRITE_DRY_RUN=true` in CI to inspect what would change before enabling real rewrites.

---

## ❗ Fail policy (by design)

- `HEALING_FAIL_ON_HEAL=true` (default): **fail whenever a heal occurred** even if no rewrite happened.
- `HEALING_FAIL_AFTER_REWRITE=true` (default): **fail after a successful auto‑rewrite** that changed file(s).

This ensures the team sees and reviews locator updates instead of silently masking underlying changes.

---

## 🧩 Troubleshooting

- **Ollama connection fails**: Run `Test Ollama Connection` early; verify Ollama is running and the model name matches `OLLAMA_MODEL`.
- **No Selenium driver found**: Ensure you opened a browser (`Open Browser` or `Ensure Browser Open`) before actions.
- **Suggestion not in DOM**: Loosen/tune validation knobs (`HEALING_REQUIRE_VISIBLE`, `HEALING_REQUIRE_UNIQUE`, retries/interval) or rerun after page load is stable.
- **Artifacts not saved**: Check write permissions and `HEALING_ARTIFACT_DIR` path under Robot `${OUTPUT DIR}`.
- **Repo not rewritten**: Enable `HEALING_AUTOREWRITE=true` or set `HEALING_REWRITE_ROOT` correctly. Review globs/excludes.

---

## 🔐 Change control & safety

- Auto‑rewrite makes **timestamped backups** of changed files.
- Default fail policy surfaces changes in CI so updates go through code review.
- Audit stream (`*_history.jsonl`) provides traceability of model‑assisted changes.

---

## 📎 Metadata

- **Scope**: `GLOBAL`
- **Version**: `2.0.0`
- **Files created at runtime**: `healed_locators.json`, `healed_locators_history.jsonl`, artifacts under `${OUTPUT DIR}/healing_screens/`

---

## 🤝 Contributing

- File issues/ideas, or open PRs with focused changes. Please include unit/integration examples if possible.

---

## 📜 License

Add your project’s license here (e.g., MIT, Apache‑2.0, or Internal/Proprietary).

---

## 🧭 Appendix: Available library arguments

```text
model, base_url, healed_file, auto_heal, auto_rewrite,
sl_timeout, sl_implicit_wait, sl_run_on_failure,
default_url, default_browser
```

> Arguments map to env vars where applicable; arguments take precedence.

---

Happy healing! 🧪🩹
