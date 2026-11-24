// Enclosure for the boiler probe board: Waveshare ESP32-P4-ETH with its PoE module (esphome/boiler-probes.yaml).
// Print in PETG: box open side up, lid outside face down; 0.2 mm layers, 3 walls, no supports needed.
// Render one part at a time: set PART below, F6, then File > Export > STL.
//
// Board outline, RJ45 and USB-C positions come from Waveshare's dimension drawing (ESP32-P4-ETH-details-size.jpg,
// 78.00 x 21.0 mm). Heights and the PoE module are NOT published: the values marked MEASURE are placeholders to be
// replaced with measurements of the real board before printing.

PART = "box";              // "box", "lid", or "both" (preview only)

/* [Board, from Waveshare's drawing] */
board_l = 78.0;            // along the board, RJ45 end to USB-C end
board_w = 21.0;
pcb_t = 1.6;               // MEASURE (usual 1.6)
rj45_w = 16.0;             // jack width across the board
rj45_y = 2.45;             // jack's near edge from the board's bottom long edge
rj45_h = 13.5;             // MEASURE: jack height above the PCB top
rj45_overhang = 1.0;       // MEASURE: how far the jack sticks out past the board edge
usbc_w = 9.0;              // USB-C receptacle width (centred on the board's short edge)
usbc_h = 3.3;              // receptacle height above the PCB top
// Mounting holes, from the RJ45 end and the bottom long edge (drawing: 22.26 and 78 - 4.67 = 73.33 along).
hole_x = [22.26, 73.33];
hole_y_inset = 1.2;        // MEASURE: hole centre from the long edge
hole_d = 2.6;              // MEASURE: hole diameter (the kit's standoffs are M2.5)

/* [Underneath: PoE module and header pins] */
under_h = 12.0;            // MEASURE: from the PCB bottom to the lowest point (PoE module, pins); standoffs this tall
top_h = rj45_h;            // tallest part above the PCB (the RJ45 jack)

/* [Terminal block for the two 1-Wire buses] */
tb_l = 32.0;               // e.g. two 3-pole 5.0 mm blocks side by side, or one 6-pole
tb_w = 10.0;
tb_hole_pitch = 25.0;      // two M3 screws holding the block (or glue it and set tb_screws = false)
tb_screws = true;
tb_bay_w = 22.0;           // space beside the board for the block, the 4.7 k resistors and the wires
grommet_d = 9.0;           // probe cables enter here (a 9 mm rubber grommet, or a PG7 gland with 12.5)
grommets = 2;              // one per bus

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
in_l = board_l + 2 * clear + rj45_overhang + 2 * boss_d;    // bosses sit in the corners, clear of the board
// The lid-screw bosses fill the inside corners. The board sits far enough from the front wall that the RJ45
// opening starts past the front boss (0.5 mm spare), so a plug goes straight in.
front_gap = max(clear, boss_d + 0.5 + 0.6 - rj45_y);
in_w = front_gap + board_w + clear + tb_bay_w;
in_h = under_h + pcb_t + top_h + 2;                          // 2 mm above the jack
out_l = in_l + 2 * wall;
out_w = in_w + 2 * wall;
out_h = in_h + floor_t;

board_x0 = wall + boss_d + rj45_overhang;   // board's RJ45 edge, from the box's outer RJ45-end face
board_y0 = wall + front_gap;                // board's bottom long edge
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
        // RJ45 opening in the end wall, 0.6 mm clearance all round
        translate([-1, board_y0 + rj45_y - 0.6, pcb_z + pcb_t - 0.6])
            cube([wall + 2, rj45_w + 1.2, rj45_h + 1.2]);
        // USB-C opening (for flashing), with room for the plug's overmould
        translate([out_l - wall - 1, board_y0 + board_w / 2 - (usbc_w + 3) / 2, pcb_z + pcb_t - 1.5])
            cube([wall + 2, usbc_w + 3, usbc_h + 3]);
        // grommet holes in the long wall beside the terminal block
        for (i = [0 : grommets - 1])
            translate([out_l / 2 + (i - (grommets - 1) / 2) * (grommet_d + 8), out_w + 1, floor_t + 4 + grommet_d / 2])
                rotate([90, 0, 0]) cylinder(d = grommet_d, h = wall + 2);
        // label engraved 0.6 mm into the front face
        translate([out_l / 2, 0.6, out_h - 6]) rotate([90, 0, 0])
            linear_extrude(1) text("BOILER PROBES", size = 4, halign = "center", valign = "center");
    }
}

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
            }
        }
        for (x = [wall + boss_d / 2, out_l - wall - boss_d / 2], y = [wall + boss_d / 2, out_w - wall - boss_d / 2])
            translate([x, y, -1]) {
                cylinder(d = 3.4, h = lid_t + lip_h + 2);
                cylinder(d1 = lid_screw_head_d, d2 = 3.4, h = 1.6);    // countersink on the outside face
            }
        if (vent)                                               // over the board's RJ45 / PoE end
            for (i = [0 : 5]) translate([board_x0 + 4 + i * 4, board_y0 + 3, -1]) cube([2, board_w - 6, lid_t + 2]);
    }
}

// The board, its jacks and the space under it, see-through in the preview (F5) only: never part of an STL.
module board_ghost() {
    %translate([board_x0, board_y0, 0]) {
        translate([0, 0, floor_t]) cube([board_l, board_w, under_h]);                              // PoE module, pins
        translate([0, 0, pcb_z]) cube([board_l, board_w, pcb_t]);                                  // PCB
        translate([-rj45_overhang, rj45_y, pcb_z + pcb_t]) cube([22.26 - 2, rj45_w, rj45_h]);     // RJ45 jack
        translate([board_l - 7.5, (board_w - usbc_w) / 2, pcb_z + pcb_t]) cube([7.5 + 0.5, usbc_w, usbc_h]);  // USB-C
    }
}

if (PART == "box") { box(); board_ghost(); }
else if (PART == "lid") lid();
else { box(); board_ghost(); translate([0, 0, out_h + 20]) mirror([0, 0, 1]) translate([0, 0, -lid_t]) lid(); }

echo(str("outside: ", out_l, " x ", out_w, " x ", out_h, " mm (plus ", tab_l, " mm tabs each end); inside height ", in_h));
