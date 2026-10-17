import re
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from src.logging_config import logger


class PromptItem(BaseModel):
    index: int
    label: str
    text: str
    timecode: Optional[str] = None
    narration_cue: Optional[str] = None
    visual_approach: Optional[str] = None
    source_block_id: Optional[str] = None
    section: Optional[str] = None
    is_ambiguous: bool = False
    warning: Optional[str] = None


class ParseResult(BaseModel):
    prompts: List[PromptItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    ambiguous_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    detected_section: Optional[str] = None
    total_count: int = 0
    is_confident: bool = True
    error_message: Optional[str] = None


class PromptParser:
    """Configurable parser for Notion page blocks and plain text prompt sheets,
    supporting real-world visual production plans (Shot-based, Ready-to-paste prompts,
    IMAGE 001 blocks, and numbered lists).
    """

    SHOT_HEADER_PATTERN = re.compile(
        r"^Shot\s*0*(\d+)(?:\s*[\u2014\-:]\s*(\d{1,2}:\d{2}))?", re.IGNORECASE
    )

    PROMPT_LABEL_PATTERNS = [
        # "Ready-to-paste image prompt: [text]"
        re.compile(r"^Ready-to-paste image prompt\s*[:.\-]?\s*(.*)", re.IGNORECASE),
        # "IMAGE 001", "IMAGE 1"
        re.compile(r"^(?:IMAGE|Image)\s*#?\s*0*(\d+)\b[:.\-]?\s*(.*)", re.IGNORECASE),
        # "PROMPT 001", "PROMPT 1"
        re.compile(r"^(?:PROMPT|Prompt)\s*#?\s*0*(\d+)\b[:.\-]?\s*(.*)", re.IGNORECASE),
        # Numbered item "1.", "1 -", "001."
        re.compile(r"^0*(\d+)\s*[\.\-\)]\s+(.*)", re.IGNORECASE),
    ]

    METADATA_PATTERNS = {
        "narration_cue": re.compile(r"^Exact narration cue\s*[:.\-]?\s*[\u201c\"]?(.*?)[\u201d\"]?$", re.IGNORECASE),
        "visual_approach": re.compile(r"^Visual approach\s*[:.\-]?\s*(.*)", re.IGNORECASE),
        "purpose": re.compile(r"^Purpose\s*(?:/\s*viewer takeaway)?\s*[:.\-]?\s*(.*)", re.IGNORECASE),
        "continuity": re.compile(r"^Continuity\s*(?:/\s*edit)?\s*[:.\-]?\s*(.*)", re.IGNORECASE),
    }

    IGNORE_HEADINGS = [
        "visual production rules",
        "production rules",
        "final visual audit",
        "visual audit",
        "script source",
        "narration script",
    ]

    def __init__(
        self,
        prompt_section_heading: Optional[str] = None,
        custom_pattern: Optional[str] = None,
    ):
        self.prompt_section_heading = prompt_section_heading.strip() if prompt_section_heading else None
        self.custom_pattern = re.compile(custom_pattern, re.IGNORECASE) if custom_pattern else None

    def parse_blocks(self, blocks: List[Dict[str, Any]]) -> ParseResult:
        result = ParseResult()

        if not blocks:
            result.error_message = "No blocks provided from Notion page."
            result.is_confident = False
            return result

        # Flatten nested blocks while tracking hierarchy
        flattened_items = self._flatten_blocks(blocks)

        # Detect Shot-based structure (like the 60-shot visual plan)
        has_shot_headers = any(self.SHOT_HEADER_PATTERN.match(it["text"]) for it in flattened_items)

        if has_shot_headers:
            prompts, ambiguous, warnings = self._extract_shot_based_prompts(flattened_items)
            result.detected_section = "Shot-Based Visual Plan"
        else:
            # Fall back to standard section or sequential pattern extraction
            target_items = flattened_items
            if self.prompt_section_heading:
                filtered_items, detected_name = self._filter_by_section(flattened_items, self.prompt_section_heading)
                if filtered_items:
                    target_items = filtered_items
                    result.detected_section = detected_name
                else:
                    result.warnings.append(
                        f"Specified prompt section '{self.prompt_section_heading}' not found. Scanning full page."
                    )

            prompts, ambiguous, warnings = self._extract_standard_prompts(target_items)

        result.prompts = prompts
        result.ambiguous_blocks = ambiguous
        result.warnings.extend(warnings)
        result.total_count = len(prompts)

        if ambiguous:
            result.is_confident = False
            result.warnings.append(
                f"Found {len(ambiguous)} ambiguous block(s) that might be prompts but lacked clear labels."
            )

        if result.total_count == 0:
            result.is_confident = False
            result.error_message = "No image prompts could be identified in the Notion content."

        return result

    def parse_raw_text(self, text: str) -> ParseResult:
        lines = [line.strip() for line in text.splitlines()]
        synthetic_blocks = []
        for i, line in enumerate(lines):
            if line:
                synthetic_blocks.append({
                    "id": f"synthetic-{i}",
                    "type": "paragraph",
                    "text": line,
                    "children": [],
                })
        return self.parse_blocks(synthetic_blocks)

    def _flatten_blocks(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        flattened = []

        def _traverse(node_list: List[Dict[str, Any]], current_heading: Optional[str] = None):
            for block in node_list:
                b_type = block.get("type", "unknown")
                text_content = self._extract_text_from_block(block)

                if b_type in ("heading_1", "heading_2", "heading_3"):
                    current_heading = text_content.strip()

                item = {
                    "id": block.get("id", ""),
                    "type": b_type,
                    "text": text_content.strip(),
                    "heading": current_heading,
                    "raw": block,
                }
                flattened.append(item)

                children = block.get("children", [])
                if children:
                    _traverse(children, current_heading)

        _traverse(blocks)
        return flattened

    def _extract_text_from_block(self, block: Dict[str, Any]) -> str:
        b_type = block.get("type", "")
        if b_type in block and isinstance(block[b_type], dict):
            rich_texts = block[b_type].get("rich_text", [])
            if rich_texts:
                return "".join([rt.get("plain_text", "") for rt in rich_texts])
        if "text" in block:
            return block["text"]
        return ""

    def _extract_shot_based_prompts(
        self, items: List[Dict[str, Any]]
    ) -> Tuple[List[PromptItem], List[Dict[str, Any]], List[str]]:
        """Extracts prompts from structured shot-by-shot visual plans (e.g. 'Shot 01 — 00:00')."""
        prompts: List[PromptItem] = []
        ambiguous: List[Dict[str, Any]] = []
        warnings: List[str] = []

        current_shot_idx: Optional[int] = None
        current_timecode: Optional[str] = None
        current_narration: Optional[str] = None
        current_approach: Optional[str] = None
        in_ignored_section = False

        i = 0
        n = len(items)

        while i < n:
            item = items[i]
            text = item["text"]
            b_id = item["id"]

            if not text:
                i += 1
                continue

            # Check if entering an ignored section like rules or audit
            lower_text = text.lower()
            if any(ign in lower_text for ign in self.IGNORE_HEADINGS):
                in_ignored_section = True
                i += 1
                continue

            # Check for Shot Header: "Shot 01 — 00:00"
            m_shot = self.SHOT_HEADER_PATTERN.match(text)
            if m_shot:
                in_ignored_section = False
                current_shot_idx = int(m_shot.group(1))
                current_timecode = m_shot.group(2)
                current_narration = None
                current_approach = None
                i += 1
                continue

            if in_ignored_section:
                i += 1
                continue

            # Check metadata fields
            m_narr = self.METADATA_PATTERNS["narration_cue"].match(text)
            if m_narr:
                current_narration = m_narr.group(1).strip()
                i += 1
                continue

            m_appr = self.METADATA_PATTERNS["visual_approach"].match(text)
            if m_appr:
                current_approach = m_appr.group(1).strip()
                i += 1
                continue

            # Check for "Ready-to-paste image prompt:"
            if text.lower().startswith("ready-to-paste image prompt"):
                # Extract text after prefix
                prompt_text = re.sub(r"^ready-to-paste image prompt\s*[:.\-]?\s*", "", text, flags=re.IGNORECASE).strip()
                if not prompt_text and i + 1 < n:
                    # Body is on the next line / block
                    prompt_text = items[i + 1]["text"].strip()
                    i += 1

                if prompt_text:
                    shot_idx = current_shot_idx or (len(prompts) + 1)
                    label = f"Shot {shot_idx:02d}"
                    if current_timecode:
                        label += f" ({current_timecode})"

                    prompts.append(PromptItem(
                        index=shot_idx,
                        label=label,
                        text=prompt_text,
                        timecode=current_timecode,
                        narration_cue=current_narration,
                        visual_approach=current_approach,
                        source_block_id=b_id,
                    ))
                else:
                    ambiguous.append({
                        "block": item,
                        "reason": "Found 'Ready-to-paste image prompt' label but prompt body was empty.",
                    })
                i += 1
                continue

            # Check if an inline prompt starts with "Use the provided reference image" without explicit label
            if text.startswith("Use the provided reference image") and len(text) > 80:
                shot_idx = current_shot_idx or (len(prompts) + 1)
                prompts.append(PromptItem(
                    index=shot_idx,
                    label=f"Shot {shot_idx:02d}",
                    text=text,
                    timecode=current_timecode,
                    narration_cue=current_narration,
                    visual_approach=current_approach,
                    source_block_id=b_id,
                ))
                i += 1
                continue

            i += 1

        return prompts, ambiguous, warnings

    def _extract_standard_prompts(
        self, items: List[Dict[str, Any]]
    ) -> Tuple[List[PromptItem], List[Dict[str, Any]], List[str]]:
        """Extracts prompts using IMAGE 001, Prompt 1, or numbered patterns."""
        prompts: List[PromptItem] = []
        ambiguous: List[Dict[str, Any]] = []
        warnings: List[str] = []

        i = 0
        n = len(items)

        while i < n:
            item = items[i]
            text = item["text"]
            b_id = item["id"]
            heading = item.get("heading")

            if not text:
                i += 1
                continue

            matched = False
            for pattern in self.PROMPT_LABEL_PATTERNS:
                m = pattern.match(text)
                if m:
                    matched = True
                    groups = m.groups()
                    idx_val = len(prompts) + 1
                    remainder = ""

                    if len(groups) == 1:
                        remainder = groups[0].strip()
                    elif len(groups) >= 2:
                        idx_val = int(groups[0]) if groups[0].isdigit() else len(prompts) + 1
                        remainder = groups[1].strip()

                    if remainder:
                        prompts.append(PromptItem(
                            index=idx_val,
                            label=f"IMAGE {idx_val:03d}",
                            text=remainder,
                            source_block_id=b_id,
                            section=heading,
                        ))
                    else:
                        if i + 1 < n and items[i + 1]["text"]:
                            next_text = items[i + 1]["text"].strip()
                            prompts.append(PromptItem(
                                index=idx_val,
                                label=f"IMAGE {idx_val:03d}",
                                text=next_text,
                                source_block_id=b_id,
                                section=heading,
                            ))
                            i += 1
                        else:
                            ambiguous.append({
                                "block": item,
                                "reason": f"Found label '{text}' but body was empty.",
                            })
                    break

            if not matched:
                lower = text.lower()
                if any(w in lower for w in ["note:", "script:", "voiceover:", "narration:"]):
                    pass
                elif len(text) > 40 and not text.startswith("#"):
                    ambiguous.append({
                        "block": item,
                        "reason": "Text block has no recognized IMAGE / Prompt label.",
                    })

            i += 1

        return prompts, ambiguous, warnings

    def _filter_by_section(
        self, items: List[Dict[str, Any]], heading_target: str
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        target_lower = heading_target.lower()
        in_section = False
        section_items = []
        detected_heading = None
        section_level = 0
        heading_level_map = {"heading_1": 1, "heading_2": 2, "heading_3": 3}

        for item in items:
            b_type = item["type"]
            text = item["text"]

            if b_type in heading_level_map:
                current_level = heading_level_map[b_type]
                if target_lower in text.lower():
                    in_section = True
                    detected_heading = text
                    section_level = current_level
                    continue
                elif in_section and current_level <= section_level:
                    in_section = False

            if in_section:
                section_items.append(item)

        return section_items, detected_heading
