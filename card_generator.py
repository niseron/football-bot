"""
card_generator.py — Branded 1080×1080 PNG cards for thepicksai.

Three card types:
  generate_picks_card()   — daily / evening picks
  generate_results_card() — daily settled results
  generate_weekly_card()  — Monday weekly summary
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Palette ───────────────────────────────────────────────────────────────────
_BG     = (6,   6,   6)
_NEON   = (0,   255, 136)
_N_DIM  = (0,   170, 90)
_WHITE  = (255, 255, 255)
_DIM    = (148, 148, 148)
_WIN    = (0,   255, 136)
_LOSS   = (255, 51,  85)
_VOID   = (200, 175, 60)
_GRID   = (13,  43,  26)
_SEP    = (28,  58,  42)

SIZE = 1080
PAD  = 64               # outer margin
IW   = SIZE - 2 * PAD  # 952 usable px

CARDS_DIR = Path(__file__).parent / "cards"

_FC: dict[tuple, ImageFont.FreeTypeFont] = {}


# ── Font helpers ──────────────────────────────────────────────────────────────

def _font(sz: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (sz, bold)
    if key in _FC:
        return _FC[key]
    paths = (
        [r"C:\Windows\Fonts\consolab.ttf",
         r"C:\Windows\Fonts\courbd.ttf",
         r"C:\Windows\Fonts\arialbd.ttf"]
        if bold else
        [r"C:\Windows\Fonts\consola.ttf",
         r"C:\Windows\Fonts\cour.ttf",
         r"C:\Windows\Fonts\arial.ttf"]
    )
    for p in paths:
        try:
            f = ImageFont.truetype(p, sz)
            _FC[key] = f
            return f
        except OSError:
            pass
    f = ImageFont.load_default()
    _FC[key] = f
    return f


# ── Drawing primitives ────────────────────────────────────────────────────────

def _tw(s: str, f: ImageFont.FreeTypeFont) -> int:
    bb = f.getbbox(s)
    return bb[2] - bb[0]


def _th(f: ImageFont.FreeTypeFont) -> int:
    bb = f.getbbox("Ag")
    return bb[3] - bb[1]


def _cx(d: ImageDraw.ImageDraw, y: int, s: str, f: ImageFont.FreeTypeFont, c: tuple) -> None:
    d.text(((SIZE - _tw(s, f)) // 2, y), s, font=f, fill=c)


def _spaced_cx(d: ImageDraw.ImageDraw, y: int, s: str,
               f: ImageFont.FreeTypeFont, c: tuple, gap: int = 6) -> None:
    widths = [_tw(ch, f) for ch in s]
    total  = sum(widths) + gap * (len(s) - 1)
    x      = (SIZE - total) // 2
    for ch, w in zip(s, widths):
        d.text((x, y), ch, font=f, fill=c)
        x += w + gap


def _hr(d: ImageDraw.ImageDraw, y: int, color: tuple | None = None) -> None:
    d.line([(PAD, y), (SIZE - PAD, y)], fill=color or _N_DIM, width=1)


def _sep(d: ImageDraw.ImageDraw, y: int, x0: int = PAD) -> None:
    d.line([(x0, y), (SIZE - PAD, y)], fill=_SEP, width=1)


def _bracket(d: ImageDraw.ImageDraw) -> None:
    m, arm, t = 38, 55, 3
    e = SIZE - m
    for x, y, sx, sy in [(m, m, 1, 1), (e, m, -1, 1), (m, e, 1, -1), (e, e, -1, -1)]:
        d.line([(x, y), (x + sx * arm, y)], fill=_NEON, width=t)
        d.line([(x, y), (x, y + sy * arm)], fill=_NEON, width=t)


def _clip(s: str, f: ImageFont.FreeTypeFont, max_w: int) -> str:
    if _tw(s, f) <= max_w:
        return s
    while s and _tw(s + "…", f) > max_w:
        s = s[:-1]
    return (s + "…") if s else "…"


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (SIZE, SIZE), _BG)
    d   = ImageDraw.Draw(img)
    for x in range(0, SIZE + 1, 54):
        d.line([(x, 0), (x, SIZE)], fill=_GRID, width=1)
    for y in range(0, SIZE + 1, 54):
        d.line([(0, y), (SIZE, y)], fill=_GRID, width=1)
    return img, d


def _draw_header(d: ImageDraw.ImageDraw, subtitle: str) -> int:
    """Brand + subtitle + opening divider. Returns y cursor after divider."""
    y = PAD + 14
    _spaced_cx(d, y, "THEPICKSAI", _font(30, bold=True), _NEON, gap=6)
    y += _th(_font(30)) + 18
    _cx(d, y, subtitle, _font(21), _DIM)
    y += _th(_font(21)) + 26
    _hr(d, y)
    return y + 26


def _draw_footer(d: ImageDraw.ImageDraw, label: str) -> None:
    f  = _font(18)
    fy = SIZE - PAD - _th(f) - 14
    _hr(d, fy - 18)
    _cx(d, fy, label, f, _N_DIM)


def _pnl_str(v: float) -> str:
    return f"+{v:.2f}u" if v >= 0 else f"{v:.2f}u"


def _conf_color(conf: str) -> tuple:
    cl = conf.strip().lower()
    return _WIN if cl == "high" else _VOID if cl == "medium" else _LOSS


# ── Card 1: Daily picks ───────────────────────────────────────────────────────

def generate_picks_card(
    picks: list[dict],
    overall_win_rate: float = 0.0,
    card_date: date | None = None,
    session: str = "morning",
) -> Path:
    """
    Generate picks card. picks come from Claude + Kelly enrichment in main.py.
    session="morning" → picks_YYYY-MM-DD.png
    session="evening" → picks_YYYY-MM-DD_evening.png
    """
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    today  = card_date or date.today()
    datstr = today.strftime("%d %b %Y").upper()
    is_eve = session == "evening"

    img, d = _canvas()
    _bracket(d)

    subtitle = f"EVENING PICKS  ·  {datstr}" if is_eve else datstr
    y        = _draw_header(d, subtitle)

    f_num   = _font(19, bold=True)
    f_match = _font(24, bold=True)
    f_sub   = _font(19)
    f_stat  = _font(19)
    f_conf  = _font(16, bold=True)

    max_name_w = IW - 30 - 80  # room for pick number + confidence tag

    for i, p in enumerate(picks, 1):
        conf     = p.get("confidence", "")
        conf_tag = f"[{conf.upper()}]"
        conf_col = _conf_color(conf)

        # Pick number
        d.text((PAD, y + 4), str(i), font=f_num, fill=_NEON)
        x0 = PAD + 30

        # Match name (left) + confidence tag (right)
        name = _clip(p.get("match", ""), f_match, max_name_w)
        d.text((x0, y), name, font=f_match, fill=_WHITE)
        d.text((SIZE - PAD - _tw(conf_tag, f_conf), y + 6),
               conf_tag, font=f_conf, fill=conf_col)
        y += _th(f_match) + 7

        # Bet type · pick selection
        bet = _clip(f"{p.get('bet_type', '')}  ·  {p.get('pick', '')}", f_sub, IW - 30)
        d.text((x0, y), bet, font=f_sub, fill=_DIM)
        y += _th(f_sub) + 6

        # Odds + Kelly stake
        kelly = p.get("kelly")
        if kelly and kelly.get("stake", 0) > 0:
            stat = f"Odds {p.get('odds', '')}    Kelly €{kelly['stake']:.2f}"
        else:
            stat = f"Odds {p.get('odds', '')}"
        d.text((x0, y), stat, font=f_stat, fill=_NEON)
        y += _th(f_stat) + 22

        if i < len(picks):
            _sep(d, y, x0=x0)
            y += 16

    suffix  = "_evening" if is_eve else ""
    wr_lbl  = f"{overall_win_rate:.0f}% WIN RATE  ·  {'EVENING' if is_eve else 'DAILY'} PICKS"
    _draw_footer(d, wr_lbl)

    out = CARDS_DIR / f"picks_{today.strftime('%Y-%m-%d')}{suffix}.png"
    img.save(out, "PNG")
    return out


# ── Card 2: Daily results ─────────────────────────────────────────────────────

def generate_results_card(
    results: list[dict],
    card_date: date | None = None,
) -> Path:
    """
    results — list of settled pick dicts with keys: match, bet_type, pick, result, pnl.
    Only WIN / LOSS / VOID rows should be passed.
    """
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    today  = card_date or date.today()
    datstr = today.strftime("%d %b %Y").upper()

    img, d = _canvas()
    _bracket(d)

    y = _draw_header(d, f"RESULTS  ·  {datstr}")

    f_badge = _font(15, bold=True)
    f_match = _font(23, bold=True)
    f_sub   = _font(18)
    f_pnl   = _font(21, bold=True)

    total_pnl = 0.0
    settled   = [r for r in results if r.get("result") in ("WIN", "LOSS", "VOID")][:8]

    for i, r in enumerate(settled):
        res = r.get("result", "")
        pv  = r.get("pnl") or 0.0
        total_pnl += pv

        # Coloured result badge
        rc    = _WIN if res == "WIN" else _LOSS if res == "LOSS" else _VOID
        btxt  = res
        bw    = _tw(btxt, f_badge) + 14
        bh    = _th(f_badge) + 8
        try:
            d.rounded_rectangle([PAD, y + 2, PAD + bw, y + bh + 2], radius=3, fill=rc)
        except AttributeError:
            d.rectangle([PAD, y + 2, PAD + bw, y + bh + 2], fill=rc)
        d.text((PAD + 7, y + 5), btxt, font=f_badge, fill=_BG)

        x0 = PAD + bw + 14

        # Match name
        mname = _clip(r.get("match", ""), f_match, SIZE - PAD - x0 - 100)
        d.text((x0, y), mname, font=f_match, fill=_WHITE)

        # P&L right-aligned on same row
        pv_str = _pnl_str(pv)
        pv_col = _WIN if pv > 0 else _LOSS if pv < 0 else _DIM
        d.text((SIZE - PAD - _tw(pv_str, f_pnl), y + 2), pv_str, font=f_pnl, fill=pv_col)
        y += _th(f_match) + 8

        # Bet type below
        bet = _clip(f"{r.get('bet_type', '')}  ·  {r.get('pick', '')}", f_sub, IW - bw - 14)
        d.text((x0, y), bet, font=f_sub, fill=_DIM)
        y += _th(f_sub) + 20

        if i < len(settled) - 1:
            _sep(d, y)
            y += 14

    # Total P&L
    f_tot = _font(28, bold=True)
    _hr(d, y + 12)
    y += 40
    tot_str = f"DAILY P&L:  {_pnl_str(total_pnl)}"
    tot_col = _WIN if total_pnl > 0 else _LOSS if total_pnl < 0 else _DIM
    _cx(d, y, tot_str, f_tot, tot_col)

    _draw_footer(d, "THEPICKSAI")

    out = CARDS_DIR / f"results_{today.strftime('%Y-%m-%d')}.png"
    img.save(out, "PNG")
    return out


# ── Card 3: Weekly summary ────────────────────────────────────────────────────

def generate_weekly_card(data: dict) -> Path:
    """data — dict returned by excel_tracker.get_weekly_data()."""
    CARDS_DIR.mkdir(parents=True, exist_ok=True)

    w_start    = data.get("week_start", "")
    w_end      = data.get("week_end",   "")
    week_label = f"{w_start.upper()}  –  {w_end.upper()}"

    img, d = _canvas()
    _bracket(d)

    y = _draw_header(d, week_label)

    f_lbl = _font(17)
    f_big = _font(60, bold=True)
    f_med = _font(40, bold=True)
    f_sub = _font(20)

    # ── Row 1: PICKS / WINS / LOSSES ─────────────────────────────────────────
    col3   = IW // 3
    stats3 = [
        ("PICKS",  str(data.get("total_picks", 0)), _WHITE),
        ("WINS",   str(data.get("wins",        0)), _WIN),
        ("LOSSES", str(data.get("losses",      0)), _LOSS),
    ]
    for idx, (lbl, val, col) in enumerate(stats3):
        cx = PAD + idx * col3 + col3 // 2
        d.text((cx - _tw(lbl, f_lbl) // 2, y), lbl, font=f_lbl, fill=_DIM)
        d.text((cx - _tw(val, f_big) // 2, y + _th(f_lbl) + 10), val, font=f_big, fill=col)

    # Thin vertical dividers between columns
    for xi in [PAD + col3, PAD + 2 * col3]:
        d.line([(xi, y), (xi, y + _th(f_lbl) + 10 + _th(f_big))], fill=_SEP, width=1)

    y += _th(f_lbl) + 10 + _th(f_big) + 36
    _hr(d, y, color=_GRID)
    y += 30

    # ── Row 2: WIN RATE / WEEKLY P&L ─────────────────────────────────────────
    wr  = data.get("win_rate",  0.0)
    pw  = data.get("pnl_week",  0.0)
    col2 = IW // 2
    stats2 = [
        ("WIN RATE",   f"{wr:.1f}%", _WHITE),
        ("WEEKLY P&L", _pnl_str(pw), _WIN if pw >= 0 else _LOSS),
    ]
    for idx, (lbl, val, col) in enumerate(stats2):
        cx = PAD + idx * col2 + col2 // 2
        d.text((cx - _tw(lbl, f_lbl) // 2, y), lbl, font=f_lbl, fill=_DIM)
        d.text((cx - _tw(val, f_med) // 2, y + _th(f_lbl) + 10), val, font=f_med, fill=col)

    d.line([(PAD + col2, y), (PAD + col2, y + _th(f_lbl) + 10 + _th(f_med))],
           fill=_SEP, width=1)

    y += _th(f_lbl) + 10 + _th(f_med) + 40
    _hr(d, y, color=_GRID)
    y += 30

    # ── Best pick ─────────────────────────────────────────────────────────────
    best = data.get("best_pick")
    if best:
        d.text((PAD, y), "BEST PICK OF THE WEEK", font=f_lbl, fill=_DIM)
        y += _th(f_lbl) + 12

        f_bp   = _font(26, bold=True)
        bname  = _clip(best.get("match", ""), f_bp, IW - 120)
        bp_pv  = best.get("pnl") or 0.0
        bp_str = f"+{bp_pv:.2f}u"
        d.text((PAD, y), bname, font=f_bp, fill=_WHITE)
        d.text((SIZE - PAD - _tw(bp_str, f_bp), y), bp_str, font=f_bp, fill=_WIN)
        y += _th(f_bp) + 8

        bbet = _clip(f"{best.get('bet_type', '')}  ·  {best.get('pick', '')}", f_sub, IW)
        d.text((PAD, y), bbet, font=f_sub, fill=_DIM)
        y += _th(f_sub) + 32

    _hr(d, y, color=_GRID)
    y += 30

    # ── Running P&L ───────────────────────────────────────────────────────────
    rt     = data.get("running_total", 0.0)
    f_run  = _font(32, bold=True)
    rt_str = f"RUNNING P&L:  {_pnl_str(rt)}"
    rt_col = _WIN if rt >= 0 else _LOSS
    _cx(d, y, rt_str, f_run, rt_col)

    _draw_footer(d, "THEPICKSAI  ·  WEEKLY SUMMARY")

    try:
        ws_dt = datetime.strptime(w_start, "%d %b").replace(year=date.today().year)
        fname = f"weekly_{ws_dt.strftime('%d-%b')}.png"
    except ValueError:
        fname = f"weekly_{date.today().strftime('%d-%b')}.png"

    out = CARDS_DIR / fname
    img.save(out, "PNG")
    return out
