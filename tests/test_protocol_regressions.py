import io
import inspect
import unittest

from PIL import Image

from pixcut.image_to_cut import add_printer_margins
from pixcut.orchestrator import (
    PRINTER_ERROR_CODES,
    RECOVERABLE_ERROR_CODES,
    PixcutClient,
    _completion_confirmed,
    _extract_alert_codes,
    _extract_error_code,
    _extract_job_ids,
    _prepare_jpg_for_printer,
)
from pixcut.svg_to_plt import _plt_path_commands, convert_svg_to_plt


class RasterRegistrationTests(unittest.TestCase):
    def test_add_printer_margins_4x7(self):
        logical = Image.new("RGB", (1200, 2100), "white")
        padded = add_printer_margins(logical)
        self.assertEqual(padded.size, (1216, 2128))

    def test_send_path_pads_logical_4x7_jpeg(self):
        source = Image.new("RGB", (1200, 2100), "white")
        raw = io.BytesIO()
        source.save(raw, "JPEG", quality=80)

        prepared = _prepare_jpg_for_printer(raw.getvalue(), 5013)
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertEqual(image.size, (1216, 2128))

    def test_already_padded_jpeg_is_unchanged(self):
        source = Image.new("RGB", (1216, 2128), "white")
        raw = io.BytesIO()
        source.save(raw, "JPEG", quality=80)
        original = raw.getvalue()

        self.assertEqual(_prepare_jpg_for_printer(original, 5013), original)


class AlertModelTests(unittest.TestCase):
    def test_numeric_zero_is_not_an_alert(self):
        self.assertEqual(_extract_alert_codes(0), set())
        self.assertEqual(_extract_alert_codes("::0"), set())

    def test_expanded_error_map_is_present(self):
        self.assertEqual(len(PRINTER_ERROR_CODES), 67)
        for code in (5199, 5506, 8008, 8301, 8413, 9005):
            self.assertIn(code, PRINTER_ERROR_CODES)

    def test_recoverable_set_matches_current_protocol_model(self):
        self.assertEqual(
            RECOVERABLE_ERROR_CODES,
            {
                5001, 5306, 5401, 5402, 5417, 5418, 5419, 5420,
                5506, 8101, 8102, 8104, 8106,
            },
        )


class JobControlTests(unittest.TestCase):
    def test_job_id_parser_handles_known_shapes(self):
        self.assertEqual(_extract_job_ids("12;13"), [12, 13])
        self.assertEqual(_extract_job_ids({"job-id-list": [4, 5]}), [4, 5])
        self.assertEqual(_extract_job_ids({"result": {"job_id": "9"}}), [9])

    def test_error_code_parser_handles_nested_response(self):
        self.assertEqual(_extract_error_code({"result": [{"error-code": "8011"}]}), 8011)

    def test_confirm_and_cancel_command_shapes(self):
        class Logger:
            keep_json = False

        client = PixcutClient(object(), Logger())
        sent = []

        def fake_send(obj, expect_response=True, quiet=False):
            sent.append(obj)
            return {"result": ["OK"]}

        client._send_json = fake_send  # type: ignore[method-assign]

        client.confirm_job(42)
        client.cancel_job(42)

        self.assertEqual(sent[0]["method"], "confirm_job")
        self.assertEqual(sent[0]["params"], {"job-id": 42})
        self.assertEqual(sent[1]["method"], "cancel-job")
        self.assertEqual(sent[1]["params"], {"job-id": 42})

    def test_combo_completion_requires_cut_and_stable_idle(self):
        self.assertFalse(
            _completion_confirmed(
                print_only=False,
                cut_started=False,
                completion_reported=True,
                idle_polls=3,
            )
        )
        self.assertTrue(
            _completion_confirmed(
                print_only=False,
                cut_started=True,
                completion_reported=True,
                idle_polls=3,
            )
        )
        self.assertTrue(
            _completion_confirmed(
                print_only=True,
                cut_started=False,
                completion_reported=True,
                idle_polls=3,
            )
        )


class PltRegressionTests(unittest.TestCase):
    def test_svg_perf_default_is_53_not_60(self):
        self.assertEqual(
            inspect.signature(convert_svg_to_plt).parameters["perf_knife_pressure"].default,
            53,
        )

    def test_dashed_blade_up_mode_never_emits_kp_none(self):
        commands = _plt_path_commands(
            [[(0.0, 0.0), (100.0, 0.0)]],
            dash_u=20.0,
            gap_u=10.0,
        )
        self.assertFalse(any(command == "KPNone" for command in commands))
        self.assertTrue(any(command.startswith("D") for command in commands))
        self.assertTrue(any(command.startswith("U") for command in commands))

    def test_pressure_mode_reseats_when_kp_changes(self):
        commands = _plt_path_commands(
            [[(0.0, 0.0), (100.0, 0.0)]],
            dash_u=20.0,
            gap_u=10.0,
            dash_kp=53,
            gap_kp=42,
            nudge_u=4.0,
        )
        self.assertIn("KP53", commands)
        self.assertIn("KP42", commands)
        # 0.1 mm at 40 PLT units/mm = 4 units; the reseat travel should appear.
        self.assertIn("U24,0", commands)


if __name__ == "__main__":
    unittest.main()
