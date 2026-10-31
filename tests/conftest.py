import os
import sys
from pathlib import Path

os.environ.setdefault("RESOLVE_PACE", "0")
os.environ["LLM_MODE"] = "offline"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("SARVAM_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
