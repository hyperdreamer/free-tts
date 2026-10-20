import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPLITTER_FILES = [
    pytest.param("script.js", id="web-preview"),
    pytest.param("extension/background.js", id="extension-speak-this"),
]


def _extract_split_regex(relative_path: str) -> re.Pattern[str]:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    match = re.search(r"const sentences = text\.match\(/(.+?)/g\) \|\| \[text\];", text)
    assert match, f"splitSentences regex not found in {relative_path}"
    return re.compile(match.group(1))


def split_sentences_like_frontend(relative_path: str, text: str) -> list[str]:
    pattern = _extract_split_regex(relative_path)
    matches = pattern.findall(text) or [text]
    return [item.strip() for item in matches if item.strip()]


@pytest.mark.parametrize("relative_path", SPLITTER_FILES)
def test_split_sentences_preserves_newline_delimited_selection(relative_path):
    text = "First line\nSecond line\nThird line"
    assert split_sentences_like_frontend(relative_path, text) == [
        "First line",
        "Second line",
        "Third line",
    ]


@pytest.mark.parametrize("relative_path", SPLITTER_FILES)
def test_split_sentences_keeps_heading_before_punctuated_lines(relative_path):
    text = "Heading\nThis is sentence one.\nThis is sentence two."
    assert split_sentences_like_frontend(relative_path, text) == [
        "Heading",
        "This is sentence one.",
        "This is sentence two.",
    ]


@pytest.mark.parametrize("relative_path", SPLITTER_FILES)
def test_split_sentences_keeps_cjk_newline_fragments(relative_path):
    text = "第一句\n第二句。\n第三句"
    assert split_sentences_like_frontend(relative_path, text) == [
        "第一句",
        "第二句。",
        "第三句",
    ]


@pytest.mark.parametrize("relative_path", SPLITTER_FILES)
def test_split_sentences_preserves_punctuation_splitting(relative_path):
    text = "One. Two! Three?"
    assert split_sentences_like_frontend(relative_path, text) == [
        "One.",
        "Two!",
        "Three?",
    ]


@pytest.mark.parametrize("relative_path", SPLITTER_FILES)
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('He said "Hello." Then left.', ['He said "Hello."', "Then left."]),
        ("“Hello.” Then left.", ["“Hello.”", "Then left."]),
        ("He said “Hello.”\nThen left.", ["He said “Hello.”", "Then left."]),
        ("「你好。」然后走了。", ["「你好。」", "然后走了。"]),
        ('Sentence one." Sentence two.', ['Sentence one."', "Sentence two."]),
        ("Sentence one.” Sentence two.", ["Sentence one.”", "Sentence two."]),
        ("Sentence one.) Sentence two.", ["Sentence one.)", "Sentence two."]),
        ('Sentence one!"\nSentence two?"', ['Sentence one!"', 'Sentence two?"']),
    ],
)
def test_split_sentences_keeps_closing_quotes_with_sentence(
    relative_path, text, expected
):
    assert split_sentences_like_frontend(relative_path, text) == expected


# --- Pipeline ownership and retained web-preview source guard --------------
@pytest.mark.parametrize(
    "case",
    [
        "W15 same_index_and_return_navigation",
        "W16 poll_identity_and_no_overlap",
        "W30 navigation_resume_attempt_binding",
    ],
)
def test_extension_pipeline_attempt_ownership(case):
    try:
        result = subprocess.run(
            [
                "node",
                "--test",
                "--test-reporter=tap",
                "--test-name-pattern",
                f"^{re.escape(case)}$",
                "tests/extension_playback.test.cjs",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"Named ownership case timed out after 120 seconds: {case}\n{error}")
    output = (result.stdout + result.stderr)[-12000:]
    assert result.returncode == 0, output
    assert re.search(rf"(?m)^ok \d+ - {re.escape(case)}\r?$", result.stdout), (
        f"Named ownership case did not run and pass: {case}\n{output}"
    )


@pytest.mark.parametrize(
    ("relative_path", "function_name", "guard_line"),
    [
        pytest.param(
            "script.js",
            "playNextPreviewSentence",
            "const startIdx = idx;",
            id="web-preview",
        ),
    ],
)
def test_pipeline_has_startIdx_guard(relative_path, function_name, guard_line):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    assert f"async function {function_name}" in text, f"{function_name} not found"
    assert guard_line in text, (
        f"startIdx guard missing in {relative_path} — add 'const startIdx = ...' at top of {function_name}"
    )
