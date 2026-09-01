# Font Atlas

Index every font on a computer without moving a single file, then test
headlines and export type specimen sheets organized by style category.

Fonts are only ever **read** in place: nothing is moved, copied, installed,
renamed, or deleted. The index (categories, tags, groups, settings) is saved
per user outside the app folder (on a Mac: `~/Library/Application Support/
Font Atlas/`), so downloading a newer Font Atlas never loses your sorting.
The app checks GitHub for a newer version on launch and shows a small
"new version" link when there is one.

## Run it

Anyone with a Mac can double-click **Start Font Atlas.command**.
The first time, macOS will block it ("Apple could not verify..."): click
Done, then open System Settings > Privacy & Security, scroll down, and click
**Open Anyway**. That's only needed once.

Or from a terminal, on any Mac / Windows / Linux machine with Python 3:

```
python3 app.py
```

Then it opens at http://localhost:8765. No installs, no dependencies, and
nothing leaves the machine (unless you turn on AI mode, below).

## What it does

1. **Scan**: walks the folders you pick (system font folders by default, or
   your whole home folder) and indexes every .ttf, .otf, .ttc, .woff, .woff2
   and .dfont it finds. It reads each font's internal name table, style hints
   (PANOSE, fixed pitch), and character coverage to suggest a category:
   Serif, Sans Serif, Slab Serif, Monospace, Script & Handwriting,
   Blackletter, Display & Decorative, Symbols & Icons, Other Writing Systems.
2. **Sorting modes**: with **built-in** sorting everything happens offline
   with those heuristics. Flip to **AI (Claude)** mode, paste your own
   Anthropic API key (console.anthropic.com/settings/keys), and Claude sorts
   the fonts by what it knows about each typeface, inventing new categories
   where a style deserves its own section. Only font names are sent, never
   files. The key lives in plain text in `data/index.json` on this computer.
3. **Review and edit**: low-confidence guesses get a "?" flag and a review
   panel for one-click sorting. Each specimen has a discreet "edit…" menu on
   the right to re-file it (or send it back to automatic). Hand-sorted fonts
   show a ✓ and are never overwritten by a rescan or by AI sorting.
4. **Categories**: add your own, rename (double-click), delete, drag to
   reorder, check on/off which ones appear in the sheet.
5. **Specimen sheet**: type any headline and see it in every font, grouped by
   category on a white page, like an old phototype catalog. Or flip on "use
   each font's own name" for the classic specimen-book look. Tick the little
   checkbox under favorites as you browse to build a shortlist.
6. **Export PDF**: pick page size, specimen size, fonts per page, whether to
   include file paths, and whether to export everything visible or just your
   selected fonts. It opens the print dialog: choose "Save as PDF" there
   (that's what embeds the real font shapes into the PDF).

## Notes

- macOS may ask permission for Desktop / Documents / Downloads when you scan
  your whole home folder. That's read-only access for the scan.
- A few formats can't be previewed in the browser (.dfont, and .ttc shows
  only its first face on some browsers). They're still indexed and sortable.
- "Other Writing Systems" holds fonts with no Latin letters (Arabic, CJK,
  Devanagari and friends); their previews would just be a fallback font, and
  they're flagged "non-Latin".
- Rescanning is safe: your manual category choices are kept.
