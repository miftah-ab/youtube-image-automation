from pathlib import Path
from typing import Dict, List, Optional, Tuple
import yaml
from pydantic import BaseModel, Field

from src.logging_config import logger


class NotionChannelSettings(BaseModel):
    prompt_section_heading: Optional[str] = "Prompts"
    prompt_pattern: Optional[str] = r"^(?:IMAGE|PROMPT|Image|Prompt)\s*0*(\d+)[:.]?\s*(.*)"


class ChannelConfig(BaseModel):
    id: str
    name: str
    theme: str
    description: str
    output_subfolder: str
    filename_pattern: str = "image-{index:03d}.{ext}"
    requires_reference: bool = False
    reference_images: List[str] = Field(default_factory=list)
    visual_instructions: str = ""
    apply_visual_instructions_mode: str = "append_if_missing"  # "never", "always", "append_if_missing"
    notion: NotionChannelSettings = Field(default_factory=NotionChannelSettings)

    def resolve_reference_paths(self, base_dir: Optional[Path] = None) -> List[Path]:
        """Resolves reference image paths relative to project root or absolute."""
        root = base_dir or Path.cwd()
        resolved = []
        for ref in self.reference_images:
            p = Path(ref)
            if not p.is_absolute():
                p = (root / p).resolve()
            resolved.append(p)
        return resolved

    def check_references(self, base_dir: Optional[Path] = None) -> Tuple[List[Path], List[Path]]:
        """Returns tuple of (found_paths, missing_paths)."""
        found = []
        missing = []
        for p in self.resolve_reference_paths(base_dir):
            if p.exists() and p.is_file():
                found.append(p)
            else:
                missing.append(p)
        return found, missing

    def format_filename(self, index: int, ext: str = "png") -> str:
        clean_ext = ext.lstrip(".")
        return self.filename_pattern.format(index=index, ext=clean_ext)

    def prepare_prompt(self, raw_prompt: str) -> str:
        """Applies channel visual instructions according to configured mode."""
        prompt = raw_prompt.strip()
        if not self.visual_instructions or self.apply_visual_instructions_mode == "never":
            return prompt

        if self.apply_visual_instructions_mode == "always":
            return f"{prompt} -- {self.visual_instructions}"

        # "append_if_missing": Check if prompt already contains detailed style keywords
        prompt_lower = prompt.lower()
        instructions_lower = self.visual_instructions.lower()
        # Simple heuristic check: if key tokens from visual instructions already exist in prompt
        key_tokens = [w for w in instructions_lower.replace(",", "").split() if len(w) > 4]
        overlap = sum(1 for token in key_tokens if token in prompt_lower)
        if overlap >= 2:
            # Already heavily specified
            return prompt

        return f"{prompt} -- Style: {self.visual_instructions}"


class ChannelRegistry:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or Path("config/channels.yaml")
        self.channels: Dict[str, ChannelConfig] = {}
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            logger.warning(f"Channels config file not found at {self.config_path}")
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        channels_dict = data.get("channels", {})
        self.channels.clear()
        for ch_id, ch_data in channels_dict.items():
            ch_data["id"] = ch_id
            self.channels[ch_id] = ChannelConfig(**ch_data)
        logger.info(f"Loaded {len(self.channels)} channel configurations: {list(self.channels.keys())}")

    def get(self, channel_id: str) -> Optional[ChannelConfig]:
        return self.channels.get(channel_id)

    def list_all(self) -> List[ChannelConfig]:
        return list(self.channels.values())

    def update_reference(self, channel_id: str, relative_path: str, requires_reference: bool = True) -> None:
        """Updates channel reference image and persists back to channels config YAML."""
        if not self.config_path.exists():
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "channels" in data and channel_id in data["channels"]:
            data["channels"][channel_id]["requires_reference"] = requires_reference
            data["channels"][channel_id]["reference_images"] = [relative_path]
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
            self.load()

    def set_requires_reference(self, channel_id: str, requires_reference: bool) -> None:
        """Toggles whether the channel requires a reference image and persists to YAML."""
        if not self.config_path.exists():
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "channels" in data and channel_id in data["channels"]:
            data["channels"][channel_id]["requires_reference"] = requires_reference
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
            self.load()
