from unittest.mock import patch, MagicMock

from .common import ValorTestCase


class TestValorMediaHelpers(ValorTestCase):

    def test_upload_image_to_s3_uses_correct_key_and_content_type(self):
        valor = self.Valor.create({"task_id": "eval_media_123"})
        fake_bytes = b"data"
        mime = "image/jpeg"

        with patch("odoo.addons.valor.models.models.boto3.client") as client_mock:
            s3_mock = MagicMock()
            client_mock.return_value = s3_mock

            key = valor._upload_image_to_s3(1, fake_bytes, mime)

            client_mock.assert_called_once()
            s3_mock.put_object.assert_called_once()
            args, kwargs = s3_mock.put_object.call_args
            self.assertEqual(kwargs["Bucket"], "prod-grtlabs")
            self.assertTrue(key.startswith("images/eval_media_123/turn_1."))
            self.assertEqual(kwargs["ContentType"], mime)

    def test_upload_image_to_s3_defaults_to_png_when_no_mime(self):
        valor = self.Valor.create({"task_id": "eval_media_default"})
        fake_bytes = b"data"

        with patch("odoo.addons.valor.models.models.boto3.client") as client_mock:
            s3_mock = MagicMock()
            client_mock.return_value = s3_mock

            key = valor._upload_image_to_s3(2, fake_bytes, None)

            args, kwargs = s3_mock.put_object.call_args
            self.assertEqual(kwargs["ContentType"], "image/png")
            self.assertIn("turn_2", key)

    def test_ensure_image_handle_for_turn_raises_on_invalid_base64(self):
        valor = self.Valor.create({"task_id": "eval_img_invalid"})
        valor.image_1 = "not-base64"
        with self.assertRaisesRegex(Exception, "Invalid image data for this turn."):
            valor._ensure_image_handle_for_turn(1, genai_api_key="dummy")

    def test_ensure_image_handle_for_turn_noop_when_handle_already_set(self):
        """If handle_id is already set, helper should return without uploading."""
        valor = self.Valor.create({"task_id": "eval_img_skip"})
        valor.image_1 = "ZmFrZQ=="
        valor.image_handle_id_1 = "EXISTING"
        with patch("odoo.addons.valor.models.models.Valor._upload_image_to_s3") as up_mock:
            valor._ensure_image_handle_for_turn(1, genai_api_key="dummy")
        up_mock.assert_not_called()

    def test_ensure_image_handle_for_turn_record_writes_handle_and_mime(self):
        valor = self.Valor.create({"task_id": "eval_img_ok"})
        turn = self.ValorTurn.create(
            {
                "valor_id": valor.id,
                "sequence": 1,
                "image": b"ZmFrZQ==",  # base64 for "fake"
                "image_mime": "image/png",
            }
        )

        with patch(
            "odoo.addons.valor.models.models.Valor._upload_image_to_s3",
            return_value="images/eval_img_ok/turn_1.png",
        ) as up_mock, patch(
            "odoo.addons.valor.models.models.Valor._upload_attachment_meta",
            return_value={"handle_id": "H123", "mime": "image/png"},
        ):
            valor._ensure_image_handle_for_turn_record(turn, genai_api_key="dummy")

        up_mock.assert_called_once()
        self.assertEqual(turn.image_handle_id, "H123")
        self.assertEqual(turn.image_mime, "image/png")

    def test_build_message_metagen_with_text_only(self):
        valor = self.Valor.create({"task_id": "eval_msg"})
        msgs = valor._build_message_metagen("assistant", text="Hello", attachment_handle_id=None)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["source"]["role"], "assistant")
        self.assertEqual(msgs[0]["contents"][0]["text"]["text"], "Hello")

    def test_build_message_metagen_with_user_image_and_text(self):
        valor = self.Valor.create({"task_id": "eval_msg2"})
        msgs = valor._build_message_metagen(
            "user", text="Hi", attachment_handle_id="H1", attachment_mime="image/png"
        )
        # Should have two messages: one attachment, one text
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["contents"][0]["attachment"]["handle_id"], "H1")
        self.assertEqual(msgs[1]["contents"][0]["text"]["text"], "Hi")

    def test_build_message_metagen_without_text_or_attachment_returns_minimal(self):
        """If no text and no attachment, still returns a single empty message dict."""
        valor = self.Valor.create({"task_id": "eval_msg3"})
        msgs = valor._build_message_metagen("assistant")
        self.assertEqual(len(msgs), 1)
        self.assertIn("source", msgs[0])
        self.assertEqual(msgs[0]["source"]["role"], "assistant")


class TestValorTurnQc(ValorTestCase):

    def test_run_kimi_qc_after_eval_returns_none_when_no_eval_result(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "P",
            }
        )
        self.assertIsNone(turn._run_kimi_qc_after_eval([], eval_data=None))

    def test_run_kimi_qc_after_eval_sets_qc_status_and_error_flags(self):
        # Prepare human scores and AI scores to force one mismatch
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "P",
                "client_response_a": "A",
                "client_response_b": "B",
                "truthfulness_a": "1",
                "store_truthfulness_a": "3",  # diff > 1 → error_truthfulness_a True
                "ab_preference": "1",
                "store_ab_preference": "3",
                "ab_comment": "ok",
            }
        )

        fake_eval_result = [
            {
                "evaluation_result": {},
                "comparison_ab": None,
            }
        ]
        fake_qc_data = [
            {
                "qc_status": "QC_Fail",
                "ab_comment": [
                    "preference_matches_comment: fail - mismatch reason",
                    "grounded_in_dimension_ratings: fail - some detail",
                ],
            }
        ]

        with patch(
            "odoo.addons.valor.models.valor_turn.run_qc_kimi", return_value=fake_qc_data
        ):
            write_vals = turn._run_kimi_qc_after_eval(fake_eval_result, eval_data=None)

        self.assertIsInstance(write_vals, dict)
        self.assertEqual(write_vals["qc_task_status"], "fail")
        self.assertTrue(write_vals["error_truthfulness_a"])
        self.assertTrue(write_vals["error_ab_preference"])
        self.assertTrue(write_vals["error_ab_comment"])

    def test_run_kimi_qc_after_eval_sets_pass_status_when_qc_pass(self):
        turn = self.ValorTurn.create(
            {
                "valor_id": self.valor.id,
                "sequence": 1,
                "client_prompt": "P",
                "client_response_a": "A",
                "client_response_b": "B",
                "truthfulness_a": "2",
                "store_truthfulness_a": "2",
                "ab_preference": "1",
                "store_ab_preference": "1",
                "ab_comment": "ok",
            }
        )

        fake_eval_result = [
            {
                "evaluation_result": {},
                "comparison_ab": None,
            }
        ]
        fake_qc_data = [
            {
                "qc_status": "QC_Pass",
                "ab_comment": [],
            }
        ]

        with patch("odoo.addons.valor.models.valor_turn.run_qc_kimi", return_value=fake_qc_data):
            write_vals = turn._run_kimi_qc_after_eval(fake_eval_result, eval_data=None)

        self.assertEqual(write_vals["qc_task_status"], "pass")
        self.assertFalse(write_vals["error_ab_comment"])

