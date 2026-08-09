"""Byte-level conformance with the Speech Dispatcher module protocol."""

import array
import io

from desktop import protocol


def _io(input_bytes=b""):
    stdin = io.BytesIO(input_bytes)
    stdout = io.BytesIO()
    return protocol.ProtocolIO(stdin, stdout), stdout


class TestEscapeAudio:
    def test_plain_bytes_unchanged(self):
        assert protocol.escape_audio(b"\x01\x02\x03") == b"\x01\x02\x03"

    def test_newline_escaped(self):
        assert protocol.escape_audio(b"\n") == b"\x7d\x2a"

    def test_escape_byte_escaped(self):
        assert protocol.escape_audio(b"\x7d") == b"\x7d\x5d"

    def test_mixed_payload(self):
        assert protocol.escape_audio(b"a\nb\x7dc") == b"a\x7d\x2ab\x7d\x5dc"

    def test_empty(self):
        assert protocol.escape_audio(b"") == b""

    def test_escaped_output_contains_no_raw_newline(self):
        payload = bytes(range(256))
        assert b"\n" not in protocol.escape_audio(payload)


class TestReading:
    def test_read_line_strips_newline(self):
        io_, _ = _io(b"SPEAK\n")
        assert io_.read_line() == "SPEAK"

    def test_read_line_returns_none_at_eof(self):
        io_, _ = _io(b"")
        assert io_.read_line() is None

    def test_read_line_decodes_utf8(self):
        io_, _ = _io("caf\u00e9\n".encode("utf-8"))
        assert io_.read_line() == "caf\u00e9"

    def test_read_line_survives_invalid_utf8(self):
        io_, _ = _io(b"\xff\xfe\n")
        assert io_.read_line() is not None

    def test_read_data_block_stops_at_dot(self):
        io_, _ = _io(b"rate=10\npitch=-5\n.\ntrailing\n")
        assert io_.read_data_block() == ["rate=10", "pitch=-5"]
        assert io_.read_line() == "trailing"

    def test_read_data_block_unstuffs_dots(self):
        io_, _ = _io(b"..\n.hidden\n.\n")
        assert io_.read_data_block() == [".", "hidden"]

    def test_read_data_block_at_eof_returns_partial(self):
        io_, _ = _io(b"a=1\n")
        assert io_.read_data_block() == ["a=1"]

    def test_read_message_joins_lines(self):
        io_, _ = _io(b"<speak>one\ntwo</speak>\n.\n")
        assert io_.read_message() == "<speak>one\ntwo</speak>"

    def test_read_message_empty_block(self):
        io_, _ = _io(b".\n")
        assert io_.read_message() == ""


class TestParseSettings:
    def test_parses_key_values(self):
        assert protocol.parse_settings(["rate=10", "pitch=-5"]) == {
            "rate": "10",
            "pitch": "-5",
        }

    def test_keeps_equals_in_value(self):
        assert protocol.parse_settings(["voice=a=b"]) == {"voice": "a=b"}

    def test_ignores_lines_without_separator(self):
        assert protocol.parse_settings(["garbage", "rate=1"]) == {"rate": "1"}

    def test_empty_list(self):
        assert protocol.parse_settings([]) == {}


class TestWriting:
    def test_send_appends_newline_and_flushes(self):
        io_, out = _io()
        io_.send(protocol.OK_SPEAKING)
        assert out.getvalue() == b"200 OK SPEAKING\n"

    def test_send_multiline_prefixes_detail_lines(self):
        io_, out = _io()
        io_.send_multiline(["299-ready"], protocol.OK_LOADED)
        assert out.getvalue() == b"299-ready\n299 OK LOADED SUCCESSFULLY\n"

    def test_events_use_exact_codes(self):
        io_, out = _io()
        io_.event_begin()
        io_.event_end()
        io_.event_stop()
        io_.event_pause()
        assert out.getvalue() == b"701 BEGIN\n702 END\n703 STOP\n704 PAUSE\n"

    def test_index_mark_is_two_lines(self):
        io_, out = _io()
        io_.index_mark("__spd_3")
        assert out.getvalue() == b"700-__spd_3\n700 INDEX MARK\n"

    def test_send_voices_tab_separated_then_terminator(self):
        io_, out = _io()
        io_.send_voices([("en-US-AvaMultilingualNeural", "en-US", "none")])
        assert out.getvalue() == (
            b"200-en-US-AvaMultilingualNeural\ten-US\tnone\n200 OK VOICE LIST SENT\n"
        )

    def test_send_voices_empty_reports_cannot_list(self):
        io_, out = _io()
        io_.send_voices([])
        assert out.getvalue() == b"304 CANT LIST VOICES\n"


class TestSendAudio:
    def _pcm(self, samples):
        return array.array("h", samples).tobytes()

    def test_headers_describe_the_track(self):
        io_, out = _io()
        io_.send_audio(self._pcm([1, 2, 3, 4]), sample_rate=24000)
        text = out.getvalue()
        assert b"705-bits=16\n" in text
        assert b"705-num_channels=1\n" in text
        assert b"705-sample_rate=24000\n" in text
        assert b"705-num_samples=4\n" in text
        assert b"705-big_endian=" in text

    def test_frame_structure_and_terminator(self):
        io_, out = _io()
        io_.send_audio(self._pcm([1]), sample_rate=24000)
        payload = out.getvalue()
        assert b"705-AUDIO\x00" in payload
        assert payload.endswith(b"705 AUDIO\n")

    def test_payload_is_escaped(self):
        io_, out = _io()
        # 0x0a0a as one sample would otherwise emit a raw newline.
        io_.send_audio(b"\x0a\x0a", sample_rate=24000)
        body = out.getvalue().split(b"705-AUDIO\x00", 1)[1]
        assert b"\x7d\x2a" in body

    def test_large_buffer_is_split_into_frames(self):
        io_, out = _io()
        io_.send_audio(b"\x00\x01" * 12000, sample_rate=24000)
        assert out.getvalue().count(b"705 AUDIO\n") == 3

    def test_frame_never_exceeds_chunk_limit(self):
        io_, out = _io()
        io_.send_audio(b"\x00\x01" * 12000, sample_rate=24000)
        for header in out.getvalue().split(b"705-num_samples=")[1:]:
            count = int(header.split(b"\n", 1)[0])
            assert count * 2 <= protocol.MAX_AUDIO_CHUNK_BYTES

    def test_empty_pcm_writes_nothing(self):
        io_, out = _io()
        io_.send_audio(b"", sample_rate=24000)
        assert out.getvalue() == b""
