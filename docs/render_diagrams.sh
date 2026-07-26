#!/usr/bin/env bash
# Extracts each ```mermaid fenced block from docs/diagrams.md and renders it
# to docs/diagrams/<n>_<slug>.png for embedding in docs/technical_report.tex.
# Requires Node (uses `npx @mermaid-js/mermaid-cli` — no local install needed,
# npx fetches/caches it on first run).
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p diagrams
python3 - <<'PYEOF'
import re, pathlib

src = pathlib.Path("diagrams.md").read_text()
blocks = re.findall(r"```mermaid\n(.*?)\n```", src, re.DOTALL)

# Slugs matched by order of appearance in diagrams.md — keep in sync with that file.
slugs = [
    "01_sequence_full_lifecycle",
    "02_sequence_active_passive",
    "03_flowchart_thread_dispatch",
    "04_flowchart_gbn_sender",
    "05_flowchart_gbn_receiver",
    "06_flowchart_client_mode_select",
]

pathlib.Path("diagrams").mkdir(exist_ok=True)
for slug, block in zip(slugs, blocks):
    (pathlib.Path("diagrams") / f"{slug}.mmd").write_text(block)

print(f"Extracted {len(blocks)} mermaid blocks (expected {len(slugs)}).")
PYEOF

for f in diagrams/*.mmd; do
    out="${f%.mmd}.png"
    echo "Rendering $f -> $out"
    npx -y @mermaid-js/mermaid-cli -i "$f" -o "$out" -b white -s 2
done

echo "Done. PNGs in docs/diagrams/."
