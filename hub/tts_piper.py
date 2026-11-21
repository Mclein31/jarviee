"""Long-lived Piper speaker (local, free): the JARVIS voice. Run with the venv python in ~/.jarviee/tts.
Reads one job per stdin line, 'MODEL_PATH<TAB>SPEED[,NOISE,NOISE_W][;GAIN]<TAB>TEXT', synthesises it and plays it with afplay, one at a time."""
import os
import subprocess
import sys
import tempfile
import wave


def main():
    from piper import PiperVoice, SynthesisConfig
    voices = {}
    print("ready", flush=True)
    for line in sys.stdin:
        try:
            model, speed, text = line.rstrip("\n").split("\t", 2)
            speed, _, gain = speed.partition(";")   # optional ";GAIN": afplay -v while the volume is ducked
            if model not in voices:
                voices[model] = PiperVoice.load(model)
            parts = [float(x) for x in speed.split(",")]   # speed[,noise,noise_w]
            cfg = SynthesisConfig(length_scale=1.0 / max(0.5, min(2.0, parts[0])),
                                  noise_scale=parts[1] if len(parts) > 1 else None,
                                  noise_w_scale=parts[2] if len(parts) > 2 else None)
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                with wave.open(path, "wb") as w:
                    voices[model].synthesize_wav(text, w, cfg)
                subprocess.run(["afplay", *(["-v", str(float(gain))] if gain else []), path], timeout=300)
            finally:
                os.unlink(path)
        except Exception as e:   # one bad phrase must not kill the worker
            print(f"error: {e}", file=sys.stderr, flush=True)
        print("done", flush=True)   # the caller waits for this, so phrases never overlap


if __name__ == "__main__":
    main()
