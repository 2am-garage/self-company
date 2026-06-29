"""Test package. Make `import _helpers` (and the scripts dir) resolve whether the
suite is run via `unittest discover -s tests` or `python3 -m unittest tests.test_X`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
