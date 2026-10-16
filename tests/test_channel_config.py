import pytest
from pathlib import Path
from src.channel_config import ChannelRegistry, ChannelConfig


def test_channel_registry_loads_all_three_channels():
    registry = ChannelRegistry(Path("config/channels.yaml"))
    channels = registry.list_all()

    assert len(channels) == 3
    channel_ids = [c.id for c in channels]
    assert "ninety_sense" in channel_ids
    assert "case_xyz" in channel_ids
    assert "american_wallet" in channel_ids


def test_filename_formatting():
    ch = ChannelConfig(
        id="ninety_sense",
        name="Ninety Sense",
        theme="Psychology",
        description="Behavior",
        output_subfolder="Ninety-Sense",
        filename_pattern="image-{index:03d}.{ext}",
    )

    assert ch.format_filename(index=1, ext="png") == "image-001.png"
    assert ch.format_filename(index=42, ext=".webp") == "image-042.webp"
    assert ch.format_filename(index=105, ext="jpg") == "image-105.jpg"


def test_visual_instructions_application():
    ch = ChannelConfig(
        id="case_xyz",
        name="Case XYZ",
        theme="Storytelling",
        description="Cases",
        output_subfolder="Case-XYZ",
        visual_instructions="gritty documentary aesthetic, cinematic lighting",
        apply_visual_instructions_mode="append_if_missing",
    )

    # Bare prompt: should append style instructions
    bare_prompt = "A detective examining fingerprints on a glass table"
    result = ch.prepare_prompt(bare_prompt)
    assert "gritty documentary aesthetic" in result

    # Already styled prompt: should not blindly append duplicate instructions
    styled_prompt = "A detective examining fingerprints, gritty documentary aesthetic, cinematic lighting in rain"
    result_already_styled = ch.prepare_prompt(styled_prompt)
    assert result_already_styled == styled_prompt


def test_reference_image_verification(tmp_path):
    ref_file = tmp_path / "reference.png"
    ref_file.write_bytes(b"dummy image data")

    ch = ChannelConfig(
        id="test_channel",
        name="Test",
        theme="Theme",
        description="Desc",
        output_subfolder="Test",
        requires_reference=True,
        reference_images=[str(ref_file), str(tmp_path / "missing.png")],
    )

    found, missing = ch.check_references(base_dir=tmp_path)
    assert len(found) == 1
    assert found[0].name == "reference.png"
    assert len(missing) == 1
    assert missing[0].name == "missing.png"
