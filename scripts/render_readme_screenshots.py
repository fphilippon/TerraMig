#!/usr/bin/env python3
"""Render deterministic, anonymized README screenshots from TerraMig's UI language.

The generated SVG sources deliberately reuse the layout, labels, spacing, and
color tokens implemented in web/index.html and web/styles.css.  They contain no
runtime data, credentials, project identifiers, or customer names.
"""

from __future__ import annotations

from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "screenshots"

INK = "#1d1d1f"
MUTED = "#65656a"
CANVAS = "#f7f7f8"
PAPER = "#ffffff"
LINE = "#d8d8dc"
PURPLE = "#844fba"
PURPLE_DARK = "#663a94"
PURPLE_PALE = "#f1eafa"
GREEN = "#168570"
GREEN_PALE = "#e5f5f1"
AMBER = "#b97800"
AMBER_PALE = "#fff6df"


def rect(x, y, width, height, *, fill=PAPER, stroke="none", radius=0, extra=""):
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'fill="{fill}" stroke="{stroke}" rx="{radius}" {extra}/>'
    )


def line(x1, y1, x2, y2, *, stroke=LINE, width=1):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{width}"/>'


def circle(x, y, radius, *, fill=PAPER, stroke="none", width=1):
    return f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>'


def text(x, y, value, *, size=14, fill=INK, weight=400, anchor="start", family="Inter,Arial,sans-serif", spacing=0):
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}" letter-spacing="{spacing}">{escape(value)}</text>'
    )


def multiline(x, y, values, *, size=14, fill=MUTED, weight=400, leading=22, family="Inter,Arial,sans-serif"):
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if index == 0 else leading}">{escape(value)}</tspan>'
        for index, value in enumerate(values)
    )
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}">{spans}</text>'
    )


def pill(x, y, width, label, *, fill=PURPLE_PALE, stroke="#c7b0df", color=PURPLE_DARK):
    return "".join(
        [
            rect(x, y, width, 28, fill=fill, stroke=stroke, radius=14),
            text(x + width / 2, y + 19, label, size=10, fill=color, weight=760, anchor="middle"),
        ]
    )


def button(x, y, width, label, *, primary=False):
    fill = PURPLE if primary else PAPER
    color = PAPER if primary else PURPLE_DARK
    stroke = PURPLE if primary else "#c7b0df"
    return "".join(
        [
            rect(x, y, width, 34, fill=fill, stroke=stroke, radius=2),
            text(x + width / 2, y + 22, label, size=10, fill=color, weight=740, anchor="middle"),
        ]
    )


def checkbox(x, y, checked=True):
    parts = [rect(x, y, 18, 18, fill=PURPLE if checked else PAPER, stroke=PURPLE if checked else "#9a9aa0", radius=2)]
    if checked:
        parts.append(f'<path d="M{x+4} {y+9} l3 3 7 -8" fill="none" stroke="white" stroke-width="2" stroke-linecap="round"/>')
    return "".join(parts)


def radio(x, y, checked=True):
    parts = [circle(x, y, 9, fill=PAPER, stroke=PURPLE if checked else "#9a9aa0", width=2)]
    if checked:
        parts.append(circle(x, y, 4, fill=PURPLE))
    return "".join(parts)


def panel(x, y, width, height):
    return rect(x, y, width, height, fill=PAPER, stroke=LINE, radius=0, extra='filter="url(#shadow)"')


def base(screen_index, kicker, title_lines):
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000" role="img">',
        '<defs><filter id="shadow" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="8" stdDeviation="12" flood-color="#000" flood-opacity=".045"/></filter></defs>',
        rect(0, 0, 1600, 1000, fill=CANVAS),
        rect(0, 0, 248, 1000, fill="#000000"),
        '<polygon points="44,28 61,38 61,58 44,68 27,58 27,38" fill="#844fba"/>',
        text(44, 52, "TM", size=9, fill=PAPER, weight=800, anchor="middle"),
        text(76, 52, "TerraMig", size=19, fill=PAPER, weight=760),
        text(30, 112, "ADOPTION WORKFLOW", size=9, fill="#707078", weight=800, spacing=1.3),
        rect(18, 128, 212, 58, fill="#2b2b2f"),
        rect(18, 128, 3, 58, fill="#a067da"),
        text(34, 157, "⌁", size=18, fill="#c4c4c8"),
        text(68, 151, "Infrastructure adoption", size=12, fill=PAPER, weight=650),
        text(68, 170, "ClickOps → governed IaC", size=9, fill="#b7b7bd"),
        text(30, 224, "EXPLORE", size=9, fill="#707078", weight=800, spacing=1.3),
    ]
    nav = [("◇", "Inventory"), ("▦", "Private modules"), ("◉", "Operations"), ("⚙", "Configuration")]
    for idx, (icon, label) in enumerate(nav):
        y = 255 + idx * 48
        out.extend([text(34, y, icon, size=15, fill="#c4c4c8"), text(68, y, label, size=13, fill="#a9a9af", weight=560)])
    out.extend(
        [
            line(27, 881, 221, 881, stroke="#3b3b40"),
            circle(35, 912, 5, fill="#37c99a"),
            text(52, 908, "Production integrations", size=11, fill=PAPER, weight=650),
            text(52, 926, "Ready", size=9, fill="#929298"),
            line(27, 948, 221, 948, stroke="#3b3b40"),
            text(30, 975, "demo-admin", size=10, fill=PAPER, weight=650),
            text(191, 975, "Sign out", size=9, fill="#d8b8f4", anchor="middle"),
            text(298, 55, kicker, size=11, fill=PURPLE_DARK, weight=800, spacing=1.65),
            multiline(298, 91, title_lines, size=17, fill=MUTED, leading=25),
            pill(1421, 40, 135, "●  Production · ready", fill=PAPER, stroke=LINE, color=INK),
        ]
    )
    out.append(stepper(screen_index))
    return out


def stepper(current):
    labels = [("Scope", "Select project"), ("Discover", "Map resources"), ("Match", "Find modules"), ("Generate", "Agent composition"), ("Verify", "Local plan"), ("Deliver", "Git branch / PR"), ("Import", "HCP lifecycle")]
    x = 298
    y = 145
    width = 1258
    parts = [rect(x, y, width, 84, fill=PAPER, stroke=LINE)]
    step_width = 166
    for idx, (label, sublabel) in enumerate(labels):
        sx = x + 30 + idx * 174
        state_color = PURPLE_DARK if idx <= current else "#a0a0a5"
        if idx < current:
            parts.extend([circle(sx, y + 42, 17, fill=PURPLE_PALE, stroke=PURPLE, width=1.5), text(sx, y + 47, "✓", size=13, fill=PURPLE_DARK, weight=800, anchor="middle")])
        elif idx == current:
            parts.extend([circle(sx, y + 42, 17, fill=PURPLE, stroke=PURPLE), text(sx, y + 47, str(idx + 1), size=12, fill=PAPER, weight=800, anchor="middle")])
        else:
            parts.extend([circle(sx, y + 42, 17, fill=PAPER, stroke="#c8c8cd"), text(sx, y + 47, str(idx + 1), size=12, fill="#a0a0a5", weight=650, anchor="middle")])
        parts.extend([text(sx + 25, y + 37, label, size=12, fill=state_color, weight=730), text(sx + 25, y + 54, sublabel, size=9, fill=state_color)])
        if idx < 6:
            parts.append(line(sx + 123, y + 42, sx + 157, y + 42))
    return "".join(parts)


def activity(events):
    x, y, width, height = 1240, 251, 316, 675
    parts = [panel(x, y, width, height), text(x + 24, y + 34, "AUDIT TRAIL", size=10, fill=PURPLE_DARK, weight=800, spacing=1.5), text(x + 24, y + 68, "Run activity", size=24, fill="#000", weight=730)]
    cy = y + 112
    for index, event in enumerate(events):
        parts.extend([circle(x + 32, cy - 4, 5, fill=PURPLE), circle(x + 32, cy - 4, 10, fill="none", stroke=PURPLE_PALE, width=5)])
        if index < len(events) - 1:
            parts.append(line(x + 32, cy + 8, x + 32, cy + 48, stroke=LINE))
        parts.append(multiline(x + 55, cy, event if isinstance(event, tuple) else (event,), size=11, fill=MUTED, leading=17))
        cy += 70 if isinstance(event, tuple) and len(event) > 1 else 54
    parts.extend([rect(x + 24, y + height - 106, width - 48, 76, fill=PURPLE_PALE), rect(x + 24, y + height - 106, 3, 76, fill=PURPLE), text(x + 44, y + height - 76, "Review stays mandatory", size=11, fill=INK, weight=720), text(x + 44, y + height - 55, "No automatic apply", size=10, fill=MUTED)])
    return "".join(parts)


def resource_row(y, icon, name, resource_type, location, badge, *, checked=True):
    color = GREEN if badge == "Supported" else AMBER
    badge_fill = GREEN_PALE if badge == "Supported" else AMBER_PALE
    return "".join(
        [
            rect(324, y, 874, 66, fill=PAPER, stroke="#e1e1e4"),
            checkbox(340, y + 24, checked),
            rect(376, y + 15, 36, 36, fill=PURPLE),
            text(394, y + 38, icon, size=9, fill=PAPER, weight=800, anchor="middle"),
            text(428, y + 28, name, size=12, fill=INK, weight=720),
            text(428, y + 47, f"{resource_type} · {location} · 0 dependencies", size=9, fill=MUTED),
            pill(1088, y + 18, 88, badge, fill=badge_fill, stroke="#9ed6ca" if badge == "Supported" else "#e8ca91", color=color),
        ]
    )


def render_discover():
    out = base(1, "INFRASTRUCTURE ADOPTION", ("Discover what exists, resolve dependencies, and select", "the resources that should enter governed Terraform."))
    out.extend(
        [
            panel(298, 251, 920, 675),
            text(324, 287, "DISCOVERY", size=10, fill=PURPLE_DARK, weight=800, spacing=1.5),
            text(324, 321, "Review existing infrastructure", size=24, fill="#000", weight=730),
            text(1178, 301, "6 resources", size=11, fill=INK, weight=700, anchor="end"),
            multiline(324, 353, ("Choose the deployed resources TerraMig should adopt. Dependencies are included automatically.", "Partial and previously imported resources remain unselected by default."), size=11, fill=MUTED, leading=18),
            rect(324, 401, 874, 118, fill="#f7f6f8", stroke=LINE),
            text(340, 426, "4 selected", size=11, fill=INK, weight=720),
            text(340, 445, "Supported resources grouped for review", size=9, fill=MUTED),
            rect(340, 462, 260, 36, fill=PAPER, stroke="#bdbdc2", radius=2),
            text(353, 484, "Search name, type, location…", size=9, fill="#8d8d92"),
            rect(613, 462, 168, 36, fill=PAPER, stroke="#bdbdc2", radius=2),
            text(626, 484, "Migration domain  ▾", size=9, fill=INK),
            button(1012, 463, 166, "Select supported unmanaged"),
            rect(324, 539, 874, 46, fill="#f7f6f8", stroke=LINE),
            text(340, 561, "Core networking", size=11, fill=INK, weight=720),
            text(340, 578, "2 resources · 2 selected", size=9, fill=MUTED),
            button(1044, 545, 134, "Select group"),
            resource_row(586, "NET", "app-vpc", "compute.googleapis.com/Network", "global", "Supported"),
            resource_row(653, "SUB", "app-subnet-eu", "compute.googleapis.com/Subnetwork", "europe-west1", "Supported"),
            rect(324, 735, 874, 46, fill="#f7f6f8", stroke=LINE),
            text(340, 757, "Data and storage", size=11, fill=INK, weight=720),
            text(340, 774, "2 resources · 2 selected", size=9, fill=MUTED),
            resource_row(782, "DB", "orders-primary", "sqladmin.googleapis.com/Instance", "europe-west1", "Supported"),
            resource_row(849, "OBJ", "archive-bucket", "storage.googleapis.com/Bucket", "eu", "Supported"),
            activity(("Workflow created", "Discovered 6 resources", ("Capability assessment:", "6 supported"), "Selected 4 resources")),
        ]
    )
    out.append("</svg>")
    return "".join(out)


def option(y, selected, title, detail, badge, *, composite=False):
    fill = PURPLE_PALE if selected else PAPER
    stroke = PURPLE if selected else LINE
    badge_fill = AMBER_PALE if composite else GREEN_PALE if badge == "PRIVATE" else PAPER
    badge_color = AMBER if composite else GREEN if badge == "PRIVATE" else PURPLE_DARK
    badge_stroke = "#e8ca91" if composite else "#9ed6ca" if badge == "PRIVATE" else "#c7b0df"
    return "".join(
        [
            rect(346, y, 826, 62, fill=fill, stroke=stroke, radius=0),
            radio(364, y + 31, selected),
            text(386, y + 26, title, size=11, fill=INK, weight=720),
            text(386, y + 45, detail, size=9, fill=MUTED),
            pill(1070, y + 17, 84, badge, fill=badge_fill, stroke=badge_stroke, color=badge_color),
        ]
    )


def render_match():
    out = base(2, "INFRASTRUCTURE ADOPTION", ("Match each selected resource to a trusted private module", "or keep the deterministic provider-resource fallback."))
    out.extend(
        [
            panel(298, 251, 920, 675),
            text(324, 287, "MODULE MATCHING", size=10, fill=PURPLE_DARK, weight=800, spacing=1.5),
            text(324, 321, "Select the Terraform representation", size=24, fill="#000", weight=730),
            pill(1088, 278, 104, "2 module matches"),
            multiline(324, 354, ("Private Library modules are trusted by provenance. Keep direct resources when a module", "does not represent the deployed infrastructure exactly."), size=11, fill=MUTED, leading=18),
            rect(324, 402, 874, 58, fill="#f7f6f8", stroke=LINE),
            text(340, 426, "Bulk selection", size=11, fill=INK, weight=720),
            text(340, 445, "Latest usable versions resolved from the registry", size=9, fill=MUTED),
            button(852, 414, 176, "Latest compatible"),
            button(1038, 414, 140, "Use direct resources"),
            rect(324, 478, 874, 48, fill="#f7f6f8", stroke=LINE),
            text(340, 500, "Data and storage", size=11, fill=INK, weight=720),
            text(340, 517, "2 resources · 1 module · 1 direct", size=9, fill=MUTED),
            button(1044, 485, 134, "Latest compatible"),
            rect(324, 541, 874, 242, fill=PAPER, stroke="#e1e1e4"),
            rect(340, 558, 36, 36, fill=PURPLE),
            text(358, 581, "OBJ", size=9, fill=PAPER, weight=800, anchor="middle"),
            text(390, 572, "archive-bucket", size=13, fill=INK, weight=720),
            text(390, 590, "storage.googleapis.com/Bucket · europe-west1", size=9, fill=MUTED),
            text(1168, 579, "View details →", size=9, fill=PURPLE_DARK, weight=700, anchor="end"),
            option(611, True, "app.terraform.io/example-org/terraform-google-storage/gcp @ 3.2.0", "96% match · HCP Private Library · single-resource ownership", "PRIVATE"),
            option(682, False, "google_storage_bucket", "Direct provider resource · deterministic import ID preserved", "DIRECT"),
            rect(324, 799, 874, 102, fill=PAPER, stroke="#e1e1e4"),
            rect(340, 815, 36, 36, fill=PURPLE),
            text(358, 838, "DB", size=9, fill=PAPER, weight=800, anchor="middle"),
            text(390, 829, "orders-primary", size=13, fill=INK, weight=720),
            text(390, 847, "sqladmin.googleapis.com/Instance · europe-west1", size=9, fill=MUTED),
            option(854, True, "google_sql_database_instance", "Direct provider resource selected for exact brownfield ownership", "DIRECT"),
            activity(("Workflow created", "Selected 4 resources", "Private registry scanned", ("Found module candidates", "for 2 resources"), "Selections reviewed")),
        ]
    )
    out.append("</svg>")
    return "".join(out)


def terminal(x, y, width, height, label, rows):
    parts = [rect(x, y, width, height, fill="#111113", stroke="#34343a"), rect(x, y, width, 34, fill="#202024", stroke="#34343a")]
    for idx, color in enumerate(("#d05a67", "#d9a23b", "#35a17f")):
        parts.append(circle(x + 18 + idx * 18, y + 17, 5, fill=color))
    parts.append(text(x + 76, y + 21, label, size=9, fill="#b9b9c0", weight=700, family="ui-monospace,Menlo,monospace"))
    ty = y + 62
    for row in rows:
        parts.append(text(x + 18, ty, row, size=10, fill="#e8dafa", family="ui-monospace,Menlo,monospace"))
        ty += 25
    return "".join(parts)


def check_row(x, y, label):
    return "".join(
        [
            circle(x, y, 9, fill=GREEN),
            f'<path d="M{x-4} {y} l3 3 6 -7" fill="none" stroke="white" stroke-width="2" stroke-linecap="round"/>',
            text(x + 22, y + 4, label, size=10, fill=INK, weight=620),
        ]
    )


def render_verify():
    out = base(4, "INFRASTRUCTURE ADOPTION", ("Validate the generated Terraform and prove that the plan", "contains imports only before Git or HCP submission."))
    out.extend(
        [
            panel(298, 251, 920, 356),
            text(324, 287, "LIVE TERRAFORM SESSION", size=10, fill=PURPLE_DARK, weight=800, spacing=1.5),
            text(324, 321, "Validation and import-plan terminal", size=24, fill="#000", weight=730),
            pill(1098, 278, 92, "COMPLETED", fill=GREEN_PALE, stroke="#9ed6ca", color=GREEN),
            text(324, 354, "Commands, diagnostics, and bounded output are retained as review evidence.", size=11, fill=MUTED),
            terminal(324, 382, 868, 198, "verify · completed", ("$ terraform init -backend=false -input=false", "$ terraform validate -json", "Success! The configuration is valid.", "$ terraform plan -refresh=true -detailed-exitcode", "Plan: 0 to add, 0 to change, 0 to destroy", "$ 4 deterministic imports covered")),
            panel(298, 629, 920, 297),
            text(324, 666, "LOCAL TERRAFORM GATE", size=10, fill=PURPLE_DARK, weight=800, spacing=1.5),
            text(324, 700, "Validate and drift plan", size=24, fill="#000", weight=730),
            pill(1100, 657, 90, "PASSED", fill=GREEN_PALE, stroke="#9ed6ca", color=GREEN),
            rect(324, 727, 868, 68, fill=GREEN_PALE, stroke="#9ed6ca"),
            circle(350, 761, 12, fill=GREEN),
            text(350, 765, "✓", size=12, fill=PAPER, weight=800, anchor="middle"),
            text(376, 754, "Import-only plan · no drift detected", size=12, fill="#075d4c", weight=740),
            text(376, 775, "0 to add · 0 to change · 0 to destroy · 4 imports covered", size=10, fill="#3a7565"),
            check_row(342, 827, "Terraform configuration is valid"),
            check_row(342, 858, "Every selected resource has a deterministic import"),
            check_row(716, 827, "No create, update, or destroy actions"),
            check_row(716, 858, "Review evidence retained"),
            button(997, 872, 195, "Continue to Git delivery", primary=True),
            activity(("Workflow created", "Discovered 6 resources", "Selected 4 resources", "AI proposal accepted", ("Local verification passed", "No drift detected"))),
        ]
    )
    out.append("</svg>")
    return "".join(out)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rendered = {
        "discover.svg": render_discover(),
        "match.svg": render_match(),
        "verify.svg": render_verify(),
    }
    for filename, content in rendered.items():
        (OUTPUT / filename).write_text(content + "\n", encoding="utf-8")
        print(f"rendered {OUTPUT / filename}")


if __name__ == "__main__":
    main()
