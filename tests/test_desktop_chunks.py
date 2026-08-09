"""Speech Dispatcher SSML is split at __spd_ index marks, without an XML parser."""

from desktop.chunks import Chunk, split_marked, strip_ssml


class TestStripSsml:
    def test_plain_text_untouched(self):
        assert strip_ssml("Plain text.") == "Plain text."

    def test_tags_removed(self):
        assert strip_ssml("Hello <emphasis>world</emphasis>.") == "Hello world."

    def test_self_closing_tag_removed(self):
        assert strip_ssml('<speak>Hi <break time="500ms"/>there</speak>') == "Hi there"

    def test_named_entities_decoded(self):
        assert strip_ssml("&lt;a&gt; &amp; &quot;b&quot; &apos;c&apos;") == '<a> & "b" \'c\''

    def test_unknown_entity_kept(self):
        assert strip_ssml("Unknown &copy; stays.") == "Unknown &copy; stays."

    def test_unterminated_tag_dropped(self):
        assert strip_ssml("Keep <unfinished") == "Keep "


class TestSplitMarked:
    def test_splits_at_marks(self):
        ssml = (
            '<speak>Hello there. <mark name="__spd_0"/> '
            'How are you? <mark name="__spd_1"/></speak>'
        )
        assert split_marked(ssml) == [
            Chunk("Hello there.", "__spd_0"),
            Chunk("How are you?", "__spd_1"),
        ]

    def test_trailing_text_without_mark(self):
        ssml = '<speak>First. <mark name="__spd_0"/> No final mark</speak>'
        assert split_marked(ssml) == [
            Chunk("First.", "__spd_0"),
            Chunk("No final mark", None),
        ]

    def test_unmarked_message_is_one_chunk(self):
        assert split_marked("<speak>Just one sentence</speak>") == [
            Chunk("Just one sentence", None)
        ]

    def test_plain_text_message(self):
        assert split_marked("no tags at all") == [Chunk("no tags at all", None)]

    def test_empty_message_yields_nothing(self):
        assert split_marked("<speak>   </speak>") == []

    def test_blank_segments_dropped_but_marks_preserved(self):
        ssml = (
            '<speak>One. <mark name="__spd_0"/> <mark name="__spd_1"/> Two.</speak>'
        )
        assert split_marked(ssml) == [Chunk("One.", "__spd_0"), Chunk("Two.", None)]

    def test_entities_decoded_inside_chunks(self):
        ssml = '<speak>Tom &amp; Jerry. <mark name="__spd_0"/></speak>'
        assert split_marked(ssml) == [Chunk("Tom & Jerry.", "__spd_0")]

    def test_long_chunk_split_at_whitespace(self):
        words = " ".join(["word"] * 50)
        chunks = split_marked(f"<speak>{words}</speak>", max_chars=40)
        assert len(chunks) > 1
        assert all(len(chunk.text) <= 40 for chunk in chunks)
        assert " ".join(chunk.text for chunk in chunks) == words

    def test_long_chunk_keeps_mark_on_last_piece_only(self):
        words = " ".join(["word"] * 30)
        ssml = f'<speak>{words} <mark name="__spd_7"/></speak>'
        chunks = split_marked(ssml, max_chars=40)
        assert chunks[-1].mark == "__spd_7"
        assert all(chunk.mark is None for chunk in chunks[:-1])

    def test_unbreakable_run_is_hard_split(self):
        run = "x" * 100
        chunks = split_marked(f"<speak>{run}</speak>", max_chars=40)
        assert all(len(chunk.text) <= 40 for chunk in chunks)
        assert "".join(chunk.text for chunk in chunks) == run

    def test_non_spd_marks_are_not_split_points(self):
        ssml = '<speak>A <mark name="custom"/> B. <mark name="__spd_0"/></speak>'
        assert split_marked(ssml) == [Chunk("A  B.", "__spd_0")]
