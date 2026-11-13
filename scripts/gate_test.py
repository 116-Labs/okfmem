#!/usr/bin/env python3
"""Pool test gate: pick the pytest slice by platform and run it (#223).

Windows runs only the `os_real` tests, serially; every other platform runs the
full suite. CI runs the full suite on all three OSes and backstops the slice.
`OKFMEM_TEST_SLICE=full|os_real` forces a slice on any platform.
"""
import os
import subprocess
import sys


def pick_args(platform, env):
    forced = env.get("OKFMEM_TEST_SLICE", "").strip().lower()
    if forced == "os_real" or (forced != "full" and platform == "win32"):
        return "os_real", ["-q", "-m", "os_real"]
    return "full", ["-q", "-n", "auto"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    slice_name, args = pick_args(sys.platform, os.environ)
    print(f"gate_test: slice={slice_name} platform={sys.platform}", flush=True)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.call([sys.executable, "-m", "pytest", *args, *argv], cwd=root)


if __name__ == "__main__":
    sys.exit(main())
