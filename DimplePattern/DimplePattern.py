# DimplePattern -- a Fusion 360 add-in that cuts an aperiodic, depth-randomized
# spherical-cap dimple texture into a selected planar face.
#
# Copyright (C) 2026 Joshua Jacobs
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Dimple Pattern -- Fusion 360 Add-In.

Adds a "Dimple Pattern" command (Solid > Create) that cuts randomized, aperiodic,
depth-randomized spherical-cap dimples into a selected planar face -- an acoustic scattering
texture for an ultrasonic delay line. All parameters are exposed in the command dialog.

Geometry core is identical to the validated standalone script:
  * Poisson-disk (Bridson) blue-noise interior.
  * Interior gap fill.
  * Border densify: full-size, depth-randomized dimples reaching each edge at a >= border
    spacing floor (features stay discrete / wavelength-scale -- no sub-lambda homogenization).
  * Corner nestle: 1-2 dimples per convex corner with relaxed depth/spacing, kept INSIDE the
    face (edge_clearance respected -- no spill). Generic corner detection (any polygon).
  * History guardrail: appends only; never edits/deletes existing timeline features.
"""

import adsk.core
import adsk.fusion
import traceback
import math
import random

MM_TO_CM = 0.1

# module-level so run()/stop() and the handlers share them
app = adsk.core.Application.get()
ui = app.userInterface
_handlers = []                      # keep event-handler objects alive
CMD_ID = 'dimplePatternCmdId'
CMD_NAME = 'Dimple Pattern'
CMD_TOOLTIP = 'Cut a randomized, aperiodic spherical-cap dimple texture into a selected planar face.'
PANEL_ID = 'SolidCreatePanel'

DEFAULTS = {
    'ball_diameter': 3.175, 'depth_min': 0.50, 'depth_max': 1.55,
    'min_spacing': 1.90, 'edge_clearance': 0.30, 'keepout_clearance': 1.00,
    'border_spacing': 0.91, 'corner_depth_min': 0.35,
    'corner_angle_thresh': 35.0, 'corner_max_per': 2, 'corner_fill': True,
    'fill_passes': 3, 'border_passes': 8, 'poisson_k': 30,
    'fill_raster': 0.25, 'border_raster': 0.20, 'fill_spacing_frac': 0.55,
    'seed': 20260717,
    'drill_points': True,
}


# =====================================================================================
# Geometry core (pure; millimetres)
# =====================================================================================

def crater_radius(a, h):
    if h <= 0:
        return 0.0
    if h >= a:
        return a
    return math.sqrt(2.0 * a * h - h * h)


def depth_for_radius(a, r):
    if r >= a:
        return a
    return a - math.sqrt(a * a - r * r)


def sphere_centre_offset(a, h):
    return a - h


def max_depth_at_point(a, d_edge, edge_clearance, depth_max):
    r_allow = d_edge - edge_clearance
    if r_allow <= 0:
        return 0.0
    if r_allow >= a:
        return depth_max
    return min(depth_max, depth_for_radius(a, r_allow))


def point_in_polygon(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-300) + xi):
            inside = not inside
        j = i
    return inside


def dist_point_to_segment(pt, p0, p1):
    px, py = pt
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    seg2 = dx * dx + dy * dy
    if seg2 == 0.0:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / seg2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def dist_to_polygon(pt, poly):
    best = float("inf")
    n = len(poly)
    for i in range(n):
        d = dist_point_to_segment(pt, poly[i], poly[(i + 1) % n])
        if d < best:
            best = d
    return best


def region_distance(pt, outer, inners, keepout_clearance):
    if not point_in_polygon(pt, outer):
        return None
    d = dist_to_polygon(pt, outer)
    for inner in inners:
        if point_in_polygon(pt, inner):
            return None
        di = dist_to_polygon(pt, inner) - keepout_clearance
        if di < d:
            d = di
    return d if d > 0 else None


def detect_convex_corners(poly, angle_thresh_deg):
    if len(poly) > 1 and math.hypot(poly[-1][0] - poly[0][0], poly[-1][1] - poly[0][1]) < 1e-9:
        poly = poly[:-1]
    n = len(poly)
    if n < 3:
        return []
    area2 = sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1]
                for i in range(n))
    ccw = area2 > 0.0
    out = []
    for i in range(n):
        pp = poly[(i - 1) % n]
        p = poly[i]
        pn = poly[(i + 1) % n]
        v1 = (p[0] - pp[0], p[1] - pp[1])
        v2 = (pn[0] - p[0], pn[1] - p[1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-9 or l2 < 1e-9:
            continue
        v1 = (v1[0] / l1, v1[1] / l1)
        v2 = (v2[0] / l2, v2[1] / l2)
        dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1]))
        turn = math.degrees(math.acos(dot))
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        is_convex = (cross > 0) if ccw else (cross < 0)
        if turn >= angle_thresh_deg and is_convex:
            bx, by = (-v1[0] + v2[0]), (-v1[1] + v2[1])
            bl = math.hypot(bx, by)
            if bl < 1e-9:
                bx, by = -v1[1], v1[0]
                bl = 1.0
            bx, by = bx / bl, by / bl
            if not point_in_polygon((p[0] + bx * 0.05, p[1] + by * 0.05), poly):
                bx, by = -bx, -by
            out.append((p, (bx, by), turn))
    return out


def generate_dimples(outer, inners, params):
    a = params["ball_radius"]
    r_min = params["min_spacing"]
    edge_clear = params["edge_clearance"]
    keepout_clear = params["keepout_clearance"]
    d_min = params["depth_min"]
    d_max = params["depth_max"]
    k = params["poisson_k"]

    if d_max >= a:
        raise ValueError("depth_max (%.4f) must be < ball radius (%.4f)" % (d_max, a))

    rng = random.Random(params["seed"])
    xs = [p[0] for p in outer]
    ys = [p[1] for p in outer]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    cell = r_min / math.sqrt(2.0)
    gw = int(math.ceil((xmax - xmin) / cell)) + 1
    gh = int(math.ceil((ymax - ymin) / cell)) + 1
    grid = [[None] * gh for _ in range(gw)]

    def gidx(p):
        return int((p[0] - xmin) / cell), int((p[1] - ymin) / cell)

    def far_enough(p):
        gx, gy = gidx(p)
        for ix in range(max(0, gx - 2), min(gw, gx + 3)):
            for iy in range(max(0, gy - 2), min(gh, gy + 3)):
                q = grid[ix][iy]
                if q is not None and math.hypot(p[0] - q[0], p[1] - q[1]) < r_min:
                    return False
        return True

    def admissible(p):
        d_edge = region_distance(p, outer, inners, keepout_clear)
        if d_edge is None:
            return None
        h_hi = max_depth_at_point(a, d_edge, edge_clear, d_max)
        if h_hi < d_min:
            return None
        return d_edge, h_hi

    seed_pt = None
    for _ in range(200000):
        p = (rng.uniform(xmin, xmax), rng.uniform(ymin, ymax))
        if admissible(p) is not None:
            seed_pt = p
            break
    if seed_pt is None:
        raise RuntimeError("No admissible point in the region (face too small for these settings).")

    samples = []
    active = []

    def add_sample(x, y, depth, d_edge):
        samples.append({"x": x, "y": y, "depth": depth,
                        "radius": crater_radius(a, depth), "d_edge": d_edge})

    def accept(p):
        info = admissible(p)
        if info is None:
            return False
        d_edge, h_hi = info
        add_sample(p[0], p[1], rng.uniform(d_min, h_hi), d_edge)
        gx, gy = gidx(p)
        grid[gx][gy] = p
        active.append(p)
        return True

    accept(seed_pt)
    while active:
        i = rng.randrange(len(active))
        base = active[i]
        placed = False
        for _ in range(k):
            ang = rng.uniform(0, 2 * math.pi)
            rad = rng.uniform(r_min, 2.0 * r_min)
            p = (base[0] + rad * math.cos(ang), base[1] + rad * math.sin(ang))
            if not (xmin <= p[0] <= xmax and ymin <= p[1] <= ymax):
                continue
            if not far_enough(p):
                continue
            if accept(p):
                placed = True
                break
        if not placed:
            active.pop(i)

    poisson_count = len(samples)

    def is_covered(c):
        for s in samples:
            if (c[0] - s["x"]) ** 2 + (c[1] - s["y"]) ** 2 <= s["radius"] ** 2:
                return True
        return False

    # interior gap fill
    if params["fill_passes"] > 0:
        step = params["fill_raster"]
        r_fill = r_min * params["fill_spacing_frac"]
        cells = []
        y = ymin
        while y <= ymax:
            x = xmin
            while x <= xmax:
                if region_distance((x, y), outer, inners, keepout_clear) is not None:
                    cells.append((x, y))
                x += step
            y += step
        for _ in range(params["fill_passes"]):
            open_cells = [c for c in cells if not is_covered(c)]
            if not open_cells:
                break
            rng.shuffle(open_cells)
            added = 0
            for c in open_cells:
                if is_covered(c):
                    continue
                info = admissible(c)
                if info is None:
                    continue
                d_edge, h_hi = info
                if any(math.hypot(c[0] - s["x"], c[1] - s["y"]) < r_fill for s in samples):
                    continue
                add_sample(c[0], c[1], rng.uniform(d_min, h_hi), d_edge)
                added += 1
            if added == 0:
                break

    fill_count = len(samples) - poisson_count

    # border densify (discrete, full-size, reaches toward each edge)
    b_spacing = params["border_spacing"]
    step = params["border_raster"]
    for _ in range(params["border_passes"]):
        cells = []
        y = ymin
        while y <= ymax:
            x = xmin
            while x <= xmax:
                de = region_distance((x, y), outer, inners, keepout_clear)
                if de is not None and de >= edge_clear and not is_covered((x, y)):
                    cells.append(((x, y), de))
                x += step
            y += step
        if not cells:
            break
        cells.sort(key=lambda cd: cd[1])
        added = 0
        for (c, de) in cells:
            if is_covered(c):
                continue
            traj = []
            cx, cy = c
            for _ in range(90):
                de2 = region_distance((cx, cy), outer, inners, keepout_clear)
                if de2 is None:
                    break
                h_hi = max_depth_at_point(a, de2, edge_clear, d_max)
                dcc = math.hypot(cx - c[0], cy - c[1])
                reach = depth_for_radius(a, dcc)
                if h_hi >= d_min and h_hi >= reach and de2 >= edge_clear:
                    traj.append((cx, cy, de2, h_hi, dcc, reach))
                if de2 >= a + edge_clear:
                    break
                e = 0.05
                dxp = region_distance((cx + e, cy), outer, inners, keepout_clear) or 0.0
                dxm = region_distance((cx - e, cy), outer, inners, keepout_clear) or 0.0
                dyp = region_distance((cx, cy + e), outer, inners, keepout_clear) or 0.0
                dym = region_distance((cx, cy - e), outer, inners, keepout_clear) or 0.0
                gx = dxp - dxm
                gy = dyp - dym
                gl = math.hypot(gx, gy)
                if gl < 1e-9:
                    break
                cx += gx / gl * e
                cy += gy / gl * e
            if not traj:
                continue
            cx, cy, de2, h_hi, dcc, reach = max(traj, key=lambda t: t[3])
            if any(math.hypot(cx - s["x"], cy - s["y"]) < b_spacing for s in samples):
                continue
            lo = max(d_min, reach)
            hi = h_hi
            if hi < lo:
                continue
            h = rng.uniform(lo, hi)
            if crater_radius(a, h) < dcc - 1e-9:
                continue
            add_sample(cx, cy, h, de2)
            added += 1
        if added == 0:
            break

    densify_count = len(samples) - poisson_count - fill_count

    # corner nestle (relaxed depth+spacing, stays INSIDE the face)
    if params.get("corner_fill"):
        c_dmin = params["corner_depth_min"]
        for (V, b, turn) in detect_convex_corners(outer, params["corner_angle_thresh"]):
            cnt = 0
            s = 0.0
            while s <= 2.2 * a and cnt < params["corner_max_per"]:
                probe = (V[0] + b[0] * s, V[1] + b[1] * s)
                if region_distance(probe, outer, inners, keepout_clear) is None or is_covered(probe):
                    s += 0.3
                    continue
                traj = []
                cx, cy = probe
                for _ in range(90):
                    de2 = region_distance((cx, cy), outer, inners, keepout_clear)
                    if de2 is None:
                        break
                    h_hi = max_depth_at_point(a, de2, edge_clear, d_max)
                    dcc = math.hypot(cx - probe[0], cy - probe[1])
                    reach = depth_for_radius(a, dcc)
                    if h_hi >= c_dmin and h_hi >= reach and de2 >= edge_clear:
                        traj.append((cx, cy, de2, h_hi, dcc, reach))
                    if de2 >= a + edge_clear:
                        break
                    e = 0.05
                    dxp = region_distance((cx + e, cy), outer, inners, keepout_clear) or 0.0
                    dxm = region_distance((cx - e, cy), outer, inners, keepout_clear) or 0.0
                    dyp = region_distance((cx, cy + e), outer, inners, keepout_clear) or 0.0
                    dym = region_distance((cx, cy - e), outer, inners, keepout_clear) or 0.0
                    gx = dxp - dxm
                    gy = dyp - dym
                    gl = math.hypot(gx, gy)
                    if gl < 1e-9:
                        break
                    cx += gx / gl * e
                    cy += gy / gl * e
                if not traj:
                    s += 0.3
                    continue
                cx, cy, de2, h_hi, dcc, reach = min(traj, key=lambda t: t[4])
                lo = max(c_dmin, reach)
                hi = h_hi
                if hi < lo:
                    s += 0.3
                    continue
                h = rng.uniform(lo, hi)
                if crater_radius(a, h) < dcc - 1e-9:
                    s += 0.3
                    continue
                add_sample(cx, cy, h, de2)
                cnt += 1
                s += 1.2 * crater_radius(a, h)

    corner_count = len(samples) - poisson_count - fill_count - densify_count

    stats = {
        "count": len(samples), "poisson_count": poisson_count, "fill_count": fill_count,
        "densify_count": densify_count, "corner_count": corner_count,
        "depth_min": min(s["depth"] for s in samples), "depth_max": max(s["depth"] for s in samples),
        "radius_min": min(s["radius"] for s in samples), "radius_max": max(s["radius"] for s in samples),
        "min_land": min(s["d_edge"] - s["radius"] for s in samples),
    }
    return samples, stats


def _tessellate_loop(loop, plane, tol_mm=0.02):
    origin = plane.origin
    u_dir = plane.uDirection
    v_dir = plane.vDirection
    pts = []
    for coedge in loop.coEdges:
        ev = coedge.edge.evaluator
        ok, p0, p1 = ev.getParameterExtents()
        if not ok:
            continue
        ok, strokes = ev.getStrokes(p0, p1, tol_mm * MM_TO_CM)
        if not ok:
            continue
        strokes = list(strokes)
        if coedge.isOpposedToEdge:
            strokes.reverse()
        for sp in strokes:
            dx = sp.x - origin.x
            dy = sp.y - origin.y
            dz = sp.z - origin.z
            u = (dx * u_dir.x + dy * u_dir.y + dz * u_dir.z) / MM_TO_CM
            v = (dx * v_dir.x + dy * v_dir.y + dz * v_dir.z) / MM_TO_CM
            pts.append((u, v))
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    if len(out) > 1 and math.hypot(out[-1][0] - out[0][0], out[-1][1] - out[0][1]) < 1e-6:
        out.pop()
    return out


def _outward_normal(face, plane):
    normal = plane.normal.copy()
    normal.normalize()
    ev = face.evaluator
    ok_p, param = ev.getParameterAtPoint(face.pointOnFace)
    ok_n, fnorm = ev.getNormalAtParameter(param)
    if ok_n and (fnorm.x * normal.x + fnorm.y * normal.y + fnorm.z * normal.z) < 0:
        normal.scaleBy(-1.0)
    return normal


# =====================================================================================
# Command dialog + execution
# =====================================================================================

def _params_from_inputs(inputs):
    """Read the dialog into a params dict (millimetres / degrees / ints)."""
    def L(cid):                                   # length input: internal cm -> mm
        return inputs.itemById(cid).value * 10.0
    bd = L('ball_diameter')
    return {
        'ball_radius': bd / 2.0,
        'depth_min': L('depth_min'),
        'depth_max': L('depth_max'),
        'min_spacing': L('min_spacing'),
        'edge_clearance': L('edge_clearance'),
        'keepout_clearance': L('keepout_clearance'),
        'border_spacing': L('border_spacing'),
        'corner_depth_min': L('corner_depth_min'),
        'fill_raster': L('fill_raster'),
        'border_raster': L('border_raster'),
        'fill_spacing_frac': inputs.itemById('fill_spacing_frac').value,
        'corner_angle_thresh': math.degrees(inputs.itemById('corner_angle_thresh').value),
        'corner_fill': inputs.itemById('corner_fill').value,
        'corner_max_per': inputs.itemById('corner_max_per').value,
        'fill_passes': inputs.itemById('fill_passes').value,
        'border_passes': inputs.itemById('border_passes').value,
        'poisson_k': inputs.itemById('poisson_k').value,
        'seed': inputs.itemById('seed').value,
        'drill_points': inputs.itemById('drill_points').value,
    }


def _run_dimples(face, params):
    design = adsk.fusion.Design.cast(app.activeProduct)
    root = design.rootComponent
    tl = design.timeline

    # --- history guardrail: snapshot, force append-only ---
    pre_tokens = []
    for i in range(tl.count):
        try:
            pre_tokens.append(tl.item(i).entity.entityToken)
        except Exception:
            pre_tokens.append(None)
    pre_count = tl.count
    try:
        tl.moveToEnd()
    except Exception:
        pass

    created = []
    try:
        plane = adsk.core.Plane.cast(face.geometry)
        outer, inners = None, []
        for loop in face.loops:
            poly = _tessellate_loop(loop, plane)
            if len(poly) < 3:
                continue
            if loop.isOuter:
                outer = poly
            else:
                inners.append(poly)
        if outer is None:
            raise RuntimeError("Could not tessellate the face's outer loop.")

        dimples, stats = generate_dimples(outer, inners, params)

        a = params["ball_radius"]
        normal = _outward_normal(face, plane)
        origin = plane.origin
        u_dir, v_dir = plane.uDirection, plane.vDirection

        tbm = adsk.fusion.TemporaryBRepManager.get()
        spheres = []
        for d in dimples:
            off = sphere_centre_offset(a, d["depth"])
            cx = origin.x + (d["x"] * u_dir.x + d["y"] * v_dir.x + off * normal.x) * MM_TO_CM
            cy = origin.y + (d["x"] * u_dir.y + d["y"] * v_dir.y + off * normal.y) * MM_TO_CM
            cz = origin.z + (d["x"] * u_dir.z + d["y"] * v_dir.z + off * normal.z) * MM_TO_CM
            spheres.append(tbm.createSphere(adsk.core.Point3D.create(cx, cy, cz), a * MM_TO_CM))

        target = face.body
        target_name = target.name
        base = root.features.baseFeatures.add()
        created.append(base)
        base.startEdit()
        tool_names = []
        for i, s in enumerate(spheres):
            b = root.bRepBodies.add(s, base)
            b.name = "dimple_tool_%d" % i
            tool_names.append(b.name)
        base.finishEdit()

        tool_bodies = adsk.core.ObjectCollection.create()
        for nm in tool_names:
            bb = root.bRepBodies.itemByName(nm)
            if bb:
                tool_bodies.add(bb)
        tgt = root.bRepBodies.itemByName(target_name)
        if tgt:
            target = tgt
        if tool_bodies.count != len(spheres):
            raise RuntimeError("Tool body references lost after finishEdit: %d of %d."
                               % (tool_bodies.count, len(spheres)))

        comb_in = root.features.combineFeatures.createInput(target, tool_bodies)
        comb_in.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
        comb_in.isKeepToolBodies = False
        created.append(root.features.combineFeatures.add(comb_in))

        # --- optional CAM drill points at each dimple's bottom-centre (cap vertex) ---
        # One sketch point per dimple at depth h below the surface along the inward normal.
        # A ball-mill Drilling op that plunges the tool tip to each point reproduces the
        # dimple exactly (the ball then sits where the cut sphere was), and per-point Z gives
        # each dimple its own depth in a single operation.
        n_drill = 0
        if params.get('drill_points'):
            sk = root.sketches.add(root.xYConstructionPlane)
            sk.name = 'Dimple drill points'
            sk.isComputeDeferred = True
            spts = sk.sketchPoints
            for d in dimples:
                sx = origin.x + (d['x'] * u_dir.x + d['y'] * v_dir.x) * MM_TO_CM
                sy = origin.y + (d['x'] * u_dir.y + d['y'] * v_dir.y) * MM_TO_CM
                sz = origin.z + (d['x'] * u_dir.z + d['y'] * v_dir.z) * MM_TO_CM
                hh = d['depth'] * MM_TO_CM
                spts.add(adsk.core.Point3D.create(sx - normal.x * hh,
                                                  sy - normal.y * hh,
                                                  sz - normal.z * hh))
            sk.isComputeDeferred = False
            created.append(sk)
            n_drill = len(dimples)
        stats['drill_points'] = n_drill

        ok_guard = tl.count >= pre_count
        if ok_guard:
            for i, tok in enumerate(pre_tokens):
                if tok is None:
                    continue
                try:
                    cur = tl.item(i).entity.entityToken
                except Exception:
                    cur = None
                if cur != tok:
                    ok_guard = False
                    break
        if not ok_guard:
            raise RuntimeError("History guardrail tripped: an existing timeline entry changed.")

        return stats
    except:
        for feat in reversed(created):
            try:
                feat.deleteMe()
            except Exception:
                pass
        raise


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            eventArgs = adsk.core.CommandEventArgs.cast(args)
            inputs = eventArgs.command.commandInputs
            sel = inputs.itemById('face')
            if sel.selectionCount != 1:
                ui.messageBox('Select exactly one planar face.')
                return
            face = adsk.fusion.BRepFace.cast(sel.selection(0).entity)
            if not face or not adsk.core.Plane.cast(face.geometry):
                ui.messageBox('Selection must be a planar face.')
                return
            params = _params_from_inputs(inputs)
            stats = _run_dimples(face, params)
            ui.messageBox(
                "Dimples placed: {c} (poisson {p} + fill {f} + border {b} + corner {k})\n"
                "Depth: {dl:.3f} - {dh:.3f} mm   Surface dia: {rl:.2f} - {rh:.2f} mm\n"
                "Min land (rim to boundary): {ml:.3f} mm\n"
                "CAM drill points: {dp} (sketch 'Dimple drill points')".format(
                    c=stats['count'], p=stats['poisson_count'], f=stats['fill_count'],
                    b=stats['densify_count'], k=stats['corner_count'],
                    dl=stats['depth_min'], dh=stats['depth_max'],
                    rl=2 * stats['radius_min'], rh=2 * stats['radius_max'], ml=stats['min_land'],
                    dp=stats.get('drill_points', 0)))
        except:
            ui.messageBox('Dimple Pattern failed:\n{}'.format(traceback.format_exc()))


class _ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            eventArgs = adsk.core.ValidateInputsEventArgs.cast(args)
            inputs = eventArgs.inputs
            a = inputs.itemById('ball_diameter').value * 10.0 / 2.0
            dmin = inputs.itemById('depth_min').value * 10.0
            dmax = inputs.itemById('depth_max').value * 10.0
            ec = inputs.itemById('edge_clearance').value * 10.0
            ms = inputs.itemById('min_spacing').value * 10.0
            face_ok = inputs.itemById('face').selectionCount == 1
            ok = (face_ok and 0.0 < dmin <= dmax < a and ec >= 0.0 and ms > 0.0)
            eventArgs.areInputsValid = ok
        except:
            args.areInputsValid = False


class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = adsk.core.Command.cast(args.command)
            cmd.isExecutedWhenPreEmpted = False
            inputs = cmd.commandInputs

            def val(v_mm):
                return adsk.core.ValueInput.createByReal(v_mm * MM_TO_CM)

            sel = inputs.addSelectionInput('face', 'Target face', 'Select one planar face')
            sel.addSelectionFilter('PlanarFaces')
            sel.setSelectionLimits(1, 1)

            inputs.addBoolValueInput('drill_points', 'Add CAM drill points (dimple bottoms)',
                                     True, '', DEFAULTS['drill_points'])

            g1 = inputs.addGroupCommandInput('g_cutter', 'Cutter & depth')
            c1 = g1.children
            c1.addValueInput('ball_diameter', 'Ball / cutter diameter', 'mm', val(DEFAULTS['ball_diameter']))
            c1.addValueInput('depth_min', 'Min depth', 'mm', val(DEFAULTS['depth_min']))
            c1.addValueInput('depth_max', 'Max depth', 'mm', val(DEFAULTS['depth_max']))

            g2 = inputs.addGroupCommandInput('g_pack', 'Spacing & clearance')
            c2 = g2.children
            c2.addValueInput('min_spacing', 'Min spacing (interior)', 'mm', val(DEFAULTS['min_spacing']))
            c2.addValueInput('border_spacing', 'Border spacing (min)', 'mm', val(DEFAULTS['border_spacing']))
            c2.addValueInput('edge_clearance', 'Edge clearance', 'mm', val(DEFAULTS['edge_clearance']))
            c2.addValueInput('keepout_clearance', 'Keep-out clearance', 'mm', val(DEFAULTS['keepout_clearance']))

            g3 = inputs.addGroupCommandInput('g_corner', 'Corners')
            c3 = g3.children
            c3.addBoolValueInput('corner_fill', 'Fill sharp corners', True, '', DEFAULTS['corner_fill'])
            c3.addValueInput('corner_angle_thresh', 'Corner angle threshold', 'deg',
                             adsk.core.ValueInput.createByReal(math.radians(DEFAULTS['corner_angle_thresh'])))
            c3.addValueInput('corner_depth_min', 'Corner min depth', 'mm', val(DEFAULTS['corner_depth_min']))
            c3.addIntegerSpinnerCommandInput('corner_max_per', 'Max dimples per corner', 0, 6, 1, DEFAULTS['corner_max_per'])

            g4 = inputs.addGroupCommandInput('g_adv', 'Coverage (advanced)')
            g4.isExpanded = False
            c4 = g4.children
            c4.addIntegerSpinnerCommandInput('fill_passes', 'Interior fill passes', 0, 20, 1, DEFAULTS['fill_passes'])
            c4.addIntegerSpinnerCommandInput('border_passes', 'Border passes', 0, 40, 1, DEFAULTS['border_passes'])
            c4.addIntegerSpinnerCommandInput('poisson_k', 'Poisson attempts (k)', 4, 100, 1, DEFAULTS['poisson_k'])
            c4.addValueInput('fill_raster', 'Interior raster step', 'mm', val(DEFAULTS['fill_raster']))
            c4.addValueInput('border_raster', 'Border raster step', 'mm', val(DEFAULTS['border_raster']))
            c4.addValueInput('fill_spacing_frac', 'Fill spacing fraction', '',
                             adsk.core.ValueInput.createByReal(DEFAULTS['fill_spacing_frac']))
            c4.addIntegerSpinnerCommandInput('seed', 'Random seed', 0, 2147483647, 1, DEFAULTS['seed'])

            onExec = _ExecuteHandler()
            cmd.execute.add(onExec)
            _handlers.append(onExec)
            onValidate = _ValidateHandler()
            cmd.validateInputs.add(onValidate)
            _handlers.append(onValidate)
        except:
            ui.messageBox('Dimple Pattern (dialog) failed:\n{}'.format(traceback.format_exc()))


def run(context):
    try:
        cmdDef = ui.commandDefinitions.itemById(CMD_ID)
        if not cmdDef:
            cmdDef = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TOOLTIP)
        onCreated = _CreatedHandler()
        cmdDef.commandCreated.add(onCreated)
        _handlers.append(onCreated)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmdDef)
    except:
        if ui:
            ui.messageBox('Dimple Pattern add-in failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        cmdDef = ui.commandDefinitions.itemById(CMD_ID)
        if cmdDef:
            cmdDef.deleteMe()
        _handlers.clear()
    except:
        if ui:
            ui.messageBox('Dimple Pattern add-in failed to stop:\n{}'.format(traceback.format_exc()))
