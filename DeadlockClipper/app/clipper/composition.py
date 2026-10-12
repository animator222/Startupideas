"""What a finished clip looks like: a canvas (1080x1920 or 1920x1080) with a blurred background
and layers on top, bottom to top.

  video layer: a part of the source video ("src", fractions x, y, w, h of the source frame)
               placed on the canvas ("dst": centre x, centre y and width, as fractions of the
               canvas; the height follows from the source part's shape). A layer may be bigger
               than the canvas - that is how the gameplay gets zoomed.
  text layer:  text with font, size, colour, shadow and outline, centred at (cx, cy).

The same description drives the live preview in the editor and the ffmpeg export.
"""
import copy

CANVAS = {"vertical": (1080, 1920), "horizontal": (1920, 1080)}

TEXT_DEFAULTS = {
    "type": "text", "name": "Text", "text": "YOUR TEXT", "font": "Anton", "size": 0.06, "bold": False,
    "color": "#ffffff", "shadow": True, "shadow_color": "#000000", "shadow_strength": 0.6,
    "outline": True, "outline_color": "#000000", "outline_width": 0.10,
    "cx": 0.5, "cy": 0.15, "hidden": False,
}


def canvas_size(comp):
    return CANVAS.get(comp.get("canvas"), CANVAS["vertical"])


def _fit_width(info, src, canvas, zoom=1.0):
    """Width (fraction of the canvas) at which the source part just fits the canvas, times zoom."""
    cw, ch = CANVAS[canvas]
    sw, sh = src[2] * info.width, src[3] * info.height
    return min(cw / sw, ch / sh) * zoom * sw / cw


def simple_comp(info, orientation, zoom=1.0):
    """Full frame (zoom 1) or zoomed gameplay, centred, on the blurred background."""
    src = [0.0, 0.0, 1.0, 1.0]
    return {"canvas": orientation, "layers": [
        {"type": "video", "name": "Gameplay", "src": src,
         "dst": [0.5, 0.5, _fit_width(info, src, orientation, max(1.0, zoom))], "hidden": False}]}


def new_video_layer(info, comp, name="Webcam"):
    """A new layer showing a corner of the source, placed near the top of the canvas."""
    src = [0.02, 0.70, 0.22, 0.27] if info.width >= info.height else [0.05, 0.05, 0.40, 0.15]
    return {"type": "video", "name": name, "src": src, "dst": [0.5, 0.15, 0.45], "hidden": False}


def new_text_layer():
    return dict(TEXT_DEFAULTS)


def _box(v):
    return isinstance(v, (list, tuple)) and len(v) == 4 and all(isinstance(x, (int, float)) for x in v) \
        and v[2] > 0.01 and v[3] > 0.01


def clean_comp(comp):
    """A valid composition or None (used for saved layouts and per-clip edits)."""
    if not isinstance(comp, dict) or comp.get("canvas") not in CANVAS:
        return None
    layers = []
    for lay in comp.get("layers") or []:
        if not isinstance(lay, dict):
            continue
        if lay.get("type") == "video" and _box(lay.get("src")) and isinstance(lay.get("dst"), (list, tuple)) \
                and len(lay["dst"]) == 3 and lay["dst"][2] > 0.01:
            layers.append({"type": "video", "name": str(lay.get("name") or "Video")[:30],
                           "src": [min(1.0, max(0.0, float(v))) for v in lay["src"]],
                           "dst": [float(v) for v in lay["dst"]], "hidden": bool(lay.get("hidden"))})
        elif lay.get("type") == "text":
            t = dict(TEXT_DEFAULTS)
            t.update({k: lay[k] for k in TEXT_DEFAULTS if k in lay})
            t["text"] = str(t["text"])[:300]
            layers.append(t)
    if not any(lay["type"] == "video" for lay in layers):
        return None
    return {"canvas": comp["canvas"], "layers": layers}


def template_key(info, orientation):
    """Saved layouts depend on the source shape and the output shape."""
    return ("portrait" if info.height > info.width else "landscape") + ">" + orientation


def comp_for_clip(clip, settings, info):
    """The clip's own edited layout, or the default from the settings."""
    if clip is not None and getattr(clip, "comp", None):
        comp = clean_comp(clip.comp)
        if comp:
            return comp
    orientation = settings.output_orientation
    if settings.vertical_format == "custom":
        saved = clean_comp((settings.my_layouts or {}).get(template_key(info, orientation)))
        if saved and saved["canvas"] == orientation:
            return copy.deepcopy(saved)
    zoom = settings.zoom_level if settings.vertical_format in ("zoom", "custom") else 1.0
    return simple_comp(info, orientation, zoom)


def video_rect(layer, info, comp):
    """(x, y, w, h) of a video layer on the canvas, in canvas pixels (may go past the edges)."""
    cw, ch = canvas_size(comp)
    sx, sy, sw, sh = layer["src"]
    cx, cy, wf = layer["dst"]
    w = wf * cw
    h = w * (sh * info.height) / max(1e-6, sw * info.width)
    return cx * cw - w / 2, cy * ch - h / 2, w, h


def _even(v):
    return max(2, int(round(v / 2)) * 2)


def build_filter(info, comp, text_inputs, fps_str=None):
    """ffmpeg filter graph from input 0 (the video) to [v].
    text_inputs maps a text layer's index to the ffmpeg input number of its rendered PNG."""
    cw, ch = canvas_size(comp)
    layers = [(i, lay) for i, lay in enumerate(comp["layers"]) if not lay.get("hidden")]
    videos = [(i, lay) for i, lay in layers if lay["type"] == "video"]
    bw, bh = cw // 4, ch // 4
    graph = (f"[0:v:0]split={len(videos) + 1}[bgsrc]" + "".join(f"[s{i}]" for i, _ in videos) + ";"
             f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
             f"boxblur=10:2,eq=brightness=-0.06,scale={cw}:{ch},setsar=1[c0];")
    current, k = "c0", 0
    for i, lay in layers:
        k += 1
        if lay["type"] == "video":
            sx, sy, sw, sh = lay["src"]
            pw, ph = _even(sw * info.width), _even(sh * info.height)
            px = max(0, min(_even(sx * info.width), info.width - pw))
            py = max(0, min(_even(sy * info.height), info.height - ph))
            x, y, w, h = video_rect(lay, info, comp)
            graph += (f"[s{i}]crop={pw}:{ph}:{px}:{py},scale={_even(w)}:{_even(h)}:flags=lanczos,setsar=1[l{i}];"
                      f"[{current}][l{i}]overlay={int(round(x))}:{int(round(y))}[c{k}];")
        elif i in text_inputs:
            graph += f"[{current}][{text_inputs[i]}:v]overlay=0:0:eof_action=repeat[c{k}];"
        else:
            continue
        current = f"c{k}"
    return graph + f"[{current}]setsar=1" + (f",fps={fps_str}" if fps_str else "") + ",format=yuv420p[v]"
