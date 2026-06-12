# HealingSelenium.py
# -*- coding: utf-8 -*-
# ruff: noqa: E501, PLR0912, PLR0911, PLR0913
# pylint: disable=invalid-name,too-many-instance-attributes,too-many-public-methods,line-too-long
# pylint: disable=misplaced-bare-raise,no-else-return,no-else-continue,too-many-arguments
# pylint: disable=too-many-branches,too-many-nested-blocks,too-many-locals,too-many-return-statements
# pylint: disable=broad-exception-caught,too-many-lines
"""
HealingSelenium — Dynamic wrapper around SeleniumLibrary with self-healing & safety nets.

What it does (high level)
-------------------------
• Import ONLY this library in Robot. You can still use ALL SeleniumLibrary keywords through it.
• Healing-aware overrides for **Click Element** and **Input Text**.
• If the FIRST (coded) locator fails →
  1) Ask an LLM (Ollama) for alternate locator candidates using the current DOM
     locator library plus a compact current-page HTML snapshot,
  2) Validate and score all candidates in the live page (visibility/uniqueness
     are configurable),
  3) Persist the top-scored healed choice and scoring report
     (healed_locators.json + audit JSONL),
  4) Optionally rewrite your source files to replace the old locator literal,
  5) **Intentionally fail the test** so the team notices & commits the change.
This matches the policy: "rewrite the new locator in code and fail the case as the mentioned locator (in code) was not found".

✨ NEW (2.1.0)
--------------
• Collect a compact “DOM Locator Library” from the live WebDriver DOM (ids, names, data-test*, aria-labels, roles, short texts, css classes).
• Include that library in the LLM prompt so the model suggests selectors that actually exist **in the current DOM inventory**.
• Include compact current-page HTML in the prompt and score multiple AI locator candidates before choosing the final healed locator.
• New Robot keywords:
    - Print Locator Library
    - Get Locator Library

Environment variables / Library args
------------------------------------
OLLAMA_BASE_URL              default: http://localhost:11434
OLLAMA_MODEL                 default: llama3
HEALING_AUTOHEAL             default: true  (arg: auto_heal)
HEALING_AUTOREWRITE          default: false (arg: auto_rewrite)

# Validation knobs
HEALING_REQUIRE_VISIBLE      default: false
HEALING_REQUIRE_UNIQUE       default: false
HEALING_VALIDATE_RETRIES     default: 1
HEALING_VALIDATE_RETRY_INTERVAL_MS default: 150

# Failure artifacts
HEALING_CAPTURE_ON_FAIL      default: true
HEALING_SAVE_HTML_ON_FAIL    default: false
HEALING_ARTIFACT_DIR         default: healing_screens (under ${OUTPUT DIR})

# Browser safety nets (optional defaults used by Ensure Browser Open)
HEALING_DEFAULT_URL          default: about:blank
HEALING_DEFAULT_BROWSER      default: chrome

# Test fail policy
HEALING_FAIL_ON_HEAL         default: true  ← fail the test when a heal occurs (even if no file was rewritten)
HEALING_FAIL_AFTER_REWRITE   default: true  ← fail the test after a successful auto-rewrite changed any file(s)

# Source rewrite knobs
HEALING_REWRITE_ROOT         default: cwd
HEALING_REWRITE_GLOBS        default: **/*.robot,**/*.resource,**/*.py,**/*.json,**/*.yaml,**/*.yml,**/*.txt
HEALING_REWRITE_EXCLUDE      default: healed_locators.json,*_history.jsonl,.git/**,venv/**,.venv/**,node_modules/**,__pycache__/**
HEALING_REWRITE_DRY_RUN      default: false
HEALING_REWRITE_MAX_BYTES    default: 2_097_152

Typical Robot usage
-------------------
*** Settings ***
Library    HealingSelenium.py    sl_run_on_failure=Capture Page Screenshot
Suite Setup    Open Browser    ${BASE_URL}    ${BROWSER}
Suite Teardown    Close All Browsers

*** Test Cases ***
Heals-Rewrites-Then-Fails
    Click Element    xpath=//button[@id='login']    # wrong on purpose
    # Optional: Inspect the WebDriver DOM locator library
    Print Locator Library
"""
from __future__ import annotations
import fnmatch
import glob
import inspect
import json
import os
import re
import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import requests
from robot.libraries.BuiltIn import BuiltIn
# Import SeleniumLibrary as a Python class (do NOT register with Robot directly)
from SeleniumLibrary import SeleniumLibrary as _SLib


class HealingSelenium:
    """Dynamic wrapper around SeleniumLibrary with locator healing."""

    # Keep scope GLOBAL to be consistent with SeleniumLibrary and avoid multi-instance surprises
    ROBOT_LIBRARY_SCOPE = "GLOBAL"
    ROBOT_LIBRARY_VERSION = "2.1.0"

    # ------------------------------ Initialization ------------------------------
    def __init__(  # noqa: PLR0913
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        healed_file: str = "healed_locators.json",
        auto_heal: Optional[bool] = None,
        auto_rewrite: Optional[bool] = None,
        # Optional SeleniumLibrary init options
        sl_timeout: Optional[str] = None,       # e.g., "10 seconds"
        sl_implicit_wait: Optional[str] = None, # e.g., "0.5 seconds"
        sl_run_on_failure: Optional[str] = None,# e.g., "Capture Page Screenshot" or "Nothing"
        # Optional browser defaults for safety nets
        default_url: Optional[str] = None,
        default_browser: Optional[str] = None,
    ):
        # Ollama
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3")
        self.base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.ollama_url = f"{self.base_url.rstrip('/')}/api/generate"

        # Files
        self.healed_file = healed_file
        root, _ = os.path.splitext(self.healed_file)
        self.audit_file = f"{root}_history.jsonl"

        # Switches (env defaults; args override)
        self.auto_heal = self._arg_or_env_bool(auto_heal, "HEALING_AUTOHEAL", True)
        self.auto_rewrite = self._arg_or_env_bool(auto_rewrite, "HEALING_AUTOREWRITE", False)

        # Validation knobs
        self.require_visible = self._arg_or_env_bool(None, "HEALING_REQUIRE_VISIBLE", False)
        self.require_unique = self._arg_or_env_bool(None, "HEALING_REQUIRE_UNIQUE", False)
        self.validate_retries = int(os.getenv("HEALING_VALIDATE_RETRIES", "3"))
        self.validate_retry_interval_ms = int(os.getenv("HEALING_VALIDATE_RETRY_INTERVAL_MS", "150"))

        # Failure artifact toggle
        self.capture_on_fail = self._arg_or_env_bool(None, "HEALING_CAPTURE_ON_FAIL", True)

        # NEW: fail policy
        self.fail_on_heal = self._arg_or_env_bool(None, "HEALING_FAIL_ON_HEAL", True)
        self.fail_after_rewrite = self._arg_or_env_bool(None, "HEALING_FAIL_AFTER_REWRITE", True)

        # Browser defaults (safety nets)
        self.default_url = default_url if default_url is not None else os.getenv("HEALING_DEFAULT_URL", "about:blank")
        self.default_browser = (
            default_browser if default_browser is not None else os.getenv("HEALING_DEFAULT_BROWSER", "chrome")
        )

        # Internal SeleniumLibrary instance (NOT registered with Robot)
        sl_kwargs: Dict[str, Any] = {}
        if sl_timeout is not None:
            sl_kwargs["timeout"] = sl_timeout
        if sl_implicit_wait is not None:
            sl_kwargs["implicit_wait"] = sl_implicit_wait
        if sl_run_on_failure is not None:
            sl_kwargs["run_on_failure"] = sl_run_on_failure
        self._sl = _SLib(**sl_kwargs)

        # State
        self.healed_locators = self._load_healed_locators()

        # Build keyword map (overrides + utilities)
        self._init_custom_keyword_map()

        # Banner
        print("\n[HealingSelenium] ✅ Library loaded (scope=GLOBAL)")
        print(f"[HealingSelenium] Model: {self.model}")
        print(f"[HealingSelenium] Endpoint: {self.ollama_url}")
        print(
            f"[HealingSelenium] Auto-Heal:{self.auto_heal} Auto-Rewrite:{self.auto_rewrite}\n"
            f"[HealingSelenium] Validation → visible:{self.require_visible} unique:{self.require_unique} "
            f"retries:{self.validate_retries} interval_ms:{self.validate_retry_interval_ms}\n"
            f"[HealingSelenium] Capture-On-Fail:{self.capture_on_fail} "
            f"Fail-On-Heal:{self.fail_on_heal} Fail-After-Rewrite:{self.fail_after_rewrite}\n"
            f"Artifacts dir:{os.getenv('HEALING_ARTIFACT_DIR', 'healing_screens')}\n"
            f"[HealingSelenium] Defaults → url:{self.default_url} browser:{self.default_browser}\n"
        )

    # -------------------------- Dynamic Library Interface --------------------------
    def _init_custom_keyword_map(self):
        self._custom_kw: Dict[str, Any] = {
            # healing-aware overrides
            "Click Element": self.click_element,
            "Input Text": self.input_text,
            # utilities
            "Test Ollama Connection": self.test_ollama_connection,
            "Highlight Healings": self.highlight_healings,
            "Validate Locator": self.validate_locator,
            # NEW: DOM locator library
            "Print Locator Library": self.print_locator_library,
            "Get Locator Library": self.get_locator_library,
            # runtime toggles
            "Enable Auto Healing": self.enable_auto_healing,
            "Disable Auto Healing": self.disable_auto_healing,
            "Set Auto Healing": self.set_auto_healing,
            "Get Auto Healing Status": self.get_auto_healing_status,
            "Enable Auto Rewrite": self.enable_auto_rewrite,
            "Disable Auto Rewrite": self.disable_auto_rewrite,
            "Set Auto Rewrite": self.set_auto_rewrite,
            "Get Auto Rewrite Status": self.get_auto_rewrite_status,
            # safety nets
            "Ensure Browser Open": self.ensure_browser_open,
            "Is Browser Open": self.is_browser_open,
            "Get Browser Count": self.get_browser_count,
            "Open Browser If Needed": self.open_browser_if_needed,
        }
        self._custom_norm = {self._norm(n): n for n in self._custom_kw}

    def get_keyword_names(self) -> List[str]:
        """
        Exported keywords:
        - all SeleniumLibrary keywords (from our internal instance),
        - except the ones we override (Click Element, Input Text),
        - plus our utility/toggle keywords.
        """
        try:
            sl_names = set(self._sl.get_keyword_names())
        except Exception:
            sl_names = set()
        # Filter out our overrides to avoid duplicates
        sl_filtered = [n for n in sl_names if self._norm(n) not in self._custom_norm]
        names = set(sl_filtered) | set(self._custom_kw.keys())
        return sorted(names)

    def run_keyword(self, name: str, args: list, kwargs: Optional[dict] = None):
        """Dispatch Robot keywords: prefer our overrides, else proxy to internal SeleniumLibrary."""
        if kwargs is None:
            kwargs = {}
        norm = self._norm(name)
        if norm in self._custom_norm:
            real = self._custom_norm[norm]
            func = self._custom_kw[real]
            return func(*args, **kwargs)
        # Proxy to internal SeleniumLibrary instance
        return self._sl.run_keyword(name, args, kwargs)

    def get_keyword_documentation(self, name: str) -> str:
        """Return keyword documentation"""
        norm = self._norm(name)
        if norm in self._custom_norm:
            real = self._custom_norm[norm]
            func = self._custom_kw[real]
            return inspect.getdoc(func) or real
        try:
            return self._sl.get_keyword_documentation(name)
        except Exception:
            func = getattr(self._sl, self._kw_to_method(name), None)
            return inspect.getdoc(func) or name

    def get_keyword_arguments(self, name: str) -> List[str]:
        """Return keyword signature as list of strings (compatible with Robot’s introspection)."""
        norm = self._norm(name)
        if norm in self._custom_norm:
            real = self._custom_norm[norm]
            func = self._custom_kw[real]
            try:
                sig = inspect.signature(func)
                out: List[str] = []
                for p in sig.parameters.values():
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty:
                        out.append(p.name)
                    elif p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                        out.append(f"{p.name}={repr(p.default)}")
                    elif p.kind == p.VAR_POSITIONAL:
                        out.append("*" + p.name)
                    elif p.kind == p.VAR_KEYWORD:
                        out.append("**" + p.name)
                return out
            except Exception:
                return []
        try:
            return self._sl.get_keyword_arguments(name)
        except Exception:
            func = getattr(self._sl, self._kw_to_method(name), None)
            if not func:
                return []
            try:
                sig = inspect.signature(func)
                out: List[str] = []
                for p in sig.parameters.values():
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty:
                        out.append(p.name)
                    elif p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                        out.append(f"{p.name}={repr(p.default)}")
                    elif p.kind == p.VAR_POSITIONAL:
                        out.append("*" + p.name)
                    elif p.kind == p.VAR_KEYWORD:
                        out.append("**" + p.name)
                return out
            except Exception:
                return []

    # ------------------------------ Utilities & Infra ------------------------------
    def _norm(self, name: str) -> str:
        return "".join(ch for ch in name.lower() if ch not in " _")

    def _kw_to_method(self, name: str) -> str:
        return name.strip().lower().replace(" ", "_")

    def _to_bool(self, v: Any, default: bool = False) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def _arg_or_env_bool(self, arg_value: Optional[bool], env_name: str, default: bool) -> bool:
        if arg_value is not None:
            return self._to_bool(arg_value, default)
        return self._to_bool(os.getenv(env_name), default)

    def _iso_now(self) -> str:
        return datetime.now().isoformat()

    # ---- Screenshot / HTML capture helpers ----
    def _get_output_dir(self) -> str:
        """Return Robot's OUTPUT DIR (fallback to cwd when not under Robot)."""
        try:
            return BuiltIn().get_variable_value("${OUTPUT DIR}") or os.getcwd()
        except Exception:
            return os.getcwd()

    def _capture_page_artifacts(self, reason: str, locator: Optional[str] = None) -> Optional[str]:
        """
        Capture a screenshot (and optional HTML dump) into ${OUTPUT DIR}/<HEALING_ARTIFACT_DIR>.
        Filenames include timestamp + reason for easier debugging.
        """
        try:
            out_dir = os.path.join(
                self._get_output_dir(),
                os.getenv("HEALING_ARTIFACT_DIR", "healing_screens"),
            )
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base = f"{ts}__{reason}" + (f"__{self._sanitize_name(locator)}" if locator else "")
            png_name = base + ".png"
            png_path = os.path.join(out_dir, png_name)

            try:
                # Prefer SeleniumLibrary’s keyword to integrate with Robot logs
                self._sl.capture_page_screenshot(png_path)
                print(f"[Healing] 🖼️ Screenshot captured → {png_path}")
            except Exception as e:
                # Fallback to raw driver if needed
                drv = getattr(self._sl, "driver", None)
                if drv:
                    if drv.get_screenshot_as_file(png_path):
                        print(f"[Healing] 🖼️ (driver) Screenshot captured → {png_path}")
                    else:
                        print(f"[Healing] ⚠️ Screenshot attempt failed via driver: {e}")
                else:
                    print(f"[Healing] ⚠️ No driver available for screenshot: {e}")

            if self._to_bool(os.getenv("HEALING_SAVE_HTML_ON_FAIL"), False):
                html_name = base + ".html"
                html_path = os.path.join(out_dir, html_name)
                try:
                    drv = getattr(self._sl, "driver", None)
                    if drv:
                        with open(html_path, "w", encoding="utf-8") as f:
                            f.write(drv.page_source or "")
                        print(f"[Healing] 📝 HTML saved → {html_path}")
                except Exception as e:
                    print(f"[Healing] ⚠️ Saving HTML failed: {e}")
            return png_path
        except Exception as e:
            print(f"[Healing] ⚠️ Artifact capture failed: {e}")
            return None

    def _sanitize_name(self, s: Optional[str]) -> str:
        if not s:
            return "locator"
        return re.sub(r"[^A-Za-z0-9_.\-]+", "_", str(s))[:140]

    # ---- Normalization & Validation ----
    def _normalize_locator(self, typ: Optional[str], loc: Optional[str]) -> Optional[str]:
        """
        Convert (type, locator) into a SeleniumLibrary-friendly locator.
        Produces 'strategy=…' or 'strategy:…' formats where appropriate.
        Keeps raw valid prefixes if already present.
        """
        if not loc:
            return loc
        s = str(loc).strip()
        if not s:
            return s
        lowered = s.lower()

        # Already explicit
        if lowered.startswith(("xpath=", "xpath:", "css=", "css:", "id=", "name=")):
            return s

        # If no explicit type was provided, infer from shape
        if not typ or typ not in ("xpath", "css", "id", "name"):
            if s.startswith(("/", "(", ".//", "//")):
                typ = "xpath"
            elif s.startswith(("css=", "css:")):
                return s
            elif s.startswith(("#", ".", "[")) and "id=" not in s:
                typ = "css"
            elif re.match(r"^[A-Za-z_][\w\-.:]*$", s) and " " not in s:
                # Looks like a single token → id (or name, but id first)
                typ = "id"
            else:
                typ = "xpath"

        # Build SeleniumLibrary-friendly locator
        if typ == "css":
            # SeleniumLibrary accepts css= and css:
            if s.startswith(("#", ".", "[")) or any(ch in s for ch in (">", "+", "~", " ")):
                return f"css:{s}"
            return f"css={s}"
        return f"{typ}={s}"

    def _locator_exists_in_dom(
        self,
        entry: Dict[str, Any],
        require_visible: bool = False,
        unique: bool = False,
        retries: int = 1,
        interval_ms: int = 150,
    ) -> Tuple[bool, str, int]:
        """
        Check if the locator exists in the current DOM using SeleniumLibrary.
        Returns (exists: bool, sl_locator: str, count: int)
        """
        try:
            typ = (entry or {}).get("type")
            loc = (entry or {}).get("locator")
            sl_loc = self._normalize_locator(typ, loc)
            if not sl_loc:
                return (False, sl_loc or "", 0)
            last_count = 0
            for attempt in range(max(1, int(retries))):
                elems = self._sl.get_webelements(sl_loc)  # SeleniumLibrary API
                if require_visible:
                    elems = [e for e in elems if getattr(e, "is_displayed", lambda: True)()]
                last_count = len(elems)
                ok = (last_count == 1) if unique else (last_count >= 1)
                if ok:
                    return (True, sl_loc, last_count)
                # small wait before retrying
                if attempt < retries - 1:
                    time.sleep(max(0, interval_ms) / 1000.0)
            return (False, sl_loc, last_count)
        except Exception:
            return (False, str((entry or {}).get("locator") or ""), 0)

    def _is_locator_currently_valid(self, entry: Dict[str, Any]) -> bool:
        ok, _, _ = self._locator_exists_in_dom(
            entry,
            require_visible=self.require_visible,
            unique=self.require_unique,
            retries=self.validate_retries,
            interval_ms=self.validate_retry_interval_ms,
        )
        return ok

    # ---------------------------- Persistence (snapshot + audit) ----------------------------
    def _load_healed_locators(self) -> Dict[str, Any]:
        if os.path.exists(self.healed_file):
            try:
                with open(self.healed_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                upgraded: Dict[str, Any] = {}
                for old, val in data.items():
                    if isinstance(val, dict) and "current" in val and "history" in val:
                        upgraded[old] = val
                    elif isinstance(val, dict) and "locator" in val:
                        upgraded[old] = {
                            "current": {
                                "type": val.get("type"),
                                "locator": val.get("locator"),
                                "updated_at": val.get("updated_at") or None,
                            },
                            "history": [],
                        }
                    else:
                        upgraded[old] = {
                            "current": {"type": None, "locator": val, "updated_at": None},
                            "history": [],
                        }
                return upgraded
            except Exception:
                return {}
        return {}

    def _write_snapshot(self):
        with open(self.healed_file, "w", encoding="utf-8") as f:
            json.dump(self.healed_locators, f, indent=2, ensure_ascii=False)

    def _append_audit(self, old_locator: str, new_entry: Any, event_type: str = "heal", extra: Optional[dict] = None):
        try:
            payload: Dict[str, Any] = {
                "ts": self._iso_now(),
                "event": event_type,
                "old_locator": old_locator,
                "model": self.model,
                "source": "ollama",
            }
            if event_type == "heal":
                payload["new"] = {
                    "type": new_entry.get("type") if isinstance(new_entry, dict) else None,
                    "locator": new_entry.get("locator") if isinstance(new_entry, dict) else new_entry,
                }
            if extra:
                payload.update(extra)
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            # do not break tests for audit issues
            pass

    # ------------------------------------- DOM Library (NEW) -------------------------------------
    def _collect_dom_locator_library(self, max_nodes: int = 1000) -> Dict[str, Any]:
        """
        Collect a compact inventory of candidate locator tokens from the live DOM via WebDriver.
        Returns a dict of arrays: ids, names, dataTest, ariaLabels, roles, texts, cssClasses.
        """
        drv = getattr(self._sl, "driver", None)
        if not drv:
            return {}
        script = r"""
            return (function(maxNodes){
                try {
                    const out = {
                      ids:[], names:[], dataTest:[], ariaLabels:[], roles:[], texts:[], cssClasses:[]
                    };
                    const nodes = Array.from(document.querySelectorAll('*'));
                    const sample = nodes.slice(0, Math.min(nodes.length, maxNodes||1000));
                    const uniq = {
                        ids:new Set(), names:new Set(), dataTest:new Set(), aria:new Set(),
                        roles:new Set(), texts:new Set(), css:new Set()
                    };
                    const MAX_TEXT_LEN = 60;
                    const norm = s => (s||'').trim().replace(/\s+/g,' ');
                    for (const el of sample) {
                        const id = norm(el.id);
                        if (id) uniq.ids.add(id);

                        const name = norm(el.getAttribute('name'));
                        if (name) uniq.names.add(name);

                        // data-test, data-testid, data_testid, etc
                        const attrNames = el.getAttributeNames ? el.getAttributeNames() : [];
                        for (const a of attrNames) {
                            if (/^data[-_]?test(id)?$/i.test(a)) {
                                const v = norm(el.getAttribute(a));
                                if (v) uniq.dataTest.add(v);
                            }
                        }

                        const aria = norm(el.getAttribute('aria-label'));
                        if (aria) uniq.aria.add(aria);

                        const role = norm(el.getAttribute('role'));
                        if (role) uniq.roles.add(role);

                        const txt = norm(el.innerText || el.textContent || '');
                        if (txt && txt.length <= MAX_TEXT_LEN) uniq.texts.add(txt);

                        const clsRaw = norm(el.className || '');
                        if (clsRaw) {
                            for (const c of clsRaw.split(' ')) {
                                if (c) uniq.css.add('.' + c);
                            }
                        }
                    }
                    const toArr = s => Array.from(s).slice(0, 500);
                    return {
                        ids: toArr(uniq.ids),
                        names: toArr(uniq.names),
                        dataTest: toArr(uniq.dataTest),
                        ariaLabels: toArr(uniq.aria),
                        roles: toArr(uniq.roles),
                        texts: toArr(uniq.texts),
                        cssClasses: toArr(uniq.css)
                    };
                } catch (e) {
                    return { error: String(e) };
                }
            })(arguments[0]);
        """
        try:
            lib = drv.execute_script(script, max_nodes)
            return lib or {}
        except Exception as e:
            print(f"[Healing] ⚠️ DOM library collection failed: {e}")
            return {}

    def _derive_locator_hints(self, old_locator: str) -> List[str]:
        """
        Extract simple keywords from the broken locator to help the LLM (e.g., 'login', 'email').
        """
        tokens = re.findall(r"[A-Za-z0-9_]{3,}", old_locator or "")
        common = {
            "xpath", "css", "id", "name", "div", "span", "button", "input",
            "class", "type", "text", "contains", "and", "or", "following", "preceding"
        }
        out = [t.lower() for t in tokens if t.lower() not in common]
        # Keep first few distinct hints
        uniq: List[str] = []
        for t in out:
            if t not in uniq:
                uniq.append(t)
            if len(uniq) >= 10:
                break
        return uniq

    def _compact_html_for_prompt(self, html: str, max_chars: int = 8000) -> str:
        """
        Keep the prompt grounded in the current page HTML without flooding the model.
        Script/style/comment content is removed, whitespace is compacted, and long HTML
        keeps both the beginning and end because either may contain useful page context.
        """
        if not html:
            return ""
        compact = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", str(html))
        compact = re.sub(r"(?s)<!--.*?-->", " ", compact)
        compact = re.sub(r">\s+<", "><", compact)
        compact = re.sub(r"\s+", " ", compact).strip()
        if len(compact) <= max_chars:
            return compact
        half = max(1, max_chars // 2)
        return f"{compact[:half]}\n...[truncated current page HTML]...\n{compact[-half:]}"

    def _infer_locator_type(self, loc: str) -> str:
        """Infer a SeleniumLibrary locator type from a locator string."""
        s = str(loc or "").strip()
        lowered = s.lower()
        if lowered.startswith(("xpath=", "xpath:")) or s.startswith(("/", "(", ".//", "//")):
            return "xpath"
        if lowered.startswith(("css=", "css:")) or (
            s.startswith(("#", ".", "["))
            or any(ch in s for ch in (">", "+", "~"))
        ):
            return "css"
        if lowered.startswith("name="):
            return "name"
        if lowered.startswith("id="):
            return "id"
        if re.match(r"^[A-Za-z_][\w\-.:]*$", s) and " " not in s:
            return "id"
        return "xpath"

    def _coerce_locator_candidate(self, candidate: Any) -> Optional[Dict[str, Any]]:
        """
        Accept model candidates in flexible shapes and normalize them into
        {'type', 'locator', optional 'reason', optional 'ai_confidence'}.
        """
        if isinstance(candidate, str):
            raw: Dict[str, Any] = {"locator": candidate}
        elif isinstance(candidate, dict):
            raw = candidate
        else:
            return None

        loc = raw.get("locator") or raw.get("value") or raw.get("selector")
        if not loc or not str(loc).strip():
            return None

        typ = str(raw.get("type") or self._infer_locator_type(str(loc))).strip().lower()
        if typ not in ("xpath", "css", "id", "name"):
            typ = self._infer_locator_type(str(loc))

        out: Dict[str, Any] = {"type": typ, "locator": str(loc).strip()}
        if raw.get("reason"):
            out["reason"] = str(raw.get("reason"))[:300]

        confidence = raw.get("confidence", raw.get("score"))
        if confidence is not None:
            try:
                out["ai_confidence"] = max(0.0, min(1.0, float(confidence)))
            except Exception:
                pass
        return out

    def _extract_locator_candidates(self, suggestion: Any) -> List[Dict[str, Any]]:
        """
        Extract a candidate list from modern or legacy model JSON.
        Supports:
        - {'candidates': [{...}, {...}]}
        - {'locators': [...]}
        - {'selectors': [...]}
        - a legacy single {'type': ..., 'locator': ...}
        - a top-level list
        """
        if isinstance(suggestion, list):
            raw_candidates = suggestion
        elif isinstance(suggestion, dict):
            raw_candidates = (
                suggestion.get("candidates")
                or suggestion.get("locators")
                or suggestion.get("selectors")
                or suggestion.get("alternatives")
            )
            if raw_candidates is None:
                raw_candidates = [suggestion]
            elif isinstance(raw_candidates, dict):
                raw_candidates = [raw_candidates]
        else:
            raw_candidates = []

        out: List[Dict[str, Any]] = []
        seen = set()
        for raw in raw_candidates:
            item = self._coerce_locator_candidate(raw)
            if not item:
                continue
            key = (item.get("type"), item.get("locator"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= 8:
                break
        return out

    def _dom_library_counts(self, dom_lib: Dict[str, Any]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for key, value in (dom_lib or {}).items():
            counts[key] = len(value) if isinstance(value, list) else 0
        return counts

    def _candidate_dom_evidence(self, candidate: Dict[str, Any], dom_lib: Dict[str, Any]) -> Tuple[int, List[str]]:
        """
        Score whether a candidate uses values that were actually seen in the DOM
        locator library. This is separate from WebDriver validation and explains
        why a selector is stable or weak.
        """
        loc = str(candidate.get("locator") or "")
        typ = str(candidate.get("type") or "").lower()
        loc_l = loc.lower()
        score = 0
        reasons: List[str] = []

        ids = set(str(x) for x in dom_lib.get("ids", []) if x)
        names = set(str(x) for x in dom_lib.get("names", []) if x)
        data_tests = set(str(x) for x in dom_lib.get("dataTest", []) if x)
        aria_labels = set(str(x) for x in dom_lib.get("ariaLabels", []) if x)
        texts = set(str(x) for x in dom_lib.get("texts", []) if x)
        css_classes = set(str(x) for x in dom_lib.get("cssClasses", []) if x)

        raw_value = re.sub(r"^(id|name|css|xpath)[:=]", "", loc, flags=re.I)
        if typ == "id" and raw_value in ids:
            score += 14
            reasons.append("id appears in DOM library")
        if typ == "name" and raw_value in names:
            score += 12
            reasons.append("name appears in DOM library")

        data_match = re.search(r"data[-_]?test(?:id)?\s*=\s*['\"]([^'\"]+)['\"]", loc, re.I)
        if data_match and data_match.group(1) in data_tests:
            score += 16
            reasons.append("data-test value appears in DOM library")

        aria_match = re.search(r"aria-label\s*=\s*['\"]([^'\"]+)['\"]", loc, re.I)
        if aria_match and aria_match.group(1) in aria_labels:
            score += 12
            reasons.append("aria-label appears in DOM library")

        for txt in texts:
            if txt and len(txt) >= 3 and txt.lower() in loc_l:
                score += 8
                reasons.append("visible text appears in DOM library")
                break

        for cls in css_classes:
            if cls and cls.lower() in loc_l:
                score += 4
                reasons.append("CSS class appears in DOM library")
                break

        return min(score, 20), reasons

    def _score_locator_candidate(
        self,
        candidate: Dict[str, Any],
        old_locator: str,
        dom_lib: Dict[str, Any],
        hints: List[str],
        ordinal: int,
    ) -> Dict[str, Any]:
        """
        Validate and score one locator candidate against the live WebDriver DOM.
        Higher scores favor existing, visible, unique, stable, and context-matched
        locators. Invalid candidates remain in the report but are not selected.
        """
        normalized = self._normalize_locator(candidate.get("type"), candidate.get("locator"))
        count = 0
        visible_count = 0
        error = None
        try:
            elems = self._sl.get_webelements(normalized)
            count = len(elems)
            visible_count = sum(1 for e in elems if getattr(e, "is_displayed", lambda: True)())
        except Exception as exc:
            error = str(exc)[:200]

        exists = count > 0
        visible_ok = visible_count > 0
        unique_ok = count == 1
        config_ok = exists and (visible_ok or not self.require_visible) and (unique_ok or not self.require_unique)

        score = 0
        reasons: List[str] = []
        if exists:
            score += 35
            reasons.append("exists in live DOM")
        else:
            reasons.append("not found in live DOM")

        if visible_ok:
            score += 15
            reasons.append("has visible match")
        elif self.require_visible:
            reasons.append("fails visible requirement")

        if unique_ok:
            score += 20
            reasons.append("unique match")
        elif count > 1:
            score += max(0, 10 - min(count, 10))
            reasons.append(f"{count} matches")
            if self.require_unique:
                reasons.append("fails unique requirement")

        normalized_l = str(normalized or "").lower()
        locator_l = str(candidate.get("locator") or "").lower()
        typ = str(candidate.get("type") or "").lower()
        if "data-test" in normalized_l or "data-testid" in normalized_l:
            score += 24
            reasons.append("stable data-test selector")
        elif typ == "id" or normalized_l.startswith("id="):
            score += 22
            reasons.append("stable id selector")
        elif typ == "name" or normalized_l.startswith("name="):
            score += 18
            reasons.append("stable name selector")
        elif "aria-label" in normalized_l:
            score += 16
            reasons.append("aria-label selector")
        elif typ == "css" or normalized_l.startswith(("css=", "css:")):
            score += 10
            reasons.append("CSS selector")
        elif typ == "xpath" or normalized_l.startswith(("xpath=", "xpath:")):
            score += 8
            reasons.append("XPath selector")

        dom_score, dom_reasons = self._candidate_dom_evidence(candidate, dom_lib)
        score += dom_score
        reasons.extend(dom_reasons)

        hint_hits = [h for h in hints if h and h in locator_l]
        if hint_hits:
            score += min(10, len(hint_hits) * 4)
            reasons.append(f"matches broken-locator hints: {', '.join(hint_hits[:3])}")

        confidence = candidate.get("ai_confidence")
        if isinstance(confidence, (int, float)):
            score += int(round(float(confidence) * 5))
            reasons.append(f"AI confidence {float(confidence):.2f}")

        if re.search(r":nth-child|\[\d+\]|\(\s*//", normalized_l):
            score -= 6
            reasons.append("penalized for positional/brittle pattern")
        if len(str(normalized or "")) <= 140:
            score += 3
            reasons.append("compact selector")

        if not exists:
            score = min(score, 10)
        elif not config_ok:
            score = min(score, 45)

        return {
            "rank_from_ai": ordinal,
            "type": candidate.get("type"),
            "locator": candidate.get("locator"),
            "normalized_locator": normalized,
            "ok": config_ok,
            "exists": exists,
            "visible": visible_ok,
            "unique": unique_ok,
            "count": count,
            "visible_count": visible_count,
            "score": max(0, int(score)),
            "reasons": reasons[:12],
            "ai_reason": candidate.get("reason"),
            "ai_confidence": candidate.get("ai_confidence"),
            "error": error,
        }

    def _score_locator_candidates(
        self,
        old_locator: str,
        candidates: List[Dict[str, Any]],
        dom_lib: Dict[str, Any],
        html_chars_used: int,
    ) -> Dict[str, Any]:
        """Score all model candidates and select the best valid locator."""
        hints = self._derive_locator_hints(old_locator)
        scored = [
            self._score_locator_candidate(candidate, old_locator, dom_lib, hints, idx)
            for idx, candidate in enumerate(candidates, 1)
        ]
        scored.sort(key=lambda item: (bool(item.get("ok")), int(item.get("score", 0))), reverse=True)
        for rank, item in enumerate(scored, 1):
            item["score_rank"] = rank
        selected = next((item for item in scored if item.get("ok")), None)

        return {
            "old_locator": old_locator,
            "selected": selected,
            "candidates": scored,
            "candidate_count": len(candidates),
            "html_chars_used": html_chars_used,
            "dom_library_counts": self._dom_library_counts(dom_lib),
            "requirements": {
                "visible": self.require_visible,
                "unique": self.require_unique,
                "retries": self.validate_retries,
                "interval_ms": self.validate_retry_interval_ms,
            },
        }

    def _print_locator_score_report(self, score_report: Dict[str, Any]):
        """Print a compact score report into Robot logs/console."""
        print("\n==== Locator Candidate Score Report ====")
        selected = score_report.get("selected") or {}
        selected_norm = selected.get("normalized_locator")
        for item in score_report.get("candidates", []):
            mark = "*" if item.get("normalized_locator") == selected_norm and item.get("ok") else " "
            print(
                f"{mark} rank={item.get('score_rank')} score={item.get('score')} ok={item.get('ok')} "
                f"count={item.get('count')} visible={item.get('visible_count')} "
                f"locator={item.get('normalized_locator')}"
            )
            reason_line = "; ".join(item.get("reasons") or [])
            if reason_line:
                print(f"  reasons: {reason_line}")
            if item.get("ai_reason"):
                print(f"  ai_reason: {item.get('ai_reason')}")
            if item.get("error"):
                print(f"  error: {item.get('error')}")
        if selected_norm:
            print(f"[Healing] Selected top-scored locator: {selected_norm} (score={selected.get('score')})")
        else:
            print("[Healing] No AI candidate passed live DOM validation/scoring requirements.")
        print("========================================\n")

    # ---------------------------------- Healing & Ollama ----------------------------------
    def _extract_first_json_object(self, s: str) -> Optional[dict]:
        """
        Return the first balanced top-level JSON object from the string `s`.
        Accounts for braces inside quotes. Returns None if not found.
        """
        in_str = False
        esc = False
        depth = 0
        start_idx = -1
        for i, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            else:
                if ch == '"':
                    in_str = True
                    continue
                if ch == "{":
                    if depth == 0:
                        start_idx = i
                    depth += 1
                    continue
                if ch == "}":
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and start_idx != -1:
                            segment = s[start_idx : i + 1]
                            try:
                                return json.loads(segment)
                            except Exception:
                                # keep scanning; there might be another valid one later
                                pass
        return None

    def _ask_ollama_for_new_locator(self, old_locator: str, html: str) -> Optional[Dict[str, Any]]:
        print(f"\n[Healing] Asking Ollama to heal locator: {old_locator}")

        # Collect DOM locator library, hints, and compact current-page HTML.
        dom_lib = self._collect_dom_locator_library()
        hints = self._derive_locator_hints(old_locator)
        lib_json = json.dumps(dom_lib, ensure_ascii=False)
        html_limit = int(os.getenv("HEALING_PROMPT_HTML_MAX_CHARS", "8000"))
        html_excerpt = self._compact_html_for_prompt(html, max_chars=html_limit)

        prompt = (
            "You are a locator healing function. Search the provided current DOM inventory and current page HTML, "
            "then output ONLY ONE JSON object with this schema:\n"
            '{"candidates":[{"type":"xpath|css|id|name","locator":"<new_locator_string>",'
            '"reason":"short evidence from DOM/HTML","confidence":0.0}]}\n'
            "Constraints:\n"
            "- Output exactly one JSON object, no code fences, no prose.\n"
            "- Return 3 to 5 distinct locator candidates when possible.\n"
            "- Prefer stable selectors in this order: data-test/data-testid, id, name, aria-label/role, short CSS, robust XPath.\n"
            "- Avoid brittle positional XPath/CSS unless no better locator exists.\n"
            f"- The broken locator was: {old_locator}\n"
            "- WebDriver DOM Locator Library:\n"
            f"{lib_json[:4000]}\n"
            "- Compact current page HTML snapshot:\n"
            f"{html_excerpt}\n"
            "- If you choose id/name, the value MUST be one listed above. For data-test*, use css selectors like [data-test=\"...\"] if present.\n"
            f"- Helpful keywords (from the broken locator): {hints}\n"
            "- Each candidate must be grounded in values visible in the DOM library or HTML snapshot."
            "\n- Return only the JSON object."
        )

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # Ask Ollama to format output as JSON
            "format": "json",
            "options": {"temperature": 0, "top_p": 0.1},
        }

        content = ""
        try:
            print(f"[Healing] → Posting to {self.ollama_url}")
            r = requests.post(self.ollama_url, json=payload, timeout=200)
            print(f"[Healing] Ollama status: {r.status_code}")
            if r.status_code != 200:
                print(r.text)
                return None

            # Ollama returns an envelope with the model's text in 'response'
            content = r.json().get("response", "")

            # Try strict parse first (works when model honors format=json)
            suggestion: Any = None
            try:
                suggestion = json.loads(content)
            except Exception:
                # Fallback: extract the first balanced JSON object
                suggestion = self._extract_first_json_object(content)

            if isinstance(suggestion, list):
                suggestion = {"candidates": suggestion}
            if not isinstance(suggestion, dict):
                raise ValueError("No JSON object found in model response.")

            candidates = self._extract_locator_candidates(suggestion)
            if not candidates:
                raise ValueError(f"Model response missing locator candidates: {suggestion}")

            print(f"[Healing] AI returned {len(candidates)} locator candidate(s).")
            for idx, candidate in enumerate(candidates, 1):
                print(f"[Healing] Candidate {idx}: {candidate}")
            return {
                "candidates": candidates,
                "dom_library": dom_lib,
                "hints": hints,
                "html_chars_used": len(html_excerpt),
            }
        except Exception as e:
            preview = (content or "")[:200].replace("\n", " ")
            print(f"[Healing] ❌ Ollama error: {e}\n response preview: {preview}")
            return None

    # ---------------------------------- Auto-rewrite (optional) ----------------------------------
    def _maybe_autorewrite(self, old_locator: str, new_locator: str) -> Optional[Dict[str, Any]]:  # noqa: PLR0912
        if not new_locator or new_locator == old_locator:
            return None
        try:
            stats = self._apply_source_rewrite(old_locator, new_locator)
            self._append_audit(old_locator, new_locator, event_type="rewrite", extra={"rewrite": stats})
            changed = stats.get("files_changed", 0)
            total = stats.get("occurrences_replaced", 0)
            if changed:
                print(
                    f"[Healing] 🛠️ Auto-rewrite: replaced '{old_locator}' with '{new_locator}' "
                    f"in {changed} file(s), {total} occurrence(s)."
                )
            else:
                print(
                    f"[Healing] 🛠️ Auto-rewrite: no occurrences of '{old_locator}' found in codebase."
                )
            return stats
        except Exception as e:
            print(f"[Healing] ❌ Auto-rewrite error: {e}")
            return None

    def _apply_source_rewrite(self, old_locator: str, new_locator: str) -> Dict[str, Any]:  # noqa: PLR0912
        root = os.getenv("HEALING_REWRITE_ROOT", os.getcwd())
        include_globs = os.getenv(
            "HEALING_REWRITE_GLOBS",
            "**/*.robot,**/*.resource,**/*.py,**/*.json,**/*.yaml,**/*.yml,**/*.txt",
        )
        exclude_globs = os.getenv(
            "HEALING_REWRITE_EXCLUDE",
            "healed_locators.json,*_history.jsonl,.git/**,venv/**,.venv/**,node_modules/**,__pycache__/**",
        )
        dry_run = self._to_bool(os.getenv("HEALING_REWRITE_DRY_RUN"), False)
        max_bytes = int(os.getenv("HEALING_REWRITE_MAX_BYTES", "2097152"))

        include_patterns = [p.strip() for p in include_globs.split(",") if p.strip()]
        exclude_patterns = [p.strip() for p in exclude_globs.split(",") if p.strip()]
        candidates = set()
        for pat in include_patterns:
            candidates.update(glob.glob(os.path.join(root, pat), recursive=True))

        def is_excluded(path: str) -> bool:
            rel = os.path.relpath(path, root)
            for ep in exclude_patterns:
                if fnmatch.fnmatch(rel, ep) or fnmatch.fnmatch(path, ep):
                    return True
            base = os.path.basename(path)
            if base == os.path.basename(self.healed_file) or base == os.path.basename(self.audit_file):
                return True
            return False

        file_list = [p for p in candidates if os.path.isfile(p) and not is_excluded(p)]

        # Build patterns: quoted and whitespace-bound (for .robot/.txt)
        quoted = re.compile(r'(?P<q>["\']?)' + re.escape(old_locator) + r'(?P=q)')
        ws_bound = re.compile(r'(?<=\s)' + re.escape(old_locator) + r'(?=(\s|$))')

        files_changed = 0
        occurrences_replaced = 0
        changed_files: List[Dict[str, Any]] = []
        for path in sorted(file_list):
            try:
                if os.path.getsize(path) > max_bytes:
                    continue
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                original_text = text
                ext = os.path.splitext(path)[1].lower()
                count = 0

                # Prefer quoted replacements where applicable (preserve quotes)
                def _quoted_sub(m: re.Match) -> str:  # type: ignore[name-defined]
                    q = m.group("q") or ""
                    return f"{q}{new_locator}{q}"

                text, n1 = quoted.subn(_quoted_sub, text)
                count += n1

                # Also try whitespace-bound for plain occurrences in .robot/.txt
                if ext in (".robot", ".resource", ".txt"):
                    text, n2 = ws_bound.subn(new_locator, text)
                    count += n2

                if count > 0 and text != original_text:
                    if not dry_run:
                        backup_path = f"{path}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
                        try:
                            shutil.copy2(path, backup_path)
                        except Exception:
                            pass
                        with open(path, "w", encoding="utf-8", errors="ignore") as f:
                            f.write(text)
                    files_changed += 1
                    occurrences_replaced += count
                    changed_files.append({"path": os.path.relpath(path, root), "replacements": count})
            except Exception:
                continue

        return {
            "root": root,
            "files_changed": files_changed,
            "occurrences_replaced": occurrences_replaced,
            "changed_files": changed_files,
            "dry_run": dry_run,
        }

    # ------------------------- Public Keywords (overrides + utilities) -------------------------
    def test_ollama_connection(self) -> int:
        """Test connection to Ollama API"""
        print(f"[Healing] Testing Ollama connection → {self.ollama_url}")
        payload = {"model": self.model, "prompt": "ping", "stream": False}
        try:
            r = requests.post(self.ollama_url, json=payload, timeout=200)
            print(f"[Healing] Status: {r.status_code}, Response: {r.text[:200]}")
            return r.status_code
        except Exception as e:
            print(f"[Healing] ❌ Connection failed: {e}")
            raise

    # ---- Overrides with healing ----
    def click_element(self, locator: str):  # noqa: PLR0911
        """Healable version of Click Element"""
        print(f"[Healing] Click Element → {locator}")
        try:
            self._sl.click_element(locator)
            return
        except Exception as e:
            print(f"[Healing] ⚠️ Failed: {e}")
            if self.capture_on_fail:
                self._capture_page_artifacts("locator_not_found_click", locator)
            if not self.auto_heal:
                raise

        # Try known healed first (and revalidate)
        healed = self._get_current_healed(locator)
        if healed and not self._is_locator_currently_valid(healed):
            healed = None

        heal_result = self._heal_locator(locator) if not healed else self._build_cached_heal_result(locator, healed)
        if heal_result and heal_result.get("entry"):
            new_loc = heal_result["entry"]["locator"]
            print(f"[Healing] Retrying with healed locator: {new_loc}")
            try:
                self._sl.click_element(new_loc)
            except Exception as e2:
                print(f"[Healing] ❌ Healed click failed: {e2}")
                if self.capture_on_fail:
                    self._capture_page_artifacts("healed_locator_click_failed", new_loc)
                raise
            # Action succeeded with healed selector → enforce fail policy
            self._maybe_fail_post_heal(
                locator,
                new_loc,
                heal_result.get("rewrite_stats"),
                heal_result.get("score_report"),
            )
            return
        else:
            # Healing could not find a working locator
            if self.capture_on_fail:
                self._capture_page_artifacts("healing_failed_click", locator)
            raise

    def input_text(self, locator: str, text: str):  # noqa: PLR0911
        """Healable version of Input Text"""
        print(f"[Healing] Input Text → {locator}")
        try:
            self._sl.input_text(locator, text)
            return
        except Exception as e:
            print(f"[Healing] ⚠️ Failed: {e}")
            if self.capture_on_fail:
                self._capture_page_artifacts("locator_not_found_input", locator)
            if not self.auto_heal:
                raise

        # Try known healed first (and revalidate)
        healed = self._get_current_healed(locator)
        if healed and not self._is_locator_currently_valid(healed):
            healed = None

        heal_result = self._heal_locator(locator) if not healed else self._build_cached_heal_result(locator, healed)
        if heal_result and heal_result.get("entry"):
            new_loc = heal_result["entry"]["locator"]
            print(f"[Healing] Retrying with healed locator: {new_loc}")
            try:
                self._sl.input_text(new_loc, text)
            except Exception as e2:
                print(f"[Healing] ❌ Healed input failed: {e2}")
                if self.capture_on_fail:
                    self._capture_page_artifacts("healed_locator_input_failed", new_loc)
                raise
            # Action succeeded with healed selector → enforce fail policy
            self._maybe_fail_post_heal(
                locator,
                new_loc,
                heal_result.get("rewrite_stats"),
                heal_result.get("score_report"),
            )
            return
        else:
            if self.capture_on_fail:
                self._capture_page_artifacts("healing_failed_input", locator)
            raise

    def validate_locator(  # noqa: PLR0913
        self,
        locator: Any,
        type_hint: Optional[str] = None,
        require_visible: Optional[bool] = None,
        unique: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Utility keyword to validate a locator quickly from Robot:
        - Accepts either a raw locator string or a dict with {'type','locator'}.
        - Optionally pass type_hint for raw strings to help normalization.
        - Returns a dict: {'ok': bool, 'locator': <normalized>, 'count': int}
        """
        if isinstance(locator, dict):
            entry = locator
        else:
            entry = {"type": type_hint, "locator": locator}
        ok, sl_loc, count = self._locator_exists_in_dom(
            entry,
            require_visible=self.require_visible if require_visible is None else self._to_bool(require_visible),
            unique=self.require_unique if unique is None else self._to_bool(unique),
            retries=self.validate_retries,
            interval_ms=self.validate_retry_interval_ms,
        )
        print(f"[Healing] Validate Locator → ok={ok} locator={sl_loc} count={count}")
        return {"ok": ok, "locator": sl_loc, "count": count}

    def highlight_healings(self):
        """Print all healed locators including history"""
        print("\n==== Healed Locators ====")
        if not self.healed_locators:
            print("No healed locators yet.")
        for old, data in self.healed_locators.items():
            if isinstance(data, dict):
                cur = data.get("current")
                print(f"{old} → {cur}")
                hist = data.get("history", [])
                for i, h in enumerate(hist, 1):
                    print(f"  [{i}] was → {h}")
            else:
                print(f"{old} → {data}")
        print("==========================\n")

    # ---- NEW: DOM Library keywords ----
    def print_locator_library(self, max_nodes: int = 1000) -> Dict[str, Any]:
        """
        Collect and print the DOM Locator Library (ids, names, dataTest, ariaLabels, roles, texts, cssClasses).
        Returns the dictionary as well for further use.
        """
        lib = self._collect_dom_locator_library(max_nodes=max_nodes)
        print("\n==== DOM Locator Library ====")
        try:
            pretty = json.dumps(lib, indent=2, ensure_ascii=False)
            print(pretty)
        except Exception:
            print(str(lib))
        print("=============================\n")
        return lib

    def get_locator_library(self, max_nodes: int = 1000) -> Dict[str, Any]:
        """
        Return the DOM Locator Library (without printing).
        """
        return self._collect_dom_locator_library(max_nodes=max_nodes)

    # ---- Toggles ----
    def enable_auto_healing(self):
        """Enable automatic locator healing at runtime."""
        self.auto_heal = True
        print("[Healing] Auto-Heal enabled.")

    def disable_auto_healing(self):
        """Disable automatic locator healing at runtime."""
        self.auto_heal = False        # noqa: FBT003
        print("[Healing] Auto-Heal disabled.")

    def set_auto_healing(self, value: Any):
        """Set automatic locator healing ON/OFF (accepts true/false)."""
        self.auto_heal = self._to_bool(value, self.auto_heal)
        print(f"[Healing] Auto-Heal set to: {self.auto_heal}")

    def get_auto_healing_status(self) -> bool:
        """Return current Auto-Heal boolean status."""
        return self.auto_heal

    def enable_auto_rewrite(self):
        """Enable automatic source rewrite at runtime."""
        self.auto_rewrite = True
        print("[Healing] Auto-Rewrite enabled.")

    def disable_auto_rewrite(self):
        """Disable automatic source rewrite at runtime."""
        self.auto_rewrite = False     # noqa: FBT003
        print("[Healing] Auto-Rewrite disabled.")

    def set_auto_rewrite(self, value: Any):
        """Set automatic source rewrite ON/OFF (accepts true/false)."""
        self.auto_rewrite = self._to_bool(value, self.auto_rewrite)
        print(f"[Healing] Auto-Rewrite set to: {self.auto_rewrite}")

    def get_auto_rewrite_status(self) -> bool:
        """Return current Auto-Rewrite boolean status."""
        return self.auto_rewrite

    # ---- Safety nets (help avoid "No browser is open") ----
    def is_browser_open(self) -> bool:
        """Return True if at least one browser is open (window titles can be fetched)."""
        try:
            titles = self._sl.get_window_titles()
            return bool(titles)
        except Exception:
            return False

    def get_browser_count(self) -> int:
        """Return the number of open browsers known to SeleniumLibrary."""
        try:
            ids = self._sl.get_browser_ids()
            return len(ids)
        except Exception:
            return 0

    def ensure_browser_open(  # noqa: PLR0913
        self, url: Optional[str] = None, browser: Optional[str] = None, **open_kwargs
    ):
        """
        Ensure a browser session exists; if not, open one.
        Parameters are passed to SeleniumLibrary's Open Browser keyword.
        """
        if self.is_browser_open():
            print("[Healing] Browser already open.")
            return
        url = url or self.default_url or "about:blank"
        browser = browser or self.default_browser or "chrome"
        print(f"[Healing] No browser open → Opening: url={url} browser={browser}")
        self._sl.open_browser(url, browser, **open_kwargs)

    def open_browser_if_needed(self, url: Optional[str] = None, browser: Optional[str] = None, **open_kwargs):
        """
        Alias of Ensure Browser Open (kept for readability in some suites).
        """
        return self.ensure_browser_open(url=url, browser=browser, **open_kwargs)

    # -------------------------------------- Healing action --------------------------------------
    def _get_current_healed(self, locator: str) -> Optional[Dict[str, Any]]:
        entry = self.healed_locators.get(locator)
        if not entry:
            return None
        if isinstance(entry, dict) and "current" in entry:
            return entry["current"]
        if isinstance(entry, dict) and "locator" in entry:
            return entry  # legacy flat format
        return None

    def _build_cached_heal_result(self, old_locator: str, healed: Dict[str, Any]) -> Dict[str, Any]:
        """
        Reuse a previously healed locator and still apply runtime policies like
        auto-rewrite. Previously cached heals skipped rewrite entirely, which made
        auto_rewrite=True look like it was ignored.
        """
        new_locator = healed.get("locator")
        print(f"[Healing] Using cached healed locator: {new_locator}")
        rewrite_stats: Optional[Dict[str, Any]] = None
        if self.auto_rewrite and new_locator:
            rewrite_stats = self._maybe_autorewrite(old_locator, new_locator)
        return {
            "entry": healed,
            "rewrite_stats": rewrite_stats,
            "score_report": healed.get("score_report") if isinstance(healed, dict) else None,
        }

    def _record_healing(
        self,
        old_locator: str,
        suggestion: Dict[str, Any],
        score_report: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Persist a healed locator with timestamp and history.
        'suggestion' is a dict like {'type': 'xpath', 'locator': '...'}.
        'score_report' captures all AI candidates and deterministic scoring.
        Also triggers code rewrite when enabled.
        Returns rewrite_stats (dict) or None.
        """
        now = self._iso_now()
        new_entry = {
            "type": suggestion.get("type"),
            "locator": suggestion.get("locator"),
            "updated_at": now,
        }
        if suggestion.get("score") is not None:
            new_entry["score"] = suggestion.get("score")
        if score_report:
            new_entry["score_report"] = score_report
        existing = self.healed_locators.get(old_locator)
        if not existing:
            self.healed_locators[old_locator] = {"current": new_entry, "history": []}
        else:
            prev_current = existing.get("current")
            if prev_current:
                existing.setdefault("history", []).append(prev_current)
            existing["current"] = new_entry

        self._write_snapshot()
        audit_extra = {"score_report": score_report} if score_report else None
        self._append_audit(old_locator, new_entry, event_type="heal", extra=audit_extra)

        rewrite_stats: Optional[Dict[str, Any]] = None
        if self.auto_rewrite:
            rewrite_stats = self._maybe_autorewrite(old_locator, new_entry["locator"])
        return rewrite_stats

    def _heal_locator(self, locator: str) -> Optional[Dict[str, Any]]:  # noqa: PLR0912
        """
        Attempt to heal given locator using current page HTML via Ollama.
        On success, persists with timestamp/history and returns a dict:
        { 'entry': <current_healed_entry>, 'rewrite_stats': <dict or None>, 'score_report': <dict> }
        """
        driver = getattr(self._sl, "driver", None)
        if not driver:
            print("[Healing] ❌ No Selenium driver found.")
            return None

        ai_result = self._ask_ollama_for_new_locator(locator, driver.page_source)
        if ai_result and ai_result.get("candidates"):
            score_report = self._score_locator_candidates(
                locator,
                ai_result.get("candidates", []),
                ai_result.get("dom_library", {}),
                int(ai_result.get("html_chars_used", 0)),
            )
            self._print_locator_score_report(score_report)

            selected = score_report.get("selected")
            if not selected:
                if self.capture_on_fail:
                    self._capture_page_artifacts("no_scored_locator_candidate", locator)
                print("[Healing] ❌ No scored locator candidate passed live DOM validation.")
                return None

            suggestion = {
                "type": selected.get("type"),
                "locator": selected.get("normalized_locator"),
                "score": selected.get("score"),
            }
            rewrite_stats = self._record_healing(locator, suggestion, score_report=score_report)
            return {
                "entry": self._get_current_healed(locator),
                "rewrite_stats": rewrite_stats,
                "score_report": score_report,
            }
        return None

    def _maybe_fail_post_heal(
        self,
        old_locator: str,
        new_locator: str,
        rewrite_stats: Optional[Dict[str, Any]],
        score_report: Optional[Dict[str, Any]] = None,
    ):
        """Apply fail policy after a successful heal+action."""
        changed = int((rewrite_stats or {}).get("files_changed", 0))
        total = int((rewrite_stats or {}).get("occurrences_replaced", 0))
        dry_run = bool((rewrite_stats or {}).get("dry_run", False))

        must_fail = False
        reason_parts = ["Original locator not found in DOM → auto-healed."]
        if score_report and score_report.get("selected"):
            selected = score_report["selected"]
            reason_parts.append(
                "Scoring selected locator "
                f"'{selected.get('normalized_locator')}' with score {selected.get('score')} "
                f"from {score_report.get('candidate_count')} AI candidate(s)."
            )
        if rewrite_stats is not None:
            reason_parts.append(
                f"Rewrote occurrences: '{old_locator}' → '{new_locator}' ({changed} file(s), {total} occurrence(s){' DRY-RUN' if dry_run else ''})."
            )
            if self.fail_after_rewrite and changed and not dry_run:
                must_fail = True
        else:
            reason_parts.append("No source rewrite performed (AUTOREWRITE disabled or no literal occurrences found).")

        if self.fail_on_heal:
            must_fail = True

        if must_fail:
            msg = "\n".join(reason_parts + [
                "Failing the test intentionally so the incorrect coded locator is fixed and committed."
            ])
            try:
                BuiltIn().fail(msg)
            except Exception as exc:
                # In case we're not under Robot execution context
                raise AssertionError(msg) from exc
# End of class
