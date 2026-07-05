"""
card_generator.py — Branded PNG cards for thepicksai.

Four card types:
  generate_picks_card()    — daily / evening picks, 1080×(dynamic), up to 5 picks (Telegram)
  generate_picks_card_ig() — Instagram-feed variant, 1080×1350 max, up to 3 picks
  generate_results_card()  — daily settled results
  generate_weekly_card()   — Monday weekly summary
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

SIZE    = 1080
CANVAS_H = 1440          # taller canvas keeps large text readable on mobile
PAD     = 64             # outer margin
IW      = SIZE - 2 * PAD  # 952 usable px

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


def _bracket(d: ImageDraw.ImageDraw, canvas_h: int = CANVAS_H) -> None:
    m, arm, t = 38, 55, 3
    ex = SIZE - m
    ey = canvas_h - m
    for x, y, sx, sy in [(m, m, 1, 1), (ex, m, -1, 1), (m, ey, 1, -1), (ex, ey, -1, -1)]:
        d.line([(x, y), (x + sx * arm, y)], fill=_NEON, width=t)
        d.line([(x, y), (x, y + sy * arm)], fill=_NEON, width=t)


def _clip(s: str, f: ImageFont.FreeTypeFont, max_w: int) -> str:
    if _tw(s, f) <= max_w:
        return s
    while s and _tw(s + "…", f) > max_w:
        s = s[:-1]
    return (s + "…") if s else "…"


def _canvas(canvas_h: int = CANVAS_H) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (SIZE, canvas_h), _BG)
    d   = ImageDraw.Draw(img)
    for x in range(0, SIZE + 1, 54):
        d.line([(x, 0), (x, canvas_h)], fill=_GRID, width=1)
    for y in range(0, canvas_h + 1, 54):
        d.line([(0, y), (SIZE, y)], fill=_GRID, width=1)
    return img, d


def _draw_header(d: ImageDraw.ImageDraw, subtitle: str) -> int:
    """Brand + subtitle + opening divider. Returns y cursor after divider."""
    y = PAD + 14
    _spaced_cx(d, y, "THEPICKSAI", _font(52, bold=True), _NEON, gap=8)
    y += _th(_font(52)) + 22
    _cx(d, y, subtitle, _font(36), _DIM)
    y += _th(_font(36)) + 32
    _hr(d, y)
    return y + 32


def _draw_footer(d: ImageDraw.ImageDraw, label: str) -> None:
    f  = _font(30)
    fy = CANVAS_H - PAD - _th(f) - 14
    _hr(d, fy - 22)
    _cx(d, fy, label, f, _N_DIM)


def _pnl_str(v: float) -> str:
    return f"+{v:.2f}u" if v >= 0 else f"{v:.2f}u"


def _conf_color(conf: str) -> tuple:
    cl = conf.strip().lower()
    return _WIN if cl == "high" else _VOID if cl == "medium" else _LOSS


# ── Card 1: Daily picks ───────────────────────────────────────────────────────

def generate_picks_card(
    picks: list[dict],
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

    f_num   = _font(56, bold=True)
    f_match = _font(80, bold=True)
    f_sub   = _font(60)
    f_stat  = _font(64)
    f_conf  = _font(44, bold=True)

    shown  = picks[:5]
    text_w = IW - 90                    # content width right of the pick number
    tag_w  = 240                        # right-hand room for the confidence tag

    def _match_lines(name: str) -> list[str]:
        """Match name on one line when it fits beside the tag, else wrapped at ' vs '."""
        if _tw(name, f_match) <= text_w - tag_w:
            return [name]
        if " vs " in name:
            home, away = name.split(" vs ", 1)
            return [_clip(f"{home} vs", f_match, text_w - tag_w), _clip(away, f_match, text_w)]
        return [_clip(name, f_match, text_w - tag_w)]

    def _bet_lines(p: dict) -> list[str]:
        """Bet type · pick on one line when it fits, else the pick wraps below."""
        bet = f"{p.get('bet_type', '')} · {p.get('pick', '')}"
        if _tw(bet, f_sub) <= text_w:
            return [bet]
        return [_clip(f"{p.get('bet_type', '')} ·", f_sub, text_w),
                _clip(str(p.get("pick", "")), f_sub, text_w)]

    # Size the canvas to the content so there is no dead space at the bottom
    header_h = PAD + 14 + _th(_font(52)) + 22 + _th(_font(36)) + 32 + 32
    def _block_h(p: dict) -> int:
        nm = len(_match_lines(p.get("match", "")))
        nb = len(_bet_lines(p))
        return (nm * _th(f_match) + (nm - 1) * 6 + 10
                + nb * _th(f_sub) + (nb - 1) * 6 + 8 + _th(f_stat) + 30)
    canvas_h = header_h + sum(_block_h(p) for p in shown) + max(len(shown) - 1, 0) * 22 + PAD

    img, d = _canvas(canvas_h)
    _bracket(d, canvas_h)

    subtitle = f"EVENING PICKS  ·  {datstr}" if is_eve else datstr
    y        = _draw_header(d, subtitle)

    for i, p in enumerate(shown, 1):
        conf     = p.get("confidence", "")
        conf_tag = f"[{conf.upper()}]"
        conf_col = _conf_color(conf)

        # Pick number
        d.text((PAD, y + 6), str(i), font=f_num, fill=_NEON)
        x0 = PAD + 90

        # Match name (wrapped at ' vs ' if needed) + confidence tag top-right
        d.text((SIZE - PAD - _tw(conf_tag, f_conf), y + 16),
               conf_tag, font=f_conf, fill=conf_col)
        for line in _match_lines(p.get("match", "")):
            d.text((x0, y), line, font=f_match, fill=_WHITE)
            y += _th(f_match) + 6
        y += 4

        # Bet type · pick selection (wraps when too long)
        bet_lines = _bet_lines(p)
        for j, line in enumerate(bet_lines):
            d.text((x0, y), line, font=f_sub, fill=_DIM)
            y += _th(f_sub) + (6 if j < len(bet_lines) - 1 else 0)
        y += 8

        # Odds — Claude estimate, plus real market odds/value tag when available
        market_odds = p.get("market_odds")
        if market_odds is not None:
            stat = f"Claude {p.get('odds', '')} · Mkt {market_odds}"
            if p.get("value"):
                stat += " [VALUE]"
        else:
            stat = f"Odds {p.get('odds', '')}"
        stat = _clip(stat, f_stat, text_w)
        d.text((x0, y), stat, font=f_stat, fill=_NEON)
        y += _th(f_stat) + 30

        if i < len(shown):
            _sep(d, y, x0=x0)
            y += 22

    suffix = "_evening" if is_eve else ""

    out = CARDS_DIR / f"picks_{today.strftime('%Y-%m-%d')}{suffix}.png"
    img.save(out, "PNG")
    return out


# ── Card 1b: Daily picks, Instagram variant (max 3 picks, 1080×1350 cap) ──────

_IG_MAX_H = 1350


def _ig_pick_priority(p: dict) -> int:
    """Lower sorts first: 🔥 VALUE picks, then HIGH confidence, then MEDIUM, then everything else."""
    if p.get("value"):
        return 0
    conf = (p.get("confidence") or "").strip().lower()
    if conf == "high":
        return 1
    if conf == "medium":
        return 2
    return 3


def generate_picks_card_ig(
    picks: list[dict],
    card_date: date | None = None,
) -> Path:
    """
    Instagram-feed variant of the picks card: 1080 wide, capped at 1350 tall,
    at most 3 picks. Selection order: 🔥 VALUE picks first, then HIGH
    confidence, then MEDIUM (a stable sort — ties keep their original order,
    and lower-priority picks still fill remaining slots if fewer than 3
    VALUE/HIGH/MEDIUM picks exist). Rendering matches generate_picks_card()
    (confidence tag top-right, "[VALUE]" suffix on the odds line) so the two
    variants look like the same brand.

    Font sizes shrink in small steps (never below 70% of the base size) if 3
    picks would overflow 1350px — e.g. long match names or bet descriptions
    that wrap to extra lines. generate_picks_card() (the 5-pick Telegram
    card) is untouched by this function.
    """
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    today  = card_date or date.today()
    datstr = today.strftime("%d %b %Y").upper()

    shown  = sorted(picks, key=_ig_pick_priority)[:3]
    text_w = IW - 90
    tag_w  = 240

    base_sizes = {"num": 56, "match": 80, "sub": 60, "stat": 64, "conf": 44}
    header_h   = PAD + 14 + _th(_font(52)) + 22 + _th(_font(36)) + 32 + 32

    def _build(scale: float):
        sz = {k: max(round(v * scale), 18) for k, v in base_sizes.items()}
        f_num   = _font(sz["num"], bold=True)
        f_match = _font(sz["match"], bold=True)
        f_sub   = _font(sz["sub"])
        f_stat  = _font(sz["stat"])
        f_conf  = _font(sz["conf"], bold=True)

        def _match_lines(name: str) -> list[str]:
            if _tw(name, f_match) <= text_w - tag_w:
                return [name]
            if " vs " in name:
                home, away = name.split(" vs ", 1)
                return [_clip(f"{home} vs", f_match, text_w - tag_w), _clip(away, f_match, text_w)]
            return [_clip(name, f_match, text_w - tag_w)]

        def _bet_lines(p: dict) -> list[str]:
            bet = f"{p.get('bet_type', '')} · {p.get('pick', '')}"
            if _tw(bet, f_sub) <= text_w:
                return [bet]
            return [_clip(f"{p.get('bet_type', '')} ·", f_sub, text_w),
                    _clip(str(p.get("pick", "")), f_sub, text_w)]

        def _block_h(p: dict) -> int:
            nm = len(_match_lines(p.get("match", "")))
            nb = len(_bet_lines(p))
            return (nm * _th(f_match) + (nm - 1) * 6 + 10
                    + nb * _th(f_sub) + (nb - 1) * 6 + 8 + _th(f_stat) + 30)

        canvas_h = header_h + sum(_block_h(p) for p in shown) + max(len(shown) - 1, 0) * 22 + PAD
        return (f_num, f_match, f_sub, f_stat, f_conf, _match_lines, _bet_lines, canvas_h)

    scale = 1.0
    built = _build(scale)
    while built[-1] > _IG_MAX_H and scale > 0.7:
        scale = round(scale - 0.05, 2)
        built = _build(scale)
    f_num, f_match, f_sub, f_stat, f_conf, _match_lines, _bet_lines, canvas_h = built
    canvas_h = min(canvas_h, _IG_MAX_H)

    img, d = _canvas(canvas_h)
    _bracket(d, canvas_h)

    y = _draw_header(d, datstr)

    for i, p in enumerate(shown, 1):
        conf     = p.get("confidence", "")
        conf_tag = f"[{conf.upper()}]"
        conf_col = _conf_color(conf)

        d.text((PAD, y + 6), str(i), font=f_num, fill=_NEON)
        x0 = PAD + 90

        d.text((SIZE - PAD - _tw(conf_tag, f_conf), y + 16),
               conf_tag, font=f_conf, fill=conf_col)
        for line in _match_lines(p.get("match", "")):
            d.text((x0, y), line, font=f_match, fill=_WHITE)
            y += _th(f_match) + 6
        y += 4

        bet_lines = _bet_lines(p)
        for j, line in enumerate(bet_lines):
            d.text((x0, y), line, font=f_sub, fill=_DIM)
            y += _th(f_sub) + (6 if j < len(bet_lines) - 1 else 0)
        y += 8

        # IG card always shows the plain odds line — never the Claude/Mkt
        # comparison — regardless of whether market_odds/value are present
        # on the pick dict. That data still drives the Telegram card above.
        stat = f"Odds {p.get('odds', '')}"
        stat = _clip(stat, f_stat, text_w)
        d.text((x0, y), stat, font=f_stat, fill=_NEON)
        y += _th(f_stat) + 30

        if i < len(shown):
            _sep(d, y, x0=x0)
            y += 22

    out = CARDS_DIR / f"picks_ig_{today.strftime('%Y-%m-%d')}.png"
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

    f_badge = _font(28, bold=True)
    f_match = _font(44, bold=True)
    f_sub   = _font(36)
    f_pnl   = _font(42, bold=True)

    total_pnl = 0.0
    settled   = [r for r in results if r.get("result") in ("WIN", "LOSS", "VOID")][:6]

    for i, r in enumerate(settled):
        res = r.get("result", "")
        pv  = r.get("pnl") or 0.0
        total_pnl += pv

        # Coloured result badge
        rc    = _WIN if res == "WIN" else _LOSS if res == "LOSS" else _VOID
        btxt  = res
        bw    = _tw(btxt, f_badge) + 20
        bh    = _th(f_badge) + 14
        try:
            d.rounded_rectangle([PAD, y + 2, PAD + bw, y + bh + 2], radius=5, fill=rc)
        except AttributeError:
            d.rectangle([PAD, y + 2, PAD + bw, y + bh + 2], fill=rc)
        d.text((PAD + 10, y + 8), btxt, font=f_badge, fill=_BG)

        x0 = PAD + bw + 20

        # Match name
        mname = _clip(r.get("match", ""), f_match, SIZE - PAD - x0 - 160)
        d.text((x0, y), mname, font=f_match, fill=_WHITE)

        # P&L right-aligned on same row
        pv_str = _pnl_str(pv)
        pv_col = _WIN if pv > 0 else _LOSS if pv < 0 else _DIM
        d.text((SIZE - PAD - _tw(pv_str, f_pnl), y + 2), pv_str, font=f_pnl, fill=pv_col)
        y += _th(f_match) + 10

        # Bet type below
        bet = _clip(f"{r.get('bet_type', '')}  ·  {r.get('pick', '')}", f_sub, IW - bw - 20)
        d.text((x0, y), bet, font=f_sub, fill=_DIM)
        y += _th(f_sub) + 28

        if i < len(settled) - 1:
            _sep(d, y)
            y += 20

    # Total P&L
    f_tot = _font(52, bold=True)
    _hr(d, y + 16)
    y += 56
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

    f_lbl = _font(30)
    f_big = _font(80, bold=True)
    f_med = _font(56, bold=True)
    f_sub = _font(34)

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

        f_bp   = _font(46, bold=True)
        bname  = _clip(best.get("match", ""), f_bp, IW - 200)
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
    f_run  = _font(52, bold=True)
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