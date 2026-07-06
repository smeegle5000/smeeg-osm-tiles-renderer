import mapnik, math, os, re, subprocess, time
import pygame
from multiprocessing import Pool, cpu_count

STYLE = "mapnik.xml"
OUT_DIR = "tiles"
TILE_PX = 256
BASE_MAX_ZOOM = 5
POLL_SECONDS = 10
PBF_FILE = None

# window size - edit here
WIN_W = 1920
WIN_H = 1080

# ---------- projection helpers ----------

def deg2num(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def num2deg(xtile, ytile, zoom):
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    return math.degrees(lat_rad), lon_deg

def latlon_to_3857(lat, lon):
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return x, y * 20037508.34 / 180.0

# ---------- pbf bbox ----------

def get_pbf_bbox(pbf_path):
    result = subprocess.run(["ogrinfo", "-al", "-so", pbf_path, "lines"],
                             capture_output=True, text=True)
    m = re.search(r"Extent:\s*\(([-\d.]+),\s*([-\d.]+)\)\s*-\s*\(([-\d.]+),\s*([-\d.]+)\)", result.stdout)
    if not m:
        raise RuntimeError("could not parse bbox, check ogrinfo output")
    lon_min, lat_min, lon_max, lat_max = map(float, m.groups())
    return lat_min, lat_max, lon_min, lon_max

def tile_range_for_bbox(lat_min, lat_max, lon_min, lon_max, z):
    x0, y0 = deg2num(lat_max, lon_min, z)
    x1, y1 = deg2num(lat_min, lon_max, z)
    return range(min(x0, x1), max(x0, x1) + 1), range(min(y0, y1), max(y0, y1) + 1)

# ---------- render worker ----------

_map = None

def init_worker():
    global _map
    _map = mapnik.Map(TILE_PX, TILE_PX)
    mapnik.load_map(_map, STYLE)
    _map.buffer_size = 128

def render_tile(args):
    z, x, y = args
    tile_dir = os.path.join(OUT_DIR, str(z), str(x))
    tile_path = os.path.join(tile_dir, f"{y}.png")
    if os.path.exists(tile_path):
        return
    lat0, lon0 = num2deg(x, y, z)
    lat1, lon1 = num2deg(x + 1, y + 1, z)
    X0, Y0 = latlon_to_3857(lat0, lon0)
    X1, Y1 = latlon_to_3857(lat1, lon1)
    _map.zoom_to_box(mapnik.Box2d(min(X0, X1), min(Y0, Y1), max(X0, X1), max(Y0, Y1)))
    os.makedirs(tile_dir, exist_ok=True)
    mapnik.render_to_file(_map, tile_path)

class RenderManager:
    def __init__(self):
        self.pool = Pool(processes=cpu_count(), initializer=init_worker)

    def submit_tiles(self, jobs):
        self.pool.map_async(render_tile, jobs)
        return len(jobs)

    def shutdown(self):
        self.pool.close()

def render_base_layer(lat_min, lat_max, lon_min, lon_max):
    print(f"rendering base layer z0-{BASE_MAX_ZOOM} within data bbox")
    with Pool(processes=cpu_count(), initializer=init_worker) as pool:
        for z in range(0, BASE_MAX_ZOOM + 1):
            xr, yr = tile_range_for_bbox(lat_min, lat_max, lon_min, lon_max, z)
            jobs = [(z, x, y) for x in xr for y in yr]
            print(f"  z{z}: {len(jobs)} tiles")
            pool.map(render_tile, jobs)
    print("base layer done")

# ---------- viewer ----------

class Viewer:
    def __init__(self, lat_min, lat_max, lon_min, lon_max):
        pygame.init()
        self.W, self.H = WIN_W, WIN_H
        self.screen = pygame.display.set_mode((self.W, self.H))
        pygame.display.set_caption("tile viewer")
        self.font = pygame.font.SysFont(None, 20)

        self.data_bbox = (lat_min, lat_max, lon_min, lon_max)
        self.zoom = 0
        self.view_scale = 1.0  # >1 = zoomed in visually, <1 = zoomed out, same data layer

        self.tile_cache = {}      # (z,x,y) -> Surface
        self.tile_mtime = {}      # (z,x,y) -> last loaded mtime
        self.selections = {}      # z -> list of (x0,y0,x1,y1) tile boxes
        self.show_grid = False
        self.show_hud = True

        self.render_mgr = RenderManager()

        x0, y0 = deg2num(lat_max, lon_min, self.zoom)
        self.offset_x, self.offset_y = float(x0), float(y0)

        self.dragging = False
        self.drag_start = None
        self.drag_cur = None
        self.panning = False
        self.pan_start_mouse = None
        self.pan_start_offset = None

        self.last_poll = 0
        self.last_resize_time = 0
        self.running = True

    def tile_px(self):
        return TILE_PX * self.view_scale

    def load_tile(self, z, x, y, force=False):
        key = (z, x, y)
        path = os.path.join(OUT_DIR, str(z), str(x), f"{y}.png")
        if not os.path.exists(path):
            if key in self.tile_cache:
                del self.tile_cache[key]
                del self.tile_mtime[key]
            return None
        mtime = os.path.getmtime(path)
        if key in self.tile_cache and self.tile_mtime.get(key) == mtime and not force:
            return self.tile_cache[key]
        try:
            surf = pygame.image.load(path).convert()
            self.tile_cache[key] = surf
            self.tile_mtime[key] = mtime
            return surf
        except Exception:
            return self.tile_cache.get(key)

    def draw(self):
        self.screen.fill((0, 0, 0))
        tpx = self.tile_px()
        tiles_x = int(self.W // tpx) + 2
        tiles_y = int(self.H // tpx) + 2
        start_tx = int(math.floor(self.offset_x))
        start_ty = int(math.floor(self.offset_y))

        for tx in range(start_tx, start_tx + tiles_x):
            for ty in range(start_ty, start_ty + tiles_y):
                screen_x = int((tx - self.offset_x) * tpx)
                screen_y = int((ty - self.offset_y) * tpx)
                surf = self.load_tile(self.zoom, tx, ty)
                if surf:
                    if tpx != TILE_PX:
                        surf = pygame.transform.scale(surf, (int(tpx), int(tpx)))
                    self.screen.blit(surf, (screen_x, screen_y))
                if self.show_grid:
                    rect = (screen_x, screen_y, int(tpx), int(tpx))
                    pygame.draw.rect(self.screen, (80, 80, 80), rect, 1)
                    label = self.font.render(f"{self.zoom}/{tx}/{ty}", True, (255, 0, 0))
                    self.screen.blit(label, (screen_x + 4, screen_y + 4))

        for (x0, y0, x1, y1) in self.selections.get(self.zoom, []):
            sx0 = int((min(x0, x1) - self.offset_x) * tpx)
            sy0 = int((min(y0, y1) - self.offset_y) * tpx)
            sx1 = int((max(x0, x1) + 1 - self.offset_x) * tpx)
            sy1 = int((max(y0, y1) + 1 - self.offset_y) * tpx)
            pygame.draw.rect(self.screen, (0, 255, 0), (sx0, sy0, sx1 - sx0, sy1 - sy0), 2)

        if self.dragging and self.drag_start and self.drag_cur:
            x0, y0 = self.drag_start
            x1, y1 = self.drag_cur
            rx0, ry0 = min(x0, x1), min(y0, y1)
            rx1, ry1 = max(x0, x1), max(y0, y1)
            pygame.draw.rect(self.screen, (255, 255, 0), (rx0, ry0, rx1 - rx0, ry1 - ry0), 2)

        if self.show_hud:
            lines = [
                f"zoom {self.zoom}  scale {self.view_scale:.2f}",
                f"selections: {len(self.selections.get(self.zoom, []))}",
                "",
                "R-drag: select",
                "L-drag: pan",
                "scroll: view scale",
                "up/down: zoom layer",
                "enter: render same zoom",
                "shift+enter: render zoom+1",
                "g: toggle grid",
                "c: clear selection",
                "h: toggle this panel",
                "q: quit",
            ]
            pad = 8
            line_h = 20
            w = 220
            h = pad * 2 + line_h * len(lines)
            panel = pygame.Surface((w, h))
            panel.set_alpha(180)
            panel.fill((20, 20, 20))
            self.screen.blit(panel, (10, 10))
            for i, line in enumerate(lines):
                text = self.font.render(line, True, (255, 255, 0))
                self.screen.blit(text, (10 + pad, 10 + pad + i * line_h))

        pygame.display.flip()

    def screen_to_tile(self, sx, sy):
        tpx = self.tile_px()
        return self.offset_x + sx / tpx, self.offset_y + sy / tpx

    def handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    self.running = False
                elif event.key == pygame.K_UP:
                    self.change_zoom(1)
                elif event.key == pygame.K_DOWN:
                    self.change_zoom(-1)
                elif event.key in (pygame.K_EQUALS, pygame.K_PLUS):
                    self.view_scale = min(4.0, self.view_scale * 1.25)
                elif event.key == pygame.K_MINUS:
                    self.view_scale = max(0.01, self.view_scale / 1.25)
                elif event.key == pygame.K_g:
                    self.show_grid = not self.show_grid
                elif event.key == pygame.K_h:
                    self.show_hud = not self.show_hud
                elif event.key == pygame.K_c:
                    self.selections[self.zoom] = []
                elif event.key == pygame.K_RETURN:
                    mods = pygame.key.get_mods()
                    if mods & pygame.KMOD_SHIFT:
                        self.trigger_render(next_zoom=True)
                    else:
                        self.trigger_render(next_zoom=False)
            elif event.type == pygame.MOUSEWHEEL:
                if event.y > 0:
                    self.view_scale = min(4.0, self.view_scale * 1.15)
                elif event.y < 0:
                    self.view_scale = max(0.01, self.view_scale / 1.15)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 3:
                    self.dragging = True
                    self.drag_start = event.pos
                    self.drag_cur = event.pos
                elif event.button == 1:
                    self.panning = True
                    self.pan_start_mouse = event.pos
                    self.pan_start_offset = (self.offset_x, self.offset_y)
            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 3 and self.dragging:
                    self.dragging = False
                    tx0, ty0 = self.screen_to_tile(*self.drag_start)
                    tx1, ty1 = self.screen_to_tile(*event.pos)
                    box = (int(math.floor(tx0)), int(math.floor(ty0)),
                           int(math.floor(tx1)), int(math.floor(ty1)))
                    self.selections.setdefault(self.zoom, []).append(box)
                elif event.button == 1:
                    self.panning = False
            elif event.type == pygame.MOUSEMOTION:
                if self.dragging:
                    self.drag_cur = event.pos
                if self.panning:
                    tpx = self.tile_px()
                    dx = event.pos[0] - self.pan_start_mouse[0]
                    dy = event.pos[1] - self.pan_start_mouse[1]
                    self.offset_x = self.pan_start_offset[0] - dx / tpx
                    self.offset_y = self.pan_start_offset[1] - dy / tpx

    def change_zoom(self, delta):
        new_zoom = self.zoom + delta
        if new_zoom < 0:
            return
        cx_screen, cy_screen = self.W / 2, self.H / 2
        lat, lon = num2deg(self.offset_x + cx_screen / self.tile_px(),
                            self.offset_y + cy_screen / self.tile_px(), self.zoom)
        self.zoom = new_zoom
        self.view_scale = 1.0
        cx, cy = deg2num(lat, lon, self.zoom)
        self.offset_x = cx - cx_screen / TILE_PX
        self.offset_y = cy - cy_screen / TILE_PX

    def trigger_render(self, next_zoom=False):
        boxes = self.selections.get(self.zoom, [])
        if not boxes:
            print("no selection at this zoom, drag one first")
            return
        z = self.zoom + 1 if next_zoom else self.zoom
        jobs = []
        for (x0, y0, x1, y1) in boxes:
            xlo, xhi = min(x0, x1), max(x0, x1)
            ylo, yhi = min(y0, y1), max(y0, y1)
            if next_zoom:
                xlo, xhi = xlo * 2, xhi * 2 + 1
                ylo, yhi = ylo * 2, yhi * 2 + 1
            for cx in range(xlo, xhi + 1):
                for cy in range(ylo, yhi + 1):
                    jobs.append((z, cx, cy))
        n = self.render_mgr.submit_tiles(jobs)
        print(f"submitted {n} tiles at zoom {z}")

    def poll_new_tiles(self):
        now = time.time()
        if now - self.last_poll > POLL_SECONDS:
            self.last_poll = now  # load_tile checks mtime per-call, no full clear needed

    def run(self):
        clock = pygame.time.Clock()
        while self.running:
            self.handle_events()
            self.poll_new_tiles()
            self.draw()
            clock.tick(30)
        self.render_mgr.shutdown()
        pygame.quit()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        PBF_FILE = sys.argv[1]
    if not PBF_FILE:
        print("usage: python3 tile_viewer.py /path/to/file.osm.pbf")
        sys.exit(1)

    lat_min, lat_max, lon_min, lon_max = get_pbf_bbox(PBF_FILE)
    print(f"pbf bbox: lat {lat_min}-{lat_max}, lon {lon_min}-{lon_max}")

    render_base_layer(lat_min, lat_max, lon_min, lon_max)

    viewer = Viewer(lat_min, lat_max, lon_min, lon_max)
    viewer.run()
