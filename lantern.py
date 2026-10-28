#!/usr/bin/env python3
"""
lantern - an ofrenda for the words you forgot you wrote.

Point it at a folder of old notes. A local open-weight model (via Ollama)
reads them, names the ghosts still living in them, writes an epitaph for
each one, and renders a candle-lit HTML altar.

Nothing leaves this machine.
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("LANTERN_MODEL", "gemma3:4b")
SUFFIXES = {".txt", ".md", ".markdown"}
MAX_CHARS = 6000

PROMPT = """You are the keeper of an ofrenda. Below is an archive of someone's old
notes, journals and abandoned drafts. The person is still alive; the words are not.

Find the {count} ghosts that still live in this archive. A ghost is a motif that
recurs, an idea that was abandoned but never quite died, or a feeling the writer
kept circling back to. Prefer specific and strange over generic and wise.

Return JSON only, exactly this shape:
{{
  "altar": "one short sentence naming what this archive was really about",
  "ghosts": [
    {{
      "name": "two or three words, evocative, no quotes",
      "epitaph": "at most 18 words, written on a gravestone",
      "line": "a short phrase copied near-verbatim from the archive"
    }}
  ]
}}

Do not invent memories that are not in the text. Do not use the word "journey".

ARCHIVE:
{archive}
"""

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ofrenda - {altar}</title>
<style>
  :root {{ color-scheme: dark; --marigold: #f0a028; --bone: #f4e8d4; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 7vh 1.25rem 4rem; min-height: 100vh;
    font: 17px/1.65 "Iowan Old Style", "Palatino Linotype", Georgia, serif;
    color: var(--bone);
    background: radial-gradient(120% 75% at 50% 0%, #241329 0%, #120a18 45%, #070509 100%);
  }}
  main {{ max-width: 960px; margin: 0 auto; text-align: center; }}
  .rule {{ width: 90px; height: 2px; margin: 0 auto 2rem; background: var(--marigold); opacity: .7; }}
  h1 {{ font-size: clamp(1.9rem, 5vw, 2.9rem); font-weight: 400; margin: 0 0 .6rem; letter-spacing: .01em; }}
  .altar {{ color: #cbb6d8; font-style: italic; margin: 0 auto 3.5rem; max-width: 34rem; }}
  .shelf {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1rem; }}
  .ghost {{
    padding: 2.4rem 1.1rem 1.5rem; border-radius: 10px;
    border: 1px solid rgba(240, 160, 40, .22);
    background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.012));
  }}
  .candle {{ width: 12px; height: 34px; margin: 0 auto .9rem; border-radius: 3px 3px 1px 1px;
             background: linear-gradient(180deg, #f6efdf, #cbb99a); position: relative; }}
  .flame {{
    position: absolute; left: 50%; bottom: 100%; width: 11px; height: 20px; margin-left: -5.5px;
    border-radius: 50% 50% 45% 45% / 62% 62% 38% 38%;
    background: radial-gradient(circle at 50% 72%, #fff4c4 0%, #ffb703 45%, #e8631a 78%, transparent 80%);
    filter: drop-shadow(0 0 12px rgba(255, 150, 40, .85));
    transform-origin: 50% 100%; animation: sway 2.6s ease-in-out infinite;
  }}
  @keyframes sway {{
    0%, 100% {{ transform: rotate(-3deg) scaleY(1); opacity: .95; }}
    50%      {{ transform: rotate(3deg) scaleY(1.08); opacity: 1; }}
  }}
  @media (prefers-reduced-motion: reduce) {{ .flame {{ animation: none; }} }}
  h2 {{ font-size: 1.05rem; font-weight: 400; letter-spacing: .14em; text-transform: uppercase;
        color: var(--marigold); margin: 0 0 .7rem; }}
  .epitaph {{ margin: 0 0 1.1rem; }}
  .line {{ margin: 0; font-size: .86rem; color: #9c8ea8; font-style: italic; }}
  .line::before {{ content: "\\201C"; }} .line::after {{ content: "\\201D"; }}
  footer {{ margin-top: 4rem; font-size: .78rem; color: #6f6379; letter-spacing: .04em; }}
</style>
<main>
  <div class="rule"></div>
  <h1>Ofrenda</h1>
  <p class="altar">{altar}</p>
  <div class="shelf">
{cards}
  </div>
  <footer>{words} words read &middot; {model} &middot; {route}</footer>
</main>
</html>
"""

CARD = """    <section class="ghost">
      <div class="candle"><span class="flame"></span></div>
      <h2>{name}</h2>
      <p class="epitaph">{epitaph}</p>
      <p class="line">{line}</p>
    </section>"""


def gather(path: Path) -> str:
    """Read every note in a file or folder and flatten it into one archive."""
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.suffix.lower() in SUFFIXES and p.is_file())
        if not files:
            sys.exit(f"lantern: no .txt or .md notes in {path}")
        text = "\n".join(f"\n--- {p.name} ---\n{p.read_text(encoding='utf-8', errors='replace')}"
                         for p in files)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = " ".join(text.split())
    if not text:
        sys.exit(f"lantern: {path} is empty")
    return text[:MAX_CHARS]


def unfence(raw: str) -> str:
    """Models often wrap JSON in a ```json fence even when told not to."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return text
    return text[start:end + 1]


def invoke(model: str, archive: str, count: int) -> dict:
    """Ask the local model which ghosts remain. Returns the parsed altar."""
    body = {
        "model": model,
        "prompt": PROMPT.format(count=count, archive=archive),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.9},
    }
    request = urllib.request.Request(
        f"{HOST}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = json.load(response).get("response", "")
    except urllib.error.URLError as exc:
        sys.exit(f"lantern: cannot reach Ollama at {HOST} ({exc.reason}).\n"
                 f"        start it with `ollama serve`, or set OLLAMA_HOST.")
    try:
        altar = json.loads(unfence(raw))
    except json.JSONDecodeError:
        sys.exit("lantern: the model did not return valid JSON - try a larger model or rerun.")

    ghosts = altar.get("ghosts") or []
    if not isinstance(ghosts, list) or not ghosts:
        sys.exit("lantern: the model found no ghosts in this archive.")
    for ghost in ghosts:
        if not isinstance(ghost, dict):
            sys.exit("lantern: malformed ghost in model output.")
        ghost["name"] = str(ghost.get("name", "Unnamed")).strip()
        ghost["epitaph"] = str(ghost.get("epitaph", "")).strip()
        ghost["line"] = str(ghost.get("line", "")).strip().strip('"\u201c\u201d').strip()
    altar["ghosts"] = ghosts
    altar["altar"] = str(altar.get("altar", "An archive that outlived its intentions.")).strip()
    return altar


def route_of(model: str) -> str:
    """Say plainly where inference happened; ollama tags cloud-routed models."""
    if "cloud" in model.lower():
        return "this model is cloud-routed via Ollama"
    return "local inference, nothing left this machine"


def render(altar: dict, model: str, words: int) -> str:
    cards = "\n".join(
        CARD.format(name=html.escape(g["name"]), epitaph=html.escape(g["epitaph"]),
                    line=html.escape(g["line"]))
        for g in altar["ghosts"]
    )
    return PAGE.format(altar=html.escape(altar["altar"]), cards=cards,
                       model=html.escape(model), words=words, route=route_of(model))


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn old notes into a candle-lit ofrenda.")
    parser.add_argument("notes", type=Path, help="a .md/.txt file, or a folder of them")
    parser.add_argument("-o", "--out", type=Path, default=Path("ofrenda.html"))
    parser.add_argument("--model", default=MODEL, help=f"any Ollama model (default: {MODEL})")
    parser.add_argument("--ghosts", type=int, default=5, help="how many ghosts to raise (default: 5)")
    args = parser.parse_args()

    if not args.notes.exists():
        sys.exit(f"lantern: no such path: {args.notes}")
    if args.ghosts < 1:
        sys.exit("lantern: --ghosts must be at least 1")

    archive = gather(args.notes)
    words = len(archive.split())
    print(f"reading {words} words from {args.notes} ...", file=sys.stderr)

    altar = invoke(args.model, archive, args.ghosts)
    args.out.write_text(render(altar, args.model, words), encoding="utf-8")

    print(f"\n{altar['altar']}\n", file=sys.stderr)
    for ghost in altar["ghosts"]:
        print(f"  {ghost['name']}: {ghost['epitaph']}", file=sys.stderr)
    print(f"\nlit {args.out}  (open it in a browser)", file=sys.stderr)


if __name__ == "__main__":
    main()
