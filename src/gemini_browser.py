import os
import time
import base64
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List, Union
from pydantic import BaseModel

from src.logging_config import logger


# Custom Automation Exceptions
class GeminiAuthRequiredError(Exception):
    """Raised when the Gemini session requires manual user sign-in."""
    pass


class GeminiRateLimitError(Exception):
    """Raised when Gemini usage limits or quotas are detected."""
    pass


class GeminiPromptRefusedError(Exception):
    """Raised when Gemini refuses to generate an image due to safety/policy."""
    pass


class GeminiSelectorError(Exception):
    """Raised when Gemini DOM layout does not match known selectors."""
    pass


class GeminiBrowserAdapter:
    """Automates Gemini web interface in Chrome using a persistent Playwright user profile."""

    # Selectors for Gemini UI elements (with fallbacks)
    PROMPT_INPUT_SELECTORS = [
        "div[contenteditable='true'][role='textbox']",
        "div.ql-editor[contenteditable='true']",
        "textarea[aria-label*='prompt' i]",
        "rich-textarea div[contenteditable='true']",
        "div[aria-label*='prompt' i][contenteditable='true']",
        "div[data-placeholder*='Ask' i]",
    ]

    SEND_BUTTON_SELECTORS = [
        "button[aria-label*='Send' i]",
        "button[aria-label*='Submit' i]",
        "button.send-button",
        "button[data-test-id='send-button']",
        "div.send-button-container button",
    ]

    NEW_CHAT_SELECTORS = [
        "a[aria-label*='New chat' i]",
        "button[aria-label*='New chat' i]",
        "button[data-test-id='new-chat-button']",
        "div[aria-label*='New chat' i]",
    ]

    FILE_INPUT_SELECTORS = [
        "input[type='file']",
        "button[aria-label*='Upload' i]",
        "button[aria-label*='Add image' i]",
        "button[aria-label*='attach' i]",
    ]

    STOP_GENERATING_SELECTORS = [
        "button[aria-label*='Stop response' i]",
        "button[aria-label*='Stop generating' i]",
        "button.stop-generating-button",
        "mat-spinner",
        "div.loading-indicator",
    ]

    RATE_LIMIT_MARKERS = [
        "reached your limit",
        "too many requests",
        "try again later",
        "quota exceeded",
        "usage limit",
        "rate limit",
        "capacity",
    ]

    REFUSAL_MARKERS = [
        "can't generate images of",
        "cannot generate images of",
        "policy does not allow",
        "against our safety guidelines",
        "i can't fulfill this request",
    ]

    def __init__(
        self,
        profile_dir: Union[str, Path] = "data/browser_profile",
        headless: bool = False,
        slow_mo_ms: int = 50,
        timeout_ms: int = 45000,
        gemini_url: str = "https://gemini.google.com/app",
        chrome_executable_path: Optional[str] = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.timeout_ms = timeout_ms
        self.gemini_url = gemini_url
        self.chrome_executable_path = chrome_executable_path

        self._playwright = None
        self._context = None
        self._page = None

    def launch(self) -> None:
        """Launches or attaches to the persistent browser session."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ImportError("Playwright is not installed. Install via: pip install playwright && playwright install chromium")

        if self._page is not None and self._context is not None:
            return

        # Clean up any stale lockfiles from previous crashes
        for lock_name in ("lockfile", "SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = self.profile_dir / lock_name
            if lock_path.exists():
                try:
                    lock_path.unlink()
                except Exception:
                    pass

        logger.info(f"Launching persistent browser context from {self.profile_dir}...")
        self._playwright = sync_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        base_kwargs: Dict[str, Any] = {
            "user_data_dir": str(self.profile_dir.resolve()),
            "headless": self.headless,
            "slow_mo": self.slow_mo_ms,
            "viewport": {"width": 1280, "height": 900},
            "args": launch_args,
        }

        # Multi-browser fallback priority: Edge -> Chrome -> bundled Chromium
        edge_path = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        chrome_path = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")

        candidates: List[Dict[str, Any]] = []
        if self.chrome_executable_path and Path(self.chrome_executable_path).exists():
            candidates.append({"executable_path": self.chrome_executable_path})
        if edge_path.exists():
            candidates.append({"channel": "msedge"})
        if chrome_path.exists():
            candidates.append({"channel": "chrome"})
        candidates.append({})  # Fallback to default bundled chromium

        launched = False
        last_error = None
        for candidate in candidates:
            attempt_kwargs = {**base_kwargs, **candidate}
            try:
                self._context = self._playwright.chromium.launch_persistent_context(**attempt_kwargs)
                launched = True
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Could not launch browser with {candidate}: {e}")

        if not launched:
            raise RuntimeError(f"Failed to launch browser: {last_error}")

        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

        # Navigate to Gemini
        self.navigate_to_gemini()

    def navigate_to_gemini(self) -> None:
        if not self._page:
            return
        try:
            curr_url = self._page.url
            if "gemini.google.com" not in curr_url:
                logger.info(f"Navigating to {self.gemini_url}...")
                self._page.goto(self.gemini_url, wait_until="domcontentloaded")
                time.sleep(2)
        except Exception as e:
            logger.warning(f"Error navigating to Gemini: {e}")

    def check_auth_status(self) -> Tuple[bool, str]:
        """Inspects whether the current page is authenticated or requires manual login."""
        if not self._page:
            return False, "Browser is not running."

        try:
            curr_url = self._page.url.lower()

            # Check for Google sign-in redirect
            if "accounts.google.com" in curr_url:
                return False, "Google Sign-In page detected. Manual authentication required."

            # Check for sign-in buttons on Gemini landing
            sign_in_elements = self._page.query_selector_all("a[href*='accounts.google.com'], button:has-text('Sign in')")
            if sign_in_elements:
                return False, "Gemini 'Sign in' prompt visible. Please log in through the open browser."

            # Check if the prompt input or chat interface is visible
            for sel in self.PROMPT_INPUT_SELECTORS:
                if self._page.query_selector(sel):
                    return True, "Authenticated and ready."

            # If still loading or uncertain
            return False, "Waiting for Gemini interface to load or manual authentication."
        except Exception as e:
            logger.warning(f"Exception during check_auth_status: {e}")
            return False, f"Authentication check pending: {e}"


    def new_conversation(self) -> bool:
        """Starts a fresh conversation in Gemini to keep prompt context clean."""
        if not self._page:
            return False

        for sel in self.NEW_CHAT_SELECTORS:
            btn = self._page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(1.5)
                return True

        # Fallback: navigate directly to main app url
        self._page.goto(self.gemini_url, wait_until="domcontentloaded")
        time.sleep(2)
        return True

    def attach_reference_image(self, image_path: Path) -> bool:
        """Uploads a reference image to the current conversation if required."""
        if not image_path.exists():
            raise FileNotFoundError(f"Reference image not found on disk: {image_path}")

        logger.info(f"Attaching reference image: {image_path.name}")
        file_input = self._page.query_selector("input[type='file']")
        if file_input:
            file_input.set_input_files(str(image_path.resolve()))
            time.sleep(2)
            return True

        # Try clicking upload button to reveal input
        for sel in self.FILE_INPUT_SELECTORS:
            btn = self._page.query_selector(sel)
            if btn and btn.is_visible():
                with self._page.expect_file_chooser(timeout=5000) as fc_info:
                    btn.click()
                file_chooser = fc_info.value
                file_chooser.set_files(str(image_path.resolve()))
                time.sleep(2)
                return True

        logger.warning("Could not find file input for reference image attachment.")
        return False

    def submit_prompt(self, prompt_text: str) -> None:
        """Finds prompt input, types the prompt, and clicks the submit button."""
        if not self._page:
            raise RuntimeError("Browser session is not running.")

        # Step 1: Check authentication
        is_auth, auth_msg = self.check_auth_status()
        if not is_auth:
            raise GeminiAuthRequiredError(auth_msg)

        # Step 2: Locate prompt input element
        input_el = None
        matched_selector = None
        for sel in self.PROMPT_INPUT_SELECTORS:
            el = self._page.query_selector(sel)
            if el and el.is_visible():
                input_el = el
                matched_selector = sel
                break

        if not input_el:
            self._dump_diagnostics("prompt_input_missing")
            raise GeminiSelectorError(
                f"Could not find Gemini prompt input box. DOM layout may have changed. "
                f"Tried selectors: {self.PROMPT_INPUT_SELECTORS}"
            )

        # Step 3: Enter prompt text
        input_el.click()
        time.sleep(0.3)
        input_el.fill("")
        input_el.fill(prompt_text)
        time.sleep(0.5)

        # Step 4: Click Send button
        send_btn = None
        for sel in self.SEND_BUTTON_SELECTORS:
            btn = self._page.query_selector(sel)
            if btn and btn.is_visible():
                send_btn = btn
                break

        if send_btn:
            send_btn.click()
        else:
            # Fallback: Press Enter inside input
            input_el.press("Enter")

        logger.info("Submitted prompt to Gemini.")

    def wait_for_image_completion(self, timeout_seconds: int = 120) -> bytes:
        """Waits for image generation to complete, checking for limits/refusals,
        and retrieves the generated image bytes.
        """
        if not self._page:
            raise RuntimeError("Browser session is not running.")

        start_time = time.time()
        time.sleep(3)  # Initial wait for generation trigger

        while time.time() - start_time < timeout_seconds:
            # Check for rate limit banners
            body_text = self._page.inner_text("body").lower()
            for marker in self.RATE_LIMIT_MARKERS:
                if marker in body_text:
                    raise GeminiRateLimitError(f"Gemini usage limit detected: '{marker}'")

            # Check for policy refusal
            for marker in self.REFUSAL_MARKERS:
                if marker in body_text:
                    raise GeminiPromptRefusedError(f"Gemini refused prompt: '{marker}'")

            # Check if still generating (spinner or stop button present)
            still_generating = False
            for sel in self.STOP_GENERATING_SELECTORS:
                indicator = self._page.query_selector(sel)
                if indicator and indicator.is_visible():
                    still_generating = True
                    break

            if not still_generating:
                # Check for generated image elements in the response
                image_bytes = self._extract_latest_image_bytes()
                if image_bytes:
                    return image_bytes

            time.sleep(2)

        self._dump_diagnostics("generation_timeout")
        raise TimeoutError(f"Image generation timed out after {timeout_seconds} seconds.")

    def _extract_latest_image_bytes(self) -> Optional[bytes]:
        """Extracts generated image from the DOM as raw bytes."""
        # Find candidate images inside the response containers
        images = self._page.query_selector_all("img")
        for img in reversed(images):
            try:
                src = img.get_attribute("src") or ""
                # Generated images on Gemini typically start with data:image or blob: or googleusercontent
                if src.startswith("data:image"):
                    header, base64_data = src.split(",", 1)
                    return base64.b64decode(base64_data)
                elif "googleusercontent.com" in src or "lh3.googleusercontent.com" in src:
                    # Download directly using browser request context to retain authentication cookies
                    response = self._page.request.get(src)
                    if response.ok and len(response.body()) > 1000:
                        return response.body()
                elif src.startswith("blob:"):
                    # Extract blob data through canvas / fetch in page context
                    js_blob_extract = """
                    async (imgElement) => {
                        const response = await fetch(imgElement.src);
                        const blob = await response.blob();
                        return new Promise((resolve) => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve(reader.result);
                            reader.readAsDataURL(blob);
                        });
                    }
                    """
                    data_url = self._page.evaluate(js_blob_extract, img)
                    if data_url and data_url.startswith("data:image"):
                        _, base64_data = data_url.split(",", 1)
                        return base64.b64decode(base64_data)
            except Exception as e:
                logger.debug(f"Image candidate check failed: {e}")

        return None

    def _dump_diagnostics(self, reason: str) -> None:
        """Saves current DOM diagnostic snapshot for troubleshooting."""
        try:
            diag_dir = Path("logs/diagnostics")
            diag_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            snap_path = diag_dir / f"{reason}_{ts}.html"
            with open(snap_path, "w", encoding="utf-8") as f:
                f.write(self._page.content() if self._page else "No page")
            logger.info(f"Saved diagnostic DOM dump to {snap_path}")
        except Exception as e:
            logger.warning(f"Could not dump diagnostics: {e}")

    def close(self) -> None:
        """Closes browser context and Playwright cleanly."""
        try:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning(f"Error during browser close: {e}")
        finally:
            self._page = None
            self._context = None
            self._playwright = None
