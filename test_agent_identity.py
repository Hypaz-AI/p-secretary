"""Test offline dell'identità nelle richieste AI."""
import json
import unittest
from unittest.mock import MagicMock, patch

import brain
import config


class AgentIdentityTests(unittest.TestCase):
    def test_identity_uses_configured_name(self):
        with patch.object(config, "AGENT_NAME", "Ada"):
            prompt = brain._with_identity("Regole operative")
        self.assertIn('Il tuo nome è "Ada"', prompt)
        self.assertTrue(prompt.endswith("Regole operative"))

    def test_chat_request_includes_identity(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}]
        }).encode()
        with patch.object(config, "AGENT_NAME", "Ada"), patch(
            "brain.urllib.request.urlopen", return_value=response
        ) as request:
            self.assertEqual(brain._chat("Regole", "Messaggio"), "ok")
        payload = json.loads(request.call_args.args[0].data)
        self.assertIn('Il tuo nome è "Ada"', payload["messages"][0]["content"])

    def test_user_dialogue_includes_identity(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "Mi chiamo Ada."}}]
        }).encode()
        with patch.object(config, "AGENT_NAME", "Ada"), patch(
            "db.communications_for_day", return_value=[]
        ), patch("db.upcoming_appointments", return_value=[]), patch(
            "db.personal_memories", return_value=[]
        ), patch("brain.remember_user", return_value="nessuna modifica necessaria"
        ), patch("db.all_rules", return_value=[]), patch(
            "connectors.gcal.is_active", return_value=False
        ), patch("brain.urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(brain.chat_with_user("Come ti chiami?"), "Mi chiamo Ada.")
        payload = json.loads(request.call_args.args[0].data)
        self.assertIn('Il tuo nome è "Ada"', payload["messages"][0]["content"])
        # Multi-event agendas must not share the old 600-token reasoning cap.
        self.assertEqual(payload["max_tokens"], 16384)


if __name__ == "__main__":
    unittest.main()