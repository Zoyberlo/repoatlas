"""Draw the paired-interval figure for docs/article.md, from the numbers.

The figure was ASCII once, which reads as noise wherever it is rendered in
a proportional context. It is now SVG — but generated rather than drawn, so
a bar cannot drift from the interval it represents. Change a number here
and the picture changes with it; there is no second place to edit.

Two files, light and dark, because the article is read on GitHub and a
single SVG cannot put legible text on both grounds. `<picture>` with a
`prefers-color-scheme` media query is what selects between them.

    python scripts/interval_plot.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"


@dataclass(frozen=True)
class Row:
    """One paired comparison: its estimate, its interval, and what it means."""

    label: str
    point: float
    low: float
    high: float
    verdict: str = "null"
    note: str = ""


# Every agent-level comparison that produced an interval, in the order it
# was run. `held` is an interval excluding zero that survived a re-run;
# `withdrawn` cleared the bar once and did not reproduce.
ROWS = [
    Row("localise a change", 0.032, -0.059, 0.122),
    Row("hook, first run", 0.093, 0.009, 0.197, "withdrawn", "withdrawn"),
    Row("hook, re-run", -0.031, -0.094, 0.000),
    Row("map + tools + hook + turns", 0.031, 0.000, 0.094),
    Row("user wording, English", 0.017, -0.052, 0.101),
    Row("user wording, Ukrainian", -0.001, -0.026, 0.023),
    Row("Ukrainian vs English", -0.006, -0.038, 0.026),
    Row("review, index vs grep", 0.000, 0.000, 0.000),
    Row("review, no checkout", 0.337, 0.129, 0.565, "held", "+0.337"),
    Row("two clarifying questions", 0.195, 0.066, 0.350, "held", "+0.195"),
]

# Where the subject changes: everything above is a better tool, the last
# row is a better question. The rule is information, not decoration.
DIVIDER_BEFORE = len(ROWS) - 1

LO, HI = -0.12, 0.60
LEFT, RIGHT = 258, 848
TOP, ROW_H = 92, 30
WIDTH = 900
# The divider needs a line of its own, so rows below it are pushed down.
DIVIDER_GAP = 26
HEIGHT = TOP + len(ROWS) * ROW_H + DIVIDER_GAP + 58

THEMES = {
    "light": {
        "text": "#1f2328", "muted": "#59636e", "faint": "#818b98",
        "rule": "#d1d9e0", "axis": "#1f2328",
        "null": "#8c959f", "held": "#1a6560", "withdrawn": "#9a3b2e",
    },
    "dark": {
        "text": "#e6edf3", "muted": "#9198a1", "faint": "#7d8590",
        "rule": "#30363d", "axis": "#e6edf3",
        "null": "#7d8590", "held": "#5fbdb4", "withdrawn": "#d98a79",
    },
}

FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"


def x_of(value: float) -> float:
    return LEFT + (value - LO) / (HI - LO) * (RIGHT - LEFT)


def svg(theme: str) -> str:
    c = THEMES[theme]
    zero = x_of(0.0)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" font-family="{FONT}" '
        f'role="img" aria-label="Ten paired comparisons with 95% intervals. '
        f'Eight overlap zero; two do not.">'
    ]

    parts.append(
        f'<text x="{LEFT}" y="26" font-size="15" font-weight="600" fill="{c["text"]}">'
        f"Paired difference in accuracy, with 95% interval</text>"
    )
    parts.append(
        f'<text x="{LEFT}" y="46" font-size="12.5" fill="{c["muted"]}">'
        f"Eight of ten overlap zero. Both that do not are to the right of it.</text>"
    )

    # Gridlines first, so every mark sits on top of them.
    bottom = TOP + len(ROWS) * ROW_H + DIVIDER_GAP
    for tick in (-0.1, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        x = x_of(tick)
        parts.append(
            f'<line x1="{x:.1f}" y1="{TOP - 8}" x2="{x:.1f}" '
            f'y2="{bottom}" stroke="{c["rule"]}" stroke-width="1"/>'
        )

    # The zero line is the whole argument, so it is drawn like it.
    parts.append(
        f'<line x1="{zero:.1f}" y1="{TOP - 14}" x2="{zero:.1f}" y2="{bottom + 6}" '
        f'stroke="{c["axis"]}" stroke-width="1.5"/>'
    )
    parts.append(
        f'<text x="{zero:.1f}" y="{TOP - 20}" font-size="11.5" font-weight="600" '
        f'fill="{c["axis"]}" text-anchor="middle">no difference</text>'
    )

    for index, row in enumerate(ROWS):
        shift = DIVIDER_GAP if index >= DIVIDER_BEFORE else 0
        y = TOP + index * ROW_H + ROW_H / 2 + shift
        colour = c[row.verdict]

        if index == DIVIDER_BEFORE:
            line_y = TOP + index * ROW_H + DIVIDER_GAP - 8
            parts.append(
                f'<line x1="24" y1="{line_y}" x2="{RIGHT}" y2="{line_y}" '
                f'stroke="{c["rule"]}" stroke-width="1" stroke-dasharray="2 3"/>'
            )
            parts.append(
                f'<text x="24" y="{line_y - 9}" font-size="10.5" fill="{c["faint"]}" '
                f'letter-spacing="0.08em">A BETTER QUESTION, NOT A BETTER TOOL</text>'
            )

        parts.append(
            f'<text x="{LEFT - 14}" y="{y + 4:.1f}" font-size="13" '
            f'fill="{c["text"]}" text-anchor="end">{row.label}</text>'
        )

        low, high = x_of(row.low), x_of(row.high)
        dash = ' stroke-dasharray="5 4"' if row.verdict == "withdrawn" else ""
        if high - low < 1.5:
            # A zero-width interval: a bar would be invisible, and a dot
            # alone would read as missing data rather than as the finding.
            parts.append(
                f'<line x1="{low:.1f}" y1="{y - 6:.1f}" x2="{low:.1f}" y2="{y + 6:.1f}" '
                f'stroke="{colour}" stroke-width="2.5"/>'
            )
        else:
            parts.append(
                f'<line x1="{low:.1f}" y1="{y:.1f}" x2="{high:.1f}" y2="{y:.1f}" '
                f'stroke="{colour}" stroke-width="2.5" stroke-linecap="round"{dash}/>'
            )
            for cap in (low, high):
                parts.append(
                    f'<line x1="{cap:.1f}" y1="{y - 4.5:.1f}" x2="{cap:.1f}" '
                    f'y2="{y + 4.5:.1f}" stroke="{colour}" stroke-width="2"/>'
                )
        parts.append(
            f'<circle cx="{x_of(row.point):.1f}" cy="{y:.1f}" r="4.5" fill="{colour}"/>'
        )
        if row.note:
            if row.verdict == "held":
                parts.append(
                    f'<text x="{x_of(row.point):.1f}" y="{y - 10:.1f}" font-size="11.5" '
                    f'font-weight="600" fill="{colour}" text-anchor="middle">{row.note}</text>'
                )
            else:
                parts.append(
                    f'<text x="{high + 12:.1f}" y="{y + 4:.1f}" font-size="11.5" '
                    f'fill="{colour}">{row.note}</text>'
                )

    for tick in (-0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        label = "0" if tick == 0 else f"{tick:+.1f}".replace("0.", ".")
        parts.append(
            f'<text x="{x_of(tick):.1f}" y="{bottom + 24}" font-size="11.5" '
            f'font-family="{MONO}" fill="{c["faint"]}" text-anchor="middle">{label}</text>'
        )
    parts.append(
        f'<text x="{(LEFT + RIGHT) / 2:.0f}" y="{bottom + 45}" font-size="11.5" '
        f'fill="{c["muted"]}" text-anchor="middle">'
        f"symbol recall for the localisation rows, F1 for the review rows</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        path = OUT / f"intervals-{theme}.svg"
        path.write_text(svg(theme), encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
