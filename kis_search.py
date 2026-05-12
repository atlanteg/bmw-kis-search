#!/usr/bin/env python3
"""
kis_search.py  –  BMW KIS database search tool

Scans a KIS.data (HSQLDB binary) file and lets you search software entries
by SGBM_NR, description, version, full ID, or type — using any combination
of substring patterns.

Usage
-----
  python3 kis_search.py [OPTIONS] [TERM ...]

  Positional:
    TERM            One or more substrings.  Default: ALL must match (AND).
                    Separate with | for OR:   EQ ALEV4 | G70

  Options:
    -d, --db PATH   Path to KIS.data or its containing directory.
                    Auto-detected if omitted (searches cwd and parents).
    -t, --type T    Filter by SGBM type: SWFK, CAFD, BTLD, HWEL, FLSL, …
    -o, --or        Match ANY term instead of ALL (OR mode).
    -s, --sort COL  Sort by: sgbm (default), type, version, desc.
    -i, --interactive   Interactive REPL with live search.
    --rebuild-cache Force re-scan even if cache exists.
    --no-color      Disable ANSI colours.
    --version       Show version.

Examples
--------
  python3 kis_search.py EQ ALEV4
  python3 kis_search.py -t SWFK EQ ALEV4
  python3 kis_search.py "EQ G70"
  python3 kis_search.py EQ ALEV4 --or EQ ALEV3
  python3 kis_search.py 00008891
  python3 kis_search.py 00008891_015
  python3 kis_search.py -i
"""

import argparse
import json
import mmap
import os
import re
import struct
import sys
import time
from pathlib import Path

try:
    import readline  # noqa: F401  – enables arrow keys / history in REPL
except ImportError:
    pass

__version__ = "1.1.0"

# ── ANSI colours ──────────────────────────────────────────────────────────────
R  = "\033[0m";  B  = "\033[1m";  D  = "\033[2m"
CY = "\033[36m"; GR = "\033[32m"; YL = "\033[33m"
RE = "\033[31m"; BL = "\033[34m"; MG = "\033[35m"
WH = "\033[37m"

_USE_COLOR = sys.stdout.isatty()

def _c(text, *codes):
    return ("".join(codes) + str(text) + R) if _USE_COLOR else str(text)

# ── SGBM type codes ───────────────────────────────────────────────────────────
# Derived from SGBMID byte 3 (0-indexed from MSB in the 8-byte big-endian SGBMID)
_SGBM_TYPES = {
    0x01: "HWEL", 0x02: "HWAP", 0x03: "HWFR",
    0x04: "CAFD", 0x05: "BTLD", 0x06: "BTLD",
    0x07: "FLSL", 0x08: "FLSL", 0x09: "ENTD",
    0x0A: "ENTD", 0x0B: "SWID", 0x0C: "SWFL",
    0x0D: "SWFK", 0x0E: "ENTD", 0x0F: "CHSC",
    0x10: "CRSF", 0x11: "ORSI",
}
_TYPE_COL = {
    "SWFK": GR, "CAFD": YL, "BTLD": CY,
    "HWEL": BL, "FLSL": MG, "ENTD": WH,
}

def _type_name(code):
    if code is None:
        return "????"
    return _SGBM_TYPES.get(code, f"T{code:02X}")

def _type_col(name):
    return _TYPE_COL.get(name, "")


# ── Binary extraction helpers ─────────────────────────────────────────────────

# Each TECHNISCHEEINHEIT row in HSQLDB binary:
#   ...SGBMID(8B)...WERT(4B)... [01] SGBM_NR(8 ASCII) [01 00 00 00 MAJOR]
#                                [01 00 00 00 MINOR] [01 00 00 00 PATCH]
#                                (5× CHAR(1)) BESCHREIBUNG(VARCHAR) ...
#
# Pattern: 8 uppercase hex ASCII chars followed by three HSQLDB INTEGERs
# stored as 01 00 00 00 <value_byte>  (covers versions 0–255, sufficient for BMW)

_ENTRY_RE = re.compile(
    rb'([0-9A-F]{8})'
    rb'\x01\x00\x00\x00([\x00-\xff])'   # MAJOR
    rb'\x01\x00\x00\x00([\x00-\xff])'   # MINOR
    rb'\x01\x00\x00\x00([\x00-\xff])'   # PATCH
)


def _skip_char1(data, pos):
    """Advance past one HSQLDB CHAR(1) field (null=1 byte, not-null=6 bytes)."""
    if pos >= len(data):
        return pos
    b = data[pos]
    if b == 0x00:
        return pos + 1  # NULL
    if b == 0x01 and pos + 5 < len(data) and data[pos + 1:pos + 5] == b'\x00\x00\x00\x01':
        return pos + 6  # 01 + length(1) + char
    return pos  # unrecognised — stop advancing


def _read_varchar(data, pos):
    """Read one HSQLDB VARCHAR field; returns (text, next_pos)."""
    if pos >= len(data):
        return "", pos
    if data[pos] == 0x00:
        return "", pos + 1  # NULL
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
    """
    Find the SGBM type byte from the SGBMID that precedes the ASCII SGBM_NR.
    SGBMID structure (big-endian 8 bytes): XX XX subtype TYPE 00 00 HIGH LOW
    where the last 4 bytes equal the numeric value of SGBM_NR.
    """
    try:
        nr_bytes = bytes.fromhex(sgbm_nr_hex)          # e.g. b'\x00\x00\x88\x91'
        window = data[max(0, match_start - 28): match_start]
        bi = window.rfind(nr_bytes)
        if bi >= 1:
            return window[bi - 1]                       # TYPE byte
    except Exception:
        pass
    return None


def extract_entries(data_path, progress=True, progress_cb=None):
    """
    Full binary scan of KIS.data.  Returns list of entry dicts.
    Each entry: sgbm_nr, major, minor, patch, version, full_id, desc, type.

    progress_cb(float 0..1) — optional callback called ~every 150 ms with
    the fraction of the file scanned so far.
    """
    path = str(data_path)
    size = os.path.getsize(path)

    if progress:
        print(_c(f"Scanning {path}  ({size / 1024**3:.2f} GB) …", D, YL),
              file=sys.stderr)

    t0 = time.time()
    entries = []
    _last_cb = [0.0]

    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for m in _ENTRY_RE.finditer(mm):
                if progress_cb:
                    now = time.time()
                    if now - _last_cb[0] >= 0.15:
                        progress_cb(m.start() / size)
                        _last_cb[0] = now
                sgbm_nr = m.group(1).decode("ascii")
                major   = m.group(2)[0]
                minor   = m.group(3)[0]
                patch   = m.group(4)[0]

                # Version sanity guard
                if major > 999 or minor > 999 or patch > 9999:
                    continue

                # Skip 5× CHAR(1) fields (GUELTIG_IM_SERVICE … LAESST_FLASHEN_ZU)
                p = m.end()
                for _ in range(5):
                    np = _skip_char1(mm, p)
                    if np == p:
                        break
                    p = np

                # BESCHREIBUNG (VARCHAR 1024)
                desc, _ = _read_varchar(mm, p)

                # Reject obvious garbage: description should be printable ASCII/latin
                if desc and not all(0x20 <= ord(c) < 0x100 for c in desc):
                    desc = ""

                tcode = _extract_type(mm, m.start(), sgbm_nr)
                tname = _type_name(tcode)

                entries.append({
                    "sgbm_nr": sgbm_nr,
                    "major":   major,
                    "minor":   minor,
                    "patch":   patch,
                    "version": f"{major}.{minor}.{patch}",
                    "full_id": f"{sgbm_nr}_{major:03d}_{minor:03d}_{patch:03d}",
                    "desc":    desc,
                    "type":    tname,
                })
        finally:
            mm.close()

    elapsed = time.time() - t0
    if progress:
        print(_c(f"  → {len(entries)} entries found in {elapsed:.1f}s", D),
              file=sys.stderr)

    return entries


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_path(data_path):
    return Path(str(data_path)).parent / ".kis_cache.json"


def load_cache(data_path):
    cp = _cache_path(data_path)
    if not cp.exists():
        return None
    try:
        with open(cp) as f:
            c = json.load(f)
        dp = str(data_path)
        if (abs(c.get("mtime", 0) - os.path.getmtime(dp)) < 1
                and c.get("size") == os.path.getsize(dp)):
            return c["entries"]
    except Exception:
        pass
    return None


def save_cache(data_path, entries):
    cp = _cache_path(data_path)
    try:
        with open(cp, "w") as f:
            json.dump({
                "mtime":   os.path.getmtime(str(data_path)),
                "size":    os.path.getsize(str(data_path)),
                "version": __version__,
                "entries": entries,
            }, f, separators=(",", ":"))
        print(_c(f"  Cache saved → {cp}", D), file=sys.stderr)
    except Exception as e:
        print(_c(f"  Warning: could not save cache: {e}", RE), file=sys.stderr)


# ── Search ────────────────────────────────────────────────────────────────────

def _haystack(e):
    return " ".join([
        e["full_id"], e["sgbm_nr"], e["desc"], e["type"], e["version"],
    ]).upper()


def _and_match(entry, include, exclude):
    """
    Return True if ALL include-terms match AND NO exclude-term matches.
    Terms prefixed with '!' in the lists are treated as exclude terms here
    only if already separated; caller is expected to split them first.
    """
    h = _haystack(entry)
    return (all(t.upper() in h for t in include) and
            not any(t.upper() in h for t in exclude))


def search(entries, groups, exclude=None, type_filter=None):
    """
    Filter entries.

    groups      – list of AND-term lists; entry matches if ANY group fully matches
                  e.g. [['EQ','ALEV4'], ['EQ','ALEV3']]  →  (EQ & ALEV4) OR (EQ & ALEV3)
                  e.g. [['EQ','ALEV4']]                  →  EQ & ALEV4 (plain AND)
    exclude     – list of terms that must NOT appear (applied after group match)
    type_filter – restrict to this SGBM type string, e.g. 'SWFK'
    """
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
    """
    Convert a flat list of CLI/REPL tokens into:
      - OR-groups of AND include-terms
      - a flat list of exclude-terms  (tokens starting with '!')

    Syntax:
      '|' token (or embedded '|') → OR separator between AND groups
      '!TERM'                     → exclude term (never appears in any group)

    Examples:
      ['EQ', 'ALEV4', '|', 'EQ', 'ALEV3']  →  groups=[['EQ','ALEV4'],['EQ','ALEV3']], excl=[]
      ['B58', '!ASD', '!RWD']              →  groups=[['B58']], excl=['ASD','RWD']
      ['B58', '!ASD', '|', 'B48', '!RWD'] →  groups=[['B58'],['B48']], excl=['ASD','RWD']
    """
    groups  = []
    cur     = []
    exclude = []

    for tok in term_list:
        # Split embedded pipes first
        parts = tok.split("|")
        for i, part in enumerate(parts):
            if i > 0:                          # a '|' was embedded
                if cur:
                    groups.append(cur)
                cur = []
            part = part.strip()
            if not part:
                continue
            if part.startswith("!"):
                exclude.append(part[1:])
            elif part == "|":
                if cur:
                    groups.append(cur)
                cur = []
            else:
                cur.append(part)

    if cur:
        groups.append(cur)

    return (groups if groups else [[]]), exclude


# ── Display ───────────────────────────────────────────────────────────────────

_COL_WIDTHS = {"sgbm": 8, "type": 4, "version": 10, "full_id": 24}


def _highlight(text, terms):
    """Wrap matched substrings with yellow+bold ANSI codes."""
    if not _USE_COLOR or not terms:
        return text
    for t in terms:
        text = re.sub(
            re.escape(t),
            lambda m: _c(m.group(), YL, B),
            text,
            flags=re.IGNORECASE,
        )
    return text


def print_table(results, terms=None, excl_terms=None, sort_by="sgbm"):
    if not results:
        print(_c("  No matches found.", RE))
        return

    # Sort
    _sort_keys = {
        "sgbm":    lambda e: (e["sgbm_nr"], e["major"], e["minor"], e["patch"]),
        "type":    lambda e: (e["type"], e["sgbm_nr"]),
        "version": lambda e: (e["major"], e["minor"], e["patch"]),
        "desc":    lambda e: e["desc"].lower(),
    }
    key = _sort_keys.get(sort_by, _sort_keys["sgbm"])
    results = sorted(results, key=key)

    try:
        term_w = os.get_terminal_size().columns
    except OSError:
        term_w = 120
    w = _COL_WIDTHS
    w_desc = max(20, term_w - w["sgbm"] - w["type"] - w["version"] - w["full_id"] - 12)

    sep = _c("─" * min(term_w, 100), D)
    hdr = (f"{'SGBM_NR':<{w['sgbm']}}  {'TYPE':<{w['type']}}  "
           f"{'VERSION':<{w['version']}}  {'FULL ID':<{w['full_id']}}  DESCRIPTION")
    print(_c(hdr, B))
    print(sep)

    prev_nr = None
    for e in results:
        if e["sgbm_nr"] != prev_nr and prev_nr is not None:
            print()   # blank line between different SGBM_NRs
        prev_nr = e["sgbm_nr"]

        tname = e["type"]
        tc    = _type_col(tname)
        desc  = e["desc"][:w_desc] if e["desc"] else _c("(no description)", D)

        sgbm  = _c(e["sgbm_nr"], B)
        typ   = _c(f"{tname:<{w['type']}}", tc, B)
        ver   = f"{e['version']:<{w['version']}}"
        fid   = _c(f"{e['full_id']:<{w['full_id']}}", CY)
        dsc   = _highlight(desc, terms)
        if excl_terms and _USE_COLOR:
            for t in excl_terms:
                dsc = re.sub(re.escape(t), _c(t, RE, B), dsc, flags=re.IGNORECASE)

        print(f"{sgbm}  {typ}  {ver}  {fid}  {dsc}")

    print()
    print(_c(f"  {len(results)} result(s)", D))


# ── Interactive REPL ──────────────────────────────────────────────────────────

_REPL_HELP = """
  Search syntax:
    term1 term2        both terms must match  (AND)
    term1 | term2      either term matches    (OR)
    00008891_015       partial full-ID match
    "EQ G70"           phrase match (quoted, treated as one term)

  Commands:
    :type SWFK     restrict to SGBM type  (SWFK CAFD BTLD HWEL FLSL …)
    :type          clear type restriction
    :sort sgbm     sort by sgbm_nr (default)
    :sort type | version | desc
    :and / :or     set default match mode
    :all           clear all filters
    :count         show total loaded entries
    :help          this message
    :q / :quit     exit
"""


def interactive_loop(entries, db_path):
    print(_c(f"\nKIS Search  [{db_path}]", B, GR))
    print(_c(f"{len(entries)} entries loaded.  Type :help for syntax.\n", D))

    type_filter = None
    mode        = "and"
    sort_by     = "sgbm"

    while True:
        tf_label = f" [{type_filter}]" if type_filter else ""
        prompt   = _c(f"kis{tf_label}> ", GR, B)
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        # ── Commands ──────────────────────────────────────────────────────
        if raw.startswith(":"):
            cmd  = raw[1:].strip()
            argv = cmd.split(None, 1)
            verb = argv[0].lower() if argv else ""

            if verb in ("q", "quit", "exit"):
                break
            elif verb == "help":
                print(_c(_REPL_HELP, D))
            elif verb == "count":
                print(_c(f"  {len(entries)} entries total", D))
            elif verb == "all":
                type_filter = None
                mode        = "and"
                print(_c("  Filters cleared.", D))
            elif verb == "type":
                type_filter = argv[1].strip().upper() if len(argv) > 1 else None
                if type_filter:
                    print(_c(f"  Type filter: {type_filter}", D))
                else:
                    print(_c("  Type filter cleared.", D))
            elif verb == "sort":
                val = argv[1].strip().lower() if len(argv) > 1 else "sgbm"
                if val in ("sgbm", "type", "version", "desc"):
                    sort_by = val
                    print(_c(f"  Sort by: {sort_by}", D))
                else:
                    print(_c(f"  Unknown sort key '{val}'. Use: sgbm type version desc", RE))
            elif verb in ("and", "or"):
                mode = verb
                print(_c(f"  Match mode: {mode.upper()}", D))
            else:
                print(_c(f"  Unknown command ':{verb}'. Type :help.", RE))
            continue

        # ── Search ────────────────────────────────────────────────────────
        # Tokenise (honour double-quotes), build OR-of-AND groups via |, ! for exclude
        tokens = re.findall(r'"[^"]*"|\S+', raw)
        tokens = [t.strip('"') for t in tokens]
        groups, excl = _parse_terms(tokens)

        results    = search(entries, groups, exclude=excl, type_filter=type_filter)
        flat_terms = [t for grp in groups for t in grp]
        print()
        print_table(results, terms=flat_terms, excl_terms=excl, sort_by=sort_by)
        print()


# ── DB discovery ──────────────────────────────────────────────────────────────

def find_db(hint=None):
    """Locate KIS.data from a hint path, or by searching cwd upward."""
    if hint:
        p = Path(hint)
        if p.is_file() and p.name == "KIS.data":
            return p
        if p.is_dir():
            hits = sorted(p.glob("**/KIS.data"))
            if hits:
                return hits[0]
        raise FileNotFoundError(f"No KIS.data found at: {hint}")

    # Walk cwd and up to 3 parent levels, including one subdirectory level each
    cwd = Path.cwd()
    for base in [cwd] + list(cwd.parents)[:3]:
        hits = sorted(base.glob("*/KIS.data")) + sorted(base.glob("KIS.data"))
        if hits:
            return hits[0]

    raise FileNotFoundError(
        "Could not find KIS.data automatically. Use -d/--db to specify the path."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        prog="kis_search.py",
        description="Search BMW KIS (HSQLDB) database for software entries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 kis_search.py EQ ALEV4
  python3 kis_search.py -t SWFK G70
  python3 kis_search.py 00008891_015
  python3 kis_search.py "EQ G70" --or "EQ G68"
  python3 kis_search.py -i
  python3 kis_search.py -d /path/to/S18A EQ ALEV4
        """,
    )
    p.add_argument("terms", nargs="*",
                   help="Search terms. AND by default. Use | between terms for OR: EQ '|' ALEV4")
    p.add_argument("-d", "--db", metavar="PATH",
                   help="Path to KIS.data or its containing directory")
    p.add_argument("-t", "--type", metavar="TYPE", dest="type_filter",
                   help="Filter by SGBM type: SWFK, CAFD, BTLD, HWEL, FLSL, …")
    p.add_argument("-o", "--or", dest="or_mode", action="store_true",
                   help="Match ANY term (OR).  Default is ALL (AND).")
    p.add_argument("-s", "--sort", metavar="COL", dest="sort_by",
                   default="sgbm", choices=["sgbm", "type", "version", "desc"],
                   help="Sort by: sgbm (default), type, version, desc")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="Launch interactive search REPL")
    p.add_argument("--rebuild-cache", action="store_true",
                   help="Force re-scan even if cached data exists")
    p.add_argument("--no-color", action="store_true",
                   help="Disable ANSI colour output")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main():
    global _USE_COLOR

    parser = build_parser()
    args   = parser.parse_args()

    # Build OR-of-AND groups + exclusions from token list
    # --or flag: every non-excluded token becomes its own OR alternative
    if args.or_mode:
        _, excl   = _parse_terms(args.terms)
        pos_terms = [t for t in args.terms if not t.startswith("!")]
        groups    = [[t] for t in pos_terms if t]
    else:
        groups, excl = _parse_terms(args.terms)

    if args.no_color:
        _USE_COLOR = False

    if not args.interactive and not args.terms and not args.rebuild_cache:
        parser.print_help()
        print()
        sys.exit(0)

    # Locate DB
    try:
        db = find_db(args.db)
    except FileNotFoundError as e:
        print(_c(f"Error: {e}", RE), file=sys.stderr)
        sys.exit(1)

    # Load or build cache
    entries = None
    if not args.rebuild_cache:
        entries = load_cache(db)
        if entries is not None:
            print(_c(f"Loaded {len(entries)} entries from cache  [{db}]", D),
                  file=sys.stderr)

    if entries is None:
        entries = extract_entries(db, progress=True)
        save_cache(db, entries)

    if args.interactive:
        interactive_loop(entries, db)
        return

    flat_terms = [t for grp in groups for t in grp]
    results = search(entries, groups, exclude=excl, type_filter=args.type_filter)
    print_table(results, terms=flat_terms, excl_terms=excl, sort_by=args.sort_by)


if __name__ == "__main__":
    main()
