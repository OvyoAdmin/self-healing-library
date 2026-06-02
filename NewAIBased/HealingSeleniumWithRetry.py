# HealingSelenium.py
# Self-healing Selenium for Robot Framework:
# - Local (no-LLM) robust selector generation (anchors, labels, text, stable attrs)
# - Validation across top document, iframes (recursive), and open Shadow DOM (CSS)
# - Context-aware actions (frame hop, JS for shadow)
# - LLM fallback with multi-candidate suggestions, stochastic sampling & dedupe
# - History/audit, optional auto-rewrite, invalid session guards, diagnostics

import os
import re
import glob
import json
import time
import shutil
import fnmatch
import inspect
import requests
from datetime import datetime

# Robot & SeleniumLibrary (Robot Framework)
from robot.libraries.BuiltIn import BuiltIn
from SeleniumLibrary import SeleniumLibrary as _SLib


class HealingSelenium:
    """
    ▶ Capabilities
      • Drop-in wrapper around SeleniumLibrary (use all its keywords via this lib)
      • Healing-aware overrides: Click Element, Input Text (context-aware)
      • Local (no-LLM) robust selector synthesis from DOM
      • Cross-context validation: top doc, iframes (recursive), open Shadow DOM (CSS)
      • Context-aware click/input (auto frame switch, JS for shadow)
      • LLM fallback for multi-candidate suggestions with diversity + de-dupe
      • History snapshot (healed_locators.json) + append-only audit log
      • Optional source auto-rewrite (OFF by default)
      • Diagnostics: Why Locator Not Matching

    Environment variables / Library args (defaults in parentheses):

      OLLAMA_BASE_URL (http://localhost:11434)      OLLAMA_MODEL (llama3)
      HEALING_AUTOHEAL (true)                        HEALING_AUTOREWRITE (false)

      # Validation & context search
      HEALING_REQUIRE_VISIBLE (false)                HEALING_REQUIRE_UNIQUE (false)
      HEALING_VALIDATE_RETRIES (1)                   HEALING_VALIDATE_RETRY_INTERVAL_MS (150)
      HEALING_VALIDATE_WAIT_SECS (0.0)
      HEALING_SEARCH_IFRAMES (true)                  HEALING_IFRAME_DEPTH (2)
      HEALING_SEARCH_SHADOW_DOM (true)

      # Heal retries (distinct LLM attempts) & multi-candidate size
      HEALING_HEAL_ATTEMPTS (3)                      HEALING_LLM_TOPK (5)

      # LLM sampling to avoid repeats
      HEALING_HEAL_TEMP_START (0.3)                  HEALING_HEAL_TEMP_STEP (0.2)
      HEALING_HEAL_TOP_P (0.95)                      HEALING_RANDOMIZE_SEED (true)
      HEALING_HEAL_SPIN_LIMIT (5)

      # Auto-reopen on invalid session (optional)
      HEALING_AUTO_REOPEN_SESSION (false)
      HEALING_DEFAULT_BROWSER (chrome)               HEALING_DEFAULT_URL (about:blank)
    """

    # -------------------------
    # Initialization
    # -------------------------
    def __init__(
        self,
        model=None,
        base_url=None,
        healed_file="healed_locators.json",
        auto_heal=None,
        auto_rewrite=None,

        # Validation knobs
        validate_retries=None,
        validate_retry_interval_ms=None,
        validate_wait_secs=None,
        require_visible=None,
        require_unique=None,

        # Cross-context
        search_iframes=None,
        iframe_depth=None,
        search_shadow_dom=None,

        # Heal retries & LLM
        heal_retries=None,
        heal_attempts=None,     # alias for heal_retries
        heal_topk=None,

        # Sampling knobs
        heal_temp_start=None,
        heal_temp_step=None,
        heal_top_p=None,
        randomize_seed=None,
        heal_spin_limit=None,

        # SeleniumLibrary options
        sl_timeout=None,
        sl_implicit_wait=None,
        sl_run_on_failure=None
    ):
        # Ollama
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3")
        self.base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.ollama_url = f"{self.base_url.rstrip('/')}/api/generate"

        # Files
        self.healed_file = healed_file
        root, _ = os.path.splitext(self.healed_file)
        self.audit_file = f"{root}_history.jsonl"

        # Switches
        self.auto_heal = self._arg_or_env_bool(auto_heal, "HEALING_AUTOHEAL", True)
        self.auto_rewrite = self._arg_or_env_bool(auto_rewrite, "HEALING_AUTOREWRITE", False)

        # Validation
        self.require_visible = self._arg_or_env_bool(require_visible, "HEALING_REQUIRE_VISIBLE", False)
        self.require_unique  = self._arg_or_env_bool(require_unique,  "HEALING_REQUIRE_UNIQUE",  False)
        self.validate_retries = int(validate_retries) if validate_retries is not None else int(os.getenv("HEALING_VALIDATE_RETRIES", "1"))
        self.validate_retry_interval_ms = int(validate_retry_interval_ms) if validate_retry_interval_ms is not None else int(os.getenv("HEALING_VALIDATE_RETRY_INTERVAL_MS", "150"))
        self.validate_wait_secs = float(validate_wait_secs) if validate_wait_secs is not None else float(os.getenv("HEALING_VALIDATE_WAIT_SECS", "0"))

        # Cross-context
        self.search_iframes = self._arg_or_env_bool(search_iframes, "HEALING_SEARCH_IFRAMES", True)
        self.iframe_depth = int(iframe_depth) if iframe_depth is not None else int(os.getenv("HEALING_IFRAME_DEPTH", "2"))
        self.search_shadow_dom = self._arg_or_env_bool(search_shadow_dom, "HEALING_SEARCH_SHADOW_DOM", True)

        # Heal retries & LLM top-K
        header_heal = heal_retries if heal_retries is not None else heal_attempts
        self.heal_retries = max(1, int(header_heal)) if header_heal is not None else max(1, int(os.getenv("HEALING_HEAL_ATTEMPTS", "3")))
        self.heal_topk = int(heal_topk) if heal_topk is not None else int(os.getenv("HEALING_LLM_TOPK", "5"))

        # LLM sampling
        self.heal_temp_start = float(heal_temp_start) if heal_temp_start is not None else float(os.getenv("HEALING_HEAL_TEMP_START", "0.3"))
        self.heal_temp_step  = float(heal_temp_step)  if heal_temp_step  is not None else float(os.getenv("HEALING_HEAL_TEMP_STEP",  "0.2"))
        self.heal_top_p      = float(heal_top_p)      if heal_top_p      is not None else float(os.getenv("HEALING_HEAL_TOP_P",      "0.95"))
        self.randomize_seed  = self._arg_or_env_bool(randomize_seed, "HEALING_RANDOMIZE_SEED", True)
        self.heal_spin_limit = int(heal_spin_limit)   if heal_spin_limit is not None else int(os.getenv("HEALING_HEAL_SPIN_LIMIT", "5"))

        # Auto-reopen session
        self.auto_reopen_session = self._arg_or_env_bool(None, "HEALING_AUTO_REOPEN_SESSION", False)
        self.default_browser = os.getenv("HEALING_DEFAULT_BROWSER", "chrome")
        self.default_url = os.getenv("HEALING_DEFAULT_URL", "about:blank")

        # Internal SeleniumLibrary (NOT registered with Robot)
        sl_kwargs = {}
        if sl_timeout is not None: sl_kwargs["timeout"] = sl_timeout
        if sl_implicit_wait is not None: sl_kwargs["implicit_wait"] = sl_implicit_wait
        if sl_run_on_failure is not None: sl_kwargs["run_on_failure"] = sl_run_on_failure
        self._sl = _SLib(**sl_kwargs)

        # State
        self.healed_locators = self._load_healed_locators()

        # Keywords
        self._init_custom_keyword_map()

        print(f"\n[HealingSelenium] ✅ Loaded")
        print(f"[HealingSelenium] Model: {self.model} @ {self.ollama_url}")
        print(
            f"[HealingSelenium] Auto-Heal={self.auto_heal} | Auto-Rewrite={self.auto_rewrite}\n"
            f"[HealingSelenium] Validate: visible={self.require_visible}, unique={self.require_unique}, "
            f"retries={self.validate_retries}, interval={self.validate_retry_interval_ms}ms, wait={self.validate_wait_secs}s\n"
            f"[HealingSelenium] Context: iframes={self.search_iframes} (depth={self.iframe_depth}), shadow={self.search_shadow_dom}\n"
            f"[HealingSelenium] LLM: attempts={self.heal_retries}, topK={self.heal_topk}, spins={self.heal_spin_limit}, "
            f"temp_start={self.heal_temp_start}, temp_step={self.heal_temp_step}, top_p={self.heal_top_p}, seedRand={self.randomize_seed}\n"
            f"[HealingSelenium] Auto-reopen session={self.auto_reopen_session} ({self.default_browser} @ {self.default_url})\n"
        )

    # -------------------------
    # Dynamic Library Interface
    # -------------------------
    def _init_custom_keyword_map(self):
        self._custom_kw = {
            "Click Element": self.click_element,
            "Input Text": self.input_text,

            "Test Ollama Connection": self.test_ollama_connection,
            "Highlight Healings": self.highlight_healings,
            "Validate Locator": self.validate_locator,
            "Why Locator Not Matching": self.why_locator_not_matching,

            "Enable Auto Healing": self.enable_auto_healing,
            "Disable Auto Healing": self.disable_auto_healing,
            "Set Auto Healing": self.set_auto_healing,
            "Get Auto Healing Status": self.get_auto_healing_status,
            "Enable Auto Rewrite": self.enable_auto_rewrite,
            "Disable Auto Rewrite": self.disable_auto_rewrite,
            "Set Auto Rewrite": self.set_auto_rewrite,
            "Get Auto Rewrite Status": self.get_auto_rewrite_status,
        }
        self._custom_norm = {self._norm(n): n for n in self._custom_kw.keys()}

    def get_keyword_names(self):
        try:
            sl_names = set(self._sl.get_keyword_names())
        except Exception:
            sl_names = set()
        sl_filtered = [n for n in sl_names if self._norm(n) not in self._custom_norm]
        names = set(sl_filtered) | set(self._custom_kw.keys())
        return sorted(names)

    def run_keyword(self, name, args, kwargs=None):
        if kwargs is None: kwargs = {}
        norm = self._norm(name)
        if norm in self._custom_norm:
            real = self._custom_norm[norm]
            return self._custom_kwreal
        return self._sl.run_keyword(name, args, kwargs)

    def get_keyword_documentation(self, name):
        norm = self._norm(name)
        if norm in self._custom_norm:
            real = self._custom_norm[norm]
            return inspect.getdoc(self._custom_kw[real]) or real
        try:
            return self._sl.get_keyword_documentation(name)
        except Exception:
            func = getattr(self._sl, self._kw_to_method(name), None)
            return inspect.getdoc(func) or name

    def get_keyword_arguments(self, name):
        norm = self._norm(name)
        if norm in self._custom_norm:
            real = self._custom_norm[norm]
            func = self._custom_kw[real]
            try:
                sig = inspect.signature(func)
                out = []
                for p in sig.parameters.values():
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty: out.append(p.name)
                    elif p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD): out.append(f"{p.name}={repr(p.default)}")
                    elif p.kind == p.VAR_POSITIONAL: out.append("*" + p.name)
                    elif p.kind == p.VAR_KEYWORD: out.append("**" + p.name)
                return out
            except Exception:
                return []
        try:
            return self._sl.get_keyword_arguments(name)
        except Exception:
            func = getattr(self._sl, self._kw_to_method(name), None)
            if not func: return []
            try:
                sig = inspect.signature(func)
                out = []
                for p in sig.parameters.values():
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty: out.append(p.name)
                    elif p.kind in (p.POSITIONAL_ONLY, p.POSITIONIONAL_OR_KEYWORD): out.append(f"{p.name}={repr(p.default)}")
                    elif p.kind == p.VAR_POSITIONAL: out.append("*" + p.name)
                    elif p.kind == p.VAR_KEYWORD: out.append("**" + p.name)
                return out
            except Exception:
                return []

    # -------------------------
    # Utilities & Infra
    # -------------------------
    def _norm(self, name: str) -> str:
        return "".join(ch for ch in name.lower() if ch not in " _")

    def _kw_to_method(self, name: str) -> str:
        return name.strip().lower().replace(" ", "_")

    def _to_bool(self, v, default=False):
        if isinstance(v, bool): return v
        if v is None: return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def _arg_or_env_bool(self, arg_value, env_name, default):
        if arg_value is not None: return self._to_bool(arg_value, default)
        return self._to_bool(os.getenv(env_name), default)

    def _iso_now(self):
        return datetime.now().isoformat()

    # -------------------------
    # Invalid session helpers
    # -------------------------
    def _is_invalid_session_error(self, e) -> bool:
        msg = (str(e) or "").lower()
        return "invalid session id" in msg or "invalidsessionid" in msg

    def _session_alive(self) -> bool:
        try:
            _ = self._sl.get_window_titles()
            return True
        except Exception as ex:
            if self._is_invalid_session_error(ex): return False
            return True

    def _fail_for_dead_session(self, where: str):
        print(f"[Healing] ❌ WebDriver session invalid at: {where}")
        if self.auto_reopen_session:
            try:
                print(f"[Healing] 🔄 Auto-reopen → {self.default_browser} @ {self.default_url}")
                self._sl.open_browser(self.default_url, self.default_browser)
                print("[Healing] ✅ New session opened. Re-run action.")
                return
            except Exception as rex:
                print(f"[Healing] ❌ Auto-reopen failed: {rex}")
        raise AssertionError(
            "[Healing] Invalid WebDriver session. Fix: re-open browser; avoid mixing SeleniumLibrary instances; "
            "check teardown/Grid stability."
        )

    # -------------------------
    # Normalization
    # -------------------------
    def _normalize_locator(self, typ: str, loc: str) -> str:
        if not loc: return loc
        s = str(loc).strip()
        if not s: return s
        lowered = s.lower()
        if lowered.startswith(("xpath=", "xpath:", "css=", "css:", "id=", "name=")):
            return s
        if not typ or typ not in ("xpath", "css", "id", "name"):
            if s.startswith(("/", "(", ".//", "//")):
                typ = "xpath"
            elif s.startswith(("css=", "css:")):
                return s
            elif s.startswith(("#", ".", "[")):
                typ = "css"
            elif re.match(r"^[A-Za-z_][\w\-\:\.]*$", s) and " " not in s:
                typ = "id"
            else:
                typ = "xpath"
        if typ == "css":
            if s.startswith(("#", ".", "[")) or any(ch in s for ch in (">", "+", "~", " ")):
                return f"css:{s}"
            return f"css={s}"
        return f"{typ}={s}"

    def _canon_selector_key(self, typ: str, loc: str) -> str:
        norm = (self._normalize_locator(typ, loc) or "").strip()
        return norm.lower().replace("css=", "css:")

    def _strip_strategy_prefix(self, s: str) -> str:
        if not s: return s
        low = s.lower()
        for p in ("xpath=", "xpath:", "css=", "css:", "id=", "name="):
            if low.startswith(p): return s[len(p):].strip()
        return s.strip()

    # -------------------------
    # Cross-context validation & context capture
    # -------------------------
    def _locator_exists_with_context(self, entry: dict):
        """
        Return (ok, normalized, count, context)
        context may include: {'frame_path': [indices...]} or {'shadow_css': '<css>'}
        """
        ok, sl_loc, count = self._locator_exists_in_dom(entry)
        context = None
        driver = getattr(self._sl, "driver", None)
        if not driver:
            return (False, sl_loc, 0, None)

        if not ok:
            # Shadow (CSS only)
            if self.search_shadow_dom and sl_loc.lower().startswith(("css=", "css:")):
                css_raw = self._strip_strategy_prefix(sl_loc)
                if self._deep_exists_shadow(driver, css_raw):
                    return (True, sl_loc, 1, {"shadow_css": css_raw})
            # Iframes
            if self.search_iframes and self.iframe_depth > 0:
                fp = self._find_first_iframe_path(driver, sl_loc, self.iframe_depth)
                if fp is not None:
                    return (True, sl_loc, 1, {"frame_path": fp})

        return (ok, sl_loc, count, context)

    def _locator_exists_in_dom(
        self,
        entry: dict,
        require_visible: bool = False,
        unique: bool = False,
        retries: int = 1,
        interval_ms: int = 150,
        search_iframes: bool = None,
        iframe_depth: int = None,
        search_shadow: bool = None,
        wait_secs: float = None,
    ):
        try:
            typ = (entry or {}).get("type")
            loc = (entry or {}).get("locator")
            sl_loc = self._normalize_locator(typ, loc)
            if not sl_loc:
                return (False, sl_loc, 0)

            if search_iframes is None: search_iframes = self.search_iframes
            if iframe_depth is None:  iframe_depth = self.iframe_depth
            if search_shadow is None: search_shadow = self.search_shadow_dom
            if wait_secs is None: wait_secs = self.validate_wait_secs
            if wait_secs and wait_secs > 0: time.sleep(wait_secs)

            driver = getattr(self._sl, "driver", None)
            if not driver: return (False, sl_loc, 0)

            def _vis(arr):
                return [e for e in arr if (not require_visible or e.is_displayed())]

            total = 0
            # Top
            elems = self._sl.get_webelements(sl_loc)
            elems = _vis(elems)
            total += len(elems)
            if (unique and total == 1) or (not unique and total >= 1):
                return (True, sl_loc, total)

            # Shadow (CSS only)
            if search_shadow and sl_loc.lower().startswith(("css=", "css:")):
                css_raw = self._strip_strategy_prefix(sl_loc)
                shadow_count = self._deep_query_selector_all_count(driver, css_raw)
                if require_visible and shadow_count:
                    shadow_count = self._deep_query_visible_count(driver, css_raw)
                total += shadow_count
                if (unique and total == 1) or (not unique and total >= 1):
                    return (True, sl_loc, total)

            # Iframes
            if search_iframes and iframe_depth > 0:
                found = self._search_iframes_for_locator(driver, sl_loc, require_visible, unique, iframe_depth, search_shadow)
                if found > 0:
                    total += found
                    if (unique and total == 1) or (not unique and total >= 1):
                        return (True, sl_loc, total)

            # retries
            last_total = total
            for _ in range(max(1, int(retries)) - 1):
                time.sleep(max(0, interval_ms) / 1000.0)
                total = 0
                elems = self._sl.get_webelements(sl_loc)
                elems = _vis(elems)
                total += len(elems)
                if search_shadow and sl_loc.lower().startswith(("css=", "css:")):
                    css_raw = self._strip_strategy_prefix(sl_loc)
                    shadow_count = self._deep_query_selector_all_count(driver, css_raw)
                    if require_visible and shadow_count:
                        shadow_count = self._deep_query_visible_count(driver, css_raw)
                    total += shadow_count
                if search_iframes and iframe_depth > 0:
                    found = self._search_iframes_for_locator(driver, sl_loc, require_visible, unique, iframe_depth, search_shadow)
                    total += found
                if (unique and total == 1) or (not unique and total >= 1):
                    return (True, sl_loc, total)
                last_total = total

            return (False, sl_loc, last_total)
        except Exception:
            return (False, str((entry or {}).get("locator")), 0)

    # Shadow existence & deep search helpers
    def _deep_exists_shadow(self, driver, css_selector: str) -> bool:
        js = r"""
        const sel = arguments[0];
        function queryFirstDeep(root) {
          try { const m = root.querySelector(sel); if (m) return m; } catch(e){}
          const all = root.querySelectorAll('*');
          for (const el of all) {
            if (el.shadowRoot) {
              try { const m2 = el.shadowRoot.querySelector(sel); if (m2) return m2; } catch(e){}
              const deeper = queryFirstDeep(el.shadowRoot);
              if (deeper) return deeper;
            }
          }
          return null;
        }
        return !!queryFirstDeep(document);
        """
        try:
            return bool(driver.execute_script(js, css_selector))
        except Exception:
            return False

    def _deep_query_selector_all_count(self, driver, css_selector: str) -> int:
        js = r"""
        const sel = arguments[0];
        function queryAllDeep(root) {
          const out = [];
          try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
          const all = root.querySelectorAll('*');
          for (const el of all) {
            if (el.shadowRoot) {
              try { out.push(...el.shadowRoot.querySelectorAll(sel)); } catch (e) {}
              out.push(...queryAllDeep(el.shadowRoot));
            }
          }
          return out;
        }
        return queryAllDeep(document).length;
        """
        try:
            return int(driver.execute_script(js, css_selector) or 0)
        except Exception:
            return 0

    def _deep_query_visible_count(self, driver, css_selector: str) -> int:
        js = r"""
        const sel = arguments[0];
        function isVisible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none';
        }
        function queryAllDeepVisible(root) {
          let out = [];
          try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
          out = out.filter(isVisible);
          const all = root.querySelectorAll('*');
          for (const el of all) {
            if (el.shadowRoot) {
              let arr = [];
              try { arr.push(...el.shadowRoot.querySelectorAll(sel)); } catch (e) {}
              arr = arr.filter(isVisible);
              out.push(...arr);
              out.push(...queryAllDeepVisible(el.shadowRoot));
            }
          }
          return out;
        }
        return queryAllDeepVisible(document).length;
        """
        try:
            return int(driver.execute_script(js, css_selector) or 0)
        except Exception:
            return 0

    def _search_iframes_for_locator(self, driver, sl_loc: str, require_visible: bool, unique: bool,
                                    max_depth: int, search_shadow: bool, _depth: int = 1) -> int:
        count = 0
        try:
            frames = driver.find_elements("tag name", "iframe") + driver.find_elements("tag name", "frame")
        except Exception:
            frames = []
        for fr in frames:
            try:
                driver.switch_to.frame(fr)
                elems = self._sl.get_webelements(sl_loc)
                if require_visible:
                    elems = [e for e in elems if e.is_displayed()]
                count_here = len(elems)
                count += count_here
                if search_shadow and sl_loc.lower().startswith(("css=", "css:")):
                    css_raw = self._strip_strategy_prefix(sl_loc)
                    s_count = self._deep_query_selector_all_count(driver, css_raw)
                    if require_visible and s_count:
                        s_count = self._deep_query_visible_count(driver, css_raw)
                    count += s_count
                if _depth < max_depth and count == 0:
                    count += self._search_iframes_for_locator(
                        driver, sl_loc, require_visible, unique, max_depth, search_shadow, _depth=_depth + 1
                    )
            except Exception:
                pass
            finally:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
            if unique and count > 1: break
        return count

    def _find_first_iframe_path(self, driver, sl_loc: str, max_depth: int, _path=None):
        if _path is None: _path = []
        try:
            if self._sl.get_webelements(sl_loc):
                return list(_path)
        except Exception:
            pass
        try:
            frames = driver.find_elements("tag name", "iframe") + driver.find_elements("tag name", "frame")
        except Exception:
            frames = []
        for idx, fr in enumerate(frames):
            try:
                driver.switch_to.frame(fr)
                if self._sl.get_webelements(sl_loc):
                    driver.switch_to.parent_frame()
                    return _path + [idx]
                if len(_path) + 1 < max_depth:
                    sub = self._find_first_iframe_path(driver, sl_loc, max_depth, _path + [idx])
                    if sub is not None:
                        driver.switch_to.parent_frame()
                        return sub
                driver.switch_to.parent_frame()
            except Exception:
                try: driver.switch_to.default_content()
                except Exception: pass
                continue
        return None

    # -------------------------
    # Persistence (snapshot + audit)
    # -------------------------
    def _load_healed_locators(self):
        if os.path.exists(self.healed_file):
            try:
                with open(self.healed_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                upgraded = {}
                for old, val in data.items():
                    if isinstance(val, dict) and "current" in val and "history" in val:
                        upgraded[old] = val
                    elif isinstance(val, dict) and "locator" in val:
                        upgraded[old] = {
                            "current": {"type": val.get("type"), "locator": val.get("locator"), "updated_at": val.get("updated_at")},
                            "history": []
                        }
                    else:
                        upgraded[old] = {"current": {"type": None, "locator": val, "updated_at": None}, "history": []}
                return upgraded
            except Exception:
                return {}
        return {}

    def _write_snapshot(self):
        with open(self.healed_file, "w", encoding="utf-8") as f:
            json.dump(self.healed_locators, f, indent=2, ensure_ascii=False)

    def _append_audit(self, old_locator, new_entry, event_type="heal", extra=None):
        try:
            payload = {
                "ts": self._iso_now(),
                "event": event_type,
                "old_locator": old_locator,
                "model": self.model,
                "source": "ollama"
            }
            if event_type == "heal":
                payload["new"] = {
                    "type": new_entry.get("type") if isinstance(new_entry, dict) else None,
                    "locator": new_entry.get("locator") if isinstance(new_entry, dict) else new_entry,
                    "context": new_entry.get("context") if isinstance(new_entry, dict) else None,
                }
            if extra: payload.update(extra)
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # -------------------------
    # Auto-rewrite (optional)
    # -------------------------
    def _maybe_autorewrite(self, old_locator, new_locator):
        if not new_locator or new_locator == old_locator: return
        try:
            stats = self._apply_source_rewrite(old_locator, new_locator)
            self._append_audit(old_locator, new_locator, event_type="rewrite", extra={"rewrite": stats})
            changed = stats.get("files_changed", 0); total = stats.get("occurrences_replaced", 0)
            if changed:
                print(f"[Healing] 🛠️ Rewrote '{old_locator}' → '{new_locator}' in {changed} file(s), {total} occurrence(s).")
            else:
                print(f"[Healing] 🛠️ No occurrences of '{old_locator}' found.")
        except Exception as e:
            print(f"[Healing] ❌ Auto-rewrite error: {e}")

    def _apply_source_rewrite(self, old_locator, new_locator):
        root = os.getenv("HEALING_REWRITE_ROOT", os.getcwd())
        include_globs = os.getenv("HEALING_REWRITE_GLOBS", "**/*.robot,**/*.resource,**/*.py,**/*.json,**/*.yaml,**/*.yml,**/*.txt")
        exclude_globs = os.getenv("HEALING_REWRITE_EXCLUDE", "healed_locators.json,*_history.jsonl,.git/**,venv/**,.venv/**,node_modules/**,__pycache__/**")
        dry_run = self._to_bool(os.getenv("HEALING_REWRITE_DRY_RUN"), False)
        max_bytes = int(os.getenv("HEALING_REWRITE_MAX_BYTES", "2097152"))

        include_patterns = [p.strip() for p in include_globs.split(',') if p.strip()]
        exclude_patterns = [p.strip() for p in exclude_globs.split(',') if p.strip()]

        candidates = set()
        for pat in include_patterns:
            candidates.update(glob.glob(os.path.join(root, pat), recursive=True))

        def is_excluded(path):
            rel = os.path.relpath(path, root)
            for ep in exclude_patterns:
                if fnmatch.fnmatch(rel, ep) or fnmatch.fnmatch(path, ep): return True
            base = os.path.basename(path)
            if base == os.path.basename(self.healed_file) or base == os.path.basename(self.audit_file): return True
            return False

        candidates = [p for p in candidates if os.path.isfile(p) and not is_excluded(p)]
        quoted = re.compile(r'(?P<q>["\']?)' + re.escape(old_locator) + r'(?P=q)')
        ws_bound = re.compile(r'(?<=\s)' + re.escape(old_locator) + r'(?=\s|\n|$)')

        files_changed=0; occ=0; changed_files=[]
        for path in sorted(candidates):
            try:
                if os.path.getsize(path) > max_bytes: continue
                with open(path, 'r', encoding='utf-8', errors='ignore') as f: text = f.read()
                orig = text; ext = os.path.splitext(path)[1].lower()
                text, n1 = quoted.subn(lambda m: f"{m.group('q')}{new_locator}{m.group('q')}", text); c=n1
                if ext in ('.robot', '.resource', '.txt'):
                    text, n2 = ws_bound.subn(new_locator, text); c += n2
                if c>0 and text != orig:
                    if not dry_run:
                        bk=f"{path}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
                        try: shutil.copy2(path, bk)
                        except Exception: pass
                        with open(path,'w',encoding='utf-8',errors='ignore') as f: f.write(text)
                    files_changed+=1; occ+=c; changed_files.append({"path": os.path.relpath(path, root), "replacements": c})
            except Exception:
                continue
        return {"root": root, "files_changed": files_changed, "occurrences_replaced": occ, "changed_files": changed_files, "dry_run": dry_run}

    # -------------------------
    # Heuristic candidate generation (anchors + labels + tokens)
    # -------------------------
    def _extract_hints_from_locator(self, old_locator: str):
        hints = {"tokens": set(), "tag": None}
        s = (old_locator or "").strip()
        if not s: return {"tokens": [], "tag": None}

        # Tag hint from xpath like //button or //input
        m_tag = re.search(r"//\s*([a-zA-Z][a-zA-Z0-9]*)", s)
        if m_tag: hints["tag"] = m_tag.group(1).lower()

        # Quoted strings → often text or attribute values
        for q in re.findall(r"'([^']+)'|\"([^\"]+)\"", s):
            token = (q[0] or q[1]).strip()
            if token and len(token) <= 64: hints["tokens"].add(token)

        # Attribute values in xpath/css
        for attr in ("id","name","data-testid","data-test-id","data-qa","aria-label","placeholder","value"):
            for m in re.finditer(attr + r"\s*=\s*'\"['\"]", s, re.I):
                val = m.group(1).strip()
                if val: hints["tokens"].add(val)

        # From CSS like #login-btn or .btn-primary
        for m in re.finditer(r"#([A-Za-z_][-A-Za-z0-9_:\.]*)", s): hints["tokens"].add(m.group(1))
        for m in re.finditer(r"\.([A-Za-z_][-A-Za-z0-9_:\.]*)", s): hints["tokens"].add(m.group(1))

        cleaned = []
        for t in hints["tokens"]:
            lt = t.strip()
            if lt and len(lt) >= 2: cleaned.append(lt)
        return {"tokens": cleaned[:10], "tag": hints["tag"]}

    def _escape_xpath_text(self, s: str) -> str:
        if "'" not in s: return f"'{s}'"
        if '"' not in s: return f'"{s}"'
        parts = s.split("'")
        return "concat(" + ", \"'\", ".join("'" + p + "'" for p in parts) + ")"

    def _heuristic_xpath_patterns(self, tag: str, tok: str):
        clauses = [
            f"contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(@data-testid,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(@data-test-id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(@data-qa,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')",
            f"contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')"
        ]
        node = tag if tag else "*"
        xp = f"//{node}[{' or '.join(clauses)}]"
        xps = [
            xp,
            f"//{node}[@id='{tok}']",
            f"//{node}[@name='{tok}']",
            f"//{node}[@data-testid='{tok}']",
            f"//{node}[@data-test-id='{tok}']",
            f"//{node}[@data-qa='{tok}']",
            f"//{node}[@aria-label='{tok}']",
            f"//{node}[@placeholder='{tok}']",
            f"//{node}[normalize-space(text())='{tok}']",
        ]
        if not tag or tag in ("button", "a", "div", "span"):
            xps += [
                f"//*[@role='button' and contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')]",
                f"//button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')]",
                f"//a[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')]",
            ]
        if not tag or tag in ("input", "textarea", "select"):
            xps += [
                f"//input[@placeholder='{tok}']",
                f"//input[contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')]",
                f"//input[@name='{tok}']",
                f"//input[@id='{tok}']",
                f"//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{tok.lower()}')]",
            ]
        return list(dict.fromkeys(xps))

    def _heuristic_css_patterns(self, tag: str, tok: str):
        parts = [
            f"[data-testid='{tok}']",
            f"[data-test-id='{tok}']",
            f"[data-qa='{tok}']",
            f"[aria-label='{tok}']",
            f"[name='{tok}']",
            f"#{tok}",
            f"[id='{tok}']",
        ]
        if tag:
            parts += [f"{tag}{p}" for p in parts]
        parts += [f".{tok}"]
        if tag:
            parts += [f"{tag}.{tok}"]
        parts += [
            f"[name*='{tok}']",
            f"[id*='{tok}']",
            f"[data-testid*='{tok}']",
            f"[data-test-id*='{tok}']",
            f"[data-qa*='{tok}']",
            f"[aria-label*='{tok}']",
            f"[placeholder*='{tok}']",
        ]
        if tag:
            parts += [f"{tag}{p}" for p in parts if not p.startswith(tag)]
        parts = list(dict.fromkeys(parts))
        return [("css", p if p.startswith(("css=", "css:")) else f"css:{p}") for p in parts]

    def _js_collect_anchors(self, driver):
        js = r"""
        const attrs = ['data-testid','data-test-id','data-qa','aria-label','role','id','name'];
        const counts = {};
        for (const a of attrs) counts[a] = Object.create(null);
        const all = document.querySelectorAll('*');
        for (const el of all) {
          for (const a of attrs) {
            const v = el.getAttribute(a);
            if (v) counts[a][v] = (counts[a][v] || 0) + 1;
          }
        }
        const out = [];
        for (const el of all) {
          let tag = (el.tagName || '').toLowerCase();
          for (const a of attrs) {
            const v = el.getAttribute(a);
            if (!v) continue;
            const c = counts[a][v] || 0;
            if (c > 0 && c <= 3) {
              out.push({attr: a, value: v, unique: c === 1, tag});
            }
          }
        }
        const seen = new Set();
        const dedup = [];
        for (const r of out) {
          const k = r.attr + '::' + r.value;
          if (!seen.has(k)) { seen.add(k); dedup.push(r); }
        }
        return dedup.slice(0, 200);
        """
        try:
            return self._sl.driver.execute_script(js) or []
        except Exception:
            return []

    def _build_anchor_xpath(self, anchor_attr, anchor_val, inner_xpath):
        val = self._escape_xpath_text(anchor_val)
        return f"//*[@{anchor_attr}={val}]{inner_xpath}"

    def _anchored_descendant_xpaths(self, anchor, tokens):
        out = []
        tags_click = ("button", "a", "span", "div")
        tags_input = ("input", "textarea", "select")
        tok_lit = [t for t in tokens if t and len(t) <= 64]
        for t in tok_lit:
            lit = self._escape_xpath_text(t)
            inner = "|".join([f".//{tg}[normalize-space(.)={lit}]" for tg in tags_click])
            if inner:
                out.append({"type": "xpath", "locator": self._build_anchor_xpath(anchor["attr"], anchor["value"], f"//({inner})"), "meta":{"anchored": True}})
            inner = "|".join([f".//{tg}[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{t.lower()}')]" for tg in tags_click])
            if inner:
                out.append({"type": "xpath", "locator": self._build_anchor_xpath(anchor["attr"], anchor["value"], f"//({inner})"), "meta":{"anchored": True}})
            inner = "|".join([f".//{tg}[@name={lit} or @placeholder={lit} or contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{t.lower()}') or contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{t.lower()}')]" for tg in tags_input])
            if inner:
                out.append({"type": "xpath", "locator": self._build_anchor_xpath(anchor["attr"], anchor["value"], f"//({inner})"), "meta":{"anchored": True}})
        for t in tok_lit:
            for attr in ("data-testid","data-test-id","data-qa","aria-label","id","name"):
                lit = self._escape_xpath_text(t)
                inner = f".//*[@{attr}={lit}] | .//*[@{attr}][contains(translate(@{attr},'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{t.lower()}')]"
                out.append({"type": "xpath", "locator": self._build_anchor_xpath(anchor["attr"], anchor["value"], f"//({inner})"), "meta":{"anchored": True}})
        return out[:60]

    def _js_collect_label_relations(self, driver):
        js = r"""
        function textOf(el){ return (el.textContent || '').replace(/\s+/g,' ').trim(); }
        const out = [];
        for (const l of document.querySelectorAll('label[for]')) {
          const t = textOf(l);
          const fid = l.getAttribute('for');
          if (t && fid) out.push({labelText: t, targetId: fid, wrapper: false});
        }
        for (const l of document.querySelectorAll('label')) {
          const t = textOf(l);
          if (!t) continue;
          const cand = l.querySelector('input,textarea,select');
          if (cand) {
            const tag = (cand.tagName||'').toLowerCase();
            out.push({labelText: t, cssPath: tag, wrapper: true});
          }
        }
        return out.slice(0, 200);
        """
        try:
            return self._sl.driver.execute_script(js) or []
        except Exception:
            return []

    def _label_based_xpaths(self, label_text):
        lit = self._escape_xpath_text(label_text)
        out = []
        out.append({"type":"xpath","locator":f"//label[normalize-space(.)={lit}]/@for"})
        out.append({"type":"xpath","locator":f"//label[normalize-space(.)={lit}]//input"})
        out.append({"type":"xpath","locator":f"//label[normalize-space(.)={lit}]//textarea"})
        out.append({"type":"xpath","locator":f"//label[normalize-space(.)={lit}]//select"})
        out.append({"type":"xpath","locator":f"//label[normalize-space(.)={lit}]/following::input[1]"})
        out.append({"type":"xpath","locator":f"//label[normalize-space(.)={lit}]/following::textarea[1]"})
        out.append({"type":"xpath","locator":f"//label[normalize-space(.)={lit}]/following::select[1]"})
        return out

    def _resolve_label_for_xpath(self, locator: str, driver):
        if not locator.endswith("/@for"): return None
        base = locator[:-4]
        js = r"""
        function xp(x){ try{ return document.evaluate(x, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; }catch(e){return null;}}
        const base = arguments[0];
        const lab = xp(base);
        if (!lab) return null;
        const fid = lab.getAttribute('for');
        return fid || null;
        """
        try:
            fid = driver.execute_script(js, base)
            if fid:
                return {"type":"id","locator": fid}
        except Exception:
            return None
        return None

    def _generate_heuristic_candidates(self, old_locator: str):
        driver = getattr(self._sl, "driver", None)
        if not driver: return []

        hints = self._extract_hints_from_locator(old_locator)
        tokens = hints["tokens"]
        tag = hints["tag"]

        candidates = []
        for tok in tokens:
            for xp in self._heuristic_xpath_patterns(tag, tok):
                candidates.append({"type": "xpath", "locator": xp})
            for typ, css in self._heuristic_css_patterns(tag, tok):
                candidates.append({"type": "css", "locator": css})

        # Anchored descendant XPaths
        anchors = self._js_collect_anchors(driver)
        def anchor_rank(a):
            order = {"data-testid":0,"data-test-id":1,"data-qa":2,"aria-label":3,"role":4,"id":5,"name":6}
            return order.get(a["attr"], 99), (0 if a.get("unique") else 1)
        anchors = sorted(anchors, key=anchor_rank)[:60]
        for a in anchors:
            candidates.extend(self._anchored_descendant_xpaths(a, tokens or []))

        # Label-based
        labelrels = self._js_collect_label_relations(driver)
        for rel in labelrels:
            txt = rel.get("labelText")
            if not txt: continue
            for c in self._label_based_xpaths(txt):
                if c["locator"].endswith("/@for"):
                    resolved = self._resolve_label_for_xpath(c["locator"], driver)
                    if resolved:
                        candidates.append(resolved)
                else:
                    candidates.append(c)

        if not tokens and tag:
            candidates += [
                {"type": "xpath", "locator": f"//{tag}[@id]"},
                {"type": "xpath", "locator": f"//{tag}[@name]"},
                {"type": "xpath", "locator": f"//*[@role='button']"},
            ]

        seen = set(); out=[]
        for c in candidates:
            key = self._canon_selector_key(c.get("type"), c.get("locator"))
            if key and key not in seen:
                seen.add(key); out.append(c)
            if len(out) >= 300:
                break
        return out

    # -------------------------
    # LLM (multi-candidate) helpers
    # -------------------------
    def _sampling_options(self, attempt_idx: int, spin_idx: int):
        temp = max(0.0, min(1.5, self.heal_temp_start + self.heal_temp_step * (attempt_idx + spin_idx)))
        top_p = max(0.1, min(1.0, self.heal_top_p))
        opts = {"temperature": temp, "top_p": top_p}
        if self.randomize_seed:
            opts["seed"] = int(time.time() * 1000) % 2_147_483_647
        return opts

    def _extract_first_json_object(self, s: str):
        in_str = False
        esc = False
        depth = 0
        start_idx = -1
        for i, ch in enumerate(s):
            if ch == '"' and not esc:
                in_str = not in_str
                esc = (ch == '\\') and not esc if in_str else False
            if in_str: continue
            if ch == '{':
                if depth == 0: start_idx = i
                depth += 1
            elif ch == '}':
                if depth > 0: depth -= 1
                if depth == 0 and start_idx != -1:
                    segment = s[start_idx:i+1]
                    try: return json.loads(segment)
                    except Exception: pass
        return None

    def _ask_ollama_for_candidates(self, old_locator, html, topk=5, exclude=None, llm_options=None):
        exclude = exclude or []
        avoid_block = ""
        if exclude:
            shown = exclude[-10:] if len(exclude) > 10 else exclude
            avoid_block = "\nAvoid these prior selectors (do NOT repeat):\n- " + "\n- ".join(shown)

        prompt = f"""
You are a function that outputs ONLY ONE JSON object with this schema:
{{"candidates":[{{"type":"css|xpath|id|name","locator":"<selector>","why":"<short reason>"}}, ... up to {topk}]}}
Rules:
- Provide {topk} diverse, robust selectors. Prefer this order: data-testid/data-test-id/data-qa → aria-label → id/name → robust CSS → XPath.
- Avoid brittle absolute XPaths with deep indices like //div[3]/div[2].
- If CSS is possible, use CSS and set "type":"css".
- Use meaningful attributes seen in the HTML snippet (id/name/aria/data-*/placeholder/value/text).
- Broken locator: {old_locator}
- HTML (truncated):
{html[:3000]}
{avoid_block}
""".strip()

        llm_options = llm_options or {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": llm_options.get("temperature", 0.4),
                "top_p": llm_options.get("top_p", 0.95),
            }
        }
        if "seed" in llm_options:
            payload["options"]["seed"] = llm_options["seed"]

        content = ""
        try:
            print(f"[Healing] → LLM candidates: posting {self.ollama_url} with options: {payload['options']}")
            r = requests.post(self.ollama_url, json=payload, timeout=45)
            print(f"[Healing] Ollama status: {r.status_code}")
            if r.status_code != 200:
                print(r.text)
                return []

            content = r.json().get("response", "")
            try:
                obj = json.loads(content)
            except Exception:
                obj = self._extract_first_json_object(content)
            if not isinstance(obj, dict) or "candidates" not in obj:
                return []

            out = []
            for c in obj.get("candidates", []):
                loc = (c.get("locator") or "").strip()
                typ = (c.get("type") or "").strip().lower()
                if not loc:
                    continue
                if typ not in ("css", "xpath", "id", "name", ""):
                    typ = ""
                out.append({"type": typ or None, "locator": loc, "why": c.get("why")})
            print(f"[Healing] ✅ LLM returned {len(out)} candidate(s).")
            return out[:max(1, int(topk))]
        except Exception as e:
            preview = (content or "")[:200].replace("\n", " ")
            print(f"[Healing] ❌ LLM error: {e} | preview: {preview}")
            return []

    # -------------------------
    # Public Keywords (overrides + utilities)
    # -------------------------
    def test_ollama_connection(self):
        """Test connection to Ollama API"""
        print(f"[Healing] Testing Ollama connection → {self.ollama_url}")
        payload = {"model": self.model, "prompt": "ping", "stream": False}
        try:
            r = requests.post(self.ollama_url, json=payload, timeout=30)
            print(f"[Healing] Status: {r.status_code}, Response: {r.text[:200]}")
            return r.status_code
        except Exception as e:
            print(f"[Healing] ❌ Connection failed: {e}")
            raise

    def click_element(self, locator):
        """Healable version of Click Element (context-aware)."""
        print(f"[Healing] Click Element → {locator}")
        if not self._session_alive():
            self._fail_for_dead_session("Click Element (pre)")
            return
        try:
            self._sl.click_element(locator)
            return
        except Exception as e:
            if self._is_invalid_session_error(e):
                self._fail_for_dead_session("Click Element (action)")
                return
            print(f"[Healing] ⚠️ Failed: {e}")

        if not self.auto_heal: raise
        healed = self._get_current_healed(locator)
        if healed:
            ok, _, _, ctx = self._locator_exists_with_context(healed)
            if not ok:
                healed = None
            else:
                healed["context"] = ctx
        healed = healed or self._heal_locator(locator)
        if healed:
            self._perform_action_with_context("click", healed, None)
        else:
            raise

    def input_text(self, locator, text):
        """Healable version of Input Text (context-aware)."""
        print(f"[Healing] Input Text → {locator}")
        if not self._session_alive():
            self._fail_for_dead_session("Input Text (pre)")
            return
        try:
            self._sl.input_text(locator, text)
            return
        except Exception as e:
            if self._is_invalid_session_error(e):
                self._fail_for_dead_session("Input Text (action)")
                return
            print(f"[Healing] ⚠️ Failed: {e}")

        if not self.auto_heal: raise
        healed = self._get_current_healed(locator)
        if healed:
            ok, _, _, ctx = self._locator_exists_with_context(healed)
            if not ok:
                healed = None
            else:
                healed["context"] = ctx
        healed = healed or self._heal_locator(locator)
        if healed:
            self._perform_action_with_context("input", healed, text)
        else:
            raise

    def _perform_action_with_context(self, action, healed_entry, text=None):
        """Perform click/input respecting frame/shadow context if present."""
        context = healed_entry.get("context") or {}
        loc = healed_entry["locator"]
        drv = self._sl.driver

        # iframe path (auto hop)
        if self.search_iframes:
            frame_path = context.get("frame_path")
            if frame_path is None:
                # discover on the fly if not captured
                frame_path = self._find_first_iframe_path(drv, loc, self.iframe_depth)
            if frame_path:
                try:
                    for idx in frame_path:
                        frames = drv.find_elements("tag name","iframe")+drv.find_elements("tag name","frame")
                        if idx < 0 or idx >= len(frames): break
                        drv.switch_to.frame(frames[idx])
                    if action == "click":
                        self._sl.click_element(loc)
                    else:
                        self._sl.input_text(loc, text)
                finally:
                    try: drv.switch_to.default_content()
                    except Exception: pass
                return

        # shadow css (deep)
        if self.search_shadow_dom and loc.lower().startswith(("css=", "css:")):
            css = self._strip_strategy_prefix(loc)
            # try deep match; if found act via JS
            if self._deep_exists_shadow(drv, css):
                if action == "click":
                    self._js_click_shadow(css)
                else:
                    self._js_input_shadow(css, text)
                return

        # normal
        if action == "click":
            self._sl.click_element(loc)
        else:
            self._sl.input_text(loc, text)

    def _js_click_shadow(self, css):
        js = r"""
        const sel = arguments[0];
        function queryFirstDeep(root) {
          try { const m = root.querySelector(sel); if (m) return m; } catch(e){}
          const all = root.querySelectorAll('*');
          for (const el of all) {
            if (el.shadowRoot) {
              try { const m2 = el.shadowRoot.querySelector(sel); if (m2) return m2; } catch(e){}
              const deeper = queryFirstDeep(el.shadowRoot);
              if (deeper) return deeper;
            }
          }
          return null;
        }
        const el = queryFirstDeep(document);
        if (el) { el.click(); return true; }
        return false;
        """
        self._sl.driver.execute_script(js, css)

    def _js_input_shadow(self, css, value):
        js = r"""
        const sel = arguments[0], val = arguments[1];
        function queryFirstDeep(root) {
          try { const m = root.querySelector(sel); if (m) return m; } catch(e){}
          const all = root.querySelectorAll('*');
          for (const el of all) {
            if (el.shadowRoot) {
              try { const m2 = el.shadowRoot.querySelector(sel); if (m2) return m2; } catch(e){}
              const deeper = queryFirstDeep(el.shadowRoot);
              if (deeper) return deeper;
            }
          }
          return null;
        }
        const el = queryFirstDeep(document);
        if (el) {
          el.focus();
          el.value = val;
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          return true;
        }
        return false;
        """
        self._sl.driver.execute_script(js, css, value)

    def validate_locator(self, locator, type_hint=None, require_visible=None, unique=None):
        entry = {"type": type_hint, "locator": locator} if not isinstance(locator, dict) else locator
        ok, sl_loc, count = self._locator_exists_in_dom(
            entry,
            require_visible=self.require_visible if require_visible is None else self._to_bool(require_visible),
            unique=self.require_unique if unique is None else self._to_bool(unique),
            retries=self.validate_retries,
            interval_ms=self.validate_retry_interval_ms
        )
        print(f"[Healing] Validate → ok={ok} locator={sl_loc} count={count}")
        return {"ok": ok, "locator": sl_loc, "count": count}

    def why_locator_not_matching(self, locator, type_hint=None):
        entry = {"type": type_hint, "locator": locator} if not isinstance(locator, dict) else locator
        typ = entry.get("type"); sl_loc = self._normalize_locator(typ, entry.get("locator"))
        driver = getattr(self._sl, "driver", None)
        if not driver or not sl_loc: return {"error":"No driver or bad locator."}
        report = {"locator": sl_loc, "top": 0, "shadow": 0, "iframes_total": 0, "iframe_depth": self.iframe_depth}
        try:
            top_elems = self._sl.get_webelements(sl_loc)
            report["top"] = len(top_elems)
        except Exception:
            report["top"] = -1
        if self.search_shadow_dom and sl_loc.lower().startswith(("css=", "css:")):
            css_raw = self._strip_strategy_prefix(sl_loc)
            report["shadow"] = self._deep_query_selector_all_count(driver, css_raw)
        if self.search_iframes and self.iframe_depth > 0:
            report["iframes_total"] = self._search_iframes_for_locator(
                driver, sl_loc, require_visible=False, unique=False,
                max_depth=self.iframe_depth, search_shadow=self.search_shadow_dom
            )
        print("\n[Healing] Why Locator Not Matching:")
        print(f"  Locator: {sl_loc}")
        print(f"  Top doc matches: {report['top']}")
        print(f"  Shadow DOM matches: {report['shadow']}")
        print(f"  Iframe matches (<= depth {report['iframe_depth']}): {report['iframes_total']}\n")
        return report

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

    # Toggles
    def enable_auto_healing(self):
        self.auto_heal = True
        print("[Healing] Auto-Heal enabled.")

    def disable_auto_healing(self):
        self.auto_heal = False
        print("[Healing] Auto-Heal disabled.")

    def set_auto_healing(self, value):
        self.auto_heal = self._to_bool(value, self.auto_heal)
        print(f"[Healing] Auto-Heal set to: {self.auto_heal}")

    def get_auto_healing_status(self):
        return self.auto_heal

    def enable_auto_rewrite(self):
        self.auto_rewrite = True
        print("[Healing] Auto-Rewrite enabled.")

    def disable_auto_rewrite(self):
        self.auto_rewrite = False
        print("[Healing] Auto-Rewrite disabled.")

    def set_auto_rewrite(self, value):
        self.auto_rewrite = self._to_bool(value, self.auto_rewrite)
        print(f"[Healing] Auto-Rewrite set to: {self.auto_rewrite}")

    def get_auto_rewrite_status(self):
        return self.auto_rewrite

    # -------------------------
    # Healing action (main)
    # -------------------------
    def _get_current_healed(self, locator):
        entry = self.healed_locators.get(locator)
        if not entry:
            return None
        if isinstance(entry, dict) and "current" in entry:
            return entry["current"]
        if isinstance(entry, dict) and "locator" in entry:
            return entry  # legacy flat format
        return None

    def _is_locator_currently_valid(self, entry: dict) -> bool:
        ok, _, _ = self._locator_exists_in_dom(
            entry,
            require_visible=self.require_visible,
            unique=self.require_unique,
            retries=self.validate_retries,
            interval_ms=self.validate_retry_interval_ms
        )
        return ok

    def _score_selector_string(self, typ: str, loc: str, meta=None) -> int:
        s = (self._normalize_locator(typ, loc) or "").lower()
        score = 0
        if meta and meta.get("anchored"): score += 40
        for k in ("data-testid=", "data-test-id=", "data-qa="):
            if k in s: score += 50
        if "aria-label=" in s: score += 35
        if "@role=" in s or "[@role=" in s or " css:[role=" in s: score += 25
        if "id=" in s or "css:#" in s or "css:[id=" in s: score += 22
        if "name=" in s or "css:[name=" in s: score += 18
        if "label[" in s or "/following::input" in s or "/following::textarea" in s or "/following::select" in s: score += 16
        if "normalize-space(text())" in s or "contains(translate(normalize-space(" in s: score += 10
        if "css:." in s: score += 5
        if s.startswith("xpath=") or s.startswith("xpath:") or "xpath=" in s:
            depth_idx = len(re.findall(r"\[[0-9]+\]", s))
            score -= min(25, depth_idx * 3)
            if s.count("//div") >= 2 and depth_idx >= 2:
                score -= 10
        return score

    def _pick_first_matching_candidate(self, candidates):
        """
        Validate candidates ordered by score; prefer visible and unique matches when choosing.
        """
        scored = []
        for c in candidates:
            s = self._score_selector_string(c.get("type"), c.get("locator"), c.get("meta"))
            scored.append((s, c))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Try tiers: (visible+unique) → (visible) → (exists)
        tiers = [
            {"require_visible": True,  "unique": True},
            {"require_visible": True,  "unique": False},
            {"require_visible": False, "unique": False},
        ]

        for tier in tiers:
            for score, c in scored:
                ok, normalized, count, ctx = self._locator_exists_with_context(c)
                # For stricter tier constraints, re-check with flags:
                if not ok:
                    ok2, normalized2, count2 = self._locator_exists_in_dom(
                        c,
                        require_visible=tier["require_visible"],
                        unique=tier["unique"],
                        retries=max(1, self.validate_retries),
                        interval_ms=self.validate_retry_interval_ms
                    )
                    ok, normalized, count = ok2, normalized2, count2
                    if ok:
                        ctx = None  # found in top doc on stricter check
                print(f"[Healing] Candidate score={score} tier={tier} → {normalized} | ok={ok} count={count}")
                if ok:
                    out = {"type": c.get("type"), "locator": normalized}
                    if ctx:
                        out["context"] = ctx
                    return out
        return None

    def _heal_locator(self, locator):
        """
        Healing flow:
          1) Heuristics-first: generate candidates from tokens/anchors/labels and validate.
          2) If none match, ask LLM for top-K candidates; validate & score; repeat with new seeds.
        """
        driver = getattr(self._sl, "driver", None)
        if not driver:
            print("[Healing] ❌ No Selenium driver found.")
            return None
        if not self._session_alive():
            self._fail_for_dead_session("_heal_locator (pre-check)")
            return None

        # 1) Heuristics-first
        heuristics = self._generate_heuristic_candidates(locator)
        print(f"[Healing] 🔎 Heuristic candidates: {len(heuristics)}")
        best = self._pick_first_matching_candidate(heuristics)
        if best:
            self._record_healing(locator, best)
            return self._get_current_healed(locator)

        # 2) LLM multi-candidate fallback with retries/spins
        seen_keys = set()
        seen_human = set()
        for attempt_idx in range(self.heal_retries):
            try:
                html = driver.page_source
            except Exception as e:
                if self._is_invalid_session_error(e):
                    self._fail_for_dead_session("_heal_locator (reading page_source)")
                    return None
                raise

            chosen = None
            for spin in range(self.heal_spin_limit):
                llm_opts = self._sampling_options(attempt_idx, spin)
                excl = list(seen_human)
                llm_cands = self._ask_ollama_for_candidates(
                    locator, html, topk=self.heal_topk, exclude=excl, llm_options=llm_opts
                )
                filtered = []
                for c in llm_cands:
                    norm = self._normalize_locator(c.get("type"), c.get("locator"))
                    key = (norm or "").lower().replace("css=", "css:")
                    human = self._strip_strategy_prefix(norm)
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    if human:
                        seen_human.add(human)
                    filtered.append({"type": c.get("type") or None, "locator": norm})

                if not filtered:
                    print(f"[Healing] ↻ LLM returned only duplicates; re-asking (attempt {attempt_idx+1}, spin {spin+1}/{self.heal_spin_limit})")
                    continue

                chosen = self._pick_first_matching_candidate(filtered)
                if chosen:
                    break

            if chosen:
                self._record_healing(locator, chosen)
                return self._get_current_healed(locator)

            print(f"[Healing] ❌ No LLM candidate matched [attempt {attempt_idx+1}/{self.heal_retries}]")

        print("[Healing] ❌ Exhausted heal attempts without finding a matching locator.")
        return None