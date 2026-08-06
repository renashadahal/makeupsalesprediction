# src/__init__.py
import os
import sys

# Ensure parent project root directory is always on Python path for cross-platform imports
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
