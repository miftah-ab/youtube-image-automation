import pytest
from src.prompt_parser import PromptParser, PromptItem, ParseResult


def test_parse_paired_label_and_text_blocks():
    """Tests IMAGE 001 followed by prompt body in separate paragraph block."""
    blocks = [
        {"type": "heading_1", "text": "Video Script & Prompts"},
        {"type": "heading_2", "text": "Prompts"},
        {"type": "paragraph", "text": "IMAGE 001", "id": "b-1"},
        {"type": "paragraph", "text": "A close-up shot of a vintage wristwatch on a wooden desk, soft natural light.", "id": "b-2"},
        {"type": "paragraph", "text": "IMAGE 002", "id": "b-3"},
        {"type": "paragraph", "text": "Dramatic wide shot of a crowded city intersection in the rain at twilight.", "id": "b-4"},
    ]

    parser = PromptParser(prompt_section_heading="Prompts")
    result = parser.parse_blocks(blocks)

    assert result.is_confident is True
    assert result.total_count == 2
    assert result.prompts[0].index == 1
    assert result.prompts[0].label == "IMAGE 001"
    assert result.prompts[0].text == "A close-up shot of a vintage wristwatch on a wooden desk, soft natural light."
    assert result.prompts[1].index == 2
    assert result.prompts[1].label == "IMAGE 002"
    assert result.prompts[1].text == "Dramatic wide shot of a crowded city intersection in the rain at twilight."


def test_parse_inline_prefixed_prompts():
    """Tests 'IMAGE 001: Prompt text' in a single block."""
    blocks = [
        {"type": "heading_2", "text": "Prompts"},
        {"type": "paragraph", "text": "IMAGE 001: Golden hour sunset over rolling green hills.", "id": "p-1"},
        {"type": "paragraph", "text": "IMAGE 002: Modern minimalist living room with floor to ceiling windows.", "id": "p-2"},
        {"type": "paragraph", "text": "3. A stack of classic leatherbound books with a steaming cup of tea.", "id": "p-3"},
    ]

    parser = PromptParser(prompt_section_heading="Prompts")
    result = parser.parse_blocks(blocks)

    assert result.total_count == 3
    assert result.prompts[0].index == 1
    assert result.prompts[0].text == "Golden hour sunset over rolling green hills."
    assert result.prompts[1].index == 2
    assert result.prompts[1].text == "Modern minimalist living room with floor to ceiling windows."
    assert result.prompts[2].index == 3
    assert result.prompts[2].text == "A stack of classic leatherbound books with a steaming cup of tea."


def test_ignores_script_and_notes_sections():
    """Ensures parser isolates prompts section and never treats script / notes as image prompts."""
    blocks = [
        {"type": "heading_1", "text": "Voiceover Script"},
        {"type": "paragraph", "text": "Welcome back to the channel. Today we explore cognitive dissonance.", "id": "s-1"},
        {"type": "paragraph", "text": "Note: Remember to mention the Stanford experiment.", "id": "n-1"},
        {"type": "heading_2", "text": "Prompts"},
        {"type": "paragraph", "text": "IMAGE 001: Split brain illustration showing rational vs emotional thought.", "id": "pr-1"},
        {"type": "heading_2", "text": "Production Notes"},
        {"type": "paragraph", "text": "Sound effects required for timestamp 02:45.", "id": "pn-1"},
    ]

    parser = PromptParser(prompt_section_heading="Prompts")
    result = parser.parse_blocks(blocks)

    assert result.total_count == 1
    assert result.prompts[0].index == 1
    assert result.prompts[0].text == "Split brain illustration showing rational vs emotional thought."


def test_reports_ambiguous_blocks_inside_prompt_section():
    """Ensures unlabelled potential prompt blocks trigger ambiguous warning and do not silently disappear."""
    blocks = [
        {"type": "heading_2", "text": "Prompts"},
        {"type": "paragraph", "text": "IMAGE 001: Valid prompt text here.", "id": "b-1"},
        {"type": "paragraph", "text": "Mysterious unlabelled paragraph that might be a prompt but lacks index number.", "id": "b-amb"},
    ]

    parser = PromptParser(prompt_section_heading="Prompts")
    result = parser.parse_blocks(blocks)

    assert result.total_count == 1
    assert len(result.ambiguous_blocks) == 1
    assert result.is_confident is False
    assert "ambiguous" in result.warnings[0].lower()


def test_nested_blocks_traversal():
    """Ensures nested children (e.g. inside toggle blocks or columns) are extracted in order."""
    blocks = [
        {"type": "heading_2", "text": "Prompts"},
        {
            "type": "toggle",
            "text": "Act 1 Prompts",
            "id": "tog-1",
            "children": [
                {"type": "paragraph", "text": "IMAGE 001: Scene 1 wide angle.", "id": "c-1"},
                {"type": "paragraph", "text": "IMAGE 002: Scene 2 medium shot.", "id": "c-2"},
            ]
        }
    ]

    parser = PromptParser(prompt_section_heading="Prompts")
    result = parser.parse_blocks(blocks)

    assert result.total_count == 2
    assert result.prompts[0].text == "Scene 1 wide angle."
    assert result.prompts[1].text == "Scene 2 medium shot."
