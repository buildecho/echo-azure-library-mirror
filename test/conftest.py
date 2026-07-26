import pathlib
import sys

# Make `import sync` work when running pytest from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
