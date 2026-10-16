import pytest
from src.prompt_parser import PromptParser, ParseResult
from src.channel_config import ChannelRegistry


SAMPLE_NOTION_TEXT = """
TAW-001 — 60-Shot Visual Plan
TAW-001 — Complete Visual Plan for the Updated Narration
Script source: [TAW-001 — 3.6% Social Security Raise: Calculate Your 2027 Check](https://app.notion.com/p/TAW-001-3-6-Social-Security-Raise-Calculate-Your-2027-Check-3faad75b1d9d81aca34bf35b1a32df88?pvs=21)
Visual production rules — complete-image standard
60 purposeful, distinct 16:9 stills mapped to the revised narration. Timecodes remain provisional until narration is recorded and timed.
Use only Deep Navy #14263D and Warm Ivory #F5F1E8, with tactile paper/ink texture and controlled natural-looking light. No gradients, neon, or unapproved colors.
Every image must be a finished, self-contained visual.

Shot 01 — 00:00
Exact narration cue: “If your Social Security check is $2,000 a month,”
Visual approach: semi-realistic lifestyle
Purpose / viewer takeaway: Make the viewer recognize a real household money decision immediately.
Ready-to-paste image prompt: Use the provided reference image for the character. A lived-in American kitchen at early morning, medium-wide cinematic frame from just behind the referenced character’s shoulder: a monthly benefits letter, calculator, reading glasses, unopened mail, and a plain ceramic mug spread across a warm ivory table.
Continuity / edit: Use character only if it is the approved reference; slow push toward the statement.

Shot 02 — 00:04
Exact narration cue: “the new 3.6% raise works out to about $72 extra.”
Visual approach: explainer diagram
Purpose / viewer takeaway: Make the increase feel concrete, not abstract.
Ready-to-paste image prompt: Use the provided reference image for the character. A bold dimensional paper infographic on a deep navy background: one large warm-ivory benefit block with a smaller ivory add-on tile visibly attached at its right edge.
Continuity / edit: No character.

Final visual audit — corrected
60 narration-mapped prompts revised.
All 60 ready-to-paste prompts begin with the exact first sentence “Use the provided reference image for the character.”
"""


def test_parse_real_american_wallet_notion_structure():
    parser = PromptParser()
    res: ParseResult = parser.parse_raw_text(SAMPLE_NOTION_TEXT)

    assert res.is_confident is True
    assert res.total_count == 2

    # Shot 1
    shot1 = res.prompts[0]
    assert shot1.index == 1
    assert "Shot 01" in shot1.label
    assert shot1.timecode == "00:00"
    assert shot1.narration_cue == "If your Social Security check is $2,000 a month,"
    assert shot1.visual_approach == "semi-realistic lifestyle"
    assert shot1.text.startswith("Use the provided reference image for the character.")

    # Shot 2
    shot2 = res.prompts[1]
    assert shot2.index == 2
    assert "Shot 02" in shot2.label
    assert shot2.timecode == "00:04"
    assert shot2.narration_cue == "the new 3.6% raise works out to about $72 extra."
    assert shot2.visual_approach == "explainer diagram"
    assert shot2.text.startswith("Use the provided reference image for the character.")


def test_american_wallet_prompt_preparation_no_duplicate_instructions():
    reg = ChannelRegistry()
    ch = reg.get("american_wallet")
    assert ch is not None

    raw_p = "Use the provided reference image for the character. A lived-in American kitchen, Deep Navy #14263D and Warm Ivory #F5F1E8, tactile paper texture."
    prepared = ch.prepare_prompt(raw_p)

    # Since prompt already includes style tokens, it must not blindly duplicate them
    assert prepared == raw_p
