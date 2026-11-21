"""Long-lived Kokoro speaker (local, free). Run with the venv python in ~/.jarviee/tts.
Reads one job per stdin line, 'VOICE<TAB>SPEED[;GAIN]<TAB>TEXT', synthesises it and plays it with afplay, one at a time."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

D = Path.home() / ".jarviee" / "tts"


def main():
    import soundfile as sf
    from kokoro_onnx import Kokoro
    k = Kokoro(str(D / "kokoro-v1.0.onnx"), str(D / "voices-v1.0.bin"))
    print("ready", flush=True)
    for line in sys.stdin:
        try:
            voice, speed, text = line.rstrip("\n").split("\t", 2)
            speed, _, gain = speed.partition(";")   # optional ";GAIN": afplay -v while the volume is ducked
            samples, rate = k.create(text, voice=voice, speed=float(speed), lang="en-gb" if voice[:1] == "b" else "en-us")
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                sf.write(path, samples, rate)
                subprocess.run(["afplay", *(["-v", str(float(gain))] if gain else []), path], timeout=120)
            finally:
                os.unlink(path)
        except Exception as e:   # one bad phrase must not kill the worker
            print(f"error: {e}", file=sys.stderr, flush=True)
        print("done", flush=True)   # the caller waits for this, so phrases never overlap


if __name__ == "__main__":
    main()
