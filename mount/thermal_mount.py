#!/usr/bin/env python3
"""Parametric 3D-printed case: GY-MCU90640 thermal sensor on top of the UnitV2.

The board sits upside down (sensor can at the bottom, close to the camera) in a slim closed
case centred above the UnitV2 lens. Only the sensor lens shows through the front.

Parts:
  shell    front, side, top and bottom walls, open at the back. Two M3 heat-set inserts in
           the strip above the board take the screws that close the case.
  lid_arm  back cover + arm. The board screws onto two bosses on the cover (M3 screws from
           the sensor side, heat-set inserts in the bosses). A rim locates the cover in the
           shell. A slot lets the Grove plug through. A 45-degree fin carries a pin bar that
           fixes to the UnitV2's LEGO back brick with three Technic friction pins.

Assembly:
  1. Heat-set inserts: 2 in the lid bosses, 2 in the shell's back face.
  2. Solder the Grove cable to VIN/GND/RX/TX, push the plug out through the lid slot.
  3. Screw the board to the lid bosses (screws from the sensor side).
  4. Slide the shell on from the front; 2 screws through the lid.
  5. Push the 3 Technic pins into the brick and press the pin bar onto them.
     The cable runs down the fin and the back of the pin bar to the Grove port.

    pip install cadquery matplotlib
    python3 thermal_mount.py [--render] [--set NAME=VALUE ...]

Coordinates (mm), seen from in front of the UnitV2:
    X  across the UnitV2, 0 at its left side
    Y  depth, 0 at its front face, positive towards the back
    Z  up, 0 at its bottom (top face at body_h)
"""

import argparse
import os

import cadquery as cq

# ---------------------------------------------------------------------------
# Parameters. (measure) = taken from drawings/photos, check on your parts.
# ---------------------------------------------------------------------------
P = dict(
    # UnitV2, M5Stack U078-D drawing, docs/reference/unitv2-u078d-*.png
    # (first-angle projection: vents on top, USB-C + Grove at the bottom)
    body_w=24.3,
    body_h=48.0,
    body_d=18.5,              # (measure) front face to back face, without the brick
    lens_d=9.5,
    lens_from_top=12.1,
    # Back brick (LEGO compatible)
    brick_t=8.0,              # (measure) back-brick base: body back face to brick back face
    brick_w=28.0,
    brick_h=26.7,
    brick_center_from_top=18.0,   # (measure)

    # LEGO Technic
    pitch=8.0,
    pin_hole_d=4.9,           # tuned with pin_test.stl, 0.6 mm nozzle: PLA -> 4.9, PETG -> 5.0
    pin_d=4.8,
    cbore_d=6.3,
    cbore_depth=0.8,
    pin_holes=((0, -1), (0, 0), (0, 1)),   # brick holes used (across, up) in pitch units: centre column

    # GY-MCU90640 board, product photo (15.1 x 25.1). "top" = edge nearest the can.
    pcb_w=15.1,
    pcb_h=25.1,
    pcb_t=1.6,                # (measure)
    can_d=9.3,                # measured: lens assembly diameter
    can_h=5.0,                # (measure) board front face to can top
    can_from_top=5.3,         # (measure) can centre from the board's top edge
    pcb_hole_spacing=10.0,    # measured: centre-to-centre, holes symmetric about the board centre
    pcb_hole_from_top=10.8,   # from measured 2.7 mm can rim to hole centre: 5.3 + sqrt((4.7+2.7)^2 - 5^2)
    pin_col_x=1.3,            # VIN/GND/RX/TX column from the board's left edge (front view, can up)
    pin_first_from_top=15.5,
    pin_last_from_top=23.2,

    # Case
    air_gap=3.0,              # between UnitV2 top (vents) and case bottom
    wall=2.0,
    lip_t=0.8,                # front lip in front of the can
    lip_overlap=0.0,          # >0: the lip covers the can's rim (0 = lens passes through the hole)
    can_gap=0.3,              # can top to lip (the lip doesn't press on the can)
    bore_clear=0.4,
    pcb_clear=0.6,            # board outline position is photo-derived; extra room so it cannot jam
    back_space=5.0,           # board back to lid = boss length (back-side parts ~1 mm)
    lid_t=2.5,
    rim_t=1.2,                # locating rim on the lid, inside the shell opening
    rim_d=1.5,
    rim_clear=0.15,
    corner_r=3.0,             # outline corner radius (front view)
    edge_r=1.0,               # front / back face edge rounding

    # Heat-set inserts M3 x L x 4.2 and screws
    insert_hole_d=4.0,
    case_insert_len=6,        # 2 in the shell top strip (M3x6x4.2), screws M3x8 through the lid
    case_insert_dx=5.0,       # +- from centre
    boss_insert_len=4,        # lid bosses (M3x4x4.2), board screws M3x6 from the front
    boss_d=6.2,
    screw_clear_d=3.4,

    # Grove plug slot in the lid (HY2.0-4P housing ~10 x 5 mm plus latch)
    grove_slot_w=11.0,        # (measure your plug)
    grove_slot_h=6.8,
    grove_slot_dx=1.5,        # towards the pin column

    # Arm
    pin_bar_t=8.0,            # one Technic module: 2L pins seat fully
    arm_fillet_in=5.0,
    arm_fillet_out=3.0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def box(x0, x1, y0, y1, z0, z1):
    return cq.Solid.makeBox(x1 - x0, y1 - y0, z1 - z0, cq.Vector(x0, y0, z0))


def cyl_y(x, z, d, y0, y1):
    return cq.Solid.makeCylinder(d / 2.0, y1 - y0, cq.Vector(x, y0, z), cq.Vector(0, 1, 0))


def prism_yz(pts, x0, x1):
    wire = cq.Wire.makePolygon([cq.Vector(x0, y, z) for y, z in pts], close=True)
    return cq.Solid.extrudeLinear(cq.Face.makeFromWires(wire), cq.Vector(x1 - x0, 0, 0))


def prism_xz(pts, y0, y1):
    wire = cq.Wire.makePolygon([cq.Vector(x, y0, z) for x, z in pts], close=True)
    return cq.Solid.extrudeLinear(cq.Face.makeFromWires(wire), cq.Vector(0, y1 - y0, 0))


def try_fillet(solid, r, edges):
    edges = list(edges)
    if not edges:
        return solid
    try:
        out = solid.fillet(r, edges)
        return out if out.isValid() else solid
    except Exception:  # noqa: BLE001 - cosmetic
        return solid


def edges_sel(solid, *selectors):
    wp = cq.Workplane().add(solid).edges(selectors[0])
    for s in selectors[1:]:
        wp = wp.edges(s)
    return wp.vals()


def edges_near(solid, pts, tol=0.05):
    pts = [cq.Vector(*p) for p in pts]
    return [e for e in solid.Edges() if any((e.Center() - p).Length < tol for p in pts)]


def rounded_rect_y(x0, x1, z0, z1, y0, y1, r):
    """Box along Y with rounded corners in the XZ plane."""
    b = box(x0, x1, y0, y1, z0, z1)
    return try_fillet(b, r, edges_sel(b, "|Y"))


def fuse_all(shapes):
    out = shapes[0]
    for s in shapes[1:]:
        out = out.fuse(s)
    return out.clean()


# ---------------------------------------------------------------------------
# Derived geometry
# ---------------------------------------------------------------------------

def derive(p):
    d = dict(p)
    d["xc"] = p["body_w"] / 2.0
    d["lens_z"] = p["body_h"] - p["lens_from_top"]
    d["zc"] = p["body_h"] - p["brick_center_from_top"]
    d["yb"] = p["body_d"] + p["brick_t"]

    d["cav_hw"] = max(p["pcb_w"] / 2 + p["pcb_clear"], p["pcb_hole_spacing"] / 2 + p["boss_d"] / 2 + 0.3)
    d["out_hw"] = d["cav_hw"] + p["wall"]
    d["x0"], d["x1"] = d["xc"] - d["out_hw"], d["xc"] + d["out_hw"]

    d["z0"] = p["body_h"] + p["air_gap"]
    d["z_floor"] = d["z0"] + p["wall"]
    d["z_pcb_bot"] = d["z_floor"] + p["pcb_clear"]
    d["z_can"] = d["z_pcb_bot"] + p["can_from_top"]
    d["z_pcb_top"] = d["z_pcb_bot"] + p["pcb_h"]
    d["z_cav_top"] = d["z_pcb_top"] + p["pcb_clear"]
    r_ins = p["insert_hole_d"] / 2
    d["z_ins"] = d["z_cav_top"] + 1.0 + r_ins                # top strip inserts
    d["z1"] = d["z_ins"] + r_ins + 1.4

    d["y_pcb_front"] = p["lip_t"] + p["can_gap"] + p["can_h"]
    d["y_pcb_back"] = d["y_pcb_front"] + p["pcb_t"]
    d["y_shell"] = d["y_pcb_back"] + p["back_space"]
    d["y_lid"] = d["y_shell"] + p["lid_t"]

    zh = d["z_pcb_bot"] + p["pcb_hole_from_top"]
    d["pcb_holes"] = [(d["xc"] - p["pcb_hole_spacing"] / 2, zh), (d["xc"] + p["pcb_hole_spacing"] / 2, zh)]
    d["case_screws"] = [(d["xc"] - p["case_insert_dx"], d["z_ins"]), (d["xc"] + p["case_insert_dx"], d["z_ins"])]
    # upside down: the photo-left pin column ends up on the right (front view)
    d["wire_x"] = d["xc"] + p["pcb_w"] / 2 - p["pin_col_x"]
    d["wire_z"] = (d["z_pcb_bot"] + p["pin_first_from_top"], d["z_pcb_bot"] + p["pin_last_from_top"])

    d["holes"] = [(d["xc"] + i * p["pitch"], d["zc"] + j * p["pitch"]) for i, j in p["pin_holes"]]
    d["y_bar_back"] = d["yb"] + p["pin_bar_t"]
    d["z_bar_bot"] = min(h[1] for h in d["holes"]) - 5.0
    # 45-degree fin from the bar's back edge up to the lid
    d["fin_top"] = d["z0"] + (d["y_bar_back"] - d["y_lid"])
    # Grove slot: between the fin top and the case screws
    lo = d["fin_top"] + 1.5
    hi = d["z_ins"] - p["screw_clear_d"] / 2 - 0.8
    d["slot_z"] = (lo + hi) / 2
    d["slot_x"] = d["xc"] + p["grove_slot_dx"]
    d["slot_room"] = hi - lo
    return d


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------

def build_shell(p):
    d = derive(p)
    s = rounded_rect_y(d["x0"], d["x1"], d["z0"], d["z1"], 0, d["y_shell"], p["corner_r"])
    s = try_fillet(s, p["edge_r"], edges_sel(s, "<Y"))                       # soften the front face
    s = s.cut(box(d["xc"] - d["cav_hw"], d["xc"] + d["cav_hw"], p["wall"], d["y_shell"] + 1,
                  d["z_floor"], d["z_cav_top"]))
    s = s.cut(cyl_y(d["xc"], d["z_can"], p["can_d"] + p["bore_clear"], p["lip_t"], p["wall"] + 0.5))
    s = s.cut(cyl_y(d["xc"], d["z_can"], p["can_d"] + p["bore_clear"] - 2 * p["lip_overlap"], -1, p["lip_t"] + 0.01))
    depth = p["case_insert_len"] + 0.5
    for x, z in d["case_screws"]:
        s = s.cut(cyl_y(x, z, p["insert_hole_d"], d["y_shell"] - depth, d["y_shell"] + 1))
    return s.clean()


def build_arm(p, d):
    """Side profile (YZ) extruded across the case width: fin + pin bar."""
    y0 = d["y_lid"] - 1.0                                     # starts inside the lid
    yb, yk, z0, zb = d["yb"], d["y_bar_back"], d["z0"], d["z_bar_bot"]
    prof = [(y0, z0), (yb, z0), (yb, zb), (yk, zb), (yk, z0), (y0, z0 + (yk - y0))]
    arm = prism_yz(prof, d["x0"], d["x1"])
    xm = d["xc"]
    arm = try_fillet(arm, p["arm_fillet_in"], edges_near(arm, [(xm, yb, z0)]))
    arm = try_fillet(arm, p["arm_fillet_out"], edges_near(arm, [(xm, yb, zb), (xm, yk, zb), (xm, yk, z0)]))
    side_edges = [e for e in arm.Edges() if abs(e.Center().x - d["x0"]) < 1e-3 or abs(e.Center().x - d["x1"]) < 1e-3]
    side_edges = [e for e in side_edges if e.Center().y > d["y_lid"] + 0.5]   # not where it joins the lid
    return try_fillet(arm, 0.8, side_edges)


def build_lid_arm(p):
    d = derive(p)
    lid = rounded_rect_y(d["x0"], d["x1"], d["z0"], d["z1"], d["y_shell"], d["y_lid"], p["corner_r"])

    # Locating rim inside the shell opening.
    o = d["cav_hw"] - p["rim_clear"]
    zr0, zr1 = d["z_floor"] + p["rim_clear"], d["z_cav_top"] - p["rim_clear"]
    ys = d["y_shell"]
    rim = box(d["xc"] - o, d["xc"] + o, ys - p["rim_d"], ys + 0.01, zr0, zr1).cut(
        box(d["xc"] - o + p["rim_t"], d["xc"] + o - p["rim_t"], ys - p["rim_d"] - 1, ys + 1,
            zr0 + p["rim_t"], zr1 - p["rim_t"]))

    # Board bosses: round + 45-degree chin towards -X (the print bed), clipped to slide into
    # the shell, and kept 1 mm back from the board so only the round face touches it.
    r = p["boss_d"] / 2
    clip = box(d["xc"] - d["cav_hw"] + 0.3, d["xc"] + d["cav_hw"] - 0.3, d["y_pcb_back"] - 1, ys + 0.5,
               d["z_floor"] + 0.3, d["z_cav_top"] - 0.3)
    bosses = []
    for x, z in d["pcb_holes"]:
        chin = prism_xz([(x, z - r), (x - r * 1.4142, z), (x, z + r)], d["y_pcb_back"] + 1.0, ys + 0.01)
        bosses.append(cyl_y(x, z, p["boss_d"], d["y_pcb_back"], ys + 0.01).fuse(chin).intersect(clip))

    m = fuse_all([lid, rim, build_arm(p, d)] + bosses)
    m = try_fillet(m, p["edge_r"] * 0.8, [e for e in edges_sel(m, ">Y") if e.Center().z > d["fin_top"] + 1])

    for x, z in d["holes"]:
        m = m.cut(cyl_y(x, z, p["pin_hole_d"], d["yb"] - 1, d["y_bar_back"] + 1))
        m = m.cut(cyl_y(x, z, p["cbore_d"], d["yb"] - 1, d["yb"] + p["cbore_depth"]))
    for x, z in d["case_screws"]:
        m = m.cut(cyl_y(x, z, p["screw_clear_d"], ys - 1, d["y_lid"] + 1))
    for x, z in d["pcb_holes"]:
        m = m.cut(cyl_y(x, z, p["insert_hole_d"], d["y_pcb_back"] - 1, d["y_pcb_back"] + p["boss_insert_len"] + 0.5))
    w, h = p["grove_slot_w"], p["grove_slot_h"]
    slot = rounded_rect_y(d["slot_x"] - w / 2, d["slot_x"] + w / 2, d["slot_z"] - h / 2, d["slot_z"] + h / 2,
                          ys - p["rim_d"] - 1, d["y_lid"] + 1, 1.5)
    m = m.cut(slot)
    return m.clean()


def build_reference(p):
    d = derive(p)
    ref = {}
    ref["unitv2"] = box(0, p["body_w"], 0, p["body_d"], 0, p["body_h"]).fuse(
        cyl_y(d["xc"], d["lens_z"], p["lens_d"], -1.0, 0.01))
    brick = box(d["xc"] - p["brick_w"] / 2, d["xc"] + p["brick_w"] / 2, p["body_d"], d["yb"],
                d["zc"] - p["brick_h"] / 2, d["zc"] + p["brick_h"] / 2)
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            if abs(i) + abs(j) <= 1:
                brick = brick.cut(cyl_y(d["xc"] + 8 * i, d["zc"] + 8 * j, 4.85, p["body_d"] - 1, d["yb"] + 1))
    ref["brick"] = brick
    pcb = box(d["xc"] - p["pcb_w"] / 2, d["xc"] + p["pcb_w"] / 2, d["y_pcb_front"], d["y_pcb_back"],
              d["z_pcb_bot"], d["z_pcb_top"])
    for x, z in d["pcb_holes"]:
        pcb = pcb.cut(cyl_y(x, z, 3.2, 0, 20))
    ref["pcb"] = pcb
    ref["can"] = cyl_y(d["xc"], d["z_can"], p["can_d"], d["y_pcb_front"] - p["can_h"], d["y_pcb_front"])
    ref["pins"] = fuse_all([cyl_y(x, z, p["pin_d"], d["yb"] - 7.8, d["yb"] + 7.8) for x, z in d["holes"]])
    return ref


def build_pin_test(p):
    """Coupon with three hole sizes (nominal -0.1 / nominal / +0.1). Notches mark 1/2/3."""
    t, h, sp, n = 8.0, 10.0, 11.0, 3
    b = box(0, sp * n, 0, t, 0, h)
    bar = try_fillet(b, 1.0, edges_sel(b, "|Y"))
    for k in range(n):
        x = sp * (k + 0.5)
        bar = bar.cut(cyl_y(x, h / 2, p["pin_hole_d"] - 0.1 + 0.1 * k, -1, t + 1))
        bar = bar.cut(cyl_y(x, h / 2, p["cbore_d"], -1, p["cbore_depth"]))
        for m in range(k + 1):
            nx = x + (m - k / 2.0) * 1.6
            bar = bar.cut(box(nx - 0.4, nx + 0.4, -1, t + 1, h - 0.8, h + 1))
    return bar.clean()


# ---------------------------------------------------------------------------
# Checks, export, preview
# ---------------------------------------------------------------------------

def to_bed(shape, axis, ang):
    r = shape.rotate(cq.Vector(0, 0, 0), cq.Vector(*axis), ang)
    bb = r.BoundingBox()
    return r.translate(cq.Vector(-bb.xmin, -bb.ymin, -bb.zmin))


def fit_report(shell, lid, ref, d, p):
    print("fit check (overlap should be 0):")
    ok = True
    pairs = [("shell", shell, "lid", lid)] + \
            [("shell", shell, k, ref[k]) for k in ("unitv2", "pcb", "can")] + \
            [("lid", lid, k, ref[k]) for k in ("unitv2", "brick", "pcb", "can", "pins")]
    for an, a, bn, b in pairs:
        v = a.intersect(b).Volume()
        good = v < 1e-3
        ok &= good
        print("  %-5s vs %-6s overlap %7.3f mm3  min gap %5.2f mm  %s" % (an, bn, v, a.distance(b), "OK" if good else "COLLISION"))
    slot_ok = d["slot_room"] >= p["grove_slot_h"]
    ok &= slot_ok
    print("grove slot %.1f x %.1f at z %.1f; room between fin top and case screws %.1f mm  %s" % (
        p["grove_slot_w"], p["grove_slot_h"], d["slot_z"], d["slot_room"], "OK" if slot_ok else "TOO SMALL"))
    print("wire pins z %.1f..%.1f, slot z %.1f..%.1f" % (d["wire_z"][0], d["wire_z"][1],
          d["slot_z"] - p["grove_slot_h"] / 2, d["slot_z"] + p["grove_slot_h"] / 2))
    print("board screw room: board front to front wall %.2f mm (socket head M3 = 3.0)" % (d["y_pcb_front"] - p["wall"]))
    print("lens (%.2f, %.2f)  sensor (%.2f, %.2f)  parallax %.1f mm (vertical)" % (
        d["xc"], d["lens_z"], d["xc"], d["z_can"], d["z_can"] - d["lens_z"]))
    for name, s in (("shell", shell), ("lid_arm", lid)):
        bb = s.BoundingBox()
        print("  %-7s %5.1f x %5.1f x %5.1f mm  %.1f cm3  valid=%s solids=%d" % (
            name, bb.xlen, bb.ylen, bb.zlen, s.Volume() / 1000, s.isValid(), len(s.Solids())))
    return ok


def export_all(p, outdir):
    os.makedirs(outdir, exist_ok=True)
    d = derive(p)
    shell, lid, ref = build_shell(p), build_lid_arm(p), build_reference(p)
    ok = fit_report(shell, lid, ref, d, p)

    stl = dict(tolerance=0.02, angularTolerance=0.1)
    shell_print = to_bed(shell, (1, 0, 0), 90)       # front face on the bed
    lid_print = to_bed(lid, (0, 1, 0), -90)          # left side on the bed
    cq.exporters.export(cq.Workplane().add(shell_print), os.path.join(outdir, "shell.stl"), **stl)
    cq.exporters.export(cq.Workplane().add(lid_print), os.path.join(outdir, "lid_arm.stl"), **stl)
    cq.exporters.export(cq.Workplane().add(build_pin_test(p)), os.path.join(outdir, "pin_test.stl"), **stl)

    assy = cq.Assembly(name="unitv2_thermal_case")
    assy.add(shell, name="shell", color=cq.Color(0.2, 0.22, 0.25))
    assy.add(lid, name="lid_arm", color=cq.Color(0.3, 0.32, 0.36))
    assy.export(os.path.join(outdir, "thermal_case.step"))
    colors = {"unitv2": (0.85, 0.85, 0.88), "brick": (0.95, 0.75, 0.2), "pcb": (0.1, 0.1, 0.12),
              "can": (0.3, 0.3, 0.3), "pins": (0.15, 0.15, 0.15)}
    for k, v in ref.items():
        assy.add(v, name="ref_" + k, color=cq.Color(*colors[k]))
    assy.export(os.path.join(outdir, "thermal_case_assembly.step"))
    print("wrote shell.stl, lid_arm.stl, pin_test.stl, thermal_case.step, thermal_case_assembly.step to %s" % outdir)
    return shell, lid, ref, shell_print, lid_print, ok


def render(items, path, elev, azim, title, ortho=False):
    import math
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    e, a = math.radians(elev), math.radians(azim)
    light = np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)]) + np.array([0.3, -0.2, 0.6])
    light /= np.linalg.norm(light)
    tris, cols = [], []
    for shape, rgb in items:
        verts, faces = shape.tessellate(0.05, 0.15)
        v = np.array([[q.x, q.y, q.z] for q in verts])
        for f in faces:
            t = v[list(f)]
            n = np.cross(t[1] - t[0], t[2] - t[0])
            nn = np.linalg.norm(n)
            if nn:
                s = 0.35 + 0.65 * abs(np.dot(n / nn, light))
                tris.append(t)
                cols.append(tuple(min(1, c * s) for c in rgb))
    fig = plt.figure(figsize=(7, 6), dpi=110)
    ax = fig.add_subplot(111, projection="3d")
    if ortho:
        ax.set_proj_type("ortho")
    ax.add_collection3d(Poly3DCollection(tris, facecolors=cols, edgecolors="none"))
    allv = np.concatenate(tris)
    lo, hi = allv.min(0), allv.max(0)
    ax.set_xlim(lo[0], hi[0]); ax.set_ylim(lo[1], hi[1]); ax.set_zlim(lo[2], hi[2])
    ax.set_box_aspect(hi - lo)
    ax.view_init(elev=elev, azim=azim)
    ax.set_xlabel("X"); ax.set_ylabel("Y (back)"); ax.set_zlabel("Z")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def render_all(shell, lid, ref, shell_print, lid_print, outdir):
    case, lidc, grey = (0.95, 0.5, 0.15), (0.98, 0.68, 0.32), (0.8, 0.8, 0.83)
    yellow, dark = (0.95, 0.78, 0.25), (0.25, 0.27, 0.3)
    base = [(ref["unitv2"], grey), (ref["brick"], yellow), (ref["pins"], (0.2, 0.2, 0.2))]
    render(base + [(lid, lidc), (shell, case)], os.path.join(outdir, "preview_front.png"), 15, -60, "assembled, front-right")
    render(base + [(lid, lidc), (shell, case)], os.path.join(outdir, "preview_back.png"), 20, 50, "assembled, back-right")
    render(base + [(lid, lidc), (shell, case)], os.path.join(outdir, "preview_side.png"), 0, 0, "side (front at left)", ortho=True)
    exploded = shell.translate(cq.Vector(0, -18, 0))
    render(base + [(lid, lidc), (ref["pcb"], dark), (ref["can"], dark), (exploded, case)],
           os.path.join(outdir, "preview_exploded.png"), 15, -55, "shell pulled forward: board on lid bosses")
    render([(shell_print, case)], os.path.join(outdir, "preview_print_shell.png"), 35, -60, "shell, print orientation")
    render([(lid_print, lidc)], os.path.join(outdir, "preview_print_lid.png"), 35, -60, "lid + arm, print orientation")
    print("wrote preview PNGs to %s" % outdir)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    ap.add_argument("--render", action="store_true", help="write preview PNGs (needs matplotlib)")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="override a parameter")
    a = ap.parse_args()
    p = dict(P)
    for kv in a.set:
        k, v = kv.split("=", 1)
        if k not in P or isinstance(P[k], tuple):
            ap.error("unknown or non-scalar parameter %s" % k)
        p[k] = type(P[k])(v)
    shell, lid, ref, sp, lp, ok = export_all(p, a.out)
    if a.render:
        render_all(shell, lid, ref, sp, lp, a.out)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
