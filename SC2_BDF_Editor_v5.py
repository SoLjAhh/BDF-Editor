"""
BDF Editor — GUI tool for Supreme Commander 2 TOC files  (PC & Xbox 360)
Decompile .bdf → edit folders / gamedata / file entries → recompile .bdf

Supports both platforms:
  • PC   (little-endian, magic MFDB, toc.win.bdf)
  • Xbox 360  (big-endian, magic BDFM, toc.x360.bdf)
Platform is auto-detected on open and selectable on save.

USAGE
  • Run directly:          python BDF_Editor.py
  • Drag-and-drop:         drag a .bdf onto this script (or a .exe built from it)
  • Command line:          python BDF_Editor.py  path/to/file.bdf

Requirements: Python 3.8+  (tkinter is included with standard Python installs)

FIELD REFERENCE (list_of_files.json)
  folder_number               Index into folders.txt   (0-based line number)
  file_path                   Name of the file
  prefetch                    Prefetch textures boolean  1 = true, 0 = false
  scd_number                  Index into gamedata.txt  (0-based line number)
                              Use 4294967295 (0xFFFFFFFF) for "not in any SCD"
  serial_number inside scd    0-based position of the file inside the .scd archive
  file_size                   Size in bytes
"""

import struct
import zlib
import json
import os
import sys
import zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# ──────────────────────────────────────────────────────────────────────────────
#  BDF core logic  (decompile / compile)
# ──────────────────────────────────────────────────────────────────────────────

def align(data: bytes, boundary: int) -> bytes:
    pad = (boundary - len(data) % boundary) % boundary
    return data + b"\x00" * pad


def decompile_bdf(file_path: str):
    """Read a .bdf → (platform, folders, gamedata, list_of_files).

    Platform is "pc" (little-endian, MFDB) or "xbox" (big-endian, BDFM).
    File entries always use the key "prefetch" internally.
    """
    with open(file_path, "rb") as f:
        magic = f.read(4)
        if magic == b"MFDB":
            platform = "pc"
            endian = "<"
        elif magic == b"BDFM":
            platform = "xbox"
            endian = ">"
        else:
            raise ValueError(f"Not a BDF file (magic={magic!r})")
        major, minor, reserved = struct.unpack(f"{endian}3I", f.read(12))
        comp_sz, decomp_sz, offset_count, num_files = struct.unpack(f"{endian}4I", f.read(16))
        ot_start = f.tell()
        f.seek(ot_start + 4 * offset_count)
        comp_start = (f.tell() + 31) & ~31
        f.seek(comp_start)
        raw = zlib.decompress(f.read(comp_sz))
        if len(raw) != decomp_sz:
            raise ValueError("Decompressed size mismatch")

    hdr = struct.unpack(f"{endian}7I", raw[:28])
    folder_count   = hdr[1]
    off_folders    = hdr[2]
    gamedata_count = hdr[3]
    off_gamedata   = hdr[4]
    files_count    = hdr[5]
    off_files      = hdr[6]

    def read_strings(count, offset):
        out = []
        for i in range(count):
            ptr = struct.unpack_from(f"{endian}I", raw, offset + i * 4)[0]
            end = raw.index(b"\x00", ptr)
            out.append(raw[ptr:end].decode("utf-8"))
        return out

    folders  = read_strings(folder_count, off_folders)
    gamedata = read_strings(gamedata_count, off_gamedata)

    files = []
    for i in range(files_count):
        base = off_files + i * 24
        folder_num, str_ptr, prefetch, scd_num, serial, size = struct.unpack_from(f"{endian}6I", raw, base)
        end = raw.index(b"\x00", str_ptr)
        fp = raw[str_ptr:end].decode("utf-8")
        files.append({
            "folder_number": folder_num,
            "file_path": fp,
            "prefetch": prefetch,
            "scd_number": scd_num,
            "serial_number inside the scd": serial,
            "file_size": size,
        })

    return platform, folders, gamedata, files


def compile_bdf(folders, gamedata, list_of_files, output_path: str, platform: str = "xbox"):
    """Build a .bdf from the three data lists.

    platform: "pc" (little-endian, MFDB) or "xbox" (big-endian, BDFM).
    File entries should use the key "prefetch" (legacy "unknown?" also accepted).
    """
    endian = "<" if platform == "pc" else ">"
    magic  = b"MFDB" if platform == "pc" else b"BDFM"

    fc = len(folders)
    gc = len(gamedata)
    lc = len(list_of_files)

    folder_off = []
    gd_off     = []
    file_ents  = []
    strings    = b""
    cur = 28 + fc * 4 + gc * 4 + lc * 24

    for s in folders:
        folder_off.append(cur)
        d = align(s.encode("utf-8") + b"\x00", 4)
        strings += d; cur += len(d)

    for s in gamedata:
        gd_off.append(cur)
        d = align(s.encode("utf-8") + b"\x00", 4)
        strings += d; cur += len(d)

    for e in list_of_files:
        fp_off = cur
        d = align(e["file_path"].encode("utf-8") + b"\x00", 4)
        strings += d; cur += len(d)
        # Accept both "prefetch" and legacy "unknown?" key
        pf = e.get("prefetch", e.get("unknown?", 0))
        file_ents.append(struct.pack(
            f"{endian}6I", e["folder_number"], fp_off, pf,
            e["scd_number"], e["serial_number inside the scd"], e["file_size"],
        ))

    raw = (
        struct.pack(f"{endian}7I", 1, fc, 28, gc, 28 + fc * 4, lc, 28 + fc * 4 + gc * 4)
        + b"".join(struct.pack(f"{endian}I", o) for o in folder_off)
        + b"".join(struct.pack(f"{endian}I", o) for o in gd_off)
        + b"".join(file_ents)
        + strings
    )

    HS = 28
    ot = []
    ot.extend(HS + i * 4 for i in range(fc));  ot.append(8)
    ot.extend(HS + fc * 4 + i * 4 for i in range(gc));  ot.append(16)
    ot.extend(HS + fc * 4 + gc * 4 + i * 24 + 4 for i in range(lc));  ot.append(24)

    ot_data    = align(b"".join(struct.pack(f"{endian}I", v) for v in ot), 32)
    compressed = zlib.compress(raw)

    with open(output_path, "wb") as f:
        f.write(struct.pack(f"{endian}4s7I", magic, 7, 2, 0,
                            len(compressed), len(raw), len(ot), 1))
        f.write(ot_data)
        f.write(align(compressed, 32))
    return output_path


def _normalize_file_entries(files):
    """Ensure all file entries use 'prefetch' key (handles legacy 'unknown?' key)."""
    for e in files:
        if "unknown?" in e and "prefetch" not in e:
            e["prefetch"] = e.pop("unknown?")
        elif "unknown?" in e and "prefetch" in e:
            e.pop("unknown?")
    return files


# ──────────────────────────────────────────────────────────────────────────────
#  Auto-build TOC from game folder
# ──────────────────────────────────────────────────────────────────────────────

# Directories that contain loose files (not inside any .scd)
LOOSE_DIRS = {"", "fonts", "gamedata", "movies", "sounds"}

# Prefetch heuristic: returns 1 or 0 based on file extension and folder
def _guess_prefetch(file_name: str, folder_path: str) -> int:
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    top_folder = folder_path.split("/")[0] if "/" in folder_path else folder_path

    if ext in ("scm", "usm", "scr"):
        return 1
    if ext == "dds":
        # Textures folder is mostly non-prefetch; other folders mostly prefetch
        return 0 if top_folder == "textures" else 1
    if ext in ("sca", "bdf"):
        return 1
    return 0


def scan_game_folder(game_dir: str, progress_callback=None, platform: str = "xbox"):
    """Scan a Supreme Commander 2 game directory and build TOC data.

    Looks for:
      • gamedata/*.scd  (ZIP archives containing game files)
      • Loose files in the root and known subdirectories

    Platform tags (.x360. / .win.) are stripped from filenames, and files
    for the wrong platform are skipped.  For example, when building an Xbox
    TOC, "file.x360.dds" becomes "file.dds" and "file.win.dds" is skipped.

    Returns (folders, gamedata, files) ready for the editor.
    progress_callback(message, percent) is called if provided.
    """
    # Platform tag to KEEP (strip from name) vs SKIP (ignore entirely)
    if platform == "xbox":
        keep_tag = ".x360."
        skip_tag = ".win."
    else:
        keep_tag = ".win."
        skip_tag = ".x360."
    gamedata_dir = None
    # Try common casing
    for name in ("gamedata", "Gamedata", "GameData", "GAMEDATA"):
        candidate = os.path.join(game_dir, name)
        if os.path.isdir(candidate):
            gamedata_dir = candidate
            gamedata_dirname = name
            break
    if gamedata_dir is None:
        raise FileNotFoundError(
            "No 'gamedata' folder found.\n\n"
            "Select the Supreme Commander 2 game directory\n"
            "(the folder containing the 'gamedata' subfolder).")

    # ── 1. Discover .scd files ────────────────────────────────────────────
    scd_files = sorted([
        f for f in os.listdir(gamedata_dir)
        if f.lower().endswith(".scd") and os.path.isfile(os.path.join(gamedata_dir, f))
    ])
    if not scd_files:
        raise FileNotFoundError(
            f"No .scd files found in:\n{gamedata_dir}\n\n"
            "Make sure this is the correct game directory.")

    # gamedata.txt uses backslash paths: "gamedata\bp.scd" (always lowercase)
    gamedata_list = [f"{gamedata_dirname.lower()}\\{f.lower()}" for f in scd_files]

    if progress_callback:
        progress_callback(f"Found {len(scd_files)} SCD archives", 5)

    # ── 2. Scan each .scd archive ─────────────────────────────────────────
    all_folders = {""}  # root folder (empty string) always exists
    file_entries = []

    for scd_idx, scd_name in enumerate(scd_files):
        scd_path = os.path.join(gamedata_dir, scd_name)
        if progress_callback:
            pct = 5 + int(80 * scd_idx / len(scd_files))
            progress_callback(f"Scanning {scd_name}…", pct)

        # Two-pass: collect entries, then deduplicate preferring platform-tagged
        scd_entries = {}  # key=(folder, name) → (entry_dict, is_platform_specific)

        try:
            with zipfile.ZipFile(scd_path, "r") as zf:
                # Enumerate ALL entries (dirs + files) to get correct serial numbers
                for serial, info in enumerate(zf.infolist()):
                    # Skip directory entries
                    if info.is_dir():
                        # But collect the directory path (lowercased for TOC)
                        dir_path = info.filename.rstrip("/").replace("\\", "/").lower()
                        all_folders.add(dir_path)
                        parts = dir_path.split("/")
                        for j in range(1, len(parts)):
                            all_folders.add("/".join(parts[:j]))
                        continue

                    # File entry — lowercase for TOC
                    full_path = info.filename.replace("\\", "/").lower()
                    if "/" in full_path:
                        folder_path = full_path.rsplit("/", 1)[0]
                        file_name = full_path.rsplit("/", 1)[1]
                    else:
                        folder_path = ""
                        file_name = full_path

                    if not file_name:
                        continue

                    # Platform tag handling
                    is_platform = False
                    if skip_tag in file_name:
                        # Wrong platform — skip entirely
                        continue
                    if keep_tag in file_name:
                        # Right platform — strip the tag
                        file_name = file_name.replace(keep_tag, ".")
                        is_platform = True

                    # Collect folder and all parents
                    all_folders.add(folder_path)
                    parts = folder_path.split("/")
                    for j in range(1, len(parts)):
                        all_folders.add("/".join(parts[:j]))

                    entry = {
                        "_folder_path": folder_path,
                        "file_path": file_name,
                        "prefetch": _guess_prefetch(file_name, folder_path),
                        "scd_number": scd_idx,
                        "serial_number inside the scd": serial,
                        "file_size": info.file_size,
                    }

                    key = (folder_path, file_name)
                    existing = scd_entries.get(key)
                    if existing is None:
                        scd_entries[key] = (entry, is_platform)
                    elif is_platform and not existing[1]:
                        # Platform-specific version overrides plain version
                        scd_entries[key] = (entry, is_platform)
                    # else: keep existing (first platform-specific, or first plain)

            file_entries.extend(entry for entry, _ in scd_entries.values())

        except (zipfile.BadZipFile, Exception) as ex:
            raise ValueError(f"Error reading {scd_name}:\n{ex}")

    # ── 3. Scan for loose files ───────────────────────────────────────────
    if progress_callback:
        progress_callback("Scanning loose files…", 88)

    for root, dirs, filenames in os.walk(game_dir):
        rel_root = os.path.relpath(root, game_dir).replace("\\", "/").lower()
        if rel_root == ".":
            rel_root = ""

        # Only scan known loose-file directories
        top = rel_root.split("/")[0] if rel_root else ""
        if top and top not in LOOSE_DIRS:
            continue

        # Skip inside the .scd files themselves (they're ZIPs, not dirs)
        for fname in filenames:
            full_disk_path = os.path.join(root, fname)
            if not os.path.isfile(full_disk_path):
                continue

            fname_lower = fname.lower()

            # Platform tag handling for loose files
            if skip_tag in fname_lower:
                continue  # Wrong platform
            if keep_tag in fname_lower:
                fname_lower = fname_lower.replace(keep_tag, ".")

            all_folders.add(rel_root)
            # Add parent directories
            if rel_root:
                parts = rel_root.split("/")
                for j in range(1, len(parts)):
                    all_folders.add("/".join(parts[:j]))

            file_size = os.path.getsize(full_disk_path)
            file_entries.append({
                "_folder_path": rel_root,
                "file_path": fname_lower,
                "prefetch": _guess_prefetch(fname_lower, rel_root),
                "scd_number": NO_SCD,
                "serial_number inside the scd": 0,
                "file_size": file_size,
            })

    # ── 4. Build sorted folder list and resolve folder numbers ────────────
    if progress_callback:
        progress_callback("Building folder index…", 93)

    sorted_folders = sorted(all_folders)
    folder_to_idx = {f: i for i, f in enumerate(sorted_folders)}

    for entry in file_entries:
        entry["folder_number"] = folder_to_idx[entry.pop("_folder_path")]

    # ── 5. Sort files by (folder_number, file_path) — REQUIRED ───────────
    # The game engine expects this exact ordering for file lookups.
    file_entries.sort(key=lambda e: (e["folder_number"], e["file_path"].lower()))

    if progress_callback:
        progress_callback("Done!", 100)

    return sorted_folders, gamedata_list, file_entries


# ──────────────────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────────────────

NO_SCD = 4294967295  # 0xFFFFFFFF  → "not inside any .scd"

DARK_BG       = "#1a1b26"
DARKER_BG     = "#13141c"
SURFACE       = "#24253a"
SURFACE_LIGHT = "#2f3050"
ACCENT        = "#7aa2f7"
ACCENT_DIM    = "#3d59a1"
TEXT          = "#c0caf5"
TEXT_DIM      = "#565f89"
GREEN         = "#9ece6a"
RED           = "#f7768e"
YELLOW        = "#e0af68"
BORDER        = "#3b3d57"


# ──────────────────────────────────────────────────────────────────────────────
#  Tooltip helper
# ──────────────────────────────────────────────────────────────────────────────

class ToolTip:
    """Hover tooltip for any widget."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, event=None):
        if self.tipwindow:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify="left",
                         background="#1e1e2e", foreground="#c0caf5",
                         relief="solid", borderwidth=1,
                         font=("Segoe UI", 9), padx=8, pady=4)
        label.pack()

    def hide(self, event=None):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


# ──────────────────────────────────────────────────────────────────────────────
#  Main application
# ──────────────────────────────────────────────────────────────────────────────

class BDFEditorApp(tk.Tk):
    def __init__(self, initial_file=None):
        super().__init__()
        self.title("BDF Editor — Supreme Commander 2 TOC  (PC & Xbox 360)")
        self.geometry("1280x780")
        self.minsize(960, 580)
        self.configure(bg=DARK_BG)

        # State
        self.folders: list = []
        self.gamedata: list = []
        self.files: list = []
        self.current_bdf_path = None
        self.platform = "xbox"   # "pc" or "xbox" — auto-detected on open
        self.unsaved = False

        self._build_styles()
        self._build_menu()
        self._build_toolbar()
        self._build_notebook()
        self._build_statusbar()
        self._bind_shortcuts()
        self._setup_drag_and_drop()

        # Auto-open if a file was passed on the command line / drag-and-drop
        if initial_file and os.path.isfile(initial_file):
            self.after(100, lambda: self._load_bdf(initial_file))

    # ── theming ───────────────────────────────────────────────────────────
    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".", background=DARK_BG, foreground=TEXT,
                     fieldbackground=SURFACE, borderwidth=0,
                     font=("Segoe UI", 10))
        s.configure("TNotebook", background=DARK_BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=SURFACE, foreground=TEXT_DIM,
                     padding=[16, 7], font=("Segoe UI", 10, "bold"))
        s.map("TNotebook.Tab",
              background=[("selected", ACCENT_DIM)],
              foreground=[("selected", "#ffffff")])
        s.configure("Treeview", background=SURFACE, foreground=TEXT,
                     fieldbackground=SURFACE, rowheight=24,
                     font=("Consolas", 10))
        s.configure("Treeview.Heading", background=SURFACE_LIGHT,
                     foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        s.map("Treeview",
              background=[("selected", ACCENT_DIM)],
              foreground=[("selected", "#ffffff")])
        s.configure("TButton", background=SURFACE_LIGHT, foreground=TEXT,
                     padding=[10, 4], font=("Segoe UI", 10))
        s.map("TButton", background=[("active", ACCENT_DIM)])
        s.configure("Accent.TButton", background=ACCENT_DIM,
                     foreground="#ffffff", padding=[12, 5],
                     font=("Segoe UI", 10, "bold"))
        s.map("Accent.TButton", background=[("active", ACCENT)])
        s.configure("TLabel", background=DARK_BG, foreground=TEXT)
        s.configure("TFrame", background=DARK_BG)
        s.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT)
        s.configure("Status.TLabel", background=DARKER_BG, foreground=TEXT_DIM,
                     font=("Segoe UI", 9))
        s.configure("Vertical.TScrollbar", background=SURFACE_LIGHT,
                     troughcolor=SURFACE, arrowcolor=TEXT_DIM, borderwidth=0)

    # ── menu ──────────────────────────────────────────────────────────────
    def _build_menu(self):
        mb = tk.Menu(self, bg=SURFACE, fg=TEXT, activebackground=ACCENT_DIM,
                     activeforeground="#fff", relief="flat", bd=0)

        fm = tk.Menu(mb, tearoff=0, bg=SURFACE, fg=TEXT,
                     activebackground=ACCENT_DIM, activeforeground="#fff")
        fm.add_command(label="Open BDF…",            accelerator="Ctrl+O",  command=self.open_bdf)
        fm.add_command(label="Import text files…",                           command=self.import_text_files)
        fm.add_separator()
        fm.add_command(label="Build from Game Folder…", accelerator="Ctrl+B", command=self.build_from_folder)
        fm.add_separator()
        fm.add_command(label="Save / Compile BDF…",  accelerator="Ctrl+S",  command=self.save_bdf)
        fm.add_command(label="Export text files…",                           command=self.export_text_files)
        fm.add_separator()
        fm.add_command(label="Compare with Reference BDF…",                  command=self.compare_with_reference)
        fm.add_separator()
        fm.add_command(label="Exit",                                         command=self.quit)
        mb.add_cascade(label="File", menu=fm)

        em = tk.Menu(mb, tearoff=0, bg=SURFACE, fg=TEXT,
                     activebackground=ACCENT_DIM, activeforeground="#fff")
        em.add_command(label="Add entry…",         accelerator="Ctrl+N",  command=self.add_entry)
        em.add_command(label="Edit selected…",     accelerator="Enter",   command=self.edit_selected)
        em.add_command(label="Duplicate selected", accelerator="Ctrl+D",  command=self.duplicate_selected)
        em.add_command(label="Delete selected",    accelerator="Del",     command=self.delete_selected)
        em.add_separator()
        em.add_command(label="Find…",              accelerator="Ctrl+F",  command=self.find_dialog)
        mb.add_cascade(label="Edit", menu=em)

        hm = tk.Menu(mb, tearoff=0, bg=SURFACE, fg=TEXT,
                     activebackground=ACCENT_DIM, activeforeground="#fff")
        hm.add_command(label="Field reference…", command=self._show_help)
        mb.add_cascade(label="Help", menu=hm)

        self.config(menu=mb)

    # ── toolbar ───────────────────────────────────────────────────────────
    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(8, 0))

        b1 = ttk.Button(bar, text="Open BDF",  command=self.open_bdf)
        b1.pack(side="left", padx=2)
        ToolTip(b1, "Open and decompile a .bdf file  (Ctrl+O)")

        b_build = ttk.Button(bar, text="Build from Folder", command=self.build_from_folder)
        b_build.pack(side="left", padx=2)
        ToolTip(b_build, "Scan a game folder to auto-build the TOC  (Ctrl+B)\n"
                         "Reads all .scd archives + loose files automatically")

        b2 = ttk.Button(bar, text="Compile BDF", style="Accent.TButton", command=self.save_bdf)
        b2.pack(side="left", padx=2)
        ToolTip(b2, "Compile current data into a new .bdf  (Ctrl+S)")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)

        # Platform selector
        ttk.Label(bar, text="Platform:").pack(side="left", padx=(0, 2))
        self.platform_var = tk.StringVar(value="Xbox 360")
        plat_combo = ttk.Combobox(bar, textvariable=self.platform_var,
                                   values=["PC  (toc.win.bdf)", "Xbox 360  (toc.x360.bdf)"],
                                   width=22, state="readonly")
        plat_combo.pack(side="left", padx=2)
        plat_combo.bind("<<ComboboxSelected>>", self._on_platform_changed)
        self.plat_combo = plat_combo
        ToolTip(plat_combo, "PC = little-endian (MFDB)\nXbox 360 = big-endian (BDFM)\nAuto-detected when opening a .bdf")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)

        b3 = ttk.Button(bar, text="Add",    command=self.add_entry)
        b3.pack(side="left", padx=2)
        ToolTip(b3, "Add a new entry to the current tab  (Ctrl+N)")

        b4 = ttk.Button(bar, text="Edit",   command=self.edit_selected)
        b4.pack(side="left", padx=2)
        ToolTip(b4, "Edit the selected entry  (Enter / Double-click)")

        b5 = ttk.Button(bar, text="Duplicate", command=self.duplicate_selected)
        b5.pack(side="left", padx=2)
        ToolTip(b5, "Duplicate selected entries  (Ctrl+D)")

        b6 = ttk.Button(bar, text="Delete", command=self.delete_selected)
        b6.pack(side="left", padx=2)
        ToolTip(b6, "Delete selected entries  (Del)")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)

        b7 = ttk.Button(bar, text="Find", command=self.find_dialog)
        b7.pack(side="left", padx=2)

        # Live-filter on the right
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._live_filter())
        se = ttk.Entry(bar, textvariable=self.search_var, width=30)
        se.pack(side="right", padx=2)
        ttk.Label(bar, text="Filter:").pack(side="right")

    # ── notebook / tabs ───────────────────────────────────────────────────
    def _build_notebook(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        # ─── Folders tab ───
        f_frame = ttk.Frame(self.notebook)
        self.notebook.add(f_frame, text="  Folders  ")
        self.folders_tree = self._make_tree(f_frame,
            columns=("#", "path"),
            headings=("# (folder_number)", "Folder Path"),
            widths=(130, 600))

        # ─── Gamedata tab ───
        g_frame = ttk.Frame(self.notebook)
        self.notebook.add(g_frame, text="  Gamedata  ")
        self.gamedata_tree = self._make_tree(g_frame,
            columns=("#", "path"),
            headings=("# (scd_number)", "Gamedata Path"),
            widths=(130, 600))

        # ─── Files tab ───
        files_frame = ttk.Frame(self.notebook)
        self.notebook.add(files_frame, text="  Files  ")
        self.files_tree = self._make_tree(files_frame,
            columns=("folder_num", "folder_path", "file_path",
                     "prefetch", "scd_num", "scd_path",
                     "serial", "file_size"),
            headings=("Folder #", "Folder Path", "File Path",
                      "Prefetch", "SCD #", "SCD (gamedata)",
                      "Serial #", "File Size"),
            widths=(65, 200, 260, 65, 55, 180, 65, 85))

        self.files_info = ttk.Label(files_frame, text="", style="Status.TLabel")
        self.files_info.pack(fill="x")

        self.notebook.bind("<<NotebookTabChanged>>", lambda _: self._update_status())

    def _make_tree(self, parent, columns, headings, widths):
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)

        tree = ttk.Treeview(container, columns=columns, show="headings",
                            selectmode="extended")
        vsb = ttk.Scrollbar(container, orient="vertical",   command=tree.yview)
        hsb = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        for col, hd, w in zip(columns, headings, widths):
            tree.heading(col, text=hd, anchor="w",
                         command=lambda c=col: self._sort_tree(tree, c, False))
            tree.column(col, width=w, minwidth=40, anchor="w")

        tree.tag_configure("even", background=SURFACE)
        tree.tag_configure("odd",  background=SURFACE_LIGHT)
        tree.tag_configure("no_scd", foreground=TEXT_DIM)

        tree.bind("<Double-1>", lambda _: self.edit_selected())
        return tree

    # ── status bar ────────────────────────────────────────────────────────
    def _build_statusbar(self):
        self.statusbar = ttk.Label(
            self,
            text="  No file loaded  —  drag & drop a .bdf onto this window, or use File > Open",
            style="Status.TLabel", anchor="w", padding=(8, 4))
        self.statusbar.pack(fill="x", side="bottom")

    # ── keyboard shortcuts ────────────────────────────────────────────────
    def _bind_shortcuts(self):
        self.bind_all("<Control-o>", lambda _: self.open_bdf())
        self.bind_all("<Control-b>", lambda _: self.build_from_folder())
        self.bind_all("<Control-s>", lambda _: self.save_bdf())
        self.bind_all("<Control-n>", lambda _: self.add_entry())
        self.bind_all("<Control-d>", lambda _: self.duplicate_selected())
        self.bind_all("<Control-f>", lambda _: self.find_dialog())
        self.bind_all("<Delete>",    lambda _: self.delete_selected())
        self.bind_all("<Return>",    lambda _: self.edit_selected())

    # ── drag and drop ─────────────────────────────────────────────────────
    def _setup_drag_and_drop(self):
        """Try tkinterdnd2 for native drag-and-drop; skip silently if absent.
        Command-line drag-and-drop (onto .exe) always works via sys.argv."""
        try:
            from tkinterdnd2 import DND_FILES
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event):
        path = event.data.strip().strip("{}")
        if path.lower().endswith(".bdf"):
            self._load_bdf(path)

    # ── platform selector ─────────────────────────────────────────────────
    def _on_platform_changed(self, event=None):
        sel = self.platform_var.get()
        self.platform = "pc" if sel.startswith("PC") else "xbox"
        if self.files:
            self.unsaved = True
        self._update_status()

    def _sync_platform_combo(self):
        """Update the combo box to reflect the current self.platform value."""
        if self.platform == "pc":
            self.platform_var.set("PC  (toc.win.bdf)")
        else:
            self.platform_var.set("Xbox 360  (toc.x360.bdf)")

    # ──────────────────────────────────────────────────────────────────────
    #  Helpers to resolve numbers ↔ names
    # ──────────────────────────────────────────────────────────────────────

    def _folder_name(self, idx):
        if 0 <= idx < len(self.folders):
            return self.folders[idx] if self.folders[idx] else "(root)"
        return f"?{idx}"

    def _gamedata_name(self, idx):
        if idx == NO_SCD:
            return "(none)"
        if 0 <= idx < len(self.gamedata):
            return self.gamedata[idx]
        return f"?{idx}"

    # ──────────────────────────────────────────────────────────────────────
    #  File I/O
    # ──────────────────────────────────────────────────────────────────────

    def open_bdf(self):
        path = filedialog.askopenfilename(
            title="Open BDF file",
            filetypes=[("BDF files", "*.bdf"), ("All files", "*.*")])
        if path:
            self._load_bdf(path)

    def _load_bdf(self, path):
        try:
            self.platform, self.folders, self.gamedata, self.files = decompile_bdf(path)
            self.current_bdf_path = path
            self.unsaved = False
            self._sync_platform_combo()
            self._refresh_all()
            plat_label = "PC" if self.platform == "pc" else "Xbox 360"
            self._set_status(f"Opened: {os.path.basename(path)}  [{plat_label}]  —  "
                             f"{len(self.folders)} folders · {len(self.gamedata)} gamedata · "
                             f"{len(self.files)} files")
        except Exception as ex:
            messagebox.showerror("Error opening BDF", str(ex))

    def build_from_folder(self):
        """Scan a game directory to auto-build the TOC."""
        game_dir = filedialog.askdirectory(
            title="Select Supreme Commander 2 game folder  "
                  "(the one containing the 'gamedata' subfolder)")
        if not game_dir:
            return

        # Progress dialog
        dlg = tk.Toplevel(self)
        dlg.title("Building TOC…")
        dlg.configure(bg=DARK_BG)
        dlg.geometry("460x140")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="Scanning game folder…",
                  font=("Segoe UI", 11, "bold")).pack(padx=20, pady=(20, 5))
        status_var = tk.StringVar(value="Starting…")
        status_label = ttk.Label(dlg, textvariable=status_var, foreground=TEXT_DIM)
        status_label.pack(padx=20)
        progress = ttk.Progressbar(dlg, length=400, mode="determinate")
        progress.pack(padx=20, pady=(10, 20))

        def update_progress(msg, pct):
            status_var.set(msg)
            progress["value"] = pct
            dlg.update_idletasks()

        try:
            folders, gamedata, files = scan_game_folder(game_dir, update_progress,
                                                         platform=self.platform)
            dlg.destroy()

            self.folders = folders
            self.gamedata = gamedata
            self.files = files
            self.current_bdf_path = None
            self.unsaved = True

            # Auto-detect platform from folder contents
            # If we find x360 in SCD names or folder names, assume Xbox
            has_x360 = any("x360" in f.lower() or "360" in f.lower()
                           for f in os.listdir(game_dir)
                           if os.path.isfile(os.path.join(game_dir, f)))
            self.platform = "xbox" if has_x360 else "pc"
            self._sync_platform_combo()

            self._refresh_all()
            self._set_status(
                f"Built from: {os.path.basename(game_dir)}  —  "
                f"{len(self.folders)} folders · {len(self.gamedata)} gamedata · "
                f"{len(self.files)} files")
            messagebox.showinfo(
                "Build complete",
                f"TOC built from game folder:\n{game_dir}\n\n"
                f"  Folders:    {len(self.folders)}\n"
                f"  Gamedata:  {len(self.gamedata)} SCD archives\n"
                f"  Files:        {len(self.files)}\n\n"
                f"Prefetch flags were set by heuristic.\n"
                f"Review and edit as needed, then Compile BDF to save.")

        except Exception as ex:
            dlg.destroy()
            messagebox.showerror("Build error", str(ex))

    def save_bdf(self):
        if not self.folders and not self.files:
            messagebox.showwarning("Nothing to save", "No data loaded.")
            return
        default_name = "toc.win.bdf" if self.platform == "pc" else "toc.x360.bdf"
        path = filedialog.asksaveasfilename(
            title="Compile & save BDF",
            defaultextension=".bdf",
            initialfile=default_name,
            filetypes=[("BDF files", "*.bdf"), ("All files", "*.*")])
        if not path:
            return
        try:
            compile_bdf(self.folders, self.gamedata, self.files, path, platform=self.platform)
            self.current_bdf_path = path
            self.unsaved = False
            plat_label = "PC" if self.platform == "pc" else "Xbox 360"
            self._set_status(f"Compiled [{plat_label}] -> {os.path.basename(path)}")
            messagebox.showinfo("Saved", f"BDF file compiled to:\n{path}\n\nPlatform: {plat_label}")
        except Exception as ex:
            messagebox.showerror("Error saving BDF", str(ex))

    def import_text_files(self):
        folder = filedialog.askdirectory(
            title="Select folder with folders.txt, gamedata.txt, list_of_files.json")
        if not folder:
            return
        try:
            fp = os.path.join(folder, "folders.txt")
            gp = os.path.join(folder, "gamedata.txt")
            lp = os.path.join(folder, "list_of_files.json")
            missing = [n for n, p in [("folders.txt", fp), ("gamedata.txt", gp),
                                       ("list_of_files.json", lp)] if not os.path.isfile(p)]
            if missing:
                messagebox.showerror("Missing files", "Not found:\n" + "\n".join(missing))
                return
            with open(fp, "r", encoding="utf-8") as f:
                self.folders = [l.rstrip("\r\n") for l in f.readlines()]
            with open(gp, "r", encoding="utf-8") as f:
                self.gamedata = [l.strip() for l in f if l.strip()]
            with open(lp, "r", encoding="utf-8") as f:
                self.files = _normalize_file_entries(json.load(f))
            self.unsaved = True
            self._refresh_all()
            self._set_status(f"Imported from: {folder}")
        except Exception as ex:
            messagebox.showerror("Import error", str(ex))

    def export_text_files(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if not folder:
            return
        try:
            with open(os.path.join(folder, "folders.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(self.folders))
            with open(os.path.join(folder, "gamedata.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(self.gamedata))
            with open(os.path.join(folder, "list_of_files.json"), "w", encoding="utf-8") as f:
                json.dump(self.files, f, indent=4)
            self._set_status(f"Exported -> {folder}")
            messagebox.showinfo("Exported", f"Text files saved to:\n{folder}")
        except Exception as ex:
            messagebox.showerror("Export error", str(ex))

    def compare_with_reference(self):
        """Compare current data with a known-good reference BDF and show a diff report."""
        if not self.files:
            messagebox.showwarning("No data", "Load or build data first, then compare.")
            return

        path = filedialog.askopenfilename(
            title="Select the WORKING reference BDF to compare against",
            filetypes=[("BDF files", "*.bdf"), ("All files", "*.*")])
        if not path:
            return

        try:
            _, ref_folders, ref_gamedata, ref_files = decompile_bdf(path)
        except Exception as ex:
            messagebox.showerror("Error", f"Could not read reference BDF:\n{ex}")
            return

        # Normalize ref files key
        _normalize_file_entries(ref_files)

        # Build lookup sets using (folder_path, file_name) as key
        def make_key(entry, folder_list):
            fn = entry["folder_number"]
            folder = folder_list[fn] if 0 <= fn < len(folder_list) else f"?{fn}"
            return (folder, entry["file_path"])

        def make_detail(entry, folder_list, gd_list):
            fn = entry["folder_number"]
            folder = folder_list[fn] if 0 <= fn < len(folder_list) else f"?{fn}"
            scd = entry["scd_number"]
            scd_name = gd_list[scd] if scd != NO_SCD and 0 <= scd < len(gd_list) else ("(none)" if scd == NO_SCD else f"?{scd}")
            pf = entry.get("prefetch", entry.get("unknown?", 0))
            return (folder, entry["file_path"], pf, scd_name,
                    entry["serial_number inside the scd"], entry["file_size"])

        cur_keys = {}
        for e in self.files:
            k = make_key(e, self.folders)
            cur_keys[k] = make_detail(e, self.folders, self.gamedata)

        ref_keys = {}
        for e in ref_files:
            k = make_key(e, ref_folders)
            ref_keys[k] = make_detail(e, ref_folders, ref_gamedata)

        cur_set = set(cur_keys.keys())
        ref_set = set(ref_keys.keys())

        only_in_current = sorted(cur_set - ref_set)
        only_in_ref     = sorted(ref_set - cur_set)
        in_both         = sorted(cur_set & ref_set)

        # Check metadata differences for files in both
        metadata_diffs = []
        for k in in_both:
            c = cur_keys[k]
            r = ref_keys[k]
            diffs = []
            if c[2] != r[2]:  # prefetch
                diffs.append(f"prefetch: {c[2]} vs {r[2]}")
            if c[3] != r[3]:  # scd
                diffs.append(f"scd: {c[3]} vs {r[3]}")
            if c[4] != r[4]:  # serial
                diffs.append(f"serial: {c[4]} vs {r[4]}")
            if c[5] != r[5]:  # size
                diffs.append(f"size: {c[5]} vs {r[5]}")
            if diffs:
                metadata_diffs.append((k, diffs))

        # Folder diffs
        cur_folder_set = set(self.folders)
        ref_folder_set = set(ref_folders)
        folders_only_cur = sorted(cur_folder_set - ref_folder_set)
        folders_only_ref = sorted(ref_folder_set - cur_folder_set)

        # Gamedata diffs
        cur_gd_set = set(self.gamedata)
        ref_gd_set = set(ref_gamedata)
        gd_only_cur = sorted(cur_gd_set - ref_gd_set)
        gd_only_ref = sorted(ref_gd_set - cur_gd_set)

        # Build report
        lines = []
        lines.append("=" * 70)
        lines.append("  BDF COMPARISON REPORT")
        lines.append("=" * 70)
        lines.append(f"Reference:  {os.path.basename(path)}")
        lines.append(f"Current:    {'(auto-built)' if not self.current_bdf_path else os.path.basename(self.current_bdf_path)}")
        lines.append("")
        lines.append(f"{'':30s} {'CURRENT':>10s}  {'REFERENCE':>10s}  {'DIFF':>10s}")
        lines.append(f"{'Folders':30s} {len(self.folders):10d}  {len(ref_folders):10d}  {len(self.folders)-len(ref_folders):+10d}")
        lines.append(f"{'Gamedata entries':30s} {len(self.gamedata):10d}  {len(ref_gamedata):10d}  {len(self.gamedata)-len(ref_gamedata):+10d}")
        lines.append(f"{'File entries':30s} {len(self.files):10d}  {len(ref_files):10d}  {len(self.files)-len(ref_files):+10d}")
        lines.append("")
        lines.append(f"Files only in CURRENT:     {len(only_in_current)}")
        lines.append(f"Files only in REFERENCE:   {len(only_in_ref)}")
        lines.append(f"Files in both:             {len(in_both)}")
        lines.append(f"  with metadata diffs:     {len(metadata_diffs)}")

        if gd_only_cur or gd_only_ref:
            lines.append("")
            lines.append("-" * 70)
            lines.append("GAMEDATA DIFFERENCES")
            lines.append("-" * 70)
            if gd_only_cur:
                lines.append(f"\n  Only in CURRENT ({len(gd_only_cur)}):")
                for g in gd_only_cur:
                    lines.append(f"    + {g}")
            if gd_only_ref:
                lines.append(f"\n  Only in REFERENCE ({len(gd_only_ref)}):")
                for g in gd_only_ref:
                    lines.append(f"    - {g}")

        if folders_only_cur or folders_only_ref:
            lines.append("")
            lines.append("-" * 70)
            lines.append(f"FOLDER DIFFERENCES  (current has {len(folders_only_cur)} extra, reference has {len(folders_only_ref)} extra)")
            lines.append("-" * 70)
            if folders_only_cur:
                lines.append(f"\n  Only in CURRENT ({len(folders_only_cur)}):")
                for f in folders_only_cur[:100]:
                    lines.append(f"    + '{f}'")
                if len(folders_only_cur) > 100:
                    lines.append(f"    ... and {len(folders_only_cur)-100} more")
            if folders_only_ref:
                lines.append(f"\n  Only in REFERENCE ({len(folders_only_ref)}):")
                for f in folders_only_ref[:100]:
                    lines.append(f"    - '{f}'")
                if len(folders_only_ref) > 100:
                    lines.append(f"    ... and {len(folders_only_ref)-100} more")

        if only_in_current:
            lines.append("")
            lines.append("-" * 70)
            lines.append(f"FILES ONLY IN CURRENT  ({len(only_in_current)} — these may be EXTRA)")
            lines.append("-" * 70)
            for folder, fname in only_in_current[:200]:
                full = f"{folder}/{fname}" if folder else fname
                detail = cur_keys[(folder, fname)]
                lines.append(f"  + {full:60s}  scd={detail[3]}")
            if len(only_in_current) > 200:
                lines.append(f"  ... and {len(only_in_current)-200} more")

        if only_in_ref:
            lines.append("")
            lines.append("-" * 70)
            lines.append(f"FILES ONLY IN REFERENCE  ({len(only_in_ref)} — these are MISSING)")
            lines.append("-" * 70)
            for folder, fname in only_in_ref[:200]:
                full = f"{folder}/{fname}" if folder else fname
                detail = ref_keys[(folder, fname)]
                lines.append(f"  - {full:60s}  scd={detail[3]}")
            if len(only_in_ref) > 200:
                lines.append(f"  ... and {len(only_in_ref)-200} more")

        if metadata_diffs:
            lines.append("")
            lines.append("-" * 70)
            lines.append(f"METADATA DIFFERENCES  ({len(metadata_diffs)} files differ)")
            lines.append("-" * 70)
            for (folder, fname), diffs in metadata_diffs[:200]:
                full = f"{folder}/{fname}" if folder else fname
                lines.append(f"  {full}")
                for d in diffs:
                    lines.append(f"      {d}")
            if len(metadata_diffs) > 200:
                lines.append(f"  ... and {len(metadata_diffs)-200} more")

        report = "\n".join(lines)

        # Show in a scrollable window
        dlg = tk.Toplevel(self)
        dlg.title("Comparison Report")
        dlg.configure(bg=DARK_BG)
        dlg.geometry("900x650")
        dlg.transient(self)

        text = tk.Text(dlg, bg=SURFACE, fg=TEXT, font=("Consolas", 10),
                       wrap="none", padx=10, pady=10, insertbackground=TEXT)
        vsb = ttk.Scrollbar(dlg, orient="vertical", command=text.yview)
        hsb = ttk.Scrollbar(dlg, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        dlg.rowconfigure(0, weight=1)
        dlg.columnconfigure(0, weight=1)

        text.insert("1.0", report)
        text.configure(state="disabled")

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=8)

        def save_report():
            p = filedialog.asksaveasfilename(title="Save Report",
                defaultextension=".txt", initialfile="bdf_comparison.txt",
                filetypes=[("Text files", "*.txt")])
            if p:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(report)
                messagebox.showinfo("Saved", f"Report saved to:\n{p}")

        ttk.Button(btn_frame, text="Save Report…", command=save_report).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close", command=dlg.destroy).pack(side="left", padx=4)

    # ──────────────────────────────────────────────────────────────────────
    #  Editing actions
    # ──────────────────────────────────────────────────────────────────────

    def _current_tab(self):
        return ("folders", "gamedata", "files")[self.notebook.index(self.notebook.select())]

    def _current_tree(self):
        return {"folders": self.folders_tree, "gamedata": self.gamedata_tree,
                "files": self.files_tree}[self._current_tab()]

    # ── add ───────────────────────────────────────────────────────────────
    def add_entry(self):
        tab = self._current_tab()
        if tab in ("folders", "gamedata"):
            val = simpledialog.askstring("Add entry",
                                         f"New {tab} path:", parent=self)
            if val is None:
                return
            lst = self.folders if tab == "folders" else self.gamedata
            lst.append(val.strip())
            self.unsaved = True
            self._refresh_tab(tab)
        else:
            self._file_entry_dialog()

    # ── edit ──────────────────────────────────────────────────────────────
    def edit_selected(self):
        tab = self._current_tab()
        tree = self._current_tree()
        sel = tree.selection()
        if not sel:
            return
        item = sel[0]
        vals = tree.item(item, "values")

        if tab in ("folders", "gamedata"):
            idx = int(vals[0])  # the "#" column
            lst = self.folders if tab == "folders" else self.gamedata
            val = simpledialog.askstring("Edit entry", "Edit path:",
                                         initialvalue=lst[idx], parent=self)
            if val is None:
                return
            lst[idx] = val.strip()
            self.unsaved = True
            self._refresh_tab(tab)
        else:
            tags = tree.item(item, "tags")
            real_idx = int(tags[1])  # tag[1] = original data index
            self._file_entry_dialog(real_idx)

    # ── duplicate ─────────────────────────────────────────────────────────
    def duplicate_selected(self):
        tab = self._current_tab()
        tree = self._current_tree()
        sel = tree.selection()
        if not sel:
            return
        lst = {"folders": self.folders, "gamedata": self.gamedata, "files": self.files}[tab]
        for item in sel:
            if tab in ("folders", "gamedata"):
                idx = int(tree.item(item, "values")[0])
                lst.append(lst[idx])
            else:
                idx = int(tree.item(item, "tags")[1])
                lst.append(dict(lst[idx]))
        self.unsaved = True
        self._refresh_tab(tab)

    # ── delete ────────────────────────────────────────────────────────────
    def delete_selected(self):
        tab = self._current_tab()
        tree = self._current_tree()
        sel = tree.selection()
        if not sel:
            return
        if not messagebox.askyesno("Confirm", f"Delete {len(sel)} selected entries?"):
            return
        lst = {"folders": self.folders, "gamedata": self.gamedata, "files": self.files}[tab]
        if tab in ("folders", "gamedata"):
            indices = sorted([int(tree.item(s, "values")[0]) for s in sel], reverse=True)
        else:
            indices = sorted([int(tree.item(s, "tags")[1]) for s in sel], reverse=True)
        for i in indices:
            del lst[i]
        self.unsaved = True
        self._refresh_tab(tab)

    # ──────────────────────────────────────────────────────────────────────
    #  File entry dialog  (the main editing form)
    # ──────────────────────────────────────────────────────────────────────

    def _file_entry_dialog(self, edit_index=None):
        editing = edit_index is not None
        dlg = tk.Toplevel(self)
        dlg.title("Edit File Entry" if editing else "Add File Entry")
        dlg.configure(bg=DARK_BG)
        dlg.geometry("620x500")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        defaults = self.files[edit_index] if editing else {
            "folder_number": 0, "file_path": "", "prefetch": 0,
            "scd_number": NO_SCD, "serial_number inside the scd": 0, "file_size": 0,
        }

        row = 0

        # ── Folder (dropdown showing resolved path) ──────────────────────
        ttk.Label(dlg, text="Folder:", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, padx=12, pady=(16, 2), sticky="ne")

        folder_choices = []
        for i, f in enumerate(self.folders):
            display = f if f else "(root)"
            folder_choices.append(f"{i}:  {display}")
        folder_var = tk.StringVar()
        sel_idx = defaults["folder_number"]
        if 0 <= sel_idx < len(folder_choices):
            folder_var.set(folder_choices[sel_idx])
        else:
            folder_var.set(f"{sel_idx}:  (unknown)")

        folder_combo = ttk.Combobox(dlg, textvariable=folder_var,
                                     values=folder_choices, width=58, state="readonly")
        folder_combo.grid(row=row, column=1, padx=12, pady=(16, 2), sticky="w")

        ttk.Label(dlg, text="Line # in folders.txt  —  the dropdown shows the resolved path",
                  foreground=TEXT_DIM, font=("Segoe UI", 8)).grid(
            row=row + 1, column=1, padx=12, sticky="w")
        row += 2

        # ── File path ─────────────────────────────────────────────────────
        ttk.Label(dlg, text="File Path:", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, padx=12, pady=(10, 2), sticky="ne")
        filepath_var = tk.StringVar(value=defaults["file_path"])
        ttk.Entry(dlg, textvariable=filepath_var, width=60).grid(
            row=row, column=1, padx=12, pady=(10, 2), sticky="w")
        ttk.Label(dlg, text="Name of the file",
                  foreground=TEXT_DIM, font=("Segoe UI", 8)).grid(
            row=row + 1, column=1, padx=12, sticky="w")
        row += 2

        # ── Prefetch Textures (checkbox) ──────────────────────────────────
        ttk.Label(dlg, text="Prefetch Textures:", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, padx=12, pady=(10, 2), sticky="ne")
        prefetch_var = tk.IntVar(value=defaults["prefetch"])
        pf_frame = ttk.Frame(dlg)
        pf_frame.grid(row=row, column=1, padx=12, pady=(10, 2), sticky="w")
        cb = tk.Checkbutton(pf_frame, variable=prefetch_var, text=" Yes",
                            bg=DARK_BG, fg=TEXT, selectcolor=SURFACE,
                            activebackground=DARK_BG, activeforeground=TEXT,
                            font=("Segoe UI", 10))
        cb.pack(side="left")
        tk.Label(pf_frame, text="  (1=true, 0=false)",
                 bg=DARK_BG, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(side="left")
        row += 1

        # ── SCD Number (dropdown showing resolved gamedata path) ──────────
        ttk.Label(dlg, text="SCD (gamedata):", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, padx=12, pady=(10, 2), sticky="ne")

        scd_choices = [f"{NO_SCD}:  (none — not in any SCD)"]
        for i, g in enumerate(self.gamedata):
            scd_choices.append(f"{i}:  {g}")
        scd_var = tk.StringVar()
        scd_idx = defaults["scd_number"]
        if scd_idx == NO_SCD:
            scd_var.set(scd_choices[0])
        elif 0 <= scd_idx < len(self.gamedata):
            scd_var.set(scd_choices[scd_idx + 1])
        else:
            scd_var.set(f"{scd_idx}:  (unknown)")

        scd_combo = ttk.Combobox(dlg, textvariable=scd_var,
                                  values=scd_choices, width=58, state="readonly")
        scd_combo.grid(row=row, column=1, padx=12, pady=(10, 2), sticky="w")

        ttk.Label(dlg, text="Line # in gamedata.txt  —  use 4294967295 for files not inside any .scd",
                  foreground=TEXT_DIM, font=("Segoe UI", 8)).grid(
            row=row + 1, column=1, padx=12, sticky="w")
        row += 2

        # ── Serial # inside SCD ──────────────────────────────────────────
        ttk.Label(dlg, text="Serial # in SCD:", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, padx=12, pady=(10, 2), sticky="ne")
        serial_var = tk.StringVar(value=str(defaults["serial_number inside the scd"]))
        ttk.Entry(dlg, textvariable=serial_var, width=20).grid(
            row=row, column=1, padx=12, pady=(10, 2), sticky="w")
        ttk.Label(dlg, text="0-based position inside the .scd  (WinRAR → Tools → Generate Report to count)",
                  foreground=TEXT_DIM, font=("Segoe UI", 8)).grid(
            row=row + 1, column=1, padx=12, sticky="w")
        row += 2

        # ── File Size ─────────────────────────────────────────────────────
        ttk.Label(dlg, text="File Size:", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, padx=12, pady=(10, 2), sticky="ne")
        size_var = tk.StringVar(value=str(defaults["file_size"]))
        ttk.Entry(dlg, textvariable=size_var, width=20).grid(
            row=row, column=1, padx=12, pady=(10, 2), sticky="w")
        ttk.Label(dlg, text="Size in bytes  (right-click file → Properties)",
                  foreground=TEXT_DIM, font=("Segoe UI", 8)).grid(
            row=row + 1, column=1, padx=12, sticky="w")
        row += 2

        # ── OK / Cancel ──────────────────────────────────────────────────
        def on_ok():
            # Parse folder #
            try:
                f_num = int(folder_var.get().split(":")[0].strip())
            except (ValueError, IndexError):
                messagebox.showerror("Invalid", "Select a valid folder.", parent=dlg)
                return
            # Parse SCD #
            try:
                s_num = int(scd_var.get().split(":")[0].strip())
            except (ValueError, IndexError):
                messagebox.showerror("Invalid", "Select a valid SCD.", parent=dlg)
                return

            fp = filepath_var.get().strip()
            if not fp:
                messagebox.showerror("Invalid", "File path cannot be empty.", parent=dlg)
                return
            try:
                serial = int(serial_var.get())
                fsize  = int(size_var.get())
            except ValueError:
                messagebox.showerror("Invalid",
                                     "Serial # and File Size must be integers.", parent=dlg)
                return

            entry = {
                "folder_number": f_num,
                "file_path": fp,
                "prefetch": prefetch_var.get(),
                "scd_number": s_num,
                "serial_number inside the scd": serial,
                "file_size": fsize,
            }
            if editing:
                self.files[edit_index] = entry
            else:
                self.files.append(entry)
            self.unsaved = True
            self._refresh_tab("files")
            dlg.destroy()

        bf = ttk.Frame(dlg)
        bf.grid(row=row, column=0, columnspan=2, pady=18)
        ttk.Button(bf, text="    OK    ", style="Accent.TButton",
                   command=on_ok).pack(side="left", padx=8)
        ttk.Button(bf, text="  Cancel  ", command=dlg.destroy).pack(side="left", padx=8)

        dlg.bind("<Return>", lambda _: on_ok())
        dlg.bind("<Escape>", lambda _: dlg.destroy())

    # ── find dialog ───────────────────────────────────────────────────────
    def find_dialog(self):
        q = simpledialog.askstring("Find", "Search for:", parent=self)
        if q is not None:
            self.search_var.set(q)

    def _live_filter(self):
        self._refresh_tab(self._current_tab())

    # ──────────────────────────────────────────────────────────────────────
    #  Tree population
    # ──────────────────────────────────────────────────────────────────────

    def _refresh_all(self):
        for t in ("folders", "gamedata", "files"):
            self._refresh_tab(t)

    def _refresh_tab(self, tab):
        q = self.search_var.get().lower()
        if tab == "folders":
            self._populate_indexed(self.folders_tree, self.folders, q)
        elif tab == "gamedata":
            self._populate_indexed(self.gamedata_tree, self.gamedata, q)
        else:
            self._populate_files(q)
        self._update_status()

    def _populate_indexed(self, tree, data, query=""):
        tree.delete(*tree.get_children())
        for i, item in enumerate(data):
            display = item if item else "(root)"
            if query and query not in display.lower() and query not in str(i):
                continue
            tag = "even" if i % 2 == 0 else "odd"
            tree.insert("", "end", values=(i, display), tags=(tag,))

    def _populate_files(self, query=""):
        tree = self.files_tree
        tree.delete(*tree.get_children())
        shown = 0
        for i, e in enumerate(self.files):
            folder_path = self._folder_name(e["folder_number"])
            scd_path    = self._gamedata_name(e["scd_number"])
            prefetch    = "Yes" if e["prefetch"] else "No"
            scd_display = e["scd_number"] if e["scd_number"] != NO_SCD else "—"

            row = (
                e["folder_number"], folder_path, e["file_path"],
                prefetch, scd_display, scd_path,
                e["serial_number inside the scd"], e["file_size"],
            )
            row_text = " ".join(str(v) for v in row).lower()
            if query and query not in row_text:
                continue

            stripe = "even" if shown % 2 == 0 else "odd"
            # tags: [0]=stripe, [1]=real data index, [2]=optional style
            extra_tag = "no_scd" if e["scd_number"] == NO_SCD else ""
            tree.insert("", "end", values=row,
                        tags=(stripe, str(i), extra_tag) if extra_tag
                        else (stripe, str(i)))
            shown += 1

        self.files_info.config(text=f"  Showing {shown} / {len(self.files)} entries")

    # ── sort ──────────────────────────────────────────────────────────────
    def _sort_tree(self, tree, col, reverse):
        data = [(tree.set(k, col), k) for k in tree.get_children("")]
        try:
            data.sort(key=lambda t: int(t[0]) if t[0] not in ("—", "", "(none)") else -1,
                      reverse=reverse)
        except ValueError:
            data.sort(key=lambda t: t[0].lower(), reverse=reverse)
        for idx, (_, k) in enumerate(data):
            tree.move(k, "", idx)
        tree.heading(col, command=lambda: self._sort_tree(tree, col, not reverse))

    # ── status ────────────────────────────────────────────────────────────
    def _update_status(self):
        parts = []
        if self.current_bdf_path:
            parts.append(os.path.basename(self.current_bdf_path))
        plat_label = "PC" if self.platform == "pc" else "Xbox 360"
        parts.append(f"[{plat_label}]")
        parts.append(f"{len(self.folders)} folders")
        parts.append(f"{len(self.gamedata)} gamedata")
        parts.append(f"{len(self.files)} files")
        if self.unsaved:
            parts.append("* unsaved changes")
        self.statusbar.config(text="  |  ".join(parts))

    def _set_status(self, msg):
        self.statusbar.config(text=msg)

    # ── help ──────────────────────────────────────────────────────────────
    def _show_help(self):
        help_text = (
            "BUILD FROM GAME FOLDER\n"
            "=" * 40 + "\n"
            "Scans the SC2 game directory automatically:\n"
            "  • Reads all gamedata/*.scd archives (ZIPs)\n"
            "  • Discovers loose files (movies, fonts, etc)\n"
            "  • Builds folders, gamedata, and file lists\n"
            "  • Sets prefetch flags by file type heuristic\n"
            "  • Review results and Compile BDF to save\n\n"
            "FILE ENTRY FIELDS\n"
            "=" * 40 + "\n\n"
            "Folder #\n"
            "  Line number (0-based) in folders.txt.\n"
            "  The dropdown resolves it to the actual path.\n\n"
            "File Path\n"
            "  Name of the file.\n\n"
            "Prefetch Textures\n"
            "  Boolean:  1 = Yes,  0 = No.\n\n"
            "SCD #  (gamedata)\n"
            "  Line number (0-based) in gamedata.txt.\n"
            "  Use 4294967295 for files NOT inside any SCD.\n"
            "  The dropdown resolves it to the .scd path.\n\n"
            "Serial # inside SCD\n"
            "  0-based position of the file inside the\n"
            "  .scd archive.  To find this number:\n"
            "    WinRAR -> Tools -> Generate Report\n"
            "    Count from 0 in the report output.\n\n"
            "File Size\n"
            "  Size in bytes.\n"
            "  Right-click the file -> Properties to check."
        )
        messagebox.showinfo("Help — Field Reference", help_text)


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point — supports:   python BDF_Editor.py [path_to.bdf]
#                             drag .bdf onto BDF_Editor.exe
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    app = BDFEditorApp(initial_file=initial)
    app.mainloop()
