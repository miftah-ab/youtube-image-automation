# YouTube Image Automation System

Production-quality local automation engine designed to automate the complete image-generation workflow for YouTube video production using the free Gemini website, Notion video script pages, SQLite persistence, and Google Drive for Desktop synchronization.

---

## 1. Supported Channels

The system supports three dedicated channel profiles out of the box:

1. **Ninety Sense**
   - **Theme:** Psychology and Human Behavior
   - **Visual Identity:** Moody ambient lighting, subtle film grain, thoughtful human expression, cinematic 16:9 ratio.
   - **Output Directory:** `Ninety-Sense/`

2. **Case XYZ**
   - **Theme:** Case-Based Storytelling & Investigations
   - **Visual Identity:** Gritty investigative journalism documentary aesthetic, dramatic shadows, cinematic color grading.
   - **Output Directory:** `Case-XYZ/`

3. **American Wallet**
   - **Theme:** American Personal Finance & Wealth
   - **Visual Identity:** Clean modern editorial photography, crisp warm lighting, polished professional atmosphere.
   - **Output Directory:** `American-Wallet/`

Each channel configuration is isolated in [`config/channels.yaml`](file:///c:/Users/Dell/.gemini/antigravity-ide/scratch/youtube-image-automation/config/channels.yaml) with its own output subfolder, naming convention, prompt-processing rules, and reference image settings.

---

## 2. System Architecture

```
youtube-image-automation/
│
├── app.py                     # Streamlit local dashboard (127.0.0.1:8501)
├── run.bat                    # One-click Windows launcher
├── requirements.txt           # Pinned production dependencies
├── run_integration_test.py    # Mock & Live integration test runner
│
├── config/
│   ├── settings.yaml          # Global timeout, browser, and automation settings
│   └── channels.yaml          # Channel profiles, visual rules, and prompt regexes
│
├── src/
│   ├── __init__.py
│   ├── logging_config.py      # Structured logging with token & secret redaction
│   ├── channel_config.py      # Channel registry and prompt style applicator
│   ├── prompt_parser.py       # Robust Notion page block and prompt section parser
│   ├── notion_client.py       # Notion API client with cursor pagination and retry
│   ├── image_validator.py     # Image verification (PIL load, SHA256, format check)
│   ├── image_downloader.py    # Atomic file writer (.tmp promotion, duplicate check)
│   ├── drive_sync.py          # Google Drive for Desktop sync & disclaimer handling
│   ├── progress_store.py      # SQLite ACID store with Compare-And-Swap transitions
│   ├── recovery.py            # Startup crash reconciliation and file audit
│   ├── gemini_browser.py      # Persistent Chrome profile automation via Playwright
│   └── job_manager.py         # Multi-threaded worker orchestrator and lock manager
│
├── tests/                     # Automated unit and integration test suite
│   ├── test_prompt_parser.py
│   ├── test_channel_config.py
│   ├── test_image_validator.py
│   ├── test_progress_store.py
│   ├── test_recovery.py
│   ├── test_drive_sync.py
│   ├── test_notion_client.py
│   └── test_job_manager.py
│
├── assets/
│   └── references/            # Channel reference images (.png)
├── data/
│   ├── jobs.db                # SQLite database (auto-created)
│   └── browser_profile/       # Persistent Chrome profile (keeps Google session)
├── logs/                      # Rotating log files
└── output/                    # Local image output root
```

---

## 3. Quick Start (Windows)

### Prerequisites
- **Windows 10 / 11** (64-bit)
- **Python 3.10+** (Python 3.12 installed)
- **Google Chrome** installed (used by Playwright persistent session)

### 1. Launch Application
Double-click [`run.bat`](file:///c:/Users/Dell/.gemini/antigravity-ide/scratch/youtube-image-automation/run.bat) or run in PowerShell:
```powershell
.\run.bat
```
The launcher will automatically:
1. Detect your Python runtime.
2. Create the virtual environment (`.venv`).
3. Install pinned dependencies from `requirements.txt`.
4. Install the Playwright Chromium browser.
5. Create `.env` from `.env.example`.
6. Start the dashboard at `http://127.0.0.1:8501`.

---

## 4. Notion Integration Setup

The application reads one Notion page per video and parses all prompt blocks in sequential order.

### Step 1: Create an Internal Integration Token
1. Go to [https://www.notion.so/my-integrations](https://www.notion.so/my-integrations).
2. Click **+ New integration**.
3. Name it `YouTube Image Automation` and select your workspace.
4. Copy the **Internal Integration Secret** (starts with `secret_...`).

### Step 2: Configure `.env`
Add your token to `.env`:
```env
NOTION_API_KEY=secret_your_notion_token_here
```

### Step 3: Share Your Video Pages with the Integration
1. Open your video page in Notion.
2. Click the `...` menu in the top-right corner.
3. Select **Add connections** (or **Connections**).
4. Search for `YouTube Image Automation` and click **Confirm**.
*(Without this step, Notion returns a 404 error when accessing the page).*

### Step 4: Formatting Prompts Inside Your Notion Page
The parser handles multiple common formats:

**Option A: Label Block followed by Text Block**
```
IMAGE 001
A close-up shot of an antique brass compass lying on weathered nautical map.

IMAGE 002
Dramatic shot of stormy ocean waves crashing against jagged cliffs.
```

**Option B: Single-Line Prefixed Format**
```
IMAGE 001: A close-up shot of an antique brass compass.
IMAGE 002: Dramatic shot of stormy ocean waves.
```

**Option C: Section Heading Isolation**
If your Notion page contains a script, notes, and prompts together, place your prompts under a heading named `Prompts` or `Image Prompts`. The parser will isolate this section and ignore script text, narration, and production notes.

---

## 5. Gemini Persistent Browser Authentication

The automation interacts with the free Gemini web interface (`https://gemini.google.com/app`) using a **persistent browser profile**. You do not need to provide your Google password to the application.

1. In the dashboard, switch to **Tab 3: System Diagnostics & Recovery**.
2. Click **Open Gemini Browser for Manual Login**.
3. A Google Chrome window will open pointing to Gemini.
4. Sign in to your Google Account manually.
5. Once logged in and on the Gemini chat page, close or leave the browser open.
6. The session credentials and cookies remain safely stored locally inside `data/browser_profile/` for all future automated runs.

---

## 6. Google Drive for Desktop Configuration

The application supports two storage modes:

### Mode A: Local Storage Only (`STORAGE_MODE=local`)
Images are saved directly to `output/<Channel>/<Video>/image-001.png`.

### Mode B: Google Drive Synced Folder (`STORAGE_MODE=gdrive`)
Images are written directly into your configured Google Drive for Desktop folder:
```env
STORAGE_MODE=gdrive
GDRIVE_SYNC_PATH=G:\My Drive\YouTube-Automation
```
- The drive letter (`G:`, `D:`, etc.) and subpath are fully configurable.
- The manager checks that the path exists and is writable.
- Files are written atomically using a `.tmp` staging file to avoid partial sync.
- **Sync Status Disclaimer:** The dashboard displays `Saved locally; cloud sync unverified`. Writing to the local Google Drive folder puts the file into Google Drive's local sync queue; cloud synchronization depends on the Google Drive desktop client.

---

## 7. Running Automated Tests

To execute the full automated test suite (mocked Notion, SQLite, PIL, and Job Manager):
```powershell
.\.venv\Scripts\pytest tests/ -v
```

To run the end-to-end integration test:
```powershell
.\.venv\Scripts\python.exe run_integration_test.py
```

---

## 8. Failure Recovery & Error Handling

- **Interrupted Generations:** If the computer restarts or crashes during image generation, the task is flagged as `NEEDS_ATTENTION` upon startup reconciliation, preventing duplicate submissions.
- **Interrupted Downloads:** If an interruption occurs while downloading or validating, the temporary file is purged and the task is safely reset to `PENDING`.
- **Rate Limits:** If Gemini displays a usage limit message ("reached your limit"), the worker immediately halts new submissions, sets the job to `PAUSED`, and alerts the user to resume after cooldown.
- **Corruption Auditing:** Running `Run Disk Reconciliation Audit` in Tab 3 verifies every completed image against physical disk storage using PIL decodability and SHA-256 hash checks.

---

## 9. Verified Features & Limitations

### Verified Features
- Multi-channel support with isolated visual instructions and reference images.
- Notion API integration with cursor pagination, nested blocks, and section filtering.
- Deterministic file naming (`image-001.png`, `image-002.png`) with collision avoidance.
- True atomic image validation (PIL decodability, dimensions, format, SHA-256).
- SQLite ACID transactional job management with Compare-And-Swap status updates.
- Streamlit interactive dashboard with non-blocking background worker thread.
- Single-instance concurrency protection preventing duplicate browser workers.
- Google Drive for Desktop synchronized directory support.

### Known Limitations
- Free Gemini web interface speed and rate limits are determined by Google; batch generation of 60+ images should maintain reasonable inter-prompt delays (default: 4s).
- Live browser automation depends on DOM elements; selector diagnostics automatically record HTML snapshots in `logs/diagnostics/` if Gemini updates its layout.
