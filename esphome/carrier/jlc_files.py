"""JLCPCB assembly files for the SMD carrier: the BOM (Comment, Designator, Footprint, LCSC Part #) from
build_board.py's raw list, and the placement file (Designator, Mid X, Mid Y, Layer, Rotation) from KiCad's.
Only parts with an LCSC number are listed: the factory places those; the headers, terminals and sockets are
soldered by hand.

  python3 jlc_files.py <placed>-bom-raw.csv <kicad pos csv> out-bom.csv out-cpl.csv"""
import csv
import sys

raw, pos, bom_out, cpl_out = sys.argv[1:5]
parts = [r for r in csv.DictReader(open(raw)) if r["lcsc"]]
groups = {}
for r in parts:
    key = (r["value"], r["footprint"].split(":")[1], r["lcsc"])
    groups.setdefault(key, []).append(r["ref"])
with open(bom_out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    for (value, fp, lcsc), refs in sorted(groups.items()):
        w.writerow([value, ",".join(sorted(refs, key=lambda s: (s[0], int(s[1:])))), fp, lcsc])
placed = {r["ref"] for r in parts}
with open(cpl_out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    for r in csv.DictReader(open(pos)):
        if r["Ref"] in placed:
            w.writerow([r["Ref"], r["PosX"] + "mm", r["PosY"] + "mm", "Top" if r["Side"] == "top" else "Bottom", r["Rot"]])
missing = placed - {r["Ref"] for r in csv.DictReader(open(pos))}
if missing:
    raise SystemExit(f"not in the placement file: {sorted(missing)}")
print(f"BOM: {len(groups)} lines, CPL: {len(placed)} parts")
