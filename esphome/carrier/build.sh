#!/bin/sh
# Builds the carrier boards in Docker: placement, Freerouting, ground pours, DRC, fabrication files.
# Variants: tht, smd (the 100 x 60 mm board for the box) and din-tht, din-smd (the DIN-rail board). Default: all four.
# Needs Docker and freerouting.jar (https://github.com/freerouting/freerouting/releases, v2.1.0) in this folder.
# Output: fab/<variant>/ with the KiCad board, Gerbers + drill (zipped for the PCB maker), DRC report, renders,
# and for SMD the JLCPCB BOM and placement files.
set -eu
cd "$(dirname "$0")"
KICAD="docker run --rm -u $(id -u):$(id -g) -e HOME=/w -v $PWD:/w -w /w kicad/kicad:9.0"
JAVA="docker run --rm -u $(id -u):$(id -g) -e HOME=/w -v $PWD:/w -w /w eclipse-temurin:21-jre"
for v in ${*:-tht smd din-tht din-smd}; do
  case $v in din-*) lay=din; parts=${v#din-} ;; *) lay=box; parts=$v ;; esac
  o=fab/$v; rm -rf "$o"; mkdir -p "$o"
  $KICAD python3 build_board.py place "$parts" "$o/carrier-$v-placed.kicad_pcb" "$lay"
  b=$o/carrier-$v.kicad_pcb
  # Freerouting's result differs from run to run, and now and then a route doesn't survive the import into KiCad or
  # crosses another net: route again (up to 4 tries) until KiCad's DRC finds nothing unconnected and no shorts,
  # clearance errors or crossing tracks.
  try=1
  while :; do
    $JAVA java -jar freerouting.jar -de "$o/carrier-$v-placed.dsn" -do "$o/carrier-$v.ses" -mp 40 --gui.enabled=false \
      > "$o/freerouting.log" 2>&1 || { tail -20 "$o/freerouting.log"; exit 1; }
    $KICAD python3 build_board.py finish "$o/carrier-$v-placed.kicad_pcb" "$o/carrier-$v.ses" "$b" "$lay"
    # Freerouting sometimes leaves short stubs that end in mid-air: take them off, a layer at a time
    n=0
    while [ "$n" -lt 6 ] && ! $KICAD python3 build_board.py clean "$b" "$b" 2>/dev/null | grep -q "^removed 0 "; do n=$((n + 1)); done
    $KICAD kicad-cli pcb drc --severity-all -o "$o/drc.rpt" "$b" >/dev/null || true
    if grep -q "Found 0 unconnected pads" "$o/drc.rpt" &&
       ! grep -q -E "^\[(shorting_items|clearance|tracks_crossing|hole_clearance|copper_edge_clearance)\]" "$o/drc.rpt"; then
      break
    fi
    [ "$try" -ge 4 ] && { echo "$v: still failing DRC after $try routing tries; see $o/drc.rpt"; exit 1; }
    try=$((try + 1))
  done
  echo "$v: routed (try $try)"
  mkdir -p "$o/gerbers"
  $KICAD kicad-cli pcb export gerbers -l F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts,F.Paste,B.Paste \
    --subtract-soldermask -o "$o/gerbers/" "$b" >/dev/null
  $KICAD kicad-cli pcb export drill --format excellon --excellon-separate-th -o "$o/gerbers/" "$b" >/dev/null
  $KICAD python3 -c "import shutil,sys; shutil.make_archive(sys.argv[1], \"zip\", sys.argv[2])" "$o/carrier-$v-gerbers" "$o/gerbers"
  for side in top bottom; do
    $KICAD kicad-cli pcb render --side "$side" --width 1600 --height 1000 --quality high -o "$o/render-$side.png" "$b" >/dev/null
  done
  if [ "$parts" = smd ]; then
    $KICAD kicad-cli pcb export pos --format csv --units mm --side front --smd-only -o "$o/carrier-$v-pos.csv" "$b" >/dev/null
    $KICAD python3 jlc_files.py "$o/carrier-$v-placed-bom-raw.csv" "$o/carrier-$v-pos.csv" \
      "$o/carrier-$v-jlc-bom.csv" "$o/carrier-$v-jlc-cpl.csv"
  fi
done
