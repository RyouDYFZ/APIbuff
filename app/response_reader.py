"""Read public CLI output, bound to a newly persisted user message.

The chat metadata sidecar is a file summary, not a completion signal. Only
the public response supplies answer text; run metadata supplies status only.
"""
import json
import math
import os
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from .exceptions import (FreebuffAuthError, FreebuffBadRequestError,
    FreebuffPaymentError, FreebuffPermissionError, FreebuffRateLimitError,
    FreebuffServerError, FreebuffInteractionRequiredError)

MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_LOG_READ = 1024 * 1024
TRANSCRIPT_FALLBACK_SECONDS = 1.0


def read_messages(directory):
    return _read_messages(directory) or []


def _read_messages(directory):
    """Distinguish a valid empty transcript from an unpublished/partial write."""
    if not directory:
        return None
    path = os.path.join(directory, "chat-messages.json")
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_TRANSCRIPT_BYTES + 1)
        if len(raw) > MAX_TRANSCRIPT_BYTES:
            raise FreebuffServerError("CLI transcript exceeds the supported size limit.")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise FreebuffServerError("Unsupported CLI transcript format.")
        return [item for item in data if isinstance(item, dict)]
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None  # An atomic rewrite may not have been published yet.


def transcript_signature(directory):
    return _file_signature(directory, "chat-messages.json")


def _file_signature(directory, filename):
    try:
        stat = os.stat(os.path.join(directory, filename))
        return stat.st_ino, stat.st_size, stat.st_mtime_ns
    except (OSError, TypeError):
        return None


class TranscriptReader:
    """Poll the metadata sidecar first, retaining only file signatures.

    CLI checkpoints publish messages before metadata, including updates to an
    existing assistant message. The sidecar is only a change hint: periodically
    inspect the transcript too, in case the CLI never refreshes the sidecar.
    """

    def __init__(self):
        self._directory = None
        self._messages_signature = None
        self._metadata_signature = None
        self._next_check = 0.0
        self._retry = False

    def read_changed(self, directory):
        """Return a new complete snapshot, or None; call off the event loop."""
        if directory != self._directory:
            self._directory = directory
            self._messages_signature = None
            self._metadata_signature = None
            self._next_check = 0.0
            self._retry = False
        metadata_signature = _file_signature(directory, "chat-meta.json")
        now = time.monotonic()
        if (not self._retry and self._messages_signature is not None
                and metadata_signature is not None
                and metadata_signature == self._metadata_signature
                and now < self._next_check):
            return None

        signature = transcript_signature(directory)
        self._metadata_signature = metadata_signature
        self._next_check = now + TRANSCRIPT_FALLBACK_SECONDS
        if signature is None:
            self._retry = True
            return None
        if not self._retry and signature == self._messages_signature:
            return None
        messages = _read_messages(directory)
        if messages is None:
            # Do not consume a notification or wait for the sidecar to change
            # again before retrying a now-complete write.
            self._retry = True
            return None
        self._messages_signature = signature
        self._retry = False
        return messages


def _matches_prompt(message, prompt):
    content = message.get("content")
    if not isinstance(content, str):
        return False
    # A submitted text-only prompt cannot include other content or attachments.
    if any(message.get(key) not in (None, [])
           for key in ("attachments", "fileAttachments", "blocks")):
        return False
    text_attachments = message.get("textAttachments")
    if text_attachments in (None, []):
        # Some CLI transcript formats store the expanded upstream content.
        return content.replace("\r\n", "\n") in (prompt, f"[Pasted Text]\n{prompt}")
    # OpenTUI converts a long paste into an attachment. The current CLI stores
    # the original empty input body and its single text attachment separately.
    return (content == "" and isinstance(text_attachments, list)
            and len(text_attachments) == 1 and isinstance(text_attachments[0], dict)
            and isinstance(text_attachments[0].get("content"), str)
            and text_attachments[0]["content"].replace("\r\n", "\n") == prompt)


def current_response(messages, baseline_ids, prompt):
    """Require an exact, new user prompt before accepting its assistant output."""
    user_index = None
    for index, message in enumerate(messages):
        if (message.get("variant") == "user" and isinstance(message.get("id"), str)
                and message["id"]
                and message["id"] not in baseline_ids
                and _matches_prompt(message, prompt)):
            if user_index is not None:
                raise FreebuffServerError("CLI persisted multiple matching user turns.")
            user_index = index
    if user_index is None:
        return None
    responses = []
    for message in messages[user_index + 1:]:
        if message.get("variant") == "user":
            break
        if message.get("variant") != "ai" or message.get("id") in baseline_ids:
            continue
        if not isinstance(message.get("id"), str) or not message["id"]:
            raise FreebuffServerError("CLI response is missing a message identity.")
        # The official streamed root shell has an ai- ID and no parentId.
        # Other ai variants include mode dividers and command notices.
        if message.get("parentId") or not message["id"].startswith("ai-"):
            continue
        if message.get("userError"):
            raise FreebuffServerError("CLI reported an error for the current response.")
        text, reasoning = [], []
        for block in message.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("ask_user", "ask-user") and not (block.get("answers") or block.get("skipped")):
                raise FreebuffInteractionRequiredError()
            # Only top-level public text blocks; no nested agent/internal state.
            if block.get("type") == "text" and isinstance(block.get("content"), str):
                (reasoning if block.get("textType") == "reasoning" else text).append(block["content"])
        if not message.get("blocks") and isinstance(message.get("content"), str):
            text.append(message["content"])
        complete = message.get("isComplete") is True
        if complete:
            metadata = message.get("metadata")
            run_state = metadata.get("runState") if isinstance(metadata, dict) else None
            output = run_state.get("output") if isinstance(run_state, dict) else None
            # Abort and restored-checkpoint handlers also mark isComplete,
            # without handleRunCompletion's committed successful run state.
            # Inspect only the discriminator, never internal output.value.
            if not isinstance(output, dict) or output.get("type") not in (
                    "allMessages", "lastMessage", "structuredOutput"):
                raise FreebuffServerError("CLI response has no successful completion record.")
        responses.append({"id": message.get("id"), "user_id": messages[user_index]["id"], "content": "".join(text),
            "reasoning_content": "".join(reasoning),
            "complete": complete})
    if len(responses) > 1:
        raise FreebuffServerError("CLI persisted multiple responses for the current turn.")
    return responses[-1] if responses else None


def read_log_records(path, position):
    try:
        size = os.path.getsize(path)
        if size < position:
            position = 0
        with open(path, "rb") as stream:
            stream.seek(position)
            raw = stream.read(MAX_LOG_READ)
        last = raw.rfind(b"\n")
        if last < 0:
            if len(raw) == MAX_LOG_READ:
                raise FreebuffServerError("CLI log record exceeds the supported size limit.")
            return position, []
        records = []
        for line in raw[:last + 1].split(b"\n"):
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except (ValueError, UnicodeDecodeError):
                continue
        return position + last + 1, records
    except FileNotFoundError:
        return position, []


def retry_after_seconds(value):
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            return max(0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, AttributeError):
            return None


def check_log_error(record):
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    status = data.get("status") or record.get("status") or data.get("statusCode") or error.get("statusCode") or error.get("status")
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None
    code = data.get("errorCode") or error.get("code") or ""
    if isinstance(data.get("error"), str):
        code = data["error"]
    message = str(record.get("msg") or "")
    lower = message.lower()
    # Do not return raw debug records: they can contain prompts or credentials.
    if status == 401 or any(x in lower for x in ("authentication failed", "invalid api key", "authentication error")):
        raise FreebuffAuthError()
    if status == 400:
        raise FreebuffBadRequestError("Upstream rejected the request.", str(code or "upstream_bad_request"))
    if status == 402:
        raise FreebuffPaymentError("Upstream requires payment or additional credit.")
    if status == 403 or code in ("free_mode_cli_required", "country_not_allowed", "anonymous_network", "residential_proxy", "ip_capped"):
        raise FreebuffPermissionError("Upstream denied this account or request.", str(code or "permission_denied"))
    if status == 429 or code in ("free_mode_rate_limited", "spend_limited", "session_limit_reached", "free_mode_capacity_deferred"):
        headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
        retry = next((value for value in (data.get("retryAfter"), data.get("retry_after"), headers.get("retry-after")) if value is not None), None)
        raise FreebuffRateLimitError("Upstream rate or quota limit reached.", str(code or "rate_limit_exceeded"), retry_after_seconds(retry))
    if status and status >= 500:
        raise FreebuffServerError("Upstream service failed.", status_code=status)
    if data.get("toolName") == "ask_user" or record.get("type") == "ask_user":
        raise FreebuffInteractionRequiredError()
    if record.get("type") == "prompt-error":
        raise FreebuffServerError("CLI prompt execution failed.")
