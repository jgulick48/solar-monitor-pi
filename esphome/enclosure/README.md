# Enclosure for the boiler probe board

A 3D-printed box for the **Waveshare ESP32-P4-ETH with its PoE module** (the board in `../boiler-probes.yaml`):
the board on four standoffs, the RJ45 jack and USB-C (for flashing) through the end walls, a bay beside the board
for the probe buses' screw terminal block and the 4.7 kΩ pull-ups, two grommet holes for the probe cables, screw
tabs for the wall, and a screwed lid with vents over the PoE end.

> **Not ready to print yet.** Waveshare's drawing gives the board's outline (78.0 × 21.0 mm), the RJ45 and USB-C
> positions and the mounting holes along the board, but not the heights or the PoE module. The sizes marked
> `MEASURE` in `boiler-probes-box.scad` are placeholders until the real board is measured. The STLs here are
> rendered from those placeholders.

| File | What |
| --- | --- |
| `boiler-probes-box.scad` | The model (OpenSCAD). Every size is a parameter at the top. |
| `box.stl`, `lid.stl` | The two parts, rendered from the `.scad` with the current parameters |
| `images/` | Previews of the same render |

Outside: 99.8 × 49.8 × 31.1 mm, plus a 14 mm screw tab on each end.

![Box and lid](images/both-iso.png)

| Box: RJ45 end and grommets | Box: USB-C end and label | Lid: lip side | Lid: outside |
| --- | --- | --- | --- |
| ![](images/box-rj45.png) | ![](images/box-usb.png) | ![](images/lid-lip.png) | ![](images/lid-under.png) |

## Still to measure

| Parameter | Now | What to measure |
| --- | --- | --- |
| `under_h` | 12 mm | PCB bottom to the lowest point underneath (the PoE module, or header pins); the standoffs are this tall |
| `rj45_h` | 13.5 mm | RJ45 jack height above the PCB top |
| `rj45_overhang` | 1.0 mm | How far the jack sticks out past the board's edge |
| `hole_d`, `hole_y_inset` | 2.6 mm, 1.2 mm | Mounting hole diameter, and its centre's distance from the long edge |
| `pcb_t` | 1.6 mm | Board thickness |
| PoE module | not modelled | Above or below the board, its footprint and height |

## Printing

- **PETG** (it sits in a warm boiler room; PLA softens at about 55 °C). For PLA, set `fit = 0.15`.
- 0.2 mm layers, 3 perimeters, 20% infill, no supports.
- Box: open side up. Lid: outside face down (the lip up).
- Hardware: 4 × M2.5 self-tapping screws for the board, 4 × M3 × 8 countersunk self-tapping screws for the lid,
  2 × M3 for the terminal block (or glue it and set `tb_screws = false`), 2 × 9 mm grommets (or PG7 cable glands
  with `grommet_d = 12.5`), and 2 × 4 mm screws through the tabs.
- A cheap fit check first: print only the RJ45 end (cut the STL in the slicer about 10 mm from that end).

## Rendering

In OpenSCAD: open the `.scad`, set `PART` to `"box"` or `"lid"` at the top, render (F6) and export STL. Without
OpenSCAD installed, the official image works:

```
docker run --rm -v "$PWD":/w -w /w openscad/openscad:dev \
  openscad -D 'PART="box"' --backend=manifold -o box.stl boiler-probes-box.scad
```

Add `--render --imgsize=1400,900 --viewall --autocenter --camera=0,0,0,35,0,210,0 -o box.png` for a preview image.
