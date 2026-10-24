"""Offline regressions for exact correlation of CLI text attachments."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.exceptions import FreebuffServerError
from app.response_reader import current_response


def user(content, message_id="user-new"):
    return {"variant": "user", "id": message_id, "content": content}


def answer():
    return {
        "variant": "ai", "id": "ai-new", "content": "current answer",
        "isComplete": True,
        "metadata": {"runState": {"output": {"type": "allMessages"}}},
    }


def attached_user(prompt, message_id="user-new"):
    return {
        **user("", message_id),
        "textAttachments": [{
            "id": "attachment-new", "content": prompt,
            "preview": prompt[:80], "charCount": len(prompt.encode("utf-16-le")) // 2,
        }],
    }


class ResponseAttachmentTests(unittest.TestCase):
    def test_raw_and_attachment_prompts_match_at_long_input_sizes(self):
        for size in (2000, 2001, 32 * 1024, 128 * 1024):
            prompt = ("中文👩‍💻\n" * size)[:size]
            for content in (prompt, "[Pasted Text]\n" + prompt):
                with self.subTest(size=size, attachment=content != prompt):
                    result = current_response([user(content), answer()], set(), prompt)
                    self.assertEqual(result["content"], "current answer")
                    self.assertEqual(result["user_id"], "user-new")
                    self.assertTrue(result["complete"])

    def test_stored_single_attachment_matches_long_unicode_prompt(self):
        for size in (2001, 32 * 1024, 128 * 1024):
            prompt = ("中文👩‍💻\n" * size)[:size]
            with self.subTest(size=size):
                result = current_response([attached_user(prompt), answer()], set(), prompt)
                self.assertEqual(result["content"], "current answer")
                self.assertEqual(result["user_id"], "user-new")
                self.assertTrue(result["complete"])

    def test_stored_attachment_keeps_existing_crlf_normalization(self):
        messages = [attached_user("first line\r\nsecond line"), answer()]
        self.assertEqual(current_response(messages, set(), "first line\nsecond line")["id"], "ai-new")

    def test_stored_attachment_requires_exactly_one_text_attachment(self):
        valid_attachment = attached_user("hello")["textAttachments"][0]
        for attachments in (None, [], {}, "hello", [None], [{"content": None}],
                            [{"content": "hello extra"}], [valid_attachment, valid_attachment]):
            with self.subTest(attachments=attachments):
                message = {**user(""), "textAttachments": attachments}
                self.assertIsNone(current_response([message, answer()], set(), "hello"))

    def test_stored_attachment_requires_an_empty_input_body(self):
        for body in ("hello", " ", "[Pasted Text]\nhello", None, []):
            with self.subTest(body=body):
                message = {**attached_user("hello"), "content": body}
                self.assertIsNone(current_response([message, answer()], set(), "hello"))

    def test_extra_image_file_or_block_content_cannot_match(self):
        for message in (user("hello"), user("[Pasted Text]\nhello"), attached_user("hello")):
            for key in ("attachments", "fileAttachments", "blocks"):
                with self.subTest(message=message, key=key):
                    mixed = {**message, key: [{"content": "unexpected extra content"}]}
                    self.assertIsNone(current_response([mixed, answer()], set(), "hello"))

    def test_empty_other_attachment_lists_do_not_prevent_matching(self):
        message = {**attached_user("hello"), "attachments": [], "fileAttachments": [], "blocks": []}
        self.assertIsNotNone(current_response([message, answer()], set(), "hello"))

    def test_stored_attachment_obeys_baseline_ids_and_duplicate_detection(self):
        messages = [attached_user("hello", "user-old"), answer()]
        self.assertIsNone(current_response(messages, {"user-old"}, "hello"))
        messages = [attached_user("hello"), answer()]
        self.assertIsNone(current_response(messages, {"ai-new"}, "hello"))
        messages = [user("hello", "user-first"), attached_user("hello"), answer()]
        with self.assertRaisesRegex(FreebuffServerError, "multiple matching user turns"):
            current_response(messages, set(), "hello")

    def test_attachment_keeps_existing_crlf_normalization(self):
        prompt = "first line\nsecond line"
        messages = [user("[Pasted Text]\r\nfirst line\r\nsecond line"), answer()]
        self.assertEqual(current_response(messages, set(), prompt)["id"], "ai-new")

    def test_attachment_matches_complete_content_only(self):
        prompt = "expected prompt"
        for content in (
            "[Pasted Text]\n" + prompt + " extra",
            "extra [Pasted Text]\n" + prompt,
            "[Pasted Text]\n" + prompt[:-1],
            "[Pasted Text]\n[Pasted Text]\n" + prompt,
            "[Pasted Text] " + prompt,
            "[Pasted Text]\n" + prompt + "\n",
        ):
            with self.subTest(content=content):
                self.assertIsNone(current_response([user(content), answer()], set(), prompt))

    def test_literal_prefix_in_prompt_is_not_stripped(self):
        prompt = "[Pasted Text]\nactual prompt content"
        for content in (prompt, "[Pasted Text]\n" + prompt):
            with self.subTest(content=content):
                self.assertIsNotNone(current_response([user(content), answer()], set(), prompt))
        self.assertIsNone(current_response([user("actual prompt content"), answer()], set(), prompt))

    def test_old_attachment_user_cannot_bind_a_new_answer(self):
        messages = [user("[Pasted Text]\nhello", "user-old"), answer()]
        self.assertIsNone(current_response(messages, {"user-old"}, "hello"))

    def test_new_attachment_cannot_bind_an_old_answer(self):
        messages = [user("[Pasted Text]\nhello"), answer()]
        self.assertIsNone(current_response(messages, {"ai-new"}, "hello"))

    def test_raw_and_attachment_matches_still_reject_duplicate_turns(self):
        messages = [user("hello", "user-first"), user("[Pasted Text]\nhello"), answer()]
        with self.assertRaisesRegex(FreebuffServerError, "multiple matching user turns"):
            current_response(messages, set(), "hello")

    def test_attachment_requires_a_valid_user_identity(self):
        for identity in (None, "", 1):
            with self.subTest(identity=identity):
                messages = [user("[Pasted Text]\nhello", identity), answer()]
                self.assertIsNone(current_response(messages, set(), "hello"))


if __name__ == "__main__":
    unittest.main()
