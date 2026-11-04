import os
import sys

# make `src.*` importable when pytest is invoked from anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
