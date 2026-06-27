import re
from pathlib import Path


def _extract_split_regex() -> re.Pattern[str]:
    background = Path(__file__).resolve().parents[1] / "extension" / "background.js"
    text = background.read_text(encoding="utf-8")
    match = re.search(r"const sentences = text\.match\(/(.+?)/g\) \|\| \[text\];", text)
    assert match, "splitSentences regex not found"
    return re.compile(match.group(1))


def split_sentences_like_extension(text: str) -> list[str]:
    pattern = _extract_split_regex()
    matches = pattern.findall(text) or [text]
    return [item.strip() for item in matches if item.strip()]


def test_split_sentences_preserves_newline_delimited_selection():
    text = "First line\nSecond line\nThird line"
    assert split_sentences_like_extension(text) == ["First line", "Second line", "Third line"]


def test_split_sentences_keeps_heading_before_punctuated_lines():
    text = "Heading\nThis is sentence one.\nThis is sentence two."
    assert split_sentences_like_extension(text) == [
        "Heading",
        "This is sentence one.",
        "This is sentence two.",
    ]


def test_split_sentences_keeps_cjk_newline_fragments():
    text = "第一句\n第二句。\n第三句"
    assert split_sentences_like_extension(text) == ["第一句", "第二句。", "第三句"]


def test_split_sentences_preserves_punctuation_splitting():
    text = "One. Two! Three?"
    assert split_sentences_like_extension(text) == ["One.", "Two!", "Three?"]
