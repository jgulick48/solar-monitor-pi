# Enclosure for the boiler probe board

A 3D-printed box for the **Waveshare ESP32-P4-ETH with its PoE module** (the board in `../boiler-probes.yaml`):
the board on four standoffs with the RJ45 jack's front flush with the outside of its end wall (so the latch can be
pressed to unplug it), USB-C (for flashing) through the other end wall, a bay beside the board
for the probe buses' screw terminal block and the 4.7 kΩ pull-ups, two grommet holes for the probe cables, a
labelled hole in the front wall for pressing RESET with a paperclip, screw tabs for the wall, and a screwed lid with
vents over the PoE module.

The layout follows the photo of the board, `images/poe_board.png`. The PoE module (B) stacks **on top**,
components down, plugged into the 6-pin header just behind the RJ45, and overhangs the board's back edge. The header
pins point down. RESET is a side-press button under the board's front long edge just behind the RJ45, and BOOT is
on the other edge.

![The board with the PoE module fitted, and the two apart](images/poe_board.png)

> **Not ready to print yet.** Waveshare's drawing gives the board's outline (78.0 × 21.0 mm), the RJ45 and USB-C
> positions and the mounting holes along the board, but not the heights, the PoE module's size or the RESET button's position. The sizes marked
> `MEASURE` in `boiler-probes-box.scad` are placeholders until the real board is measured. The STLs here are
> rendered from those placeholders.

| File | What |
| --- | --- |
| `boiler-probes-box.scad` | The model (OpenSCAD). Every size is a parameter at the top. |
| `box.stl`, `lid.stl` | The two parts, rendered from the `.scad` with the current parameters |
| `images/` | Previews of the same render |

Outside: 82.4 × 56.3 × 28.6 mm, plus a 14 mm screw tab on each end. In the box previews the board, its jacks and the
space under it show see-through (OpenSCAD's preview only; they're never in the STL), to check that nothing is in the way.

![Box and lid](images/both-iso.png)

| Box: RJ45 end, flush | Box: front, RESET | Box: inside and grommets | Box: USB-C end | Lid: lip side and tongue | Lid: outside |
| --- | --- | --- | --- | --- | --- |
| ![](images/box-rj45-face.png) | ![](images/box-front.png) | ![](images/box-rj45.png) | ![](images/box-usb.png) | ![](images/lid-lip.png) | ![](images/lid-under.png) |

**RJ45 end.** The board's end sits in a notch in the end wall, open to the top and as wide as the board, from just
below the PCB: the board drops in from above and the jack's front ends flush with the outside. The lid has a tongue
that closes the notch above the PCB around the jack (two fingers beside it and a bar over it), and its lip is cut
away over the jack. The lid-screw posts fill the inside corners and the board runs beside the two front ones.

## Still to measure

| Parameter | Now | What to measure |
| --- | --- | --- |
| `under_h` | 9 mm | PCB bottom to the header pins' tips; the standoffs are this tall |
| `rj45_h` | 13.5 mm | RJ45 jack height above the PCB top |
| `rj45_overhang` | 1.0 mm | How far the jack sticks out past the board's edge (sets where the board sits, so the jack ends flush) |
| `notch_below` | 0.5 mm | How far below the PCB the RJ45-end notch goes: more if the PoE module or anything underneath reaches that edge |
| `hole_d`, `hole_y_inset` | 2.6 mm, 1.2 mm | Mounting hole diameter, and its centre's distance from the long edge |
| `pcb_t` | 1.6 mm | Board thickness |
| `poe_x`, `poe_l`, `poe_w`, `poe_y` | 19, 52, 27, 0 mm | The PoE module's footprint: where it starts from the RJ45 end, its length, its width, and its front edge from the board's front edge |
| `poe_top` | 14 mm | Main PCB top to the PoE module's top |
| `reset_x`, `reset_z` | 18 mm, −1.5 mm | The RESET button's centre: from the board's RJ45 end, and its height from the PCB bottom (negative is below) |

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
