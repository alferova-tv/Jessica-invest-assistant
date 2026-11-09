import unittest
from unittest.mock import patch

import requests

from jessica import notify
from jessica.config import Config
from pathlib import Path


class NotifyLoggingTest(unittest.TestCase):
    def test_network_error_does_not_log_token_or_url(self):
        token = "SYNTHETIC_PRIVATE_TOKEN"
        config = Config({"telegram": {"token": token, "chat_id": 1}}, Path("/unused"))
        error = requests.ConnectionError(f"failed https://api.telegram.org/bot{token}/sendMessage")
        with patch.object(notify.requests, "post", side_effect=error), \
                patch.object(notify.time, "sleep"), self.assertLogs(notify.log, "WARNING") as logs:
            self.assertFalse(notify.send(config, "Тест"))
        output = "\n".join(logs.output)
        self.assertIn("ConnectionError", output)
        self.assertNotIn(token, output)
        self.assertNotIn("api.telegram.org", output)
