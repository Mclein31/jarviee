"""Local speech to text for the command bar's mic button.

Engines, best first:
  handy    the Handy app (github.com/cjpais/Handy, /Applications/Handy.app): `handy --transcribe-file <wav> --json`
           runs Handy's own offline model (the one picked in Handy) headlessly. Also gives the owner system-wide
           push-to-talk: hold Control+Option anywhere, release, and Handy types the text where the cursor is.
  whisper  whisper.cpp (`whisper-cli`) with a ggml model in ~/.jarviee/models, the older setup.
Either way ffmpeg first turns the browser recording into 16 kHz mono WAV.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_AUDIO = 25 * 1024 * 1024
MODELS = ("ggml-small.bin", "ggml-small.en.bin")   # multilingual first (auto-detects the language), English-only as fallback
HANDY_APP = Path("/Applications/Handy.app")
HANDY_DATA = Path.home() / "Library" / "Application Support" / "com.pais.handy"


def model_path():
    env = os.environ.get("JARVIEE_WHISPER_MODEL")
    if env:
        return Path(env).expanduser()
    d = Path.home() / ".jarviee" / "models"
    return next((d / m for m in MODELS if (d / m).is_file()), d / MODELS[0])


def handy_bin():
    env = os.environ.get("JARVIEE_HANDY_BIN")
    p = Path(env).expanduser() if env else HANDY_APP / "Contents" / "MacOS" / "handy"
    return p if p.is_file() and os.access(p, os.X_OK) else None


def handy_settings():
    try:
        d = json.loads((HANDY_DATA / "settings_store.json").read_text(encoding="utf-8"))
        return d.get("settings", d) if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


HF_HUB = Path(os.environ.get("HF_HUB_CACHE") or Path.home() / ".cache" / "huggingface" / "hub")


def handy_ready():
    """Handy is installed and has a model picked and downloaded (Handy's first-run setup is done). Handy keeps
    models in its own folder or, for catalog models like 'org/repo/file.gguf', in the Hugging Face cache."""
    if not handy_bin():
        return False
    sel = str(handy_settings().get("selected_model") or "")
    if not sel:
        return False
    models = HANDY_DATA / "models"
    if models.is_dir() and any(models.iterdir()):
        return True
    parts = sel.split("/")
    if len(parts) >= 2:
        repo = HF_HUB / f"models--{parts[0]}--{parts[1]}"
        name = parts[-1] if len(parts) > 2 else ""
        if repo.is_dir():
            return not name or any(repo.rglob(name))
    return False


def whisper_ready():
    return bool(shutil.which("whisper-cli") and model_path().is_file())


def engine():
    """'handy', 'whisper' or '' (nothing set up). ffmpeg is needed by both."""
    if not shutil.which("ffmpeg"):
        return ""
    if handy_ready():
        return "handy"
    return "whisper" if whisper_ready() else ""


def available():
    return bool(engine())


def status():
    """For the dashboard: which engine, and what is missing when none."""
    hint = ""
    if not shutil.which("ffmpeg"):
        hint = "Install ffmpeg (brew install ffmpeg)."
    elif handy_bin() and not handy_ready():
        hint = "Open Handy and finish its setup (pick and download a model, allow Microphone and Accessibility)."
    elif not handy_bin() and not whisper_ready():
        hint = "Install Handy (brew install --cask handy)."
    shortcut = (handy_settings().get("bindings") or {}).get("transcribe", {}).get("current_binding", "") if handy_bin() else ""
    return {"local": available(), "engine": engine(), "handy_installed": bool(handy_bin()), "handy_shortcut": shortcut,
            "hint": hint}


def _handy_text(out):
    """The transcript from `--transcribe-file --json` output (log lines may come first), else the plain stdout."""
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            for k in ("text", "transcript", "transcription", "result"):
                if isinstance(d.get(k), str):
                    return d[k]
    lines = [x for x in (out or "").splitlines() if x.strip() and not x.startswith("[")]
    return lines[-1] if lines else ""


def transcribe(audio: bytes):
    """Return the transcript text. Raises RuntimeError with a readable message on failure."""
    eng = engine()
    if not eng:
        raise RuntimeError("local speech-to-text is not set up: " + (status()["hint"] or "no engine found"))
    with tempfile.TemporaryDirectory(prefix="jarviee-stt-") as d:
        src, wav = Path(d) / "in.audio", Path(d) / "in.wav"
        src.write_bytes(audio)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav)],
                           capture_output=True, timeout=60)
        if r.returncode:
            raise RuntimeError("could not read the recording")
        if eng == "handy":
            r = subprocess.run([str(handy_bin()), "--transcribe-file", str(wav), "--json"],
                               capture_output=True, text=True, timeout=180)
            if r.returncode:
                raise RuntimeError("Handy could not transcribe it")
            return " ".join(_handy_text(r.stdout).split())
        r = subprocess.run(["whisper-cli", "-m", str(model_path()), "-f", str(wav), "-nt", "-np", "-l",
                            "en" if ".en." in model_path().name else "auto"], capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError("transcription failed")
        return " ".join(r.stdout.split())
