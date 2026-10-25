"""Generate labelled synthetic ticket fixtures without any recognition or network call.

These are isolated benchmark inputs, not evidence of real-ticket model accuracy.
The caller supplies a local font and owns the output directory.
"""
import hashlib
import json

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

TITLES = ["流浪地球", "星际穿越", "千与千寻", "哪吒之魔童降世", "寻梦环游记",
          "疯狂动物城", "长安三万里", "让子弹飞", "无间道", "大话西游",
          "Metropolis", "Nosferatu", "The General", "The Kid", "Sherlock Jr.",
          "City Lights", "Modern Times", "The Gold Rush", "Safety Last!", "The Phantom of the Opera"]
CONDITIONS = ("clean", "tilt", "low_contrast", "blur", "perspective", "missing_field")


def generate(destination, font_path):
    samples = []
    for index, title in enumerate(TITLES):
        english = index >= 10
        labels = ["Film", "Cinema", "Screen", "Seat", "Showtime"] if english else ["片名", "影院", "影厅", "座位", "放映时间"]
        fields = {"title": title, "cinema": "Aurora Cinema" if english else f"星光影城{index + 1}店",
                  "hall": str(index % 7 + 1) if english else f"{index % 7 + 1}号厅",
                  "seat": f"G{index + 1}" if english else f"{index % 9 + 1}排{index + 1}座",
                  "showtime": f"2026-11-{index + 1:02d}T{18 + index % 4:02d}:30"}
        for condition in CONDITIONS:
            expected = dict(fields)
            if condition == "missing_field":
                del expected[list(fields)[index % 5]]
            picture = Image.new("RGB", (1060, 700), "#f5f2e8")
            draw = ImageDraw.Draw(picture)
            font = ImageFont.truetype(str(font_path), 30 + index % 3 * 3)
            small = ImageFont.truetype(str(font_path), 22)
            draw.rectangle((18, 18, 1041, 681), outline="#555555", width=2)
            draw.text((55, 40), "MOVIE TICKET / 电影票 · 合成测试", font=small, fill="#555555")
            y = 112
            # Layout order varies, without changing the evidence required by the parser.
            order = list(fields)
            if index % 2:
                order[0], order[1] = order[1], order[0]
            for key in order:
                if key in expected:
                    value = expected[key].replace("T", " ") if key == "showtime" else expected[key]
                    draw.text((55 + index % 3 * 9, y), f"{labels[list(fields).index(key)]}: {value}", font=font, fill="#151515")
                y += 82
            draw.text((55, 590), "ORDER: TEST-0000   PRINT: 2026-10-22 09:00", font=small, fill="#555555")
            if condition == "tilt":
                picture = picture.rotate(4 if index % 2 else -4, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            elif condition == "low_contrast":
                picture = ImageEnhance.Contrast(picture).enhance(.24)
            elif condition == "blur":
                picture = picture.filter(ImageFilter.GaussianBlur(1.15))
            elif condition == "perspective":
                picture = picture.transform(picture.size, Image.Transform.PERSPECTIVE,
                                            (1, .055, -15, .015, 1, 0, .000035, .00006),
                                            Image.Resampling.BICUBIC, fillcolor="white")
            path = destination / f"ticket-{index:02d}-{condition}.png"
            picture.save(path)
            picture.close()
            samples.append({"path": path.name, "condition": condition,
                            "split": "development" if index in {0, 1, 10, 11} else "held_out", "expected": expected})
    # Negative controls avoid assuming
    # purchase/printing labels and their dates always survive in one text line.
    for index, label in enumerate(("购买时间", "PRINT", "", "放映时间")):
        picture = Image.new("RGB", (1060, 700), "white")
        draw = ImageDraw.Draw(picture)
        font = ImageFont.truetype(str(font_path), 36)
        draw.text((60, 150), label, font=font, fill="black")
        draw.text((60, 280), "2026-10-22 18:00", font=font, fill="black")
        path = destination / f"date-control-{index:02d}.png"
        picture.save(path)
        picture.close()
        samples.append({"path": path.name, "condition": "date_control", "split": "control", "expected": {}})
    for sample in samples:
        sample["sha256"] = hashlib.sha256((destination / sample["path"]).read_bytes()).hexdigest()
    (destination / "labels.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    return samples
