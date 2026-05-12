#!/usr/bin/env python3
"""
kis_search_gui.py  –  BMW KIS Database Search GUI
Written by NBTBoost (c) Atlanteg

Graphical companion to kis_search.py.
Requires Python 3.8+ with Tkinter (bundled in standard Python for Windows).

Platform folders are auto-detected: any subfolder containing KIS.data
in the same directory as this script (or the given base path).

All platforms are preloaded in parallel at startup so switching is instant.
A fast binary cache (.kis_gui_cache.pkl) is maintained per platform next to
KIS.data — first scan takes ~1 min, every subsequent start is under 1 second.

Usage:
    python kis_search_gui.py
    python kis_search_gui.py  C:\\path\\to\\databases
"""

import pickle
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

# ── DPI awareness on Windows ──────────────────────────────────────────────────
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ── Shared logic from kis_search.py ──────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
sys.path.insert(0, str(_THIS_DIR))
try:
    from kis_search import extract_entries, search, _parse_terms
except ImportError as _e:
    print(f"Error: kis_search.py must be in the same folder as this script.\n{_e}")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
APP_TITLE   = "BMW KIS Search  ·  Written by NBTBoost © Atlanteg"
WIN_W       = 1150
WIN_H       = 700
FONT_UI     = ("Segoe UI", 9)
FONT_MONO   = ("Consolas", 9)
FONT_BOLD   = ("Segoe UI", 9, "bold")
FONT_BIG    = ("Segoe UI", 13)
FONT_HUGE   = ("Segoe UI", 18, "bold")
TYPES       = ["All", "SWFK", "CAFD", "BTLD", "HWEL", "FLSL", "ENTD", "HWAP", "SWFL"]
SORT_OPTS   = ["sgbm_nr", "type", "version", "desc"]

COL_IDS    = ("sgbm_nr", "type", "version", "full_id", "desc")
COL_HEADS  = ("SGBM_NR",  "Type", "Version", "Full ID",  "Description")
COL_WIDTHS = (92,          62,     88,         215,        390)
COL_ANCHOR = ("w",         "c",    "w",        "w",        "w")

# ── Colour palette ────────────────────────────────────────────────────────────
C_BG      = "#1e1e2e"
C_PANEL   = "#2a2a3e"
C_INPUT   = "#313145"
C_BORDER  = "#44445a"
C_FG      = "#cdd6f4"
C_DIM     = "#7f849c"
C_ACCENT  = "#89b4fa"
C_GREEN   = "#a6e3a1"
C_YELLOW  = "#f9e2af"
C_RED     = "#f38ba8"
C_CYAN    = "#89dceb"
C_SEL     = "#313160"
C_ROW_ALT = "#252538"
C_OVERLAY = "#1a1a28"

TYPE_COLORS = {
    "SWFK": C_GREEN, "CAFD": C_YELLOW,
    "BTLD": C_CYAN,  "HWEL": C_ACCENT, "FLSL": "#cba6f7",
}

# Spinner frames (cycling arrow)
_SPINNER = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]


# ── Fast pickle cache (per platform) ─────────────────────────────────────────

def _pkl_path(db_path: Path) -> Path:
    return db_path.parent / ".kis_gui_cache.pkl"

def load_fast_cache(db_path: Path):
    """Load pickle cache if it exists and is newer than KIS.data."""
    pkl = _pkl_path(db_path)
    try:
        if pkl.exists() and pkl.stat().st_mtime >= db_path.stat().st_mtime:
            with open(pkl, "rb") as f:
                return pickle.load(f)
    except Exception:
        pass
    return None

def save_fast_cache(db_path: Path, entries: list):
    """Save entries to pickle cache."""
    try:
        with open(_pkl_path(db_path), "wb") as f:
            pickle.dump(entries, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        print(f"Warning: could not save GUI cache: {e}")


# ── Application ───────────────────────────────────────────────────────────────

class KisSearchApp:
    def __init__(self, root: tk.Tk, base_path: Path):
        self.root      = root
        self.base_path = base_path

        # per-platform state
        self.platforms   : list[Path] = []   # Path to each platform folder
        self._db         : dict[str, list | None] = {}   # name → entries | None
        self._loading    : set[str] = set()  # names currently loading
        self._queues     : dict[str, queue.Queue] = {}

        # current view
        self.entries     : list = []
        self._sort_col   = "sgbm_nr"
        self._sort_rev   = False
        self._debounce   = None
        self._spin_idx   = 0
        self._spin_after = None

        self._setup_style()
        self._build_ui()
        self._find_and_preload()

    # ── ttk style ─────────────────────────────────────────────────────────────

    def _setup_style(self):
        self.root.title(APP_TITLE)
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.minsize(820, 520)
        self.root.configure(bg=C_BG)

        s = ttk.Style()
        for theme in ("clam", "alt", "default"):
            if theme in s.theme_names():
                s.theme_use(theme)
                break

        s.configure(".",           background=C_BG, foreground=C_FG,
                    font=FONT_UI,  borderwidth=0, relief="flat")
        s.configure("TFrame",      background=C_BG)
        s.configure("P.TFrame",    background=C_PANEL)
        s.configure("TLabel",      background=C_BG,    foreground=C_FG,  font=FONT_UI)
        s.configure("Dim.TLabel",  background=C_BG,    foreground=C_DIM, font=FONT_UI)
        s.configure("P.TLabel",    background=C_PANEL, foreground=C_FG,  font=FONT_UI)
        s.configure("PD.TLabel",   background=C_PANEL, foreground=C_DIM, font=FONT_UI)
        s.configure("Ov.TLabel",   background=C_OVERLAY, foreground=C_FG)
        s.configure("OvD.TLabel",  background=C_OVERLAY, foreground=C_DIM)
        s.configure("OvH.TLabel",  background=C_OVERLAY, foreground=C_ACCENT,
                    font=FONT_HUGE)
        s.configure("OvS.TLabel",  background=C_OVERLAY, foreground=C_DIM,
                    font=FONT_BIG)

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
              foreground=[("readonly", C_FG)])

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

        s.configure("TProgressbar", troughcolor=C_PANEL,
                    background=C_ACCENT, borderwidth=0)
        s.configure("TScrollbar", background=C_PANEL, troughcolor=C_BG,
                    arrowcolor=C_DIM, relief="flat", borderwidth=0)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # ── Top bar ───────────────────────────────────────────────────────────
        top = ttk.Frame(self.root, style="P.TFrame", padding=(12, 8))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(3, weight=1)

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

        # loading indicators per platform (shown right side)
        self.lbl_loading_all = ttk.Label(top, text="", style="P.TLabel",
                                         background=C_PANEL, foreground=C_ACCENT)
        self.lbl_loading_all.grid(row=0, column=4, sticky="e", padx=(10, 0))

        # ── Search controls ───────────────────────────────────────────────────
        sf = ttk.Frame(self.root, padding=(12, 8, 12, 4))
        sf.grid(row=1, column=0, sticky="ew")
        sf.columnconfigure(1, weight=1)
        sf.columnconfigure(4, weight=0)

        # Row 0: include
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

        self.btn_search = ttk.Button(sf, text="Найти",
                                     command=self._do_search)
        self.btn_search.grid(row=0, column=5, rowspan=2, sticky="ns",
                             padx=(12, 0), ipadx=8)

        # Row 1: exclude + sort
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

        # ── Results area (treeview + loading overlay) ─────────────────────────
        self.rf = ttk.Frame(self.root)
        self.rf.grid(row=2, column=0, sticky="nsew", padx=10, pady=(4, 0))
        self.rf.columnconfigure(0, weight=1)
        self.rf.rowconfigure(0, weight=1)

        # Treeview
        self.tree = ttk.Treeview(self.rf, columns=COL_IDS, show="headings",
                                 selectmode="extended")
        for cid, head, w, anc in zip(COL_IDS, COL_HEADS, COL_WIDTHS, COL_ANCHOR):
            self.tree.heading(cid, text=head,
                              command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=w, minwidth=40, anchor=anc,
                             stretch=(cid == "desc"))
        vsb = ttk.Scrollbar(self.rf, orient="vertical",
                            command=self.tree.yview)
        hsb = ttk.Scrollbar(self.rf, orient="horizontal",
                            command=self.tree.xview)
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

        # Loading overlay (shown over treeview while platform is loading)
        self.overlay = tk.Frame(self.rf, bg=C_OVERLAY)
        # Not placed yet; shown via _show_overlay / _hide_overlay

        self.lbl_spin = tk.Label(self.overlay, text="", bg=C_OVERLAY,
                                 fg=C_ACCENT, font=("Segoe UI", 22, "bold"))
        self.lbl_spin.place(relx=0.5, rely=0.38, anchor="center")

        self.lbl_load_title = tk.Label(self.overlay, text="",
                                       bg=C_OVERLAY, fg=C_FG,
                                       font=("Segoe UI", 14, "bold"))
        self.lbl_load_title.place(relx=0.5, rely=0.48, anchor="center")

        self.lbl_load_sub = tk.Label(self.overlay, text="",
                                     bg=C_OVERLAY, fg=C_DIM,
                                     font=("Segoe UI", 10))
        self.lbl_load_sub.place(relx=0.5, rely=0.56, anchor="center")

        self.pbar = ttk.Progressbar(self.overlay, mode="indeterminate",
                                    length=320)
        self.pbar.place(relx=0.5, rely=0.64, anchor="center")

        # ── Status bar ────────────────────────────────────────────────────────
        sb = ttk.Frame(self.root, padding=(12, 4))
        sb.grid(row=3, column=0, sticky="ew")
        sb.columnconfigure(0, weight=1)

        self.var_status = tk.StringVar(value="Инициализация…")
        ttk.Label(sb, textvariable=self.var_status,
                  style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(sb, text="ДвойнойКлик / Ctrl+C → копировать Full ID",
                  style="Dim.TLabel").grid(row=0, column=1, sticky="e")

        # ── Context menu ──────────────────────────────────────────────────────
        self._ctx = tk.Menu(self.root, tearoff=False,
                            bg=C_PANEL, fg=C_FG,
                            activebackground=C_ACCENT, activeforeground=C_BG,
                            font=FONT_UI)
        self._ctx.add_command(label="Копировать Full ID",
                              command=self._copy_full_id)
        self._ctx.add_command(label="Копировать SGBM_NR",
                              command=lambda: self._copy_col(0))
        self._ctx.add_command(label="Копировать описание",
                              command=lambda: self._copy_col(4))
        self._ctx.add_separator()
        self._ctx.add_command(label="Копировать все строки (TSV)",
                              command=self._copy_all_tsv)

    # ── Overlay animation ─────────────────────────────────────────────────────

    def _show_overlay(self, title: str, sub: str = ""):
        self.lbl_load_title.config(text=title)
        self.lbl_load_sub.config(text=sub)
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.overlay.lift()
        self.pbar.start(14)
        self._spin_idx = 0
        self._animate_spin()

    def _hide_overlay(self):
        if self._spin_after:
            self.root.after_cancel(self._spin_after)
            self._spin_after = None
        self.pbar.stop()
        self.overlay.place_forget()

    def _animate_spin(self):
        frame = _SPINNER[self._spin_idx % len(_SPINNER)]
        self.lbl_spin.config(text=frame)
        self._spin_idx += 1
        self._spin_after = self.root.after(100, self._animate_spin)

    # ── Platform detection & preloading ──────────────────────────────────────

    def _find_and_preload(self):
        """Discover all platform folders, populate dropdown, start loading all."""
        found = []
        base  = self.base_path
        if base.is_dir():
            for sub in sorted(base.iterdir()):
                if sub.is_dir() and (sub / "KIS.data").exists():
                    found.append(sub)
        if (base / "KIS.data").exists() and base not in found:
            found.insert(0, base)

        self.platforms = found
        names = [p.name for p in found]
        self.cb_platform["values"] = names

        if not found:
            self._set_status("KIS.data не найден.  "
                             "Разместите скрипт рядом с папками платформ.")
            return

        # Pre-initialise state
        for p in found:
            self._db[p.name]     = None
            self._queues[p.name] = queue.Queue()

        # Select first platform, show overlay
        self.cb_platform.current(0)
        first = found[0].name
        self._show_overlay(
            f"Загрузка базы данных  {first}",
            "Первый запуск займёт ~1 минуту — создаётся кэш…"
            if not _pkl_path(found[0] / "KIS.data").exists()
            else "Загрузка из кэша…"
        )

        # Start loading ALL platforms in background
        for p in found:
            self._start_load_thread(p)

        # Poll loop
        self.root.after(120, self._poll_all)

    def _start_load_thread(self, plat_path: Path, force: bool = False):
        name   = plat_path.name
        db     = plat_path / "KIS.data"
        q      = self._queues[name]
        self._loading.add(name)

        def _worker():
            try:
                t0 = time.time()
                if not force:
                    cached = load_fast_cache(db)
                    if cached is not None:
                        q.put(("done", name, cached, True, time.time() - t0))
                        return
                entries = extract_entries(db, progress=False)
                save_fast_cache(db, entries)
                q.put(("done", name, entries, False, time.time() - t0))
            except Exception as exc:
                q.put(("error", name, str(exc)))

        threading.Thread(target=_worker, daemon=True, name=f"load-{name}").start()

    def _poll_all(self):
        """Check all loading queues; update UI when any finishes."""
        active = self.cb_platform.get()

        finished_now = []
        for name, q in self._queues.items():
            try:
                msg = q.get_nowait()
            except queue.Empty:
                continue

            if msg[0] == "done":
                _, pname, entries, from_cache, elapsed = msg
                self._db[pname] = entries
                self._loading.discard(pname)
                finished_now.append((pname, len(entries), from_cache, elapsed))
            else:
                _, pname, err = msg
                self._db[pname] = []
                self._loading.discard(pname)
                messagebox.showerror(
                    "Ошибка загрузки",
                    f"Платформа {pname}:\n{err}")

        # Update UI when active platform finishes
        for pname, n, from_cache, elapsed in finished_now:
            if pname == active:
                self.entries = self._db[pname]
                src = "кэш" if from_cache else f"сканирование {elapsed:.0f}с"
                self.lbl_plat_info.config(
                    text=f"{pname}  ·  {n:,} записей  ({src})")
                self._do_search()

        # If active platform is now loaded, hide overlay
        if active not in self._loading and self._db.get(active) is not None:
            self._hide_overlay()
            n = len(self._db[active])
            still = len(self._loading)
            status = f"Готово — {n:,} записей  [{active}]"
            if still:
                status += f"  ·  загружается ещё {still} платформ в фоне…"
            self._set_status(status)

        # Update top-bar loading indicator
        still_loading = sorted(self._loading)
        if still_loading:
            spin = _SPINNER[self._spin_idx % len(_SPINNER)]
            self.lbl_loading_all.config(
                text=f"{spin} загружается: {', '.join(still_loading)}")
        else:
            self.lbl_loading_all.config(text="✓ все платформы готовы")

        # Keep polling while anything still loading
        if self._loading:
            self.root.after(120, self._poll_all)

    # ── Platform switching ────────────────────────────────────────────────────

    def _on_platform_change(self, _event=None):
        name = self.var_platform.get()
        entries = self._db.get(name)

        if entries is not None:
            # Already loaded → instant switch
            self.entries = entries
            n = len(entries)
            self.lbl_plat_info.config(text=f"{name}  ·  {n:,} записей")
            self._do_search()
            self._set_status(f"Платформа: {name}  ·  {n:,} записей")
        else:
            # Still loading → show overlay and wait
            self.entries = []
            self._clear_table()
            is_first = not _pkl_path(
                next(p for p in self.platforms if p.name == name) / "KIS.data"
            ).exists()
            self._show_overlay(
                f"Загрузка  {name}",
                "Первый запуск — создаётся кэш (~1 мин)…" if is_first
                else "Загрузка из кэша…"
            )
            self._set_status(f"Загрузка платформы {name}…")
            self._wait_for_platform(name)

    def _wait_for_platform(self, name: str):
        """Poll until the requested platform finishes loading."""
        entries = self._db.get(name)
        if entries is not None:
            self.entries = entries
            self._do_search()
            self._hide_overlay()
            self.lbl_plat_info.config(
                text=f"{name}  ·  {len(entries):,} записей")
            self._set_status(f"Платформа: {name}  ·  {len(entries):,} записей")
            return
        # Still loading — try the queue
        q = self._queues.get(name)
        if q:
            try:
                msg = q.get_nowait()
                if msg[0] == "done":
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
            except queue.Empty:
                pass
        self.root.after(150, lambda: self._wait_for_platform(name))

    def _reload_db(self):
        name = self.var_platform.get()
        plat = next((p for p in self.platforms if p.name == name), None)
        if not plat:
            return
        self._db[name] = None
        self._loading.add(name)
        self._queues[name] = queue.Queue()
        self.entries = []
        self._clear_table()
        self._show_overlay(f"Пересканирование  {name}",
                           "Повторное считывание базы данных…")
        self._set_status(f"Перезагрузка {name}…")
        self._start_load_thread(plat, force=True)
        self._wait_for_platform(name)

    # ── Search ────────────────────────────────────────────────────────────────

    def _on_key(self, _event=None):
        if self._debounce:
            self.root.after_cancel(self._debounce)
        self._debounce = self.root.after(320, self._do_search)

    def _do_search(self):
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
        self._populate_table(results)

        parts = [f"{len(results):,} результатов"]
        if inc_raw:
            parts.append(f"поиск: {inc_raw}")
        if exc_raw:
            parts.append(f"исключить: {exc_raw}")
        self._set_status("  ·  ".join(parts))

    def _clear(self):
        self.var_include.set("")
        self.var_exclude.set("")
        self.var_type.set("All")
        self._do_search()

    # ── Table ─────────────────────────────────────────────────────────────────

    def _clear_table(self):
        self.tree.delete(*self.tree.get_children())

    def _populate_table(self, results: list):
        self._clear_table()
        prev_nr = None
        alt     = False
        for e in results:
            if e["sgbm_nr"] != prev_nr:
                if prev_nr is not None:
                    alt = not alt
                prev_nr = e["sgbm_nr"]
            tname = e["type"]
            tag   = f"t_{tname}" + ("_alt" if alt else "")
            self.tree.insert("", "end",
                             values=(e["sgbm_nr"], tname, e["version"],
                                     e["full_id"], e["desc"]),
                             tags=(tag,))

    def _sort_by(self, col):
        sk = {"full_id": "sgbm_nr"}.get(col, col)
        if col in SORT_OPTS:
            sk = col
        self._sort_rev = (not self._sort_rev) if self._sort_col == sk else False
        self.var_sort.set(sk)
        self._do_search()
        for c in COL_IDS:
            base = dict(zip(COL_IDS, COL_HEADS))[c]
            arrow = (" ▲" if not self._sort_rev else " ▼") if c == col else ""
            self.tree.heading(c, text=base + arrow)

    # ── Clipboard ─────────────────────────────────────────────────────────────

    def _sel_values(self):
        return [self.tree.item(i)["values"] for i in self.tree.selection()]

    def _copy_col(self, idx, _event=None):
        rows = self._sel_values()
        if not rows:
            return
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

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _set_status(self, text: str):
        self.var_status.set(text)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else _THIS_DIR
    root = tk.Tk()
    KisSearchApp(root, base)
    root.mainloop()


if __name__ == "__main__":
    main()
