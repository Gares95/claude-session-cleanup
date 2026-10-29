import datetime as dt
import json
import os

from claude_session_cleanup import transcript
from support import NOW, IsolatedTestCase, iso, session_uuid, requires_symlinks

SID = session_uuid(1)
# Platform-native absolute paths: recorded cwds are Windows paths on Windows.
ROOT = os.path.abspath(os.sep)
P = os.path.join(ROOT, "p")
P_SUB = os.path.join(P, "sub")
START, MIDDLE, END = (os.path.join(ROOT, name) for name in ("start", "middle", "end"))


class TranscriptTests(IsolatedTestCase):
    def write(self, lines, *, trailing_newline=True) -> str:
        path = os.path.join(self.tmp, "t.jsonl")
        body = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body + ("\n" if trailing_newline else ""))
        return path

    def user(self, text, *, cwd=P, when=NOW, **extra):
        record = {"type": "user", "sessionId": SID, "cwd": cwd, "timestamp": iso(when),
                  "message": {"role": "user", "content": text}}
        record.update(extra)
        return record

    def test_extracts_cwd_timestamps_and_labels(self) -> None:
        path = self.write([
            {"type": "permission-mode", "sessionId": SID},
            self.user("first", cwd=P, when=NOW - dt.timedelta(days=3), entrypoint="cli", version="2.1.300"),
            self.user("later", cwd=P_SUB, when=NOW - dt.timedelta(days=1), version="2.1.310"),
        ])
        summary = transcript.summarize_transcript(path, SID)
        self.assertEqual(P, summary.first_cwd)
        self.assertEqual(P_SUB, summary.last_cwd)
        self.assertEqual(NOW - dt.timedelta(days=3), summary.created_at)
        self.assertEqual(NOW - dt.timedelta(days=1), summary.content_last_at)
        self.assertEqual(("cli",), summary.entrypoints)
        self.assertEqual(("2.1.300", "2.1.310"), summary.versions)
        self.assertEqual("first", summary.first_prompt)

    def test_title_records_last_one_wins_and_foreign_records_are_ignored(self) -> None:
        path = self.write([
            self.user("p"),
            {"type": "custom-title", "customTitle": "old", "sessionId": SID},
            {"type": "custom-title", "customTitle": "new", "sessionId": SID},
            {"type": "custom-title", "customTitle": "spoofed", "sessionId": session_uuid(2)},
            {"type": "ai-title", "aiTitle": "generated", "sessionId": SID},
        ])
        summary = transcript.summarize_transcript(path, SID)
        self.assertEqual("new", summary.custom_title)
        self.assertEqual("generated", summary.ai_title)
        self.assertEqual(1, summary.foreign_records)

    def test_meta_and_command_prompts_are_skipped(self) -> None:
        path = self.write([
            self.user("<command-name>/model</command-name>"),
            self.user("ignored", isMeta=True),
            self.user([{"type": "text", "text": "real question"}]),
        ])
        self.assertEqual("real question", transcript.summarize_transcript(path, SID).first_prompt)

    def test_continued_in_links_are_found_anywhere_and_validated(self) -> None:
        filler = [self.user("x" * 100) for _ in range(300)]
        path = self.write(filler + [
            {"type": "continued-in", "continuedInSessionId": session_uuid(7), "sessionId": SID},
            {"type": "continued-in", "continuedInSessionId": "not-a-uuid", "sessionId": SID},
            {"type": "continued-in", "continuedInSessionId": SID, "sessionId": SID},
        ] + filler)
        summary = transcript.summarize_transcript(path, SID, chunk_size=997)
        self.assertEqual((session_uuid(7),), summary.continued_in)

    def test_marker_inside_large_content_is_not_trusted(self) -> None:
        big = self.user('"continued-in" ' + "y" * (transcript.MAX_METADATA_LINE + 10))
        big["type"] = "continued-in"
        big["continuedInSessionId"] = session_uuid(9)
        path = self.write([self.user("p"), big])
        self.assertEqual((), transcript.summarize_transcript(path, SID).continued_in)

    def test_malformed_oversized_and_truncated_lines_are_counted(self) -> None:
        huge = json.dumps(self.user("z" * (transcript.MAX_LINE + 5)))
        path = self.write([self.user("p"), "{not json", huge, '{"type": "user", "cwd": ' + json.dumps(P)],
                          trailing_newline=False)
        summary = transcript.summarize_transcript(path, SID, chunk_size=4096)
        self.assertEqual(1, summary.parse_errors)
        self.assertEqual(1, summary.oversized_lines)
        self.assertTrue(summary.incomplete_last_line)
        self.assertEqual(P, summary.first_cwd)

    def test_tail_window_supplies_latest_cwd_in_large_files(self) -> None:
        lines = [self.user("p", cwd=START)] + [self.user("x" * 1000, cwd=MIDDLE) for _ in range(400)]
        lines.append(self.user("end", cwd=END, when=NOW))
        summary = transcript.summarize_transcript(self.write(lines), SID)
        self.assertEqual(START, summary.first_cwd)
        self.assertEqual(END, summary.last_cwd)

    def test_invalid_timestamps_are_ignored(self) -> None:
        path = self.write([self.user("p", when=NOW) | {"timestamp": "2026-10-25T12:00:00"},
                           self.user("q") | {"timestamp": "garbage"}])
        summary = transcript.summarize_transcript(path, SID)
        self.assertIsNone(summary.created_at)
        self.assertIsNone(summary.content_last_at)

    @requires_symlinks
    def test_symlink_and_missing_files_are_not_read(self) -> None:
        target = self.write([self.user("p")])
        link = os.path.join(self.tmp, "link.jsonl")
        os.symlink(target, link)
        if hasattr(os, "O_NOFOLLOW"):
            self.assertIsNone(transcript.summarize_transcript(link, SID))
        self.assertIsNone(transcript.summarize_transcript(os.path.join(self.tmp, "missing"), SID))

    def test_fifo_does_not_block(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        path = os.path.join(self.tmp, "fifo.jsonl")
        os.mkfifo(path)
        self.assertIsNone(transcript.summarize_transcript(path, SID))
