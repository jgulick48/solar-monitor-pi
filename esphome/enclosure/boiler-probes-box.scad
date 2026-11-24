// Enclosure for the boiler probe board: Waveshare ESP32-P4-ETH with its PoE module (esphome/boiler-probes.yaml).
// Print in PETG: box open side up, lid outside face down; 0.2 mm layers, 3 walls, no supports needed.
// Render one part at a time: set PART below, F6, then File > Export > STL.
//
// The board's sizes come from Waveshare's outline drawing (ESP32-P4-ETH.zip, outline PDF of 2025-12-19): 78 x 21 mm,
// 1.6 mm PCB, RJ45 16 wide x 13.3 high and 0.8 past the edge, USB-C 9 x 3.2, header pins 8.3 below the PCB, mounting
// holes at 22.3 and 76.5 from the RJ45 end and 18.2 apart across, BOOT and RESET 18 from the RJ45 end. The PoE
// module's size is NOT published: the values marked MEASURE are placeholders until it is measured. The layout follows the photo in images/poe_board.png:
// the PoE module (B) stacks on top, components down, on the 6-pin header behind the RJ45; header pins point down; the
// RESET button sits under the board's front long edge just behind the RJ45 (BOOT on the other edge).

PART = "box";              // "box", "lid", or "both" (preview only)

/* [Board, from Waveshare's drawing] */
board_l = 78.0;            // along the board, RJ45 end to USB-C end
board_w = 21.0;
pcb_t = 1.6;
rj45_w = 16.0;             // jack width across the board
rj45_y = 2.5;              // jack's near edge from the board's front (RESET) long edge: (21 - 16) / 2
rj45_h = 13.3;             // jack height above the PCB top
rj45_overhang = 0.8;       // how far the jack sticks out past the board edge
notch_below = 0.5;         // the RJ45-end notch goes this far below the PCB (MEASURE: more if anything underneath reaches that edge)
usbc_w = 9.0;              // USB-C receptacle width (centred on the board's short edge)
usbc_h = 3.2;              // receptacle height above the PCB top
// Mounting holes, from the RJ45 end (22.3, then 54.2 further) and 18.2 apart across the board.
hole_x = [22.3, 76.5];
hole_y_inset = 1.4;        // (21 - 18.2) / 2
hole_d = 2.6;              // MEASURE: hole diameter (the kit's standoffs are M2.5)

/* [Underneath: header pins] */
under_h = 9.0;             // the header pins reach 8.3 below the PCB; the standoffs are this tall

/* [PoE module (B), stacked on top] */
poe_x = 19.0;              // MEASURE: its RJ45-end edge, from the board's RJ45 edge
poe_l = 52.0;              // MEASURE: along the board
poe_w = 27.0;              // MEASURE: across; it overhangs the board's back edge (away from RESET)
poe_y = 0.0;               // MEASURE: its front edge, from the board's front long edge
poe_top = 14.0;            // MEASURE: from the main PCB's top to the PoE module's top
top_h = max(rj45_h, poe_top);   // tallest part above the PCB

/* [RESET and BOOT buttons: side-press, under the board's two long edges] */
reset_x = 18.0;            // button centre from the board's RJ45 edge (both buttons)
reset_z = -1.5;            // MEASURE: button centre height from the PCB bottom (negative: below the board)
reset_hole_d = 3.5;        // for a paperclip or a pen tip

/* [Terminal block for the two 1-Wire buses] */
tb_l = 32.0;               // e.g. two 3-pole 5.0 mm blocks side by side, or one 6-pole
tb_w = 10.0;
tb_hole_pitch = 25.0;      // two M3 screws holding the block (or glue it and set tb_screws = false)
tb_screws = true;
tb_bay_w = 22.0;           // space beside the board for the block, the 4.7 k resistors and the wires
grommet_d = 9.0;           // probe cables enter here (a 9 mm rubber grommet, or a PG7 gland with 12.5)
grommets = 2;              // one per bus

/* [OLED in the lid: SSD1306 0.96" I2C module, screen facing out through a window] */
oled = true;
oled_pcb = [27.3, 27.8];   // MEASURE: module board, across (x) and along the pins' direction (y)
oled_holes = [23.3, 23.8]; // MEASURE: mounting hole spacing (M2), centred on the board
oled_view = [23.0, 12.5];  // the window: the 21.7 x 10.9 mm active area plus a margin
oled_view_dy = -1.5;       // MEASURE: window centre from the board centre, away from the pin edge (+y = pin edge)
oled_glass_h = 1.8;        // MEASURE: from the module board's face to the glass front; the posts are this tall
oled_depth = 6.0;          // MEASURE: from the glass front to the back of the module and its wires
oled_pos = [0, 0];         // centre of the module board, from the lid's centre (0, 0 = middle, see below)

/* [Box] */
wall = 2.4;                // 3 perimeters at 0.4 mm nozzle x 2
floor_t = 2.0;
clear = 1.0;               // around the board
lid_t = 2.0;
lip_h = 3.0;               // lid lip that drops inside the walls
fit = 0.25;                // gap between lid lip and walls (PETG; 0.15 for PLA)
corner_r = 2.0;
lid_screw_d = 2.5;         // pilot for M3 self-tapping screws into the corner bosses
lid_screw_head_d = 6.0;
boss_d = 7.0;
vent = true;               // slots in the lid over the PoE module

/* [Screw tabs] */
tab_l = 14.0;
tab_t = 3.0;
tab_slot_w = 4.5;          // #8 / 4 mm wood screws
tab_slot_l = 8.0;

$fn = 48;

// ----------------------------------------------------------------- derived sizes
// The RJ45 jack's front is flush with the outside of its end wall, so its latch can be pressed: the board's end
// sits in a notch in that wall. The lid-screw bosses fill the inside corners; the board runs beside the two front
// ones (0.5 mm spare) so it can reach the end wall.
board_x0 = rj45_overhang;                   // board's RJ45 edge, from the box's outer RJ45-end face
front_gap = boss_d + 0.5;
out_l = board_x0 + board_l + clear + wall;
in_l = out_l - 2 * wall;
in_w = front_gap + board_w + clear + tb_bay_w;
in_h = under_h + pcb_t + top_h + 2 + (oled ? oled_depth : 0); // 2 mm above the jack, plus the OLED hanging from the lid
out_w = in_w + 2 * wall;
out_h = in_h + floor_t;

board_y0 = wall + front_gap;                // board's bottom long edge
notch = [board_y0 - 0.5, board_y0 + board_w + 0.5];          // the notch's span across the end wall
pcb_z = floor_t + under_h;                  // PCB bottom

module rbox(l, w, h, r) {
    hull() for (x = [r, l - r], y = [r, w - r]) translate([x, y, 0]) cylinder(r = r, h = h);
}

module standoff(h) {
    difference() {
        cylinder(d = hole_d + 3.2, h = h);
        cylinder(d = hole_d - 0.5, h = h + 1);            // self-tapping M2.5 pilot
    }
}

module box() {
    difference() {
        union() {
            difference() {
                rbox(out_l, out_w, out_h, corner_r);
                translate([wall, wall, floor_t]) rbox(in_l, in_w, in_h + 1, max(corner_r - wall, 0.5));
            }
            // corner bosses for the lid screws
            for (x = [wall + boss_d / 2, out_l - wall - boss_d / 2], y = [wall + boss_d / 2, out_w - wall - boss_d / 2])
                translate([x, y, 0]) difference() {
                    cylinder(d = boss_d, h = out_h - lip_h - fit);
                    translate([0, 0, floor_t]) cylinder(d = lid_screw_d, h = out_h);
                }
            // board standoffs
            for (x = hole_x, y = [hole_y_inset, board_w - hole_y_inset])
                translate([board_x0 + x, board_y0 + y, floor_t - 0.01]) standoff(under_h + 0.01);
            // terminal block bosses
            if (tb_screws) for (dx = [-tb_hole_pitch / 2, tb_hole_pitch / 2])
                translate([out_l / 2 + dx, board_y0 + board_w + clear + tb_bay_w / 2, floor_t - 0.01])
                    difference() { cylinder(d = 7, h = 4); cylinder(d = 2.5, h = 5); }
            // screw tabs on both ends
            for (end = [0, 1]) translate([end ? out_l - corner_r : -tab_l, 0, 0]) difference() {
                rbox(tab_l + corner_r, out_w, tab_t, corner_r);
                translate([end ? corner_r + tab_l / 2 : tab_l / 2, out_w / 2, -1])
                    hull() for (dy = [-tab_slot_l / 2, tab_slot_l / 2]) translate([0, dy, 0]) cylinder(d = tab_slot_w, h = tab_t + 2);
            }
        }
        // RJ45 end: a notch open to the top, as wide as the board, from just below the PCB; the board drops in
        // with the jack's front flush with the outside. The lid's tongue closes it above the PCB.
        translate([-1, notch[0], pcb_z - notch_below]) cube([wall + 2, notch[1] - notch[0], out_h]);
        // USB-C opening (for flashing), with room for the plug's overmould
        translate([out_l - wall - 1, board_y0 + board_w / 2 - (usbc_w + 3) / 2, pcb_z + pcb_t - 1.5])
            cube([wall + 2, usbc_w + 3, usbc_h + 3]);
        // grommet holes in the long wall beside the terminal block
        for (i = [0 : grommets - 1])
            translate([out_l / 2 + (i - (grommets - 1) / 2) * (grommet_d + 8), out_w + 1, floor_t + 4 + grommet_d / 2])
                rotate([90, 0, 0]) cylinder(d = grommet_d, h = wall + 2);
        // RESET: a hole through the front wall in line with the button, countersunk to guide a paperclip, labelled
        translate([board_x0 + reset_x, -1, pcb_z + reset_z]) rotate([-90, 0, 0]) {
            cylinder(d = reset_hole_d, h = wall + 2);
            cylinder(d1 = reset_hole_d + 3, d2 = reset_hole_d, h = 2.2);
        }
        translate([board_x0 + reset_x + reset_hole_d / 2 + 2.5, 0.6, pcb_z + reset_z]) rotate([90, 0, 0])
            linear_extrude(1) text("RESET", size = 3, halign = "left", valign = "center");
        // BOOT, the same through the back wall (hold it while pressing RESET or plugging in, to flash over USB)
        translate([board_x0 + reset_x, out_w + 1, pcb_z + reset_z]) rotate([90, 0, 0]) {
            cylinder(d = reset_hole_d, h = wall + 2);
            cylinder(d1 = reset_hole_d + 3, d2 = reset_hole_d, h = 2.2);
        }
        translate([board_x0 + reset_x - reset_hole_d / 2 - 2.5, out_w - 0.6, pcb_z + reset_z]) rotate([90, 0, 180])
            linear_extrude(1) text("BOOT", size = 3, halign = "left", valign = "center");
        // label engraved 0.6 mm into the front face
        translate([out_l / 2, 0.6, out_h - 6]) rotate([90, 0, 0])
            linear_extrude(1) text("BOILER PROBES", size = 4, halign = "center", valign = "center");
    }
}

// The OLED's centre on the lid: over the board toward the USB-C end, with its pin edge toward the back (+y).
oled_c = [out_l - wall - 4 - oled_pcb[0] / 2 + oled_pos[0], out_w / 2 + oled_pos[1]];

module lid() {
    difference() {
        union() {
            rbox(out_l, out_w, lid_t, corner_r);
            // lip inside the walls, clear of the corner bosses
            translate([wall + fit, wall + fit, lid_t - 0.01]) difference() {
                rbox(in_l - 2 * fit, in_w - 2 * fit, lip_h, max(corner_r - wall, 0.5));
                translate([1.6, 1.6, -1]) rbox(in_l - 2 * fit - 3.2, in_w - 2 * fit - 3.2, lip_h + 2, 0.5);
                for (x = [boss_d / 2 - fit, in_l - fit - boss_d / 2], y = [boss_d / 2 - fit, in_w - fit - boss_d / 2])
                    translate([x, y, -1]) cylinder(d = boss_d + 1, h = lip_h + 2);     // clear of the bosses
                translate([-1, notch[0] - wall - fit - 1, -1]) cube([3.2, notch[1] - notch[0] + 2, lip_h + 2]);  // over the jack
            }
            // tongue that closes the RJ45-end notch above the PCB, around the jack (lid coordinates: z up from
            // its outside face; fitted, a height z here is at out_h + lid_t - z in the box)
            jack_top = pcb_z + pcb_t + rj45_h + 0.4;
            difference() {
                translate([0, notch[0] + fit, lid_t - 0.01]) cube([wall, notch[1] - notch[0] - 2 * fit, out_h - (pcb_z + pcb_t + 0.3) + 0.01]);
                translate([-1, board_y0 + rj45_y - 0.4, out_h + lid_t - jack_top]) cube([wall + 2, rj45_w + 0.8, out_h]);
            }
        }
        for (x = [wall + boss_d / 2, out_l - wall - boss_d / 2], y = [wall + boss_d / 2, out_w - wall - boss_d / 2])
            translate([x, y, -1]) {
                cylinder(d = 3.4, h = lid_t + lip_h + 2);
                cylinder(d1 = lid_screw_head_d, d2 = 3.4, h = 1.6);    // countersink on the outside face
            }
        if (vent)                                               // over the PoE module, which makes the most heat,
            for (i = [0 : floor((poe_l - 8) / 4)])               // and clear of the OLED
                let (x = board_x0 + poe_x + 4 + i * 4)
                if (!oled || x + 2 < oled_c[0] - oled_pcb[0] / 2 - 2 || x > oled_c[0] + oled_pcb[0] / 2 + 2)
                    translate([x, board_y0 + poe_y + 3, -1]) cube([2, poe_w - 6, lid_t + 2]);
        if (oled) {                                             // the window, chamfered 45 degrees on the outside
            c = [oled_c[0], oled_c[1] + oled_view_dy];
            translate([c[0] - oled_view[0] / 2, c[1] - oled_view[1] / 2, -1]) cube([oled_view[0], oled_view[1], lid_t + 2]);
            hull() {
                translate([c[0] - oled_view[0] / 2, c[1] - oled_view[1] / 2, lid_t / 2]) cube([oled_view[0], oled_view[1], 0.01]);
                translate([c[0] - oled_view[0] / 2 - lid_t / 2 - 0.5, c[1] - oled_view[1] / 2 - lid_t / 2 - 0.5, -0.01])
                    cube([oled_view[0] + lid_t + 1, oled_view[1] + lid_t + 1, 0.01]);
            }
        }
    }
    // OLED posts: the module's glass rests just behind the window, its board on these with M2 self-tapping screws
    if (oled)
        for (dx = [-oled_holes[0] / 2, oled_holes[0] / 2], dy = [-oled_holes[1] / 2, oled_holes[1] / 2])
            translate([oled_c[0] + dx, oled_c[1] + dy, lid_t - 0.01]) difference() {
                cylinder(d = 4.2, h = oled_glass_h + 0.01);
                cylinder(d = 1.7, h = oled_glass_h + 1);
            }
}

// The board, its jacks and the space under it, see-through in the preview (F5) only: never part of an STL.
module board_ghost() {
    %translate([board_x0, board_y0, 0]) {
        translate([0, 0, floor_t]) cube([board_l, board_w, under_h]);                              // header pins
        translate([poe_x, poe_y, pcb_z + pcb_t]) cube([poe_l, poe_w, poe_top]);                    // PoE module on top
        translate([reset_x - 2, -1, pcb_z + reset_z - 1.5]) cube([4, 1.5, 3]);                    // RESET button
        translate([reset_x - 2, board_w - 0.5, pcb_z + reset_z - 1.5]) cube([4, 1.5, 3]);         // BOOT button
        translate([0, 0, pcb_z]) cube([board_l, board_w, pcb_t]);                                  // PCB
        translate([-rj45_overhang, rj45_y, pcb_z + pcb_t]) cube([22.26 - 2, rj45_w, rj45_h]);     // RJ45 jack
        translate([board_l - 7.5, (board_w - usbc_w) / 2, pcb_z + pcb_t]) cube([7.5 + 0.5, usbc_w, usbc_h]);  // USB-C
    }
}

if (PART == "box") { box(); board_ghost(); }
else if (PART == "lid") lid();
else { box(); board_ghost(); translate([0, 0, out_h + 20]) mirror([0, 0, 1]) translate([0, 0, -lid_t]) lid(); }

echo(str("outside: ", out_l, " x ", out_w, " x ", out_h, " mm (plus ", tab_l, " mm tabs each end); inside height ", in_h));
