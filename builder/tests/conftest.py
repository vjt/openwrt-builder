import sys
from pathlib import Path

# Add builder/ to sys.path so tests can import modules directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
