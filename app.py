#!/usr/bin/env python3
"""Font Atlas: index every font on your computer without moving a single file.

Run:  python3 app.py
Then open http://localhost:8765 (it opens automatically).

Reads font files in place (TTF, OTF, TTC, WOFF, dfont), pulls their names and
style hints out of the font tables, suggests a category for each one, and
serves a little web app for reviewing categories and printing specimen sheets.
Nothing is ever moved, copied, or modified.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zlib
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "1.1.4"
VERSION_URL = ("https://raw.githubusercontent.com/clairesophi/Font-Atlas/"
               "main/VERSION")
DOWNLOAD_URL = "https://clairesophi.github.io/Font-Atlas/"

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def user_data_dir():
    """A per-user home for the index, outside the app folder, so replacing
    the app with a newer download never touches anyone's library."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support",
                            "Font Atlas")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", home), "Font Atlas")
    return os.path.join(home, ".local", "share", "font-atlas")


DATA_DIR = user_data_dir()
INDEX_PATH = os.path.join(DATA_DIR, "index.json")
LEGACY_INDEX = os.path.join(APP_DIR, "data", "index.json")
PORT = 8765

FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".dfont"}
CLASSIFIER_VERSION = 9  # bump to force re-classification of unchanged files

# Tag categories applied automatically by name at scan time.
AUTO_TAGS = [("trial", "Trial Fonts", ["trial", "demo version", "unlicensed"])]

SKIP_DIR_NAMES = {
    "node_modules", "caches", "cache", ".git", ".svn", ".hg", ".npm",
    ".cargo", "deriveddata", "cloudstorage", ".trash", "tmp", "temp",
    "photos library.photoslibrary", "movies", "music",
}

DEFAULT_CATEGORIES = [
    ("serif", "Serif"),
    ("sans", "Sans Serif"),
    ("slab", "Slab Serif"),
    ("mono", "Monospace"),
    ("script", "Script & Handwriting"),
    ("blackletter", "Blackletter"),
    ("display", "Display & Decorative"),
    ("symbols", "Symbols & Icons"),
    ("unsorted", "Unsorted"),
]


def default_scan_roots():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        sysdirs = ["/System/Library/Fonts", "/Library/Fonts",
                   os.path.join(home, "Library", "Fonts")]
    elif sys.platform.startswith("win"):
        sysdirs = [r"C:\Windows\Fonts",
                   os.path.join(home, r"AppData\Local\Microsoft\Windows\Fonts")]
    else:
        sysdirs = ["/usr/share/fonts", "/usr/local/share/fonts",
                   os.path.join(home, ".fonts"),
                   os.path.join(home, ".local", "share", "fonts")]
    return [d for d in sysdirs if os.path.isdir(d)], home


# ---------------------------------------------------------------- font parsing

def _sfnt_tables(data, off):
    """Table directory of a plain sfnt (TTF/OTF) starting at `off`."""
    num = struct.unpack_from(">H", data, off + 4)[0]
    tables = {}
    p = off + 12
    for _ in range(min(num, 512)):
        tag, _cks, toff, tlen = struct.unpack_from(">4sIII", data, p)
        p += 16
        tables[tag.decode("latin1")] = (toff, tlen, False)
    return tables


def _woff_tables(data):
    num = struct.unpack_from(">H", data, 12)[0]
    tables = {}
    p = 44
    for _ in range(min(num, 512)):
        tag, toff, clen, olen, _cks = struct.unpack_from(">4sIIII", data, p)
        p += 20
        tables[tag.decode("latin1")] = (toff, clen, clen != olen)
    return tables


def _get_table(data, tables, tag):
    if tag not in tables:
        return None
    toff, tlen, compressed = tables[tag]
    raw = data[toff:toff + tlen]
    if compressed:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            return None
    return raw


def _parse_name_table(tb):
    """Best english strings for the name IDs we care about."""
    if not tb or len(tb) < 6:
        return {}
    count, str_off = struct.unpack_from(">HH", tb, 2)
    best = {}
    for i in range(min(count, 400)):
        base = 6 + 12 * i
        if base + 12 > len(tb):
            break
        pid, _eid, lid, nid, length, off = struct.unpack_from(">6H", tb, base)
        if nid not in (1, 2, 4, 6, 16, 17):
            continue
        raw = tb[str_off + off:str_off + off + length]
        if not raw:
            continue
        try:
            if pid in (0, 3):
                s = raw.decode("utf-16-be")
            elif pid == 1:
                s = raw.decode("mac_roman", "replace")
            else:
                continue
        except UnicodeDecodeError:
            continue
        s = s.strip().replace("\x00", "")
        if not s:
            continue
        score = 2 if (pid == 3 and lid == 0x409) or (pid == 1 and lid == 0) else 1
        if nid not in best or best[nid][0] < score:
            best[nid] = (score, s)
    return {k: v[1] for k, v in best.items()}


def _cmap_cover(data, tables):
    """Return a cover(codepoint)->bool function over all readable cmap
    subtables, or None if the character map is unreadable."""
    cm = _get_table(data, tables, "cmap")
    if not cm or len(cm) < 4:
        return None
    n = struct.unpack_from(">H", cm, 2)[0]
    subs, seen = [], set()
    for i in range(min(n, 30)):
        try:
            _pid, _eid, off = struct.unpack_from(">HHI", cm, 4 + 8 * i)
        except struct.error:
            break
        if off in seen or off + 16 > len(cm):
            continue
        seen.add(off)
        fmt = struct.unpack_from(">H", cm, off)[0]
        if fmt in (4, 12):
            subs.append((fmt, off))
    if not subs:
        return None

    def cov(ch):
        for fmt, off in subs:
            try:
                if fmt == 4:
                    if ch > 0xFFFE:
                        continue
                    seg_x2 = struct.unpack_from(">H", cm, off + 6)[0]
                    seg = seg_x2 // 2
                    ends = struct.unpack_from(">%dH" % seg, cm, off + 14)
                    starts = struct.unpack_from(">%dH" % seg, cm,
                                                off + 16 + seg_x2)
                    if any(s <= ch <= e for s, e in zip(starts, ends)):
                        return True
                else:
                    ngroups = struct.unpack_from(">I", cm, off + 12)[0]
                    for g in range(min(ngroups, 20000)):
                        s, e, _ = struct.unpack_from(">III", cm,
                                                     off + 16 + 12 * g)
                        if s <= ch <= e:
                            return True
                        if s > ch:
                            break
            except struct.error:
                continue
        return False
    return cov


# For fonts with no Latin letters: probe one codepoint per script and show a
# native sample instead of a misleading fallback headline.
SCRIPT_SAMPLES = [
    (0x1F69A, "\U0001F69A\U0001F600\U0001F308\U0001F389✨"),
    (0x3042, "あのうちのトラック"),
    (0xAC00, "안녕, 나는 트럭"),
    (0x4E00, "永字八法 天地玄黃"),
    (0x0627, "أبجد هوز حطي"),
    (0x05D0, "אבגד הוזח"),
    (0x0915, "कखगघ नमस्ते"),
    (0x0995, "কখগঘ স্বাগত"),
    (0x0A15, "ਕਖਗਘ ਸਤਿ"),
    (0x0A95, "કખગઘ નમસ્તે"),
    (0x0B95, "கஙசஞ வணக்கம்"),
    (0x0C15, "కఖగఘ నమస్తే"),
    (0x0C95, "ಕಖಗಘ ನಮಸ್ಕಾರ"),
    (0x0D15, "കഖഗഘ നമസ്കാരം"),
    (0x0D9A, "කඛගඝ ආයුබෝවන්"),
    (0x0E01, "กขคง สวัสดี"),
    (0x0E81, "ກຂຄງ ສະບາຍດີ"),
    (0x0F40, "ཀཁགང བཀྲ་ཤིས"),
    (0x1000, "ကခဂဃ မင်ဂလာဘါ"),
    (0x1780, "កខគឃ ជំរាបសួរ"),
    (0x10D0, "აბგდ გამარჯობა"),
    (0x0531, "ԱԲԳԴ Բարեւ"),
    (0x1200, "ሀለሐመ ሰቀበተ"),
    (0x13A0, "ᎠᎡᎢᎣ ᎤᎥᎦᎧ"),
    (0x1401, "ᐁᐃᐅᐊ ᐱᐳᐸᑉ"),
    (0x0391, "ΑΒΓΔ αβγδ"),
    (0x0410, "АБВГ абвг"),
]


def _parse_face(data, off, is_woff):
    """One face -> metadata dict, or None if unreadable."""
    tables = _woff_tables(data) if is_woff else _sfnt_tables(data, off)
    names = _parse_name_table(_get_table(data, tables, "name"))
    family = names.get(16) or names.get(1) or ""
    subfamily = names.get(17) or names.get(2) or ""
    fullname = names.get(4) or (family + " " + subfamily).strip()
    if not family:
        return None

    panose = None
    weight = 400
    os2 = _get_table(data, tables, "OS/2")
    if os2 and len(os2) >= 42:
        weight = struct.unpack_from(">H", os2, 4)[0]
        panose = list(os2[32:42])

    fixed_pitch = False
    post = _get_table(data, tables, "post")
    if post and len(post) >= 16:
        fixed_pitch = struct.unpack_from(">I", post, 12)[0] != 0

    cov = _cmap_cover(data, tables)
    has_latin = (cov(0x41) and cov(0x61)) if cov else None
    if has_latin is False and cov(0xF041) and cov(0xF061):
        # symbol-encoded (Wingdings-style): browsers remap Latin onto the
        # symbols, so it previews fine
        has_latin = True
    sample = None
    if cov and has_latin is False:
        for cp, text in SCRIPT_SAMPLES:
            if cov(cp):
                sample = text
                break
    return {
        "family": family,
        "subfamily": subfamily,
        "fullname": fullname,
        "weight": weight,
        "panose": panose,
        "fixed_pitch": fixed_pitch,
        "has_latin": has_latin,
        "sample": sample,
    }


def parse_font_file(path):
    """All faces in a font file. Never modifies the file (read-only open)."""
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    fallback = [{"family": stem, "subfamily": "", "fullname": stem,
                 "weight": 400, "panose": None, "fixed_pitch": False,
                 "has_latin": None, "sample": None}]
    if ext in (".woff2", ".dfont"):
        # woff2 needs brotli and dfont a resource-fork parser; classify by name.
        return fallback
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    if len(data) < 44:
        return []
    faces = []
    try:
        tag = data[:4]
        if tag == b"ttcf":
            num = struct.unpack_from(">I", data, 8)[0]
            for i in range(min(num, 32)):
                off = struct.unpack_from(">I", data, 12 + 4 * i)[0]
                face = _parse_face(data, off, False)
                if face:
                    faces.append(face)
        elif tag == b"wOFF":
            face = _parse_face(data, 0, True)
            if face:
                faces.append(face)
        elif tag in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
            face = _parse_face(data, 0, False)
            if face:
                faces.append(face)
    except (struct.error, IndexError, ValueError):
        pass
    return faces or fallback


# ------------------------------------------------------------- classification

KEYWORDS = [
    ("blackletter", 0.95, ["fraktur", "blackletter", "black letter", "olde engl",
        "old english", "cloister black", "textur", "schwabach", "wedding text",
        "goudy text", "fette "]),
    ("symbols", 0.95, ["dingbat", "wingding", "webding", "symbol", "emoji",
        "ornaments", "pictograph", " icons", "glyphicon", "fontawesome",
        "bravura", "last resort", "keyboard"]),
    ("mono", 0.9, ["mono", "courier", "consol", "menlo", "monaco",
        "typewriter", "terminal", "fixed", " code", "andale"]),
    ("script", 0.9, ["script", "handwrit", "hand of", "brush", "calligraph",
        "cursive", "signature", "marker", "comic", "chalk", "casual",
        "zapfino", "snell", "chancery", "savoye", "noteworthy", "bradley"]),
    ("slab", 0.9, ["slab", "clarendon", "rockwell", "egyptienne", "egyptian",
        "memphis", "archer", "chunk"]),
    ("display", 0.9, ["display", "poster", "outline", "shadow", "inline",
        "stencil", "headline", "titling", "decorativ", "grunge", "groovy",
        "balloon", "western", "wood type", "neon", "pixel", "bitmap",
        "arcade", "lcd", "cooper black", "marquee", "engraved", "ornate",
        "impact", "playbill", "rosewood", "herculanum",
        "party", "curlz", "jazz", "bombard"]),
    ("world", 0.85, ["devanagari", "telugu", "gujarati", "kannada", "tamil",
        "bangla", "bengali", "oriya", "gurmukhi", "malayalam", "sinhala",
        "myanmar", "khmer", "lao mn", "lao ui", "thonburi", "sathu",
        "krungthep", "ayuthaya", "silom", "kailasa", "geeza", "baghdad",
        "nadeem", "farah", "kufi", "naskh", "hebrew", "raanana", "mshtakan",
        "heiti", "songti", "kaiti", "fangsong", "pingfang", "hiragino",
        "mincho", "meiryo", "yu gothic", "yu mincho", "osaka", "kohinoor",
        "euphemia", "plantagenet", "inai mathi", "cherokee", "mongolian",
        "tibetan", "amharic", "kefa", "hanzipen", "hannotate", "wawati",
        "xingkai", "yuanti", "libian", "baoli", "lantinghei", "gungseo",
        "pcmyungjo", "hoefler cyrillic"]),
    ("sans", 0.85, ["sans", "grotesk", "grotesque", "gothic", "helvetica",
        "arial", "futura", "avenir", "verdana", "tahoma", "geneva", "univers",
        "akzidenz", "roboto", "lato", "montserrat", "inter", "seravek",
        "optima", "frutiger", "myriad", "segoe", "calibri", "din ", "lucida",
        "charcoal", "chicago", "sf pro", "sf compact", "system font"]),
    ("serif", 0.85, ["serif", "roman", "antiqua", "garamond", "baskerville",
        "caslon", "didot", "bodoni", "times", "georgia", "palatino", "jenson",
        "minion", "sabon", "hoefler", "athelas", "charter", "cochin",
        "book antiqua", "century schoolbook", "new york", "iowan", "sf serif"]),
]


def classify(face, filename):
    """-> (category_id, confidence). Suggestion only; the user has final say."""
    hay = " ".join([face["family"], face["subfamily"], face["fullname"],
                    filename]).lower()
    if face["fixed_pitch"]:
        return "mono", 0.95
    for cat, conf, words in KEYWORDS:
        if any(w in hay for w in words):
            return cat, conf
    if face.get("has_latin") is False:
        return "world", 0.85
    p = face.get("panose")
    if p and any(p):
        fam, serif_style = p[0], p[1]
        if fam == 5:
            return "symbols", 0.75
        if fam == 3:
            return "script", 0.75
        if fam == 4:
            return "display", 0.7
        if p[3] == 9:
            return "mono", 0.8
        if fam == 2:
            if serif_style in (11, 12, 13, 14, 15):
                return "sans", 0.7
            if serif_style in (4, 5, 6):
                return "slab", 0.6
            if serif_style in (2, 3, 7, 8, 9, 10):
                return "serif", 0.7
    return "unsorted", 0.0


# ---------------------------------------------------------------------- index

LOCK = threading.Lock()
INDEX = {"fonts": {}, "categories": []}
SCAN = {"running": False, "dirs": 0, "found": 0, "current": "", "error": "",
        "done_at": 0, "roots": []}


def load_index():
    global INDEX
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(INDEX_PATH) and os.path.exists(LEGACY_INDEX):
        # older versions kept the index inside the app folder; adopt it
        try:
            shutil.copy2(LEGACY_INDEX, INDEX_PATH)
        except OSError:
            pass
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                INDEX = json.load(f)
        except (OSError, ValueError):
            pass
    default_ids = {cid for cid, _ in DEFAULT_CATEGORIES}
    if not INDEX.get("categories"):
        INDEX["categories"] = [{"id": cid, "name": name,
                                "visible": True, "kind": "style"}
                               for cid, name in DEFAULT_CATEGORIES]
    else:
        have = {c["id"] for c in INDEX["categories"]}
        for cid, name in DEFAULT_CATEGORIES:
            if cid not in have:
                INDEX["categories"].insert(
                    max(0, len(INDEX["categories"]) - 1),
                    {"id": cid, "name": name, "visible": True,
                     "kind": "style"})
        for c in INDEX["categories"]:
            c.setdefault("kind",
                         "style" if c["id"] in default_ids else "tag")
    have = {c["id"] for c in INDEX["categories"]}
    for cid, name, _kw in AUTO_TAGS:
        if cid not in have:
            INDEX["categories"].insert(
                max(0, len(INDEX["categories"]) - 1),
                {"id": cid, "name": name, "visible": True, "kind": "tag"})
    for cid, name in (("nonlatin", "Non-Latin"),
                      ("nopreview", "Preview Unavailable")):
        if cid not in have:
            INDEX["categories"].insert(
                max(0, len(INDEX["categories"]) - 1),
                {"id": cid, "name": name, "visible": False, "kind": "tag"})
    INDEX.setdefault("fonts", {})
    INDEX.setdefault("settings", {})
    INDEX.setdefault("groups", [])
    # one-time migration: tag checkboxes became filters, so they start off
    if not INDEX["settings"].get("tags_are_filters"):
        for c in INDEX["categories"]:
            if c.get("kind") == "tag":
                c["visible"] = False
        INDEX["settings"]["tags_are_filters"] = True
    # Other Writing Systems is retired: those fonts live under the
    # Non-Latin toggle instead
    INDEX["categories"] = [c for c in INDEX["categories"]
                           if c["id"] != "world"]
    for e in INDEX["fonts"].values():
        if "world" in (e.get("category"), e.get("suggested"),
                       e.get("builtin")):
            if e.get("category") == "world":
                e["category"] = "unsorted"
            if e.get("suggested") == "world":
                e["suggested"] = "unsorted"
            if e.get("builtin") == "world":
                e["builtin"] = "unsorted"
            e["also"] = sorted(set(e.get("also", [])) | {"nonlatin"})
    # a font's primary home is always a style category; demote stray tags,
    # and its extras are tags only, never other categories
    tag_ids = {c["id"] for c in INDEX["categories"] if c.get("kind") == "tag"}
    for e in INDEX["fonts"].values():
        if e.get("category") in tag_ids:
            tag = e["category"]
            e["category"] = e.get("builtin") or "unsorted"
            e["suggested"] = e["category"]
            e["also"] = sorted(set(e.get("also", [])) | {tag})
        if e.get("also"):
            e["also"] = [t for t in e["also"] if t in tag_ids]


def save_index():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(INDEX, f, ensure_ascii=False)
    os.replace(tmp, INDEX_PATH)


def font_id(path, face_index):
    return sha1(f"{path}#{face_index}".encode("utf-8")).hexdigest()[:16]


def index_file(path, seen_ids):
    try:
        st = os.stat(path)
    except OSError:
        return 0
    if st.st_size < 1000 or st.st_size > 200 * 1024 * 1024:
        return 0
    fname = os.path.basename(path)
    with LOCK:
        probe = INDEX["fonts"].get(font_id(path, 0))
        unchanged = (probe and probe.get("mtime") == int(st.st_mtime)
                     and probe.get("size") == st.st_size
                     and probe.get("cv") == CLASSIFIER_VERSION)
    if unchanged:
        # File hasn't changed; keep existing entries (all faces share the file).
        n = 0
        with LOCK:
            for fid, e in INDEX["fonts"].items():
                if e["path"] == path:
                    seen_ids.add(fid)
                    n += 1
        return n
    faces = parse_font_file(path)
    n = 0
    for i, face in enumerate(faces):
        if face["family"].startswith("."):
            continue  # hidden system-internal face inside a collection
        cat, conf = classify(face, fname)
        if cat == "world":
            cat, conf = "unsorted", 0.85
            face = dict(face)
            face["_worldish"] = True
        txt = (face["fullname"] + " " + fname).lower()
        auto = [cid for cid, _name, kws in AUTO_TAGS
                if any(k in txt for k in kws)]
        if fname.lower().endswith(".dfont"):
            auto.append("nopreview")  # browsers can never draw these
        if face.get("has_latin") is False or face.get("_worldish"):
            auto.append("nonlatin")
        if face.get("has_latin") is False and not face.get("sample"):
            # no Latin letters and no recognizable script: nothing that
            # a preview could meaningfully show (math pieces, encodings)
            auto.append("nopreview")
        fid = font_id(path, i)
        with LOCK:
            old = INDEX["fonts"].get(fid)
            entry = {
                "id": fid, "path": path, "ext": os.path.splitext(path)[1].lower(),
                "size": st.st_size, "mtime": int(st.st_mtime), "face": i,
                "cv": CLASSIFIER_VERSION,
                "family": face["family"], "subfamily": face["subfamily"],
                "fullname": face["fullname"], "weight": face["weight"],
                "has_latin": face.get("has_latin"),
                "sample": face.get("sample"),
                # auto-managed tags are recomputed each scan; manual and
                # AI tags are kept
                "also": sorted((set(old.get("also", []) if old else [])
                                - {"trial", "nonlatin", "nopreview"})
                               | set(auto)),
                "builtin": cat,
                "suggested": (old.get("suggested", cat) if old else cat),
                "confidence": round(conf, 2),
                "category": old["category"] if old else cat,
                "locked": bool(old and old.get("locked")),
                "uncertain": (old.get("uncertain", False) if old
                              else conf < 0.7),
            }
            INDEX["fonts"][fid] = entry
            seen_ids.add(fid)
        n += 1
    return n


def scan_worker(roots):
    seen_ids = set()
    try:
        for root in roots:
            root = os.path.abspath(os.path.expanduser(root))
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [
                    d for d in dirnames
                    if d.lower() not in SKIP_DIR_NAMES
                    and not (d.startswith(".") and d.lower() not in (".fonts", ".local"))
                ]
                SCAN["dirs"] += 1
                SCAN["current"] = dirpath
                for fn in filenames:
                    if fn.startswith("."):
                        continue  # hidden system-internal fonts, unusable anyway
                    if os.path.splitext(fn)[1].lower() in FONT_EXTS:
                        SCAN["found"] += index_file(os.path.join(dirpath, fn), seen_ids)
                if SCAN["found"] and SCAN["found"] % 250 == 0:
                    with LOCK:
                        save_index()
        # Drop entries whose file is gone, but only within the scanned roots.
        with LOCK:
            for fid in list(INDEX["fonts"]):
                e = INDEX["fonts"][fid]
                if (os.path.basename(e["path"]).startswith(".")
                        or e["family"].startswith(".")):
                    del INDEX["fonts"][fid]
                    continue
                if fid not in seen_ids and any(
                        e["path"].startswith(os.path.abspath(os.path.expanduser(r)))
                        for r in roots) and not os.path.exists(e["path"]):
                    del INDEX["fonts"][fid]
            save_index()
    except Exception as exc:  # keep the app alive whatever a weird file does
        SCAN["error"] = str(exc)
    finally:
        SCAN["running"] = False
        SCAN["current"] = ""
        SCAN["done_at"] = int(time.time())
        # In AI mode, hand the leftovers straight to Claude for sorting.
        with LOCK:
            s = INDEX.get("settings", {})
            key = s.get("api_key")
            ai = s.get("mode") == "ai" and key and not AISORT["running"]
        if ai:
            AISORT.update(running=True, done=0, total=0, error="",
                          new_categories=0, cancel=False, stopped=False)
            threading.Thread(target=aisort_worker, args=(key, "uncertain"),
                             daemon=True).start()


# ------------------------------------------------------------- update check

UPDATE = {"available": False, "latest": "", "url": DOWNLOAD_URL}


def check_for_update():
    """Quietly compare our VERSION against the repo's; never blocks anything."""
    try:
        req = urllib.request.Request(VERSION_URL,
                                     headers={"User-Agent": "font-atlas"})
        with urllib.request.urlopen(req, timeout=10) as r:
            latest = r.read().decode("utf-8").strip()
        def parse(v):
            return tuple(int(x) for x in v.split("."))
        if parse(latest) > parse(VERSION):
            UPDATE.update(available=True, latest=latest)
    except Exception:
        pass  # offline or repo unreachable: simply no update notice


ZIP_URL = ("https://github.com/clairesophi/Font-Atlas/archive/refs/heads/"
           "main.zip")


def self_update():
    """Download the current release from the repo and atomically replace the
    app's own files. The user's library is elsewhere and is never touched.
    Returns the new version string, or raises with a readable message."""
    import tempfile
    import zipfile
    req = urllib.request.Request(ZIP_URL,
                                 headers={"User-Agent": "font-atlas"})
    with urllib.request.urlopen(req, timeout=120) as r:
        blob = r.read()
    with tempfile.TemporaryDirectory() as td:
        zpath = os.path.join(td, "update.zip")
        with open(zpath, "wb") as f:
            f.write(blob)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(td)
        root = next((os.path.join(td, d) for d in os.listdir(td)
                     if os.path.isdir(os.path.join(td, d))), None)
        if not root or not os.path.exists(os.path.join(root, "app.py")):
            raise RuntimeError("the download didn't look like Font Atlas")
        vfile = os.path.join(root, "VERSION")
        latest = (open(vfile).read().strip()
                  if os.path.exists(vfile) else "0.0.0")
        def parse(v):
            return tuple(int(x) for x in v.split("."))
        if parse(latest) <= parse(VERSION):
            raise RuntimeError("you already have the newest version "
                               "(v%s)" % VERSION)
        for name in ("app.py", "index.html", "README.md", "VERSION",
                     "Start Font Atlas.command"):
            src = os.path.join(root, name)
            if not os.path.exists(src):
                continue
            dst = os.path.join(APP_DIR, name)
            tmp = dst + ".new"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
    return latest


def restart_self():
    os.execv(sys.executable,
             [sys.executable, os.path.join(APP_DIR, "app.py"),
              "--no-browser"])


# ------------------------------------------------------- sorting with Claude

AISORT = {"running": False, "done": 0, "total": 0, "error": "",
          "new_categories": 0, "cancel": False, "stopped": False}
API_URL = "https://api.anthropic.com/v1/messages"


def claude_call(key, prompt, workspace=""):
    payload = {
        "model": "claude-opus-5",
        "max_tokens": 16000,
        "fallbacks": "default",
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"x-api-key": key,
               "anthropic-version": "2023-06-01",
               "anthropic-beta": "server-side-fallback-2026-07-01",
               "content-type": "application/json"}
    if workspace:
        headers["anthropic-workspace-id"] = workspace
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("stop_reason") == "refusal":
        raise RuntimeError("the model declined this request")
    text = "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text")
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def aisort_prompt(cats, batch, wanted):
    style_lines = "\n".join(f"  {c['id']}: {c['name']}" for c in cats
                            if c.get("kind") != "tag")
    tag_lines = "\n".join(f"  {c['id']}: {c['name']}" for c in cats
                          if c.get("kind") == "tag") or "  (none yet)"
    font_lines = "\n".join(
        f"  {f['id']} | {f['fullname']} | {os.path.basename(f['path'])}"
        for f in batch)
    wanted_block = ""
    if wanted:
        wanted_lines = "\n".join(f"  - {w}" for w in wanted)
        wanted_block = f"""
The user specifically wants these tags (create each as a new tag with a short lowercase id if it doesn't exist yet; if one describes a family of groups, like decades, create one tag per group as needed):
{wanted_lines}
"""
    return f"""You are helping organize a personal font library, like the sections of a classic type specimen book.

CATEGORIES (id: name) — mutually exclusive; every font lives in exactly one:
{style_lines}

TAGS (id: name) — optional labels; a font can carry several or none:
{tag_lines}
{wanted_block}
Fonts to sort, one per line (id | full name | file name):
{font_lines}

For every font give a list of ids. The FIRST must be its single best-fitting CATEGORY, chosen only from the category list ("unsorted" is a last resort). After it, add tag ids, but be selective: a good tag carves out a distinct slice of the library, it must not swallow it. Only tag a font when the tag clearly, specifically applies; most fonts should carry no tags. If a tag would end up on more than about a quarter of these fonts, it is too broad, so apply it only to the strongest examples or skip it. Judge by what you know of the typeface, or its name if unknown. Beyond the user's requested tags you may propose up to 3 new ones when several fonts clearly form a group worth naming.

Reply with ONLY this JSON, nothing else:
{{"assignments": {{"<font id>": ["<category id>", "<optional tag id>", ...], ...}},
 "new_categories": [{{"id": "<short-lowercase-id>", "name": "<Tag Display Name>"}}]}}"""


def aisort_worker(key, scope, wanted=()):
    try:
        with LOCK:
            workspace = INDEX.get("settings", {}).get("workspace_id", "")
            cats = [dict(c) for c in INDEX["categories"]]
            if scope == "all":
                todo = [f for f in INDEX["fonts"].values() if not f["locked"]]
            else:
                todo = [f for f in INDEX["fonts"].values()
                        if f["uncertain"] or f["category"] == "unsorted"]
        AISORT["total"] = len(todo)
        for i in range(0, len(todo), 80):
            if AISORT.get("cancel"):
                AISORT["stopped"] = True
                break
            batch = todo[i:i + 80]
            result = claude_call(key, aisort_prompt(cats, batch, wanted),
                                 workspace)
            new_cats = result.get("new_categories") or []
            assignments = result.get("assignments") or {}
            with LOCK:
                have = {c["id"] for c in INDEX["categories"]}
                for nc in new_cats:
                    cid = str(nc.get("id", "")).strip()
                    name = str(nc.get("name", "")).strip()
                    if cid and name and cid not in have:
                        INDEX["categories"].insert(
                            max(0, len(INDEX["categories"]) - 1),
                            {"id": cid, "name": name, "visible": True,
                             "kind": "tag"})
                        have.add(cid)
                        cats.append({"id": cid, "name": name, "kind": "tag"})
                        AISORT["new_categories"] += 1
                style_ids = {c["id"] for c in INDEX["categories"]
                             if c.get("kind") != "tag"}
                for fid, val in assignments.items():
                    if isinstance(val, str):
                        val = [val]
                    val = [c for c in val if isinstance(c, str) and c in have]
                    e = INDEX["fonts"].get(fid)
                    if not e or e["locked"] or not val:
                        continue
                    # primary must be a style category; tags go to `also`
                    prim = next((c for c in val if c in style_ids), None)
                    if prim:
                        e["category"] = prim
                        e["suggested"] = prim
                        e["uncertain"] = False
                    # add Claude's tags on top of existing ones, never
                    # replace; extras are tags only, never categories
                    e["also"] = sorted((set(e.get("also", [])) | set(val))
                                       - style_ids)
                save_index()
            AISORT["done"] = min(i + len(batch), len(todo))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = detail.get("error", {}).get("message", str(exc))
        except Exception:
            msg = str(exc)
        AISORT["error"] = f"API error {exc.code}: {msg}"
    except Exception as exc:
        AISORT["error"] = str(exc)
    finally:
        AISORT["running"] = False


def deduped_fonts():
    """One entry per (family, style); extra copies of the same face are
    folded into it. Hand-sorted copies win, then the most recent install."""
    groups = {}
    for e in INDEX["fonts"].values():
        key = (e["family"].lower().strip(), e["subfamily"].lower().strip())
        groups.setdefault(key, []).append(e)
    out = []
    for entries in groups.values():
        entries.sort(key=lambda e: (not e.get("locked"),
                                    -e.get("mtime", 0),
                                    e["path"]))
        canon = dict(entries[0])
        if len(entries) > 1:
            canon["copies"] = [x["path"] for x in entries]
        out.append(canon)
    return out


# --------------------------------------------------------------------- server

MIME = {".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection",
        ".otc": "font/collection", ".woff": "font/woff", ".woff2": "font/woff2",
        ".dfont": "application/octet-stream"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif path == "/api/data":
            with LOCK:
                fonts = deduped_fonts()
                cats = INDEX["categories"]
                settings = INDEX.get("settings", {})
            self._json({"fonts": fonts, "categories": cats,
                        "groups": INDEX.get("groups", []),
                        "version": VERSION, "update": UPDATE, "scan": SCAN,
                        "aisort": AISORT,
                        "mode": settings.get("mode", "builtin"),
                        "has_key": bool(settings.get("api_key")),
                        "workspace_id": settings.get("workspace_id", ""),
                        "roots": default_scan_roots()[0],
                        "home": default_scan_roots()[1]})
        elif path.startswith("/font/"):
            fid = path.split("/")[2]
            with LOCK:
                entry = INDEX["fonts"].get(fid)
            if not entry or not os.path.exists(entry["path"]):
                self.send_error(404)
                return
            try:
                with open(entry["path"], "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(entry["ext"],
                                                      "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError:
            self._json({"error": "bad json"}, 400)
            return
        if path == "/api/scan":
            if SCAN["running"]:
                self._json({"ok": False, "error": "scan already running"})
                return
            roots = body.get("roots") or default_scan_roots()[0]
            SCAN.update(running=True, dirs=0, found=0, current="", error="",
                        roots=roots)
            threading.Thread(target=scan_worker, args=(roots,),
                             daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/assign":
            ids, cat = body.get("ids") or [], body.get("category")
            reset, remove_from = body.get("reset"), body.get("remove_from")
            if body.get("reset_all"):
                # re-apply the built-in suggestions to every unlocked font
                auto_ids = {cid for cid, _n, _k in AUTO_TAGS}
                with LOCK:
                    for e in INDEX["fonts"].values():
                        if not e["locked"]:
                            e["category"] = e.get("builtin") or e["suggested"]
                            e["suggested"] = e["category"]
                            e["uncertain"] = e["confidence"] < 0.7
                            e["also"] = [t for t in e.get("also", [])
                                         if t in auto_ids]
                    save_index()
                self._json({"ok": True})
                return
            with LOCK:
                for fid in ids:
                    e = INDEX["fonts"].get(fid)
                    if not e:
                        continue
                    if body.get("add_to"):
                        add = body["add_to"]
                        if add != e["category"]:
                            e["also"] = sorted(set(e.get("also", [])) | {add})
                    elif remove_from:
                        e["also"] = [c for c in e.get("also", [])
                                     if c != remove_from]
                    elif reset:
                        e["category"] = e["suggested"]
                        e["locked"] = False
                        e["uncertain"] = e["confidence"] < 0.7
                    else:
                        e["category"] = cat
                        e["also"] = [c for c in e.get("also", []) if c != cat]
                        e["locked"] = True
                        e["uncertain"] = False
                save_index()
            self._json({"ok": True})
        elif path == "/api/categories":
            cats = body.get("categories") or []
            if not any(c.get("id") == "unsorted" for c in cats):
                cats.append({"id": "unsorted", "name": "Unsorted",
                             "visible": True, "kind": "style"})
            default_ids = {cid for cid, _ in DEFAULT_CATEGORIES}
            for c in cats:
                c.setdefault("kind",
                             "style" if c.get("id") in default_ids else "tag")
            ids = {c["id"] for c in cats}
            with LOCK:
                INDEX["categories"] = cats
                for e in INDEX["fonts"].values():
                    if e["category"] not in ids:
                        e["category"] = "unsorted"
                    e["also"] = [c for c in e.get("also", []) if c in ids]
                save_index()
            self._json({"ok": True})
        elif path == "/api/reveal":
            with LOCK:
                e = INDEX["fonts"].get(body.get("id", ""))
            if not e or not os.path.exists(e["path"]):
                self._json({"ok": False, "error": "file not found"})
                return
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", e["path"]])
                elif sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", "/select,", e["path"]])
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(e["path"])])
                self._json({"ok": True})
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)})
        elif path == "/api/settings":
            with LOCK:
                s = INDEX.setdefault("settings", {})
                if "mode" in body:
                    s["mode"] = body["mode"] if body["mode"] in ("ai", "builtin") else "builtin"
                if "api_key" in body:
                    if body["api_key"]:
                        s["api_key"] = body["api_key"]
                    else:
                        s.pop("api_key", None)
                if "workspace_id" in body:
                    ws = body["workspace_id"].strip()
                    # guard against the API key (or anything else) landing here
                    s["workspace_id"] = ws if ws.startswith("wrkspc_") else ""
                save_index()
            self._json({"ok": True, "has_key": bool(s.get("api_key"))})
        elif path == "/api/aisort":
            if AISORT["running"]:
                self._json({"ok": False, "error": "AI sort already running"})
                return
            with LOCK:
                key = INDEX.get("settings", {}).get("api_key")
            if not key:
                self._json({"ok": False, "error": "no API key saved"})
                return
            scope = body.get("scope", "uncertain")
            wanted = [str(w).strip() for w in (body.get("categories") or [])
                      if str(w).strip()][:20]
            AISORT.update(running=True, done=0, total=0, error="",
                          new_categories=0, cancel=False, stopped=False)
            threading.Thread(target=aisort_worker, args=(key, scope, wanted),
                             daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/self_update":
            try:
                latest = self_update()
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
                return
            self._json({"ok": True, "version": latest})
            threading.Timer(0.8, restart_self).start()
        elif path == "/api/groups":
            op = body.get("op")
            with LOCK:
                groups = INDEX.setdefault("groups", [])
                if op == "add":
                    name = str(body.get("name", "")).strip() or "group"
                    ids = [i for i in (body.get("ids") or [])
                           if isinstance(i, str)][:10000]
                    gid = "g%d" % int(time.time() * 1000)
                    groups.append({"id": gid, "name": name, "ids": ids})
                elif op == "del":
                    INDEX["groups"] = [g for g in groups
                                       if g["id"] != body.get("id")]
                elif op == "ren":
                    for g in groups:
                        if g["id"] == body.get("id"):
                            new = str(body.get("name", "")).strip()
                            g["name"] = new or g["name"]
                elif op in ("addto", "rmfrom"):
                    ids = {i for i in (body.get("ids") or [])
                           if isinstance(i, str)}
                    for g in groups:
                        if g["id"] == body.get("id"):
                            if op == "addto":
                                g["ids"] = sorted(set(g["ids"]) | ids)
                            else:
                                g["ids"] = [i for i in g["ids"]
                                            if i not in ids]
                save_index()
            self._json({"ok": True})
        elif path == "/api/nopreview":
            # the browser reports fonts its font engine refuses to draw
            with LOCK:
                for fid in body.get("ids") or []:
                    e = INDEX["fonts"].get(fid)
                    if e:
                        e["also"] = sorted(set(e.get("also", []))
                                           | {"nopreview"})
                save_index()
            self._json({"ok": True})
        elif path == "/api/aisort_stop":
            if AISORT["running"]:
                AISORT["cancel"] = True
            self._json({"ok": True})
        else:
            self.send_error(404)


def main():
    load_index()
    threading.Thread(target=check_for_update, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Font Atlas running at {url}  (Ctrl+C to quit)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
