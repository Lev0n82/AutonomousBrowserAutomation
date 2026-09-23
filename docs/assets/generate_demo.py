from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 1200, 675
ASSET_DIR = Path(__file__).resolve().parent
OUTPUT = ASSET_DIR / "browser-automation-demo.gif"
FONT_ROOT = Path(r"C:\Windows\Fonts")


def font(name, size):
    path = FONT_ROOT / name
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


REGULAR = font("segoeui.ttf", 22)
SMALL = font("segoeui.ttf", 17)
TINY = font("segoeui.ttf", 14)
SEMIBOLD = font("seguisb.ttf", 22)
TITLE = font("seguisb.ttf", 30)

COLORS = {
    "background": "#dff1ee",
    "chrome": "#123438",
    "chrome_2": "#1d4c50",
    "page": "#f8fcfb",
    "panel": "#d2ece8",
    "border": "#87bab4",
    "text": "#30383a",
    "muted": "#687577",
    "teal": "#287d77",
    "teal_2": "#74c3bb",
    "accent": "#a8e5dc",
    "white": "#ffffff",
    "success": "#2f8b67",
    "warning": "#d29a3a",
}


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text(draw, xy, value, selected_font=REGULAR, fill=None, anchor=None):
    draw.text(xy, value, font=selected_font, fill=fill or COLORS["text"], anchor=anchor)


def base_frame():
    image = Image.new("RGB", (WIDTH, HEIGHT), COLORS["background"])
    draw = ImageDraw.Draw(image)
    rounded(draw, (30, 24, 1170, 646), 26, COLORS["white"], "#a3cbc6", 2)
    rounded(draw, (30, 24, 1170, 88), 26, COLORS["chrome"])
    draw.rectangle((30, 62, 1170, 88), fill=COLORS["chrome"])
    for x, color in [(58, "#ff8d86"), (82, "#ffd276"), (106, "#7bd6a5")]:
        draw.ellipse((x - 7, 49 - 7, x + 7, 49 + 7), fill=color)
    rounded(draw, (150, 40, 760, 70), 15, COLORS["chrome_2"])
    text(draw, (174, 55), "https://quality.example/dashboard", SMALL, "#c9e8e4", "lm")
    rounded(draw, (786, 37, 1138, 73), 18, "#245b5f")
    text(draw, (962, 55), "Autonomous Browser Assistant", SMALL, "#e7fffb", "mm")
    draw.rectangle((820, 88, 1170, 646), fill=COLORS["panel"])
    draw.line((820, 88, 820, 646), fill=COLORS["border"], width=2)
    text(draw, (55, 116), "Quality Engineering Overview", TITLE, COLORS["text"])
    text(draw, (55, 154), "Release health and verification trends", SMALL, COLORS["muted"])
    rounded(draw, (55, 190, 282, 310), 16, "#e5f6f3", "#a7d4ce")
    rounded(draw, (300, 190, 527, 310), 16, "#e5f6f3", "#a7d4ce")
    rounded(draw, (545, 190, 790, 310), 16, "#e5f6f3", "#a7d4ce")
    metrics = [("Pass rate", "96.8%"), ("Open defects", "18"), ("Automation", "82%")]
    for left, (label, value) in zip((55, 300, 545), metrics):
        text(draw, (left + 20, 218), label, SMALL, COLORS["muted"])
        text(draw, (left + 20, 260), value, TITLE, COLORS["teal"])
    rounded(draw, (55, 335, 790, 610), 16, "#f2faf8", "#a7d4ce")
    text(draw, (78, 365), "Release trend", SEMIBOLD)
    for index in range(5):
        y = 408 + index * 36
        draw.line((78, y, 762, y), fill="#d2e7e3", width=1)
    points = [(95, 544), (220, 500), (345, 515), (470, 438), (595, 454), (735, 389)]
    draw.line(points, fill=COLORS["teal"], width=5, joint="curve")
    for x, y in points:
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=COLORS["white"], outline=COLORS["teal"], width=4)
    text(draw, (842, 122), "OLLAMA ASSISTANT", TINY, COLORS["teal"])
    draw.line((842, 150, 1145, 150), fill="#a9d1cc", width=1)
    return image, draw


def cursor(draw, x, y):
    draw.polygon([(x, y), (x + 4, y + 28), (x + 12, y + 20), (x + 21, y + 35), (x + 27, y + 31),
                  (x + 18, y + 17), (x + 30, y + 14)], fill="#173d40", outline="#ffffff")


def action_chip(draw, label, color=None):
    color = color or COLORS["teal"]
    rounded(draw, (842, 555, 1144, 598), 14, color)
    text(draw, (993, 576), label, SMALL, COLORS["white"], "mm")


def frame(stage, progress):
    image, draw = base_frame()
    if stage == 0:
        rounded(draw, (842, 178, 1145, 270), 16, "#eff9f7", "#a9d1cc")
        text(draw, (860, 198), "You", TINY, COLORS["muted"])
        prompt = "Open the quality dashboard,\nfilter the current release,\nand summarize the risks."
        text(draw, (860, 226), prompt, SMALL)
        action_chip(draw, "Planning autonomous task")
        cursor(draw, 1085, 520)
    elif stage == 1:
        rounded(draw, (842, 178, 1145, 242), 16, "#eff9f7", "#a9d1cc")
        text(draw, (860, 198), "Navigating to dashboard...", SMALL)
        rounded(draw, (842, 262, 1145, 316), 14, "#bfe4df")
        text(draw, (860, 289), "Navigate  ·  tab 24", SMALL, COLORS["teal"])
        action_chip(draw, "Navigate → quality.example")
        x = int(165 + progress * 500)
        rounded(draw, (150, 40, 760, 70), 15, COLORS["chrome_2"])
        draw.rectangle((150, 66, x, 70), fill=COLORS["teal_2"])
        cursor(draw, 680, 100)
    elif stage == 2:
        rounded(draw, (842, 178, 1145, 242), 16, "#eff9f7", "#a9d1cc")
        text(draw, (860, 198), "Reading visible metrics...", SMALL)
        rounded(draw, (842, 262, 1145, 316), 14, "#bfe4df")
        text(draw, (860, 289), "ReadPage  ·  14 references", SMALL, COLORS["teal"])
        action_chip(draw, "ReadPage → interactive")
        pulse = int(4 + progress * 6)
        draw.rounded_rectangle((50 - pulse, 185 - pulse, 795 + pulse, 315 + pulse),
                               radius=18, outline="#40a49b", width=4)
        cursor(draw, 267, 224)
    elif stage == 3:
        rounded(draw, (842, 178, 1145, 242), 16, "#eff9f7", "#a9d1cc")
        text(draw, (860, 198), "Applying release filter...", SMALL)
        rounded(draw, (842, 262, 1145, 316), 14, "#bfe4df")
        text(draw, (860, 289), "FormInput  ·  Release 26.3", SMALL, COLORS["teal"])
        action_chip(draw, "FormInput → release")
        rounded(draw, (610, 112, 790, 158), 12, "#d8efeb", "#4c9b94", 2)
        text(draw, (632, 135), "Release 26.3", SMALL, COLORS["teal"], "lm")
        cursor(draw, 745, 122 + int(progress * 8))
    elif stage == 4:
        rounded(draw, (842, 178, 1145, 242), 16, "#eff9f7", "#a9d1cc")
        text(draw, (860, 198), "Capturing filtered state...", SMALL)
        rounded(draw, (842, 262, 1145, 316), 14, "#bfe4df")
        text(draw, (860, 289), "ComputerBatch  ·  screenshot", SMALL, COLORS["teal"])
        action_chip(draw, "SCREENSHOT → visible tab")
        alpha = int(80 + progress * 100)
        overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle((30, 88, 820, 646), fill=(255, 255, 255, alpha))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image)
        cursor(draw, 788, 615)
    else:
        rounded(draw, (842, 178, 1145, 462), 16, "#eff9f7", "#a9d1cc")
        text(draw, (860, 198), "Release 26.3 summary", SEMIBOLD, COLORS["teal"])
        rows = [
            ("Signal", "Finding"),
            ("Pass rate", "96.8% — healthy"),
            ("Open defects", "18 — review 3 high"),
            ("Automation", "82% — trending up"),
        ]
        top = 238
        for row_index, (left, right) in enumerate(rows):
            fill = "#d9efeb" if row_index == 0 else "#ffffff"
            draw.rectangle((858, top, 1129, top + 42), fill=fill, outline="#b4d7d2")
            text(draw, (868, top + 21), left, TINY if row_index else SMALL, COLORS["text"], "lm")
            text(draw, (985, top + 21), right, TINY if row_index else SMALL, COLORS["text"], "lm")
            top += 42
        rounded(draw, (842, 487, 1145, 532), 14, "#c2e9df")
        text(draw, (993, 509), "Task complete  ✓", SMALL, COLORS["success"], "mm")
        action_chip(draw, "Structured result returned", COLORS["success"])
        cursor(draw, 1114, 508)
    return image


def build():
    frames = []
    durations = []
    for stage in range(6):
        frame_count = 6 if stage not in {0, 5} else 9
        for index in range(frame_count):
            frames.append(frame(stage, index / max(frame_count - 1, 1)))
            durations.append(130 if stage not in {0, 5} else 180)
        durations[-1] += 650
    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"Created {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
