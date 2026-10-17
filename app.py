import os
import sys
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
import streamlit as st
from dotenv import load_dotenv

# Load local environment variables
load_dotenv()

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from src.logging_config import logger
from src.channel_config import ChannelRegistry, ChannelConfig
from src.notion_client import NotionClient, NotionPageSummary
from src.prompt_parser import PromptParser, ParseResult
from src.image_validator import ImageValidator
from src.image_downloader import ImageDownloader
from src.drive_sync import DriveSyncManager, StorageStatus
from src.progress_store import ProgressStore, TaskStatus, JobStatus
from src.recovery import RecoveryManager
from src.job_manager import JobManager
from src.gemini_browser import GeminiBrowserAdapter

# Streamlit Page Setup
st.set_page_config(
    page_title="YouTube Image Automation",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)


def get_channel_registry() -> ChannelRegistry:
    return ChannelRegistry(Path("config/channels.yaml"))


@st.cache_resource
def get_progress_store() -> ProgressStore:
    return ProgressStore(Path("data/jobs.db"))


@st.cache_resource
def get_downloader() -> ImageDownloader:
    output_dir = os.getenv("LOCAL_OUTPUT_PATH", "output")
    return ImageDownloader(output_dir)


@st.cache_resource
def get_drive_sync() -> DriveSyncManager:
    mode = os.getenv("STORAGE_MODE", "local")
    local_p = os.getenv("LOCAL_OUTPUT_PATH", "output")
    gdrive_p = os.getenv("GDRIVE_SYNC_PATH", "G:\\My Drive\\YouTube-Automation")
    return DriveSyncManager(mode=mode, local_path=local_p, gdrive_path=gdrive_p)


@st.cache_resource
def get_job_manager() -> JobManager:
    store = get_progress_store()
    channels = get_channel_registry()
    dl = get_downloader()
    sync = get_drive_sync()
    browser = GeminiBrowserAdapter(
        profile_dir=Path("data/browser_profile"),
        headless=os.getenv("BROWSER_HEADLESS", "false").lower() == "true",
        gemini_url=os.getenv("GEMINI_URL", "https://gemini.google.com/app"),
    )
    return JobManager(
        store=store,
        channel_registry=channels,
        downloader=dl,
        drive_sync=sync,
        browser_adapter=browser,
    )


def main():
    registry = get_channel_registry()
    store = get_progress_store()
    drive_sync = get_drive_sync()
    job_mgr = get_job_manager()
    recovery = RecoveryManager(store)

    # --- SIDEBAR CONFIGURATION & STATUS ---
    st.sidebar.title("YouTube Automation")
    st.sidebar.markdown("**Channels & Setup**")

    # Channel Selector
    channels = registry.list_all()
    channel_names = {c.name: c.id for c in channels}
    selected_name = st.sidebar.selectbox("Active Channel", list(channel_names.keys()))
    active_channel: ChannelConfig = registry.get(channel_names[selected_name])

    if active_channel:
        st.sidebar.caption(f"Theme: {active_channel.theme}")
        st.sidebar.caption(f"Subfolder: `{active_channel.output_subfolder}`")

    st.sidebar.divider()

    # Storage Mode Selector
    storage_modes = {
        "Automatic (Desktop -> API Fallback)": "automatic",
        "Google Drive for desktop": "gdrive_desktop",
        "Google Drive API (Direct Cloud Upload)": "gdrive_api",
        "Local Storage Only": "local",
    }
    mode_keys = list(storage_modes.keys())
    saved_mode = os.getenv("STORAGE_MODE", "automatic").lower()
    default_idx = 0
    if saved_mode == "local":
        default_idx = 3
    elif saved_mode == "gdrive_api":
        default_idx = 2
    elif saved_mode in ("gdrive_desktop", "gdrive"):
        default_idx = 1

    selected_mode_label = st.sidebar.selectbox("Storage Mode", mode_keys, index=default_idx)
    drive_sync.mode = storage_modes[selected_mode_label]

    storage_info = drive_sync.inspect_storage()
    if storage_info.is_accessible and storage_info.is_writable:
        st.sidebar.success(f"{storage_info.status_label}")
    else:
        st.sidebar.warning(f"{storage_info.status_label}")
    st.sidebar.caption(f"Active Root: `{storage_info.configured_path}`")
    if storage_info.free_space_gb is not None:
        st.sidebar.caption(f"Free Disk Space: {storage_info.free_space_gb} GB")

    st.sidebar.divider()

    # Notion Connection Status
    notion_token = os.getenv("NOTION_API_KEY", "")
    notion_client = NotionClient(api_key=notion_token)
    if notion_client.is_configured:
        st.sidebar.success("Notion Token: Configured")
    else:
        st.sidebar.warning("Notion Token: Missing (.env)")

    # Browser Session Status
    browser_profile = Path("data/browser_profile")
    if browser_profile.exists() and any(browser_profile.iterdir()):
        st.sidebar.info("Gemini Profile: Initialized")
    else:
        st.sidebar.caption("Gemini Profile: Needs Google Login")

    if st.sidebar.button("🌐 Open Browser to Login to Gemini", help="Opens the Gemini browser so you can log into your Google account."):
        try:
            job_mgr.browser_adapter.launch()
            st.sidebar.success("Browser window opened! Sign in to Google, then run your job.")
        except Exception as e:
            st.sidebar.error(f"Failed to open browser: {e}")

    # --- MAIN VIEW TABS ---
    tab_select, tab_control, tab_diagnostics = st.tabs([
        "1. Video Selection & Prompts",
        "2. Job Control & Live Progress",
        "3. System Diagnostics & Recovery",
    ])

    # ----------------------------------------------------
    # TAB 1: VIDEO SELECTION & PROMPT EXTRACTION
    # ----------------------------------------------------
    with tab_select:
        st.subheader("Select Notion Video Page & Extract Prompts")

        col_search, col_direct = st.columns([2, 1])
        with col_search:
            page_options = {}
            if notion_client.is_configured:
                if st.button("Load Shared Pages from Notion"):
                    try:
                        with st.spinner("Fetching shared pages from Notion..."):
                            pages = notion_client.list_shared_pages()
                            st.session_state["notion_pages"] = pages
                    except Exception as e:
                        st.error(f"Failed to query Notion: {e}")

            cached_pages = st.session_state.get("notion_pages", [])
            if cached_pages:
                page_options = {f"{p.title} ({p.id[:8]}...)": p.id for p in cached_pages}
                selected_page_label = st.selectbox("Select Shared Video Page", list(page_options.keys()))
                selected_page_id = page_options.get(selected_page_label, "")
            else:
                selected_page_id = ""

        with col_direct:
            manual_page_id = st.text_input(
                "Or Enter Notion Page ID / URL",
                value=os.getenv("NOTION_DEFAULT_PAGE_ID", ""),
                help="Paste the 32-character Notion page ID or page URL."
            )
            if manual_page_id:
                clean_id = manual_page_id.split("/")[-1].split("?")[0].replace("-", "")
                if len(clean_id) >= 32:
                    selected_page_id = clean_id[-32:]

        # Reference Images Management
        st.markdown("#### 🎨 Channel Reference Image")
        found_refs, missing_refs = active_channel.check_references()
        
        ref_col1, ref_col2 = st.columns([1, 2])
        with ref_col1:
            if found_refs and found_refs[0].exists() and found_refs[0].stat().st_size > 100:
                st.image(str(found_refs[0]), caption=f"Active: {found_refs[0].name}", width=220)
            elif found_refs:
                st.info(f"File placeholder exists: `{found_refs[0].name}`")
            elif missing_refs:
                st.warning(f"Configured file missing: `{missing_refs[0]}`")
            else:
                st.caption("No reference image uploaded yet.")

        with ref_col2:
            req_toggle = st.checkbox(
                "Enforce reference image during generation for this channel",
                value=active_channel.requires_reference,
                key=f"req_ref_toggle_{active_channel.id}",
                help="When enabled, Gemini will be seeded with this reference image before generating video scenes.",
            )
            if req_toggle != active_channel.requires_reference:
                registry.set_requires_reference(active_channel.id, req_toggle)
                st.success(f"Reference enforcement updated to: {req_toggle}")
                st.rerun()

            uploaded_ref = st.file_uploader(
                f"Upload reference image for {active_channel.name}",
                type=["png", "jpg", "jpeg", "webp"],
                key=f"ref_uploader_{active_channel.id}",
                help="Upload an approved character model, style frame, or color palette image.",
            )
            if uploaded_ref is not None:
                save_dir = Path("assets/references")
                save_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(uploaded_ref.name).suffix.lower() or ".png"
                target_filename = f"{active_channel.id}_reference{ext}"
                target_path = save_dir / target_filename

                with open(target_path, "wb") as f:
                    f.write(uploaded_ref.getbuffer())

                registry.update_reference(
                    channel_id=active_channel.id,
                    relative_path=f"assets/references/{target_filename}",
                    requires_reference=True,
                )
                st.success(f"Reference image uploaded and set to `{target_filename}`!")
                st.rerun()

        # Extract Prompts
        st.divider()
        if selected_page_id:
            if st.button("Extract Prompts from Notion Page", type="primary"):
                try:
                    with st.spinner("Retrieving blocks and parsing prompts..."):
                        blocks = notion_client.get_all_page_blocks(selected_page_id, recursive=True)
                        page_title = notion_client.get_page_title(selected_page_id)
                        st.session_state["video_title"] = page_title

                        parser = PromptParser(
                            prompt_section_heading=active_channel.notion.prompt_section_heading,
                            custom_pattern=active_channel.notion.prompt_pattern,
                        )
                        result: ParseResult = parser.parse_blocks(blocks)
                        st.session_state["parse_result"] = result
                        st.session_state["extracted_page_id"] = selected_page_id
                except Exception as e:
                    st.error(f"Error extracting prompts: {e}")

        # Display Extracted Prompts & Preview
        parse_result: Optional[ParseResult] = st.session_state.get("parse_result")
        if parse_result:
            st.markdown(f"### Extracted Prompts for: **{st.session_state.get('video_title', 'Video')}**")
            st.info(f"Total Extracted Prompts: **{parse_result.total_count}**")

            if parse_result.detected_section:
                st.caption(f"Detected Prompt Section: `{parse_result.detected_section}`")

            if parse_result.warnings:
                for w in parse_result.warnings:
                    st.warning(w)

            if parse_result.ambiguous_blocks:
                with st.expander(f"Review Ambiguous Blocks ({len(parse_result.ambiguous_blocks)})", expanded=False):
                    for amb in parse_result.ambiguous_blocks:
                        st.write(f"- Reason: {amb['reason']} | Text: `{amb['block'].get('text', '')[:100]}`")

            # Table Preview
            preview_data = []
            for p in parse_result.prompts:
                final_p = active_channel.prepare_prompt(p.text)
                preview_data.append({
                    "Index": p.index,
                    "Label": p.label,
                    "Raw Prompt": p.text,
                    "Final Prompt (With Channel Style)": final_p,
                    "Target File": active_channel.format_filename(p.index),
                })

            st.dataframe(preview_data, use_container_width=True)

            # Create Job Button
            st.divider()
            col_create_job, col_dest = st.columns([1, 2])
            with col_create_job:
                if st.button("Create Automation Job", type="primary"):
                    try:
                        clean_page_id = st.session_state["extracted_page_id"]
                        video_title = st.session_state.get("video_title", "Video")
                        video_id = f"vid-{active_channel.id}-{clean_page_id[:8]}"
                        job_id = f"job-{active_channel.id}-{clean_page_id[:8]}-{int(time.time())}"

                        # Upsert metadata in SQLite
                        store.upsert_channel(
                            channel_id=active_channel.id,
                            name=active_channel.name,
                            theme=active_channel.theme,
                            output_subfolder=active_channel.output_subfolder,
                        )
                        store.upsert_video(
                            video_id=video_id,
                            channel_id=active_channel.id,
                            notion_page_id=clean_page_id,
                            title=video_title,
                        )

                        # Prepare prompt task dictionaries
                        prompt_records = []
                        for p in parse_result.prompts:
                            prompt_records.append({
                                "index": p.index,
                                "label": p.label,
                                "raw_prompt": p.text,
                                "final_prompt": active_channel.prepare_prompt(p.text),
                            })

                        store.create_job(
                            job_id=job_id,
                            video_id=video_id,
                            channel_id=active_channel.id,
                            prompts=prompt_records,
                        )

                        st.session_state["active_job_id"] = job_id
                        st.success(f"Job created successfully: `{job_id}`. Switch to Tab 2 to run.")
                    except Exception as e:
                        st.error(f"Failed to create job: {e}")

            with col_dest:
                dest_path = drive_sync.get_active_local_root() / active_channel.output_subfolder / st.session_state.get('video_title', 'Video')
                st.caption(f"Destination: `{dest_path}`")

    # ----------------------------------------------------
    # TAB 2: JOB CONTROL & LIVE PROGRESS
    # ----------------------------------------------------
    with tab_control:
        st.subheader("Job Controls & Execution")

        # Select Job
        existing_jobs = store.list_jobs(limit=25)
        job_options = {f"{j['id']} ({j['video_title']} - {j['status']})": j['id'] for j in existing_jobs}

        current_job_id = st.session_state.get("active_job_id")
        selected_job_key = st.selectbox(
            "Select Job to Run / Monitor",
            list(job_options.keys()) if job_options else ["None"],
            index=0 if not current_job_id else (
                [k for k, v in job_options.items() if v == current_job_id] or [list(job_options.keys())[0]]
            ).index([k for k, v in job_options.items() if v == current_job_id][0]) if any(v == current_job_id for v in job_options.values()) else 0,
        )

        active_job_id = job_options.get(selected_job_key)
        if active_job_id:
            job_data = store.get_job(active_job_id)
            tasks = store.get_tasks_for_job(active_job_id)

            # Controls Row
            c1, c2, c3, c4, c5 = st.columns(5)
            worker_state = job_mgr.state

            with c1:
                start_disabled = (worker_state.is_running and not worker_state.is_paused) or job_data["status"] == JobStatus.COMPLETED
                btn_label = "▶️ Resume Job" if worker_state.is_paused else "🚀 Start / Run Job"
                if st.button(btn_label, disabled=start_disabled, type="primary"):
                    if worker_state.is_paused:
                        job_mgr.resume_job()
                        st.success("Resumed job execution.")
                    else:
                        started = job_mgr.start_job(active_job_id)
                        if started:
                            st.success(f"Job started: `{active_job_id}`.")
                        else:
                            st.warning(f"Could not start: {job_mgr.state.error_message or 'Worker is busy'}")
                    st.rerun()

            with c2:
                pause_disabled = not worker_state.is_running or worker_state.is_paused
                if st.button("⏸️ Pause", disabled=pause_disabled):
                    job_mgr.pause_job()
                    st.rerun()

            with c3:
                resume_disabled = not worker_state.is_paused
                if st.button("▶️ Resume", disabled=resume_disabled):
                    job_mgr.resume_job()
                    st.rerun()

            with c4:
                stop_disabled = not worker_state.is_running
                if st.button("⏹️ Stop Safely", disabled=stop_disabled):
                    job_mgr.stop_job()
                    st.rerun()

            with c5:
                if st.button("🔄 Retry Failed Tasks"):
                    reset_count = recovery.reset_failed_tasks(active_job_id)
                    st.info(f"Reset {reset_count} failed tasks to PENDING.")
                    st.rerun()

            # Worker Status & Action Banners
            st.divider()
            status_lower = (worker_state.status_message or "").lower()
            if worker_state.is_paused and ("sign in" in status_lower or "action required" in status_lower or "auth" in status_lower):
                st.error("🔑 **Google / Gemini Login Required**")
                st.markdown("The automated browser opened Gemini, but you are not logged into a Google account yet. Sign in once so Gemini can generate images:")
                col_auth1, col_auth2 = st.columns([1, 1])
                with col_auth1:
                    if st.button("🌐 Open / Show Gemini Window to Sign In", type="primary", key="tab2_auth_window_btn"):
                        try:
                            job_mgr.browser_adapter.launch()
                            st.info("Browser window opened on your screen! Sign in to Google, then click Resume below.")
                        except Exception as e:
                            st.error(f"Failed to open browser: {e}")
                with col_auth2:
                    if st.button("✅ I have logged in, Resume Job", key="tab2_auth_resume_btn"):
                        job_mgr.resume_job()
                        st.rerun()
            elif worker_state.is_running:
                st.info(f"Worker Active: **{worker_state.status_message}**")
            elif worker_state.is_paused:
                st.warning(f"Worker Paused: **{worker_state.status_message}**")
            else:
                st.caption(f"Worker State: Idle | Job Status: **{job_data['status']}**")

            # Metrics
            total = len(tasks)
            completed = sum(1 for t in tasks if t["status"] == TaskStatus.COMPLETED)
            failed = sum(1 for t in tasks if t["status"] in (TaskStatus.FAILED, TaskStatus.NEEDS_ATTENTION))
            pending = total - completed - failed
            progress_pct = (completed / total) if total > 0 else 0.0

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Total Prompts", total)
            m2.metric("Completed Images", completed)
            m3.metric("Pending Tasks", pending)
            m4.metric("Failed / Flagged", failed)
            m5.metric("Completion", f"{int(progress_pct * 100)}%")

            st.progress(progress_pct)

            # Task List Table
            st.markdown("#### Task Progress Table")
            task_rows = []
            for t in tasks:
                task_rows.append({
                    "Index": t["prompt_index"],
                    "Label": t["label"],
                    "Status": t["status"],
                    "Retries": t["retry_count"],
                    "File Verified": "Yes" if t.get("file_path") else "No",
                    "Storage Status": t.get("cloud_sync_status") or "None",
                    "Last Error": t.get("last_error") or "",
                })
            st.dataframe(task_rows, width="stretch")

            # Live Auto-Refresh while background worker is running
            if worker_state.is_running and not worker_state.is_paused:
                time.sleep(2)
                st.rerun()

    # ----------------------------------------------------
    # TAB 3: SYSTEM DIAGNOSTICS & RECOVERY
    # ----------------------------------------------------
    with tab_diagnostics:
        st.subheader("System Diagnostics & Crash Reconciliation")

        # Reconciliation section
        if active_job_id:
            st.markdown(f"**Audit & Reconcile Job:** `{active_job_id}`")
            if st.button("Run Disk Reconciliation Audit"):
                with st.spinner("Inspecting files on disk against SQLite database..."):
                    report = recovery.reconcile_job(active_job_id)
                    st.success(report.summary_message)
                    if report.corrupted_or_missing:
                        st.error(f"Missing/Corrupted Files: {report.corrupted_or_missing}")
                    if report.flagged_for_attention:
                        st.warning(f"Tasks Needing Attention: {report.flagged_for_attention}")
            st.divider()

        # Browser Diagnostics
        st.markdown("#### Browser Diagnostics")
        col_b1, col_b2 = st.columns(2)
        with col_b1:
            st.write("Launch browser window to authenticate with Google/Gemini manually:")
            if st.button("Open Gemini Browser for Manual Login"):
                try:
                    job_mgr.browser_adapter.launch()
                    st.success("Browser launched! Please sign in to Gemini in the opened browser window.")
                except Exception as e:
                    st.error(f"Failed to launch browser: {e}")

        with col_b2:
            if st.button("Check Current Gemini Auth Status"):
                try:
                    is_auth, msg = job_mgr.browser_adapter.check_auth_status()
                    if is_auth:
                        st.success(f"Auth Status: {msg}")
                    else:
                        st.warning(f"Auth Status: {msg}")
                except Exception as e:
                    st.error(f"Auth check failed: {e}")

        # Google Drive API Fallback & Authorization
        st.divider()
        st.markdown("#### Google Drive API Authorization (Method 2 Fallback)")
        api_client = drive_sync.api_client
        if api_client.is_authorized():
            st.success("Google Drive API: Authorized (OAuth token present)")
        else:
            st.warning("Google Drive API: Not Authorized. Configure credentials to enable direct API cloud uploads.")

        col_g1, col_g2 = st.columns(2)
        with col_g1:
            st.write("Authorize application via OAuth 2.0 (localhost redirect):")
            auth_url, auth_err = api_client.get_authorization_url()
            if auth_url:
                st.markdown(f"[🔗 Open Google Authorization Page]({auth_url})")
                if st.button("Listen for OAuth Callback (localhost:8080)"):
                    with st.spinner("Listening for OAuth response on http://localhost:8080/oauth2callback ..."):
                        ok, msg = api_client.start_local_auth_server(timeout_seconds=90)
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
            else:
                st.caption(f"Setup Notice: {auth_err}")

        with col_g2:
            if st.button("Test Google Drive API Connection"):
                if api_client.is_authorized():
                    token = api_client.get_valid_access_token()
                    if token:
                        folder_id = api_client.get_or_create_folder("YouTube-Automation")
                        if folder_id:
                            st.success(f"Drive API Connected! Root folder ID: `{folder_id}`")
                        else:
                            st.error("Failed to query/create root folder on Drive.")
                    else:
                        st.error("Access token expired and could not be refreshed.")
                else:
                    st.error("Drive API is not yet authorized.")

        # Storage Diagnostics
        st.divider()
        st.markdown("#### Storage Diagnostic Report")
        st.json(storage_info.model_dump())


if __name__ == "__main__":
    main()
