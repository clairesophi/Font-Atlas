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

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")
PORT = 8765

FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".dfont"}
CLASSIFIER_VERSION = 4  # bump to force re-classification of unchanged files

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
    ("world", "Other Writing Systems"),
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


def _has_latin(data, tables):
    """Does the font's character map cover basic Latin (A and a)? None = unknown."""
    cm = _get_table(data, tables, "cmap")
    if not cm or len(cm) < 4:
        return None
    n = struct.unpack_from(">H", cm, 2)[0]
    sub = None
    for i in range(min(n, 30)):
        pid, eid, off = struct.unpack_from(">HHI", cm, 4 + 8 * i)
        if pid == 3 and eid in (1, 10):
            sub = off
            break
        if pid == 0 and sub is None:
            sub = off
    if sub is None or sub + 16 > len(cm):
        return None
    try:
        fmt = struct.unpack_from(">H", cm, sub)[0]
        if fmt == 4:
            seg_x2 = struct.unpack_from(">H", cm, sub + 6)[0]
            seg = seg_x2 // 2
            ends = struct.unpack_from(">%dH" % seg, cm, sub + 14)
            starts = struct.unpack_from(">%dH" % seg, cm, sub + 16 + seg_x2)
            def cov(ch):
                return any(s <= ch <= e for s, e in zip(starts, ends))
            return cov(0x41) and cov(0x61)
        if fmt == 12:
            ngroups = struct.unpack_from(">I", cm, sub + 12)[0]
            def cov(ch):
                for g in range(min(ngroups, 20000)):
                    s, e, _ = struct.unpack_from(">III", cm, sub + 16 + 12 * g)
                    if s <= ch <= e:
                        return True
                    if s > ch:
                        return False
                return False
            return cov(0x41) and cov(0x61)
    except struct.error:
        return None
    return None


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

    return {
        "family": family,
        "subfamily": subfamily,
        "fullname": fullname,
        "weight": weight,
        "panose": panose,
        "fixed_pitch": fixed_pitch,
        "has_latin": _has_latin(data, tables),
    }


def parse_font_file(path):
    """All faces in a font file. Never modifies the file (read-only open)."""
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    fallback = [{"family": stem, "subfamily": "", "fullname": stem,
                 "weight": 400, "panose": None, "fixed_pitch": False,
                 "has_latin": None}]
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
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                INDEX = json.load(f)
        except (OSError, ValueError):
            pass
    default_ids = {cid for cid, _ in DEFAULT_CATEGORIES}
    if not INDEX.get("categories"):
        INDEX["categories"] = [{"id": cid, "name": name, "visible": True,
                                "kind": "style"}
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
    INDEX.setdefault("fonts", {})
    INDEX.setdefault("settings", {})
    # a font's primary home is always a style category; demote stray tags
    tag_ids = {c["id"] for c in INDEX["categories"] if c.get("kind") == "tag"}
    for e in INDEX["fonts"].values():
        if e.get("category") in tag_ids:
            tag = e["category"]
            e["category"] = e.get("builtin") or "unsorted"
            e["suggested"] = e["category"]
            e["also"] = sorted((set(e.get("also", [])) | {tag})
                               - {e["category"]})


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
                "also": old.get("also", []) if old else [],
                "builtin": cat,
                "suggested": cat, "confidence": round(conf, 2),
                "category": old["category"] if (old and old.get("locked")) else cat,
                "locked": bool(old and old.get("locked")),
                "uncertain": conf < 0.7 and not (old and old.get("locked")),
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
The user also specifically wants these thematic groupings (create each as a new thematic category with a short lowercase id if it doesn't exist yet; if one describes a family of groups, like decades, create one category per group as needed):
{wanted_lines}
"""
    return f"""You are helping organize a personal font library, like the sections of a classic type specimen book.

STYLE categories (id: name) — every font's primary home:
{style_lines}

THEMATIC categories (id: name) — optional extra memberships (aesthetics, eras, use cases):
{tag_lines}
{wanted_block}
Fonts to sort, one per line (id | full name | file name):
{font_lines}

For every font give a list of category ids. The FIRST must be its single best-fitting STYLE category, chosen only from the style list ("unsorted" is a last resort). After it, add any thematic ids the font genuinely fits. Judge by what you know of the typeface, or its name if unknown. Beyond the user's requested groupings you may propose up to 3 more new thematic categories if several fonts clearly need them.

Reply with ONLY this JSON, nothing else:
{{"assignments": {{"<font id>": ["<style category id>", "<optional thematic id>", ...], ...}},
 "new_categories": [{{"id": "<short-lowercase-id>", "name": "<Display Name>"}}]}}"""


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
                    e["also"] = sorted(set(val) - {prim, e["category"]})
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
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/data":
            with LOCK:
                fonts = list(INDEX["fonts"].values())
                cats = INDEX["categories"]
                settings = INDEX.get("settings", {})
            self._json({"fonts": fonts, "categories": cats, "scan": SCAN,
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
                with LOCK:
                    for e in INDEX["fonts"].values():
                        if not e["locked"]:
                            e["category"] = e.get("builtin") or e["suggested"]
                            e["suggested"] = e["category"]
                            e["uncertain"] = e["confidence"] < 0.7
                            e["also"] = []
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
        elif path == "/api/aisort_stop":
            if AISORT["running"]:
                AISORT["cancel"] = True
            self._json({"ok": True})
        else:
            self.send_error(404)


def main():
    load_index()
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
