"""onboard.ps1 GpuGB must tolerate a broken nvidia-smi (static checks; pwsh not required)."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def gpugb_body():
    text = (ROOT / "nodes" / "onboard.ps1").read_text()
    start = text.index("function GpuGB {")
    end = text.index("\n}\n", start)
    return text[start:end]


class GpuGBTests(unittest.TestCase):
    def test_wrapped_in_try_catch_and_returns_zero_fallback(self):
        body = gpugb_body()
        self.assertIn("try {", body)
        self.assertIn("catch", body)
        self.assertTrue(body.rstrip().endswith("return 0"))

    def test_non_numeric_lines_are_filtered_before_conversion(self):
        body = gpugb_body()
        self.assertIn(r"-match '^\s*\d+\s*$'", body)
        self.assertNotIn("Sort-Object { [int]$_ }", body)
        self.assertLess(body.index("-match"), body.index("[int]$_.Trim()"))

    def test_nonzero_exit_is_checked(self):
        self.assertIn("$LASTEXITCODE -eq 0", gpugb_body())

    def test_only_one_nvidia_smi_probe_in_script(self):
        text = (ROOT / "nodes" / "onboard.ps1").read_text()
        calls = re.findall(r"^[^#\n]*nvidia-smi\s+--", text, re.M)
        self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
