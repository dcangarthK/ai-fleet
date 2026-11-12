"""Opt-in proof for SPEC-networking §14.2. Skipped in the hermetic gate."""
import os
import unittest


@unittest.skipUnless(os.environ.get("FLEET_REAL_ENGINE_TEST") == "1", "set FLEET_REAL_ENGINE_TEST=1")
class RealEngineIntegration(unittest.TestCase):
    def test_openai_adapter_through_controller_and_runner(self):
        # Kept here (rather than folded into fake tests) so the normal gate can never contact localhost.
        from tests.real_engine_harness import run_real_engine
        result = run_real_engine(
            os.environ.get("FLEET_REAL_ENGINE_URL", "http://127.0.0.1:11434/v1"),
            os.environ.get("FLEET_REAL_ENGINE_MODEL", "qwen2.5-coder:14b"),
        )
        self.assertEqual("done", result["state"], result)
        self.assertTrue(result["result"].strip(), result)
        print("REAL_ENGINE_RESULT " + result["result"].strip().replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
