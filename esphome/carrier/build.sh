#!/bin/sh
# Builds both carrier boards (THT and SMD) in Docker: placement, Freerouting, ground pours, DRC, fabrication files.
# Needs Docker and freerouting.jar (https://github.com/freerouting/freerouting/releases, v2.1.0) in this folder.
# Output: fab/<variant>/ with the KiCad board, Gerbers + drill (zipped for the PCB maker), DRC report, renders,
# and for SMD the JLCPCB BOM and placement files.
set -eu
cd "$(dirname "$0")"
KICAD="docker run --rm -u $(id -u):$(id -g) -e HOME=/w -v $PWD:/w -w /w kicad/kicad:9.0"
JAVA="docker run --rm -u $(id -u):$(id -g) -e HOME=/w -v $PWD:/w -w /w eclipse-temurin:21-jre"
for v in ${*:-tht smd}; do
  o=fab/$v; rm -rf "$o"; mkdir -p "$o"
  $KICAD python3 build_board.py place "$v" "$o/carrier-$v-placed.kicad_pcb"
  b=$o/carrier-$v.kicad_pcb
  # Freerouting's result differs from run to run, and now and then a route doesn't survive the import into KiCad:
  # route again (up to 4 tries) until KiCad's DRC finds nothing unconnected.
  try=1
  while :; do
    $JAVA java -jar freerouting.jar -de "$o/carrier-$v-placed.dsn" -do "$o/carrier-$v.ses" -mp 40 --gui.enabled=false \
      > "$o/freerouting.log" 2>&1 || { tail -20 "$o/freerouting.log"; exit 1; }
    $KICAD python3 build_board.py finish "$o/carrier-$v-placed.kicad_pcb" "$o/carrier-$v.ses" "$b"
    $KICAD kicad-cli pcb drc --severity-all -o "$o/drc.rpt" "$b" >/dev/null || true
    grep -q "Found 0 unconnected pads" "$o/drc.rpt" && break
    [ "$try" -ge 4 ] && { echo "$v: still unconnected after $try routing tries; see $o/drc.rpt"; exit 1; }
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
  if [ "$v" = smd ]; then
    $KICAD kicad-cli pcb export pos --format csv --units mm --side front --smd-only -o "$o/carrier-smd-pos.csv" "$b" >/dev/null
    $KICAD python3 jlc_files.py "$o/carrier-smd-placed-bom-raw.csv" "$o/carrier-smd-pos.csv" \
      "$o/carrier-smd-jlc-bom.csv" "$o/carrier-smd-jlc-cpl.csv"
  fi
done
