"""Save a local Gemini key without echoing it. .env is excluded from Git and releases."""
import getpass, os
from pathlib import Path
key = getpass.getpass("Gemini API key (input hidden): ").strip()
if not key:
    raise SystemExit("No key entered. Guided mode remains available.")
p = Path(__file__).resolve().parent / '.env'
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('GEMINI_API_KEY=' + key + '\nGEMINI_MODEL=gemini-3.5-flash-lite\n')
os.chmod(p, 0o600)
print("Gemini is configured locally. Start with python3 launch.py, then enable Gemini on the advisor screen.")
