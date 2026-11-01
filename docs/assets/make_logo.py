"""Build the wordmark logos from mark.svg. Run: python docs/assets/make_logo.py"""
from pathlib import Path

here = Path(__file__).parent
mark = (here / "mark.svg").read_text(encoding="utf-8")
inner = mark.split(">", 1)[1].rsplit("</svg>", 1)[0]
FONT = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif"
for name, fg, sub in [("logo-dark", "#F2F4F8", "#8A93A6"), ("logo-light", "#11141B", "#5B6475")]:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 128" role="img" aria-label="Rushcut">
<svg x="0" y="0" width="128" height="128" viewBox="0 0 128 128">{inner}</svg>
<text x="152" y="80" font-family="{FONT}" font-size="68" font-weight="800" letter-spacing="-1.5" fill="{fg}">rush<tspan fill="#FF6A2B">cut</tspan></text>
<text x="155" y="108" font-family="{FONT}" font-size="17" font-weight="600" letter-spacing="3" fill="{sub}">AI VIDEO EDITING OVER MCP</text>
</svg>
"""
    (here / f"{name}.svg").write_text(svg, encoding="utf-8")
print("ok")
