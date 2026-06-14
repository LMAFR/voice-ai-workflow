#!/usr/bin/env python3
"""
voice-issue: record your voice, transcribe locally with faster-whisper, and
drop a transcript file into the outbox folder for the local backend to pick up.

Cross-platform (Windows / Linux / macOS). Records from the default microphone
until you press Enter, transcribes locally (no network, no API key), and writes
a transcript JSON tagged with a repo alias.

Usage:
    voice-issue --repo myalias
    voice-issue --repo myalias --lang es
    voice-issue --repo myalias --file existing_audio.wav   # skip recording

Config / env:
    VOICE_ISSUE_OUTBOX   override outbox dir (default: ~/.voice-issue/outbox)
    VOICE_ISSUE_MODEL    whisper model size (default: medium)
    VOICE_ISSUE_DEVICE   cpu | cuda (default: cpu)
    VOICE_ISSUE_COMPUTE  compute type (default: int8 on cpu, float16 on cuda)
"""
import argparse
import datetime as dt
import json
import os
import socket
import sys
import tempfile
import threading
import uuid
from pathlib import Path

SAMPLE_RATE = 16000  # what whisper expects


def default_outbox() -> Path:
    env = os.environ.get("VOICE_ISSUE_OUTBOX")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".voice-issue" / "outbox"


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def record_to_wav(wav_path: Path):
    """Record from the default mic until the user presses Enter."""
    try:
        import numpy as np
        import sounddevice as sd
        import soundfile as sf
    except ImportError as e:
        die(
            f"missing audio dependency ({e.name}). Install with:\n"
            "    pip install -r requirements.txt"
        )

    frames = []
    stop = threading.Event()

    def callback(indata, _frames, _time, status):
        if status:
            print(status, file=sys.stderr)
        frames.append(indata.copy())

    print("🎙  Recording... press Enter to stop.", flush=True)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        callback=callback):
        try:
            input()  # blocks until Enter
        except KeyboardInterrupt:
            print("\n(stopped)")
        stop.set()

    if not frames:
        die("no audio captured — is a microphone available?")

    audio = np.concatenate(frames, axis=0)
    sf.write(str(wav_path), audio, SAMPLE_RATE)
    secs = len(audio) / SAMPLE_RATE
    print(f"   captured {secs:.1f}s of audio")
    return wav_path


def transcribe(wav_path: Path, lang: str, model_size: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("missing faster-whisper. Install with:\n    pip install -r requirements.txt")

    device = os.environ.get("VOICE_ISSUE_DEVICE", "cpu")
    compute = os.environ.get(
        "VOICE_ISSUE_COMPUTE", "float16" if device == "cuda" else "int8"
    )
    print(f"🧠 Loading whisper '{model_size}' ({device}/{compute})... "
          "(first run downloads the model)", flush=True)
    model = WhisperModel(model_size, device=device, compute_type=compute)

    language = None if lang in ("auto", "", None) else lang
    segments, info = model.transcribe(str(wav_path), language=language, beam_size=5)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    detected = info.language
    print(f"📝 Transcribed (language={detected}, p={info.language_probability:.2f})")
    return text, detected


def write_transcript(outbox: Path, repo_alias: str, text: str, language: str,
                     model_size: str) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    tid = uuid.uuid4().hex[:8]
    now = dt.datetime.now(dt.timezone.utc)
    record = {
        "id": tid,
        "repo_alias": repo_alias,
        "text": text,
        "language": language,
        "model": model_size,
        "created_at": now.isoformat(),
        "source_host": socket.gethostname(),
        "status": "new",
    }
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    # write to a temp name then rename so the watcher never sees a half-written file
    final = outbox / f"{stamp}_{repo_alias}_{tid}.json"
    tmp = outbox / f".{final.name}.partial"
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(final)
    return final


def main():
    ap = argparse.ArgumentParser(description="Record voice -> transcript file for a repo")
    ap.add_argument("--repo", required=True, help="repo alias (resolved by the backend's repos.json)")
    ap.add_argument("--lang", default="auto", help="auto|en|es|... (default: auto-detect)")
    ap.add_argument("--model", default=os.environ.get("VOICE_ISSUE_MODEL", "medium"),
                    help="whisper model size (tiny|base|small|medium|large-v3)")
    ap.add_argument("--outbox", default=None, help="outbox dir (default: ~/.voice-issue/outbox)")
    ap.add_argument("--file", default=None, help="transcribe an existing audio file instead of recording")
    ap.add_argument("--print-only", action="store_true", help="transcribe and print, do not write a transcript file")
    args = ap.parse_args()

    outbox = Path(args.outbox).expanduser() if args.outbox else default_outbox()

    if args.file:
        wav = Path(args.file).expanduser()
        if not wav.exists():
            die(f"audio file not found: {wav}")
        cleanup = None
    else:
        tmp = Path(tempfile.gettempdir()) / f"voice_issue_{uuid.uuid4().hex[:8]}.wav"
        record_to_wav(tmp)
        wav, cleanup = tmp, tmp

    try:
        text, detected = transcribe(wav, args.lang, args.model)
    finally:
        if cleanup and cleanup.exists():
            try:
                cleanup.unlink()
            except OSError:
                pass

    if not text:
        die("empty transcription — nothing was said?")

    print("\n--- transcription ---")
    print(text)
    print("---------------------\n")

    if args.print_only:
        return

    path = write_transcript(outbox, args.repo, text, detected, args.model)
    print(f"✅ Wrote transcript for repo '{args.repo}':\n   {path}")


if __name__ == "__main__":
    main()
