"""Unit tests for Telegram utility functions and robust plain-text retry fallback.

Run from the project root with:
    PYTHONPATH=src python3 -m unittest discover tests -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import telegram_utils


class TestTelegramUtils(unittest.TestCase):

    def setUp(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "mock_token"

    @patch("telegram_utils.requests.post")
    def test_send_message_success_first_try(self, mock_post):
        # Setup: first post succeeds
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = mock_response

        telegram_utils.send_message(12345, "Hello world", parse_mode="Markdown")

        # Verify: only 1 post was made with the Markdown parse mode
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["parse_mode"], "Markdown")
        self.assertEqual(kwargs["json"]["text"], "Hello world")

    @patch("telegram_utils.requests.post")
    def test_send_message_markdown_fails_fallback_succeeds(self, mock_post):
        # Setup: first post fails with bad Markdown entity error, second post succeeds without parse_mode
        mock_resp_fail = MagicMock()
        mock_resp_fail.json.return_value = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: can't parse entities: Can't find end of the entity",
        }

        mock_resp_success = MagicMock()
        mock_resp_success.json.return_value = {"ok": True, "result": {}}

        mock_post.side_effect = [mock_resp_fail, mock_resp_success]

        telegram_utils.send_message(12345, "Hello world with unclosed _italic", parse_mode="Markdown")

        # Verify: 2 posts were made
        self.assertEqual(mock_post.call_count, 2)
        
        # First call has parse_mode="Markdown"
        first_kwargs = mock_post.call_args_list[0][1]
        self.assertEqual(first_kwargs["json"]["parse_mode"], "Markdown")

        # Second call does not have parse_mode key
        second_kwargs = mock_post.call_args_list[1][1]
        self.assertNotIn("parse_mode", second_kwargs["json"])
        self.assertEqual(second_kwargs["json"]["text"], "Hello world with unclosed _italic")

    @patch("telegram_utils.requests.post")
    def test_send_message_fails_both(self, mock_post):
        # Setup: both calls fail (e.g. other error like chat not found)
        mock_resp_fail = MagicMock()
        mock_resp_fail.json.return_value = {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot was blocked by the user",
        }
        mock_post.return_value = mock_resp_fail

        with patch("telegram_utils.logging.getLogger") as mock_logger:
            mock_log = MagicMock()
            mock_logger.return_value = mock_log
            telegram_utils.send_message(12345, "Hello", parse_mode="Markdown")

            # Verify: only 1 call is made because it's not a Markdown parsing error
            mock_post.assert_called_once()
            mock_log.error.assert_called_once()
