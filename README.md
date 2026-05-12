# BMW KIS Search

Search tools for BMW KIS (Kodier Information System) databases in HSQLDB 2.7.2 binary format.

Written by **NBTBoost © Atlanteg**

---

## Tools

### `kis_search.py` — CLI / Terminal

Search the KIS database from the command line.

**Requirements:** Python 3.8+, no external dependencies.

**Usage:**

```bash
# Basic search (all terms must match)
python3 kis_search.py EQ ALEV4

# OR groups (pipe separator)
python3 kis_search.py EQ ALEV4 "|" EQ ALEV3

# Exclusion
python3 kis_search.py B58 '!ASD' '!RWD'

# Filter by type
python3 kis_search.py -t SWFK EQ ALEV4

# Interactive REPL mode
python3 kis_search.py -i

# Specify platform folder
python3 kis_search.py -d C:\kisdb\S18A EQ ALEV4
```

**Interactive REPL commands:**

| Command | Description |
|---------|-------------|
| `:type SWFK` | Filter by type (SWFK, CAFD, BTLD, HWEL, …) |
| `:sort version` | Sort by field (sgbm_nr, type, version, desc) |
| `:all` | Clear all filters |
| `:count` | Show result count only |
| `:help` | Show help |
| `:quit` | Exit |

---

### `kis_search_gui.py` — Windows GUI

Graphical interface with platform selector, search fields, and results table.

**Requirements:** Python 3.8+ with Tkinter (included in standard Windows Python).

**Usage:**

```bash
python kis_search_gui.py
python kis_search_gui.py  C:\path\to\databases
```

**Default database path (Windows):** `C:\data\psdzdata\kiswb`  
If that folder doesn't exist or contains no `KIS.data` files, a **Browse…** button appears so you can select any folder.

**Features:**
- Auto-detects all platform folders containing `KIS.data`
- Platforms preloaded sequentially at startup — switching is instant once loaded
- Chunked binary cache (`.kis_cache/`) — first scan ~1 min, subsequent starts < 1 sec
- Animated loading overlay (app stays fully responsive during any operation)
- OR search via `|` in the search field: `EQ ALEV4 | EQ ALEV3`
- Exclusion field for negative filters
- Type filter dropdown (SWFK, CAFD, BTLD, HWEL, …)
- Double-click / Ctrl+C to copy Full ID
- Right-click context menu with TSV export
- **Browse…** button to switch database folder at any time

---

## Supported Types

| Type | Description |
|------|-------------|
| SWFK | Software function |
| CAFD | Coding and adaptation flash data |
| BTLD | Bootloader |
| HWEL | Hardware element |
| FLSL | Flash segment list |
| ENTD | Entry data |
| HWAP | Hardware application |
| SWFL | Software flash |

---

## Development

After cloning, activate the git hooks once:

```bash
git config core.hooksPath hooks
```

Hooks included:
- **`hooks/pre-commit`** — increments `BUILD` in `VERSION` and `kis_search_gui.py` before every commit
- **`hooks/post-commit`** — pushes to `origin/main` after every commit

---

## Database Layout

Place this script alongside your platform folders:

```
kisdb/
├── kis_search.py
├── kis_search_gui.py
├── S18A/
│   └── KIS.data
├── F25/
│   └── KIS.data
└── ...
```
