// DIN-rail case for the boiler probe board: the DIN carrier PCB (../carrier/, layout "din") with the Waveshare
// ESP32-P4-ETH and its PoE module plugged into it. It clips onto a 35 mm top-hat rail (TS35 x 7.5).
//
// Seen from the front, on the rail: probe ports P1-P4 and the two clamp jacks face up, P5-P8, HEAT and the network
// jack face down, and nothing is on the sides, so other modules can sit against it. The OLED and the WAKE button
// are on the front.
//
// Parts: the BASE (the back: floor, standoffs, lower walls with the port windows as slots open toward the front, so
// the carrier drops straight in with its plugs) and the COVER (upper walls and the front), joined by four M3 screws
// from the front into pillars that stand in notches of the carrier's edge. Two HOOKS and the LATCH's GUIDE screw
// onto the back; the LATCH slides in the guide and has its own printed spring.
// Print in PETG, 0.2 mm layers, 3 walls, no supports. Render one part at a time: set PART below.
//
// Case coordinates: x along the rail from the left wall's outside, y up from the bottom wall's outside, z forward
// from the back. Board coordinates (../carrier/build_board.py, DIN_*): x the same way, y DOWN from its top edge.

PART = "base";             // "base", "cover", "cover-text" (lettering for a second colour), "hook", "latch", "latch-guide", "both"

/* [DIN carrier PCB, from ../carrier/build_board.py] */
car_l = 111.5;             // DIN_W, along the rail
car_w = 88.0;              // DIN_H, top to bottom
car_t = 1.6;
car_holes = [[3.3, 18.0], [3.3, 74.5], [105.5, 8.0], [107.0, 37.5], [36.0, 44.0]];     // DIN_HOLES, board coordinates
car_under = 5.0;           // standoff height: room for the through-hole pins under the carrier
esp_x = 7.0;               // DIN_ESP_X0: the ESP board's BOOT-side edge; its RJ45 edge is 1.9 below the carrier's bottom edge
esp_end = 1.9;
port_top_x = [43, 59, 75, 91];         // P1-P4 centres (board x), on the top edge
port_bottom_x = [39, 55, 71, 87];      // P5-P8, on the bottom edge
heat_x = 103.0;            // HEAT block's centre, on the bottom edge
ct_jack_x = [6.5, 19.5];   // CT1, CT2 jacks, on the top edge
// The four pillars, in board coordinates: [x0, y0, x1, y1] of each (0.5 mm inside the carrier's notches,
// DIN_NOTCHES) and the screw's centre. The bottom-left one is narrower in the base: the ESP board passes beside it.
pillars = [[26.6, 0, 34.6, 7.7], [103.8, 20.0, 111.5, 28.0], [103.8, 46.5, 111.5, 54.5], [0, 79.3, 5.7, 88.0]];
pillar_screw = [[30.6, 3.7], [107.8, 24.0], [107.8, 50.5], [2.7, 84.0]];
pillar_cover_x1 = [34.6, 111.5, 111.5, 7.7];   // the same pillars above the split (the last one wider)
term_w = 15.6;             // a 3-pin 5.08 mm pluggable header's width
term_h = 13.0;             // MEASURE: the mated plug's height above the carrier
ct_axis_h = 3.0;           // MEASURE: the jack's bore centre above the carrier
ct_hole_d = 8.5;

/* [ESP32-P4-ETH, from Waveshare's drawing] */
board_l = 78.0;
board_w = 21.0;
pcb_t = 1.6;
sock_h = 8.5 + 2.5;        // MEASURE: the ESP board's bottom above the carrier
rj45_w = 16.0;
rj45_y = 2.5;              // from the ESP board's RESET-side edge (the right one here)
rj45_h = 13.3;
rj45_overhang = 0.8;
usbc_w = 9.0;
usbc_h = 3.2;

/* [PoE module (B), stacked on the ESP board] */
poe_x = 19.0;              // MEASURE: from the ESP board's RJ45 edge
poe_l = 52.0;              // MEASURE
poe_w = 27.0;              // MEASURE: it overhangs the ESP board's BOOT-side edge (toward the left wall)
poe_top = 14.0;            // MEASURE: above the ESP board's top
top_h = max(rj45_h, poe_top);

/* [BOOT: a side-press button under the ESP board's left edge. RESET is on the other edge, inside: reach it with the cover off] */
btn_x = 18.0;              // from the ESP board's RJ45 edge
btn_z = -1.5;              // MEASURE: centre height from the ESP board's bottom
btn_hole_d = 3.5;

/* [OLED on the front: SSD1306 0.96" I2C module] */
oled = true;
oled_pcb = [27.3, 27.8];   // MEASURE
oled_holes = [23.3, 23.8]; // MEASURE
oled_view = [23.0, 12.5];
oled_view_dy = -1.5;       // MEASURE: window centre from the module's centre, away from the pin edge (+y = pin edge, up)
oled_glass_h = 1.8;        // MEASURE
oled_depth = 6.0;          // MEASURE
oled_at = [72, 32];        // the module's centre, board coordinates

/* [Wake button on the front, under the OLED] */
wake = true;
wake_d = 12.2;
wake_depth = 18.0;         // MEASURE (preview only)
wake_at = [72, 61];        // board coordinates

/* [Box] */
wall = 2.4;
floor_t = 2.0;
top_t = 2.0;
gap = 0.3;
corner_r = 2.0;
vent = true;

/* [Cover screws: M3 x 12 self-tapping, from the front] */
screw_pilot = 2.6;
screw_clear = 3.4;
screw_head_d = 6.2;        // bore for the head and the driver
screw_seat = 4.0;          // cover material under the head

/* [DIN rail: TS35 x 7.5] */
rail_h = 35.0;
rail_lip = 1.0;            // the rail's edge thickness (1.5 on some heavy rails)
clip_t = 5.5;              // how far the hooks reach behind the case
clip_fit = 0.4;            // room around the rail's edge
hook_w = 24.0;
hook_x = [45.7, 85.7];     // hook centres, case x: their screws are 7 mm either side
hook_grip = 2.5;           // how far the hook reaches down behind the top edge

/* [Sliding latch: a slider with a zigzag spring and an anchor block printed in one piece, in a guide] */
latch_x = 65.7;            // its centre line, case x: between the hooks, and between P6's and P7's pins inside
latch_w = 24.0;            // the slider's body
latch_fit = 0.3;           // room each side of the slider in the guide
latch_grip = 2.0;          // how far the tooth reaches up behind the rail's bottom edge
latch_neck_w = 16.0;       // the tooth: narrower than the body, whose shoulders stop against the guide's top
latch_tab = 7.0;           // how far the pull tab shows below the case
guide_rail_w = 7.0;        // each side rail of the guide
guide_plate_t = 1.2;       // the guide's plate, between the slider and the case's back
guide_lip = 1.5;           // the rails' 45 degree lips that hold the slider against the plate
guide_screw_y = 23.0;      // the rails' screws, case y
win_w = 18.0;              // the window in the slider that holds the spring and the anchor
spring_n = 5;              // beams in the zigzag
spring_t = 1.2;            // a beam's thickness (three 0.4 mm lines)
spring_gap = 1.2;          // between beams
spring_preload = 1.5;      // how far the spring is squeezed when the anchor is screwed down
anchor_h = 6.0;            // the anchor block: screwed through the guide's plate to the case, it does not move
anchor_w = 16.0;
anchor_screw_y = 7.7;      // case y: 5 mm clear of the port pins inside
anchor_screw_dx = 4.5;     // its two screws, either side of the centre line

$fn = 48;

// ----------------------------------------------------------------- derived sizes
car_x0 = wall + gap;
car_y0 = wall + gap;       // puts the RJ45 jack's front flush with the outside of the bottom wall
car_z = floor_t + car_under;
out_l = car_x0 + car_l + gap + wall;
out_w = car_y0 + car_w + gap + wall;
in_l = out_l - 2 * wall;
in_w = out_w - 2 * wall;
esp_z = car_z + car_t + sock_h;            // the ESP board's bottom
in_h = esp_z + pcb_t + top_h + 2 + (oled ? oled_depth : 0) - floor_t;
out_h = floor_t + in_h + top_t;
term_z = [car_z + car_t - 0.5, car_z + car_t + term_h + 0.5];
split = term_z[1];                         // base behind, cover in front
jack_top = esp_z + pcb_t + rj45_h + 0.4;
function bx(x) = car_x0 + x;               // board x, y -> case x, y
function by(y) = car_y0 + car_w - y;
esp_x0 = bx(esp_x);
btn_y = by(car_w + esp_end - btn_x);
btn_zc = esp_z + btn_z;
oled_c = [bx(oled_at[0]), by(oled_at[1])];
wake_c = [bx(wake_at[0]), by(wake_at[1])];
rail_y = out_w / 2;                        // the rail's centre line
rail_top = rail_y + rail_h / 2;
rail_bot = rail_y - rail_h / 2;
hook_seat = rail_top + 0.2;                // the case hangs on this face
hook_screw_y = hook_seat + 5.0;
guide_y1 = rail_bot - 0.8;                 // the guide's top end, clear of the rail
guide_y0 = 0.6;
latch_zb = [guide_plate_t + 0.2, clip_t];  // the slider, as distances behind the case: clear of the rail's edge too
latch_tip = rail_bot + latch_grip;
latch_shoulder = guide_y1 - 2.0;           // the body's top, against the guide's end walls when latched
anchor_y = [anchor_screw_y - anchor_h / 2, anchor_screw_y + anchor_h / 2];
spring_pitch = spring_t + spring_gap;
spring_len = spring_n * spring_pitch + spring_gap;         // as printed, from the window's top to the anchor
win_top = anchor_y[1] + spring_len - spring_preload;       // the window, with the slider latched
win_bot = anchor_y[0] - spring_preload - 0.6;              // as printed the anchor sits 0.6 above it
latch_body_y0 = win_bot - 2.0;
latch_travel = spring_n * spring_gap - spring_preload;     // down, until the zigzag's beams touch
guide_hw = latch_w / 2 + latch_fit;                        // half the channel
guide_screw_dx = guide_hw + guide_rail_w / 2;
assert(win_top <= latch_shoulder - 1.5, "the spring window reaches into the tooth");
assert(latch_tip - latch_travel < rail_bot - 0.5, "the tooth cannot be pulled clear of the rail");
clip_screws = concat([for (x = hook_x, d = [-7, 7]) [x + d, hook_screw_y]],
                     [for (d = [-1, 1]) [latch_x + d * anchor_screw_dx, anchor_screw_y]],
                     [for (d = [-1, 1]) [latch_x + d * guide_screw_dx, guide_screw_y]]);
big = 1000;

module rbox(l, w, h, r) {
    hull() for (x = [r, l - r], y = [r, w - r]) translate([x, y, 0]) cylinder(r = r, h = h);
}

// a pillar's block, from z0 to z1: board rectangle [x0, y0, x1, y1], pushed 0.8 into the wall it stands against and
// rounded (the notch's inside corners come out of the PCB maker's router with a radius)
module pillar_block(p, x1, z0, z1) {
    px0 = p[0] <= 0 ? -gap - 0.8 : p[0];
    px1 = x1 >= car_l ? car_l + gap + 0.8 : x1;
    py0 = p[1] <= 0 ? -gap - 0.8 : p[1];
    py1 = p[3] >= car_w ? car_w + gap + 0.8 : p[3];
    translate([bx(px0), by(py1), z0]) rbox(px1 - px0, py1 - py0, z1 - z0, 1.5);
}

module front_text(s, x, y, size = 3.5, halign = "center") {
    translate([x, y, out_h - 0.6]) linear_extrude(1) text(s, size = size, halign = halign, valign = "center");
}

module shell() {
    difference() {
        union() {
            difference() {
                rbox(out_l, out_w, out_h, corner_r);
                translate([wall, wall, floor_t]) rbox(in_l, in_w, in_h, max(corner_r - wall, 0.5));
            }
            for (h = car_holes) translate([bx(h[0]), by(h[1]), floor_t - 0.01]) difference() {
                cylinder(d = 6.5, h = car_under + 0.01);
                cylinder(d = 2.6, h = car_under + 1);
            }
            // pillars: narrower behind the split where the ESP board passes, full size in front of it
            for (i = [0 : len(pillars) - 1]) {
                pillar_block(pillars[i], pillars[i][2], floor_t - 0.01, split);
                pillar_block(pillars[i], pillar_cover_x1[i], split, out_h - top_t + 0.01);
            }
            if (oled)
                for (dx = [-oled_holes[0] / 2, oled_holes[0] / 2], dy = [-oled_holes[1] / 2, oled_holes[1] / 2])
                    translate([oled_c[0] + dx, oled_c[1] + dy, out_h - top_t - oled_glass_h]) difference() {
                        cylinder(d = 4.2, h = oled_glass_h + 0.01);
                        translate([0, 0, -1]) cylinder(d = 1.7, h = oled_glass_h + 1);
                    }
        }
        // cover screws: pilot in the base pillar, clearance and a bore for the head in the cover's
        for (c = pillar_screw) translate([bx(c[0]), by(c[1]), 0]) {
            translate([0, 0, split - 12]) cylinder(d = screw_pilot, h = 12.01);
            translate([0, 0, split - 0.01]) cylinder(d = screw_clear, h = screw_seat + 0.02);
            translate([0, 0, split + screw_seat]) cylinder(d = screw_head_d, h = out_h);
        }
        // hook and latch screws, through the floor from inside (heads under the carrier)
        for (c = clip_screws) translate([c[0], c[1], -1]) cylinder(d = screw_clear, h = floor_t + 2);
        // bottom wall. Network jack: behind the split a slot as wide as the ESP board, in front a U around the jack
        translate([esp_x0 - 0.5, -1, esp_z - 0.5]) cube([board_w + 1, wall + 2, split - esp_z + 0.51]);
        translate([esp_x0 + board_w - rj45_y - rj45_w - 0.4, -1, esp_z]) cube([rj45_w + 0.8, wall + 2, jack_top - esp_z]);
        // P5-P8 and HEAT: one window, open to the split
        translate([bx(port_bottom_x[0]) - term_w / 2 - 0.5, -1, term_z[0]])
            cube([bx(heat_x) - bx(port_bottom_x[0]) + term_w + 1, wall + 2, split - term_z[0] + 0.01]);
        // top wall: P1-P4, the clamp jacks' holes and a tunnel for a USB-C plug
        translate([bx(port_top_x[0]) - term_w / 2 - 0.5, out_w - wall - 1, term_z[0]])
            cube([bx(port_top_x[3]) - bx(port_top_x[0]) + term_w + 1, wall + 2, split - term_z[0] + 0.01]);
        for (x = ct_jack_x)
            translate([bx(x), out_w - wall - 1, car_z + car_t + ct_axis_h]) rotate([-90, 0, 0]) cylinder(d = ct_hole_d, h = wall + 2);
        translate([esp_x0 + board_w / 2 - 7, out_w - wall - 1, esp_z + pcb_t + usbc_h / 2 - 4]) cube([14, wall + 2, 8]);
        // left wall: BOOT, for a paperclip
        translate([-1, btn_y, btn_zc]) rotate([0, 90, 0]) {
            cylinder(d = btn_hole_d, h = wall + 2);
            cylinder(d1 = btn_hole_d + 3, d2 = btn_hole_d, h = 2.2);
        }
        // vents over the PoE module: slots across it
        if (vent)
            for (i = [0 : floor((poe_l - 8) / 4)])
                translate([esp_x0 + board_w - poe_w + 3, by(car_w + esp_end - poe_x - 4 - i * 4) - 2, out_h - top_t - 1])
                    cube([poe_w - 6, 2, top_t + 2]);
        if (oled) {
            c = [oled_c[0], oled_c[1] + oled_view_dy];
            translate([c[0] - oled_view[0] / 2, c[1] - oled_view[1] / 2, out_h - top_t - 1]) cube([oled_view[0], oled_view[1], top_t + 2]);
            hull() {
                translate([c[0] - oled_view[0] / 2, c[1] - oled_view[1] / 2, out_h - top_t / 2]) cube([oled_view[0], oled_view[1], 0.01]);
                translate([c[0] - oled_view[0] / 2 - top_t / 2 - 0.5, c[1] - oled_view[1] / 2 - top_t / 2 - 0.5, out_h + 0.01])
                    cube([oled_view[0] + top_t + 1, oled_view[1] + top_t + 1, 0.01]);
            }
        }
        if (wake) translate([wake_c[0], wake_c[1], out_h - top_t - 1]) cylinder(d = wake_d, h = top_t + 2);
        labels();
    }
}

// Every label, cut 0.6 mm into the front; cover_text() is the same lettering as a part that fills the cuts.
module labels() {
    for (i = [0 : 3]) {
        front_text(str("P", i + 1), bx(port_top_x[i]), out_w - 5.5);
        front_text(str("P", i + 5), bx(port_bottom_x[i]), 5.5);
    }
    front_text("HEAT", bx(heat_x), 5.5, 3);
    front_text("CT1", bx(ct_jack_x[0]), out_w - 5, 2.6);
    front_text("CT2", bx(ct_jack_x[1]), out_w - 5, 2.6);
    front_text("USB", esp_x0 + board_w / 2, out_w - 10.5, 2.6);
    front_text("LAN", esp_x0 + board_w / 2, 5.5, 3);
    front_text("< BOOT", 2.5, btn_y, 2.6, "left");
    if (wake) front_text("WAKE", wake_c[0] - wake_d / 2 - 2, wake_c[1], 3.5, "right");
    front_text("BOILER PROBES", bx(72), by(47.5), 4);
}

module base() {
    intersection() { shell(); translate([-big / 2, -big / 2, -1]) cube([big, big, split + 1]); }
}

module cover() {
    intersection() { shell(); translate([-big / 2, -big / 2, split]) cube([big, big, big]); }
}

module cover_text() {
    intersection() { labels(); rbox(out_l, out_w, out_h, corner_r); }
}

// The cover prints front face down: turned over (a rotation, not a mirror, so the lettering still reads right).
module print_flip() { translate([0, out_w, out_h]) rotate([180, 0, 0]) children(); }

// ----------------------------------------------------------------- rail hooks and latch
// Modelled in place behind the case: zb is the distance behind the back (z = -zb). Both print with the far
// (wall-side) face on the bed, so the slot for the rail's edge is open toward the top and needs no support; the
// latch's guide prints the other way up, on its plate.
module behind(y0, y1, zb0, zb1, x0, x1) {
    translate([x0, y0, -zb1]) cube([x1 - x0, y1 - y0, zb1 - zb0]);
}

// One hook (print two): a block above the rail with a finger behind the rail's top edge.
module hook(xc) {
    difference() {
        union() {
            behind(hook_seat, hook_seat + 9.5, 0, clip_t, xc - hook_w / 2, xc + hook_w / 2);
            behind(hook_seat - hook_grip, hook_seat + 0.01, rail_lip + clip_fit, clip_t, xc - hook_w / 2, xc + hook_w / 2);
        }
        for (d = [-7, 7]) translate([xc + d, hook_screw_y, -clip_t - 1]) cylinder(d = screw_pilot, h = clip_t + 2);
    }
}

// The latch, in the position it has on the rail (the anchor is drawn where it is printed: spring_preload lower
// than where its screws hold it). One piece: the slider with the tooth at its top end and the pull tab at its
// bottom end, and in its window a zigzag spring down to the anchor block. The anchor is screwed to the case; the
// spring pushes the slider up from it until the body's shoulders meet the guide. The rail's edge rides down the
// tooth's ramp as the case is pushed on; to take the case off, pull the tab down with a screwdriver.
module spring_2d() {
    sw = win_w - 1.6;
    function beam_top(i) = win_top - spring_gap - i * spring_pitch;
    for (i = [0 : spring_n - 1]) {
        translate([-sw / 2, beam_top(i) - spring_t]) square([sw, spring_t]);
        // each beam hangs from the one above (the first from the window's top) at alternate ends
        translate([i % 2 == 0 ? -sw / 2 : sw / 2 - spring_t, beam_top(i) - 0.01]) square([spring_t, spring_gap + 0.02]);
    }
    // the last beam down to the anchor
    translate([spring_n % 2 == 0 ? -sw / 2 : sw / 2 - spring_t, win_top - spring_len - 0.01]) square([spring_t, spring_gap + 0.02]);
    translate([-anchor_w / 2, win_top - spring_len - anchor_h]) square([anchor_w, anchor_h]);
}

module latch() {
    h = latch_zb[1] - latch_zb[0];
    ramp = h - 0.7;                                        // 45 degrees, leaving 0.7 mm of tooth at the tip
    assert(latch_tip - ramp < rail_bot - 1, "the rail's edge misses the ramp");
    difference() {
        translate([latch_x, 0, -latch_zb[1]]) linear_extrude(h) difference() {
            union() {
                difference() {
                    union() {
                        translate([-latch_w / 2, latch_body_y0]) square([latch_w, latch_shoulder - latch_body_y0]);
                        translate([-latch_neck_w / 2, latch_shoulder - 0.01]) square([latch_neck_w, latch_tip - latch_shoulder + 0.01]);
                        translate([-5, -latch_tab]) square([10, latch_body_y0 + latch_tab + 0.01]);
                    }
                    translate([-win_w / 2, win_bot]) square([win_w, win_top - win_bot]);
                }
                spring_2d();
            }
            for (d = [-1, 1]) translate([d * anchor_screw_dx, win_top - spring_len - anchor_h / 2]) circle(d = screw_pilot);
            translate([-2, -latch_tab + 1.5]) square([4, 3.5]);                                   // screwdriver slot
        }
        // the tooth's ramp: its far upper corner
        translate([latch_x - latch_neck_w / 2 - 1, 0, 0]) rotate([90, 0, 90]) linear_extrude(latch_neck_w + 2)
            polygon([[latch_tip + 0.01, -(clip_t - ramp)], [latch_tip + 0.01, -clip_t - 0.01], [latch_tip - ramp, -clip_t - 0.01]]);
        // the body's far edges, bevelled to run under the guide's lips
        for (d = [-1, 1]) translate([latch_x, latch_body_y0 - 1, 0]) rotate([-90, 0, 0]) linear_extrude(latch_shoulder - latch_body_y0 + 2)
            polygon([[d * (latch_w / 2 + 0.01), clip_t - guide_lip], [d * (latch_w / 2 + 0.01), clip_t + 0.01],
                     [d * (latch_w / 2 - guide_lip), clip_t + 0.01]]);
    }
}

// The latch's guide: a plate against the case's back, two rails with lips that the slider runs under, and end
// walls at the top that stop the slider's shoulders. The slider goes in from the bottom end.
module latch_guide() {
    difference() {
        union() {
            behind(guide_y0, guide_y1, 0, guide_plate_t, latch_x - guide_hw - guide_rail_w, latch_x + guide_hw + guide_rail_w);
            for (d = [-1, 1]) {
                translate([latch_x, guide_y0, 0]) rotate([-90, 0, 0]) linear_extrude(guide_y1 - guide_y0)
                    polygon([[d * guide_hw, 0], [d * (guide_hw + guide_rail_w), 0], [d * (guide_hw + guide_rail_w), clip_t],
                             [d * (guide_hw - guide_lip), clip_t], [d * guide_hw, clip_t - guide_lip]]);
                behind(latch_shoulder, guide_y1, 0, clip_t, latch_x + d * (guide_hw + 0.01) + (d < 0 ? 0 : -(guide_hw - latch_neck_w / 2 - latch_fit)),
                       latch_x + d * (guide_hw + 0.01) + (d < 0 ? guide_hw - latch_neck_w / 2 - latch_fit : 0));
            }
        }
        for (d = [-1, 1]) {
            translate([latch_x + d * guide_screw_dx, guide_screw_y, -clip_t - 1]) cylinder(d = screw_pilot, h = clip_t + 2);
            translate([latch_x + d * anchor_screw_dx, anchor_screw_y, -guide_plate_t - 1]) cylinder(d = screw_clear, h = guide_plate_t + 2);
        }
    }
}

module clip_print() { translate([0, 0, clip_t]) children(); }      // wall-side face on the bed
module guide_print() { translate([0, guide_y1 + guide_y0, 0]) rotate([180, 0, 0]) children(); }    // plate on the bed

// ----------------------------------------------------------------- preview only
module boards_ghost() {
    %union() {
        translate([car_x0, car_y0, car_z]) cube([car_l, car_w, car_t]);                                  // carrier
        for (x = port_top_x) translate([bx(x) - term_w / 2, by(10.5) + 0.6, car_z + car_t]) cube([term_w, 10.5, term_h]);
        for (x = concat(port_bottom_x, [heat_x])) translate([bx(x) - term_w / 2, by(car_w) - 0.6, car_z + car_t]) cube([term_w, 10.5, term_h]);
        for (x = ct_jack_x) translate([bx(x) - 6, by(14), car_z + car_t]) cube([12, 14, 5]);             // CT jacks
        translate([esp_x0, by(car_w + esp_end), car_z + car_t]) {
            for (x = [0.3, board_w - 2.8]) translate([x, 23, 0]) cube([2.5, 51, sock_h]);                // sockets
            translate([0, 0, sock_h]) cube([board_w, board_l, pcb_t]);                                    // ESP board
            translate([board_w - rj45_y - rj45_w, -rj45_overhang, sock_h + pcb_t]) cube([rj45_w, 20.26, rj45_h]);   // RJ45
            translate([(board_w - usbc_w) / 2, board_l - 7.5, sock_h + pcb_t]) cube([usbc_w, 8, usbc_h]); // USB-C
            translate([board_w - poe_w, poe_x, sock_h + pcb_t]) cube([poe_w, poe_l, poe_top]);            // PoE module
            translate([-1, btn_x - 2, sock_h + btn_z - 1.5]) cube([1.5, 4, 3]);                          // BOOT
            translate([board_w - 0.5, btn_x - 2, sock_h + btn_z - 1.5]) cube([1.5, 4, 3]);               // RESET
        }
        if (wake) translate([wake_c[0], wake_c[1], out_h - top_t - wake_depth]) cylinder(d = wake_d - 0.4, h = wake_depth);
        if (oled) translate([oled_c[0] - oled_pcb[0] / 2, oled_c[1] - oled_pcb[1] / 2, out_h - top_t - oled_depth])
            cube([oled_pcb[0], oled_pcb[1], oled_depth]);
        // the rail: its two edges and its web
        for (y = [rail_bot, rail_top - 4]) translate([-15, y, -rail_lip]) cube([out_l + 30, 4, rail_lip]);
        translate([-15, rail_bot + 4, -7.5]) cube([out_l + 30, rail_h - 8, 1]);
    }
}

if (PART == "base") { base(); boards_ghost(); }
else if (PART == "cover") print_flip() cover();
else if (PART == "cover-text") print_flip() cover_text();
else if (PART == "hook") clip_print() hook(hook_x[0]);
else if (PART == "latch") clip_print() latch();
else if (PART == "latch-guide") guide_print() latch_guide();
else {
    color("#3a9da0") base();
    color("#d08a2e") { for (x = hook_x) hook(x); latch_guide(); }
    color("#c8c8c8") latch();
    boards_ghost();
    translate([0, 0, 25]) { color("#3a9da0") cover(); color("black") render() cover_text(); }
}

echo(str("outside: ", out_l, " x ", out_w, " x ", out_h, " mm, plus ", clip_t, " mm behind for the rail; split at ", split, " mm"));
