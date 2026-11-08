"""Start the local demo and open its browser tab. No packages required."""
import os, runpy, threading, urllib.request, webbrowser
from pathlib import Path

def main():
    os.chdir(Path(__file__).resolve().parent)
    config = Path('.env')
    if config.exists():
        for line in config.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                if key.strip() in ['GEMINI_API_KEY', 'GEMINI_MODEL', 'PORT'] and value.strip():
                    os.environ.setdefault(key.strip(), value.strip())
    try:
        port = int(os.environ.get("PORT", "8000"))
        if not 1024 <= port <= 65535: raise ValueError()
    except ValueError:
        raise SystemExit("Choose a PORT between 1024 and 65535, then try again.")
    url = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
            if response.headers.get("Server", "").startswith("Udyam/"):
                print("Udyam is already running at " + url)
                webbrowser.open(url)
                return
    except Exception:
        pass
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        runpy.run_path("app.py", run_name="__main__")
    except OSError:
        raise SystemExit(f"Could not start on port {port}. Close the app using that port, or set PORT to another number.")
    except KeyboardInterrupt:
        print("\nUdyam stopped.")

if __name__ == "__main__":
    main()
