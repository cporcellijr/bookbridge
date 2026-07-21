#!/usr/bin/env python3
"""Compatibility entry point for the complete pytest suite."""

import subprocess
import sys
from pathlib import Path

def main():
    """Run pytest once so collection and shared fixtures are process-scoped."""
    project_root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests", *sys.argv[1:]],
        cwd=project_root,
        check=False,
    ).returncode

if __name__ == '__main__':
    sys.exit(main())
