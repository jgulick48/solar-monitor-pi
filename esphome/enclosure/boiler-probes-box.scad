// Enclosure for the boiler probe board: the carrier PCB (../carrier/) with the Waveshare ESP32-P4-ETH and its PoE
// module plugged into it (esphome/boiler-probes.yaml).
//
// Two parts, split at the top of the port windows: the BASE (floor, standoffs, lower walls; the port windows, the
// RJ45 notch and the USB tunnel are slots open at the top, so the carrier drops straight in with its plugs) and the
// COVER (upper walls and top in one piece: OLED window, vents, labels). Four ears outside the front and back walls
// hold them together with M3 screws from the top.
// Print in PETG, 0.2 mm layers, 3 walls, no supports: base open side up, cover top face down.
// Render one part at a time: set PART below, F6, then File > Export > STL.
//
// The ESP32 board's sizes come from Waveshare's outline drawing (ESP32-P4-ETH.zip, outline PDF of 2025-12-19): 78 x 21
// mm, 1.6 mm PCB, RJ45 16 wide x 13.3 high and 0.8 past the edge, USB-C 9 x 3.2, BOOT and RESET 18 from the RJ45 end.
// The carrier's come from ../carrier/build_board.py. Values marked MEASURE are placeholders until measured.
//
// Box coordinates: x from the outside of the RJ45 end wall, y from the outside of the front (RESET side) wall, z up
// from the bottom. The carrier's front (P1-P4, RESET) is toward y = 0, as on its silkscreen.

PART = "base";             // "base", "cover", or "both" (preview only)

/* [Carrier PCB, from ../carrier/build_board.py] */
car_l = 100.0;
car_w = 60.0;
car_t = 1.6;
car_holes = [[4, 4], [4, 56], [96, 24.5], [96, 35.5]];   // M3, in carrier coordinates (x, y from the front edge)
car_under = 5.0;           // standoff height: room for the through-hole pins under the carrier
// The ESP board's RJ45 edge sits at carrier x = -1.9, its front edge at carrier y = 19.5 (centred, 21 wide)
esp_x = -1.9;
esp_y = 19.5;
// Probe ports: 4 along the front edge, 4 along the back, centred at these carrier x; CT and HEAT on the right edge
port_x = [32, 48, 64, 80];
term_w = 15.6;             // a 3-pin 5.08 mm pluggable header's width
term_h = 13.0;             // MEASURE: the mated plug's height above the carrier
term_out = 0.6;            // the header's face, past the carrier's edge
ct_y = [45.5, 55.7];       // CT block's pins, carrier y (front edge = 0): CT2 .. CT1
heat_y = [4.8, 15.0];      // HEAT block's pins, carrier y

/* [ESP32-P4-ETH, from Waveshare's drawing] */
board_l = 78.0;
board_w = 21.0;
pcb_t = 1.6;
sock_h = 8.5 + 2.5;        // MEASURE: female header 8.5 mm + the male header's spacer: the ESP board's bottom above the carrier
rj45_w = 16.0;
rj45_y = 2.5;              // from the ESP board's front edge
rj45_h = 13.3;
rj45_overhang = 0.8;
usbc_w = 9.0;
usbc_h = 3.2;

/* [PoE module (B), stacked on the ESP board] */
poe_x = 19.0;              // MEASURE: its RJ45-end edge, from the ESP board's RJ45 edge
poe_l = 52.0;              // MEASURE
poe_w = 27.0;              // MEASURE: it overhangs the ESP board's back edge
poe_y = 0.0;               // MEASURE: its front edge, from the ESP board's front edge
poe_top = 14.0;            // MEASURE: above the ESP board's top
top_h = max(rj45_h, poe_top);

/* [RESET and BOOT: side-press buttons under the ESP board's long edges] */
btn_x = 18.0;              // from the ESP board's RJ45 edge
btn_z = -1.5;              // MEASURE: centre height from the ESP board's bottom
btn_hole_d = 3.5;

/* [OLED in the top: SSD1306 0.96" I2C module, screen facing out through a window] */
oled = true;
oled_pcb = [27.3, 27.8];   // MEASURE: module board, across (x) and along the pins' direction (y)
oled_holes = [23.3, 23.8]; // MEASURE: mounting hole spacing (M2), centred on the board
oled_view = [23.0, 12.5];  // the window: the 21.7 x 10.9 mm active area plus a margin
oled_view_dy = -1.5;       // MEASURE: window centre from the board centre, away from the pin edge (+y = pin edge)
oled_glass_h = 1.8;        // MEASURE: from the module board's face to the glass front; the posts are this tall
oled_depth = 6.0;          // MEASURE: from the glass front to the back of the module and its wires
oled_pos = [0, 0];         // offset of the module from its default place (toward the USB-C end, centred across)

/* [Box] */
wall = 2.4;                // 3 perimeters at 0.4 mm nozzle x 2
floor_t = 2.0;
top_t = 2.0;
gap = 0.3;                 // carrier to the inside of the walls
corner_r = 2.0;
vent = true;               // slots in the top over the PoE module

/* [Ears joining base and cover: M3 x 12 self-tapping screws from the top] */
ear_w = 10.0;              // along the wall
ear_p = 8.0;               // out from the wall
ear_t = [6.0, 4.0];        // thickness: base ear (the screw bites into it), cover ear
ear_x = 6.0;               // ear centres from each end (clear of the RESET and BOOT labels)
ear_hole = [2.5, 3.4];     // base pilot, cover clearance
ear_head_d = 7.0;          // bore in the cover ear's gusset, for the screw head and driver

/* [Screw tabs] */
tab_l = 14.0;
tab_t = 3.0;
tab_slot_w = 4.5;          // #8 / 4 mm wood screws
tab_slot_l = 8.0;

$fn = 48;

// ----------------------------------------------------------------- derived sizes
car_x0 = rj45_overhang - esp_x;            // the carrier's RJ45 edge: the jack ends flush with the outside
car_y0 = wall + gap;
car_z = floor_t + car_under;               // carrier bottom
esp_x0 = car_x0 + esp_x;                   // the ESP board's RJ45 edge (= rj45_overhang)
esp_y0 = car_y0 + esp_y;
esp_z = car_z + car_t + sock_h;            // the ESP board's bottom
out_l = car_x0 + car_l + gap + wall;
out_w = car_y0 + car_w + gap + wall;
in_l = out_l - 2 * wall;
in_w = out_w - 2 * wall;
in_h = esp_z + pcb_t + top_h + 2 + (oled ? oled_depth : 0) - floor_t;
out_h = floor_t + in_h + top_t;
term_z = [car_z + car_t - 0.5, car_z + car_t + term_h + 0.5]; // port windows, bottom and top
split = term_z[1];                         // base below, cover above
btn_xb = esp_x0 + btn_x;
btn_zb = esp_z + btn_z;
jack_top = esp_z + pcb_t + rj45_h + 0.4;
oled_c = [out_l - wall - 4 - oled_pcb[0] / 2 + oled_pos[0], out_w / 2 + oled_pos[1]];
ears = [for (x = [ear_x, out_l - ear_x], s = [-1, 1]) [x, s]];   // s = -1: front wall, +1: back wall

module rbox(l, w, h, r) {
    hull() for (x = [r, l - r], y = [r, w - r]) translate([x, y, 0]) cylinder(r = r, h = h);
}

module engrave(s, x, y, z, side, size = 3.5) {
    // text 0.6 mm into a wall's outside face; side: "front" (y = 0), "back" (y = out_w), "right" (x = out_l)
    if (side == "front") translate([x, 0.6, z]) rotate([90, 0, 0]) linear_extrude(1) text(s, size = size, halign = "center", valign = "center");
    if (side == "back") translate([x, out_w - 0.6, z]) rotate([90, 0, 180]) linear_extrude(1) text(s, size = size, halign = "center", valign = "center");
    if (side == "right") translate([out_l - 0.6, y, z]) rotate([90, 0, 90]) linear_extrude(1) text(s, size = size, halign = "center", valign = "center");
}

module button_hole(side) {
    y = side == "front" ? -1 : out_w + 1;
    translate([btn_xb, y, btn_zb]) rotate([side == "front" ? -90 : 90, 0, 0]) {
        cylinder(d = btn_hole_d, h = wall + 2);
        cylinder(d1 = btn_hole_d + 3, d2 = btn_hole_d, h = 2.2);              // countersink guides a paperclip
    }
}

// The whole box as one closed shell with every opening; base() and cover() are its two halves.
module shell() {
    difference() {
        union() {
            difference() {
                rbox(out_l, out_w, out_h, corner_r);
                translate([wall, wall, floor_t]) rbox(in_l, in_w, in_h, max(corner_r - wall, 0.5));
            }
            // carrier standoffs, M3 self-tapping
            for (h = car_holes) translate([car_x0 + h[0], car_y0 + h[1], floor_t - 0.01]) difference() {
                cylinder(d = 6.5, h = car_under + 0.01);
                cylinder(d = 2.6, h = car_under + 1);
            }
            // OLED posts under the top: the module's glass rests just behind the window (M2 self-tapping screws)
            if (oled)
                for (dx = [-oled_holes[0] / 2, oled_holes[0] / 2], dy = [-oled_holes[1] / 2, oled_holes[1] / 2])
                    translate([oled_c[0] + dx, oled_c[1] + dy, out_h - top_t - oled_glass_h]) difference() {
                        cylinder(d = 4.2, h = oled_glass_h + 0.01);
                        translate([0, 0, -1]) cylinder(d = 1.7, h = oled_glass_h + 1);
                    }
        }
        // RJ45 end: below the split, a slot as wide as the ESP board from just below it; above, a U around the jack
        // (open at the cover's bottom edge). The jack's front is flush with the outside.
        translate([-1, esp_y0 - 0.5, esp_z - 0.5]) cube([wall + 2, board_w + 1, split - esp_z + 0.5]);
        translate([-1, esp_y0 + rj45_y - 0.4, esp_z]) cube([wall + 2, rj45_w + 0.8, jack_top - esp_z]);
        // probe port windows: one along the front (P1-P4) and one along the back (P5-P8), open to the split
        px = [car_x0 + port_x[0] - term_w / 2 - 0.5, car_x0 + port_x[3] + term_w / 2 + 0.5];
        for (y = [-1, out_w - wall - 1])
            translate([px[0], y, term_z[0]]) cube([px[1] - px[0], wall + 2, split - term_z[0] + 0.01]);
        // right end: CT and HEAT windows, and a tunnel for a USB-C plug (for flashing; the jack is 26 mm in)
        for (r = [ct_y, heat_y])
            translate([out_l - wall - 1, car_y0 + (r[0] + r[1]) / 2 - term_w / 2 - 0.5, term_z[0]])
                cube([wall + 2, term_w + 1, split - term_z[0] + 0.01]);
        translate([out_l - wall - 1, esp_y0 + board_w / 2 - 7, esp_z + pcb_t + usbc_h / 2 - 4]) cube([wall + 2, 14, 8]);
        button_hole("front");
        button_hole("back");
        // vents over the PoE module, clear of the OLED
        if (vent)
            for (i = [0 : floor((poe_l - 8) / 4)])
                let (x = esp_x0 + poe_x + 4 + i * 4)
                if (!oled || x + 2 < oled_c[0] - oled_pcb[0] / 2 - 2 || x > oled_c[0] + oled_pcb[0] / 2 + 2)
                    translate([x, esp_y0 + poe_y + 3, out_h - top_t - 1]) cube([2, poe_w - 6, top_t + 2]);
        // OLED window, chamfered 45 degrees on the outside
        if (oled) {
            c = [oled_c[0], oled_c[1] + oled_view_dy];
            translate([c[0] - oled_view[0] / 2, c[1] - oled_view[1] / 2, out_h - top_t - 1]) cube([oled_view[0], oled_view[1], top_t + 2]);
            hull() {
                translate([c[0] - oled_view[0] / 2, c[1] - oled_view[1] / 2, out_h - top_t / 2]) cube([oled_view[0], oled_view[1], 0.01]);
                translate([c[0] - oled_view[0] / 2 - top_t / 2 - 0.5, c[1] - oled_view[1] / 2 - top_t / 2 - 0.5, out_h + 0.01])
                    cube([oled_view[0] + top_t + 1, oled_view[1] + top_t + 1, 0.01]);
            }
        }
        // labels
        engrave("RESET", btn_xb, 0, btn_zb + 5, "front", 2.8);
        engrave("BOOT", btn_xb, 0, btn_zb + 5, "back", 2.8);
        for (i = [0 : 3]) {
            engrave(str("P", i + 1), car_x0 + port_x[i], 0, split + 3.5, "front", 3.5);
            engrave(str("P", i + 5), car_x0 + port_x[i], 0, split + 3.5, "back", 3.5);
        }
        engrave("CT", 0, car_y0 + (ct_y[0] + ct_y[1]) / 2, split + 3.5, "right", 3.5);
        engrave("HEAT", 0, car_y0 + (heat_y[0] + heat_y[1]) / 2, split + 3.5, "right", 3.5);
        engrave("USB", 0, esp_y0 + board_w / 2, split + 9, "right", 3);
        engrave("BOILER PROBES", out_l / 2 + 6, 0, out_h - 7, "front", 4);
    }
}

// An ear outside the front (s = -1) or back (s = +1) wall at x, its flat face at the split; `up` puts it on the
// cover (above the split) with its 45 degree gusset above it, otherwise on the base with the gusset below.
module ear(x, s, up) {
    t = up ? ear_t[1] : ear_t[0];
    y0 = s < 0 ? -ear_p : out_w;                 // the ear's outer part
    yw = s < 0 ? 0 : out_w - 0.01;               // the wall face
    z0 = up ? split : split - t;
    difference() {
        hull() {
            translate([x - ear_w / 2, min(y0, yw), z0]) cube([ear_w, ear_p + 0.01, t]);
            // the gusset meets the wall ear_p further from the split, so its underside (or top) is at 45 degrees
            translate([x - ear_w / 2, s < 0 ? -0.01 : out_w - 0.01, up ? z0 + t + ear_p : z0 - ear_p]) cube([ear_w, 0.02, 0.01]);
        }
        hy = s < 0 ? -ear_p / 2 : out_w + ear_p / 2;
        if (up) {
            translate([x, hy, z0 - 1]) cylinder(d = ear_hole[1], h = t + 2);
            translate([x, hy, z0 + t]) cylinder(d = ear_head_d, h = ear_p + 2);    // room for the head and driver
        } else {
            translate([x, hy, z0 - ear_p - 1]) cylinder(d = ear_hole[0], h = t + ear_p + 2);
        }
    }
}

module tabs() {
    for (end = [0, 1]) translate([end ? out_l - corner_r : -tab_l, 0, 0]) difference() {
        rbox(tab_l + corner_r, out_w, tab_t, corner_r);
        translate([end ? corner_r + tab_l / 2 : tab_l / 2, out_w / 2, -1])
            hull() for (dy = [-tab_slot_l / 2, tab_slot_l / 2]) translate([0, dy, 0]) cylinder(d = tab_slot_w, h = tab_t + 2);
    }
}

big = 1000;

module base() {
    intersection() { shell(); translate([-big / 2, -big / 2, -1]) cube([big, big, split + 1]); }
    tabs();
    for (e = ears) ear(e[0], e[1], false);
}

module cover() {
    intersection() { shell(); translate([-big / 2, -big / 2, split]) cube([big, big, big]); }
    for (e = ears) ear(e[0], e[1], true);
}

// The boards, see-through in the preview (F5) only: never part of an STL.
module boards_ghost() {
    %union() {
        translate([car_x0, car_y0, car_z]) cube([car_l, car_w, car_t]);                                  // carrier
        for (i = [0 : 3], y = [-term_out, car_w - 10.5 + term_out])                                      // probe ports
            translate([car_x0 + port_x[i] - term_w / 2, car_y0 + y, car_z + car_t]) cube([term_w, 10.5, term_h]);
        for (r = [ct_y, heat_y])                                                                         // CT, HEAT
            translate([car_x0 + car_l - 10.5 + term_out, car_y0 + (r[0] + r[1]) / 2 - term_w / 2, car_z + car_t])
                cube([10.5, term_w, term_h]);
        translate([esp_x0, esp_y0, car_z + car_t]) {
            for (y = [0.3, board_w - 2.8]) translate([23, y, 0]) cube([51, 2.5, sock_h]);                // sockets
            translate([0, 0, sock_h]) cube([board_l, board_w, pcb_t]);                                    // ESP board
            translate([-rj45_overhang, rj45_y, sock_h + pcb_t]) cube([22.26 - 2, rj45_w, rj45_h]);       // RJ45
            translate([board_l - 7.5, (board_w - usbc_w) / 2, sock_h + pcb_t]) cube([8, usbc_w, usbc_h]); // USB-C
            translate([poe_x, poe_y, sock_h + pcb_t]) cube([poe_l, poe_w, poe_top]);                      // PoE module
            for (y = [-1, board_w - 0.5]) translate([btn_x - 2, y, sock_h + btn_z - 1.5]) cube([4, 1.5, 3]); // buttons
        }
    }
}

if (PART == "base") { base(); boards_ghost(); }
else if (PART == "cover") translate([0, 0, out_h]) mirror([0, 0, 1]) cover();     // top face down, for printing
else { base(); boards_ghost(); translate([0, 0, 25]) cover(); }

echo(str("outside: ", out_l, " x ", out_w, " x ", out_h, " mm (plus ", tab_l, " mm tabs each end and ", ear_p,
         " mm ears front and back); split at ", split, " mm"));
