#!/usr/bin/env python3
"""
LeetCode Stats Card Generator
Produces a dark-theme SVG for embedding in README.md.
Called by sync.py after each run.
"""

from __future__ import annotations

import html as _html

# ─── Color Palette ────────────────────────────────────────────────────────────
_C = {
    "bg":       "#1A1A2E",
    "bg2":      "#16213E",
    "border":   "#2D3250",
    "divider":  "#2D3250",
    "text":     "#EBEBF5",
    "subtext":  "#8B8B8B",
    "accent":   "#FFA116",    # LeetCode orange
    "easy":     "#00B8A3",
    "easy_bg":  "#003D36",
    "medium":   "#FFC01E",
    "medium_bg":"#3D3010",
    "hard":     "#EF4743",
    "hard_bg":  "#3D1010",
    "tag_bg":   "#2D3250",
    "tag_text": "#A0A8D0",
}

# ─── Layout Constants ─────────────────────────────────────────────────────────
_W        = 500
_PAD      = 20
_BAR_X    = 95
_BAR_W    = 260
_BAR_H    = 8
_ROW_GAP  = 36
_HEADER_H = 80
_DIFF_Y0  = 120       # first difficulty row baseline


# ─── SVG Helpers ──────────────────────────────────────────────────────────────
def _esc(s) -> str:
    return _html.escape(str(s))


def _text(x: int, y: int, content: str, *, size: int = 12, weight: str = "normal",
          fill: str | None = None, anchor: str = "start") -> str:
    fill = fill or _C["text"]
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'font-family="\'Segoe UI\',Roboto,sans-serif" '
        f'font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}">{_esc(content)}</text>'
    )


def _bar(x: int, y: int, w: int, h: int, pct: float, fg: str, bg: str) -> str:
    radius = h // 2
    fill_w = max(0, min(w, round(w * pct / 100)))
    if 0 < fill_w < radius * 2:
        fill_w = radius * 2
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{bg}"/>\n'
        f'<rect x="{x}" y="{y}" width="{fill_w}" height="{h}" rx="{radius}" fill="{fg}"/>'
    )


def _leetcode_icon(x: int, y: int, size: int = 20) -> str:
    """Render the LeetCode 'LC' badge as a circle + monospace text."""
    cx, cy = x + size, y + size
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{size}" fill="{_C["accent"]}" opacity="0.15"/>\n'
        f'<text x="{cx}" y="{cy + 5}" text-anchor="middle" '
        f'font-family="monospace" font-size="14" font-weight="800" '
        f'fill="{_C["accent"]}">LC</text>'
    )


# ─── Main Generator ──────────────────────────────────────────────────────────
def generate_stats_card(username: str, stats_data: dict, state: dict) -> str:
    """
    Build the complete SVG string.

    Args:
        username:   LeetCode username (display only)
        stats_data: response from the getUserStats GraphQL query
        state:      sync state dict (provides tag_counts)
    """
    # ── Parse difficulty data ─────────────────────────────────────────────────
    ac_map:    dict[str, int] = {}
    total_map: dict[str, int] = {}

    matched = (stats_data.get("matchedUser") or {})
    for entry in (matched.get("submitStats") or {}).get("acSubmissionNum") or []:
        diff = entry.get("difficulty", "")
        if diff and diff != "All":
            ac_map[diff] = int(entry.get("count") or 0)

    for entry in stats_data.get("allQuestionsCount") or []:
        diff = entry.get("difficulty", "")
        if diff and diff != "All":
            total_map[diff] = int(entry.get("count") or 0)

    easy_s,   medium_s, hard_s   = ac_map.get("Easy", 0), ac_map.get("Medium", 0), ac_map.get("Hard", 0)
    easy_t,   medium_t, hard_t   = total_map.get("Easy", 850), total_map.get("Medium", 1780), total_map.get("Hard", 750)
    total_solved = easy_s + medium_s + hard_s
    total_all    = easy_t + medium_t + hard_t

    easy_pct   = (easy_s   / easy_t   * 100) if easy_t   else 0
    medium_pct = (medium_s / medium_t * 100) if medium_t else 0
    hard_pct   = (hard_s   / hard_t   * 100) if hard_t   else 0

    # ── Topic tags ────────────────────────────────────────────────────────────
    tag_counts = state.get("tag_counts") or {}
    top_tags   = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:10]
    has_tags   = bool(top_tags)

    # ── Dynamic height ────────────────────────────────────────────────────────
    card_h = 250 if not has_tags else 298

    # ── Build SVG ─────────────────────────────────────────────────────────────
    svg: list[str] = []

    # Root
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{card_h}" '
        f'viewBox="0 0 {_W} {card_h}" role="img" '
        f'aria-label="LeetCode stats for {_esc(username)}">'
    )
    svg.append(f'  <title>LeetCode Stats - {_esc(username)}</title>')

    # Background + border + accent strip
    svg.append(f'  <rect width="{_W}" height="{card_h}" rx="12" fill="{_C["bg"]}"/>')
    svg.append(f'  <rect width="{_W}" height="{card_h}" rx="12" fill="none" '
               f'stroke="{_C["border"]}" stroke-width="1"/>')
    svg.append(f'  <rect x="0" y="0" width="{_W}" height="3" fill="{_C["accent"]}"/>')

    # Header: icon + username
    svg.append(f'  {_leetcode_icon(16, 19)}')
    svg.append(f'  {_text(60, 36, username, size=18, weight="700")}')
    svg.append(f'  {_text(60, 54, f"leetcode.com/u/{username}", size=11, fill=_C["subtext"])}')

    # Header divider
    svg.append(f'  <line x1="{_PAD}" y1="{_HEADER_H}" x2="{_W - _PAD}" y2="{_HEADER_H}" '
               f'stroke="{_C["divider"]}"/>')

    # Solved summary
    # Count unique languages from state
    langs_used = set()
    for prob in (state.get("problems") or {}).values():
        lang = prob.get("language")
        if lang:
            langs_used.add(lang)
    lang_label = ", ".join(sorted(langs_used)[:3]) if langs_used else "—"
    if len(langs_used) > 3:
        lang_label += f" +{len(langs_used) - 3}"

    svg.append(f'  {_text(_PAD, 102, "SOLVED", size=10, weight="600", fill=_C["subtext"])}')
    svg.append(f'  {_text(80, 102, f"{total_solved} / {total_all}", size=11, weight="700")}')
    svg.append(f'  {_text(_W - _PAD, 102, lang_label, size=10, anchor="end", fill=_C["subtext"])}')

    # Difficulty rows
    rows = [
        ("Easy",   easy_s,   easy_t,   easy_pct,   _C["easy"],   _C["easy_bg"]),
        ("Medium", medium_s, medium_t, medium_pct, _C["medium"], _C["medium_bg"]),
        ("Hard",   hard_s,   hard_t,   hard_pct,   _C["hard"],   _C["hard_bg"]),
    ]

    for i, (label, solved, total, pct, fg, bg) in enumerate(rows):
        y = _DIFF_Y0 + i * _ROW_GAP
        bar_y = y - _BAR_H + 1
        svg.append(f'  {_text(_PAD, y, label, size=12, weight="600", fill=fg)}')
        svg.append(f'  {_bar(_BAR_X, bar_y, _BAR_W, _BAR_H, pct, fg, bg)}')
        svg.append(f'  {_text(_W - _PAD, y, f"{solved} / {total}", size=11, anchor="end", fill=_C["subtext"])}')

    # Topic tag pills
    if has_tags:
        sec_y  = _DIFF_Y0 + 3 * _ROW_GAP + 12
        pill_y = sec_y + 14
        svg.append(f'  {_text(_PAD, sec_y, "TOP TOPICS", size=10, weight="600", fill=_C["subtext"])}')

        px = _PAD
        for tag, count in top_tags:
            label  = f"{tag}  {count}"
            pill_w = max(44, int(len(label) * 7) + 16)
            if px + pill_w > _W - _PAD:
                break  # don't overflow
            svg.append(f'  <rect x="{px}" y="{pill_y}" width="{pill_w}" height="20" '
                       f'rx="10" fill="{_C["tag_bg"]}"/>')
            svg.append(f'  <text x="{px + pill_w // 2}" y="{pill_y + 14}" '
                       f'text-anchor="middle" font-family="\'Segoe UI\',Roboto,sans-serif" '
                       f'font-size="10" fill="{_C["tag_text"]}">{_esc(label)}</text>')
            px += pill_w + 6

    svg.append("</svg>")
    return "\n".join(svg)
