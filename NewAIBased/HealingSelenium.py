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
HEALING_AI                   default: true  (arg: ai_healing / ai)
HEALING_FROM_HISTORY         default: true  (arg: heal_from_history)
HEALING_NON_AI               default: false (arg: non_ai_healing / non_ai)

# Validation knobs
HEALING_REQUIRE_VISIBLE      default: false
HEALING_REQUIRE_UNIQUE       default: false
HEALING_VALIDATE_RETRIES     default: 1
HEALING_VALIDATE_RETRY_INTERVAL_MS default: 150

# Failure artifacts
HEALING_CAPTURE_ON_FAIL      default: true
HEALING_CAPTURE_HEALED_SCREENSHOT default: true
HEALING_SAVE_HTML_ON_FAIL    default: false
HEALING_ARTIFACT_DIR         default: healing_screens (under ${OUTPUT DIR})
HEALING_ALLURE_REPORT        default: true
HEALING_ALLURE_SUMMARY       default: true
HEALING_ALLURE_RESULTS_DIR   default: ${OUTPUT DIR}/allure (for environment.properties only)
HEALING_REPORT_DIR           default: healing_reports (under ${OUTPUT DIR})

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
import base64
import csv
import difflib
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
    ROBOT_LISTENER_API_VERSION = 3

    # ------------------------------ Initialization ------------------------------
    def __init__(  # noqa: PLR0913
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        healed_file: str = "healed_locators.json",
        auto_heal: Optional[bool] = None,
        auto_rewrite: Optional[bool] = None,
        ai_healing: Optional[bool] = None,
        ai: Optional[bool] = None,
        heal_from_history: Optional[bool] = None,
        non_ai_healing: Optional[bool] = None,
        non_ai: Optional[bool] = None,
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
        ai_value = ai_healing if ai_healing is not None else ai
        self.ai_healing = self._arg_or_env_bool(ai_value, "HEALING_AI", True)
        self.heal_from_history = self._arg_or_env_bool(heal_from_history, "HEALING_FROM_HISTORY", True)
        non_ai_value = non_ai_healing if non_ai_healing is not None else non_ai
        self.non_ai_healing = self._arg_or_env_bool(non_ai_value, "HEALING_NON_AI", False)

        # Validation knobs
        self.require_visible = self._arg_or_env_bool(None, "HEALING_REQUIRE_VISIBLE", False)
        self.require_unique = self._arg_or_env_bool(None, "HEALING_REQUIRE_UNIQUE", False)
        self.validate_retries = int(os.getenv("HEALING_VALIDATE_RETRIES", "3"))
        self.validate_retry_interval_ms = int(os.getenv("HEALING_VALIDATE_RETRY_INTERVAL_MS", "150"))

        # Failure artifact toggle
        self.capture_on_fail = self._arg_or_env_bool(None, "HEALING_CAPTURE_ON_FAIL", True)
        self.capture_healed_screenshot = self._arg_or_env_bool(None, "HEALING_CAPTURE_HEALED_SCREENSHOT", True)
        self.allure_report = self._arg_or_env_bool(None, "HEALING_ALLURE_REPORT", True)
        self.allure_summary = self._arg_or_env_bool(None, "HEALING_ALLURE_SUMMARY", True)

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
        self.healing_event_count = 0
        self.healing_events: List[Dict[str, Any]] = []
        self.healed_test_ids = set()
        self.active_test_identity: Dict[str, Any] = {}
        self.test_results_by_id: Dict[str, str] = {}
        self.test_stats = {"executed": 0, "passed": 0, "failed": 0, "skipped": 0}
        self.ROBOT_LIBRARY_LISTENER = self

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
            f"Capture-Healed-Screenshot:{self.capture_healed_screenshot} "
            f"Fail-On-Heal:{self.fail_on_heal} Fail-After-Rewrite:{self.fail_after_rewrite}\n"
            f"Artifacts dir:{os.getenv('HEALING_ARTIFACT_DIR', 'healing_screens')}\n"
            f"[HealingSelenium] Allure-Report:{self.allure_report} "
            f"Allure-Summary:{self.allure_summary} "
            f"AI-Healing:{self.ai_healing} History-Healing:{self.heal_from_history} "
            f"Non-AI-Healing:{self.non_ai_healing} "
            f"Report dir:{os.getenv('HEALING_REPORT_DIR', 'healing_reports')}\n"
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
            "Enable AI Healing": self.enable_ai_healing,
            "Disable AI Healing": self.disable_ai_healing,
            "Set AI Healing": self.set_ai_healing,
            "Get AI Healing Status": self.get_ai_healing_status,
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

    # -------------------------- Robot listener summary hooks --------------------------
    def start_test(self, *args):
        """Capture the active Robot test identity before any healing event fires."""
        try:
            self.active_test_identity = self._get_listener_test_identity(*args) or {}
        except Exception as exc:
            print(f"[Healing] Listener test identity capture failed: {exc}")

    def end_test(self, *args):
        """
        Robot listener hook. Tracks real test status counts so healing summaries can
        show executed/passed/failed/skipped without creating fake Allure test cases.
        """
        try:
            result = args[-1] if args else None
            status = str(getattr(result, "status", "") or "").upper()
            if not status and len(args) >= 2 and isinstance(args[1], dict):
                status = str(args[1].get("status", "")).upper()
            if not status:
                return
            self.test_stats["executed"] += 1
            if status == "PASS":
                self.test_stats["passed"] += 1
            elif status == "SKIP":
                self.test_stats["skipped"] += 1
            else:
                self.test_stats["failed"] += 1
            identity = self._get_listener_test_identity(*args) or self.active_test_identity or self._get_test_identity()
            if identity.get("test_case_id"):
                self.test_results_by_id[identity["test_case_id"]] = status
            for event in self.healing_events:
                if event.get("test_case_id") == identity["test_case_id"]:
                    event["result_status"] = status
            self.active_test_identity = {}
            self._write_healing_run_summary()
            self._write_healing_environment_summary()
        except Exception as exc:
            print(f"[Healing] Listener test summary update failed: {exc}")

    def close(self):
        """
        Robot listener hook. Called when the whole test execution ends.
        Ensures any pending healing events are marked as failures in the dashboard.
        """
        try:
            changed = False
            for event in self.healing_events:
                if event.get("result_status") == "PENDING":
                    event["result_status"] = "FAIL"
                    changed = True
            if changed and self.active_test_identity:
                test_case_id = self.active_test_identity.get("test_case_id")
                if test_case_id and test_case_id not in self.test_results_by_id:
                    self.test_results_by_id[test_case_id] = "FAIL"
                    self.test_stats["executed"] += 1
                    self.test_stats["failed"] += 1
            self.active_test_identity = {}
            
            summary_paths = self._write_healing_run_summary()
            self._inject_allure_suite_teardown_attachments(summary_paths)
            self._write_healing_environment_summary()
        except Exception as exc:
            print(f"[Healing] Listener close failed: {exc}")

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

    def _capture_healed_element_screenshot(self, old_locator: str, healed_locator: str) -> Optional[str]:
        """
        Highlight the selected healed element, capture a screenshot, then restore
        the page style. This screenshot is used as healing evidence in reports.
        """
        if not self.capture_healed_screenshot:
            return None
        drv = getattr(self._sl, "driver", None)
        if not drv or not healed_locator:
            return None

        out_dir = os.path.join(
            self._get_output_dir(),
            os.getenv("HEALING_ARTIFACT_DIR", "healing_screens"),
        )
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        png_path = os.path.join(
            out_dir,
            f"{ts}__healed_element__{self._sanitize_name(old_locator)}.png",
        )

        element = None
        try:
            elems = self._sl.get_webelements(healed_locator)
            visible = [e for e in elems if getattr(e, "is_displayed", lambda: True)()]
            element = visible[0] if visible else (elems[0] if elems else None)
            if element is None:
                print(f"[Healing] ⚠️ Could not find healed element for screenshot: {healed_locator}")
                return None

            drv.execute_script(
                r"""
                const el = arguments[0];
                const oldStyle = el.getAttribute('style') || '';
                el.setAttribute('data-healing-old-style', oldStyle);
                el.scrollIntoView({block: 'center', inline: 'center'});
                el.style.setProperty('outline', '5px solid #ff2d55', 'important');
                el.style.setProperty('outline-offset', '3px', 'important');
                el.style.setProperty('box-shadow', '0 0 0 8px rgba(255,45,85,.35), 0 0 28px rgba(255,45,85,.95)', 'important');
                el.style.setProperty('background-color', 'rgba(255,245,0,.22)', 'important');
                el.style.setProperty('transition', 'none', 'important');

                const oldBadge = document.getElementById('__healing_element_badge');
                if (oldBadge) oldBadge.remove();
                const rect = el.getBoundingClientRect();
                const badge = document.createElement('div');
                badge.id = '__healing_element_badge';
                badge.textContent = 'HEALED LOCATOR';
                badge.style.cssText = [
                    'position:fixed',
                    'z-index:2147483647',
                    'left:' + Math.max(8, rect.left) + 'px',
                    'top:' + Math.max(8, rect.top - 34) + 'px',
                    'background:#ff2d55',
                    'color:white',
                    'font:700 13px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
                    'letter-spacing:.04em',
                    'padding:6px 10px',
                    'border-radius:6px',
                    'box-shadow:0 4px 14px rgba(0,0,0,.28)',
                    'pointer-events:none'
                ].join(';');
                document.documentElement.appendChild(badge);
                """,
                element,
            )
            time.sleep(0.2)

            try:
                self._sl.capture_page_screenshot(png_path)
            except Exception:
                if not drv.get_screenshot_as_file(png_path):
                    return None
            return png_path
        except Exception as exc:
            print(f"[Healing] ⚠️ Healed element screenshot failed: {exc}")
            return None
        finally:
            if element is not None:
                try:
                    drv.execute_script(
                        r"""
                        const el = arguments[0];
                        const oldStyle = el.getAttribute('data-healing-old-style');
                        if (oldStyle === null || oldStyle === '') {
                            el.removeAttribute('style');
                        } else {
                            el.setAttribute('style', oldStyle);
                        }
                        el.removeAttribute('data-healing-old-style');
                        const badge = document.getElementById('__healing_element_badge');
                        if (badge) badge.remove();
                        """,
                        element,
                    )
                except Exception:
                    pass

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
        raw_tokens = re.findall(r"[A-Za-z0-9_]{2,}", old_locator or "")
        common = {
            "xpath", "css", "id", "name", "div", "span", "button", "input", "select",
            "class", "type", "text", "contains", "starts", "with", "and", "or",
            "following", "preceding", "true", "false"
        }
        out: List[str] = []
        for token in raw_tokens:
            parts = [token]
            parts.extend(re.split(r"[_\-.:\s]+", token))
            camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token)
            parts.extend(camel.split())
            alpha = re.sub(r"[^A-Za-z]+", "", token)
            if alpha and alpha != token:
                parts.append(alpha)
            for part in parts:
                value = part.lower().strip("_-.:")
                if len(value) >= 2 and value not in common:
                    out.append(value)
                    if len(value) >= 4:
                        out.append(value[:4])
                    if len(value) >= 3:
                        out.append(value[:3])
        # Keep first few distinct hints
        uniq: List[str] = []
        for t in out:
            if t not in uniq and t not in common:
                uniq.append(t)
            if len(uniq) >= 12:
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
        if raw.get("source"):
            out["source"] = str(raw.get("source"))[:80]

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

    def _element_nearby_context(self, element: Any) -> str:
        """
        Build a compact text/attribute fingerprint for the matched element plus
        nearby label, parent, and sibling context. Used to keep deterministic
        non-AI candidates tied to the broken locator's intent.
        """
        script = r"""
            const el = arguments[0];
            const norm = s => (s || '').toString().replace(/\s+/g, ' ').trim();
            const attrNames = [
                'id', 'name', 'type', 'class', 'aria-label', 'role', 'title',
                'placeholder', 'value', 'href', 'data-test', 'data-testid',
                'data-qa', 'data-cy'
            ];
            const parts = [];
            const add = (label, value) => {
                const v = norm(value);
                if (v) parts.push(label + '=' + v.slice(0, 180));
            };
            const addAttrs = (prefix, node) => {
                if (!node || !node.getAttribute) return;
                parts.push(prefix + ':tag=' + String(node.tagName || '').toLowerCase());
                for (const name of attrNames) add(prefix + ':' + name, node.getAttribute(name));
                add(prefix + ':text', node.innerText || node.textContent || '');
            };

            addAttrs('self', el);
            if (el.id) {
                for (const label of Array.from(document.querySelectorAll('label'))) {
                    if (label.getAttribute('for') === el.id) add('label-for', label.innerText || label.textContent || '');
                }
            }
            let parent = el.parentElement;
            for (let depth = 1; parent && depth <= 2; depth++) {
                addAttrs('parent' + depth, parent);
                parent = parent.parentElement;
            }
            for (const side of ['previousElementSibling', 'nextElementSibling']) {
                const sib = el[side];
                if (sib) addAttrs(side, sib);
            }
            return parts.join(' | ').slice(0, 4000);
        """
        try:
            drv = getattr(self._sl, "driver", None)
            if not drv:
                return ""
            return str(drv.execute_script(script, element) or "")
        except Exception:
            return ""

    def _context_hint_hits(self, element: Any, hints: List[str]) -> Tuple[bool, List[str], str]:
        context = self._element_nearby_context(element)
        context_l = context.lower()
        meaningful = [h for h in hints if len(h) >= 2 and not h.isdigit()]
        if not meaningful:
            return False, [], context[:500]
        hits = []
        words = re.findall(r"[a-z0-9_]{3,}", context_l)
        for h in meaningful:
            if h in context_l:
                hits.append(h)
                continue
            close = difflib.get_close_matches(h, words, n=1, cutoff=0.7)
            if close:
                hits.append(f"{h}~(similar to {close[0]})")
        return bool(hits), hits[:8], context[:500]

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
        elems: List[Any] = []
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
        source = str(candidate.get("source") or "ai").lower()
        is_non_ai = source == "non-ai"
        non_ai_context_ok = True
        context_hint_hits: List[str] = []
        context_preview = ""

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

        if is_non_ai:
            if not unique_ok:
                config_ok = False
                reasons.append("non-AI rejected: locator must be unique")
            elif elems:
                non_ai_context_ok, context_hint_hits, context_preview = self._context_hint_hits(elems[0], hints)
                if non_ai_context_ok:
                    score += 22
                    reasons.append(
                        "non-AI context verified near matched element: "
                        + ", ".join(context_hint_hits[:5])
                    )
                else:
                    config_ok = False
                    reasons.append("non-AI rejected: no broken-locator hint found in element/nearby DOM context")
            else:
                non_ai_context_ok = False
                config_ok = False

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
        if candidate.get("weak"):
            score -= 25
            reasons.append("penalized generic non-AI fallback")
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
            "source": candidate.get("source") or "ai",
            "weak": bool(candidate.get("weak")),
            "non_ai_context_ok": non_ai_context_ok if is_non_ai else None,
            "context_hint_hits": context_hint_hits,
            "context_preview": context_preview,
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
                "non_ai_unique": True,
                "non_ai_context_match": True,
                "retries": self.validate_retries,
                "interval_ms": self.validate_retry_interval_ms,
            },
        }

    def _print_locator_score_report(self, score_report: Dict[str, Any]):
        """Print a compact score report into Robot logs/console."""
        print("\n==== Locator Candidate Score Report ====")
        print(
            f"Candidates: total={score_report.get('candidate_count')} "
            f"ai={score_report.get('ai_candidate_count', 0)} "
            f"snapshot_history={score_report.get('history_candidate_count', 0)} "
            f"non_ai={score_report.get('non_ai_candidate_count', 0)}"
        )
        selected = score_report.get("selected") or {}
        selected_norm = selected.get("normalized_locator")
        for item in score_report.get("candidates", []):
            mark = "*" if item.get("normalized_locator") == selected_norm and item.get("ok") else " "
            print(
                f"{mark} rank={item.get('score_rank')} score={item.get('score')} ok={item.get('ok')} "
                f"count={item.get('count')} visible={item.get('visible_count')} "
                f"source={item.get('source')} locator={item.get('normalized_locator')}"
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
            print("[Healing] No locator candidate passed live DOM validation/scoring requirements.")
        print("========================================\n")

    def _html_escape(self, value: Any) -> str:
        text = "" if value is None else str(value)
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )

    def _short_log_value(self, value: Any, limit: int = 160) -> str:
        text = "" if value is None else str(value)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _image_data_url(self, path: Optional[str]) -> Optional[str]:
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except Exception:
            return None

    def _healing_report_dir(self) -> str:
        out_dir = os.path.join(
            self._get_output_dir(),
            os.getenv("HEALING_REPORT_DIR", "healing_reports"),
        )
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _build_healing_report_html(
        self,
        old_locator: str,
        new_locator: str,
        score_report: Dict[str, Any],
        rewrite_stats: Optional[Dict[str, Any]],
    ) -> str:
        selected = score_report.get("selected") or {}
        selected_norm = selected.get("normalized_locator") or new_locator
        score = selected.get("score", "")
        ai_count = score_report.get("ai_candidate_count", 0)
        history_count = score_report.get("history_candidate_count", 0)
        non_ai_count = score_report.get("non_ai_candidate_count", 0)
        total_count = score_report.get("candidate_count", 0)
        screenshot_path = score_report.get("healed_screenshot_path")
        screenshot_data_url = self._image_data_url(screenshot_path)
        screenshot_html = ""
        if screenshot_data_url:
            screenshot_html = (
                "<div class=\"card shot-card\">"
                "<div class=\"label\">Highlighted healed element screenshot</div>"
                f"<img class=\"shot\" src=\"{screenshot_data_url}\" alt=\"Highlighted healed element screenshot\">"
                "</div>"
            )
        elif screenshot_path:
            screenshot_html = (
                "<div class=\"card\">"
                "<div class=\"label\">Highlighted healed element screenshot</div>"
                f"<div class=\"locator\">{self._html_escape(screenshot_path)}</div>"
                "</div>"
            )

        rows: List[str] = []
        for item in score_report.get("candidates", []):
            is_ok = bool(item.get("ok"))
            row_cls = "selected" if item.get("normalized_locator") == selected_norm and is_ok else ""
            reasons = "<br>".join(self._html_escape(r) for r in (item.get("reasons") or []))
            status_badge = "<span class='badge-ok'>OK</span>" if is_ok else "<span class='badge-fail'>FAIL</span>"
            
            rows.append(
                f"<tr class='{row_cls}'>"
                f"<td>{self._html_escape(item.get('score_rank'))}</td>"
                f"<td>{self._html_escape(item.get('score'))}</td>"
                f"<td>{status_badge}</td>"
                f"<td>{self._html_escape(item.get('count'))}</td>"
                f"<td>{self._html_escape(item.get('source'))}</td>"
                f"<td class='code-text'>{self._html_escape(item.get('normalized_locator'))}</td>"
                f"<td><div style='max-height: 100px; overflow-y: auto; font-size: 0.85rem;'>{reasons}</div></td>"
                "</tr>"
            )

        if rewrite_stats is None:
            rewrite_text = "No source rewrite performed (Not written)."
            rewrite_class = "muted"
        else:
            files_written = ", ".join(rewrite_stats.get("written_files_with_lines", []))
            rewrite_text = (
                f"{rewrite_stats.get('files_changed', 0)} file(s), "
                f"{rewrite_stats.get('occurrences_replaced', 0)} occurrence(s) "
                f"({rewrite_stats.get('dry_run') and 'DRY-RUN' or 'WRITTEN'})."
            )
            if files_written:
                rewrite_text += f" Changed: {files_written}"
            rewrite_class = "ok" if rewrite_stats.get("files_changed", 0) else "muted"

        generated_at = self._iso_now()
        source_display = "AI HEALED" if score_report.get("ai_count", 0) > 0 else "AUTO HEALED"
        ai_class = "ai-healed" if source_display == "AI HEALED" else ""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HealingSelenium - AI Healing Report</title>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-base: #0b0f19;
      --glass-bg: rgba(255, 255, 255, 0.03);
      --glass-border: rgba(255, 255, 255, 0.08);
      --glow-success: rgba(16, 185, 129, 0.4);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-green: #10b981;
      --accent-red: #ef4444;
      --gradient-brand: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
      --gradient-success: linear-gradient(135deg, #10b981 0%, #059669 100%);
    }}
    body {{
      font-family: 'Outfit', sans-serif;
      background-color: var(--bg-base);
      background-image: 
        radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.15) 0px, transparent 50%),
        radial-gradient(at 100% 0%, rgba(139, 92, 246, 0.15) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.1) 0px, transparent 50%);
      background-attachment: fixed;
      color: var(--text-main);
      margin: 0;
      padding: 40px 20px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .container {{ max-width: 1200px; width: 100%; animation: fadeIn 0.8s ease-out; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(20px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    .header-banner {{
      background: var(--glass-bg); backdrop-filter: blur(12px); border: 1px solid var(--glass-border);
      border-radius: 16px; padding: 30px; margin-bottom: 24px; position: relative; overflow: hidden;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    }}
    .header-banner::before {{
      content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%;
      background: var(--gradient-success); box-shadow: 0 0 20px var(--glow-success);
    }}
    .status-badge {{
      display: inline-flex; align-items: center; gap: 8px; background: rgba(16, 185, 129, 0.1);
      color: var(--accent-green); padding: 6px 14px; border-radius: 20px; font-size: 0.85rem;
      font-weight: 800; letter-spacing: 1px; text-transform: uppercase;
      border: 1px solid rgba(16, 185, 129, 0.2); margin-bottom: 16px; box-shadow: 0 0 15px rgba(16, 185, 129, 0.2);
    }}
    .status-badge.ai-healed {{
      background: rgba(139, 92, 246, 0.1); color: #c4b5fd; border-color: rgba(139, 92, 246, 0.3);
      box-shadow: 0 0 15px rgba(139, 92, 246, 0.2);
    }}
    .status-badge i {{
      display: inline-block; width: 8px; height: 8px; border-radius: 50%;
      background: currentColor; box-shadow: 0 0 8px currentColor; animation: pulse 2s infinite;
    }}
    @keyframes pulse {{ 0% {{ box-shadow: 0 0 0 0 currentColor; }} 70% {{ box-shadow: 0 0 0 8px rgba(0,0,0,0); }} 100% {{ box-shadow: 0 0 0 0 rgba(0,0,0,0); }} }}
    .locator-transition {{ font-family: 'JetBrains Mono', monospace; font-size: 1.1rem; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
    .locator-box {{ background: rgba(0, 0, 0, 0.3); padding: 12px 16px; border-radius: 8px; border: 1px solid var(--glass-border); word-break: break-all; }}
    .locator-box.old {{ border-bottom: 2px solid var(--accent-red); color: #fca5a5; }}
    .locator-box.new {{ border-bottom: 2px solid var(--accent-green); color: #6ee7b7; background: rgba(16, 185, 129, 0.05); }}
    .arrow {{ color: var(--text-muted); font-weight: 800; }}
    .metrics-row {{ display: flex; gap: 12px; margin-top: 24px; flex-wrap: wrap; }}
    .metric {{
      background: rgba(255, 255, 255, 0.02); padding: 8px 16px; border-radius: 6px; font-size: 0.9rem;
      display: flex; align-items: center; gap: 8px; border: 1px solid var(--glass-border);
    }}
    .metric-value {{ font-weight: 800; color: #fff; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; margin-bottom: 24px; }}
    .card {{
      background: var(--glass-bg); backdrop-filter: blur(12px); border: 1px solid var(--glass-border);
      border-radius: 16px; padding: 24px; box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
      transition: transform 0.3s ease, box-shadow 0.3s ease;
    }}
    .card:hover {{ transform: translateY(-5px); box-shadow: 0 8px 30px rgba(0, 0, 0, 0.4); border-color: rgba(255, 255, 255, 0.15); }}
    .card-title {{ font-size: 0.85rem; text-transform: uppercase; font-weight: 800; color: var(--text-muted); margin-bottom: 12px; letter-spacing: 1px; }}
    .card-content {{ font-size: 0.95rem; line-height: 1.6; color: #cbd5e1; }}
    .reason-list {{ margin: 0; padding-left: 16px; }}
    .reason-list li {{ margin-bottom: 6px; }}
    img.shot {{ max-width: 100%; max-height: 350px; width: auto; height: auto; object-fit: contain; display: block; margin: 0 auto; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 4px 15px rgba(0,0,0,0.5); }}
    table {{
      width: 100%; border-collapse: separate; border-spacing: 0; background: var(--glass-bg);
      backdrop-filter: blur(12px); border-radius: 16px; overflow: hidden; border: 1px solid var(--glass-border);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    }}
    th, td {{ padding: 16px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.05); }}
    th {{ background: rgba(0, 0, 0, 0.2); color: var(--text-muted); font-size: 0.85rem; text-transform: uppercase; font-weight: 800; letter-spacing: 1px; }}
    tr {{ transition: background 0.2s ease; }}
    tr:hover {{ background: rgba(255, 255, 255, 0.05); }}
    tr.selected td {{ background: rgba(16, 185, 129, 0.08); position: relative; }}
    tr.selected td:first-child::before {{
      content: ''; position: absolute; left: 0; top: 0; height: 100%; width: 3px;
      background: var(--accent-green); box-shadow: 0 0 10px var(--glow-success);
    }}
    .badge-ok {{ background: rgba(16, 185, 129, 0.2); color: #6ee7b7; padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .badge-fail {{ background: rgba(239, 68, 68, 0.2); color: #fca5a5; padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; border: 1px solid rgba(239, 68, 68, 0.3); }}
    .code-text {{ font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; color: #e2e8f0; word-break: break-all; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header-banner">
      <div class="status-badge {ai_class}"><i></i> {source_display}</div>
      <div class="locator-transition">
        <div class="locator-box old">{self._html_escape(old_locator)}</div>
        <div class="arrow">➔</div>
        <div class="locator-box new">{self._html_escape(selected_norm)}</div>
      </div>
      <div class="metrics-row">
        <div class="metric">Selected Score <span class="metric-value">{self._html_escape(score)}</span></div>
        <div class="metric">Candidates <span class="metric-value">{self._html_escape(total_count)}</span></div>
        <div class="metric">AI: <span class="metric-value">{self._html_escape(ai_count)}</span></div>
        <div class="metric">History: <span class="metric-value">{self._html_escape(history_count)}</span></div>
        <div class="metric">Non-AI: <span class="metric-value">{self._html_escape(non_ai_count)}</span></div>
        <div class="metric">Generated: <span class="metric-value">{self._html_escape(generated_at)}</span></div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="card-title">Library Enhancements Active</div>
        <div class="card-content">
          <ul class="reason-list">
            <li>AI healing uses full DOM context and intelligent inference.</li>
            <li>Historical heals are cross-referenced with deterministic scoring.</li>
            <li>Candidates are vigorously ranked using contextual alignment.</li>
            <li>The final selected locator successfully bypassed the element failure.</li>
          </ul>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Rewrite Operation Details</div>
        <div class="card-content code-text" style="color: #6ee7b7;">
            {self._html_escape(rewrite_text)}
        </div>
      </div>
      {screenshot_html}
    </div>

    <table>
      <thead>
        <tr>
          <th>Rnk</th>
          <th>Score</th>
          <th>Status</th>
          <th>Matches</th>
          <th>Source</th>
          <th>Locator Candidate</th>
          <th>Reasoning</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
  </div>
</body>
</html>"""

    def _write_healing_report_files(
        self,
        old_locator: str,
        html_body: str,
        score_report: Dict[str, Any],
    ) -> Dict[str, str]:
        out_dir = self._healing_report_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base = f"{ts}__healed__{self._sanitize_name(old_locator)}"
        html_path = os.path.join(out_dir, base + ".html")
        json_path = os.path.join(out_dir, base + ".json")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(score_report, f, indent=2, ensure_ascii=False)
        return {"html": html_path, "json": json_path}

    def _attach_healing_report_to_allure(
        self,
        old_locator: str,
        html_body: str,
        score_report: Dict[str, Any],
    ):
        if not self.allure_report:
            return
        try:
            from allure_commons._allure import attach  # type: ignore
            from allure_commons.types import AttachmentType  # type: ignore
        except Exception as exc:
            print(f"[Healing] Allure attachment skipped: allure_commons not available ({exc})")
            return

        name = f"Self-Healing Locator Bar - {self._sanitize_name(old_locator)[:70]}"
        try:
            attach(html_body, name=name, attachment_type=AttachmentType.HTML)
            screenshot_path = score_report.get("healed_screenshot_path")
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    png_type = getattr(AttachmentType, "PNG", None)
                    attach(
                        f.read(),
                        name=f"Highlighted Healed Element - {self._sanitize_name(old_locator)[:70]}",
                        attachment_type=png_type or "image/png",
                    )
            json_type = getattr(AttachmentType, "JSON", None)
            attach(
                json.dumps(score_report, indent=2, ensure_ascii=False),
                name=f"Self-Healing Score JSON - {self._sanitize_name(old_locator)[:70]}",
                attachment_type=json_type or "application/json",
            )
        except Exception as exc:
            print(f"[Healing] Allure attachment failed: {exc}")

    def _attach_healing_summary_files_to_allure(self, paths: Dict[str, str]):
        if not self.allure_report or not paths:
            return
        try:
            from allure_commons._allure import attach  # type: ignore
            from allure_commons.types import AttachmentType  # type: ignore
        except Exception as exc:
            print(f"[Healing] Allure summary attachments skipped: allure_commons not available ({exc})")
            return

        try:
            html_path = paths.get("html")
            csv_path = paths.get("csv")
            json_path = paths.get("json")
            if html_path and os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    attach(f.read(), name="Healing Run Dashboard", attachment_type=AttachmentType.HTML)
            if csv_path and os.path.exists(csv_path):
                with open(csv_path, "r", encoding="utf-8") as f:
                    csv_type = getattr(AttachmentType, "CSV", None)
                    attach(f.read(), name="Healing Data CSV", attachment_type=csv_type or "text/csv")
            if json_path and os.path.exists(json_path):
                with open(json_path, "r", encoding="utf-8") as f:
                    json_type = getattr(AttachmentType, "JSON", None)
                    attach(f.read(), name="Healing Run Summary JSON", attachment_type=json_type or "application/json")
        except Exception as exc:
            print(f"[Healing] Allure summary attachment failed: {exc}")

    def _inject_allure_suite_teardown_attachments(self, paths: Dict[str, str]):
        """Directly modifies Allure result JSON to append dashboard to suite teardown."""
        if not self.allure_report or not paths:
            return
        out_dir = self._allure_results_dir()
        containers = glob.glob(os.path.join(out_dir, "*-container.json"))
        if not containers:
            return
        
        root_container = None
        max_children = -1
        for c in containers:
            try:
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                num_children = len(data.get("children", []))
                if num_children > max_children:
                    max_children = num_children
                    root_container = c
            except Exception:
                pass
                
        if not root_container:
            return

        try:
            import uuid
            import shutil
            attachments = []
            
            def add_attachment(file_path, name, mime_type, ext):
                if file_path and os.path.exists(file_path):
                    att_uuid = str(uuid.uuid4())
                    dest = os.path.join(out_dir, f"{att_uuid}-attachment.{ext}")
                    shutil.copy(file_path, dest)
                    attachments.append({
                        "name": name,
                        "source": f"{att_uuid}-attachment.{ext}",
                        "type": mime_type
                    })
                    
            add_attachment(paths.get("html"), "Healing Run Dashboard", "text/html", "html")
            add_attachment(paths.get("csv"), "Healing Data CSV", "text/csv", "csv")
            add_attachment(paths.get("json"), "Healing Run Summary JSON", "application/json", "json")
            
            if not attachments:
                return
                
            with open(root_container, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            if "afters" not in data:
                data["afters"] = []
                
            data["afters"].append({
                "name": "Healing Run Dashboard",
                "status": "passed",
                "stage": "finished",
                "attachments": attachments,
                "steps": []
            })
            
            with open(root_container, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as exc:
            print(f"[Healing] Allure suite teardown injection failed: {exc}")

    def _get_robot_context_value(self, variable_name: str, default: str = "") -> str:
        try:
            value = BuiltIn().get_variable_value(variable_name)
            return str(value) if value else default
        except Exception:
            return default

    def _get_robot_tags(self) -> List[str]:
        try:
            tags = BuiltIn().get_variable_value("@{TEST TAGS}")
            if tags is None:
                tags = BuiltIn().get_variable_value("${TEST TAGS}") or []
            return self._coerce_tags(tags)
        except Exception:
            return []

    def _coerce_tags(self, tags: Any) -> List[str]:
        if tags is None:
            return []
        if isinstance(tags, str):
            return [tag for tag in re.split(r"[, ]+", tags.strip()) if tag]
        try:
            return [str(tag) for tag in tags if str(tag)]
        except Exception:
            return [str(tags)] if str(tags) else []

    def _derive_test_case_id(self, tags: List[str], test_name: str) -> str:
        for tag in tags:
            if re.match(r"(?i)^(TC|TEST|CASE|ID)[-_]?\d+", tag) or re.match(r"(?i)^TC[_-]?\w+", tag):
                return tag
        return self._sanitize_name(test_name)[:80] or "Unknown"

    def _get_test_case_id(self) -> str:
        tags = self._get_robot_tags()
        test_name = self._get_robot_context_value("${TEST NAME}", "Unknown Robot test")
        return self._derive_test_case_id(tags, test_name)

    def _get_listener_test_identity(self, *args) -> Dict[str, Any]:
        data = args[0] if args else None
        result = args[-1] if args else None
        test_name = (
            getattr(data, "name", None)
            or getattr(result, "name", None)
            or self._get_robot_context_value("${TEST NAME}", "Unknown Robot test")
        )
        longname = getattr(result, "longname", None) or getattr(data, "longname", None) or ""
        suite_name = (
            getattr(getattr(data, "parent", None), "longname", None)
            or getattr(getattr(result, "parent", None), "longname", None)
            or self._get_robot_context_value("${SUITE NAME}", "Unknown Robot suite")
        )
        if longname and (not suite_name or suite_name == "Unknown Robot suite") and str(longname).endswith(str(test_name)):
            suite_name = str(longname)[: -len(str(test_name))].rstrip(".") or suite_name
        tags_value = getattr(data, "tags", None)
        if tags_value is None:
            tags_value = getattr(result, "tags", None)
        tags = self._coerce_tags(tags_value)
        return {
            "test_case_id": self._derive_test_case_id(tags, str(test_name)),
            "test": str(test_name),
            "suite": str(suite_name),
            "tags": tags,
            "tags_csv": ",".join(tags),
        }

    def _get_test_identity(self) -> Dict[str, Any]:
        if self.active_test_identity:
            return dict(self.active_test_identity)
        test_name = self._get_robot_context_value("${TEST NAME}", "Unknown Robot test")
        suite_name = self._get_robot_context_value("${SUITE NAME}", "Unknown Robot suite")
        tags = self._get_robot_tags()
        return {
            "test_case_id": self._derive_test_case_id(tags, test_name),
            "test": test_name,
            "suite": suite_name,
            "tags": tags,
            "tags_csv": ",".join(tags),
        }

    def _allure_results_dir(self) -> str:
        configured = (
            os.getenv("HEALING_ALLURE_RESULTS_DIR")
            or os.getenv("ALLURE_RESULTS_DIR")
            or os.getenv("ALLURE_OUTPUT_PATH")
        )
        if configured:
            out_dir = configured
        else:
            base_out = self._get_output_dir()
            import glob
            # Check if allure results are directly in the output dir
            if glob.glob(os.path.join(base_out, "*-container.json")):
                out_dir = base_out
            else:
                out_dir = os.path.join(base_out, "allure")
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _allure_enhancement_description_html(self) -> str:
        return (
            "<div>"
            "<h3>HealingSelenium library enhancements</h3>"
            "<ul>"
            "<li>When AI healing is enabled, the LLM prompt uses the current DOM locator library and compact current-page HTML.</li>"
            "<li>Previous healing history is supplied to the LLM as evidence, not reused blindly.</li>"
            "<li>AI, snapshot/history, and optional non-AI candidates are scored together against the live DOM.</li>"
            "<li>Non-AI candidates are rejected unless they are unique and match nearby DOM context from the broken locator.</li>"
            "<li>The selected healed locator is persisted with a score report and audit trail.</li>"
            "<li>The healed element bar and score table are attached to the Allure test.</li>"
            "<li>Healing totals, dashboard, and CSV are attached without adding fake test cases.</li>"
            "</ul>"
            "</div>"
        )

    def _annotate_current_allure_test(
        self,
        old_locator: str,
        new_locator: str,
        score_report: Dict[str, Any],
    ):
        if not self.allure_report:
            return
        try:
            from allure_commons._allure import dynamic  # type: ignore
        except Exception as exc:
            print(f"[Healing] Allure dynamic labels skipped: allure_commons not available ({exc})")
            return

        selected = score_report.get("selected") or {}
        try:
            dynamic.tag("self-healed")
            dynamic.tag("healing-upgraded")
            dynamic.label("healing", "true")
            dynamic.label("healing.library", "HealingSelenium")
            dynamic.label("healing.event.count", str(self.healing_event_count))
            dynamic.parameter("healing_event_count", str(self.healing_event_count))
            dynamic.parameter("healed_from", old_locator)
            dynamic.parameter("healed_to", new_locator)
            dynamic.parameter("healing_score", str(selected.get("score", "")))
            dynamic.parameter("healing_source", str(selected.get("source", "")))
        except Exception as exc:
            print(f"[Healing] Allure dynamic labeling failed: {exc}")

    def _write_healing_environment_summary(self):
        # Allure renders environment values as links in some themes/builds. Do
        # not publish HealingSelenium data there; keep it in real attachments.
        try:
            env_path = os.path.join(self._allure_results_dir(), "environment.properties")
            if not os.path.exists(env_path):
                return
            existing: Dict[str, str] = {}
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "=" in line and not line.lstrip().startswith("#"):
                        k, v = line.rstrip("\n").split("=", 1)
                        if not k.startswith("HealingSelenium."):
                            existing[k] = v
            with open(env_path, "w", encoding="utf-8") as f:
                for key in sorted(existing):
                    f.write(f"{key}={existing[key]}\n")
        except Exception as exc:
            print(f"[Healing] Allure environment cleanup failed: {exc}")

    def _record_healing_event_summary(
        self,
        old_locator: str,
        new_locator: str,
        score_report: Dict[str, Any],
        rewrite_stats: Optional[Dict[str, Any]],
    ):
        selected = score_report.get("selected") or {}
        identity = self._get_test_identity()
        self.healed_test_ids.add(identity["test_case_id"])
        changed = int((rewrite_stats or {}).get("files_changed", 0))
        dry_run = bool((rewrite_stats or {}).get("dry_run", False))
        initial_status = "FAIL" if (self.fail_on_heal or (self.fail_after_rewrite and changed and not dry_run)) else "PENDING"
        
        write_status = "NOT_WRITTEN"
        if changed > 0:
            write_status = "DRY_RUN" if dry_run else "WRITTEN"
        files_written = ", ".join((rewrite_stats or {}).get("written_files_with_lines", []))

        event = {
            "event": self.healing_event_count,
            "ts": self._iso_now(),
            "test_case_id": identity["test_case_id"],
            "test": identity["test"],
            "suite": identity["suite"],
            "tags": identity["tags_csv"],
            "result_status": initial_status,
            "healed": "YES",
            "old_locator": old_locator,
            "new_locator": new_locator,
            "score": selected.get("score"),
            "selected_source": selected.get("source"),
            "write_status": write_status,
            "files_written": files_written,
            "candidate_count": score_report.get("candidate_count", 0),
            "ai_candidate_count": score_report.get("ai_candidate_count", 0),
            "history_candidate_count": score_report.get("history_candidate_count", 0),
            "non_ai_candidate_count": score_report.get("non_ai_candidate_count", 0),
            "rewrite_files_changed": (rewrite_stats or {}).get("files_changed", 0),
            "rewrite_occurrences": (rewrite_stats or {}).get("occurrences_replaced", 0),
            "healed_screenshot_path": score_report.get("healed_screenshot_path", ""),
        }
        self.healing_events.append(event)
        print(
            "[Healing] Healed data → "
            f"test_case_id={event['test_case_id']} "
            f"status={event['result_status']} "
            f"write_status={event['write_status']} "
            f"files_written={event['files_written']} "
            f"score={event['score']} "
            f"source={event['selected_source']} "
            f"old={self._short_log_value(old_locator)} "
            f"new={self._short_log_value(new_locator)}"
        )

    def _write_healing_csv(self, csv_path: str):
        fieldnames = [
            "event",
            "ts",
            "test_case_id",
            "suite",
            "test",
            "tags",
            "result_status",
            "healed",
            "old_locator",
            "new_locator",
            "score",
            "selected_source",
            "write_status",
            "files_written",
            "candidate_count",
            "ai_candidate_count",
            "history_candidate_count",
            "non_ai_candidate_count",
            "rewrite_files_changed",
            "rewrite_occurrences",
            "healed_screenshot_path",
        ]
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for event in self.healing_events:
                writer.writerow({key: event.get(key, "") for key in fieldnames})

    def _pct(self, value: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round((value / total) * 100, 2)

    def _write_healing_run_summary(self) -> Dict[str, str]:
        """Write an aggregate run summary without creating Allure test results."""
        if not self.allure_summary:
            return {}
        try:
            out_dir = self._healing_report_dir()
            json_path = os.path.join(out_dir, "healing_run_summary.json")
            html_path = os.path.join(out_dir, "healing_run_summary.html")
            csv_path = os.path.join(out_dir, "healing_data.csv")
            executed = int(self.test_stats.get("executed", 0))
            passed = int(self.test_stats.get("passed", 0))
            failed = int(self.test_stats.get("failed", 0))
            skipped = int(self.test_stats.get("skipped", 0))
            healed = int(self.healing_event_count)
            healed_test_cases = len(self.healed_test_ids)
            chart_total = max(1, passed + failed + skipped)
            pass_pct = self._pct(passed, chart_total)
            fail_pct = self._pct(failed, chart_total)
            skip_pct = self._pct(skipped, chart_total)
            pass_end = pass_pct
            fail_end = pass_end + fail_pct
            skip_end = fail_end + skip_pct
            healed_pct = self._pct(healed_test_cases, chart_total)
            unhealed_count = max(0, executed - healed_test_cases)
            summary = {
                "test_stats": dict(self.test_stats),
                "total_healings": self.healing_event_count,
                "healed_test_cases": healed_test_cases,
                "ai_healing_enabled": self.ai_healing,
                "non_ai_healing_enabled": self.non_ai_healing,
                "library_enhancements": [
                    "DOM locator library + compact current-page HTML in LLM prompt when AI is enabled",
                    "History-aware re-healing",
                    "AI, snapshot/history, and optional non-AI candidates scored together",
                    "Non-AI candidates require unique live DOM match and nearby context verification",
                    "Selected locator persisted with score report and audit trail",
                    "Allure healed-element bar attached to real tests only",
                    "No synthetic Allure test cases emitted",
                ],
                "events": self.healing_events,
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            self._write_healing_csv(csv_path)

            rows = []
            for event in self.healing_events:
                rows.append(
                    "<tr>"
                    f"<td>{self._html_escape(event.get('event'))}</td>"
                    f"<td>{self._html_escape(event.get('test_case_id'))}</td>"
                    f"<td>{self._html_escape(event.get('test'))}</td>"
                    f"<td><span class=\"status {self._html_escape(str(event.get('result_status', '')).lower())}\">{self._html_escape(event.get('result_status'))}</span></td>"
                    f"<td><span class=\"status healed\">{self._html_escape(event.get('healed'))}</span></td>"
                    f"<td class=\"locator\"><span class=\"old\">{self._html_escape(event.get('old_locator'))}</span></td>"
                    f"<td class=\"locator\"><span class=\"new\">{self._html_escape(event.get('new_locator'))}</span></td>"
                    f"<td>{self._html_escape(event.get('score'))}</td>"
                    f"<td>{self._html_escape(event.get('selected_source'))}</td>"
                    f"<td><span class=\"write-status {self._html_escape(str(event.get('write_status', '')).lower())}\">{self._html_escape(event.get('write_status'))}</span></td>"
                    f"<td>{self._html_escape(event.get('files_written'))}</td>"
                    f"<td>{self._html_escape(event.get('candidate_count'))}</td>"
                    f"<td>{self._html_escape(event.get('ai_candidate_count'))}</td>"
                    f"<td>{self._html_escape(event.get('history_candidate_count'))}</td>"
                    f"<td>{self._html_escape(event.get('non_ai_candidate_count'))}</td>"
                    f"<td>{self._html_escape(event.get('rewrite_occurrences'))}</td>"
                    f"<td class=\"locator\">{self._html_escape(event.get('healed_screenshot_path'))}</td>"
                    "</tr>"
                )
            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HealingSelenium - Self-Healing Dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-base: #0b0f19;
      --glass-bg: rgba(255, 255, 255, 0.03);
      --glass-border: rgba(255, 255, 255, 0.08);
      --glow-success: rgba(16, 185, 129, 0.4);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-green: #10b981;
      --accent-red: #ef4444;
      --gradient-brand: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
      --gradient-success: linear-gradient(135deg, #10b981 0%, #059669 100%);
    }}
    body {{
      font-family: 'Outfit', sans-serif;
      background-color: var(--bg-base);
      background-image: 
        radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.15) 0px, transparent 50%),
        radial-gradient(at 100% 0%, rgba(139, 92, 246, 0.15) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.1) 0px, transparent 50%);
      background-attachment: fixed;
      color: var(--text-main);
      margin: 0;
      padding: 40px 20px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .container {{ max-width: 1200px; width: 100%; animation: fadeIn 0.8s ease-out; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(20px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    .bar {{
      border-left: 4px solid var(--accent-green); background: var(--glass-bg); padding: 24px; border-radius: 16px; border: 1px solid var(--glass-border); border-left-width: 4px; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    }}
    .layout {{ display: grid; grid-template-columns: minmax(260px, 1fr) minmax(260px, 1fr) minmax(320px, 1.5fr); gap: 18px; margin-top: 24px; }}
    .cards {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 12px; margin-top: 24px; }}
    .card {{ background: var(--glass-bg); backdrop-filter: blur(12px); border: 1px solid var(--glass-border); border-radius: 16px; padding: 16px; box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2); }}
    .label {{ color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; font-weight: 800; letter-spacing: 0.5px; }}
    .count {{ font-size: 2.2rem; font-weight: 800; color: #fff; margin-top: 4px; }}
    .note {{ color: var(--text-muted); font-size: 0.85rem; }}
    .pie {{ width: 200px; height: 200px; border-radius: 50%; background:
      conic-gradient(#10b981 0 {pass_end}%, #ef4444 {pass_end}% {fail_end}%,
      #f5a623 {fail_end}% {skip_end}%, #1e293b {skip_end}% 100%);
      box-shadow: 0 4px 15px rgba(0,0,0,0.4); }}
    .pie-healing {{ width: 200px; height: 200px; border-radius: 50%; background:
      conic-gradient(#10b981 0 {healed_pct}%, #1e293b {healed_pct}% 100%);
      box-shadow: 0 4px 15px rgba(0,0,0,0.4); position: relative; }}
    .pie-healing::after {{ content: '{healed_pct}%'; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #0b0f19; width: 120px; height: 120px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: 800; color: #10b981; box-shadow: inset 0 2px 8px rgba(0,0,0,0.4); }}
    .legend {{ display: grid; gap: 8px; margin-top: 12px; font-size: 0.9rem; }}
    .swatch {{ display: inline-block; width: 12px; height: 12px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }}
    .bars {{ display: grid; gap: 12px; width: 100%; }}
    .barrow {{ display: grid; grid-template-columns: 100px 1fr 40px; align-items: center; gap: 8px; font-size: 0.9rem; }}
    .track {{ height: 12px; background: rgba(255,255,255,0.05); border-radius: 999px; overflow: hidden; }}
    .fill {{ height: 100%; border-radius: 999px; }}
    .status {{ display: inline-block; border-radius: 999px; padding: 4px 8px; font-size: 11px; font-weight: 800; }}
    .status.pass {{ background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.2); }}
    .status.fail {{ background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.2); }}
    .status.skip {{ background: rgba(245, 166, 35, 0.15); color: #fdbb63; border: 1px solid rgba(245, 166, 35, 0.2); }}
    .status.healed {{ background: rgba(16, 185, 129, 0.2); color: #6ee7b7; font-weight: 900; box-shadow: 0 0 10px rgba(16, 185, 129, 0.3); }}
    .write-status {{ display: inline-block; border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 800; text-transform: uppercase; }}
    .write-status.written {{ background: rgba(59, 130, 246, 0.15); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.2); }}
    .write-status.not_written {{ background: rgba(255,255,255,0.05); color: #94a3b8; }}
    .locator {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }}
    table {{
      width: 100%; border-collapse: separate; border-spacing: 0; background: var(--glass-bg);
      backdrop-filter: blur(12px); border-radius: 16px; overflow: hidden; border: 1px solid var(--glass-border);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3); margin-top: 24px;
    }}
    th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.85rem; vertical-align: top; }}
    th {{ background: rgba(0, 0, 0, 0.2); color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; font-weight: 800; letter-spacing: 0.5px; }}
    tr {{ transition: background 0.2s ease; }}
    tr:hover {{ background: rgba(255, 255, 255, 0.03); }}
    @media (max-width: 820px) {{ .layout {{ grid-template-columns: 1fr; }} .cards {{ grid-template-columns: repeat(2, 1fr); }} }}
  </style>
</head>
<body>
  <div class="container">
    <section class="bar">
      <div class="label" style="font-size: 0.95rem; font-weight: 800; color: var(--accent-green); letter-spacing: 1px; text-transform: uppercase; margin-bottom: 8px;">Self-Healing Execution Impact Dashboard</div>
      <p style="margin: 4px 0; color: #cbd5e1; font-size: 0.95rem; line-height: 1.6;"><strong>Test result accounting:</strong> Passed + Failed + Skipped = Executed. Healing is a separate locator-repair marker on top of a real test case.</p>
      <p class="note" style="margin-top: 4px; color: var(--text-muted); font-size: 0.85rem;">CSV: healing_reports/healing_data.csv</p>
    </section>
    
    <section class="cards">
      <div class="card" style="border-left: 4px solid #607d8b;"><div class="label">Executed</div><div class="count">{self._html_escape(executed)}</div></div>
      <div class="card" style="border-left: 4px solid #10b981;"><div class="label">Passed</div><div class="count" style="color: #10b981;">{self._html_escape(passed)}</div></div>
      <div class="card" style="border-left: 4px solid #ef4444;"><div class="label">Failed</div><div class="count" style="color: #ef4444;">{self._html_escape(failed)}</div></div>
      <div class="card" style="border-left: 4px solid #f5a623;"><div class="label">Skipped</div><div class="count" style="color: #f5a623;">{self._html_escape(skipped)}</div></div>
      <div class="card" style="background: rgba(16, 185, 129, 0.05); border: 1px solid var(--accent-green); border-left: 6px solid var(--accent-green);">
        <div class="label" style="color: #10b981;">Healed test cases</div>
        <div class="count" style="color: #10b981;">{self._html_escape(healed_test_cases)}</div>
        <div class="note" style="color: #10b981; font-size: 0.85rem; margin-top: 4px;">{self._html_escape(healed)} locator heal event(s)</div>
      </div>
    </section>
    
    <section class="layout">
      <div class="card" style="display: flex; flex-direction: column; align-items: center; justify-content: space-between; min-height: 320px;">
        <div class="label" style="align-self: flex-start; margin-bottom: 16px;">Test Result Breakdown</div>
        <div class="pie"></div>
        <div class="legend" style="margin-top: 20px; align-self: flex-start; width: 100%;">
          <div style="display: flex; align-items: center; margin-bottom: 4px;"><span class="swatch" style="background:#10b981"></span>Passed: {self._html_escape(passed)}</div>
          <div style="display: flex; align-items: center; margin-bottom: 4px;"><span class="swatch" style="background:#ef4444"></span>Failed: {self._html_escape(failed)}</div>
          <div style="display: flex; align-items: center;"><span class="swatch" style="background:#f5a623"></span>Skipped: {self._html_escape(skipped)}</div>
        </div>
      </div>
      
      <div class="card" style="display: flex; flex-direction: column; align-items: center; justify-content: space-between; min-height: 320px; background: rgba(16, 185, 129, 0.02); border-color: rgba(16, 185, 129, 0.15);">
        <div class="label" style="align-self: flex-start; margin-bottom: 16px; color: #10b981;">Healing Rescue Rate</div>
        <div class="pie-healing"></div>
        <div class="legend" style="margin-top: 20px; align-self: flex-start; width: 100%;">
          <div style="font-weight: 700; color: #10b981; display: flex; align-items: center; margin-bottom: 4px;"><span class="swatch" style="background:#10b981"></span>Healed: {self._html_escape(healed_test_cases)}</div>
          <div style="display: flex; align-items: center;"><span class="swatch" style="background:#1e293b"></span>Healing not required: {self._html_escape(unhealed_count)}</div>
        </div>
      </div>
      
      <div class="card" style="display: flex; flex-direction: column; justify-content: space-between; min-height: 320px;">
        <div class="label" style="margin-bottom: 16px;">Execution vs Healing Impact</div>
        <div class="bars">
          <div class="barrow"><span>Executed</span><div class="track"><div class="fill" style="width:{self._pct(executed, max(executed, healed, 1))}%;background:#607d8b"></div></div><span>{self._html_escape(executed)}</span></div>
          <div class="barrow"><span>Passed</span><div class="track"><div class="fill" style="width:{self._pct(passed, max(executed, healed, 1))}%;background:#10b981"></div></div><span>{self._html_escape(passed)}</span></div>
          <div class="barrow"><span>Failed</span><div class="track"><div class="fill" style="width:{self._pct(failed, max(executed, healed, 1))}%;background:#ef4444"></div></div><span>{self._html_escape(failed)}</span></div>
          <div class="barrow"><span>Skipped</span><div class="track"><div class="fill" style="width:{self._pct(skipped, max(executed, healed, 1))}%;background:#f5a623"></div></div><span>{self._html_escape(skipped)}</span></div>
          
          <div style="height: 1px; background: rgba(255,255,255,0.08); margin: 8px 0;"></div>
          
          <div class="barrow"><span style="color:#10b981; font-weight:800;">Healed TCs</span><div class="track"><div class="fill" style="width:{self._pct(healed_test_cases, max(executed, healed_test_cases, 1))}%;background:#10b981"></div></div><span style="color:#10b981; font-weight:800;">{self._html_escape(healed_test_cases)}</span></div>
          <div class="barrow"><span style="color:#10b981; font-weight:800;">Heal events</span><div class="track"><div class="fill" style="width:{self._pct(healed, max(executed, healed, 1))}%;background:#10b981"></div></div><span style="color:#10b981; font-weight:800;">{self._html_escape(healed)}</span></div>
        </div>
      </div>
    </section>
    
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Test Case ID</th>
          <th>Test</th>
          <th>Result</th>
          <th>Healed</th>
          <th>Original locator</th>
          <th>Healed locator</th>
          <th>Score</th>
          <th>Selected source</th>
          <th>Write Status</th>
          <th>Files Written (Lines)</th>
          <th>Candidates</th>
          <th>AI</th>
          <th>Snapshot/history</th>
          <th>Non-AI</th>
          <th>Occurrences rewritten</th>
          <th>Highlighted screenshot</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
  </div>
</body>
</html>"""
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            return {"html": html_path, "json": json_path, "csv": csv_path}
        except Exception as exc:
            print(f"[Healing] Healing run summary generation failed: {exc}")
            return {}

    def _publish_healing_report(
        self,
        old_locator: str,
        new_locator: str,
        score_report: Dict[str, Any],
        rewrite_stats: Optional[Dict[str, Any]],
    ):
        try:
            self.healing_event_count += 1
            score_report["healing_event_count"] = self.healing_event_count
            healed_screenshot_path = self._capture_healed_element_screenshot(old_locator, new_locator)
            if healed_screenshot_path:
                score_report["healed_screenshot_path"] = healed_screenshot_path
            html_body = self._build_healing_report_html(old_locator, new_locator, score_report, rewrite_stats)
            paths = self._write_healing_report_files(old_locator, html_body, score_report)
            score_report["healing_report_paths"] = paths
            self._record_healing_event_summary(old_locator, new_locator, score_report, rewrite_stats)
            self._annotate_current_allure_test(old_locator, new_locator, score_report)
            self._attach_healing_report_to_allure(old_locator, html_body, score_report)
            self._write_healing_run_summary()
            self._write_healing_environment_summary()
        except Exception as exc:
            print(f"[Healing] Healing report generation failed: {exc}")

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

    def _ask_ollama_for_new_locator(
        self,
        old_locator: str,
        html: str,
        previous_heal_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        print(f"\n[Healing] Asking Ollama to heal locator: {old_locator}")

        # Collect DOM locator library, hints, and compact current-page HTML.
        dom_lib = self._collect_dom_locator_library()
        hints = self._derive_locator_hints(old_locator)
        lib_json = json.dumps(dom_lib, ensure_ascii=False)
        html_limit = int(os.getenv("HEALING_PROMPT_HTML_MAX_CHARS", "8000"))
        html_excerpt = self._compact_html_for_prompt(html, max_chars=html_limit)
        history_json = json.dumps(previous_heal_context or {}, ensure_ascii=False)[:3500]
        history_prompt = (
            "- Previous healing history for this locator. Use it as evidence, but do not blindly reuse it; "
            "compare it against the current DOM and HTML:\n"
            f"{history_json}\n"
            if previous_heal_context
            else ""
        )

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
            f"{history_prompt}"
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
                "previous_heal_context": previous_heal_context,
            }
        except requests.exceptions.Timeout:
            msg = f"[Healing] ❌ Ollama error: Request to {self.ollama_url} timed out. The model may be too slow or not running."
            print(msg)
            self.last_ollama_error = msg
            return None
        except requests.exceptions.ConnectionError:
            msg = f"[Healing] ❌ Ollama error: Connection refused. Ensure the Ollama server is running at {self.ollama_url}."
            print(msg)
            self.last_ollama_error = msg
            return None
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
        written_files_with_lines: List[str] = []
        for path in sorted(file_list):
            try:
                if os.path.getsize(path) > max_bytes:
                    continue
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                ext = os.path.splitext(path)[1].lower()
                count = 0
                line_numbers = []
                new_lines = []
                
                # Prefer quoted replacements where applicable (preserve quotes)
                def _quoted_sub(m: re.Match) -> str:
                    q = m.group("q") or ""
                    return f"{q}{new_locator}{q}"
                
                for idx, line in enumerate(lines, 1):
                    new_line = line
                    nl, n1 = quoted.subn(_quoted_sub, new_line)
                    n2 = 0
                    if ext in (".robot", ".resource", ".txt"):
                        nl, n2 = ws_bound.subn(new_locator, nl)
                    total_replacements = n1 + n2
                    if total_replacements > 0:
                        count += total_replacements
                        line_numbers.append(idx)
                        # Strip any existing Healed from comment to prevent accumulation/duplication
                        nl_clean = re.sub(r'\s*#\s*Healed\s+from\s+.*$', '', nl, flags=re.I)
                        stripped_line = nl_clean.rstrip("\r\n").rstrip()
                        line_ending = nl_clean[len(nl_clean.rstrip("\r\n")):]
                        comment = f"Healed from '{old_locator}'"
                        if ext in (".robot", ".resource"):
                            new_line = f"{stripped_line}    # {comment}{line_ending}"
                        elif ext in (".py", ".yaml", ".yml", ".txt"):
                            new_line = f"{stripped_line}  # {comment}{line_ending}"
                        else:
                            new_line = nl_clean
                    new_lines.append(new_line)

                if count > 0:
                    text = "".join(new_lines)
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
                    rel_path = os.path.relpath(path, root)
                    changed_files.append({
                        "path": rel_path,
                        "replacements": count,
                        "line_numbers": line_numbers
                    })
                    for lnum in line_numbers:
                        written_files_with_lines.append(f"{rel_path}:{lnum}")
            except Exception:
                continue

        return {
            "root": root,
            "files_changed": files_changed,
            "occurrences_replaced": occurrences_replaced,
            "changed_files": changed_files,
            "written_files_with_lines": written_files_with_lines,
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
            original_error = e
            print(f"[Healing] ⚠️ Failed: {e}")
            if self.capture_on_fail:
                self._capture_page_artifacts("locator_not_found_click", locator)
            if not self.auto_heal:
                raise

        previous_heal_context = self._get_healing_history_context(locator)
        heal_result = self._heal_locator(locator, previous_heal_context=previous_heal_context)
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
            if getattr(self, "last_ollama_error", None):
                try:
                    from robot.api import logger
                    logger.console(self.last_ollama_error)
                except ImportError:
                    pass
                self.last_ollama_error = None
            raise original_error

    def input_text(self, locator: str, text: str):  # noqa: PLR0911
        """Healable version of Input Text"""
        print(f"[Healing] Input Text → {locator}")
        try:
            self._sl.input_text(locator, text)
            return
        except Exception as e:
            original_error = e
            print(f"[Healing] ⚠️ Failed: {e}")
            if self.capture_on_fail:
                self._capture_page_artifacts("locator_not_found_input", locator)
            if not self.auto_heal:
                raise

        previous_heal_context = self._get_healing_history_context(locator)
        heal_result = self._heal_locator(locator, previous_heal_context=previous_heal_context)
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
            if getattr(self, "last_ollama_error", None):
                try:
                    from robot.api import logger
                    logger.console(self.last_ollama_error)
                except ImportError:
                    pass
                self.last_ollama_error = None
            raise original_error

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

    def enable_ai_healing(self):
        """Enable AI/Ollama locator suggestions at runtime."""
        self.ai_healing = True
        print("[Healing] AI-Healing enabled.")

    def disable_ai_healing(self):
        """Disable AI/Ollama locator suggestions at runtime."""
        self.ai_healing = False
        print("[Healing] AI-Healing disabled.")

    def set_ai_healing(self, value: Any):
        """Set AI/Ollama locator suggestions ON/OFF (accepts true/false)."""
        self.ai_healing = self._to_bool(value, self.ai_healing)
        print(f"[Healing] AI-Healing set to: {self.ai_healing}")

    def get_ai_healing_status(self) -> bool:
        """Return current AI-Healing boolean status."""
        return self.ai_healing

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

    def _entry_locator(self, entry: Any) -> Optional[str]:
        if isinstance(entry, dict):
            loc = entry.get("locator")
            return str(loc) if loc else None
        if isinstance(entry, str):
            return entry
        return None

    def _normalize_heal_entry(self, entry: Any, source: str) -> Optional[Dict[str, Any]]:
        loc = self._entry_locator(entry)
        if not loc:
            return None
        typ = entry.get("type") if isinstance(entry, dict) else None
        return {
            "type": typ or self._infer_locator_type(loc),
            "locator": loc,
            "updated_at": entry.get("updated_at") if isinstance(entry, dict) else None,
            "source": source,
            "score": entry.get("score") if isinstance(entry, dict) else None,
        }

    def _get_healing_history_context(self, locator: str) -> Optional[Dict[str, Any]]:
        """
        Build compact previous-heal context from healed_locators.json and the
        append-only history JSONL. This matches either the original broken locator
        key or a locator that was previously produced as a healed value.
        """
        snapshot_matches: List[Dict[str, Any]] = []
        matched_keys = set()
        locator_values = {locator}

        for old, data in self.healed_locators.items():
            current = data.get("current") if isinstance(data, dict) else None
            if current is None and isinstance(data, dict) and "locator" in data:
                current = data
            history = data.get("history", []) if isinstance(data, dict) else []
            values = {old}
            cur_loc = self._entry_locator(current)
            if cur_loc:
                values.add(cur_loc)
            for item in history:
                hist_loc = self._entry_locator(item)
                if hist_loc:
                    values.add(hist_loc)

            if locator in values:
                matched_keys.add(old)
                locator_values.update(values)
                snapshot_matches.append(
                    {
                        "old_locator": old,
                        "current": self._normalize_heal_entry(current, "snapshot-current"),
                        "history": [
                            h for h in (
                                self._normalize_heal_entry(item, "snapshot-history")
                                for item in history[-8:]
                            )
                            if h
                        ],
                    }
                )

        audit_events: List[Dict[str, Any]] = []
        if os.path.exists(self.audit_file):
            try:
                with open(self.audit_file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            event = json.loads(line)
                        except Exception:
                            continue
                        old = event.get("old_locator")
                        new = event.get("new") if isinstance(event.get("new"), dict) else {}
                        new_loc = new.get("locator")
                        if old in matched_keys or old == locator or new_loc in locator_values:
                            audit_events.append(
                                {
                                    "ts": event.get("ts"),
                                    "event": event.get("event"),
                                    "old_locator": old,
                                    "new": self._normalize_heal_entry(new, "audit-new") if new else None,
                                }
                            )
            except Exception:
                audit_events = []

        if not snapshot_matches and not audit_events:
            return None

        context = {
            "requested_locator": locator,
            "snapshot_matches": snapshot_matches[-5:],
            "audit_events": audit_events[-12:],
        }
        print(
            f"[Healing] Previous healing context found: "
            f"{len(context['snapshot_matches'])} snapshot match(es), {len(context['audit_events'])} audit event(s)."
        )
        return context

    def _previous_heal_candidates(self, previous_heal_context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Turn previous healed locators into normal candidates for the scorer."""
        if not getattr(self, "heal_from_history", True):
            return []
        if not previous_heal_context:
            return []

        raw_entries: List[Dict[str, Any]] = []
        for match in previous_heal_context.get("snapshot_matches", []):
            current = match.get("current")
            if current:
                raw_entries.append(current)
            raw_entries.extend(match.get("history", []))
        for event in previous_heal_context.get("audit_events", []):
            new = event.get("new")
            if new:
                raw_entries.append(new)

        candidates: List[Dict[str, Any]] = []
        seen = set()
        for entry in raw_entries:
            loc = entry.get("locator")
            if not loc:
                continue
            key = (entry.get("type"), loc)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "type": entry.get("type") or self._infer_locator_type(loc),
                    "locator": loc,
                    "reason": f"previously healed locator from {entry.get('source')}",
                    "confidence": 0.85,
                    "source": entry.get("source") or "history",
                }
            )
            if len(candidates) >= 6:
                break
        return candidates

    def _xpath_literal(self, value: str) -> str:
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'
        parts = value.split("'")
        return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"

    def _non_ai_candidate(self, locator: str, reason: str, weak: bool = False) -> Dict[str, Any]:
        candidate: Dict[str, Any] = {
            "type": "xpath",
            "locator": locator,
            "reason": reason,
            "source": "non-ai",
        }
        if weak:
            candidate["weak"] = True
        return candidate

    def _parse_broken_locator(self, broken_locator: str) -> Dict[str, Any]:
        """
        Extract tag name, attributes, and position/index from a broken locator.
        """
        raw = str(broken_locator or "").strip()
        lowered = raw.lower()
        info = {"tag": "*", "attrs": {}, "position": None}

        # Handle xpath prefix
        xpath_str = raw
        if lowered.startswith(("xpath=", "xpath:")):
            xpath_str = raw.split("=", 1)[1] if lowered.startswith("xpath=") else raw.split(":", 1)[1]

        # Extract position from xpath: e.g., (//input)[2]
        pos_match = re.search(r"\)\[(\d+)\]$", xpath_str)
        if pos_match:
            info["position"] = int(pos_match.group(1))
            xpath_str = re.sub(r"^\((.*)\)\[\d+\]$", r"\1", xpath_str)

        # Extract tag name: e.g. //input or //div
        tag_match = re.search(r"//([\w:-]+)", xpath_str)
        if tag_match:
            info["tag"] = tag_match.group(1)

        # Extract attributes: e.g., @id='username', @class='foo'
        for match in re.finditer(r"@([\w:-]+)\s*=\s*(['\"])(.*?)\2", xpath_str):
            info["attrs"][match.group(1)] = match.group(3)

        # Also parse simple ID or name formats
        if lowered.startswith(("id=", "id:")):
            val = raw.split("=", 1)[1] if lowered.startswith("id=") else raw.split(":", 1)[1]
            info["attrs"]["id"] = val
        elif lowered.startswith(("name=", "name:")):
            val = raw.split("=", 1)[1] if lowered.startswith("name=") else raw.split(":", 1)[1]
            info["attrs"]["name"] = val

        return info

    def _non_ai_locator_candidates(self, broken_locator: str) -> List[Dict[str, Any]]:
        """
        Generate deterministic, non-LLM candidates using advanced rule-based healing.
        """
        if not getattr(self, "non_ai_healing", False):
            return []

        raw = str(broken_locator or "").strip()
        lowered = raw.lower()
        candidates: List[Dict[str, Any]] = []

        # Feature 5: Learn from past successful heals (fuzzy history lookup)
        if os.path.exists(self.healed_file):
            try:
                with open(self.healed_file, "r", encoding="utf-8") as f:
                    past_heals = json.load(f)
                past_broken_locators = list(past_heals.keys())
                similar_broken = difflib.get_close_matches(broken_locator, past_broken_locators, n=3, cutoff=0.8)
                for sim_broken in similar_broken:
                    entry = past_heals[sim_broken]
                    healed_entry = entry.get("current") or {}
                    healed_loc = healed_entry.get("locator")
                    if healed_loc:
                        candidates.append(
                            self._non_ai_candidate(
                                healed_loc,
                                f"non-AI learned from past heal of '{sim_broken}' -> '{healed_loc}'"
                            )
                        )
            except Exception as exc:
                print(f"[Healing] Failed to load past heals for learning: {exc}")

        # Feature 1: Extract tag names, attributes and position
        info = self._parse_broken_locator(broken_locator)
        target_tag = info["tag"] or "*"
        target_attrs = info["attrs"]

        # Access WebDriver to inspect DOM and generate smart relative/sibling/parent candidates
        drv = getattr(self._sl, "driver", None)
        if drv:
            try:
                # Derive hints to filter the query
                hints = self._derive_locator_hints(broken_locator)
                meaningful_hints = [h for h in hints if len(h) >= 2 and not h.isdigit()]
                
                # Build an xpath that targets elements having similarity in id, name, class, placeholder, or text
                conditions = []
                for hint in meaningful_hints[:3]: # keep to first 3 most relevant hints to avoid massive query
                    literal = self._xpath_literal(hint)
                    conditions.append(f"contains(@id, {literal})")
                    conditions.append(f"contains(@name, {literal})")
                    conditions.append(f"contains(@class, {literal})")
                    conditions.append(f"contains(@placeholder, {literal})")
                    conditions.append(f"contains(text(), {literal})")

                tag_expr = target_tag if target_tag != "*" else "*"
                found_elems = []
                
                if conditions:
                    query = f"//{tag_expr}[{' or '.join(conditions)}]"
                    found_elems = drv.find_elements("xpath", query)
                
                # Fallback to general elements if no hits or no conditions
                if not found_elems:
                    fallback_expr = target_tag if target_tag != "*" else "input | //button | //a | //select"
                    found_elems = drv.find_elements("xpath", f"//{fallback_expr}")

                # Keep up to 10 candidate elements to process
                for el in found_elems[:10]:
                    el_id = el.get_attribute("id")
                    el_name = el.get_attribute("name")
                    el_class = el.get_attribute("class")
                    el_text = (el.text or "").strip()
                    el_tag = el.tag_name.lower()

                    # Feature 2: Generate candidates using id, text and classes
                    if el_id:
                        candidates.append(self._non_ai_candidate(f"id={el_id}", f"non-AI ID candidate: id={el_id}"))
                    if el_name:
                        candidates.append(self._non_ai_candidate(f"name={el_name}", f"non-AI name candidate: name={el_name}"))
                    if el_text and len(el_text) < 50:
                        candidates.append(self._non_ai_candidate(f"xpath=//{el_tag}[text()={self._xpath_literal(el_text)}]", f"non-AI text candidate: text={el_text}"))
                        candidates.append(self._non_ai_candidate(f"xpath=//{el_tag}[contains(text(),{self._xpath_literal(el_text)})]", f"non-AI text contains candidate"))
                    if el_class:
                        for cls in el_class.split():
                            if cls and not cls.isdigit():
                                candidates.append(self._non_ai_candidate(f"css={el_tag}.{cls}", f"non-AI class candidate: css={el_tag}.{cls}"))

                    # Feature 3: Generate locators from stable parent nodes
                    try:
                        parent = el.find_element("xpath", "./parent::*")
                        parent_id = parent.get_attribute("id")
                        parent_tag = parent.tag_name.lower()
                        if parent_id:
                            candidates.append(
                                self._non_ai_candidate(
                                    f"xpath=//{parent_tag}[@id='{parent_id}']//{el_tag}",
                                    f"non-AI stable parent candidate: parent id={parent_id}"
                                )
                            )
                    except Exception:
                        pass

                    # Feature 4: Generate sibling and relative locators
                    try:
                        # Preceding sibling label (very common for form inputs)
                        labels = el.find_elements("xpath", "./preceding-sibling::label")
                        for label in labels[:2]:
                            lbl_text = (label.text or "").strip()
                            if lbl_text:
                                candidates.append(
                                    self._non_ai_candidate(
                                        f"xpath=//label[contains(text(),{self._xpath_literal(lbl_text)})]/following-sibling::{el_tag}",
                                        f"non-AI sibling label candidate: label={lbl_text}"
                                    )
                                )
                    except Exception:
                        pass

            except Exception as exc:
                print(f"[Healing] Non-AI DOM query failed: {exc}")

        # Fallbacks (legacy rules)
        def add_contains(attr: str, value: str, reason: str):
            if value:
                candidates.append(
                    self._non_ai_candidate(
                        f"xpath=//*[contains(@{attr},{self._xpath_literal(value)})]",
                        reason,
                    )
                )

        def add_starts(attr: str, value: str, reason: str):
            prefix = value[:4]
            if prefix:
                candidates.append(
                    self._non_ai_candidate(
                        f"xpath=//*[starts-with(@{attr},{self._xpath_literal(prefix)})]",
                        reason,
                    )
                )

        if lowered.startswith(("id=", "id:")):
            value = raw.split("=", 1)[1] if raw.lower().startswith("id=") else raw.split(":", 1)[1]
            add_contains("id", value, "non-AI id contains rule")
            add_starts("id", value, "non-AI id prefix rule")
        elif lowered.startswith(("name=", "name:")):
            value = raw.split("=", 1)[1] if raw.lower().startswith("name=") else raw.split(":", 1)[1]
            add_contains("name", value, "non-AI name contains rule")
            add_starts("name", value, "non-AI name prefix rule")
        elif lowered.startswith(("css=", "css:")):
            value = raw.split("=", 1)[1] if lowered.startswith("css=") else raw.split(":", 1)[1]
            m_id = re.search(r"#([\w-]+)", value)
            if m_id:
                add_contains("id", m_id.group(1), "non-AI CSS id fragment rule")
            m_name = re.search(r"\[name=['\"]?([\w-]+)['\"]?\]", value)
            if m_name:
                add_contains("name", m_name.group(1), "non-AI CSS name fragment rule")
        elif lowered.startswith(("xpath=", "xpath:")) or raw.startswith(("/", "(", ".//", "//")):
            for attr in ("id", "name"):
                for match in re.finditer(rf"@{attr}\s*=\s*(['\"])(.*?)\1", raw):
                    value = match.group(2)
                    add_contains(attr, value, f"non-AI XPath {attr} contains rule")
                    add_starts(attr, value, f"non-AI XPath {attr} prefix rule")

        # Position suffix if extracted
        if info["position"]:
            pos = info["position"]
            pos_candidates = []
            for c in candidates:
                loc = c.get("locator")
                if loc and loc.startswith("xpath="):
                    xpath_part = loc.split("=", 1)[1]
                    pos_candidates.append(
                        self._non_ai_candidate(
                            f"xpath=({xpath_part})[{pos}]",
                            f"{c.get('reason')} (position {pos})"
                        )
                    )
            candidates.extend(pos_candidates)

        candidates.append(
            self._non_ai_candidate(
                "xpath=//button | //a | //span",
                "non-AI generic clickable/text fallback",
                weak=True,
            )
        )

        out: List[Dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            key = (candidate.get("type"), candidate.get("locator"))
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= 15:
                break
        return out

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
        if suggestion.get("source"):
            new_entry["source"] = suggestion.get("source")
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

    def _heal_locator(
        self,
        locator: str,
        previous_heal_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:  # noqa: PLR0912
        """
        Attempt to heal given locator using current page HTML via Ollama.
        On success, persists with timestamp/history and returns a dict:
        { 'entry': <current_healed_entry>, 'rewrite_stats': <dict or None>, 'score_report': <dict> }
        """
        driver = getattr(self._sl, "driver", None)
        if not driver:
            print("[Healing] ❌ No Selenium driver found.")
            return None

        ai_result = None
        if self.ai_healing:
            ai_result = self._ask_ollama_for_new_locator(
                locator,
                driver.page_source,
                previous_heal_context=previous_heal_context,
            )
        else:
            print("[Healing] AI healing disabled; skipping Ollama candidates.")
        ai_candidates = ai_result.get("candidates", []) if ai_result else []
        history_candidates = self._previous_heal_candidates(previous_heal_context)
        non_ai_candidates = self._non_ai_locator_candidates(locator)

        candidates: List[Dict[str, Any]] = []
        seen = set()
        for candidate in ai_candidates + history_candidates + non_ai_candidates:
            key = (candidate.get("type"), candidate.get("locator"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

        if candidates:
            dom_lib = ai_result.get("dom_library", {}) if ai_result else self._collect_dom_locator_library()
            html_chars_used = int(ai_result.get("html_chars_used", 0)) if ai_result else 0
            score_report = self._score_locator_candidates(
                locator,
                candidates,
                dom_lib,
                html_chars_used,
            )
            score_report["ai_candidate_count"] = len(ai_candidates)
            score_report["ai_enabled"] = self.ai_healing
            score_report["history_candidate_count"] = len(history_candidates)
            score_report["non_ai_candidate_count"] = len(non_ai_candidates)
            score_report["non_ai_enabled"] = self.non_ai_healing
            if previous_heal_context:
                score_report["previous_heal_context"] = previous_heal_context
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
                "source": selected.get("source"),
            }
            rewrite_stats = self._record_healing(locator, suggestion, score_report=score_report)
            self._publish_healing_report(
                locator,
                suggestion["locator"],
                score_report,
                rewrite_stats,
            )
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
                f"from {score_report.get('candidate_count')} candidate(s) "
                f"({score_report.get('ai_candidate_count', 0)} AI, "
                f"{score_report.get('history_candidate_count', 0)} snapshot/history, "
                f"{score_report.get('non_ai_candidate_count', 0)} non-AI)."
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
            detailed_msg = "\n".join(reason_parts + [
                "Failing the test intentionally so the incorrect coded locator is fixed and committed."
            ])
            short_msg = f"Auto-healed '{old_locator}' → '{new_locator}'. Failing intentionally to enforce code fix."
            try:
                from robot.libraries.BuiltIn import BuiltIn
                BuiltIn().log(detailed_msg, "INFO")
                BuiltIn().fail(short_msg)
            except Exception as exc:
                # In case we're not under Robot execution context
                raise AssertionError(short_msg) from exc
# End of class
