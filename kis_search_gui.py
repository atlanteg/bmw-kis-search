#!/usr/bin/env python3
"""
kis_search_gui.py  –  BMW KIS Database Search GUI  (standalone, no dependencies)
Written by NBTBoost (c) Atlanteg

Requires Python 3.8+ with Tkinter (bundled in standard Python for Windows).

TIP for Windows: rename / copy this file as  kis_search_gui.pyw  so that
double-clicking it launches pythonw.exe (no black console window at all).

Usage:
    python kis_search_gui.py
    python kis_search_gui.py  C:\\path\\to\\databases
"""

# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — absolute minimum imports so the window can open immediately
# ══════════════════════════════════════════════════════════════════════════════
import sys
import tkinter as tk
from tkinter import ttk
import threading
import queue
from pathlib import Path
import mmap
import os
import re
import struct
import time as _time_mod

# ── Hide the black Windows console window right away ─────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if _hwnd:
            ctypes.windll.user32.ShowWindow(_hwnd, 0)   # SW_HIDE
    except Exception:
        pass

# ── DPI awareness (must be before Tk() on Windows) ───────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

# ── Constants ─────────────────────────────────────────────────────────────────
_VERSION_MAJOR = "01"
_VERSION_BUILD = "0013"   # auto-incremented by pre-commit hook
APP_TITLE   = (f"BMW KIS Search  ·  v{_VERSION_MAJOR}.{_VERSION_BUILD}"
               f"  ·  by NBTboost creators © Atlanteg")
WIN_W, WIN_H = 1150, 720
FONT_UI     = ("Segoe UI", 9)
FONT_MONO   = ("Consolas", 9)
FONT_BOLD   = ("Segoe UI", 9, "bold")
FONT_HUGE   = ("Segoe UI", 18, "bold")
TYPES       = ["All", "SWFK", "CAFD", "BTLD", "HWEL", "FLSL", "SWFL", "ENTD", "HWAP",
               "GWTB", "IBAD", "NAVD"]
SORT_OPTS   = ["sgbm_nr", "type", "version", "desc"]
COL_IDS     = ("sgbm_nr", "type", "version", "full_id", "desc")
COL_HEADS   = ("SGBM_NR",  "Type", "Version", "Full ID",  "Description")
COL_WIDTHS  = (92,          62,     88,         255,        390)
COL_ANCHOR  = ("w",         "c",    "w",        "w",        "w")

# Each Treeview insert/delete call on Windows takes 2–10 ms.
# Limit visible rows; chunk inserts/deletes to never block > ~100 ms at once.
MAX_DISPLAY   = 100

# Watchdog: if the Tkinter event loop stops ticking for this many seconds
# (UI is frozen / GIL permanently held), force-exit the process.
_WATCHDOG_TIMEOUT = 20   # seconds
_INS_CHUNK    = 20    # rows per insert slice
_DEL_CHUNK    = 25    # rows per delete slice

# Default DB root on Windows.  Falls back to script dir or Browse.
_DEFAULT_DB_WIN = Path(r"C:\data\psdzdata\kiswb")

# Chunked pickle cache — each chunk holds this many entries.
# Loading one chunk holds the GIL for ~5-20 ms; time.sleep(0) between
# chunks releases the GIL so Tkinter's message pump stays alive.
_CACHE_CHUNK   = 2000
_CACHE_VERSION = 6   # bump when type mapping changes to force cache rebuild
_CACHE_SUBDIR  = ".kis_cache"

C_BG      = "#1e1e2e"; C_PANEL   = "#2a2a3e"; C_INPUT   = "#313145"
C_BORDER  = "#44445a"; C_FG      = "#cdd6f4"; C_DIM     = "#7f849c"
C_ACCENT  = "#89b4fa"; C_GREEN   = "#a6e3a1"; C_YELLOW  = "#f9e2af"
C_RED     = "#f38ba8"; C_CYAN    = "#89dceb"; C_SEL     = "#313160"
C_ROW_ALT = "#252538"; C_OVERLAY = "#1a1a28"

TYPE_COLORS = {
    "SWFK": C_GREEN,  "CAFD": C_YELLOW,
    "BTLD": C_CYAN,   "HWEL": C_ACCENT, "FLSL": "#cba6f7",
    "GWTB": C_RED,    "IBAD": "#fab387", "NAVD": "#89dceb",
    "SWFL": "#a6e3a1",
}
_SPINNER = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

_THIS_DIR = Path(__file__).parent


# ══════════════════════════════════════════════════════════════════════════════
# KIS SEARCH ENGINE  (embedded — no external kis_search.py needed)
# ══════════════════════════════════════════════════════════════════════════════

_SGBM_TYPES = {
    # Source: Scapy automotive BMW definitions (process_classes)
    0x01: "HWEL", 0x02: "HWAP", 0x03: "HWFR",
    0x04: "GWTB", 0x05: "CAFD", 0x06: "BTLD",
    0x07: "FLSL", 0x08: "SWFL", 0x09: "SWFF",
    0x0A: "SWPF", 0x0B: "ONPS", 0x0C: "IBAD",
    0x0D: "SWFK", 0x0F: "FAFP", 0x10: "FCFA",
    0x1A: "TLRT", 0x1B: "TPRG", 0x1C: "BLUP", 0x1D: "FLUP",
    0xA0: "ENTD", 0xA1: "NAVD", 0xA2: "FCFN",
    0xC0: "SWUP", 0xC1: "SWIP",
}

_ENTRY_RE = re.compile(
    rb'([0-9A-F]{8})'
    rb'\x01\x00\x00\x00([\x00-\xff])'
    rb'\x01\x00\x00\x00([\x00-\xff])'
    rb'\x01\x00\x00\x00([\x00-\xff])'
)

def _type_name(code):
    if code is None:
        return "????"
    return _SGBM_TYPES.get(code, f"T{code:02X}")

def _skip_char1(data, pos):
    if pos >= len(data):
        return pos
    b = data[pos]
    if b == 0x00:
        return pos + 1
    if b == 0x01 and pos + 5 < len(data) and data[pos + 1:pos + 5] == b'\x00\x00\x00\x01':
        return pos + 6
    return pos

def _read_varchar(data, pos):
    if pos >= len(data):
        return "", pos
    if data[pos] == 0x00:
        return "", pos + 1
    if data[pos] != 0x01 or pos + 5 > len(data):
        return "", pos
    length = struct.unpack_from(">I", data, pos + 1)[0]
    if length == 0 or length > 2048:
        return "", pos + 5
    end = pos + 5 + length
    if end > len(data):
        return "", pos + 5
    try:
        text = data[pos + 5:end].decode("utf-8", errors="replace")
    except Exception:
        text = data[pos + 5:end].decode("latin-1", errors="replace")
    return text.strip(), end

def _extract_type(data, match_start, sgbm_nr_hex):
    try:
        nr_bytes = bytes.fromhex(sgbm_nr_hex)
        window = data[max(0, match_start - 64): match_start]
        bi = window.find(nr_bytes)
        if bi >= 1:
            return window[bi - 1]
    except Exception:
        pass
    return None

def extract_entries(data_path, progress=False, progress_cb=None):
    path  = str(data_path)
    size  = os.path.getsize(path)
    entries    = []
    CHUNK      = 512 * 1024
    OVERLAP    = 30
    pos        = 0
    _last_cb_t = 0.0
    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            while pos < size:
                seg_start = max(0, pos - OVERLAP)
                seg_end   = min(pos + CHUNK, size)
                chunk = bytes(mm[seg_start:seg_end])
                for m in _ENTRY_RE.finditer(chunk):
                    abs_start = seg_start + m.start()
                    if abs_start < pos:
                        continue
                    sgbm_nr = m.group(1).decode("ascii")
                    major   = m.group(2)[0]
                    minor   = m.group(3)[0]
                    patch   = m.group(4)[0]
                    if major > 999 or minor > 999 or patch > 9999:
                        continue
                    p = seg_start + m.end()
                    for _ in range(5):
                        np = _skip_char1(mm, p)
                        if np == p:
                            break
                        p = np
                    desc, _ = _read_varchar(mm, p)
                    if desc and not all(0x20 <= ord(c) < 0x100 for c in desc):
                        desc = ""
                    tcode = _extract_type(mm, abs_start, sgbm_nr)
                    entries.append({
                        "sgbm_nr": sgbm_nr,
                        "major":   major,
                        "minor":   minor,
                        "patch":   patch,
                        "version": f"{major}.{minor}.{patch}",
                        "full_id": f"{_type_name(tcode)}_{sgbm_nr}_{major:03d}_{minor:03d}_{patch:03d}",
                        "desc":    desc,
                        "type":    _type_name(tcode),
                    })
                pos = seg_end
                _time_mod.sleep(0)
                now = _time_mod.time()
                if progress_cb and now - _last_cb_t >= 0.15:
                    progress_cb(pos / size)
                    _last_cb_t = now
        finally:
            mm.close()
    return entries

def _haystack(e):
    return " ".join([e["full_id"], e["sgbm_nr"], e["desc"], e["type"], e["version"]]).upper()

def _and_match(entry, include, exclude):
    h = _haystack(entry)
    return (all(t.upper() in h for t in include) and
            not any(t.upper() in h for t in exclude))

def search(entries, groups, exclude=None, type_filter=None):
    if type_filter:
        entries = [e for e in entries if e["type"] == type_filter.upper()]
    exclude = [e.lstrip("!") for e in (exclude or [])]
    if not groups or not any(groups):
        if exclude:
            return [e for e in entries if _and_match(e, [], exclude)]
        return list(entries)
    results = []
    for e in entries:
        if any(_and_match(e, grp, exclude) for grp in groups if grp):
            results.append(e)
    return results

def _parse_terms(term_list):
    groups = []; cur = []; exclude = []
    for tok in term_list:
        parts = tok.split("|")
        for i, part in enumerate(parts):
            if i > 0:
                if cur: groups.append(cur)
                cur = []
            part = part.strip()
            if not part: continue
            if part.startswith("!"):
                exclude.append(part[1:])
            elif part == "|":
                if cur: groups.append(cur)
                cur = []
            else:
                cur.append(part)
    if cur: groups.append(cur)
    return (groups if groups else [[]]), exclude


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2 — load pickle in background so window stays snappy
# ══════════════════════════════════════════════════════════════════════════════
_pickle = None

def _do_heavy_imports(result_q: queue.Queue):
    try:
        import pickle as _pk
        result_q.put(("ok", _pk))
    except Exception as e:
        result_q.put(("err", str(e)))


# ── Chunked pickle cache ──────────────────────────────────────────────────────
# Saves entries as many small .pkl files so that loading yields the GIL
# between chunks (via time.sleep(0)), keeping Tkinter's event loop alive.

def _cache_dir(db_path: Path) -> Path:
    return db_path.parent / _CACHE_SUBDIR

def _cache_meta(db_path: Path) -> Path:
    return _cache_dir(db_path) / "meta.pkl"


def _wipe_cache(db_path: Path):
    """Delete all cache files for this database (forces fresh scan)."""
    import shutil as _shutil
    try:
        _shutil.rmtree(_cache_dir(db_path), ignore_errors=True)
    except Exception:
        pass
    for old in (db_path.parent / ".kis_gui_cache.pkl",):
        try:
            old.unlink(missing_ok=True)
        except Exception:
            pass


def _load_fast_cache(db_path: Path):
    """Load chunked cache; yields GIL between chunks. Returns list or None."""
    meta_path = _cache_meta(db_path)
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "rb") as f:
            meta = _pickle.load(f)
        if meta.get("v") != _CACHE_VERSION:
            _wipe_cache(db_path)   # wrong version — delete so rescan is clean
            return None
        if meta.get("mtime", 0) < db_path.stat().st_mtime:
            _wipe_cache(db_path)
            return None
        cdir    = meta_path.parent
        entries = []
        for i in range(meta["chunks"]):
            with open(cdir / f"c{i:05d}.pkl", "rb") as f:
                entries.extend(_pickle.load(f))
            _time_mod.sleep(0)   # ← release GIL between every chunk
        if len(entries) == meta["count"]:
            return entries
    except Exception:
        _wipe_cache(db_path)
    return None


def _save_fast_cache(db_path: Path, entries: list):
    try:
        cdir = _cache_dir(db_path)
        cdir.mkdir(exist_ok=True)
        n_chunks = 0
        for i in range(0, len(entries), _CACHE_CHUNK):
            with open(cdir / f"c{n_chunks:05d}.pkl", "wb") as f:
                _pickle.dump(entries[i : i + _CACHE_CHUNK], f,
                             protocol=_pickle.HIGHEST_PROTOCOL)
            n_chunks += 1
        meta = {
            "v":      _CACHE_VERSION,
            "mtime":  db_path.stat().st_mtime,
            "count":  len(entries),
            "chunks": n_chunks,
        }
        with open(_cache_meta(db_path), "wb") as f:
            _pickle.dump(meta, f, protocol=_pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        print(f"Warning: cache save failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Application
# ══════════════════════════════════════════════════════════════════════════════

class KisSearchApp:
    def __init__(self, root: tk.Tk, base_path: Path):
        self.root      = root
        self.base_path = base_path

        self.platforms : list[Path]            = []
        self._db       : dict[str, list | None] = {}
        self._loading  : set[str]              = set()
        self._queues   : dict[str, queue.Queue] = {}

        self.entries    : list = []
        self._sort_col   = "sgbm_nr"
        self._sort_rev   = False
        self._debounce   = None
        self._spin_idx   = 0
        self._spin_after = None
        self._scan_mode  = False
        self._scan_start : float = 0.0

        # async delete → insert pipeline
        self._pipeline_cancel = False
        self._delete_job  = None
        self._insert_job  = None
        self._step_after  = None   # single after() id shared by delete/insert

        # async search queue (latest result wins)
        self._queued_results : tuple | None = None
        self._flush_after    : str | None   = None

        # sequential platform load queue (one at a time → no GIL stampede)
        self._load_queue : list[Path] = []

        # watchdog heartbeat: main thread sets this event every second
        self._wd_event = threading.Event()
        self._wd_dot   = True   # for toggling the status-bar indicator

        self._setup_style()
        self._build_ui()
        self._start_watchdog()
        self._find_and_preload()

    # ── ttk style ─────────────────────────────────────────────────────────────

    def _setup_style(self):
        self.root.title(APP_TITLE)
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.minsize(820, 540)
        self.root.configure(bg=C_BG)

        s = ttk.Style()
        for theme in ("clam", "alt", "default"):
            if theme in s.theme_names():
                s.theme_use(theme); break

        s.configure(".",          background=C_BG, foreground=C_FG, font=FONT_UI,
                    borderwidth=0, relief="flat")
        s.configure("TFrame",     background=C_BG)
        s.configure("P.TFrame",   background=C_PANEL)
        s.configure("TLabel",     background=C_BG,    foreground=C_FG,  font=FONT_UI)
        s.configure("Dim.TLabel", background=C_BG,    foreground=C_DIM, font=FONT_UI)
        s.configure("P.TLabel",   background=C_PANEL, foreground=C_FG,  font=FONT_UI)
        s.configure("PD.TLabel",  background=C_PANEL, foreground=C_DIM, font=FONT_UI)

        s.configure("TButton", background=C_ACCENT, foreground=C_BG,
                    font=FONT_BOLD, padding=(12, 5), relief="flat")
        s.map("TButton", background=[("active","#74a0ea"),("pressed","#5a8cd0"),
                                     ("disabled", C_BORDER)])
        s.configure("Sm.TButton", background=C_PANEL, foreground=C_DIM,
                    font=FONT_UI, padding=(8, 4), relief="flat")
        s.map("Sm.TButton", background=[("active", C_BORDER)])

        s.configure("TCombobox", fieldbackground=C_INPUT, background=C_INPUT,
                    foreground=C_FG, selectbackground=C_SEL,
                    arrowcolor=C_DIM, font=FONT_UI)
        s.map("TCombobox",
              fieldbackground=[("readonly", C_INPUT)],
              selectbackground=[("readonly", C_INPUT)],
              foreground      =[("readonly", C_FG)])

        s.configure("TEntry", fieldbackground=C_INPUT, foreground=C_FG,
                    insertcolor=C_FG, selectbackground=C_SEL,
                    font=FONT_UI, padding=(6, 4))

        s.configure("Treeview", background=C_PANEL, foreground=C_FG,
                    fieldbackground=C_PANEL, rowheight=22,
                    font=FONT_MONO, borderwidth=0, relief="flat")
        s.configure("Treeview.Heading", background=C_BORDER, foreground=C_FG,
                    font=FONT_BOLD, relief="flat", padding=(4, 5))
        s.map("Treeview",
              background=[("selected", C_SEL)],
              foreground=[("selected", C_FG)])
        s.map("Treeview.Heading",
              background=[("active", C_ACCENT), ("pressed", C_ACCENT)])

        s.configure("Det.Horizontal.TProgressbar",
                    troughcolor=C_PANEL, background=C_ACCENT,
                    borderwidth=0, thickness=8)
        s.configure("Ind.Horizontal.TProgressbar",
                    troughcolor=C_PANEL, background=C_ACCENT,
                    borderwidth=0, thickness=8)
        s.configure("TScrollbar", background=C_PANEL, troughcolor=C_BG,
                    arrowcolor=C_DIM, relief="flat", borderwidth=0)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # ── Top bar (platform + DB path) ──────────────────────────────────────
        top = ttk.Frame(self.root, style="P.TFrame", padding=(12, 8))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(3, weight=1)

        # Row 0: platform selector
        ttk.Label(top, text="Платформа:", style="P.TLabel").grid(
            row=0, column=0, padx=(0, 6))
        self.var_platform = tk.StringVar()
        self.cb_platform  = ttk.Combobox(
            top, textvariable=self.var_platform,
            state="readonly", width=22, font=FONT_UI)
        self.cb_platform.grid(row=0, column=1, padx=(0, 8))
        self.cb_platform.bind("<<ComboboxSelected>>", self._on_platform_change)

        self.btn_reload = ttk.Button(top, text="↺ Перезагрузить",
                                     style="Sm.TButton", command=self._reload_db)
        self.btn_reload.grid(row=0, column=2, padx=(0, 10))

        self.lbl_plat_info = ttk.Label(top, text="", style="PD.TLabel",
                                       background=C_PANEL)
        self.lbl_plat_info.grid(row=0, column=3, sticky="w")

        self.lbl_loading_all = ttk.Label(top, text="", style="P.TLabel",
                                         background=C_PANEL, foreground=C_ACCENT)
        self.lbl_loading_all.grid(row=0, column=4, sticky="e", padx=(10, 0))

        # Row 1: database path + Browse button
        ttk.Label(top, text="База данных:", style="PD.TLabel").grid(
            row=1, column=0, padx=(0, 6), pady=(6, 0))
        self.lbl_db_path = ttk.Label(top, text=str(self.base_path),
                                     style="PD.TLabel", background=C_PANEL)
        self.lbl_db_path.grid(row=1, column=1, columnspan=3, sticky="w",
                              pady=(6, 0))
        ttk.Button(top, text="📁 Browse…", style="Sm.TButton",
                   command=self._browse_db).grid(row=1, column=4, pady=(6, 0),
                                                 sticky="e", padx=(10, 0))

        # ── Search controls ───────────────────────────────────────────────────
        sf = ttk.Frame(self.root, padding=(12, 8, 12, 4))
        sf.grid(row=1, column=0, sticky="ew")
        sf.columnconfigure(1, weight=1)

        ttk.Label(sf, text="Поиск:").grid(
            row=0, column=0, sticky="e", padx=(0, 6))
        self.var_include = tk.StringVar()
        ent_inc = ttk.Entry(sf, textvariable=self.var_include, font=FONT_UI)
        ent_inc.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ent_inc.bind("<KeyRelease>", self._on_key)
        ent_inc.bind("<Return>", lambda e: self._do_search())
        ent_inc.focus_set()

        ttk.Label(sf, text="Тип:").grid(row=0, column=2, sticky="e", padx=(0, 6))
        self.var_type = tk.StringVar(value="All")
        cb_type = ttk.Combobox(sf, textvariable=self.var_type,
                               values=TYPES, state="readonly", width=8)
        cb_type.grid(row=0, column=3, sticky="w", padx=(0, 12))
        cb_type.bind("<<ComboboxSelected>>", lambda e: self._do_search())

        self.btn_search = ttk.Button(sf, text="Найти", command=self._do_search)
        self.btn_search.grid(row=0, column=5, rowspan=2, sticky="ns",
                             padx=(12, 0), ipadx=8)

        ttk.Label(sf, text="Исключить:").grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=(5, 0))
        self.var_exclude = tk.StringVar()
        ent_exc = ttk.Entry(sf, textvariable=self.var_exclude, font=FONT_UI)
        ent_exc.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(5, 0))
        ent_exc.bind("<KeyRelease>", self._on_key)
        ent_exc.bind("<Return>", lambda e: self._do_search())

        ttk.Label(sf, text="Сорт.:").grid(
            row=1, column=2, sticky="e", padx=(0, 6), pady=(5, 0))
        self.var_sort = tk.StringVar(value="sgbm_nr")
        cb_sort = ttk.Combobox(sf, textvariable=self.var_sort,
                               values=SORT_OPTS, state="readonly", width=10)
        cb_sort.grid(row=1, column=3, sticky="w", padx=(0, 12), pady=(5, 0))
        cb_sort.bind("<<ComboboxSelected>>", lambda e: self._do_search())

        ttk.Button(sf, text="✕ Очистить", style="Sm.TButton",
                   command=self._clear).grid(row=1, column=4, pady=(5, 0))

        # ── Results area + loading overlay ───────────────────────────────────
        self.rf = ttk.Frame(self.root)
        self.rf.grid(row=2, column=0, sticky="nsew", padx=10, pady=(4, 0))
        self.rf.columnconfigure(0, weight=1)
        self.rf.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(self.rf, columns=COL_IDS, show="headings",
                                 selectmode="extended")
        for cid, head, w, anc in zip(COL_IDS, COL_HEADS, COL_WIDTHS, COL_ANCHOR):
            self.tree.heading(cid, text=head,
                              command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=w, minwidth=40, anchor=anc,
                             stretch=(cid == "desc"))
        vsb = ttk.Scrollbar(self.rf, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(self.rf, orient="horizontal",  command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree.tag_configure("alt", background=C_ROW_ALT)
        for tname, col in TYPE_COLORS.items():
            self.tree.tag_configure(f"t_{tname}",     foreground=col)
            self.tree.tag_configure(f"t_{tname}_alt", foreground=col,
                                    background=C_ROW_ALT)

        self.tree.bind("<Double-1>",  self._on_double_click)
        self.tree.bind("<Control-c>", self._copy_full_id)
        self.tree.bind("<Button-3>",  self._show_ctx_menu)

        # ── Loading overlay ───────────────────────────────────────────────────
        self.overlay = tk.Frame(self.rf, bg=C_OVERLAY)

        self.lbl_spin = tk.Label(self.overlay, text="", bg=C_OVERLAY,
                                 fg=C_ACCENT, font=("Segoe UI", 26, "bold"))
        self.lbl_spin.place(relx=0.5, rely=0.30, anchor="center")

        self.lbl_load_title = tk.Label(self.overlay, text="",
                                       bg=C_OVERLAY, fg=C_FG,
                                       font=("Segoe UI", 15, "bold"))
        self.lbl_load_title.place(relx=0.5, rely=0.42, anchor="center")

        self.lbl_load_sub = tk.Label(self.overlay, text="",
                                     bg=C_OVERLAY, fg=C_DIM,
                                     font=("Segoe UI", 10))
        self.lbl_load_sub.place(relx=0.5, rely=0.51, anchor="center")

        self.pbar_det = ttk.Progressbar(
            self.overlay, style="Det.Horizontal.TProgressbar",
            mode="determinate", length=360, maximum=100)
        self.pbar_det.place(relx=0.5, rely=0.61, anchor="center")

        self.pbar_ind = ttk.Progressbar(
            self.overlay, style="Ind.Horizontal.TProgressbar",
            mode="indeterminate", length=360)
        self.pbar_ind.place(relx=0.5, rely=0.61, anchor="center")

        self.lbl_load_pct = tk.Label(self.overlay, text="",
                                     bg=C_OVERLAY, fg=C_ACCENT,
                                     font=("Segoe UI", 9))
        self.lbl_load_pct.place(relx=0.5, rely=0.69, anchor="center")

        # ── No-database panel (shown when no KIS.data found) ─────────────────
        self.no_db_frame = tk.Frame(self.rf, bg=C_OVERLAY)
        tk.Label(self.no_db_frame,
                 text="KIS.data не найден",
                 bg=C_OVERLAY, fg=C_FG,
                 font=("Segoe UI", 16, "bold")).pack(pady=(80, 8))
        self._lbl_no_db_path = tk.Label(self.no_db_frame, text="",
                                        bg=C_OVERLAY, fg=C_DIM,
                                        font=("Segoe UI", 10))
        self._lbl_no_db_path.pack(pady=(0, 20))
        ttk.Button(self.no_db_frame, text="📁  Выбрать папку с базами данных",
                   command=self._browse_db).pack()

        # ── Status bar ───────────────────────────────────────────────────────
        sb = ttk.Frame(self.root, padding=(12, 4))
        sb.grid(row=3, column=0, sticky="ew")
        sb.columnconfigure(0, weight=1)
        self.var_status = tk.StringVar(value="Инициализация…")
        ttk.Label(sb, textvariable=self.var_status,
                  style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(sb, text="ДвойнойКлик / Ctrl+C → копировать Full ID",
                  style="Dim.TLabel").grid(row=0, column=1, sticky="e")
        # Watchdog heartbeat indicator — pulses green every 1 s when alive;
        # stops if frozen; process auto-exits after _WATCHDOG_TIMEOUT seconds.
        self.var_wd = tk.StringVar(value="●")
        self._lbl_wd = tk.Label(sb, textvariable=self.var_wd,
                                bg=C_BG, fg=C_GREEN,
                                font=("Consolas", 9), width=1, anchor="center")
        self._lbl_wd.grid(row=0, column=2, sticky="e", padx=(8, 0))

        # ── Context menu ─────────────────────────────────────────────────────
        self._ctx = tk.Menu(self.root, tearoff=False,
                            bg=C_PANEL, fg=C_FG,
                            activebackground=C_ACCENT, activeforeground=C_BG,
                            font=FONT_UI)
        self._ctx.add_command(label="Копировать Full ID",   command=self._copy_full_id)
        self._ctx.add_command(label="Копировать SGBM_NR",   command=lambda: self._copy_col(0))
        self._ctx.add_command(label="Копировать описание",  command=lambda: self._copy_col(4))
        self._ctx.add_separator()
        self._ctx.add_command(label="Копировать все строки (TSV)", command=self._copy_all_tsv)

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _show_overlay(self, title: str, sub: str = "", scan_mode: bool = False):
        self.lbl_load_title.config(text=title)
        self.lbl_load_sub.config(text=sub)
        self.lbl_load_pct.config(text="")
        self._scan_mode = scan_mode
        self.no_db_frame.place_forget()

        if scan_mode:
            self.pbar_ind.stop()
            self.pbar_ind.place_forget()
            self.pbar_det["value"] = 0
            self.pbar_det.place(relx=0.5, rely=0.61, anchor="center")
        else:
            self.pbar_det.place_forget()
            self.pbar_ind.place(relx=0.5, rely=0.61, anchor="center")
            self.pbar_ind.start(14)

        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.overlay.lift()
        self._spin_idx = 0
        if not self._spin_after:
            self._animate_spin()

    def _hide_overlay(self):
        if self._spin_after:
            self.root.after_cancel(self._spin_after)
            self._spin_after = None
        self.pbar_ind.stop()
        self.overlay.place_forget()

    def _show_no_db(self, path: Path):
        self._hide_overlay()
        self._lbl_no_db_path.config(text=f"Путь: {path}")
        self.no_db_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.no_db_frame.lift()

    def _hide_no_db(self):
        self.no_db_frame.place_forget()

    def _animate_spin(self):
        frame = _SPINNER[self._spin_idx % len(_SPINNER)]
        self.lbl_spin.config(text=frame)
        self._spin_idx += 1
        self._spin_after = self.root.after(100, self._animate_spin)

    def _update_progress(self, pct: float):
        self.pbar_det["value"] = pct * 100
        self.lbl_load_pct.config(
            text=f"{pct * 100:.0f}%  ·  осталось ~{self._eta_str(pct)}")

    def _eta_str(self, pct: float) -> str:
        if pct <= 0.01 or not self._scan_start:
            return "…"
        elapsed = _time_mod.time() - self._scan_start
        rem = (elapsed / pct) - elapsed
        return f"{rem:.0f} с" if rem < 60 else f"{rem / 60:.1f} мин"

    # ── Database path / Browse ────────────────────────────────────────────────

    def _browse_db(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(
            title="Выбрать папку с базами данных KIS",
            initialdir=str(self.base_path),
        )
        if folder:
            self._load_from_path(Path(folder))

    def _load_from_path(self, new_path: Path):
        # Stop all background activity
        self._pipeline_cancel = True
        if self._step_after:
            self.root.after_cancel(self._step_after)
            self._step_after = None
        self._delete_job = None
        self._insert_job = None
        self._queued_results = None
        if self._flush_after:
            self.root.after_cancel(self._flush_after)
            self._flush_after = None

        # Reset state
        self.base_path = new_path
        self.platforms = []
        self._db       = {}
        self._loading  = set()
        self._queues   = {}
        self._load_queue = []
        self.entries   = []
        self.cb_platform["values"] = []
        self.var_platform.set("")
        self.lbl_plat_info.config(text="")
        self.lbl_db_path.config(text=str(new_path))
        self._cancel_pipeline()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)

        self._find_and_preload()

    # ── Platform detection & preloading ──────────────────────────────────────

    def _find_and_preload(self):
        found = []
        base  = self.base_path
        if base.is_dir():
            for sub in sorted(base.iterdir()):
                if sub.is_dir() and (sub / "KIS.data").exists():
                    found.append(sub)
        if (base / "KIS.data").exists() and base not in found:
            found.insert(0, base)

        self.platforms             = found
        self.cb_platform["values"] = [p.name for p in found]
        self.lbl_db_path.config(text=str(base))

        if not found:
            self._show_no_db(base)
            self._set_status(f"KIS.data не найден в  {base}")
            return

        self._hide_no_db()
        for p in found:
            self._db[p.name]     = None
            self._queues[p.name] = queue.Queue()

        self.cb_platform.current(0)
        first    = found[0]
        from_pkl = _cache_meta(first / "KIS.data").exists() or \
                   (first / ".kis_gui_cache.pkl").exists()
        self._show_overlay(
            f"Загрузка  {first.name}",
            "Загрузка из кэша…" if from_pkl else "Первый запуск — сканирование ~1 мин…",
            scan_mode=not from_pkl,
        )
        if not from_pkl:
            self._scan_start = _time_mod.time()

        self.root.update()   # render overlay before threads start

        self._load_queue = list(found[1:])
        self._start_load_thread(found[0])
        self.root.after(80, self._poll_all)

    def _start_next_queued(self):
        while self._load_queue:
            p = self._load_queue.pop(0)
            if self._db.get(p.name) is None and p.name not in self._loading:
                self._start_load_thread(p)
                return

    def _start_load_thread(self, plat_path: Path, force: bool = False):
        name = plat_path.name
        db   = plat_path / "KIS.data"
        q    = self._queues[name]
        self._loading.add(name)

        def _progress_cb(pct: float):
            q.put(("progress", name, pct))

        def _worker():
            try:
                t0 = _time_mod.time()
                if not force:
                    cached = _load_fast_cache(db)
                    if cached is not None:
                        q.put(("done", name, cached, True, _time_mod.time() - t0))
                        return
                entries = extract_entries(db, progress=False,
                                           progress_cb=_progress_cb)
                _save_fast_cache(db, entries)
                q.put(("done", name, entries, False, _time_mod.time() - t0))
            except Exception as exc:
                q.put(("error", name, str(exc)))

        threading.Thread(target=_worker, daemon=True,
                         name=f"load-{name}").start()

    def _poll_all(self):
        active = self.cb_platform.get()

        for name, q in list(self._queues.items()):
            last_pct     = None
            non_progress = []
            while True:
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    break
                if msg[0] == "progress":
                    last_pct = msg[2]
                else:
                    non_progress.append(msg)

            if last_pct is not None and name == active and self._scan_mode:
                self._update_progress(last_pct)

            for msg in non_progress:
                kind = msg[0]
                if kind == "done":
                    _, pname, entries, from_cache, elapsed = msg
                    self._db[pname] = entries
                    self._loading.discard(pname)
                    self._start_next_queued()
                    if pname == active:
                        self.entries = entries
                        src = "кэш" if from_cache else f"сканирование {elapsed:.0f}с"
                        self.lbl_plat_info.config(
                            text=f"{pname}  ·  {len(entries):,} записей  ({src})")
                        self._hide_overlay()
                        self._do_search()
                        still = len(self._loading) + len(self._load_queue)
                        st = f"Готово — {len(entries):,} записей  [{pname}]"
                        if still:
                            st += f"  ·  загружается ещё {still} платф. в фоне…"
                        self._set_status(st)

                elif kind == "error":
                    _, pname, err = msg
                    self._db[pname] = []
                    self._loading.discard(pname)
                    from tkinter import messagebox
                    messagebox.showerror("Ошибка загрузки",
                                         f"Платформа {pname}:\n{err}")

        loading_now = sorted(self._loading)
        queued      = [p.name for p in self._load_queue]
        if loading_now or queued:
            spin  = _SPINNER[self._spin_idx % len(_SPINNER)]
            parts = []
            if loading_now: parts.append(f"загружается: {', '.join(loading_now)}")
            if queued:      parts.append(f"в очереди: {', '.join(queued)}")
            self.lbl_loading_all.config(text=f"{spin} " + "  ·  ".join(parts))
        else:
            self.lbl_loading_all.config(text="✓ все платформы готовы")

        if self._loading or self._load_queue:
            self.root.after(80, self._poll_all)

    # ── Platform switching ────────────────────────────────────────────────────

    def _on_platform_change(self, _event=None):
        name    = self.var_platform.get()
        entries = self._db.get(name)

        if entries is not None:
            self.entries = entries
            self.lbl_plat_info.config(
                text=f"{name}  ·  {len(entries):,} записей")
            self._do_search()
            self._set_status(f"Платформа: {name}  ·  {len(entries):,} записей")
        else:
            self.entries = []
            self._cancel_pipeline()
            plat_path = next(p for p in self.platforms if p.name == name)
            from_pkl  = _cache_meta(plat_path / "KIS.data").exists() or \
                        (plat_path / ".kis_gui_cache.pkl").exists()
            self._show_overlay(
                f"Загрузка  {name}",
                "Загрузка из кэша…" if from_pkl else "Первый запуск — сканирование…",
                scan_mode=not from_pkl,
            )
            if not from_pkl:
                self._scan_start = _time_mod.time()
            self._set_status(f"Загрузка платформы {name}…")
            if plat_path in self._load_queue:
                self._load_queue.remove(plat_path)
            if name not in self._loading:
                self._start_load_thread(plat_path)
            self._wait_for_platform(name)

    def _wait_for_platform(self, name: str):
        entries = self._db.get(name)
        if entries is not None:
            self.entries = entries
            self._do_search()
            self._hide_overlay()
            self.lbl_plat_info.config(
                text=f"{name}  ·  {len(entries):,} записей")
            self._set_status(f"Платформа: {name}  ·  {len(entries):,} записей")
            return

        q = self._queues.get(name)
        if q:
            try:
                msg = q.get_nowait()
                if msg[0] == "progress":
                    if msg[1] == name and self._scan_mode:
                        self._update_progress(msg[2])
                elif msg[0] == "done":
                    _, pname, loaded, from_cache, elapsed = msg
                    self._db[pname] = loaded
                    self._loading.discard(pname)
                    if pname == name:
                        self.entries = loaded
                        self._do_search()
                        self._hide_overlay()
                        src = "кэш" if from_cache else f"скан {elapsed:.0f}с"
                        self.lbl_plat_info.config(
                            text=f"{name}  ·  {len(loaded):,} записей  ({src})")
                        self._set_status(
                            f"Платформа: {name}  ·  {len(loaded):,} записей")
                        return
                elif msg[0] == "error":
                    _, pname, err = msg
                    self._db[pname] = []
                    self._loading.discard(pname)
                    self._hide_overlay()
                    from tkinter import messagebox
                    messagebox.showerror("Ошибка загрузки",
                                         f"Платформа {pname}:\n{err}")
                    return
            except queue.Empty:
                pass

        self.root.after(100, lambda: self._wait_for_platform(name))

    def _reload_db(self):
        name = self.var_platform.get()
        plat = next((p for p in self.platforms if p.name == name), None)
        if not plat:
            return
        self._db[name]     = None
        self._loading.add(name)
        self._queues[name] = queue.Queue()
        self.entries = []
        self._cancel_pipeline()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._show_overlay(f"Пересканирование  {name}",
                           "Повторное считывание базы данных…",
                           scan_mode=True)
        self._scan_start = _time_mod.time()
        self._set_status(f"Перезагрузка {name}…")
        self._start_load_thread(plat, force=True)
        self._wait_for_platform(name)

    # ── Search ────────────────────────────────────────────────────────────────

    def _on_key(self, _event=None):
        if self._debounce:
            self.root.after_cancel(self._debounce)
        self._debounce = self.root.after(300, self._do_search)

    def _do_search(self):
        """Compute results instantly; schedule the slow tree update asynchronously."""
        if not self.entries:
            return
        inc_raw = self.var_include.get().strip()
        exc_raw = self.var_exclude.get().strip()
        tf      = self.var_type.get()
        sort_by = self.var_sort.get()

        groups, _ = _parse_terms(inc_raw.split() if inc_raw else [])
        exc       = exc_raw.split() if exc_raw else []
        tf_arg    = None if tf == "All" else tf

        results = search(self.entries, groups, exclude=exc, type_filter=tf_arg)

        rev = self._sort_rev if self._sort_col == sort_by else False
        self._sort_col = sort_by
        _keys = {
            "sgbm_nr": lambda e: (e["sgbm_nr"], e["major"], e["minor"], e["patch"]),
            "type":    lambda e: (e["type"], e["sgbm_nr"]),
            "version": lambda e: (e["major"], e["minor"], e["patch"]),
            "desc":    lambda e: e["desc"].lower(),
        }
        results.sort(key=_keys.get(sort_by, _keys["sgbm_nr"]), reverse=rev)
        total = len(results)

        shown = min(total, MAX_DISPLAY)
        parts = []
        if total > MAX_DISPLAY:
            parts.append(f"{total:,} результатов  (показано {shown} — уточните поиск)")
        else:
            parts.append(f"{total:,} результатов")
        if inc_raw: parts.append(f"поиск: {inc_raw}")
        if exc_raw: parts.append(f"исключить: {exc_raw}")
        self._set_status("  ·  ".join(parts))

        # Latest result wins — cancel any pending flush and in-flight pipeline
        self._queued_results = (results, total)
        if self._flush_after:
            self.root.after_cancel(self._flush_after)
        self._pipeline_cancel = True
        if self._step_after:
            self.root.after_cancel(self._step_after)
            self._step_after = None
        self._delete_job = None
        self._insert_job = None
        self._flush_after = self.root.after(5, self._flush_results)

    def _flush_results(self):
        self._flush_after = None
        if self._queued_results is None:
            return
        results, full_total = self._queued_results
        self._queued_results = None
        self._pipeline_cancel = False

        old_children = list(self.tree.get_children())
        if old_children:
            self._delete_job = {
                "items":      old_children,
                "offset":     0,
                "results":    results,
                "full_total": full_total,
            }
            self._step_after = self.root.after(1, self._delete_chunk)
        else:
            self._begin_insert(results, full_total)

    # ── Async delete pipeline ─────────────────────────────────────────────────

    def _delete_chunk(self):
        if self._pipeline_cancel or self._delete_job is None:
            return
        job    = self._delete_job
        items  = job["items"]
        offset = job["offset"]
        end    = min(offset + _DEL_CHUNK, len(items))
        if offset < end:
            self.tree.delete(*items[offset:end])
        job["offset"] = end
        if end < len(items) and not self._pipeline_cancel:
            self._step_after = self.root.after(1, self._delete_chunk)
        else:
            results    = job["results"]
            full_total = job["full_total"]
            self._delete_job  = None
            self._step_after  = None
            if not self._pipeline_cancel:
                self._begin_insert(results, full_total)

    # ── Async insert pipeline ─────────────────────────────────────────────────

    def _begin_insert(self, results: list, full_total: int):
        if not results:
            return
        visible = results[:MAX_DISPLAY]
        self._insert_job = {
            "results":    visible,
            "offset":     0,
            "prev_nr":    None,
            "alt":        False,
            "total":      len(visible),
            "full_total": full_total,
        }
        self._step_after = self.root.after(1, self._insert_chunk)

    def _insert_chunk(self):
        if self._pipeline_cancel or self._insert_job is None:
            return
        job     = self._insert_job
        results = job["results"]
        offset  = job["offset"]
        end     = min(offset + _INS_CHUNK, job["total"])
        prev_nr = job["prev_nr"]
        alt     = job["alt"]

        for i in range(offset, end):
            e = results[i]
            if e["sgbm_nr"] != prev_nr:
                if prev_nr is not None:
                    alt = not alt
                prev_nr = e["sgbm_nr"]
            tag = f"t_{e['type']}" + ("_alt" if alt else "")
            self.tree.insert("", "end",
                             values=(e["sgbm_nr"], e["type"], e["version"],
                                     e["full_id"], e["desc"]),
                             tags=(tag,))

        job["offset"]  = end
        job["prev_nr"] = prev_nr
        job["alt"]     = alt

        if end < job["total"] and not self._pipeline_cancel:
            self._step_after = self.root.after(1, self._insert_chunk)
        else:
            self._step_after = None
            self._insert_job = None

    def _cancel_pipeline(self):
        self._pipeline_cancel = True
        if self._step_after:
            self.root.after_cancel(self._step_after)
            self._step_after = None
        self._delete_job = None
        self._insert_job = None

    def _clear(self):
        self.var_include.set("")
        self.var_exclude.set("")
        self.var_type.set("All")
        self._do_search()

    # ── Sorting ───────────────────────────────────────────────────────────────

    def _sort_by(self, col):
        sk = {"full_id": "sgbm_nr"}.get(col, col)
        if col in SORT_OPTS:
            sk = col
        self._sort_rev = (not self._sort_rev) if self._sort_col == sk else False
        self.var_sort.set(sk)
        self._do_search()
        heads = dict(zip(COL_IDS, COL_HEADS))
        for c in COL_IDS:
            arrow = (" ▲" if not self._sort_rev else " ▼") if c == col else ""
            self.tree.heading(c, text=heads[c] + arrow)

    # ── Clipboard ─────────────────────────────────────────────────────────────

    def _sel_values(self):
        return [self.tree.item(i)["values"] for i in self.tree.selection()]

    def _copy_col(self, idx, _event=None):
        rows = self._sel_values()
        if not rows: return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(str(r[idx]) for r in rows))
        self._set_status(f"Скопировано {len(rows)} значений.")

    def _copy_full_id(self, _event=None): self._copy_col(3)
    def _on_double_click(self, _event=None): self._copy_full_id()

    def _copy_all_tsv(self):
        rows = self._sel_values() or \
               [self.tree.item(i)["values"] for i in self.tree.get_children()]
        hdr  = "\t".join(COL_HEADS)
        body = "\n".join("\t".join(str(v) for v in r) for r in rows)
        self.root.clipboard_clear()
        self.root.clipboard_append(hdr + "\n" + body)
        self._set_status(f"Скопировано {len(rows)} строк (TSV).")

    def _show_ctx_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self._ctx.post(e.x_root, e.y_root)

    def _set_status(self, text: str):
        self.var_status.set(text)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def _start_watchdog(self):
        threading.Thread(target=self._wd_run, daemon=True,
                         name="watchdog").start()
        self._wd_tick()

    def _wd_tick(self):
        """Called by the event loop every 1 s — proof that the UI is alive."""
        self._wd_event.set()
        # Toggle the dot: ● ↔ ○
        self._wd_dot = not self._wd_dot
        self.var_wd.set("●" if self._wd_dot else " ")
        self.root.after(1000, self._wd_tick)

    def _wd_run(self):
        """Watchdog thread: force-exit if the UI stops ticking.

        threading.Event.wait() releases the GIL (OS wait primitive), so this
        thread runs even when other threads briefly hold the GIL.
        If the event is never set within _WATCHDOG_TIMEOUT seconds the process
        is killed via os._exit() — no cleanup, guaranteed termination.
        """
        import time as _t
        import os   as _os
        while True:
            self._wd_event.clear()
            alive = self._wd_event.wait(_WATCHDOG_TIMEOUT)
            if not alive:
                # UI loop has been silent for TIMEOUT seconds.
                # Try a graceful exit first; if the interpreter is truly locked,
                # fall back to the process-level _exit.
                try:
                    self.root.after(0, self.root.destroy)
                except Exception:
                    pass
                _t.sleep(2)   # give destroy() a moment to fire
                _os._exit(1)  # unconditional: bypass Python cleanup


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap: show window → import heavy modules → build app
# ══════════════════════════════════════════════════════════════════════════════

def _wait_for_imports(root: tk.Tk, base_path: Path,
                      import_q: queue.Queue,
                      splash_frame: tk.Frame,
                      spin_lbl: tk.Label,
                      spin_state: list):
    try:
        msg = import_q.get_nowait()
    except queue.Empty:
        spin_state[0] = (spin_state[0] + 1) % len(_SPINNER)
        spin_lbl.config(text=_SPINNER[spin_state[0]])
        root.after(100, lambda: _wait_for_imports(
            root, base_path, import_q, splash_frame, spin_lbl, spin_state))
        return

    if msg[0] == "err":
        from tkinter import messagebox
        messagebox.showerror("Ошибка", f"Не удалось загрузить модуль pickle:\n{msg[1]}")
        root.destroy()
        return

    global _pickle
    _, _pickle = msg

    splash_frame.destroy()
    KisSearchApp(root, base_path)


def _resolve_base_path() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    if sys.platform == "win32" and _DEFAULT_DB_WIN.is_dir():
        return _DEFAULT_DB_WIN
    return _THIS_DIR


def main():
    base = _resolve_base_path()

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry(f"{WIN_W}x{WIN_H}")
    root.minsize(820, 540)
    root.configure(bg=C_BG)

    splash = tk.Frame(root, bg=C_BG)
    splash.place(relx=0, rely=0, relwidth=1, relheight=1)

    tk.Label(splash, text=APP_TITLE,
             bg=C_BG, fg=C_ACCENT,
             font=("Segoe UI", 13, "bold")).place(relx=0.5, rely=0.38, anchor="center")

    spin_lbl = tk.Label(splash, text=_SPINNER[0],
                        bg=C_BG, fg=C_ACCENT,
                        font=("Segoe UI", 26, "bold"))
    spin_lbl.place(relx=0.5, rely=0.50, anchor="center")

    tk.Label(splash, text="Загрузка модулей…",
             bg=C_BG, fg=C_DIM,
             font=("Segoe UI", 10)).place(relx=0.5, rely=0.60, anchor="center")

    root.update()   # render splash before any heavy work

    import_q = queue.Queue()
    threading.Thread(target=_do_heavy_imports, args=(import_q,),
                     daemon=True, name="imports").start()

    root.after(80, lambda: _wait_for_imports(
        root, base, import_q, splash, spin_lbl, [0]))

    root.mainloop()


if __name__ == "__main__":
    main()
