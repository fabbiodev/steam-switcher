"""Load external application bytecode from app/, keeping runtime/ for dependencies."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.executable).resolve().parent / 'app'))
