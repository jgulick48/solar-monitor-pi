"""Builds the boiler probe carrier board (KiCad 9) from this script: placement, nets and labels.
Routing is done by Freerouting afterwards (see build.sh); this script's `finish` step adds the ground pours.

  python3 build_board.py place <tht|smd> out.kicad_pcb [box|din]     # placed and netted, not routed -> export DSN
  python3 build_board.py finish in.kicad_pcb routes.ses out.kicad_pcb [box|din]   # import the routes, GND pours
  python3 build_board.py clean in.kicad_pcb out.kicad_pcb   # remove dangling track ends (run before the DRC)

Two layouts: "box" (100 x 60 mm, for ../enclosure/boiler-probes-box.scad) and "din" (111.5 x 88 mm, for the
DIN-rail case ../enclosure/boiler-probes-din.scad). Same circuit, same pins, same firmware.

The Waveshare ESP32-P4-ETH (with its PoE module on top) plugs into two 1x20 sockets. Its pins, from the
pinout drawing, by position k = 0 at the RJ45 end to 19 at the USB-C end:"""
import csv
import os
import sys

import pcbnew

RESET_ROW = ["GPIO48", "GPIO47", "GND", "GPIO46", "GPIO33", "GPIO32", "GPIO27", "GND", "GPIO26", "",
             "GPIO23", "GPIO22", "GND", "GPIO21", "GPIO20", "3V3", "", "GND", "", ""]      # RUN, EN, VSYS, VBUS unused
BOOT_ROW = ["", "", "GND", "GPIO7", "GPIO8", "", "GPIO3", "GND", "GPIO4", "GPIO5",
            "GPIO6", "GPIO14", "GND", "GPIO15", "GPIO16", "GPIO17", "GPIO18", "GND", "GPIO19", "GPIO54"]

# Probe ports: name, side, centre x, GPIO. Front (RESET side) P1-P4, back (BOOT side) P5-P8, in GPIO order along
# the row so their tracks don't cross.
PORTS = [("P1", "front", 32, "GPIO33"), ("P2", "front", 48, "GPIO27"), ("P3", "front", 64, "GPIO23"),
         ("P4", "front", 80, "GPIO21"), ("P5", "back", 32, "GPIO32"), ("P6", "back", 48, "GPIO26"),
         ("P7", "back", 64, "GPIO22"), ("P8", "back", 80, "GPIO20")]

W, H = 100.0, 60.0             # board; y = 0 is the back (BOOT) edge, y = 60 the front (RESET) edge; x = 0 the RJ45 end
HOLES = ((4.0, 4.0), (4.0, 56.0), (96.0, 6.0), (30.0, 30.0))   # M3 mounting holes (the enclosure's standoffs: car_holes)
ESP_X0 = -1.9                  # the ESP board's RJ45 edge (its jack ends flush with the enclosure's outside)
ESP_Y0, ESP_W = 19.5, 21.0     # its back edge, width
ROW_BOOT = ESP_Y0 + 1.61       # header rows 17.78 apart, centred on the 21 mm board
ROW_RESET = ESP_Y0 + ESP_W - 1.61
PIN0_X = ESP_X0 + 25.07        # first header pin from the ESP's RJ45 edge, then 2.54 mm pitch

# ---- DIN-rail layout. The board stands on the rail: x runs along the rail, y = 0 is the top edge, y = DIN_H the
# bottom edge. Everything plugs in from above or below, nothing from the sides (other modules sit there).
# The ESP board stands on end at the left, its RJ45 jack pointing down and flush with the case's bottom, USB-C up;
# its BOOT side (and the PoE module's overhang) is toward the left edge.
DIN_W, DIN_H = 111.5, 88.0
DIN_ESP_X0 = 7.0               # the ESP board's BOOT-side edge; it is ESP_W wide
DIN_ESP_END = DIN_H + 1.9      # its RJ45 edge, past the bottom edge like ESP_X0 in the box layout
DIN_ROW_BOOT = DIN_ESP_X0 + 1.61
DIN_ROW_RESET = DIN_ESP_X0 + ESP_W - 1.61
DIN_PIN0_Y = DIN_ESP_END - 25.07       # first header pin, then 2.54 mm pitch toward the top
# probe ports: P1-P4 along the top edge, P5-P8 along the bottom; the same GPIOs as the box layout
DIN_PORTS = [("P1", "top", 43, "GPIO33"), ("P2", "top", 59, "GPIO27"), ("P3", "top", 75, "GPIO23"),
             ("P4", "top", 91, "GPIO21"), ("P5", "bottom", 39, "GPIO32"), ("P6", "bottom", 55, "GPIO26"),
             ("P7", "bottom", 71, "GPIO22"), ("P8", "bottom", 87, "GPIO20")]
DIN_HEAT_X = 103.0             # HEAT block's centre, on the bottom edge
DIN_JACKS = (("J12", "CT1", 6.5, "CT1_GPIO16"), ("J16", "CT2", 19.5, "CT2_GPIO17"))    # on the top edge, centre x
DIN_HOLES = ((3.3, 18.0), (3.3, 74.5), (105.5, 8.0), (107.0, 37.5), (36.0, 44.0))      # M3, the case's standoffs
# Cut-outs in the edge for the case's pillars (the screws that join its two halves): x0, y0, x1, y1
DIN_NOTCHES = ((26.1, 0.0, 35.1, 8.2), (103.3, 19.5, DIN_W, 28.5), (103.3, 46.0, DIN_W, 55.0),
               (0.0, 78.8, 6.2, DIN_H))

FP = "/usr/share/kicad/footprints/"
MM = pcbnew.FromMM


def pt(x, y):
    return pcbnew.VECTOR2I(MM(x), MM(y))


def din_outline():
    """The DIN board's outline, clockwise from the top left, with the pillar notches cut out of its edges."""
    (tx0, _, tx1, ty1), (rx0, ra0, _, ra1), (_, rb0, _, rb1), (_, by0, bx1, _) = DIN_NOTCHES
    return [(0, 0), (tx0, 0), (tx0, ty1), (tx1, ty1), (tx1, 0), (DIN_W, 0),
            (DIN_W, ra0), (rx0, ra0), (rx0, ra1), (DIN_W, ra1), (DIN_W, rb0), (rx0, rb0), (rx0, rb1), (DIN_W, rb1),
            (DIN_W, DIN_H), (bx1, DIN_H), (bx1, by0), (0, by0)]


def use_layout(layout):
    """Point the module's W, H at the layout being built (terminal() and finish() read them)."""
    global W, H
    if layout == "din":
        W, H = DIN_W, DIN_H
    elif layout != "box":
        raise SystemExit(f"unknown layout: {layout}")


class Board:
    def __init__(self, variant, layout="box"):
        self.variant = variant
        self.layout = layout
        use_layout(layout)
        self.b = pcbnew.BOARD()
        self.nets = {}
        self.bom = []          # (ref, value, footprint, lcsc)
        ds = self.b.GetDesignSettings()
        ds.SetCopperLayerCount(2)
        ds.m_TrackMinWidth = MM(0.2)
        ds.m_MinClearance = MM(0.2)
        ds.m_ViasMinSize = MM(0.6)
        ds.m_MinThroughDrill = MM(0.3)
        ns = ds.m_NetSettings
        nc = ns.GetDefaultNetclass()
        nc.SetTrackWidth(MM(0.3)); nc.SetClearance(MM(0.25)); nc.SetViaDiameter(MM(0.7)); nc.SetViaDrill(MM(0.35))
        pw = pcbnew.NETCLASS("Power")
        pw.SetTrackWidth(MM(0.6)); pw.SetClearance(MM(0.25)); pw.SetViaDiameter(MM(0.8)); pw.SetViaDrill(MM(0.4))
        ac = pcbnew.NETCLASS("AC24")
        ac.SetTrackWidth(MM(0.4)); ac.SetClearance(MM(0.8)); ac.SetViaDiameter(MM(0.8)); ac.SetViaDrill(MM(0.4))
        ns.SetNetclass("Power", pw)
        ns.SetNetclass("AC24", ac)
        ns.SetNetclassPatternAssignment("GND", "Power")
        ns.SetNetclassPatternAssignment("3V3", "Power")
        for n in ("HC_A", "HC_B", "HC_LED"):
            ns.SetNetclassPatternAssignment(n, "AC24")
        self.outline()

    def net(self, name):
        if not name:
            return None
        if name not in self.nets:
            n = pcbnew.NETINFO_ITEM(self.b, name)
            self.b.Add(n)
            self.nets[name] = n
        return self.nets[name]

    def outline(self):
        corners = din_outline() if self.layout == "din" else [(0, 0), (W, 0), (W, H), (0, H)]
        for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
            s = pcbnew.PCB_SHAPE(self.b)
            s.SetShape(pcbnew.SHAPE_T_SEGMENT)
            s.SetStart(pt(x1, y1)); s.SetEnd(pt(x2, y2))
            s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(MM(0.1))
            self.b.Add(s)

    def add(self, lib, name, ref, value, x, y, rot=0, nets=None, lcsc="", crtyd=None):
        """Place a library footprint with pad 1 at (x, y), rotated `rot` degrees; nets: {pad number: net name}.
        crtyd: (left, top, right, bottom) relative to pad 1 before rotation, replacing the library's courtyard."""
        fp = pcbnew.FootprintLoad(FP + lib + ".pretty", name)
        if fp is None:
            raise SystemExit(f"footprint not found: {lib}:{name}")
        fp.SetReference(ref)
        fp.SetValue(value)
        if crtyd:
            for g in list(fp.GraphicalItems()):
                if g.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
                    fp.Remove(g)
            r = pcbnew.PCB_SHAPE(fp)
            r.SetShape(pcbnew.SHAPE_T_RECT)
            r.SetStart(pt(crtyd[0], crtyd[1])); r.SetEnd(pt(crtyd[2], crtyd[3]))
            r.SetLayer(pcbnew.F_CrtYd); r.SetWidth(MM(0.05))
            fp.Add(r)
        self.b.Add(fp)
        fp.SetOrientationDegrees(rot)
        p1 = next((p for p in fp.Pads() if p.GetNumber() == "1"), None)
        off = (p1.GetPosition() - fp.GetPosition()) if p1 else pcbnew.VECTOR2I(0, 0)
        fp.SetPosition(pt(x, y) - off)
        for p in fp.Pads():
            n = (nets or {}).get(p.GetNumber())
            if n:
                p.SetNet(self.net(n))
        fp.Reference().SetVisible(True)
        fp.Value().SetVisible(False)
        self.bom.append((ref, value, f"{lib}:{name}", lcsc))
        return fp

    def text(self, s, x, y, size=1.2, layer=pcbnew.F_SilkS, rot=0, bold=False):
        t = pcbnew.PCB_TEXT(self.b)
        t.SetText(s)
        t.SetPosition(pt(x, y))
        t.SetLayer(layer)
        t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
        t.SetTextThickness(MM(size * (0.2 if bold else 0.15)))
        t.SetTextAngleDegrees(rot)
        if layer == pcbnew.B_SilkS:
            t.SetMirrored(True)
        self.b.Add(t)

    # ------------------------------------------------------------------ parts
    def resistor(self, ref, value, x, y, n1, n2, rot=0, lcsc=""):
        """A resistor from pad 1 at (x, y), along x (rot 0) or y (rot 90 = up)."""
        if self.variant == "tht":
            fp = self.add("Resistor_THT", "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", ref, value, x, y, rot,
                          {"1": n1, "2": n2})
            self.ref_inside(fp)
            return fp
        return self.add("Resistor_SMD", "R_0805_2012Metric", ref, value, x, y, rot, {"1": n1, "2": n2}, lcsc)

    def capacitor(self, ref, value, x, y, n1, n2, rot=0, lcsc=""):
        if self.variant == "tht":
            fp = self.add("Capacitor_THT", "C_Disc_D5.0mm_W2.5mm_P5.00mm", ref, value, x, y, rot, {"1": n1, "2": n2})
            self.ref_inside(fp)
            return fp
        return self.add("Capacitor_SMD", "C_0805_2012Metric", ref, value, x, y, rot, {"1": n1, "2": n2}, lcsc)

    def terminal(self, ref, label, x, y, side, nets):
        """A 3-pin 5.08 mm pluggable header (Phoenix MSTBA footprint; the uxcell 2EDGR is the same pin layout),
        pad 1 at (x, y), its plug face toward `side` (front: +y, back: -y, right: +x; on the right side its pins
        run toward -y from pad 1)."""
        rot = {"front": 0, "back": 180, "right": 90}[side]
        # the real block is 15.6 x 10.5 mm: pins 2.5 mm from its back, plug face 8 mm in front of the pins
        fp = self.add("Connector_Phoenix_MSTB", "PhoenixContact_MSTBA_2,5_3-G-5,08_1x03_P5.08mm_Horizontal",
                      ref, label, x, y, rot, nets, crtyd=(-2.72, -2.5, 12.88, 10.6))
        fp.Reference().SetVisible(False)          # the port label names it
        # The library's silkscreen is for Phoenix's wider block (neighbours' outlines crossed, and its pin-1 triangle
        # sat in the "G"): instead, a plain outline of the real block, kept inside the board's edge.
        for g in fp.GraphicalItems():            # moved to the fabrication drawing, not printed (removing them from a
            if g.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS):   # footprint on the board upsets KiCad's Python)
                g.SetLayer(pcbnew.F_Fab)
        # the block's outline (-2.72..12.88 along the pins, -2.5..10.6 across, from pad 1), turned with it
        bx0, by0, bx1, by1 = {"front": (x - 2.72, y - 2.5, x + 12.88, y + 10.6),
                              "back": (x - 12.88, y - 10.6, x + 2.72, y + 2.5),
                              "right": (x - 2.5, y - 12.88, x + 10.6, y + 2.72)}[side]
        x0, y0 = max(bx0 + 0.15, 0.2), max(by0 + 0.15, 0.2)
        x1, y1 = min(bx1 - 0.15, W - 0.2), min(by1 - 0.15, H - 0.2)
        r = pcbnew.PCB_SHAPE(self.b)
        r.SetShape(pcbnew.SHAPE_T_RECT)
        r.SetStart(pt(x0, y0)); r.SetEnd(pt(x1, y1))
        r.SetLayer(pcbnew.F_SilkS); r.SetWidth(MM(0.12))
        self.b.Add(r)
        return fp

    def ref_inside(self, fp, size=0.8):
        """Put a part's reference inside its own body outline (the middle of its pads), small: it can't collide."""
        pads = [p.GetPosition() for p in fp.Pads()]
        cx = sum(p.x for p in pads) // len(pads)
        cy = sum(p.y for p in pads) // len(pads)
        t = fp.Reference()
        t.SetPosition(pcbnew.VECTOR2I(cx, cy))
        t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
        t.SetTextThickness(MM(size * 0.15))
        t.SetTextAngleDegrees(0)

    def pin_labels(self, ref, labels, dx, dy, layer=pcbnew.F_SilkS, size=1.0):
        """A label beside each pad of `ref` (pad 1 first), offset (dx, dy) from the pad."""
        fp = next(f for f in self.b.GetFootprints() if f.GetReference() == ref)
        for p in fp.Pads():
            t = labels[int(p.GetNumber()) - 1]
            if t:
                self.text(t, pcbnew.ToMM(p.GetPosition().x) + dx, pcbnew.ToMM(p.GetPosition().y) + dy, size, layer)

    # ------------------------------------------------------------------ layout
    def jack(self, ref, label, x, y, rot, nets, box):
        """A 3.5 mm jack (CUI SJ1-3523N) with its origin at (x, y); box: its body's outline (x0, y0, x1, y1)."""
        jk = self.add("Connector_Audio", "Jack_3.5mm_CUI_SJ1-3523N_Horizontal", ref, label, x, y, rot, nets)
        jk.Reference().SetVisible(False)
        for g in list(jk.GraphicalItems()):      # the library footprint draws a board-edge cutout: not wanted here
            if g.GetLayer() == pcbnew.Edge_Cuts:
                jk.Remove(g)
        for g in jk.GraphicalItems():            # its silkscreen runs past the edge (the plug end is flush with
            if g.GetLayer() == pcbnew.F_SilkS:   # it): to the fabrication drawing, and a plain outline instead
                g.SetLayer(pcbnew.F_Fab)
        r = pcbnew.PCB_SHAPE(self.b)
        r.SetShape(pcbnew.SHAPE_T_RECT)
        r.SetStart(pt(box[0], box[1])); r.SetEnd(pt(box[2], box[3]))
        r.SetLayer(pcbnew.F_SilkS); r.SetWidth(MM(0.12))
        self.b.Add(r)

    def build(self):
        if self.layout == "din":
            return self.build_din()
        return self.build_box()

    def build_din(self):
        tht = self.variant == "tht"
        # ESP32-P4-ETH sockets: pad 1 at k = 0 (RJ45 end, at the bottom), pads toward the top
        for ref, row, x in (("J1", RESET_ROW, DIN_ROW_RESET), ("J2", BOOT_ROW, DIN_ROW_BOOT)):
            self.add("Connector_PinSocket_2.54mm", "PinSocket_1x20_P2.54mm_Vertical", ref, "ESP32-P4-ETH",
                     x, DIN_PIN0_Y, rot=180, nets={str(k + 1): n for k, n in enumerate(row)})
        j1 = next(f for f in self.b.GetFootprints() if f.GetReference() == "J1")
        p2 = next(p for p in j1.Pads() if p.GetNumber() == "2")
        if abs(pcbnew.ToMM(p2.GetPosition().y) - (DIN_PIN0_Y - 2.54)) > 0.01:
            raise SystemExit("socket orientation is wrong")
        # probe ports and their pull-ups: plug faces up along the top edge, down along the bottom edge
        for i, (name, side, cx, gpio) in enumerate(DIN_PORTS):
            n = f"{name}_{gpio}"
            if side == "bottom":
                self.terminal(f"J{3 + i}", name, cx - 5.08, H - 10.0, "front", {"1": "GND", "2": n, "3": "3V3"})
                ry = H - 15.5
            else:
                self.terminal(f"J{3 + i}", name, cx + 5.08, 10.0, "back", {"1": "GND", "2": n, "3": "3V3"})
                ry = 15.5
            if tht:
                self.resistor(f"R{1 + i}", "4.7k", cx - 5.08, ry, n, "3V3")
            else:
                r = self.resistor(f"R{1 + i}", "4.7k", cx - 1.0, ry, n, "3V3", lcsc="C17673")
                if side == "top":
                    r.Reference().SetPosition(pt(cx, ry + 1.7))
            self.text(f"{name}  {gpio}", cx, H - 2.8 if side == "bottom" else 2.6, 1.2)
            self.pin_labels(f"J{3 + i}", ("G", "D", "V"), 0, -3.4 if side == "bottom" else 3.4)
        # heat call (24 VAC, isolated) on the bottom edge: outer pins, PC814 (AC input)
        self.terminal("J11", "HEAT", DIN_HEAT_X - 5.08, H - 10.0, "front", {"1": "HC_A", "3": "HC_B"})
        self.pin_labels("J11", ("AC", "", "AC"), 0, -3.4, size=0.8)
        self.text("HEAT", DIN_HEAT_X, H - 2.8, 1.2)
        # pump current clamps: two 3.5 mm jacks on the top edge, beside each other, plug face flush with the edge.
        # They are 5 mm high: CT2 and the socket ends sit under the ESP board's USB end, 11 mm up.
        for ref, label, x, gpio in DIN_JACKS:
            self.jack(ref, label, x, 6.3, 180, {"S": gpio, "T": "BIAS"}, (x - 6.0, 0.2, min(x + 6.0, DIN_NOTCHES[0][0] - 0.3), 6.3 + 7.7 - 0.1))
            self.text(label, x, 16.0 if label == "CT1" else 15.6, 1.0, layer=pcbnew.B_SilkS, bold=True)
        hx = DIN_HEAT_X
        if tht:
            self.resistor("R10", "10k", 32.5, 57.2, "HEAT_GPIO46", "3V3")
            self.capacitor("C1", "1uF", 32.5, 61.0, "HEAT_GPIO46", "GND")
            self.capacitor("C2", "10uF", 15.0, 18.5, "BIAS", "GND")          # these four sit between the sockets,
            self.resistor("R11", "10k", 12.4, 22.5, "3V3", "BIAS")           # under the ESP board
            self.resistor("R12", "10k", 12.4, 25.5, "BIAS", "GND")
            self.capacitor("C3", "100nF", 15.0, 29.5, "3V3", "GND")
            self.ref_inside(self.add("Package_DIP", "DIP-4_W7.62mm", "U1", "PC814", hx - 6.0, 60.5, 0,
                                     {"1": "HC_LED", "2": "HC_B", "3": "GND", "4": "HEAT_GPIO46"}))
            self.resistor("R9", "10k", hx - 7.0, 68.5, "HC_A", "HC_LED")
        else:
            self.resistor("R10", "10k", 33.0, 57.2, "HEAT_GPIO46", "3V3", lcsc="C17414")
            self.capacitor("C1", "1uF", 37.0, 57.2, "HEAT_GPIO46", "GND", lcsc="C28323")
            self.capacitor("C2", "10uF", 16.5, 18.5, "BIAS", "GND", lcsc="C15850")
            self.resistor("R11", "10k", 14.5, 22.5, "3V3", "BIAS", lcsc="C17414")
            self.resistor("R12", "10k", 18.5, 22.5, "BIAS", "GND", lcsc="C17414")
            self.capacitor("C3", "100nF", 16.5, 29.5, "3V3", "GND", lcsc="C49678")
            self.add("Package_SO", "SO-4_4.4x3.6mm_P2.54mm", "U1", "EL814S1(A)(TU)-V", hx - 4.0, 61.5, 0,
                     {"1": "HC_LED", "2": "HC_B", "3": "GND", "4": "HEAT_GPIO46"}, lcsc="C500388")
            self.resistor("R9", "10k", hx - 5.0, 68.5, "HC_A", "HC_LED", lcsc="C17414")
        # OLED, lid button and spare GPIOs: in the open middle, where the leads have the case's full height
        self.add("Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical", "J13", "OLED", 50.0, 40.0, rot=90,
                 nets={"1": "GND", "2": "3V3", "3": "SCL_GPIO8", "4": "SDA_GPIO7"}).Reference().SetVisible(False)
        self.text("OLED  G V SCL SDA", 53.8, 37.6, 0.8)
        self.add("Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical", "J14", "BTN", 50.0, 48.5, rot=90,
                 nets={"1": "BTN_GPIO47", "2": "GND"}).Reference().SetVisible(False)
        self.text("BTN 47 G", 51.3, 51.5, 0.8)
        self.add("Connector_PinHeader_2.54mm", "PinHeader_2x03_P2.54mm_Vertical", "J15", "SPARE", 66.0, 47.04, rot=90,
                 nets={"1": "3V3", "2": "GND", "3": "GPIO48", "4": "GPIO19", "5": "GPIO18", "6": "GPIO15"}).Reference().SetVisible(False)
        self.text("GND 19 15", 68.5, 42.0, 0.8)
        self.text("3V3 48 18", 68.5, 49.6, 0.8)
        for ref in ("J13", "J14", "J15"):       # Dupont leads are taller than the room under the ESP board
            fp = next(f for f in self.b.GetFootprints() if f.GetReference() == ref)
            for pad in fp.Pads():
                if pcbnew.ToMM(pad.GetPosition().x) < DIN_ESP_X0 + ESP_W + 2 + 1.27:
                    raise SystemExit(f"{ref} pad {pad.GetNumber()} is under (or too near) the ESP board")
        for k, n in ((3, "HEAT_GPIO46"), (1, "BTN_GPIO47"), (0, "GPIO48")):
            self.repin("J1", k, n)
        for k, n in ((14, "CT1_GPIO16"), (15, "CT2_GPIO17"), (4, "SCL_GPIO8"), (3, "SDA_GPIO7")):
            self.repin("J2", k, n)
        for name, side, cx, gpio in DIN_PORTS:
            row = "J1" if gpio in RESET_ROW else "J2"
            k = (RESET_ROW if row == "J1" else BOOT_ROW).index(gpio)
            self.repin(row, k, f"{name}_{gpio}")
        for i, (x, y) in enumerate(DIN_HOLES):
            self.add("MountingHole", "MountingHole_3.2mm_M3", f"H{i + 1}", "M3", x, y).Reference().SetVisible(False)
        self.text("Boiler probe carrier, DIN rail  ESP32-P4-ETH  " + ("THT" if tht else "SMD") + "  v1", 62.0, 33.0,
                  1.2, layer=pcbnew.B_SilkS, bold=True)
        self.text("USB", DIN_ESP_X0 + ESP_W / 2, 34.0, 0.9)
        self.text("RJ45", DIN_ESP_X0 + ESP_W / 2, 68.5, 0.9)

    def build_box(self):
        tht = self.variant == "tht"
        # ESP32-P4-ETH sockets: pad 1 at k = 0 (RJ45 end), pads along +x
        for ref, row, y in (("J1", RESET_ROW, ROW_RESET), ("J2", BOOT_ROW, ROW_BOOT)):
            self.add("Connector_PinSocket_2.54mm", "PinSocket_1x20_P2.54mm_Vertical", ref, "ESP32-P4-ETH",
                     PIN0_X, y, rot=90, nets={str(k + 1): n for k, n in enumerate(row)})
        # check the socket runs the right way
        j1 = next(f for f in self.b.GetFootprints() if f.GetReference() == "J1")
        p2 = next(p for p in j1.Pads() if p.GetNumber() == "2")
        if abs(pcbnew.ToMM(p2.GetPosition().x) - (PIN0_X + 2.54)) > 0.01:
            raise SystemExit("socket orientation is wrong")
        # probe ports and their pull-ups
        for i, (name, side, cx, gpio) in enumerate(PORTS):
            n = f"{name}_{gpio}"
            if side == "front":
                self.terminal(f"J{3 + i}", name, cx - 5.08, 50.0, "front", {"1": "GND", "2": n, "3": "3V3"})
                ry = 44.5
            else:
                self.terminal(f"J{3 + i}", name, cx + 5.08, 10.0, "back", {"1": "GND", "2": n, "3": "3V3"})
                ry = 15.5
            if tht:     # the back row sits 1.6 mm further left, clear of the CT1 jack
                self.resistor(f"R{1 + i}", "4.7k", cx - (5.08 if side == "front" else 6.7), ry, n, "3V3")
            else:
                r = self.resistor(f"R{1 + i}", "4.7k", cx - 1.0, ry, n, "3V3", lcsc="C17673")
                if side == "back":              # its name below it, clear of the pin letters above
                    r.Reference().SetPosition(pt(cx, ry + 1.7))
            ty = 57.2 if side == "front" else 2.6
            self.text(f"{name}  {gpio}", cx, ty, 1.2)
            self.pin_labels(f"J{3 + i}", ("G", "D", "V"), 0, -3.4 if side == "front" else 3.4)
        # heat call (24 VAC, isolated): J11 pins 1 and 3, PC814 (AC input). The circuit sits between the ESP sockets,
        # under the ESP board (11 mm above: room for low parts).
        self.terminal("J11", "HEAT", 90.5, 55.16, "right", {"1": "HC_A", "3": "HC_B"})
        self.pin_labels("J11", ("AC", "", "AC"), -3.0, 0, pcbnew.B_SilkS, 0.8)    # 24 VAC, on the outer pins
        self.text("HEAT", 96.0, 41.0, 0.9)
        # pump current clamps: two 3.5 mm jacks (CUI SJ1-3523N), plug face flush with the right edge. The SCT-013's
        # plug uses tip and sleeve only: sleeve to the ADC input, tip to BIAS (OpenEnergyMonitor's convention), ring
        # not connected.
        for ref, label, y, gpio in (("J12", "CT1", 19.0, "CT1_GPIO16"), ("J16", "CT2", 32.0, "CT2_GPIO17")):
            jk = self.add("Connector_Audio", "Jack_3.5mm_CUI_SJ1-3523N_Horizontal", ref, label, W - 6.3, y, 90,
                          {"S": gpio, "T": "BIAS"})
            jk.Reference().SetVisible(False)
            for g in list(jk.GraphicalItems()):      # the library footprint draws a board-edge cutout: not wanted here
                if g.GetLayer() == pcbnew.Edge_Cuts:
                    jk.Remove(g)
            for g in jk.GraphicalItems():            # its silkscreen runs past the edge (the plug end is flush with
                if g.GetLayer() == pcbnew.F_SilkS:   # it): to the fabrication drawing, and a plain outline instead
                    g.SetLayer(pcbnew.F_Fab)
            r = pcbnew.PCB_SHAPE(self.b)
            r.SetShape(pcbnew.SHAPE_T_RECT)
            r.SetStart(pt(W - 6.3 - 7.7 + 0.1, y - 6.0)); r.SetEnd(pt(W - 0.2, y + 6.0))
            r.SetLayer(pcbnew.F_SilkS); r.SetWidth(MM(0.12))
            self.b.Add(r)
            self.text(label, 81.5, y, 1.0, bold=True)
        if tht:
            self.resistor("R10", "10k", 44.5, 25.0, "HEAT_GPIO46", "3V3")
            self.capacitor("C1", "1uF", 44.5, 28.5, "HEAT_GPIO46", "GND")
            self.capacitor("C2", "10uF", 52.5, 28.5, "BIAS", "GND")
            self.resistor("R11", "10k", 44.5, 32.0, "3V3", "BIAS")
            self.resistor("R12", "10k", 44.5, 35.5, "BIAS", "GND")
            self.ref_inside(self.add("Package_DIP", "DIP-4_W7.62mm", "U1", "PC814", 61.0, 27.0, 0,
                                     {"1": "HC_LED", "2": "HC_B", "3": "GND", "4": "HEAT_GPIO46"}))
            self.resistor("R9", "10k", 58.5, 34.5, "HC_A", "HC_LED")
            self.capacitor("C3", "100nF", 72.0, 27.0, "3V3", "GND", rot=-90)
        else:
            self.resistor("R10", "10k", 45.0, 25.5, "HEAT_GPIO46", "3V3", lcsc="C17414")
            self.capacitor("C1", "1uF", 49.0, 25.5, "HEAT_GPIO46", "GND", lcsc="C28323")
            self.capacitor("C2", "10uF", 53.0, 25.5, "BIAS", "GND", lcsc="C15850")
            self.resistor("R11", "10k", 45.0, 31.0, "3V3", "BIAS", lcsc="C17414")
            self.resistor("R12", "10k", 49.0, 31.0, "BIAS", "GND", lcsc="C17414")
            self.add("Package_SO", "SO-4_4.4x3.6mm_P2.54mm", "U1", "EL814S1(A)(TU)-V", 60.0, 28.0, 0,
                     {"1": "HC_LED", "2": "HC_B", "3": "GND", "4": "HEAT_GPIO46"}, lcsc="C500388")
            self.resistor("R9", "10k", 58.0, 34.5, "HC_A", "HC_LED", lcsc="C17414")
            self.capacitor("C3", "100nF", 72.0, 30.0, "3V3", "GND", lcsc="C49678")
        # OLED (SSD1306 module order GND VCC SCL SDA), lid button, spare GPIOs: beside the ESP board, clear of the
        # RESET/BOOT paperclip paths at x = 16.1
        self.add("Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical", "J13", "OLED", 5.0, 10.5, rot=90,
                 nets={"1": "GND", "2": "3V3", "3": "SCL_GPIO8", "4": "SDA_GPIO7"}).Reference().SetVisible(False)
        self.text("OLED  G V SCL SDA", 8.8, 8.3, 0.8)
        self.add("Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical", "J14", "BTN", 10.5, 53.0, rot=90,
                 nets={"1": "BTN_GPIO47", "2": "GND"}).Reference().SetVisible(False)
        self.text("BTN 47 G", 11.8, 56.0, 0.8)
        self.add("Connector_PinHeader_2.54mm", "PinHeader_2x03_P2.54mm_Vertical", "J15", "SPARE", 2.5, 48.04, rot=90,
                 nets={"1": "3V3", "2": "GND", "3": "GPIO48", "4": "GPIO19", "5": "GPIO18", "6": "GPIO15"}).Reference().SetVisible(False)
        # rotated, the header's odd pins (1 = 3V3, square) are the lower row and its even pins the upper row
        self.text("GND 19 15", 5.0, 43.0, 0.8)
        self.text("3V3 48 18", 5.0, 50.6, 0.8)
        # the headers take Dupont leads, taller than the 11 mm under the ESP board: keep them 2 mm clear of its outline
        for ref in ("J13", "J14", "J15"):
            fp = next(f for f in self.b.GetFootprints() if f.GetReference() == ref)
            for pad in fp.Pads():
                py = pcbnew.ToMM(pad.GetPosition().y)
                if ESP_Y0 - 2 - 1.27 < py < ESP_Y0 + ESP_W + 2 + 1.27:
                    raise SystemExit(f"{ref} pad {pad.GetNumber()} at y={py:.2f} is under (or too near) the ESP board")
        # socket-side nets that go to the extras
        for k, n in ((3, "HEAT_GPIO46"), (1, "BTN_GPIO47"), (0, "GPIO48")):
            self.repin("J1", k, n)
        for k, n in ((14, "CT1_GPIO16"), (15, "CT2_GPIO17"), (4, "SCL_GPIO8"), (3, "SDA_GPIO7")):
            self.repin("J2", k, n)
        for name, side, cx, gpio in PORTS:
            row = "J1" if gpio in RESET_ROW else "J2"
            k = (RESET_ROW if row == "J1" else BOOT_ROW).index(gpio)
            self.repin(row, k, f"{name}_{gpio}")
        # mounting holes (M3) in the corners left free
        for i, (x, y) in enumerate(HOLES):
            self.add("MountingHole", "MountingHole_3.2mm_M3", f"H{i + 1}", "M3", x, y).Reference().SetVisible(False)
        self.text("Boiler probe carrier  ESP32-P4-ETH  " + ("THT" if tht else "SMD") + "  v2", 52.0, 57.0, 1.2,
                  layer=pcbnew.B_SilkS, bold=True)
        self.text("RESET >", 16.1, 41.8, 0.8)
        self.text("< BOOT", 16.1, 18.2, 0.8)

    def repin(self, ref, k, net):
        fp = next(f for f in self.b.GetFootprints() if f.GetReference() == ref)
        next(p for p in fp.Pads() if p.GetNumber() == str(k + 1)).SetNet(self.net(net))


def finish(path_in, ses, path_out, layout="box"):
    use_layout(layout)
    b = pcbnew.LoadBoard(path_in)
    if not pcbnew.ImportSpecctraSES(b, ses):
        raise SystemExit("SES import failed")
    gnd = b.FindNet("GND")
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        z = pcbnew.ZONE(b)
        z.SetLayer(layer)
        z.SetNet(gnd)
        ol = z.Outline()
        ol.NewOutline()
        for x, y in ((0.5, 0.5), (W - 0.5, 0.5), (W - 0.5, H - 0.5), (0.5, H - 0.5)):
            ol.Append(MM(x), MM(y))
        z.SetLocalClearance(MM(0.3))
        z.SetMinThickness(MM(0.25))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
        z.SetThermalReliefGap(MM(0.4))
        z.SetThermalReliefSpokeWidth(MM(0.5))
        b.Add(z)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(path_out)


def clean(path_in, path_out):
    """Take off the short stubs Freerouting now and then leaves ending in mid-air (nothing hangs on them). Its own
    step, in its own process: removing things from a board upsets KiCad's Python for whatever comes after."""
    b = pcbnew.LoadBoard(path_in)
    b.BuildConnectivity()
    conn = b.GetConnectivity()
    stubs = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T and conn.TestTrackEndpointDangling(t, False)]
    for t in stubs:
        b.Remove(t)
    pcbnew.SaveBoard(path_out, b)
    print("removed", len(stubs), "dangling track ends")


if __name__ == "__main__":
    if sys.argv[1] == "place":
        brd = Board(sys.argv[2], sys.argv[4] if len(sys.argv) > 4 else "box")
        brd.build()
        brd.b.Save(sys.argv[3])
        with open(os.path.splitext(sys.argv[3])[0] + "-bom-raw.csv", "w", newline="") as f:
            csv.writer(f).writerows([("ref", "value", "footprint", "lcsc")] + brd.bom)
        if not pcbnew.ExportSpecctraDSN(brd.b, os.path.splitext(sys.argv[3])[0] + ".dsn"):
            raise SystemExit("DSN export failed")
        print("placed", len(brd.b.GetFootprints()), "footprints,", len(brd.nets), "nets")
    elif sys.argv[1] == "clean":
        clean(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "finish":
        finish(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else "box")
