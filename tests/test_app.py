import json
import unittest
from unittest.mock import patch

import app as app_module


class FakeResponse:
    def __init__(self, output_text):
        self.output_text = output_text


class FakeResponses:
    def __init__(self, output_text):
        self.output_text = output_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.output_text)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeChatResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeChatCompletions:
    def __init__(self, output_text):
        self.output_text = output_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeChatResponse(self.output_text)


class FakeChat:
    def __init__(self, output_text):
        self.completions = FakeChatCompletions(output_text)


class FakeClient:
    def __init__(self, output_text):
        self.responses = FakeResponses(output_text)
        self.chat = FakeChat(output_text)


class AppTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_parse_prediction_form_clamps_numeric_values(self):
        query, temperature, number_of_results, model = app_module.parse_prediction_form(
            {
                "query": "Spike RBD",
                "temperature": "2.5",
                "number_of_results": "99",
                "model": "gpt-5.5",
            }
        )

        self.assertEqual(query, "Spike RBD")
        self.assertEqual(temperature, 1.0)
        self.assertEqual(number_of_results, 5)
        self.assertEqual(model, "gpt-5.5")

    def test_parse_prediction_form_rejects_unknown_model(self):
        with self.assertRaisesRegex(ValueError, "supported GPT model"):
            app_module.parse_prediction_form(
                {
                    "query": "Spike RBD",
                    "temperature": "0.5",
                    "number_of_results": "3",
                    "model": "not-a-real-model",
                }
            )

    def test_parse_binding_response_preserves_semicolons_inside_fields(self):
        text = json.dumps(
            {
                "binders": [
                    {
                        "name": "ACE2",
                        "confidence_score": 98,
                        "protein_function": "Receptor; peptidase",
                        "interaction_function": "Viral entry; attachment",
                        "reasoning": "Directly binds RBD; extensive evidence.",
                    }
                ],
                "warning": "",
            }
        )

        binders, warning = app_module.parse_binding_response(text, 5)

        self.assertEqual(warning, "")
        self.assertEqual(binders[0]["protein_function"], "Receptor; peptidase")
        self.assertEqual(binders[0]["interaction_function"], "Viral entry; attachment")
        self.assertEqual(binders[0]["reasoning"], "Directly binds RBD; extensive evidence.")

    def test_get_binding_partners_uses_responses_api_with_schema(self):
        fake_client = FakeClient(
            json.dumps(
                {
                    "binders": [
                        {
                            "name": "ACE2",
                            "confidence_score": 98,
                            "protein_function": "Receptor",
                            "interaction_function": "Viral entry",
                            "reasoning": "Known interaction.",
                        }
                    ],
                    "warning": "",
                }
            )
        )

        binders, _ = app_module.get_binding_partners(
            "Spike RBD",
            0.4,
            1,
            "gpt-5.5",
            client=fake_client,
        )

        self.assertEqual(binders[0]["name"], "ACE2")
        call = fake_client.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.5")
        self.assertEqual(call["text"]["format"]["name"], "binding_partner_results")
        self.assertEqual(call["reasoning"]["effort"], "low")
        self.assertNotIn("temperature", call)
        self.assertFalse(call["store"])

    def test_get_binding_partners_uses_chat_completions_for_legacy_models(self):
        fake_client = FakeClient(
            json.dumps(
                {
                    "binders": [
                        {
                            "name": "SOS1",
                            "confidence_score": 85,
                            "protein_function": "GEF",
                            "interaction_function": "Activates RAS",
                            "reasoning": "Canonical RAS regulator.",
                        }
                    ],
                    "warning": "",
                }
            )
        )

        binders, _ = app_module.get_binding_partners(
            "Ras",
            0.7,
            1,
            "gpt-3.5-turbo",
            client=fake_client,
        )

        self.assertEqual(binders[0]["name"], "SOS1")
        self.assertEqual(fake_client.responses.calls, [])
        call = fake_client.chat.completions.calls[0]
        self.assertEqual(call["model"], "gpt-3.5-turbo")
        self.assertEqual(call["temperature"], 0.7)
        self.assertIn("max_tokens", call)

    def test_model_options_match_dropdown_order(self):
        model_ids = [model["id"] for model in app_module.MODEL_OPTIONS]

        self.assertEqual(
            model_ids,
            ["gpt-5.5", "gpt-5.4-mini", "gpt-4", "gpt-4-1106-preview", "gpt-3.5-turbo"],
        )
        self.assertEqual(app_module.MODEL_CONFIGS["gpt-5.5"]["api"], "responses")
        self.assertEqual(app_module.MODEL_CONFIGS["gpt-5.4-mini"]["api"], "responses")
        self.assertEqual(app_module.MODEL_CONFIGS["gpt-3.5-turbo"]["api"], "chat")

    def test_index_rejects_unknown_model_before_openai_call(self):
        with patch.object(app_module, "get_binding_partners") as get_binding_partners:
            response = self.client.post(
                "/",
                data={
                    "query": "Spike RBD",
                    "temperature": "0.5",
                    "number_of_results": "3",
                    "model": "not-a-real-model",
                },
            )

        self.assertEqual(response.status_code, 400)
        get_binding_partners.assert_not_called()

    def test_repository_root_files_are_not_served_as_static_assets(self):
        self.assertEqual(self.client.get("/app.py").status_code, 404)
        self.assertEqual(self.client.get("/.env").status_code, 404)

    def test_brand_assets_are_served_from_assets_route(self):
        logo_response = self.client.get("/assets/logo-horizontal-tight.png")
        favicon_response = self.client.get("/favicon.ico")

        try:
            self.assertEqual(logo_response.status_code, 200)
            self.assertEqual(favicon_response.status_code, 200)
            self.assertEqual(logo_response.mimetype, "image/png")
            self.assertIn(favicon_response.mimetype, {"image/vnd.microsoft.icon", "image/x-icon"})
        finally:
            logo_response.close()
            favicon_response.close()

    def test_homepage_includes_brand_head_links_and_logo(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('/assets/favicon.svg', html)
        self.assertIn('/assets/apple-touch-icon.png', html)
        self.assertIn('/assets/site.webmanifest', html)
        self.assertIn('/assets/logo-horizontal-tight.png', html)


if __name__ == "__main__":
    unittest.main()
