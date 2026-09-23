#!/usr/bin/env python3
"""Generate a Nerd Font + truecolor mockup of `alloy monitor` (100 and 80 cols)."""
import unicodedata

OUT = "docs/plans/monitor-nerd-font-mockup.ansi"

# ---- palette (docs/plans/monitor-tui-redesign.md) -------------------------
PAGE = "#0a0d12"; PANEL = "#12161d"; BORDER = "#2a3038"; TEXT = "#c9d1d9"
DIM = "#6b7280"; ACCENT = "#79c0ff"; CYAN = "#56b6c2"; MAGENTA = "#d2a8ff"
RED = "#f85149"; GREEN = "#7ee787"; GRAY = "#8b949e"; YELLOW = "#e3b341"
TRACK = "#1f252e"; SEL = "#1b2533"; SEG2 = "#1f2a37"; SEG3 = "#263241"

# ---- glyphs (one table, like the icon table render.py should get) ---------
G = {
    "pl_r": "", "pl_r_thin": "", "pl_l": "",
    "cap_l": "", "cap_r": "",
    "branch": "",
    "alloy": "",      # flask
    "cog": "", "tasks": "", "clock": "", "check": "",
    "cross": "", "ban": "", "play": "", "gavel": "",
    "warn": "", "db": "", "chip": "", "dash": "",
    "list": "", "info": "", "term": "", "folder": "",
    "file": "", "history": "", "refresh": "", "sitemap": "",
    "dot": "", "plug": "", "bolt": "", "hourglass": "",
    "arrow": "", "user": "", "fork": "", "keyboard": "",
}
STATUS = {  # icon, color
    "running": (G["play"], CYAN), "judge": (G["gavel"], MAGENTA),
    "blocked": (G["ban"], RED), "done": (G["check"], GREEN), "ready": (G["clock"], GRAY),
}
EIGHTHS = " ▏▎▍▌▋▊▉"


def cw(s):
    """Display width: wide/fullwidth East Asian = 2, combining = 0, else 1."""
    n = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def rgb(h):
    h = h.lstrip("#"); return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def sgr(fg=None, bg=None, bold=False):
    c = ["0"]
    if bold: c.append("1")
    if fg: c.append("38;2;%d;%d;%d" % rgb(fg))
    if bg: c.append("48;2;%d;%d;%d" % rgb(bg))
    return "\x1b[" + ";".join(c) + "m"


class Line:
    def __init__(self, bg=PANEL):
        self.segs = []; self.bg = bg

    def add(self, text, fg=TEXT, bg=None, bold=False):
        self.segs.append((text, fg, bg or self.bg, bold)); return self

    def width(self):
        return sum(cw(t) for t, *_ in self.segs)

    def pad_to(self, w, bg=None):
        gap = w - self.width()
        if gap > 0: self.add(" " * gap, bg=bg)
        return self

    def render(self):
        return "".join(sgr(fg, bg, b) + t for t, fg, bg, b in self.segs) + "\x1b[0m"


def fit(text, w, align="l"):
    """Truncate with an ellipsis and pad on display width."""
    if cw(text) > w:
        out = ""
        for ch in text:
            if cw(out + ch) > w - 1: break
            out += ch
        text = out + "…"
    pad = " " * (w - cw(text))
    return pad + text if align == "r" else text + pad


# ---- panel chrome -----------------------------------------------------------
def top_border(width, icon, title, right=None):
    l = Line().add("╭─", BORDER).add("┤ ", BORDER).add(icon + " ", ACCENT)
    l.add(title, ACCENT, bold=True).add(" ├", BORDER)
    right_w = cw(right) + 4 if right else 0
    l.add("─" * (width - l.width() - 1 - right_w), BORDER)
    if right:
        l.add("┤ ", BORDER).add(right, DIM).add(" ├", BORDER)
    return l.add("╮", BORDER)


def bottom_border(width, hint=None):
    l = Line().add("╰", BORDER)
    if hint:
        l.add("─" * (width - 2 - cw(hint) - 4), BORDER).add("┤ ", BORDER)
        l.add(hint, DIM).add(" ├", BORDER)
    else:
        l.add("─" * (width - 2), BORDER)
    return l.add("╯", BORDER)


def boxed(width, inner):
    """Wrap an inner Line in │ … │ padded to width."""
    l = Line().add("│ ", BORDER)
    l.segs += inner.segs
    l.pad_to(width - 2)
    return l.add(" │", BORDER)


# ---- header / stats -----------------------------------------------------------
def title_line(width):
    l = Line(bg=PAGE)
    l.add(" " + G["alloy"] + " alloy ", PAGE, ACCENT, bold=True)
    l.add(G["pl_r"], ACCENT, SEG2)
    l.add(" monitor ", TEXT, SEG2)
    l.add(G["pl_r"], SEG2, SEG3)
    l.add(" " + G["folder"] + " ~/MY_SRC/alloy ", GRAY, SEG3)
    l.add(G["pl_r"], SEG3, PAGE)
    right = Line(bg=PAGE)
    right.add(G["pl_l"], SEG3, PAGE).add(" " + G["refresh"] + " 1s ", GRAY, SEG3)
    right.add(G["pl_l"], SEG2, SEG3).add(" " + G["clock"] + " 14:32:07 ", TEXT, SEG2)
    l.pad_to(width - right.width()); l.segs += right.segs
    return l


def stats_line(width, compact=False):
    l = Line(bg=PAGE)
    l.add(" " + G["dot"] + " ", GREEN, SEG2).add("scheduler", TEXT, SEG2, bold=True)
    l.add("" if compact else " pid 48213", DIM, SEG2).add(" ", bg=SEG2)
    l.add(G["pl_r"], SEG2, SEG3)
    l.add(" " + G["tasks"] + " ready ", TEXT, SEG3).add("4", ACCENT, SEG3, bold=True)
    l.add("" if compact else "/20", DIM, SEG3).add(" ", bg=SEG3)
    l.add(G["pl_r"], SEG3, PAGE)
    l.add("  " + G["check"] + " ", GREEN).add("128", TEXT, bold=True)
    l.add("  " + G["cross"] + " ", RED).add("3", TEXT, bold=True)
    l.add("  " + G["ban"] + " ", GRAY).add("1", TEXT, bold=True)
    if not compact:
        l.add("   " + G["pl_r_thin"] + " ", BORDER).add("session ", DIM)
        l.add("+6 " + G["check"] + "  +0 " + G["cross"] + "  +1 " + G["ban"], GRAY)
    else:
        l.add("  " + G["pl_r_thin"] + " ", BORDER).add("+6/0/1", GRAY)
    return l.pad_to(width)


# ---- limits -----------------------------------------------------------------------
def usage_color(p):
    return RED if p >= 80 else YELLOW if p >= 50 else GREEN


def bar(line, pct, cells, color):
    eighths = round(pct * cells * 8 / 100)
    full, part = divmod(eighths, 8)
    s = "█" * full + (EIGHTHS[part] if part else "")
    line.add(s, color, TRACK).add(" " * (cells - cw(s)), color, TRACK)


def window(line, label, pct, reset, cells, stale=None, label_w=6):
    color = usage_color(pct)
    # one color span covering label + percent + bar (lesson: wrap full segment)
    icon = G["warn"] if pct >= 80 else " "
    line.add(icon + " " + fit(label, label_w) + fit(f"{pct}%", 4, "r") + " ", color, bold=pct >= 80)
    bar(line, pct, cells, color)
    line.add(" " + G["clock"] + " " + fit(reset, 9 if not stale else cw(reset)), DIM)
    if stale:
        line.add(" ")
        line.add(G["cap_l"], "#3a3016").add(G["history"] + " " + stale, YELLOW, "#3a3016")
        line.add(G["cap_r"], "#3a3016")


def limits_panel(width, compact=False):
    cells = 8 if compact else 12
    out = [top_border(width, G["dash"], "limits", None if compact else "3 harnesses")]
    rows = [
        ("claude", [("5h", 34, "18:00"), ("weekly", 71, "Mon 09:00")], None),
        ("codex", [("5h", 88, "15:10"), ("weekly", 52, "Thu 02:00")], "42m old"),
    ]
    for name, wins, stale in rows:
        l = Line().add(G["plug"] + " ", GRAY).add(fit(name, 7), TEXT, bold=True)
        for i, (lab, pct, rst) in enumerate(wins):
            if compact and i:  # 80 cols: second window on its own line
                out.append(boxed(width, l))
                l = Line().add(" " * 9)
            window(l, lab, pct, rst, cells,
                   stale=(stale if not compact or not stale else "42m") if i == len(wins) - 1 else None)
        out.append(boxed(width, l))
    l = Line().add(G["plug"] + " ", GRAY).add(fit("jev", 7), DIM, bold=True)
    l.add(G["cap_l"], "#3d1a1a").add(G["ban"] + " unavailable", RED, "#3d1a1a", bold=True)
    l.add(G["cap_r"], "#3d1a1a").add(" TYPESAFE_API_KEY not set", RED)
    if not compact:
        l.add("  " + G["pl_r_thin"] + " auth", DIM)
    out.append(boxed(width, l))
    out.append(bottom_border(width))
    return out


# ---- runs table ----------------------------------------------------------------------
RUNS = [  # parent group, then children
    ("alloy-vrh", "Monitor TUI redesign", [
        dict(bead="vrh.9", recipe="tdd-loop", status="running", stage="verify", it="2/5", cons="0/2",
             ok=14, fail=1, el="12m", now="claude 23s", tok="54.2k",
             judge="—", cx=2),
        dict(bead="vrh.10", recipe="tdd-loop", status="judge", stage="judge", it="1/5", cons="1/2",
             ok=22, fail=0, el="4m", now="codex 8s", tok="31.7k",
             judge="rt" + G["arrow"] + "acc", cx=1),
        dict(bead="vrh.11", recipe="tdd-jev", status="ready", stage="—", it="—", cons="—",
             ok=None, fail=None, el="—", now="queued", tok="—", judge="—", cx=1),
    ]),
    ("alloy-o89", "Limits panel", [
        dict(bead="o89.3", recipe="tdd-loop", status="blocked", stage="human", it="3/5", cons="2/2",
             ok=9, fail=2, el="1h04m", now="human gate", tok="118.9k", judge="reject", cx=3),
        dict(bead="o89.2", recipe="tdd-loop", status="done", stage="—", it="2/5", cons="0/2",
             ok=31, fail=0, el="8m", now="—", tok="42.0k", judge="accept", cx=2),
    ]),
]
SELECTED = "vrh.9"
CX_BARS = {1: ("▂", GREEN, "simple"), 2: ("▂▄", YELLOW, "medium"),
           3: ("▂▄▆", RED, "complex")}

# (key, header, header-icon, width, align, min-layout)  layout: "c" comfortable(100), "w" wide(80)
COLS_100 = [("bead", "bead", "", 9, "l"), ("recipe", "recipe", "", 8, "l"),
            ("status", "status", "", 11, "l"), ("stage", "stage", "", 6, "l"),
            ("it", "iter", "", 4, "r"), ("cons", "cons", "", 4, "r"),
            ("tests", "tests", "", 7, "r"), ("el", "time", "", 5, "r"),
            ("now", "now", G["term"], 12, "l"), ("tok", "tok", G["db"], 7, "r"),
            ("judge", "judge", G["gavel"], 7, "l"), ("cx", "", G["chip"], 3, "l")]
COLS_80 = [("bead", "bead", "", 9, "l"), ("recipe", "recipe", "", 8, "l"),
           ("status", "status", "", 11, "l"), ("it", "iter", "", 4, "r"),
           ("tests", "tests", "", 8, "r"), ("el", "elapsed", "", 7, "r"),
           ("tok", "tokens", G["db"], 8, "r"), ("judge", "judge", G["gavel"], 8, "l")]


def cell(line, key, run, w, align, bg):
    if key == "status":
        icon, color = STATUS[run["status"]]
        pill = f"{icon} {run['status']}"
        line.add(G["cap_l"], color, bg).add(pill, PAGE, color, bold=True).add(G["cap_r"], color, bg)
        line.add(" " * (w - cw(pill) - 2), bg=bg)
    elif key == "tests":
        if run["ok"] is None:
            line.add(fit("—", w, "r"), DIM, bg); return
        ok = f"{G['check']}{run['ok']}"
        fail = f"{G['cross']}{run['fail']}" if run["fail"] else ""
        txt = ok + (" " + fail if fail else "")
        line.add(" " * (w - cw(txt)), bg=bg).add(ok, GREEN, bg)
        if fail: line.add(" ", bg=bg).add(fail, RED, bg, bold=True)
    elif key == "cx":
        b, color, _ = CX_BARS[run["cx"]]
        line.add(fit(b, w), color, bg)
    elif key == "judge":
        j = run["judge"]
        color = GREEN if j == "accept" else RED if j == "reject" else YELLOW if "retry" in j else DIM
        line.add(fit(j, w), color, bg)
    elif key == "now":
        color = CYAN if run["now"][-1] == "s" else YELLOW if "gate" in run["now"] else DIM
        line.add(fit(run["now"], w), color, bg)
    else:
        v = run[key]
        color = DIM if v == "—" else TEXT
        if key == "bead": color = TEXT
        line.add(fit(v, w, align), color, bg, bold=key == "bead" and run["bead"] == SELECTED)


def runs_panel(width, cols, show_groups=True):
    out = [top_border(width, G["list"], "runs", "5 runs  " + G["play"] + " 1  " + G["ban"] + " 1")]
    gap = 1
    # header row
    h = Line().add("  ")
    for i, (key, name, icon, w, align) in enumerate(cols):
        label = (icon + " " + name).rstrip() if icon else name
        if key == "bead": label = "  " + name   # room for tree glyphs
        h.add(fit(label, w, align), DIM, bold=True)
        if i < len(cols) - 1: h.add(" " * gap)
    out.append(boxed(width, h))
    sep = Line().add("─" * (width - 4), BORDER)
    out.append(boxed(width, sep))
    for parent, title, children in RUNS:
        g = Line().add(G["sitemap"] + " ", ACCENT).add(parent, ACCENT, bold=True).add("  " + title, DIM)
        out.append(boxed(width, g))
        for idx, run in enumerate(children):
            sel = run["bead"] == SELECTED
            bg = SEL if sel else PANEL
            r = Line(bg=bg)
            r.add("▌ " if sel else "  ", ACCENT, bg)
            tree = "└ " if idx == len(children) - 1 else "├ "
            for i, (key, name, icon, w, align) in enumerate(cols):
                if key == "bead":
                    r.add(tree, BORDER, bg)
                    cell(r, key, run, w - 2, align, bg)
                else:
                    cell(r, key, run, w, align, bg)
                if i < len(cols) - 1: r.add(" " * gap, bg=bg)
            r.pad_to(width - 4, bg=bg)
            line = Line().add("│ ", BORDER); line.segs += r.segs; line.add(" │", BORDER)
            out.append(line)
    out.append(bottom_border(width))
    return out


# ---- detail pane -------------------------------------------------------------------------
def detail_panel(width, stacked=False):
    out = [top_border(width, G["info"], "vrh.9 \u00b7 Nerd icon table" if width >= 100 else "vrh.9", "running " + G["play"] + " verify")]
    left = []
    def L(*segs):
        l = Line()
        for s in segs: l.add(*s)
        left.append(l)
    L((G["term"] + " ", CYAN), ("verify  ", TEXT, None, True), ("claude:opus ", DIM),
      (G["arrow"] + " ", DIM), ("claude:sonnet", CYAN), ("   23s", TEXT))
    L((G["gavel"] + " ", MAGENTA), ("judge   ", TEXT, None, True), ("retry ", YELLOW),
      ("conf 0.62", DIM), ("  iter 2/5", DIM))
    L((G["db"] + " ", ACCENT), ("tokens  ", TEXT, None, True), ("in ", DIM), ("48.1k", TEXT),
      ("  out ", DIM), ("6.1k", TEXT))
    for role, tot, pct in (("impl", "38.9k", 72), ("verify", "11.2k", 21), ("context", "4.1k", 7)):
        l = Line().add("   " + fit(role, 8), DIM).add(fit(tot, 6, "r") + " ", TEXT)
        bar(l, pct, 10, ACCENT)
        left.append(l)
    L((G["chip"] + " ", GRAY), ("models  ", TEXT, None, True), ("claude:opus ", TEXT), ("×3  ", DIM),
      ("claude:sonnet ", TEXT), ("×1", DIM))
    right = []
    def R(icon, key, val, color=TEXT):
        right.append(Line().add(icon + " ", GRAY).add(fit(key, 9), DIM).add(val, color))
    R(G["hourglass"], "cx", "▂▄ medium (est.)", YELLOW)
    R(G["folder"], "worktree", "…/wt/alloy-vrh.9")
    R(G["branch"], "branch", "alloy/alloy-vrh.9", ACCENT)
    R(G["file"], "logs", "…/logs/r-7f3a91")
    R(G["fork"], "parent", "alloy-vrh", ACCENT)
    if stacked:
        for l in left + [Line().add("─" * 20, BORDER)] + right:
            out.append(boxed(width, l))
    else:
        lw = max(l.width() for l in left) + 3
        for i in range(max(len(left), len(right))):
            l = Line()
            if i < len(left): l.segs += left[i].segs
            l.pad_to(lw - 2)
            l.add("│ ", BORDER)
            if i < len(right): l.segs += right[i].segs
            out.append(boxed(width, l))
    out.append(bottom_border(width))
    return out


# ---- footer -----------------------------------------------------------------------------------
def footer(width, compact=False):
    keys = [("j/k", "move"), ("⏎", "detail"), ("l", "logs"), ("r", "refresh"),
            ("c", "cancel"), ("q", "quit")]
    if compact: keys = [k for k in keys if k[0] not in ("l", "c")]
    l = Line(bg=PAGE).add(" ")
    for k, d in keys:
        l.add(G["cap_l"], ACCENT, PAGE).add(k, PAGE, ACCENT, bold=True).add(G["cap_r"], ACCENT, PAGE)
        l.add(" " + d + "  ", GRAY)
    right = Line(bg=PAGE).add(G["keyboard"] + " icons: nerd ", DIM)
    l.pad_to(width - right.width()); l.segs += right.segs
    return l


def screen(width, compact):
    lines = [title_line(width), stats_line(width, compact)]
    lines += limits_panel(width, compact)
    lines += runs_panel(width, COLS_80 if compact else COLS_100)
    lines += detail_panel(width, stacked=False)
    lines.append(footer(width, compact))
    for i, l in enumerate(lines):
        assert l.width() == width, (width, i, l.width(), "".join(s[0] for s in l.segs))
    return lines


def caption(text):
    return "\x1b[0m\x1b[2m" + text + "\x1b[0m"


with open(OUT, "w", encoding="utf-8") as f:
    f.write(caption("── alloy monitor · Nerd Font mockup · 100 columns (comfortable) ──") + "\n")
    for l in screen(100, False): f.write(l.render() + "\n")
    f.write("\n" + caption("── same state at 80 columns (wide tier: no stage/cons/now/cx; "
                           "limits wrap; detail keeps 2 cols) ──") + "\n")
    for l in screen(80, True): f.write(l.render() + "\n")
print("wrote", OUT)
