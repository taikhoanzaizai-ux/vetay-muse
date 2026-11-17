from pathlib import Path
import argparse
import base64
import hashlib
import json
import sys
import urllib.request
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "Data"
VERSION = "0.2.63"
PATCH_ABI = "vetay-windows-026"
PATCH_ROOT = "https://raw.githubusercontent.com/taikhoanzaizai-ux/vetay-muse/"

if (ROOT / "ffmpeg.exe").is_file():
    os.environ["FFMPEG_BINARY"] = str(ROOT / "ffmpeg.exe")
LOCK = threading.Lock()
LAST_ANALYSIS = {}
WHISPER_MODEL = None
VIDEO_SIZES = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "4:3": (1440, 1080),
    "21:9": (2520, 1080),
}

def prepare_data():
    DATA.mkdir(parents=True, exist_ok=True)
    temp = DATA / "Temp"
    temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp)
    os.environ["GRADIO_TEMP_DIR"] = str(temp / "gradio")
    os.environ["HF_HOME"] = str(DATA / "huggingface")

    local = os.environ.get("LOCALAPPDATA")
    marker = DATA / ".migration-complete"
    if not local or marker.exists():
        return
    legacy = Path(local) / "VeTay"
    if legacy.resolve() == DATA.resolve() or not legacy.is_dir():
        return
    files = list(legacy.glob("VeTay-*.mp4"))
    for name in ("models", "chrome-extension", "huggingface"):
        folder = legacy / name
        if folder.is_dir():
            files.extend(file for file in folder.rglob("*") if file.is_file())
    for source in files:
        target = DATA / source.relative_to(legacy)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as staging:
            copy = Path(staging) / source.name
            shutil.copy2(source, copy)
            if not target.exists():
                copy.replace(target)
    marker.touch()


def engine():
    path = ROOT / "engine"
    if not (path / "handdraw" / "render.py").is_file():
        raise RuntimeError("Bộ render Whiteboard Animation VI bị thiếu trong bản cài.")
    sys.path.insert(0, str(path))
    from handdraw.render import render_video
    from handdraw.settings import Settings
    return path, render_video, Settings

def ffmpeg(*args):
    exe = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("Không tìm thấy FFmpeg. Cài FFmpeg hoặc đặt FFMPEG_BINARY.")
    return subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                          check=True, capture_output=True)

def duration(path):
    import wave
    with wave.open(str(path), "rb") as track:
        return track.getnframes() / track.getframerate()

def transcribe(voice):
    from faster_whisper import WhisperModel
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        WHISPER_MODEL = WhisperModel("small", device="cpu", compute_type="int8", download_root=str(DATA / "models"))
    segments, _ = WHISPER_MODEL.transcribe(str(voice), language="vi", vad_filter=True, word_timestamps=True)
    return [(float(word.start), float(word.end), word.word)
            for segment in segments for word in (segment.words or ()) if word.word.strip()]

def scenes(words, seconds):
    if not words or seconds <= 0:
        raise ValueError("Không tìm thấy lời đọc hoặc âm thanh trống.")
    blocks = [(0.0, [])]
    previous_end = 0.0
    previous_word = ""
    for start, end, word in words:
        scene_start, current = blocks[-1]
        elapsed = start - scene_start
        pause = start - previous_end
        sentence_end = bool(re.search(r'[.!?…]+[)"”\']*$', previous_word.rstrip()))
        can_cut = current and seconds - start >= 2
        if can_cut and (elapsed >= 14 or (elapsed >= 4.5 and pause >= .55)
                        or (elapsed >= 6.5 and sentence_end)):
            current = []
            blocks.append((start, current))
        current.append(word)
        previous_end = end
        previous_word = word
    end_frame = round(seconds * 24)
    result = []
    for i, (start, words) in enumerate(blocks):
        first = round(start * 24)
        last = round(blocks[i+1][0] * 24) if i+1 < len(blocks) else end_frame
        text = "".join(words).strip()
        # Whiteboard Animation VI accepts up to 120 seconds for a single render.
        while last - first > 120 * 24:
            result.append((first, first + 120 * 24, text))
            first += 120 * 24
        if last > first:
            result.append((first, last, text))
    return result

def prompt_for(text, ratio="9:16", shared_prompt=""):
    rules = (
        (r"(?:chia|phân bổ).{0,45}(?:lương|ngân sách|phong bì)|(?:lương|ngân sách).{0,35}(?:chia|phong bì)",
         "Góc máy chếch từ trên xuống. Nhân vật chia tiền vào các phong bì trên bàn; dùng biểu tượng nhà, khiên và ly cà phê nếu cảnh có đủ ba khoản."),
        (r"chia.{0,35}(?:phần|tuần)|mỗi phần|500[.,]000",
         "Góc nhìn từ trên xuống: bàn tay đặt từng phần ngân sách vào các ô tuần trên lịch. Làm nổi bật các phần bằng hình khối lặp lại."),
        (r"lịch sử (?:giao dịch|thanh toán)|tiền đã đi đâu",
         "Cận cảnh nhân vật nhìn điện thoại, nét mặt ngạc nhiên. Trên màn hình là vài dòng giao dịch biểu thị bằng hình khối và biểu tượng mua sắm."),
        (r"quỹ dự phòng|tài khoản riêng|để dành|tiết kiệm|dành dụm",
         "Góc nhìn ba phần tư. Nhân vật chuyển một phần tiền sang hũ hoặc phong bì riêng có biểu tượng chiếc khiên; làm rõ đường di chuyển của tiền."),
        (r"nhận lương|lương (?:về|đã về)",
         "Cảnh trung tại bàn, nhân vật nhìn điện thoại khi lương về. Dùng mũi tên hướng vào ví để thể hiện dòng tiền."),
        (r"cà phê|ăn vặt|đồ uống",
         "Cận cảnh bàn tay thanh toán cạnh ly cà phê hoặc món ăn được nhắc đến. Bố trí ví và món mua thành hai điểm nhìn rõ ràng."),
        (r"nhón gót|bắp chân",
         "Góc nhìn ngang, lấy rõ từ đầu gối đến bàn chân. Thể hiện tư thế nhón gót và phần bắp chân bằng chuyển động dễ hiểu."),
        (r"thanh toán|chi tiêu|chi phí",
         "Góc nhìn chếch qua vai. Nhân vật đối chiếu các khoản chi trên bàn, với hóa đơn và đồng xu được nhóm theo nội dung đang kể."),
        (r"một năm|mỗi tháng|mỗi tuần|365 ngày|cuối tháng",
         "Dùng lịch để bàn làm chủ thể. Nhân vật chỉ vào mốc thời gian, dùng các ô lịch và mũi tên để biểu thị khoảng thời gian được nhắc."),
    )
    scene = next((visual for key, visual in rules if re.search(key, text, re.I)), "")
    if not scene:
        scene = ("Chuyển ý chính thành một hành động hoặc quan hệ nhìn thấy được. "
                 "Đặt chủ thể ở tiền cảnh, chọn bối cảnh phù hợp với hành động và dùng đạo cụ để làm rõ ý.")
    style = (shared_prompt or "").strip() or (
        "Nền trắng, nét đậm rõ, ít chi tiết, xanh ngọc và vàng nhạt. "
        "Không chữ, số, logo hoặc watermark."
    )
    return (f"Tạo một ảnh minh họa tỷ lệ {ratio} để chuyển thành video vẽ tay.\n"
            f"CẢNH CẦN VẼ — {scene}\n"
            f"PHONG CÁCH — Áp dụng trực tiếp vào nhân vật, đạo cụ, nền và bố cục cảnh trên: {style}\n"
            f"Ý NGHĨA CẢNH — {text}\n"
            "Chỉ minh họa nội dung của cảnh này. Không chép nguyên lời đọc lên ảnh. "
            "Ưu tiên yêu cầu chung nếu gợi ý đạo cụ hoặc góc nhìn xung đột với phong cách đã chọn.")


def timecode(frame):
    ms = round(frame * 1000 / 24)
    minutes, remainder = divmod(ms, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{minutes:02}:{seconds:02}.{fraction:03}"

def highlight_text(text):
    text = " ".join(text.split())
    number = re.search(
        r"(?<!\w)\d+(?:[.,]\d+)*(?:\s*(?:tỷ|triệu|nghìn|ngàn))?"
        r"(?:\s*(?:đồng|đ|%|kg|km|giờ|phút|giây|ngày|tuần|tháng|năm|lần|buổi|tuổi)\b|%)",
        text, re.IGNORECASE,
    )
    if number:
        return number.group().strip()
    phrase = re.split(r"[,;!?。]|(?<!\d)\.(?!\d)", text, maxsplit=1)[0].strip()
    return " ".join((phrase or text).split()[:7])[:74].rstrip(" ,.:;")


def highlight_overlay(text, width, height, target):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    size = max(14, min(width, height) // 13)
    padding = max(8, size // 2)
    max_width = width * 86 // 100 - 2 * padding
    for font_size in range(size, 9, -1):
        try:
            font = ImageFont.truetype(str(fonts / "arialbd.ttf"), font_size)
        except OSError:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        words = text.split()
        lines = [""]
        for word in words:
            trial = (lines[-1] + " " + word).strip()
            if lines[-1] and draw.textlength(trial, font=font) > max_width:
                lines.append(word)
            else:
                lines[-1] = trial
        if len(lines) <= 2 and all(draw.textlength(line, font=font) <= max_width for line in lines):
            break
    else:
        return False
    label = "\n".join(lines)
    spacing = max(3, font_size // 5)
    left, top, right, bottom = draw.multiline_textbbox((0, 0), label, font=font, spacing=spacing)
    box_width = right - left + padding * 2
    box_height = bottom - top + padding * 2
    x = (width - box_width) // 2
    y = height - (height // 12) - box_height
    draw.rounded_rectangle((x, y, x + box_width, y + box_height), radius=padding,
                           fill="#FFF1B8", outline="#08776B", width=max(2, padding // 5))
    draw.multiline_text((x + padding - left, y + padding - top), label, font=font,
                        fill="#07594F", spacing=spacing, align="center")
    image.save(target)
    return True


def scene_rows(plan, ratio="9:16", highlights=False, shared_prompt="", for_flow=True):
    image_ratio = {"21:9": "16:9", "4:5": "3:4"}.get(ratio, ratio) if for_flow else ratio
    if image_ratio != ratio:
        shared_prompt = (shared_prompt or "").strip() + "\nGiữ chủ thể trong vùng giữa để đặt vào video tỷ lệ " + ratio + "."
    return [[i, timecode(start), timecode(end), round((end-start)/24, 3), text, prompt_for(text, image_ratio, shared_prompt), highlight_text(text) if highlights else ""]
            for i, (start, end, text) in enumerate(plan, 1)]

def prepare_audio(script, voice, folder):
    wav = folder / "voice.wav"
    if not voice:
        if not script or not script.strip():
            raise ValueError("Nhập kịch bản hoặc tải bản thu.")
        import edge_tts
        speech = folder / "speech.mp3"
        asyncio.run(edge_tts.Communicate(script.strip(), "vi-VN-NamMinhNeural").save(str(speech)))
        voice = speech
    ffmpeg("-i", voice, "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav)
    return wav

def render_folder(voice, pictures, plan, destination, report, ratio="9:16", highlights=False, full_resolution=True):
    from PIL import Image, ImageOps
    import numpy as np
    path, render_video, Settings = engine()
    width, height = VIDEO_SIZES[ratio]
    preview_width, preview_height = (width, height) if full_resolution else tuple(size // 5 * 2 for size in (width, height))
    if len(pictures) < len(plan):
        raise ValueError(f"Cần {len(plan)} ảnh, hiện có {len(pictures)}.")
    with tempfile.TemporaryDirectory(prefix="vetay-", dir=destination.parent) as work:
        work = Path(work)
        clips = []
        for i, ((start, end, text), picture) in enumerate(zip(plan, pictures)):
            report(f"Whiteboard Animation VI: cảnh {i+1}/{len(plan)}…")
            if picture is None:
                clip = work / f"blank-{i:04}.mp4"
                ffmpeg("-f", "lavfi", "-i", f"color=white:s={preview_width}x{preview_height}:r=24",
                       "-frames:v", str(end-start), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", clip)
            else:
                with Image.open(picture) as raw:
                    art = ImageOps.contain(ImageOps.exif_transpose(raw).convert("RGB"), (preview_width, preview_height), method=Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (preview_width, preview_height), "white")
                canvas.paste(art, ((preview_width-art.width)//2, (preview_height-art.height)//2))
                settings = Settings(device="cpu", encoder="cpu", width=preview_width, height=preview_height, fps=24, fit="contain",
                                    duration=(end-start)/24, draw_percent=70, mode="e",
                                    subject_first=(path / "assets/v181d/model.safetensors").is_file())
                clip, _ = render_video(np.array(canvas), settings, output_dir=work)
            if highlights and (label := highlight_text(text)):
                overlay = work / f"highlight-{i:04}.png"
                if highlight_overlay(label, preview_width, preview_height, overlay):
                    titled = work / f"titled-{i:04}.mp4"
                    delay = min(1.3, (end-start)/48)
                    ffmpeg("-i", clip, "-i", overlay, "-filter_complex",
                           f"[0:v][1:v]overlay=enable='gte(t,{delay:.3f})':eof_action=repeat",
                           "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", titled)
                    clip = titled
            clips.append(clip)
        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
        output = work / "complete.mp4"
        ffmpeg("-f", "concat", "-safe", "0", "-i", listing, "-i", voice, "-map", "0:v", "-map", "1:a",
               "-vf", f"scale={width}:{height}:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-af", "apad", "-shortest", "-movflags", "+faststart", output)
        os.replace(output, destination)
    return str(destination)

def patch_read(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "VeTay-Windows", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Gói cập nhật vượt kích thước cho phép.")
    return data

def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Số phiên bản không hợp lệ.")
    return tuple(map(int, value.split(".")))

BUNDLED_EXTENSION = {
    "version": "1.3.38",
    "commit": "cae916926a416b7ca24417784230af4ffd5f4c30",
    "files": {
        "manifest.json": "5f7b104a8f6b194808141f6804a156f40b8a862d",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4",
        "worker.js": "19a8dfb3a3ce05cae01231889de33f5f2d545e7a",
        "flow.js": "5c9a0d0358750c316218dea722d0f34907a24f46"
    }
}
BUNDLED_EXTENSION_FILES = {
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjM4IiwKICAiYWN0aW9uIjogewogICAgImRlZmF1bHRfcG9wdXAiOiAicG9wdXAuaHRtbCIKICB9LAogICJiYWNrZ3JvdW5kIjogewogICAgInNlcnZpY2Vfd29ya2VyIjogIndvcmtlci5qcyIKICB9LAogICJwZXJtaXNzaW9ucyI6IFsKICAgICJzdG9yYWdlIiwKICAgICJhbGFybXMiLAogICAgImRlYnVnZ2VyIiwKICAgICJkb3dubG9hZHMiCiAgXSwKICAiaG9zdF9wZXJtaXNzaW9ucyI6IFsKICAgICJodHRwOi8vMTI3LjAuMC4xLyoiLAogICAgImh0dHBzOi8vKi5nb29nbGV1c2VyY29udGVudC5jb20vKiIsCiAgICAiaHR0cHM6Ly9mbG93Lmdvb2dsZS5jb20vKiIsCiAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIiwKICAgICJodHRwczovL211c2UuYWkvKiIKICBdLAogICJjb250ZW50X3NjcmlwdHMiOiBbCiAgICB7CiAgICAgICJtYXRjaGVzIjogWwogICAgICAgICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbS8qIiwKICAgICAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIgogICAgICBdLAogICAgICAianMiOiBbCiAgICAgICAgImZsb3cuanMiCiAgICAgIF0sCiAgICAgICJydW5fYXQiOiAiZG9jdW1lbnRfc3RhcnQiCiAgICB9LAogICAgewogICAgICAibWF0Y2hlcyI6IFsKICAgICAgICAiaHR0cHM6Ly9tdXNlLmFpLyoiCiAgICAgIF0sCiAgICAgICJqcyI6IFsKICAgICAgICAiZmxvdy5qcyIKICAgICAgXSwKICAgICAgInJ1bl9hdCI6ICJkb2N1bWVudF9zdGFydCIKICAgIH0KICBdCn0K",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg==",
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh7dG9rZW4sIHBvcnR9LCBwYXRoLCBib2R5KSB7CiAgY29uc3QgciA9IGF3YWl0IGZldGNoKCJodHRwOi8vMTI3LjAuMC4xOiIgKyBwb3J0ICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY29uc3QgcGVuZGluZ0Rvd25sb2FkcyA9IG5ldyBNYXAoKTsKY2hyb21lLmRvd25sb2Fkcy5vbkRldGVybWluaW5nRmlsZW5hbWUuYWRkTGlzdGVuZXIoKGl0ZW0sIHN1Z2dlc3QpID0+IHsKICBjb25zdCBwZW5kaW5nID0gWy4uLnBlbmRpbmdEb3dubG9hZHMudmFsdWVzKCldLmZpbHRlcihlbnRyeSA9PiAhZW50cnkuaWQgJiYgRGF0ZS5ub3coKSA8IGVudHJ5LmRlYWRsaW5lKTsKICBpZiAocGVuZGluZy5sZW5ndGggIT09IDEpIHJldHVybjsKICBjb25zdCBlbnRyeSA9IHBlbmRpbmdbMF07CiAgY29uc3QgdXJscyA9IFtpdGVtLnVybCwgaXRlbS5maW5hbFVybF0uZmlsdGVyKEJvb2xlYW4pOwogIGNvbnN0IGlzSW1hZ2UgPSAvXmltYWdlXC8oPzpwbmd8anBlZ3x3ZWJwKSQvaS50ZXN0KGl0ZW0ubWltZSB8fCAiIikKICAgIHx8IC9cLihwbmd8anBlP2d8d2VicCkkL2kudGVzdChpdGVtLmZpbGVuYW1lIHx8ICIiKTsKICBjb25zdCBmcm9tRmxvdyA9IGl0ZW0ucmVmZXJyZXIgPT09IGVudHJ5LnBhZ2UKICAgIHx8IHVybHMuc29tZSh1cmwgPT4gL15ibG9iOmh0dHBzOlwvXC9mbG93XC5nb29nbGVcLmNvbVwvL2kudGVzdCh1cmwpKQogICAgfHwgKGlzSW1hZ2UgJiYgIWl0ZW0ucmVmZXJyZXIgJiYgdXJscy5zb21lKHVybCA9PgogICAgICAvXmh0dHBzOlwvXC8oPzpbYS16MC05LV0rXC4pKig/Omdvb2dsZXVzZXJjb250ZW50XC5jb218Z3N0YXRpY1wuY29tKVwvL2kudGVzdCh1cmwpKSk7CiAgY29uc3QgbXVzZSA9IGVudHJ5LmZpbGVuYW1lICE9PSB1bmRlZmluZWQ7CiAgLy8gVGVuIHRlcCBNdXNlIGNodWEgbWEgY2FuaCB2ZXRheSBkdXkgbmhhdCBjdWEgZHVuZyBjYW5oIGRhbmcgY2hvLCBuZW4gY2hpIGNhbgogIC8vIGtob3AgdGVuIHRlcCAoa2UgY2Ega2hpIENocm9tZSB0aGVtICIgKDEpIiBsdWMgdHJ1bmcgdGVuKS4gRGlldSBraWVuIGN1IGRvaQogIC8vIHJlZmVycmVyIHBoYWkgYmFuZyBuZ3V5ZW4gVVJMIHRyYW5nIHhlbSBhbmggdmEgVVJMIHBoYWkgdGh1b2MgbXVzZS5haSwga2hpZW4KICAvLyBhbmggdGFpIHF1YSBDRE4gLyByZWZlcnJlciBydXQgZ29uIGJpIGJvIHF1YSB2YSBWZVRheSBraG9uZyBiYW8gZ2lvIG5oYW4gYW5oLgogIGNvbnN0IGJhc2VOYW1lID0gbmFtZSA9PiAobmFtZSB8fCAiIikuc3BsaXQoL1tcXC9dLykucG9wKCkudG9Mb3dlckNhc2UoKS5yZXBsYWNlKC8gXChcZCtcKShcLlteLl0rKSQvLCAiJDEiKTsKICBjb25zdCBmcm9tTXVzZSA9IG11c2UgJiYgaXNJbWFnZSAmJiBiYXNlTmFtZShpdGVtLmZpbGVuYW1lKSA9PT0gZW50cnkuZmlsZW5hbWUudG9Mb3dlckNhc2UoKTsKICBpZiAobXVzZSA/ICFmcm9tTXVzZSA6ICFmcm9tRmxvdykgcmV0dXJuOwogIGNvbnN0IGV4dGVuc2lvbiA9IC9cLihwbmd8anBlP2d8d2VicCkkL2kuZXhlYyhpdGVtLmZpbGVuYW1lIHx8ICIiKT8uWzFdIHx8ICJwbmciOwogIGVudHJ5LmlkID0gaXRlbS5pZDsKICBzdWdnZXN0KHtmaWxlbmFtZToobXVzZSA/ICJWZVRheS1NdXNlLyIgOiAiVmVUYXktMksvIikgKyAiIiArIGVudHJ5LnN0ZW0gKyAiLiIgKyBleHRlbnNpb24sIGNvbmZsaWN0QWN0aW9uOiJvdmVyd3JpdGUifSk7Cn0pOwpjaHJvbWUucnVudGltZS5vbk1lc3NhZ2UuYWRkTGlzdGVuZXIoKG0sc2VuZGVyLHJlcGx5KSA9PiB7CiAgKGFzeW5jKCk9PnsKICAgIGNvbnN0IG11c2UgPSBzZW5kZXIuZnJhbWVJZCA9PT0gMCAmJiBzZW5kZXIudGFiPy51cmw/LnN0YXJ0c1dpdGgoImh0dHBzOi8vbXVzZS5haS8iKTsKICAgIGlmICghbXVzZSAmJiAhZmxvd1RhYihzZW5kZXIudGFiPy51cmwpKSB0aHJvdyBFcnJvcigiVGFiIGtow7RuZyB0aHXhu5ljIEdvb2dsZSBGbG93IGhv4bq3YyBNdXNlLiIpOwogICAgY29uc3Qga2V5ID0gInBhaXJpbmc6IiArIHNlbmRlci50YWIuaWQ7CiAgICBpZiAobS5vcCA9PT0gInBhaXIiKSB7CiAgICAgIGlmICghL15bYS1mMC05XXszMn0kLy50ZXN0KG0udG9rZW4gfHwgIiIpKSB0aHJvdyBFcnJvcigiTcOjIGvhur90IG7hu5FpIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICBjb25zdCBwb3J0ID0gbS5wb3J0ID8/IDc4Nzc7CiAgICAgIGlmICghTnVtYmVyLmlzSW50ZWdlcihwb3J0KSB8fCBwb3J0IDwgMTAyNCB8fCBwb3J0ID4gNjU1MzUpIHRocm93IEVycm9yKCJD4buVbmcgVuG6vSBUYXkga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIGNvbnN0IGNvbm5lY3Rpb24gPSB7dG9rZW46bS50b2tlbiwgcG9ydH07CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShjb25uZWN0aW9uLCAiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lKSB0aHJvdyBFcnJvcigiVuG6vSBUYXkgY2jGsGEgY8OzIGPhuqNuaCBjaOG7nS4iKTsKICAgICAgaWYgKG11c2UgIT09IChqb2IucHJvdmlkZXIgPT09ICJtdXNlIikpIHRocm93IEVycm9yKCJOZ3Xhu5NuIOG6o25oIGtow7RuZyBraOG7m3AgdMOhYyB24bulLiIpOwogICAgICBhd2FpdCBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLnNldCh7W2tleV06Y29ubmVjdGlvbn0pOwogICAgICByZXR1cm4gam9iOwogICAgfQogICAgY29uc3Qgc2F2ZWQgPSBhd2FpdCBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLmdldChrZXkpOwogICAgY29uc3QgcGFpcmluZyA9IHR5cGVvZiBzYXZlZFtrZXldID09PSAic3RyaW5nIiA/IHt0b2tlbjpzYXZlZFtrZXldLHBvcnQ6Nzg3N30gOiBzYXZlZFtrZXldOwogICAgaWYgKG0ub3AgPT09ICJyZXN1bWUiKSByZXR1cm4gcGFpcmluZyA/IGFwaShwYWlyaW5nLCIvbmV4dCIpIDoge2RvbmU6dHJ1ZX07CiAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJUYWIgY2jGsGEgxJHGsOG7o2MgVuG6vSBUYXkga+G6v3QgbuG7kWkuIik7CiAgICBpZiAobXVzZSAmJiAhWyJuZXh0IiwiYmF0Y2giLCJwcm9ncmVzcyIsImVycm9yIiwiYXJtTXVzZSIsInRyYW5zZmVyTXVzZSIsInNraXBNdXNlIl0uaW5jbHVkZXMobS5vcCkpCiAgICAgIHRocm93IEVycm9yKCJUaGFvIHTDoWMga2jDtG5nIGjhu6NwIGzhu4cgdHJvbmcgdGFiIE11c2UuIik7CiAgICBpZiAobS5vcCA9PT0gInNraXBNdXNlIikgewogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkocGFpcmluZywgIi9uZXh0Iik7CiAgICAgIGlmICghbXVzZSB8fCBqb2IucHJvdmlkZXIgIT09ICJtdXNlIiB8fCBqb2Iuam9iICE9PSBtLmpvYiB8fCBqb2IuaW5kZXggIT09IG0uaW5kZXgpCiAgICAgICAgdGhyb3cgRXJyb3IoIkPhuqNuaCBNdXNlIGPhuqduIGLhu48gcXVhIGtow7RuZyBraOG7m3AuIik7CiAgICAgIHBlbmRpbmdEb3dubG9hZHMuZGVsZXRlKGtleSk7CiAgICAgIHJldHVybiBhcGkocGFpcmluZywgIi9za2lwTXVzZS8iICsgam9iLmpvYiArICIvIiArIGpvYi5pbmRleCwgInt9Iik7CiAgICB9CiAgICBpZiAobS5vcCA9PT0gImFybU11c2UiIHx8IG0ub3AgPT09ICJ0cmFuc2Zlck11c2UiKSB7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShwYWlyaW5nLCAiL25leHQiKTsKICAgICAgaWYgKCFtdXNlIHx8IGpvYi5wcm92aWRlciAhPT0gIm11c2UiIHx8IGpvYi5qb2IgIT09IG0uam9iIHx8IGpvYi5pbmRleCAhPT0gbS5pbmRleCkKICAgICAgICB0aHJvdyBFcnJvcigiVMOhYyB24bulIOG6o25oIE11c2UgxJHDoyDEkeG7lWkuIik7CiAgICAgIGlmIChtLm9wID09PSAiYXJtTXVzZSIpIHsKICAgICAgICBjb25zdCBwYWdlID0gbmV3IFVSTChzZW5kZXIudGFiLnVybCk7CiAgICAgICAgY29uc3QgZmlsZW5hbWUgPSBwYWdlLnNlYXJjaFBhcmFtcy5nZXQoImZpbGUiKT8uc3BsaXQoIi8iKS5wb3AoKTsKICAgICAgICBjb25zdCB0YWcgPSAidmV0YXkiICsgam9iLmpvYi5zbGljZSgwLCAxMikgKyAiYyIgKyBTdHJpbmcoam9iLmluZGV4ICsgMSkucGFkU3RhcnQoNSwgIjAiKTsKICAgICAgICBpZiAocGFnZS5wYXRobmFtZSAhPT0gIi9saWJyYXJ5L21lZGlhIiB8fCBmaWxlbmFtZSAhPT0gbS5maWxlbmFtZSB8fAogICAgICAgICAgICAhZmlsZW5hbWU/LnRvTG93ZXJDYXNlKCkuaW5jbHVkZXModGFnKSB8fCAhL1wuKHBuZ3xqcGU/Z3x3ZWJwKSQvaS50ZXN0KGZpbGVuYW1lKSkKICAgICAgICAgIHRocm93IEVycm9yKCLhuqJuaCDEkWFuZyBt4bufIGtow7RuZyBraOG7m3AgbcOjIGPhuqNuaCBNdXNlLiIpOwogICAgICAgIHBlbmRpbmdEb3dubG9hZHMuc2V0KGtleSwge3N0ZW06IGpvYi5qb2IgKyAiLSIgKyBqb2IuaW5kZXgsIGZpbGVuYW1lLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHBhZ2U6IHNlbmRlci50YWIudXJsLCBkZWFkbGluZTogRGF0ZS5ub3coKSArIDkwMDAwfSk7CiAgICAgICAgcmV0dXJuIHthcm1lZDp0cnVlfTsKICAgICAgfQogICAgICBjb25zdCBlbnRyeSA9IHBlbmRpbmdEb3dubG9hZHMuZ2V0KGtleSk7CiAgICAgIGlmICghZW50cnk/LmlkKSByZXR1cm4ge3JlY2VpdmVkOmZhbHNlfTsKICAgICAgY29uc3QgW2l0ZW1dID0gYXdhaXQgY2hyb21lLmRvd25sb2Fkcy5zZWFyY2goe2lkOmVudHJ5LmlkfSk7CiAgICAgIGlmIChpdGVtPy5zdGF0ZSA9PT0gImludGVycnVwdGVkIikgdGhyb3cgRXJyb3IoIkNocm9tZSDEkcOjIG5n4bqvdCBsxrDhu6N0IHThuqNpIOG6o25oIE11c2UuIik7CiAgICAgIGlmIChpdGVtPy5zdGF0ZSAhPT0gImNvbXBsZXRlIikgcmV0dXJuIHtyZWNlaXZlZDpmYWxzZX07CiAgICAgIGNvbnN0IHJlc3VsdCA9IGF3YWl0IGFwaShwYWlyaW5nLCAiL2Rvd25sb2FkTXVzZS8iICsgam9iLmpvYiArICIvIiArIGpvYi5pbmRleCwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIEpTT04uc3RyaW5naWZ5KHtwYXRoOml0ZW0uZmlsZW5hbWV9KSk7CiAgICAgIHBlbmRpbmdEb3dubG9hZHMuZGVsZXRlKGtleSk7CiAgICAgIHJldHVybiByZXN1bHQ7CiAgICB9CiAgICBpZiAobS5vcCA9PT0gImFybTJrIiB8fCBtLm9wID09PSAidHJhbnNmZXIyayIpIHsKICAgICAgY29uc3Qgam9iID0gYXdhaXQgYXBpKHBhaXJpbmcsIi9uZXh0Iik7CiAgICAgIGlmIChqb2IuZG9uZSB8fCBqb2Iuam9iICE9PSBtLmpvYiB8fCBqb2IuaW5kZXggIT09IG0uaW5kZXgpIHRocm93IEVycm9yKCJUw6FjIHbhu6Ug4bqjbmggMksgxJHDoyDEkeG7lWkuIik7CiAgICAgIGlmIChtLm9wID09PSAiYXJtMmsiKSB7CiAgICAgICAgcGVuZGluZ0Rvd25sb2Fkcy5zZXQoa2V5LHtzdGVtOmpvYi5qb2IgKyAiLSIgKyBqb2IuaW5kZXgsIGRlYWRsaW5lOkRhdGUubm93KCkrMTUwMDAwLHBhZ2U6c2VuZGVyLnRhYi51cmx9KTsKICAgICAgICByZXR1cm4ge2FybWVkOnRydWV9OwogICAgICB9CiAgICAgIGNvbnN0IGVudHJ5ID0gcGVuZGluZ0Rvd25sb2Fkcy5nZXQoa2V5KTsKICAgICAgaWYgKCFlbnRyeT8uaWQpIHJldHVybiB7cmVjZWl2ZWQ6ZmFsc2V9OwogICAgICBjb25zdCBbaXRlbV0gPSBhd2FpdCBjaHJvbWUuZG93bmxvYWRzLnNlYXJjaCh7aWQ6ZW50cnkuaWR9KTsKICAgICAgaWYgKGl0ZW0/LnN0YXRlID09PSAiaW50ZXJydXB0ZWQiKSB0aHJvdyBFcnJvcigiQ2hyb21lIMSRw6Mgbmfhuq90IGzGsOG7o3QgdOG6o2kg4bqjbmggMksuIik7CiAgICAgIGlmIChpdGVtPy5zdGF0ZSAhPT0gImNvbXBsZXRlIikgcmV0dXJuIHtyZWNlaXZlZDpmYWxzZX07CiAgICAgIGNvbnN0IHJlc3VsdCA9IGF3YWl0IGFwaShwYWlyaW5nLCIvZG93bmxvYWQyay8iICsgam9iLmpvYiArICIvIiArIGpvYi5pbmRleCxKU09OLnN0cmluZ2lmeSh7cGF0aDppdGVtLmZpbGVuYW1lfSkpOwogICAgICBwZW5kaW5nRG93bmxvYWRzLmRlbGV0ZShrZXkpOwogICAgICByZXR1cm4gcmVzdWx0OwogICAgfQogICAgaWYgKG0ub3AgPT09ICJzdWJtaXQiKSB7CiAgICAgIGNvbnN0IHRhYiA9IGF3YWl0IGNocm9tZS50YWJzLmdldChzZW5kZXIudGFiLmlkKTsKICAgICAgY29uc3QgcGFnZSA9IG5ldyBVUkwodGFiLnVybCk7CiAgICAgIGlmIChzZW5kZXIuZnJhbWVJZCAhPT0gMCB8fCBwYWdlLm9yaWdpbiAhPT0gImh0dHBzOi8vZmxvdy5nb29nbGUuY29tIgogICAgICAgIHx8ICEvXlwvcHJvamVjdFwvW14vXSsoPzpcLy4qKT8kLy50ZXN0KHBhZ2UucGF0aG5hbWUpKQogICAgICAgIHRocm93IEVycm9yKCJM4buHbmggYuG6pW0ga2jDtG5nIMSR4bq/biB04burIG3DoG4gZOG7sSDDoW4gaG/hurdjIG3DoG4geGVtIOG6o25oIEZsb3cuIik7CiAgICAgIGlmICghW20ueCxtLnldLmV2ZXJ5KE51bWJlci5pc0Zpbml0ZSkgfHwgbS54IDwgMCB8fCBtLnkgPCAwIHx8IG0ueCA+IDEwMDAwIHx8IG0ueSA+IDEwMDAwKQogICAgICAgIHRocm93IEVycm9yKCJOw7p0IEZsb3cgbuG6sW0gbmdvw6BpIHbDuW5nIGPDsyB0aOG7gyBi4bqlbS4iKTsKICAgICAgY29uc3Qgam9iID0gYXdhaXQgYXBpKHBhaXJpbmcsIi9uZXh0Iik7CiAgICAgIGlmIChqb2IuZG9uZSB8fCBqb2Iuam9iICE9PSBtLmpvYikgdGhyb3cgRXJyb3IoIlTDoWMgduG7pSBW4bq9IFRheSDEkcOjIMSR4buVaS4iKTsKICAgICAgY29uc3QgdGFyZ2V0ID0ge3RhYklkOnRhYi5pZH07CiAgICAgIGF3YWl0IGNocm9tZS5kZWJ1Z2dlci5hdHRhY2godGFyZ2V0LCIxLjMiKTsKICAgICAgdHJ5IHsKICAgICAgICBjb25zdCBpbnB1dCA9ICh0eXBlLGJ1dHRvbnMpID0+IGNocm9tZS5kZWJ1Z2dlci5zZW5kQ29tbWFuZCh0YXJnZXQsIklucHV0LmRpc3BhdGNoTW91c2VFdmVudCIsCiAgICAgICAgICB7dHlwZSx4Om0ueCx5Om0ueSxidXR0b246ImxlZnQiLGJ1dHRvbnMsY2xpY2tDb3VudDoxfSk7CiAgICAgICAgYXdhaXQgaW5wdXQoIm1vdXNlUHJlc3NlZCIsMSk7CiAgICAgICAgYXdhaXQgaW5wdXQoIm1vdXNlUmVsZWFzZWQiLDApOwogICAgICAgIGlmIChtLmNsaWNrcyA9PT0gMikgewogICAgICAgICAgY29uc3Qgc2Vjb25kID0gKHR5cGUsYnV0dG9ucykgPT4gY2hyb21lLmRlYnVnZ2VyLnNlbmRDb21tYW5kKHRhcmdldCwiSW5wdXQuZGlzcGF0Y2hNb3VzZUV2ZW50IiwKICAgICAgICAgICAge3R5cGUseDptLngseTptLnksYnV0dG9uOiJsZWZ0IixidXR0b25zLGNsaWNrQ291bnQ6Mn0pOwogICAgICAgICAgYXdhaXQgc2Vjb25kKCJtb3VzZVByZXNzZWQiLDEpOwogICAgICAgICAgYXdhaXQgc2Vjb25kKCJtb3VzZVJlbGVhc2VkIiwwKTsKICAgICAgICB9CiAgICAgIH0gZmluYWxseSB7CiAgICAgICAgYXdhaXQgY2hyb21lLmRlYnVnZ2VyLmRldGFjaCh0YXJnZXQpOwogICAgICB9CiAgICAgIHJldHVybiB7Y2xpY2tlZDp0cnVlfTsKICAgIH0KICAgIGlmIChtLm9wID09PSAiYWN0aXZhdGUiKSB7CiAgICAgIGF3YWl0IGNocm9tZS50YWJzLnVwZGF0ZShzZW5kZXIudGFiLmlkLCB7YWN0aXZlOnRydWV9KTsKICAgICAgYXdhaXQgY2hyb21lLndpbmRvd3MudXBkYXRlKHNlbmRlci50YWIud2luZG93SWQsIHtmb2N1c2VkOnRydWV9KTsKICAgICAgcmV0dXJuIHthY3RpdmU6dHJ1ZX07CiAgICB9CiAgICBpZiAobS5vcCA9PT0gIm5leHQiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9uZXh0Iik7CiAgICBpZiAobS5vcCA9PT0gImJhdGNoIikgcmV0dXJuIGFwaShwYWlyaW5nLCIvYmF0Y2giKTsKICAgIGlmIChtLm9wID09PSAicHJvZ3Jlc3MiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9wcm9ncmVzcyIsSlNPTi5zdHJpbmdpZnkoe2NvdW50Om0uY291bnQsc3RhZ2U6bS5zdGFnZX0pKTsKICAgIGlmIChtLm9wID09PSAiZXJyb3IiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9lcnJvciIsSlNPTi5zdHJpbmdpZnkoe21lc3NhZ2U6bS5tZXNzYWdlfSkpOwogICAgaWYgKG0ub3AgPT09ICJpbWFnZSIgJiYgL15bYS1mMC05XXszMn0kLy50ZXN0KG0uam9iKSAmJiBOdW1iZXIuaXNJbnRlZ2VyKG0uaW5kZXgpKSB7CiAgICAgIGxldCBpbWFnZTsKICAgICAgaWYgKG0uZGF0YSAmJiAvXmRhdGE6aW1hZ2VcLyhwbmd8d2VicHxqcGVnKTtiYXNlNjQsLy50ZXN0KG0uZGF0YSkpIHsKICAgICAgICBpbWFnZSA9IGF3YWl0IChhd2FpdCBmZXRjaChtLmRhdGEpKS5ibG9iKCk7CiAgICAgIH0gZWxzZSB7CiAgICAgICAgY29uc3QgdSA9IG5ldyBVUkwobS51cmwpOwogICAgICAgIGlmICh1LnByb3RvY29sICE9PSAiaHR0cHM6IiB8fCAhLyhefFwuKWdvb2dsZXVzZXJjb250ZW50XC5jb20kLy50ZXN0KHUuaG9zdG5hbWUpKSB0aHJvdyBFcnJvcigiS2jDtG5nIHjDoWMgxJHhu4tuaCDEkcaw4bujYyDEkeG7i2EgY2jhu4kg4bqjbmgga+G6v3QgcXXhuqMuIik7CiAgICAgICAgY29uc3QgciA9IGF3YWl0IGZldGNoKHUuaHJlZix7Y3JlZGVudGlhbHM6Im9taXQiLHJlZGlyZWN0OiJlcnJvciJ9KTsKICAgICAgICBpZiAoIXIub2spIHRocm93IEVycm9yKCJLaMO0bmcgdOG6o2kgxJHGsOG7o2Mg4bqjbmgga+G6v3QgcXXhuqMuIik7CiAgICAgICAgaW1hZ2UgPSBhd2FpdCByLmJsb2IoKTsKICAgICAgfQogICAgICBpZiAoaW1hZ2Uuc2l6ZSA+IDI0KjEwMjQqMTAyNCB8fCAhaW1hZ2UudHlwZS5zdGFydHNXaXRoKCJpbWFnZS8iKSkgdGhyb3cgRXJyb3IoIuG6om5oIGvhur90IHF14bqjIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICByZXR1cm4gYXBpKHBhaXJpbmcsIi9pbWFnZS8iK20uam9iKyIvIittLmluZGV4LGltYWdlKTsKICAgIH0KICAgIHRocm93IEVycm9yKCJZw6p1IGPhuqd1IGtow7RuZyBo4bujcCBs4buHLiIpOwogIH0pKCkudGhlbihyZXN1bHQ9PnJlcGx5KHtvazp0cnVlLHJlc3VsdH0pLGVycm9yPT5yZXBseSh7b2s6ZmFsc2UsZXJyb3I6ZXJyb3IubWVzc2FnZX0pKTsKICByZXR1cm4gdHJ1ZTsKfSk7CmNocm9tZS50YWJzLm9uUmVtb3ZlZC5hZGRMaXN0ZW5lcih0YWJJZCA9PiBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLnJlbW92ZSgicGFpcmluZzoiICsgdGFiSWQpKTsKCmNvbnN0IGV4dGVuc2lvbkNoZWNrID0gInZldGF5LWNoZWNrLWxvY2FsLXVwZGF0ZSI7CmZ1bmN0aW9uIHJlZ2lzdGVyQ2hlY2soKSB7CiAgY2hyb21lLmFsYXJtcy5jcmVhdGUoZXh0ZW5zaW9uQ2hlY2ssIHtwZXJpb2RJbk1pbnV0ZXM6MX0pOwp9CmNocm9tZS5ydW50aW1lLm9uSW5zdGFsbGVkLmFkZExpc3RlbmVyKHJlZ2lzdGVyQ2hlY2spOwpjaHJvbWUucnVudGltZS5vblN0YXJ0dXAuYWRkTGlzdGVuZXIocmVnaXN0ZXJDaGVjayk7CmNocm9tZS5hbGFybXMub25BbGFybS5hZGRMaXN0ZW5lcihhc3luYyBhbGFybSA9PiB7CiAgaWYgKGFsYXJtLm5hbWUgIT09IGV4dGVuc2lvbkNoZWNrKSByZXR1cm47CiAgdHJ5IHsKICAgIGNvbnN0IGN1cnJlbnQgPSBjaHJvbWUucnVudGltZS5nZXRNYW5pZmVzdCgpOwogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBmZXRjaChjaHJvbWUucnVudGltZS5nZXRVUkwoIm1hbmlmZXN0Lmpzb24iKSArICI/dD0iICsgRGF0ZS5ub3coKSwge2NhY2hlOiJuby1zdG9yZSJ9KTsKICAgIGlmICghcmVzcG9uc2Uub2spIHJldHVybjsKICAgIGNvbnN0IGRpc2sgPSBhd2FpdCByZXNwb25zZS5qc29uKCk7CiAgICBpZiAoIS9eXGQrXC5cZCtcLlxkKyQvLnRlc3QoZGlzay52ZXJzaW9uKSB8fCBkaXNrLm5hbWUgIT09IGN1cnJlbnQubmFtZSB8fAogICAgICAgIGRpc2subWFuaWZlc3RfdmVyc2lvbiAhPT0gY3VycmVudC5tYW5pZmVzdF92ZXJzaW9uKSByZXR1cm47CiAgICBjb25zdCBvbGQgPSBjdXJyZW50LnZlcnNpb24uc3BsaXQoIi4iKS5tYXAoTnVtYmVyKTsKICAgIGNvbnN0IG5leHQgPSBkaXNrLnZlcnNpb24uc3BsaXQoIi4iKS5tYXAoTnVtYmVyKTsKICAgIGNvbnN0IGNoYW5nZWQgPSBuZXh0LmZpbmRJbmRleCgodmFsdWUsaSk9PnZhbHVlIT09b2xkW2ldKTsKICAgIGlmIChjaGFuZ2VkIDwgMCB8fCBuZXh0W2NoYW5nZWRdIDwgb2xkW2NoYW5nZWRdKSByZXR1cm47CiAgICBmb3IoY29uc3Qga2V5IG9mIFsicGVybWlzc2lvbnMiLCJob3N0X3Blcm1pc3Npb25zIiwib3B0aW9uYWxfcGVybWlzc2lvbnMiLCJvcHRpb25hbF9ob3N0X3Blcm1pc3Npb25zIl0pIHsKICAgICAgaWYoSlNPTi5zdHJpbmdpZnkoWy4uLihjdXJyZW50W2tleV18fFtdKV0uc29ydCgpKSE9PUpTT04uc3RyaW5naWZ5KFsuLi4oZGlza1trZXldfHxbXSldLnNvcnQoKSkpIHJldHVybjsKICAgIH0KICAgIGNvbnN0IG1hdGNoZXMgPSB2YWx1ZSA9PiAodmFsdWUuY29udGVudF9zY3JpcHRzIHx8IFtdKS5mbGF0TWFwKHNjcmlwdD0+c2NyaXB0Lm1hdGNoZXMgfHwgW10pLnNvcnQoKTsKICAgIGlmKEpTT04uc3RyaW5naWZ5KG1hdGNoZXMoY3VycmVudCkpIT09SlNPTi5zdHJpbmdpZnkobWF0Y2hlcyhkaXNrKSkpIHJldHVybjsKICAgIGNvbnN0IGlkbGUgPSBhd2FpdCBmZXRjaChiYXNlKyIvZXh0ZW5zaW9uLWlkbGUiLHtjYWNoZToibm8tc3RvcmUifSk7CiAgICBpZihpZGxlLm9rICYmIChhd2FpdCBpZGxlLmpzb24oKSkuaWRsZSA9PT0gdHJ1ZSkgY2hyb21lLnJ1bnRpbWUucmVsb2FkKCk7CiAgfSBjYXRjaCB7fQp9KTsK",
    "flow.js": "aWYgKGxvY2F0aW9uLm9yaWdpbiAhPT0gImh0dHBzOi8vbXVzZS5haSIpIHsKKCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkoPzotKFswLTldezQsNX0pKT8kLy5leGVjKGxvY2F0aW9uLmhhc2gpOwogIGlmICh3aW5kb3cuX192ZVRheVJ1bm5pbmcpIHJldHVybjsKICB3aW5kb3cuX192ZVRheVJ1bm5pbmcgPSB0cnVlOwogIGNvbnN0IHNsZWVwID0gKG1zKSA9PiBuZXcgUHJvbWlzZShyZXNvbHZlID0+IHNldFRpbWVvdXQocmVzb2x2ZSxtcykpOwogIGNvbnN0IHZpc2libGUgPSBub2RlID0+IG5vZGUuZ2V0Q2xpZW50UmVjdHMoKS5sZW5ndGggJiYgZ2V0Q29tcHV0ZWRTdHlsZShub2RlKS52aXNpYmlsaXR5ICE9PSAiaGlkZGVuIjsKICBjb25zdCBzZW5kID0gYXN5bmMgKG1lc3NhZ2UpID0+IHsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgY2hyb21lLnJ1bnRpbWUuc2VuZE1lc3NhZ2UobWVzc2FnZSk7CiAgICBpZiAoIXJlc3BvbnNlPy5vaykgdGhyb3cgRXJyb3IocmVzcG9uc2U/LmVycm9yIHx8ICJN4bqldCBr4bq/dCBu4buRaSB24bubaSBW4bq9IFRheS4iKTsKICAgIHJldHVybiByZXNwb25zZS5yZXN1bHQ7CiAgfTsKICBmdW5jdGlvbiByZXN1bHRJbWFnZXMoY2FyZCkgewogICAgcmV0dXJuIFsuLi5jYXJkLnF1ZXJ5U2VsZWN0b3JBbGwoImltZyIpXS5maWx0ZXIoaW1nID0+IHsKICAgICAgaWYgKCFpbWcuY29tcGxldGUgfHwgIWltZy5uYXR1cmFsV2lkdGggfHwgIWltZy5uYXR1cmFsSGVpZ2h0IHx8ICF2aXNpYmxlKGltZykpIHJldHVybiBmYWxzZTsKICAgICAgY29uc3Qgc3JjID0gaW1nLmN1cnJlbnRTcmMgfHwgaW1nLnNyYzsKICAgICAgY29uc3QgdXJsID0gbmV3IFVSTChzcmMsIGxvY2F0aW9uLmhyZWYpOwogICAgICBjb25zdCBtZWRpYSA9IC9eKMOUIGhp4buDbiB0aOG7iyBow6xuaCDhuqNuaCBj4bunYSBuZ8aw4budaSBkw7luZ3xVc2VyIGltYWdlIHRpbGUpJC9pdS50ZXN0KGltZy5hbHQudHJpbSgpKTsKICAgICAgY29uc3Qgb3JpZ2luYWwgPSAvXihibG9iOnxodHRwczpcL1wvW14vXSpcLmdvb2dsZXVzZXJjb250ZW50XC5jb21cLykvaS50ZXN0KHNyYyk7CiAgICAgIHJldHVybiBvcmlnaW5hbCB8fCAobWVkaWEgJiYgWyJodHRwczoiLCAiZGF0YToiLCAiYmxvYjoiXS5pbmNsdWRlcyh1cmwucHJvdG9jb2wpKTsKICAgIH0pOwogIH0KICBjb25zdCBjYXB0aW9uID0gYnV0dG9uID0+IChidXR0b24uZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgYnV0dG9uLmdldEF0dHJpYnV0ZSgidGl0bGUiKSB8fCBidXR0b24udGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBmdW5jdGlvbiBwcm9tcHRGaWVsZCgpIHsKICAgIGNvbnN0IGZpZWxkcyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPSJ0cnVlIl0sW2NvbnRlbnRlZGl0YWJsZT0icGxhaW50ZXh0LW9ubHkiXScpXQogICAgICAuZmlsdGVyKGZpZWxkID0+IHZpc2libGUoZmllbGQpICYmICFmaWVsZC5jbG9zZXN0KCdbaW5lcnRdLFthcmlhLWhpZGRlbj0idHJ1ZSJdLFtyb2xlPSJzZWFyY2giXScpCiAgICAgICAgJiYgIWZpZWxkLmRpc2FibGVkICYmICFmaWVsZC5yZWFkT25seSAmJiBmaWVsZC5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IGZpZWxkID0+IFsiYXJpYS1sYWJlbCIsICJwbGFjZWhvbGRlciIsICJkYXRhLXBsYWNlaG9sZGVyIiwgInRpdGxlIl0KICAgICAgLm1hcChuYW1lID0+IGZpZWxkLmdldEF0dHJpYnV0ZShuYW1lKSB8fCAiIikuam9pbigiICIpOwogICAgY29uc3QgZWxpZ2libGUgPSBmaWVsZHMuZmlsdGVyKGZpZWxkID0+ICEvc2VhcmNofHTDrG0ga2nhur9tfHJlbmFtZXzEkeG7lWkgdMOqbnxwcm9qZWN0IG5hbWV8dMOqbiBk4buxIMOhbi9pLnRlc3QobGFiZWxzKGZpZWxkKSkKICAgICAgJiYgIWZpZWxkcy5zb21lKHBhcmVudCA9PiBwYXJlbnQgIT09IGZpZWxkICYmIHBhcmVudC5jb250YWlucyhmaWVsZCkpKTsKICAgIGNvbnN0IG1hdGNoZXMgPSBlbGlnaWJsZS5maWx0ZXIoZmllbGQgPT4gL3Byb21wdHx3aGF0ICg/OmRvfHdvdWxkKSB5b3V8ZGVzY3JpYmV8bcO0IHThuqN8YuG6oW4gbXXhu5FufHThuqFvIG7hu5lpIGR1bmcvaS50ZXN0KGxhYmVscyhmaWVsZCkpKTsKICAgIGlmIChtYXRjaGVzLmxlbmd0aCA9PT0gMSkgcmV0dXJuIG1hdGNoZXNbMF07CiAgICByZXR1cm4gZWxpZ2libGUubGVuZ3RoID09PSAxID8gZWxpZ2libGVbMF0gOiBudWxsOwogIH0KICBjb25zdCByYXRpb0xhYmVsID0gcmF0aW8gPT4gbmV3IFJlZ0V4cCgiKD86XnxbXjAtOV0pIiArIHJhdGlvLnJlcGxhY2UoIjoiLCAiXFxzKls64oi2XVxccyoiKSArICIoPyFbMC05XSkiLCAidSIpOwogIGFzeW5jIGZ1bmN0aW9uIHNlbGVjdFJhdGlvKHJhdGlvKSB7CiAgICBjb25zdCBzZWxlY3RlZCA9IG5vZGUgPT4gWyJhcmlhLWNoZWNrZWQiLCAiYXJpYS1zZWxlY3RlZCIsICJhcmlhLXByZXNzZWQiXS5zb21lKGtleSA9PiBub2RlLmdldEF0dHJpYnV0ZShrZXkpID09PSAidHJ1ZSIpCiAgICAgIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXN0YXRlIikgPT09ICJjaGVja2VkIiB8fCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS1zdGF0ZSIpID09PSAiYWN0aXZlIjsKICAgIGNvbnN0IGNvbnRyb2xzID0gKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXJhZGlvXSxbcm9sZT1vcHRpb25dLFtyb2xlPW1lbnVpdGVtcmFkaW9dLFtyb2xlPXRhYl0iKV0KICAgICAgLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgIW5vZGUuZGlzYWJsZWQgJiYgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IG5vZGUgPT4gW2NhcHRpb24obm9kZSksIG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXZhbHVlIikgfHwgIiIsIG5vZGUuaW5uZXJUZXh0IHx8ICIiXS5qb2luKCIgIik7CiAgICBjb25zdCBleGFjdCA9IHJhdGlvTGFiZWwocmF0aW8pOwogICAgY29uc3QgaWNvbk5hbWUgPSAiY3JvcF8iICsgcmF0aW8ucmVwbGFjZSgiOiIsICJfIik7CiAgICBjb25zdCBtYXRjaGVzUmF0aW8gPSBub2RlID0+IGV4YWN0LnRlc3QobGFiZWxzKG5vZGUpKSB8fCBbLi4ubm9kZS5xdWVyeVNlbGVjdG9yQWxsKCJtYXQtaWNvbixbcm9sZT1pbWddLHN2ZyIpXQogICAgICAuc29tZShpY29uID0+IFtpY29uLnRleHRDb250ZW50LCBpY29uLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpLCBpY29uLmdldEF0dHJpYnV0ZSgiZGF0YS1pY29uIildCiAgICAgICAgLnNvbWUobGFiZWwgPT4gbGFiZWw/LnRyaW0oKSA9PT0gaWNvbk5hbWUpKTsKICAgIGNvbnN0IGhhc1JhdGlvID0gcmF0aW9MYWJlbCgiWzAtOV0rOlswLTldKyIpOwogICAgY29uc3QgaXNPcHRpb24gPSBub2RlID0+IFsicmFkaW8iLCAib3B0aW9uIiwgIm1lbnVpdGVtcmFkaW8iXS5pbmNsdWRlcyhub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpKTsKICAgIGNvbnN0IHRyaWdnZXIgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+ICFpc09wdGlvbihub2RlKSAmJiAobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtaGFzcG9wdXAiKQogICAgICA/IC9hc3BlY3QgcmF0aW98dOG7tyBs4buHfHThu4kgbOG7hy9pdS50ZXN0KGxhYmVscyhub2RlKSkgfHwgaGFzUmF0aW8udGVzdChsYWJlbHMobm9kZSkpCiAgICAgIDogL14oPzphc3BlY3QgcmF0aW98dOG7tyBs4buHKD86IGtodW5nIGjDrG5oKT98dOG7iSBs4buHKD86IGtodW5nIGjDrG5oKT8pJC9pdS50ZXN0KGNhcHRpb24obm9kZSkpKSk7CiAgICBjb25zdCBvcHRpb25zID0gKCkgPT4gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAhdHJpZ2dlci5pbmNsdWRlcyhub2RlKSAmJiBub2RlICE9PSBzZXR0aW5ncyAmJiBtYXRjaGVzUmF0aW8obm9kZSkpOwogICAgY29uc3QgbW9kZWxTZXR0aW5ncyA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL05hbm8gQmFuYW5hfEltYWdlbi9pdS50ZXN0KGxhYmVscyhub2RlKSkgJiYgL1t4w5ddWzEtNF1cYi9pdS50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3Qgc2V0dGluZ3MgPSBjb250cm9scygpLmZpbmQobm9kZSA9PiAvXijEkGnhu4F1IGtp4buHbiBrw61jaCBob+G6oXQgY8OgaSDEkeG6t3R8U2V0dGluZ3MgdHJpZ2dlcnxHZW5lcmF0aW9uIHNldHRpbmdzKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSkKICAgICAgfHwgKG1vZGVsU2V0dGluZ3MubGVuZ3RoID09PSAxID8gbW9kZWxTZXR0aW5nc1swXSA6IG51bGwpOwogICAgbGV0IG9wZW5lZCA9IGZhbHNlOwogICAgaWYgKCFvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzKSB7CiAgICAgIHNldHRpbmdzLmNsaWNrKCk7CiAgICAgIG9wZW5lZCA9IHRydWU7CiAgICAgIGNvbnN0IGltYWdlID0gYXdhaXQgd2FpdEZvcigoKSA9PiBjb250cm9scygpLmZpbmQobm9kZSA9PiBub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpID09PSAicmFkaW8iICYmIC9eKEltYWdlfEjDrG5oIOG6o25ofOG6om5oKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSksIDMpOwogICAgICBpZiAoaW1hZ2UgJiYgIXNlbGVjdGVkKGltYWdlKSkgaW1hZ2UuY2xpY2soKTsKICAgIH0KICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCkgYXdhaXQgd2FpdEZvcigoKSA9PiBvcHRpb25zKCkubGVuZ3RoLCAzKTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiB0cmlnZ2VyLmxlbmd0aCA9PT0gMSkgdHJpZ2dlclswXS5jbGljaygpOwogICAgY29uc3Qgb3B0aW9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGlmICh0cmlnZ2VyLmxlbmd0aCA9PT0gMSAmJiBleGFjdC50ZXN0KGxhYmVscyh0cmlnZ2VyWzBdKSkgJiYgIW9wdGlvbnMoKS5sZW5ndGgpIHJldHVybiB0cmlnZ2VyWzBdOwogICAgICBjb25zdCBtYXRjaGVzID0gb3B0aW9ucygpOwogICAgICBjb25zdCByYWRpb3MgPSBtYXRjaGVzLmZpbHRlcihpc09wdGlvbik7CiAgICAgIHJldHVybiByYWRpb3MubGVuZ3RoID09PSAxID8gcmFkaW9zWzBdIDogbWF0Y2hlcy5sZW5ndGggPT09IDEgPyBtYXRjaGVzWzBdIDogbnVsbDsKICAgIH0sIDEyKTsKICAgIGlmICghb3B0aW9uKSB0aHJvdyBFcnJvcigiS2jDtG5nIGNo4buNbiDEkcaw4bujYyB04bu3IGzhu4cgIiArIHJhdGlvICsgIiB0cm9uZyBGbG93ICgiICsgKHNldHRpbmdzID8gIsSRw6MgdGjhuqV5IG7DunQgY8OgaSDEkeG6t3Qg4bqjbmgiIDogImNoxrBhIHRo4bqleSBuw7p0IGPDoGkgxJHhurd0IOG6o25oIikgKyAiKS4iKTsKICAgIGlmICghdHJpZ2dlci5pbmNsdWRlcyhvcHRpb24pICYmICFzZWxlY3RlZChvcHRpb24pKSBvcHRpb24uY2xpY2soKTsKICAgIGNvbnN0IHNpbmdsZSA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL15beMOXXVxzKjEkL2l1LnRlc3QoY2FwdGlvbihub2RlKSkpOwogICAgaWYgKHNpbmdsZS5sZW5ndGggPT09IDEgJiYgIXNlbGVjdGVkKHNpbmdsZVswXSkpIHNpbmdsZVswXS5jbGljaygpOwogICAgaWYgKG9wZW5lZCAmJiBvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzLmlzQ29ubmVjdGVkKSBzZXR0aW5ncy5jbGljaygpOwogIH0KICBhc3luYyBmdW5jdGlvbiBydW4oKSB7CiAgICBsZXQgYXBwbGllZFJhdGlvOwogICAgY29uc3QgYmF0Y2ggPSBhd2FpdCBzZW5kKHtvcDoiYmF0Y2gifSk7CiAgICBpZiAoYmF0Y2guZG9uZSkgcmV0dXJuOwogICAgY29uc3QgcXVldWVkID0gW107CiAgICBjb25zdCByZXN1bHRzID0gW107CiAgICBjb25zdCBjYXJkcyA9ICgpID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0iZ3JvdXAiXVthcmlhLWxhYmVsXScpXTsKICAgIGNvbnN0IG5vcm1hbGl6ZSA9IHRleHQgPT4gKHRleHQgfHwgIiIpLnJlcGxhY2UoL1xzKy9ndSwgIiAiKS50cmltKCk7CiAgICBjb25zdCBpbml0aWFsQ2FyZHMgPSBjYXJkcygpOwogICAgbGV0IG5ld2VzdEZpcnN0ID0gdHJ1ZTsKICAgIGxldCBsYXN0U3VibWl0ID0gMDsKICAgIGZ1bmN0aW9uIGNoZWNrRmxvd0Jsb2NrKCkgewogICAgICBjb25zdCBub3RpY2VzID0gWy4uLmNhcmRzKCksIC4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJhbGVydCJdLFtyb2xlPSJzdGF0dXMiXScpXTsKICAgICAgaWYgKG5vdGljZXMuc29tZShub2RlID0+IHZpc2libGUobm9kZSkgJiYKICAgICAgICAgIC9jaMO6bmcgdMO0aSBuaOG6rW4gdGjhuqV5IGPDsyBob+G6oXQgxJHhu5luZyBi4bqldCB0aMaw4budbmd8d2Ugbm90aWNlZCBzb21lIHVudXN1YWwgYWN0aXZpdHl8d2UgZGV0ZWN0ZWQgdW51c3VhbCBhY3Rpdml0eS9pLnRlc3Qobm9kZS5pbm5lclRleHQgfHwgIiIpKSkKICAgICAgICB0aHJvdyBFcnJvcigiRmxvdyBiw6FvIGhv4bqhdCDEkeG7mW5nIGLhuqV0IHRoxrDhu51uZy4gVuG6vSBUYXkgxJHDoyBk4burbmcgZ+G7rWkgcHJvbXB0LiBIw6N5IGtp4buDbSB0cmEgRmxvdyB0cm9uZyBDaHJvbWUgdHLGsOG7m2Mga2hpIHRo4butIGzhuqFpLiIpOwogICAgfQogICAgYXN5bmMgZnVuY3Rpb24gc3VibWl0U2NlbmUoam9iKSB7CiAgICAgIGNoZWNrRmxvd0Jsb2NrKCk7CiAgICAgIGNvbnN0IHtwcm9tcHR9ID0gam9iOwogICAgICBjb25zdCBhZ2VudCA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0iY2hlY2tib3giXSxpbnB1dFt0eXBlPSJjaGVja2JveCJdJyldLmZpbmQobm9kZSA9PiB2aXNpYmxlKG5vZGUpCiAgICAgICAgJiYgL14oVMOhYyBuaMOibnxBZ2VudCkkL2l1LnRlc3QoKG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgbm9kZS50ZXh0Q29udGVudCB8fCAiIikudHJpbSgpKQogICAgICAgICYmIChub2RlLmNoZWNrZWQgfHwgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtY2hlY2tlZCIpID09PSAidHJ1ZSIpKTsKICAgICAgaWYgKGFnZW50KSB7IGFnZW50LmNsaWNrKCk7IGF3YWl0IHNsZWVwKDMwMCk7IH0KICAgICAgbGV0IGZpZWxkID0gYXdhaXQgd2FpdEZvcihwcm9tcHRGaWVsZCwgMjUpOwogICAgICBpZiAoIWZpZWxkKSB0aHJvdyBFcnJvcigiS2jDtG5nIHjDoWMgxJHhu4tuaCDEkcaw4bujYyDDtCBwcm9tcHQgZHV5IG5o4bqldC4gTeG7nyB0cmFuZyB04bqhbyDhuqNuaCB0cm9uZyBk4buxIMOhbiBGbG93LiIpOwogICAgICBhd2FpdCBzZW5kKHtvcDoiYWN0aXZhdGUifSk7CiAgICAgIGNvbnN0IHJhdGlvID0gL15U4bqhbyBt4buZdCDhuqNuaCBtaW5oIGjhu41hIHThu7cgbOG7hyAoXGQrOlxkKykgL3UuZXhlYyhqb2IucHJvbXB0KT8uWzFdOwogICAgICBpZiAocmF0aW8gJiYgcmF0aW8gIT09IGFwcGxpZWRSYXRpbykgewogICAgICAgIGF3YWl0IHNlbGVjdFJhdGlvKHJhdGlvKTsKICAgICAgICBhcHBsaWVkUmF0aW8gPSByYXRpbzsKICAgICAgICBmaWVsZCA9IGF3YWl0IHdhaXRGb3IocHJvbXB0RmllbGQsIDEwKTsKICAgICAgICBpZiAoIWZpZWxkKSB0aHJvdyBFcnJvcigiS2jDtG5nIHRo4bqleSDDtCBwcm9tcHQgc2F1IGtoaSBjaOG7jW4gdOG7tyBs4buHLiIpOwogICAgICB9CiAgICAgIGZpZWxkLmZvY3VzKCk7CiAgICAgIGlmIChmaWVsZCBpbnN0YW5jZW9mIEhUTUxUZXh0QXJlYUVsZW1lbnQpIHsKICAgICAgICBPYmplY3QuZ2V0T3duUHJvcGVydHlEZXNjcmlwdG9yKEhUTUxUZXh0QXJlYUVsZW1lbnQucHJvdG90eXBlLCJ2YWx1ZSIpLnNldC5jYWxsKGZpZWxkLGpvYi5wcm9tcHQpOwogICAgICAgIGZpZWxkLmRpc3BhdGNoRXZlbnQobmV3IElucHV0RXZlbnQoImlucHV0Iix7YnViYmxlczp0cnVlLGRhdGE6am9iLnByb21wdCxpbnB1dFR5cGU6Imluc2VydFRleHQifSkpOwogICAgICB9IGVsc2UgewogICAgICAgIGNvbnN0IHNlbGVjdGlvbiA9IHdpbmRvdy5nZXRTZWxlY3Rpb24oKTsKICAgICAgICBjb25zdCByYW5nZSA9IGRvY3VtZW50LmNyZWF0ZVJhbmdlKCk7CiAgICAgICAgcmFuZ2Uuc2VsZWN0Tm9kZUNvbnRlbnRzKGZpZWxkKTsKICAgICAgICBzZWxlY3Rpb24ucmVtb3ZlQWxsUmFuZ2VzKCk7CiAgICAgICAgc2VsZWN0aW9uLmFkZFJhbmdlKHJhbmdlKTsKICAgICAgICBpZiAoIWRvY3VtZW50LmV4ZWNDb21tYW5kKCJpbnNlcnRUZXh0IixmYWxzZSxqb2IucHJvbXB0KSkgdGhyb3cgRXJyb3IoIktow7RuZyDEkWnhu4FuIMSRxrDhu6NjIHByb21wdCB0cm9uZyBGbG93LiIpOwogICAgICB9CiAgICAgIGF3YWl0IHNsZWVwKDUwMCk7CiAgICAgIGNvbnN0IGJ1dHRvbnMgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiYnV0dG9uLFtyb2xlPWJ1dHRvbl0iKV0uZmlsdGVyKGI9PnZpc2libGUoYikmJiFiLmRpc2FibGVkJiZiLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpIT09InRydWUiKTsKICAgICAgY29uc3Qgc3RhcnQgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gL14oQuG6r3QgxJHhuqd1IHThuqFvfFN0YXJ0IGdlbmVyYXRpbmd8U3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkpOwogICAgICBsZXQgZ2VuZXJhdGUgPSBzdGFydC5sZW5ndGggPT09IDEgPyBzdGFydCA6IGJ1dHRvbnMuZmlsdGVyKGI9Pi9eKEdlbmVyYXRlIEltYWdlfENyZWF0ZSBJbWFnZXxU4bqhbyBow6xuaCDhuqNuaHxU4bqhbyDhuqNuaCkkL2kudGVzdChjYXB0aW9uKGIpKSk7CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggPT09IDAgJiYgbWF0Y2hpbmcoL05hbm8gQmFuYW5hfEltYWdlbi9pKS5sZW5ndGggPT09IDEpCiAgICAgICAgZ2VuZXJhdGUgPSBidXR0b25zLmZpbHRlcihiPT4vXihHZW5lcmF0ZXxU4bqhbykkL2kudGVzdChjYXB0aW9uKGIpKSk7CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggPT09IDApIHsKICAgICAgICBjb25zdCBlZGl0b3IgPSBmaWVsZC5jbG9zZXN0KCJmb3JtIikgfHwgZmllbGQucGFyZW50RWxlbWVudDsKICAgICAgICBnZW5lcmF0ZSA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiAvXihhcnJvd19mb3J3YXJkfHNlbmR8Z+G7rWl8YuG6r3QgxJHhuqd1IHThuqFvfHN0YXJ0IGdlbmVyYXRpbmd8c3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkKICAgICAgICAgICYmIChlZGl0b3I/LmNvbnRhaW5zKGJ1dHRvbikgfHwgZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkuYm90dG9tIDw9IGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20KICAgICAgICAgICAgJiYgTWF0aC5hYnMoZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkucmlnaHQgLSBidXR0b24uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkucmlnaHQpIDwgMTAwKSk7CiAgICAgIH0KICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCAhPT0gMSkgdGhyb3cgRXJyb3IoIktow7RuZyBuaOG6rW4gZGnhu4duIMSRxrDhu6NjIG7DunQgdOG6oW8g4bqjbmguIEtp4buDbSB0cmEgY2jhur8gxJHhu5kg4bqibmgsIG1vZGVsIHbDoCBo4bqhbiBt4bupYyBGbG93LiIpOwogICAgICBjb25zdCBwcmlvciA9IG5ldyBTZXQoY2FyZHMoKSk7CiAgICAgIGNvbnN0IGVkaXRvckJveCA9IGZpZWxkLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICBjb25zdCBuZWFyYnlBcnJvd3MgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gewogICAgICAgIGNvbnN0IGJveCA9IGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgICByZXR1cm4gYm94LnRvcCA+PSBlZGl0b3JCb3guYm90dG9tIC0gOCAmJiBib3gudG9wIDw9IGVkaXRvckJveC5ib3R0b20gKyAxMDAKICAgICAgICAgICYmIE1hdGguYWJzKGJveC5yaWdodCAtIGVkaXRvckJveC5yaWdodCkgPCAxMDAKICAgICAgICAgICYmIChbLi4uYnV0dG9uLnF1ZXJ5U2VsZWN0b3JBbGwoIm1hdC1pY29uLFtkYXRhLWljb25dLFthcmlhLWhpZGRlbj10cnVlXSxpbWciKV0uc29tZShpY29uID0+CiAgICAgICAgICAgIC9eKGFycm93X2ZvcndhcmR8c2VuZCkkL2kudGVzdCgoaWNvbi5nZXRBdHRyaWJ1dGUoImRhdGEtaWNvbiIpIHx8IGljb24uZ2V0QXR0cmlidXRlKCJhbHQiKSB8fCBpY29uLnRleHRDb250ZW50IHx8ICIiKS50cmltKCkpKQogICAgICAgICAgICB8fCAvXihC4bqvdCDEkeG6p3UgdOG6oW98U3RhcnQgZ2VuZXJhdGluZ3xTdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKSk7CiAgICAgIH0pOwogICAgICBjb25zdCBzdWJtaXQgPSBuZWFyYnlBcnJvd3MubGVuZ3RoID09PSAxID8gbmVhcmJ5QXJyb3dzWzBdIDogZ2VuZXJhdGVbMF07CiAgICAgIHN1Ym1pdC5zY3JvbGxJbnRvVmlldyh7YmxvY2s6Im5lYXJlc3QiLCBpbmxpbmU6Im5lYXJlc3QifSk7CiAgICAgIGNvbnN0IGJveCA9IHN1Ym1pdC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgY29uc3QgeCA9IGJveC5sZWZ0ICsgYm94LndpZHRoIC8gMjsKICAgICAgY29uc3QgeSA9IGJveC50b3AgKyBib3guaGVpZ2h0IC8gMjsKICAgICAgaWYgKCFzdWJtaXQuY29udGFpbnMoZG9jdW1lbnQuZWxlbWVudEZyb21Qb2ludCh4LHkpKSkgdGhyb3cgRXJyb3IoIk7DunQgdOG6oW8g4bqjbmggxJFhbmcgYuG7iyBjaGUuIik7CiAgICAgIGF3YWl0IHNsZWVwKE1hdGgubWF4KDAsIDgwMDAgLSAoRGF0ZS5ub3coKSAtIGxhc3RTdWJtaXQpKSk7CiAgICAgIGNoZWNrRmxvd0Jsb2NrKCk7CiAgICAgIGxhc3RTdWJtaXQgPSBEYXRlLm5vdygpOwogICAgICBhd2FpdCBzZW5kKHtvcDoic3VibWl0Iiwgam9iOmpvYi5qb2IsIHgsIHl9KTsKICAgICAgY29uc3QgZmluZENhcmQgPSAoKSA9PiB7CiAgICAgICAgY29uc3QgbWF0Y2hlcyA9IGNhcmRzKCkuZmlsdGVyKGNhcmQgPT4gIXByaW9yLmhhcyhjYXJkKSAmJgogICAgICAgICAgKG5vcm1hbGl6ZShjYXJkLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpKSA9PT0gbm9ybWFsaXplKHByb21wdCkKICAgICAgICAgICB8fCBub3JtYWxpemUoY2FyZC50ZXh0Q29udGVudCkuaW5jbHVkZXMobm9ybWFsaXplKHByb21wdCkpKSk7CiAgICAgICAgcmV0dXJuIG1hdGNoZXMubGVuZ3RoID09PSAxID8gbWF0Y2hlc1swXSA6IG51bGw7CiAgICAgIH07CiAgICAgIGNvbnN0IGNhcmQgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsgY2hlY2tGbG93QmxvY2soKTsgcmV0dXJuIGZpbmRDYXJkKCk7IH0sIDIwKTsKICAgICAgaWYgKCFjYXJkKSB0aHJvdyBFcnJvcigiRmxvdyBjaMawYSB4w6FjIG5o4bqtbiDEkcOjIG5o4bqtbiBwcm9tcHQgY+G6o25oICIgKyAoam9iLmluZGV4ICsgMSkgKyAiLiIpOwogICAgICByZXR1cm4ge2pvYiwgY2FyZCwgZmluZENhcmR9OwogICAgfQogICAgZm9yIChjb25zdCBbb2Zmc2V0LCBwcm9tcHRdIG9mIGJhdGNoLnByb21wdHMuZW50cmllcygpKSB7CiAgICAgIGNvbnN0IGpvYiA9IHtqb2I6YmF0Y2guam9iLCBpbmRleDpiYXRjaC5pbmRleCArIG9mZnNldCwgcHJvbXB0fTsKICAgICAgY29uc3QgZW50cnkgPSBhd2FpdCBzdWJtaXRTY2VuZShqb2IpOwogICAgICBjb25zdCBjYXJkID0gZW50cnkuY2FyZDsKICAgICAgY29uc3QgY3VycmVudENhcmRzID0gY2FyZHMoKTsKICAgICAgbmV3ZXN0Rmlyc3QgJiY9IGN1cnJlbnRDYXJkcy5sZW5ndGggPT09IGluaXRpYWxDYXJkcy5sZW5ndGggKyBxdWV1ZWQubGVuZ3RoICsgMQogICAgICAgICYmIGN1cnJlbnRDYXJkc1swXSA9PT0gY2FyZDsKICAgICAgcXVldWVkLnB1c2goZW50cnkpOwogICAgICBhd2FpdCBzZW5kKHtvcDoicHJvZ3Jlc3MiLCBjb3VudDpxdWV1ZWQubGVuZ3RoLCBzdGFnZToic3VibWl0dGVkIn0pOwogICAgfQogICAgY29uc3QgY2xhaW1lZCA9IG5ldyBTZXQoKTsKICAgIGZvciAoY29uc3Qgb3JpZ2luYWwgb2YgcXVldWVkKSB7CiAgICAgIGNvbnN0IHtqb2J9ID0gb3JpZ2luYWw7CiAgICAgIGxldCBlbnRyeSA9IG9yaWdpbmFsOwogICAgICBsZXQgcmVzdWx0OwogICAgICBmb3IgKGxldCBhdHRlbXB0ID0gMDsgYXR0ZW1wdCA8IDM7IGF0dGVtcHQrKykgewogICAgICAgIGNvbnN0IHtjYXJkLCBmaW5kQ2FyZH0gPSBlbnRyeTsKICAgICAgICBjb25zdCBzdGF0dXMgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgICBjaGVja0Zsb3dCbG9jaygpOwogICAgICAgIGNvbnN0IGdyaWQgPSBjYXJkcygpOwogICAgICAgIGNvbnN0IHJlcGxhY2VtZW50ID0gYXR0ZW1wdCA9PT0gMCAmJiBuZXdlc3RGaXJzdCAmJiBpbml0aWFsQ2FyZHMubGVuZ3RoID09PSAwICYmIGdyaWQubGVuZ3RoID09PSBxdWV1ZWQubGVuZ3RoCiAgICAgICAgICAmJiBncmlkLmV2ZXJ5KGl0ZW0gPT4gcmVzdWx0SW1hZ2VzKGl0ZW0pLmxlbmd0aCA9PT0gMSkKICAgICAgICAgID8gZ3JpZFtxdWV1ZWQubGVuZ3RoIC0gMSAtIHF1ZXVlZC5maW5kSW5kZXgoZW50cnkgPT4gZW50cnkuY2FyZCA9PT0gY2FyZCldIDogbnVsbDsKICAgICAgICBjb25zdCBjdXJyZW50ID0gKGNhcmQuaXNDb25uZWN0ZWQgJiYgcmVzdWx0SW1hZ2VzKGNhcmQpLmxlbmd0aCA/IGNhcmQgOiBmaW5kQ2FyZCgpKSB8fCByZXBsYWNlbWVudCB8fCAoY2FyZC5pc0Nvbm5lY3RlZCA/IGNhcmQgOiBudWxsKTsKICAgICAgICBpZiAoIWN1cnJlbnQgfHwgY2xhaW1lZC5oYXMoY3VycmVudCkpIHJldHVybiBudWxsOwogICAgICAgIGNvbnN0IG1lc3NhZ2UgPSBub3JtYWxpemUoY3VycmVudC5pbm5lclRleHQpOwogICAgICAgIGlmICgvaOG6oW4gbeG7qWN8a2jDtG5nIMSR4bunIHTDrW4gZOG7pW5nfGNow61uaCBzw6FjaHx1bnVzdWFsIGFjdGl2aXR5fHBvbGljeSB2aW9sYXRpb258bm90IGVub3VnaCBjcmVkaXRzfHF1b3RhL2kudGVzdChtZXNzYWdlKSAmJiAhcmVzdWx0SW1hZ2VzKGN1cnJlbnQpLmxlbmd0aCkKICAgICAgICAgIHRocm93IEVycm9yKCJGbG93IHnDqnUgY+G6p3Uga2nhu4NtIHRyYSBjaMOtbmggc8OhY2ggaG/hurdjIGjhuqFuIG3hu6ljIOG7nyBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgICAgaWYgKC904bqhbyBxdcOhIG5oYW5ofHJlcXVlc3RpbmcgZ2VuZXJhdGlvbnMgdG9vIHF1aWNrbHl8cmF0ZSBsaW1pdHxnZW5lcmF0aW9uIGZhaWxlZHxraMO0bmcgdGjhu4MgdOG6oW98dOG6oW8g4bqjbmggdGjhuqV0IGLhuqFpL2kudGVzdChtZXNzYWdlKSAmJiAhcmVzdWx0SW1hZ2VzKGN1cnJlbnQpLmxlbmd0aCkKICAgICAgICAgIHJldHVybiB7ZmFpbGVkOnRydWV9OwogICAgICAgIGNvbnN0IHVybHMgPSBbLi4ubmV3IFNldChyZXN1bHRJbWFnZXMoY3VycmVudCkubWFwKGltYWdlID0+IGltYWdlLmN1cnJlbnRTcmMgfHwgaW1hZ2Uuc3JjKSldOwogICAgICAgIGlmICh1cmxzLmxlbmd0aCAhPT0gMSkgcmV0dXJuIG51bGw7CiAgICAgICAgY2xhaW1lZC5hZGQoY3VycmVudCk7CiAgICAgICAgcmV0dXJuIHtjYXJkOmN1cnJlbnR9OwogICAgICAgIH0sIDE1MCk7CiAgICAgICAgaWYgKHN0YXR1cz8uY2FyZCkgeyByZXN1bHQgPSBzdGF0dXMuY2FyZDsgYnJlYWs7IH0KICAgICAgICBpZiAoIXN0YXR1cz8uZmFpbGVkIHx8IGF0dGVtcHQgPT09IDIpIGJyZWFrOwogICAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnJlc3VsdHMubGVuZ3RoICsgMSwgc3RhZ2U6InJldHJ5In0pOwogICAgICAgIGF3YWl0IHNsZWVwKGF0dGVtcHQgPT09IDAgPyAzMDAwMCA6IDYwMDAwKTsKICAgICAgICBlbnRyeSA9IGF3YWl0IHN1Ym1pdFNjZW5lKGpvYik7CiAgICAgIH0KICAgICAgaWYgKCFyZXN1bHQpIHRocm93IEVycm9yKCJLaMO0bmcgbmjhuq1uIMSRxrDhu6NjIOG6o25oIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4gS2nhu4NtIHRyYSB0cuG6oW5nIHRow6FpIGPhuqNuaCB0cm9uZyBGbG93LiIpOwogICAgICByZXN1bHRzLnB1c2goe2pvYiwgcmVzdWx0fSk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnJlc3VsdHMubGVuZ3RofSk7CiAgICB9CiAgICBmb3IgKGNvbnN0IHtqb2IsIHJlc3VsdDpjYXJkfSBvZiByZXN1bHRzKSB7CiAgICAgIGlmICghY2FyZC5pc0Nvbm5lY3RlZCkgdGhyb3cgRXJyb3IoIlRo4bq7IOG6o25oIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIiDEkcOjIHRoYXkgxJHhu5VpLiIpOwogICAgICBjb25zdCBhY3RpdmF0ZUNvbnRyb2wgPSBhc3luYyAobm9kZSwgY2xpY2tzPTEpID0+IHsKICAgICAgICBhd2FpdCBzZW5kKHtvcDoiYWN0aXZhdGUifSk7CiAgICAgICAgbm9kZS5zY3JvbGxJbnRvVmlldyh7YmxvY2s6ImNlbnRlciIsIGlubGluZToibmVhcmVzdCJ9KTsKICAgICAgICBjb25zdCBib3ggPSBub2RlLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICAgIGNvbnN0IHggPSBib3gubGVmdCArIGJveC53aWR0aCAvIDIsIHkgPSBib3gudG9wICsgYm94LmhlaWdodCAvIDI7CiAgICAgICAgaWYgKCFub2RlLmNvbnRhaW5zKGRvY3VtZW50LmVsZW1lbnRGcm9tUG9pbnQoeCx5KSkpIHRocm93IEVycm9yKCJOw7p0IHThuqNpIOG6o25oIEZsb3cgxJFhbmcgYuG7iyBjaGUuIik7CiAgICAgICAgYXdhaXQgc2VuZCh7b3A6InN1Ym1pdCIsIGpvYjpqb2Iuam9iLCB4LCB5LCBjbGlja3N9KTsKICAgICAgfTsKICAgICAgY29uc3QgdGlsZSA9IHJlc3VsdEltYWdlcyhjYXJkKVswXTsKICAgICAgYXdhaXQgYWN0aXZhdGVDb250cm9sKHRpbGUsIDIpOwogICAgICBjb25zdCBkb3dubG9hZCA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gbWF0Y2hpbmcoL14oVOG6o2kgbuG7mWkgZHVuZyBuZ2hlIG5ow6xuIHh14buRbmd8RG93bmxvYWQgbWVkaWF8RG93bmxvYWQpJC9pdSlbMF0sIDEyKTsKICAgICAgaWYgKCFkb3dubG9hZCkgdGhyb3cgRXJyb3IoIktow7RuZyB0aOG6pXkgbsO6dCB04bqjaSDhuqNuaCBn4buRYyBj4bunYSBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIGF3YWl0IGFjdGl2YXRlQ29udHJvbChkb3dubG9hZCk7CiAgICAgIGNvbnN0IG9wdGlvbiA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJtZW51aXRlbSJdLGJ1dHRvbicpXQogICAgICAgIC5maW5kKGl0ZW0gPT4gdmlzaWJsZShpdGVtKSAmJiAvXjJLKD86XHN8JCkvaXUudGVzdChjYXB0aW9uKGl0ZW0pKSAmJiAhaXRlbS5kaXNhYmxlZCAmJiBpdGVtLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpICE9PSAidHJ1ZSIpLCA4KTsKICAgICAgaWYgKCFvcHRpb24pIHRocm93IEVycm9yKCJGbG93IGtow7RuZyBjw7MgdMO5eSBjaOG7jW4gdOG6o2kgMksgY2hvIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgYXdhaXQgc2VuZCh7b3A6ImFybTJrIixqb2I6am9iLmpvYixpbmRleDpqb2IuaW5kZXh9KTsKICAgICAgYXdhaXQgYWN0aXZhdGVDb250cm9sKG9wdGlvbik7CiAgICAgIGNvbnN0IGRlYWRsaW5lID0gRGF0ZS5ub3coKSArIDE1MDAwMDsKICAgICAgbGV0IHJlY2VpdmVkID0gZmFsc2U7CiAgICAgIHdoaWxlICghcmVjZWl2ZWQgJiYgRGF0ZS5ub3coKSA8IGRlYWRsaW5lKSB7CiAgICAgICAgcmVjZWl2ZWQgPSAoYXdhaXQgc2VuZCh7b3A6InRyYW5zZmVyMmsiLGpvYjpqb2Iuam9iLGluZGV4OmpvYi5pbmRleH0pKS5yZWNlaXZlZDsKICAgICAgICBpZiAoIXJlY2VpdmVkKSBhd2FpdCBzbGVlcCgxMDAwKTsKICAgICAgfQogICAgICBpZiAoIXJlY2VpdmVkKSB0aHJvdyBFcnJvcigiQ2hyb21lIGNoxrBhIHThuqNpIHhvbmcg4bqjbmggMksgY2hvIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgaWYgKGpvYi5pbmRleCAhPT0gYmF0Y2guaW5kZXggKyBiYXRjaC5wcm9tcHRzLmxlbmd0aCAtIDEpIHsKICAgICAgICBjb25zdCBiYWNrID0gYXdhaXQgd2FpdEZvcigoKSA9PiBtYXRjaGluZygvXihOw7p0IHF1YXkgbOG6oWkgxJHhu4MgcXVheSB24buBIHRyYW5nIHRyxrDhu5tjfEdvIGJhY2t8QmFjaykkL2l1KVswXSwgOCk7CiAgICAgICAgaWYgKCFiYWNrKSB0aHJvdyBFcnJvcigiS2jDtG5nIHRo4bqleSBuw7p0IHRy4bufIGzhuqFpIGzGsOG7m2kg4bqjbmggRmxvdy4iKTsKICAgICAgICBhd2FpdCBhY3RpdmF0ZUNvbnRyb2woYmFjayk7CiAgICAgICAgYXdhaXQgd2FpdEZvcigoKSA9PiBjYXJkcygpLmxlbmd0aCA+IDAsIDEyKTsKICAgICAgICBmb3IgKGNvbnN0IGVudHJ5IG9mIHJlc3VsdHMpIHsKICAgICAgICAgIGlmIChlbnRyeS5yZXN1bHQuaXNDb25uZWN0ZWQpIGNvbnRpbnVlOwogICAgICAgICAgY29uc3QgcmVwbGFjZW1lbnQgPSBuZXdlc3RGaXJzdCAmJiBpbml0aWFsQ2FyZHMubGVuZ3RoID09PSAwICYmIGNhcmRzKCkubGVuZ3RoID09PSBxdWV1ZWQubGVuZ3RoCiAgICAgICAgICAgID8gY2FyZHMoKVtxdWV1ZWQubGVuZ3RoIC0gMSAtIChlbnRyeS5qb2IuaW5kZXggLSBiYXRjaC5pbmRleCldIDogbnVsbDsKICAgICAgICAgIGlmIChyZXBsYWNlbWVudCkgZW50cnkucmVzdWx0ID0gcmVwbGFjZW1lbnQ7CiAgICAgICAgfQogICAgICB9CiAgICB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHdhaXRGb3IoZmluZCwgc2Vjb25kcykgewogICAgZm9yIChsZXQgaT0wO2k8c2Vjb25kcyoyO2krKykgewogICAgICBjb25zdCB2YWx1ZT1maW5kKCk7CiAgICAgIGlmICh2YWx1ZSkgcmV0dXJuIHZhbHVlOwogICAgICBhd2FpdCBzbGVlcCg1MDApOwogICAgfQogICAgcmV0dXJuIG51bGw7CiAgfQogIGNvbnN0IHByb2plY3RPcGVuID0gKCkgPT4gL1wvcHJvamVjdHM/XC9bXi9dKy8udGVzdChsb2NhdGlvbi5wYXRobmFtZSk7CiAgY29uc3QgbWF0Y2hpbmcgPSAocGF0dGVybikgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXRhYl0sYSIpXQogICAgLmZpbHRlcihpdGVtPT52aXNpYmxlKGl0ZW0pJiYhaXRlbS5kaXNhYmxlZCYmaXRlbS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSE9PSJ0cnVlIgogICAgICAmJiAocGF0dGVybi50ZXN0KGNhcHRpb24oaXRlbSkpIHx8IHBhdHRlcm4udGVzdCgoaXRlbS5pbm5lclRleHQgfHwgaXRlbS50ZXh0Q29udGVudCB8fCAiIikucmVwbGFjZSgvXHMrL2d1LCAiICIpLnRyaW0oKSkpKTsKICBhc3luYyBmdW5jdGlvbiBjb25uZWN0KCkgewogICAgY29uc3Qgc3RhdGUgPSBhd2FpdCBzZW5kKHBhaXJpbmcgPyB7b3A6InBhaXIiLHRva2VuOnBhaXJpbmdbMV0scG9ydDpOdW1iZXIocGFpcmluZ1syXSB8fCA3ODc3KX0gOiB7b3A6InJlc3VtZSJ9KTsKICAgIGlmIChzdGF0ZS5kb25lKSByZXR1cm47CiAgICBpZiAocGFpcmluZykgaGlzdG9yeS5yZXBsYWNlU3RhdGUoaGlzdG9yeS5zdGF0ZSwgIiIsIGxvY2F0aW9uLnBhdGhuYW1lICsgbG9jYXRpb24uc2VhcmNoKTsgCiAgICBpZiAoZG9jdW1lbnQucmVhZHlTdGF0ZSA9PT0gImxvYWRpbmciKSBhd2FpdCBuZXcgUHJvbWlzZShyZXNvbHZlPT5kb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJET01Db250ZW50TG9hZGVkIixyZXNvbHZlLHtvbmNlOnRydWV9KSk7CiAgICBpZiAoIXByb2plY3RPcGVuKCkpIHsKICAgICAgaWYgKCFwYWlyaW5nKSB0aHJvdyBFcnJvcigiRmxvdyB24bqrbiDhu58gdHJhbmcgY2jhu6cgc2F1IGtoaSB04bqhbyBk4buxIMOhbi4gS2nhu4NtIHRyYSDEkcSDbmcgbmjhuq1wIHLhu5NpIGNo4bqheSBs4bqhaS4iKTsKICAgICAgY29uc3QgY3JlYXRlID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgY2hvaWNlcyA9IG1hdGNoaW5nKC9eKD86KD86YWRkKD86X2NpcmNsZXxfYm94KT98cGx1c3xcKylccyopPyg/Ok5ldyBwcm9qZWN0fENyZWF0ZSBwcm9qZWN0fENyZWF0ZSBuZXcgcHJvamVjdHxE4buxIMOhbiBt4bubaXxU4bqhbyBk4buxIMOhbnxU4bqhbyBk4buxIMOhbiBt4bubaSkkL2l1KTsKICAgICAgICBjb25zdCBjb250cm9scyA9IGNob2ljZXMuZmlsdGVyKGl0ZW0gPT4gIWNob2ljZXMuc29tZShvdGhlciA9PiBvdGhlciAhPT0gaXRlbSAmJiBpdGVtLmNvbnRhaW5zKG90aGVyKSkpOwogICAgICAgIHJldHVybiBjb250cm9sc1swXSB8fCBudWxsOwogICAgICB9LDM1KTsKICAgICAgaWYgKCFjcmVhdGUpIHRocm93IEVycm9yKCJLaMO0bmcgdMOsbSB0aOG6pXkgbsO6dCB04bqhbyBk4buxIMOhbiBGbG93LiBLaeG7g20gdHJhIENocm9tZSDEkcOjIMSRxINuZyBuaOG6rXAgdsOgIHhlbSB0cmFuZyBGbG93IGPDsyB0aMO0bmcgYsOhbyBnw6wuIik7CiAgICAgIGNyZWF0ZS5jbGljaygpOwogICAgICBpZiAoIWF3YWl0IHdhaXRGb3IocHJvamVjdE9wZW4sNDUpKSB0aHJvdyBFcnJvcigiRmxvdyBjaMawYSBt4bufIGThu7Egw6FuIG3hu5tpIHNhdSBraGkgYuG6pW0gdOG6oW8uIik7CiAgICB9CiAgICBpZiAoIWF3YWl0IHdhaXRGb3IoKCk9PmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJ3RleHRhcmVhLFtjb250ZW50ZWRpdGFibGU9InRydWUiXScpLDQ1KSkKICAgICAgdGhyb3cgRXJyb3IoIkThu7Egw6FuIG3hu5tpIGNoxrBhIGhp4buHbiDDtCBwcm9tcHQuIik7CiAgICAKICAgIGF3YWl0IHJ1bigpOwogIH0KICBjb25uZWN0KCkuY2F0Y2goYXN5bmMgZXJyb3IgPT4gewogICAgdHJ5IHthd2FpdCBzZW5kKHtvcDoiZXJyb3IiLG1lc3NhZ2U6ZXJyb3IubWVzc2FnZX0pO30gY2F0Y2ggeyBjb25zb2xlLndhcm4oIlbhur0gVGF5OiBnaMOpcCBu4buRaSBob+G6t2MgZ+G7rWkgdHLhuqFuZyB0aMOhaSB0aOG6pXQgYuG6oWkuIiwgZXJyb3IubWVzc2FnZSk7IH0KICB9KS5maW5hbGx5KCgpPT57d2luZG93Ll9fdmVUYXlSdW5uaW5nPWZhbHNlO30pOwp9KSgpOwp9IGVsc2UgewooKCkgPT4gewogIGNvbnN0IG1hdGNoID0gL14jdmV0YXk9KFthLWYwLTldezMyfSktKFxkezQsNX0pJC8uZXhlYyhsb2NhdGlvbi5oYXNoKTsKICBjb25zdCBrZXkgPSAidmV0YXktbXVzZS10YXNrIjsKICBpZiAoIW1hdGNoICYmICFzZXNzaW9uU3RvcmFnZS5nZXRJdGVtKGtleSkpIHJldHVybjsKICBjb25zdCBwYXVzZSA9IG1zID0+IG5ldyBQcm9taXNlKHJlc29sdmUgPT4gc2V0VGltZW91dChyZXNvbHZlLCBtcykpOwogIGNvbnN0IGxhYmVsID0gbm9kZSA9PiAobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSB8fCBub2RlLnRpdGxlIHx8IG5vZGUudGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBjb25zdCB2aXNpYmxlID0gbm9kZSA9PiBub2RlLmdldENsaWVudFJlY3RzKCkubGVuZ3RoICYmICFub2RlLmRpc2FibGVkICYmIG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikgIT09ICJ0cnVlIjsKICBjb25zdCBjb250cm9scyA9IHBhdHRlcm4gPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dIildLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgcGF0dGVybi50ZXN0KGxhYmVsKG5vZGUpKSk7CiAgY29uc3Qgc2VuZCA9IGFzeW5jIG1lc3NhZ2UgPT4gewogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBjaHJvbWUucnVudGltZS5zZW5kTWVzc2FnZShtZXNzYWdlKTsKICAgIGlmICghcmVzcG9uc2U/Lm9rKSB0aHJvdyBFcnJvcihyZXNwb25zZT8uZXJyb3IgfHwgIk3huqV0IGvhur90IG7hu5FpIHbhu5tpIFbhur0gVGF5LiIpOwogICAgcmV0dXJuIHJlc3BvbnNlLnJlc3VsdDsKICB9OwogIGFzeW5jIGZ1bmN0aW9uIHdhaXRGb3IoZmluZCwgbXMsIG1lc3NhZ2UpIHsKICAgIGNvbnN0IGVuZCA9IERhdGUubm93KCkgKyBtczsKICAgIHdoaWxlIChEYXRlLm5vdygpIDwgZW5kKSB7CiAgICAgIGNvbnN0IHZhbHVlID0gZmluZCgpOwogICAgICBpZiAodmFsdWUpIHJldHVybiB2YWx1ZTsKICAgICAgYXdhaXQgcGF1c2UoNTAwKTsKICAgIH0KICAgIHRocm93IG1lc3NhZ2UgaW5zdGFuY2VvZiBFcnJvciA/IG1lc3NhZ2UgOiBFcnJvcihtZXNzYWdlKTsKICB9CiAgZnVuY3Rpb24gc2F2ZShzdGF0ZSkgewogICAgc2Vzc2lvblN0b3JhZ2Uuc2V0SXRlbShrZXksIEpTT04uc3RyaW5naWZ5KHN0YXRlKSk7CiAgfQogIGNsYXNzIFNjZW5lRXJyb3IgZXh0ZW5kcyBFcnJvciB7fQogIGNvbnN0IHRhZ0ZvciA9IChqb2IsIGluZGV4LCBhdHRlbXB0KSA9PgogICAgInZldGF5IiArIGpvYi5qb2Iuc2xpY2UoMCwgMTIpICsgImMiICsgU3RyaW5nKGluZGV4ICsgMSkucGFkU3RhcnQoNSwgIjAiKSArICJyIiArIGF0dGVtcHQ7CiAgYXN5bmMgZnVuY3Rpb24gYWR2YW5jZShzdGF0ZSkgewogICAgY29uc3QgbmV4dCA9IGF3YWl0IHNlbmQoe29wOiJuZXh0In0pOwogICAgaWYgKG5leHQuZG9uZSkgewogICAgICBzZXNzaW9uU3RvcmFnZS5yZW1vdmVJdGVtKGtleSk7CiAgICB9IGVsc2UgaWYgKG5leHQuaW5kZXggPCBzdGF0ZS5iYXRjaEVuZCkgewogICAgICBzYXZlKHtwaGFzZToibGlicmFyeSIsIGpvYjpuZXh0LmpvYiwgaW5kZXg6bmV4dC5pbmRleCwKICAgICAgICAgICAgdGFnOnRhZ0ZvcihuZXh0LCBuZXh0LmluZGV4LCAwKSwgYmF0Y2hFbmQ6c3RhdGUuYmF0Y2hFbmQsIGNvbmZpcm1lZDpzdGF0ZS5jb25maXJtZWQsCiAgICAgICAgICAgIGJhdGNoU3RhcnQ6c3RhdGUuYmF0Y2hTdGFydCwgY2hhdEltYWdlczpzdGF0ZS5jaGF0SW1hZ2VzfSk7CiAgICAgIGxvY2F0aW9uLmFzc2lnbigiaHR0cHM6Ly9tdXNlLmFpL2xpYnJhcnkvbWVkaWE/dmlldz1pbWFnZXMiKTsKICAgIH0gZWxzZSB7CiAgICAgIHNhdmUoe3BoYXNlOiJjb21wb3NlIn0pOwogICAgICBsb2NhdGlvbi5hc3NpZ24oImh0dHBzOi8vbXVzZS5haS8iKTsKICAgIH0KICB9CiAgYXN5bmMgZnVuY3Rpb24gY2hhdFVwbG9hZChzdGF0ZSwgam9iKSB7CiAgICAvLyBEdW9uZyBkdSBwaG9uZyBkdW5nIGNodW5nOiB0YWkgdGhhbmcgYW5oIHZ1YSB0YW8gbmdheSB0cm9uZyBraHVuZyBjaGF0CiAgICAvLyAoZGlhIGNoaSB0aHUgZHVvYyBvIHJlc3VsdCgpKSB2YSBndWkgY2hvIFZlIFRheSBxdWEgd29ya2VyIChvcCAiaW1hZ2UiKSwKICAgIC8vIGJvIHF1YSBjYSBUaHUgdmllbiBsYW4gYnVvYyBDaHJvbWUgdGFpIHh1b25nLiBEdW5nIGtoaSBraG9uZyB0aW0gdGhheSBvCiAgICAvLyBhbmggdHJvbmcgVGh1IHZpZW4sIGhvYWMga2hpIGFuaCB0YWkgeHVvbmcga2hvbmcgdmFvIGR1bmcgdGh1IG11YwogICAgLy8gVmVUYXktTXVzZSAoYXBwIGJhbyAiQ2hyb21lIGtob25nIGx1dSBhbmggMksgZHVuZyB0aHUgbXVjIGN1YSB0YWMgdnUiIC0KICAgIC8vIGRhIGdhcCBvIGpvYiA4NGRlN2EzNiBraGkgbW90IHRpZW4gaWNoIHRhaSBhbmgga2hhYyBjaGllbSBxdXllbiBkYXQgdGVuKS4KICAgIGNvbnN0IG9mZnNldCA9IHN0YXRlLmluZGV4IC0gKHN0YXRlLmJhdGNoU3RhcnQgPz8gc3RhdGUuaW5kZXgpOwogICAgY29uc3QgdXJsID0gc3RhdGUuY2hhdEltYWdlcz8uW29mZnNldF07CiAgICBpZiAoIXVybCkgdGhyb3cgRXJyb3IoIktow7RuZyBjw7Mg4bqjbmggZOG7sSBwaMOybmcgdHJvbmcga2h1bmcgY2hhdC4iKTsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgZmV0Y2godXJsLCB7Y3JlZGVudGlhbHM6ImluY2x1ZGUifSk7CiAgICBpZiAoIXJlc3BvbnNlLm9rKSB0aHJvdyBFcnJvcigiS2jDtG5nIHThuqNpIMSRxrDhu6NjIOG6o25oIHRyb25nIGtodW5nIGNoYXQuIik7CiAgICBjb25zdCBibG9iID0gYXdhaXQgcmVzcG9uc2UuYmxvYigpOwogICAgaWYgKCFibG9iLnR5cGUuc3RhcnRzV2l0aCgiaW1hZ2UvIikgfHwgYmxvYi5zaXplID4gMjQqMTAyNCoxMDI0KQogICAgICB0aHJvdyBFcnJvcigi4bqibmggdHJvbmcga2h1bmcgY2hhdCBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgIGNvbnN0IGRhdGEgPSBhd2FpdCBuZXcgUHJvbWlzZSgocmVzb2x2ZSwgcmVqZWN0KSA9PiB7CiAgICAgIGNvbnN0IHJlYWRlciA9IG5ldyBGaWxlUmVhZGVyKCk7CiAgICAgIHJlYWRlci5vbmxvYWQgPSAoKSA9PiByZXNvbHZlKHJlYWRlci5yZXN1bHQpOwogICAgICByZWFkZXIub25lcnJvciA9IHJlamVjdDsKICAgICAgcmVhZGVyLnJlYWRBc0RhdGFVUkwoYmxvYik7CiAgICB9KTsKICAgIGlmICghL15kYXRhOmltYWdlXC8ocG5nfHdlYnB8anBlZyk7YmFzZTY0LC8udGVzdChkYXRhKSkKICAgICAgdGhyb3cgRXJyb3IoIuG6om5oIHRyb25nIGtodW5nIGNoYXQga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICBhd2FpdCBzZW5kKHtvcDoicHJvZ3Jlc3MiLCBjb3VudDpqb2IuaW5kZXggKyAxLCBzdGFnZToiZ2VuZXJhdGVkIn0pOwogICAgYXdhaXQgc2VuZCh7b3A6ImltYWdlIiwgam9iOmpvYi5qb2IsIGluZGV4OmpvYi5pbmRleCwgZGF0YX0pOwogICAgYXdhaXQgYWR2YW5jZShzdGF0ZSk7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHJ1bigpIHsKICAgIGNvbnN0IHN0YXRlID0gbWF0Y2ggPyB7cGhhc2U6ImNvbXBvc2UifSA6IEpTT04ucGFyc2Uoc2Vzc2lvblN0b3JhZ2UuZ2V0SXRlbShrZXkpKTsKICAgIGNvbnN0IGpvYiA9IGF3YWl0IHNlbmQobWF0Y2ggPyB7b3A6InBhaXIiLCB0b2tlbjptYXRjaFsxXSwgcG9ydDpOdW1iZXIobWF0Y2hbMl0pfSA6IHtvcDoicmVzdW1lIn0pOwogICAgaWYgKGpvYi5kb25lKSB7CiAgICAgIHNlc3Npb25TdG9yYWdlLnJlbW92ZUl0ZW0oa2V5KTsKICAgICAgcmV0dXJuOwogICAgfQogICAgaWYgKGpvYi5wcm92aWRlciAhPT0gIm11c2UiKSB0aHJvdyBFcnJvcigiVMOhYyB24bulIGhp4buHbiB04bqhaSBraMO0bmcgdGh14buZYyBNdXNlLiIpOwogICAgaWYgKCFtYXRjaCAmJiBzdGF0ZS5waGFzZSAhPT0gImNvbXBvc2UiICYmIChzdGF0ZS5qb2IgIT09IGpvYi5qb2IgfHwgc3RhdGUuaW5kZXggIT09IGpvYi5pbmRleCkpCiAgICAgIHRocm93IEVycm9yKCJD4bqjbmggTXVzZSDEkcOjIHRoYXkgxJHhu5VpLiBIw6N5IGLhuq90IMSR4bqndSBs4bqhaSB04burIFbhur0gVGF5LiIpOwogICAgaWYgKG1hdGNoKSB7CiAgICAgIHNhdmUoc3RhdGUpOwogICAgICBoaXN0b3J5LnJlcGxhY2VTdGF0ZShoaXN0b3J5LnN0YXRlLCAiIiwgbG9jYXRpb24ucGF0aG5hbWUgKyBsb2NhdGlvbi5zZWFyY2gpOwogICAgfQogICAgaWYgKHN0YXRlLnBoYXNlID09PSAiY29tcG9zZSIpIHsKICAgICAgaWYgKHN0YXRlLmRlbGF5KSB7CiAgICAgICAgYXdhaXQgc2VuZCh7b3A6InByb2dyZXNzIiwgY291bnQ6am9iLmluZGV4ICsgMSwgc3RhZ2U6c3RhdGUuYXR0ZW1wdCA/ICJyZXRyeSIgOiAiYmF0Y2gifSk7CiAgICAgICAgYXdhaXQgcGF1c2Uoc3RhdGUuZGVsYXkpOwogICAgICB9CiAgICAgIGNvbnN0IGZpZWxkID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgZWRpdG9ycyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPXRydWVdLFtjb250ZW50ZWRpdGFibGU9cGxhaW50ZXh0LW9ubHldLGlucHV0Om5vdChbdHlwZV0pLGlucHV0W3R5cGU9dGV4dF0sW3JvbGU9dGV4dGJveF0nKV0KICAgICAgICAgIC5maWx0ZXIobm9kZSA9PiB2aXNpYmxlKG5vZGUpICYmICFub2RlLnJlYWRPbmx5ICYmICFub2RlLmNsb3Nlc3QoJ1tpbmVydF0sW2FyaWEtaGlkZGVuPXRydWVdLFtyb2xlPXNlYXJjaF0sYXNpZGUsbmF2LFtyb2xlPWRpYWxvZ10nKSkKICAgICAgICAgIC5maWx0ZXIobm9kZSA9PiBub2RlLm1hdGNoZXMoJ3RleHRhcmVhLGlucHV0JykgfHwgbm9kZS5pc0NvbnRlbnRFZGl0YWJsZSk7CiAgICAgICAgY29uc3QgYm90dG9tID0gZWRpdG9ycy5maWx0ZXIobm9kZSA9PiBub2RlLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLmJvdHRvbSA+IGlubmVySGVpZ2h0ICogMC43KTsKICAgICAgICBjb25zdCBuYW1lZCA9IGJvdHRvbS5maWx0ZXIobm9kZSA9PiBbJ2FyaWEtbGFiZWwnLCdwbGFjZWhvbGRlcicsJ2RhdGEtcGxhY2Vob2xkZXInLCd0aXRsZSddCiAgICAgICAgICAuc29tZShhdHRyID0+IC9eKE5o4bqvbiB0aW58TWVzc2FnZXxTZW5kIGEgbWVzc2FnZXxUeXBlIGEgbWVzc2FnZSkoPzokfFsu4oCmXSkvaXUudGVzdCgobm9kZS5nZXRBdHRyaWJ1dGUoYXR0cikgfHwgJycpLnRyaW0oKSkpKTsKICAgICAgICBjb25zdCBjaG9pY2VzID0gbmFtZWQubGVuZ3RoID8gbmFtZWQgOiBib3R0b207CiAgICAgICAgcmV0dXJuIGNob2ljZXMuZmlsdGVyKG5vZGUgPT4gIWNob2ljZXMuc29tZShvdGhlciA9PiBvdGhlciAhPT0gbm9kZSAmJiBub2RlLmNvbnRhaW5zKG90aGVyKSkpCiAgICAgICAgICAucmVkdWNlKChiZXN0LCBub2RlKSA9PiAhYmVzdCB8fCBub2RlLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLndpZHRoID4gYmVzdC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS53aWR0aCA/IG5vZGUgOiBiZXN0LCBudWxsKTsKICAgICAgfSwgMzAwMDAsICJLaMO0bmcgdGjhuqV5IMO0IE5o4bqvbiB0aW4uIEjDo3kgxJHEg25nIG5o4bqtcCBNdXNlIHRyb25nIENocm9tZSBy4buTaSB0aOG7rSBs4bqhaS4iKTsKICAgICAgaWYgKChmaWVsZC52YWx1ZSA/PyBmaWVsZC5pbm5lclRleHQgPz8gZmllbGQudGV4dENvbnRlbnQpLnRyaW0oKSkgdGhyb3cgRXJyb3IoIsOUIE5o4bqvbiB0aW4gTXVzZSDEkWFuZyBjw7MgYuG6o24gbmjDoXAuIik7CiAgICAgIGNvbnN0IGJhdGNoID0gYXdhaXQgc2VuZCh7b3A6ImJhdGNoIn0pOwogICAgICBpZiAoYmF0Y2guZG9uZSB8fCBiYXRjaC5qb2IgIT09IGpvYi5qb2IgfHwgYmF0Y2guaW5kZXggIT09IGpvYi5pbmRleCkKICAgICAgICB0aHJvdyBFcnJvcigiRGFuaCBzw6FjaCBj4bqjbmggTXVzZSDEkcOjIHRoYXkgxJHhu5VpLiIpOwogICAgICBjb25zdCBhdHRlbXB0ID0gc3RhdGUuYXR0ZW1wdCB8fCAwOwogICAgICBjb25zdCBwcm9tcHRzID0gYmF0Y2gucHJvbXB0cy5zbGljZSgwLCBhdHRlbXB0ID8gMSA6IDQpOwogICAgICBjb25zdCBiYXRjaEVuZCA9IGF0dGVtcHQgPyBzdGF0ZS5iYXRjaEVuZCA6IGpvYi5pbmRleCArIHByb21wdHMubGVuZ3RoOwogICAgICBjb25zdCB0YWcgPSB0YWdGb3Ioam9iLCBqb2IuaW5kZXgsIGF0dGVtcHQpOwogICAgICBjb25zdCBmaWxlcyA9IHByb21wdHMubWFwKChkZXNjcmlwdGlvbiwgb2Zmc2V0KSA9PiB7CiAgICAgICAgY29uc3QgY29kZSA9IHRhZ0Zvcihqb2IsIGpvYi5pbmRleCArIG9mZnNldCwgYXR0ZW1wdCk7CiAgICAgICAgcmV0dXJuIG5ldyBGaWxlKFtjb2RlICsgIlxuIiArIGRlc2NyaXB0aW9uXSwgY29kZSArICIudHh0Iiwge3R5cGU6InRleHQvcGxhaW4ifSk7CiAgICAgIH0pOwogICAgICBjb25zdCBzY2VuZXMgPSBwcm9tcHRzLm1hcCgoZGVzY3JpcHRpb24sIG9mZnNldCkgPT4gewogICAgICAgIGNvbnN0IGluZGV4ID0gam9iLmluZGV4ICsgb2Zmc2V0OwogICAgICAgIHJldHVybiAiQ+G6o25oICIgKyAoaW5kZXggKyAxKSArICIgfCAiICsgdGFnRm9yKGpvYiwgaW5kZXgsIGF0dGVtcHQpICsgIlxuIiArIGRlc2NyaXB0aW9uOwogICAgICB9KTsKICAgICAgY29uc3QgcHJvbXB0ID0gIlThuqFvIMSRw7puZyAiICsgc2NlbmVzLmxlbmd0aCArICIg4bqjbmggdHJvbmcgbMaw4bujdCBuw6B5LCBt4buXaSBj4bqjbmggYsOqbiBkxrDhu5tpIG3hu5l0IOG6o25oIHJpw6puZy4gIiArCiAgICAgICAgIk7hur91IGPDsyB0aOG7gywgY2jhuqF5IGPDoWMgY+G6o25oIHNvbmcgc29uZy4gTMawdSB04bqldCBj4bqjIHbDoG8gVGjGsCB2aeG7h24gaMOsbmgg4bqjbmguICIgKwogICAgICAgICLEkOG6t3QgdMOqbiBt4buXaSDhuqNuaCBi4bqvdCDEkeG6p3UgYuG6sW5nIG3DoyBj4bqjbmggdMawxqFuZyDhu6luZywgZ2nhu68gbmd1ecOqbiBtw6MuIEtow7RuZyDEkcawYSBtw6MgdsOgbyBu4buZaSBkdW5nIOG6o25oLiAiICsKICAgICAgICAiQ8OhYyB04buHcCBUWFQgxJHDrW5oIGvDqG0gbMOgIGLhuqNuIHNhbyBj4bunYSBjw6FjIHByb21wdCBiw6puIGTGsOG7m2ksIGNo4buJIHThuqFvIG3hu5l0IOG6o25oIGNobyBt4buXaSBtw6MuICIgKwogICAgICAgICJLaGkgeG9uZywgYsOhbyBIb8OgbiB04bqldCB2w6AgbGnhu4d0IGvDqiBjw6FjIG3DoyDEkcOjIHThuqFvLiBNw6MgxJHhuqd1IMSR4bujdDogIiArIHRhZyArICJcblxuIiArCiAgICAgICAgc2NlbmVzLmpvaW4oIlxuXG4iKTsKICAgICAgZmllbGQuZm9jdXMoKTsKICAgICAgY29uc3QgZGF0YSA9IG5ldyBEYXRhVHJhbnNmZXIoKTsKICAgICAgZmlsZXMuZm9yRWFjaChmaWxlID0+IGRhdGEuaXRlbXMuYWRkKGZpbGUpKTsKICAgICAgZmllbGQuZGlzcGF0Y2hFdmVudChuZXcgQ2xpcGJvYXJkRXZlbnQoInBhc3RlIiwge2NsaXBib2FyZERhdGE6ZGF0YSwgYnViYmxlczp0cnVlLCBjYW5jZWxhYmxlOnRydWV9KSk7CiAgICAgIGNvbnN0IGNvbXBvc2VyVGV4dCA9ICgpID0+IHsKICAgICAgICBjb25zdCBuYW1lc0luID0gcm9vdCA9PiB7CiAgICAgICAgICBpZiAoIXJvb3QpIHJldHVybiBmYWxzZTsKICAgICAgICAgIGNvbnN0IGNvbnRlbnQgPSByb290LnRleHRDb250ZW50IHx8ICIiOwogICAgICAgICAgY29uc3QgbGFiZWxzID0gWy4uLnJvb3QucXVlcnlTZWxlY3RvckFsbCgnW2FyaWEtbGFiZWxdLFt0aXRsZV0nKV0ubWFwKGVsID0+CiAgICAgICAgICAgIChlbC5nZXRBdHRyaWJ1dGUoJ2FyaWEtbGFiZWwnKSB8fCAnJykgKyAnICcgKyAoZWwudGl0bGUgfHwgJycpKS5qb2luKCIgIik7CiAgICAgICAgICByZXR1cm4gZmlsZXMuZXZlcnkoZmlsZSA9PiAoY29udGVudCArICIgIiArIGxhYmVscykuaW5jbHVkZXMoZmlsZS5uYW1lKSk7CiAgICAgICAgfTsKICAgICAgICBsZXQgbm9kZSA9IGZpZWxkOwogICAgICAgIGZvciAobGV0IGxldmVsID0gMDsgbm9kZSAmJiBsZXZlbCA8IDEwOyBsZXZlbCsrLCBub2RlID0gbm9kZS5wYXJlbnRFbGVtZW50KSB7CiAgICAgICAgICBpZiAobmFtZXNJbihub2RlKSkgcmV0dXJuIHRydWU7CiAgICAgICAgfQogICAgICAgIC8vIENoaXAgdGVwIGNvIHRoZSByZW5kZXIgbmdvYWkgY2F5IGNoYSBjdWEgbyBuaGFwIChwb3J0YWwpLiBUZW4gdGVwIGxhCiAgICAgICAgLy8gZHV5IG5oYXQgdGhlbyB0dW5nIGpvYi9jYW5oL2xhbiB0aHUgbmVuIHF1ZXQgY2EgdHJhbmcga2hvbmcgdGhlIG5oYW0KICAgICAgICAvLyBzYW5nIHRpbiBuaGFuIGN1LiBCYW4gY3UgY2hpIHF1ZXQgNyB0YW5nIGNoYSwgYm8gc290IGNoaXAsIGtoaWVuCiAgICAgICAgLy8gZmFsbGJhY2sgZ2FuIGxhaSBxdWEgbyBjaG9uIHRlcCB2YSBtb2kgdGVwIGJpIGRpbmgga2VtIDIgbGFuLgogICAgICAgIHJldHVybiBuYW1lc0luKGRvY3VtZW50LmJvZHkpOwogICAgICB9OwogICAgICBsZXQgYXR0YWNoZWQgPSBmYWxzZTsKICAgICAgdHJ5IHsgYXdhaXQgd2FpdEZvcihjb21wb3NlclRleHQsIDgwMDAsICJwYXN0ZS1pZ25vcmVkIik7IGF0dGFjaGVkID0gdHJ1ZTsgfSBjYXRjaCB7fQogICAgICBpZiAoIWF0dGFjaGVkKSB7CiAgICAgICAgLy8gTXVzZSBjbyB0aGUgYm8gcXVhIHN1IGtpZW4gcGFzdGUgZ2lhIGxhcDogdGh1IGxhaSBiYW5nIG8gY2hvbiB0ZXAgY28gc2FuLgogICAgICAgIGNvbnN0IGZpbGVJbnB1dCA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdpbnB1dFt0eXBlPSJmaWxlIl0nKV0uZmlsdGVyKG5vZGUgPT4gIW5vZGUuZGlzYWJsZWQpWzBdOwogICAgICAgIGlmIChmaWxlSW5wdXQpIHsKICAgICAgICAgIGNvbnN0IHRyYW5zZmVyID0gbmV3IERhdGFUcmFuc2ZlcigpOwogICAgICAgICAgZmlsZXMuZm9yRWFjaChmaWxlID0+IHRyYW5zZmVyLml0ZW1zLmFkZChmaWxlKSk7CiAgICAgICAgICBmaWxlSW5wdXQuZmlsZXMgPSB0cmFuc2Zlci5maWxlczsKICAgICAgICAgIGZpbGVJbnB1dC5kaXNwYXRjaEV2ZW50KG5ldyBFdmVudCgiaW5wdXQiLCB7YnViYmxlczp0cnVlfSkpOwogICAgICAgICAgZmlsZUlucHV0LmRpc3BhdGNoRXZlbnQobmV3IEV2ZW50KCJjaGFuZ2UiLCB7YnViYmxlczp0cnVlfSkpOwogICAgICAgIH0KICAgICAgICBhd2FpdCB3YWl0Rm9yKGNvbXBvc2VyVGV4dCwgMjAwMDAsICJNdXNlIGNoxrBhIG5o4bqtbiDEkeG7pyAiICsgZmlsZXMubGVuZ3RoICsgIiB04buHcC4gxJDDoyBk4burbmcgdHLGsOG7m2Mga2hpIGfhu61pLiIpOwogICAgICB9CiAgICAgIGlmIChmaWVsZCBpbnN0YW5jZW9mIEhUTUxUZXh0QXJlYUVsZW1lbnQgfHwgZmllbGQgaW5zdGFuY2VvZiBIVE1MSW5wdXRFbGVtZW50KSB7CiAgICAgICAgT2JqZWN0LmdldE93blByb3BlcnR5RGVzY3JpcHRvcihPYmplY3QuZ2V0UHJvdG90eXBlT2YoZmllbGQpLCAidmFsdWUiKS5zZXQuY2FsbChmaWVsZCwgcHJvbXB0KTsKICAgICAgICBmaWVsZC5kaXNwYXRjaEV2ZW50KG5ldyBJbnB1dEV2ZW50KCJpbnB1dCIsIHtidWJibGVzOnRydWUsIGRhdGE6cHJvbXB0fSkpOwogICAgICB9IGVsc2UgaWYgKCFkb2N1bWVudC5leGVjQ29tbWFuZCgiaW5zZXJ0VGV4dCIsIGZhbHNlLCBwcm9tcHQpKSB7CiAgICAgICAgdGhyb3cgRXJyb3IoIktow7RuZyDEkWnhu4FuIMSRxrDhu6NjIGzhu4duaCB04bqhbyDhuqNuaCBNdXNlLiIpOwogICAgICB9CiAgICAgIGNvbnN0IGJ1dHRvbiA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IGJ1dHRvbnMgPSBjb250cm9scygvXihH4butaXxTZW5kfFNlbmQgbWVzc2FnZSkkL2l1KTsKICAgICAgICByZXR1cm4gYnV0dG9ucy5sZW5ndGggPT09IDEgPyBidXR0b25zWzBdIDogbnVsbDsKICAgICAgfSwgODAwMCwgIktow7RuZyB0w6xtIMSRxrDhu6NjIG7DunQgR+G7rWkgY+G7p2EgTXVzZS4iKTsKICAgICAgY29uc3QgbmV4dCA9IHtwaGFzZToiYXdhaXQiLCBqb2I6am9iLmpvYiwgaW5kZXg6am9iLmluZGV4LCB0YWcsIGF0dGVtcHQsIGJhdGNoRW5kLCBzdWJtaXR0ZWRBdDpEYXRlLm5vdygpLCBwcm9tcHRUYWlsOnByb21wdC5zbGljZSgtNDgpfTsKICAgICAgc2F2ZShuZXh0KTsKICAgICAgYnV0dG9uLmNsaWNrKCk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OmpvYi5pbmRleCArIHByb21wdHMubGVuZ3RoLCBzdGFnZToic3VibWl0dGVkIn0pOwogICAgICBhd2FpdCB3YWl0Rm9yKCgpID0+ICEoZmllbGQudmFsdWUgPz8gZmllbGQudGV4dENvbnRlbnQpLmluY2x1ZGVzKHRhZyksIDEyMDAwLCAiTXVzZSBjaMawYSBuaOG6rW4gcHJvbXB0LiIpOwogICAgICByZXR1cm4gYXdhaXQgcmVzdWx0KG5leHQsIGpvYik7CiAgICB9CiAgICBpZiAoc3RhdGUucGhhc2UgPT09ICJhd2FpdCIpIHJldHVybiBhd2FpdCByZXN1bHQoc3RhdGUsIGpvYik7CiAgICBpZiAoc3RhdGUucGhhc2UgIT09ICJsaWJyYXJ5IiAmJiBzdGF0ZS5waGFzZSAhPT0gImRvd25sb2FkIikgdGhyb3cgRXJyb3IoIlRy4bqhbmcgdGjDoWkgTXVzZSBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgIGNvbnN0IGZpbGUgPSBuZXcgVVJMKGxvY2F0aW9uLmhyZWYpLnNlYXJjaFBhcmFtcy5nZXQoImZpbGUiKTsKICAgIGlmICghZmlsZSB8fCAhZmlsZS5zcGxpdCgiLyIpLnBvcCgpLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMoc3RhdGUudGFnKSkgewogICAgICBsZXQgaXRlbSA9IG51bGw7CiAgICAgIHRyeSB7CiAgICAgICAgaXRlbSA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IG1hdGNoZXMgPSBub2RlID0+IFsiYWx0IiwgInRpdGxlIiwgImFyaWEtbGFiZWwiLCAiaHJlZiIsICJkYXRhLWZpbGVuYW1lIiwgImRhdGEtZmlsZS1uYW1lIl0KICAgICAgICAgIC5zb21lKGF0dHIgPT4gKG5vZGUuZ2V0QXR0cmlidXRlKGF0dHIpIHx8ICIiKS50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHN0YXRlLnRhZykpOwogICAgICAgIGNvbnN0IGVsaWdpYmxlID0gbm9kZSA9PiB2aXNpYmxlKG5vZGUpICYmICFub2RlLmNsb3Nlc3QoJ1tyb2xlPSJkaWFsb2ciXSxkaWFsb2cnKTsKICAgICAgICBjb25zdCB0aHVtYm5haWxzID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImltZyIpXS5maWx0ZXIobm9kZSA9PiBlbGlnaWJsZShub2RlKSAmJiBtYXRjaGVzKG5vZGUpKTsKICAgICAgICBpZiAodGh1bWJuYWlscy5sZW5ndGggPT09IDEpIHJldHVybiB0aHVtYm5haWxzWzBdOwogICAgICAgIGNvbnN0IG5hbWVkID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1t0aXRsZV0sW2FyaWEtbGFiZWxdLGFbaHJlZl0sW2RhdGEtZmlsZW5hbWVdLFtkYXRhLWZpbGUtbmFtZV0nKV0KICAgICAgICAgIC5maWx0ZXIobm9kZSA9PiBlbGlnaWJsZShub2RlKSAmJiBtYXRjaGVzKG5vZGUpKTsKICAgICAgICBjb25zdCBjb250cm9scyA9IFsuLi5uZXcgU2V0KG5hbWVkLm1hcChub2RlID0+IG5vZGUuY2xvc2VzdCgnYnV0dG9uLFtyb2xlPSJidXR0b24iXSxhJykgfHwgbm9kZSkpXTsKICAgICAgICBpZiAoY29udHJvbHMubGVuZ3RoID09PSAxKSByZXR1cm4gY29udHJvbHNbMF07CiAgICAgICAgY29uc3Qgd2Fsa2VyID0gZG9jdW1lbnQuY3JlYXRlVHJlZVdhbGtlcihkb2N1bWVudC5ib2R5LCBOb2RlRmlsdGVyLlNIT1dfVEVYVCk7CiAgICAgICAgY29uc3QgZm91bmQgPSBuZXcgU2V0KCk7CiAgICAgICAgd2hpbGUgKHdhbGtlci5uZXh0Tm9kZSgpKSB7CiAgICAgICAgICBjb25zdCB0ZXh0ID0gd2Fsa2VyLmN1cnJlbnROb2RlOwogICAgICAgICAgaWYgKCF0ZXh0Lm5vZGVWYWx1ZS50cmltKCkudG9Mb3dlckNhc2UoKS5zdGFydHNXaXRoKHN0YXRlLnRhZykpIGNvbnRpbnVlOwogICAgICAgICAgY29uc3QgY2FyZCA9IHRleHQucGFyZW50RWxlbWVudC5jbG9zZXN0KCdidXR0b24sW3JvbGU9ImJ1dHRvbiJdLGEnKTsKICAgICAgICAgIGlmIChjYXJkICYmIGVsaWdpYmxlKGNhcmQpKSBmb3VuZC5hZGQoY2FyZCk7CiAgICAgICAgfQogICAgICAgIGlmIChmb3VuZC5zaXplID09PSAxKSByZXR1cm4gWy4uLmZvdW5kXVswXTsKICAgICAgICBmb3IgKGNvbnN0IGdyaWQgb2YgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnbWFpbiwgW3JvbGU9Im1haW4iXSwgbWFpbiBkaXYsIFtyb2xlPSJtYWluIl0gZGl2JykpIHsKICAgICAgICAgIGlmIChncmlkLmNsaWVudEhlaWdodCA+IDE1MCAmJiBncmlkLnNjcm9sbEhlaWdodCA+IGdyaWQuY2xpZW50SGVpZ2h0ICYmCiAgICAgICAgICAgICAgL14oYXV0b3xzY3JvbGwpJC8udGVzdChnZXRDb21wdXRlZFN0eWxlKGdyaWQpLm92ZXJmbG93WSkpIHsKICAgICAgICAgICAgY29uc3QgYXRFbmQgPSBncmlkLnNjcm9sbFRvcCArIGdyaWQuY2xpZW50SGVpZ2h0ID49IGdyaWQuc2Nyb2xsSGVpZ2h0IC0gMjsKICAgICAgICAgICAgZ3JpZC5zY3JvbGxUb3AgPSBhdEVuZCA/IDAgOiBncmlkLnNjcm9sbFRvcCArIE1hdGguZmxvb3IoZ3JpZC5jbGllbnRIZWlnaHQgKiAwLjgpOwogICAgICAgICAgfQogICAgICAgIH0KICAgICAgICByZXR1cm4gbnVsbDsKICAgICAgICB9LCA2MDAwMCwgbmV3IFNjZW5lRXJyb3IoIktow7RuZyB0w6xtIMSRxrDhu6NjIMSRw7puZyDhuqNuaCBj4bunYSBj4bqjbmggdHJvbmcgTXVzZS4iKSk7CiAgICAgIH0gY2F0Y2ggKGVycm9yKSB7CiAgICAgICAgLy8gTHVvaSBUaHUgdmllbiBraG9uZyBoZSBsbyB0ZW4gdGVwIChkYSB0aGF5IG8gam9iIGZlNzdlYzI5OiBhbmggdGFvCiAgICAgICAgLy8gZHVuZyB0ZW4gbWEgdGltIDIgdm9uZyB2YW4ga2hvbmcgdGhheSBvIG5hbykgLT4gZHVuZyBkdSBwaG9uZyBjaGF0LgogICAgICAgIHRyeSB7IGF3YWl0IGNoYXRVcGxvYWQoc3RhdGUsIGpvYik7IHJldHVybjsgfQogICAgICAgIGNhdGNoIHsgdGhyb3cgZXJyb3I7IH0KICAgICAgfQogICAgICBpdGVtLmNsaWNrKCk7CiAgICB9CiAgICB0cnkgewogICAgICBjb25zdCBidXR0b24gPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgY29uc3QgZmlsZSA9IG5ldyBVUkwobG9jYXRpb24uaHJlZikuc2VhcmNoUGFyYW1zLmdldCgiZmlsZSIpOwogICAgICBpZiAoIWZpbGU/LnNwbGl0KCIvIikucG9wKCkudG9Mb3dlckNhc2UoKS5pbmNsdWRlcyhzdGF0ZS50YWcpKSByZXR1cm4gbnVsbDsKICAgICAgY29uc3QgYnV0dG9ucyA9IGNvbnRyb2xzKC9eKFThuqNpIHh14buRbmd8RG93bmxvYWQpJC9pdSkuZmlsdGVyKG5vZGUgPT4gbm9kZS5jbG9zZXN0KCdbcm9sZT0iZGlhbG9nIl0sZGlhbG9nJykpOwogICAgICByZXR1cm4gYnV0dG9ucy5sZW5ndGggPT09IDEgPyBidXR0b25zWzBdIDogbnVsbDsKICAgIH0sIDEyMDAwLCBuZXcgU2NlbmVFcnJvcigiS2jDtG5nIG3hu58gxJHGsOG7o2Mg4bqjbmggxJHDum5nIG3DoyBj4bqjbmguIikpOwogICAgY29uc3QgZmlsZW5hbWUgPSBuZXcgVVJMKGxvY2F0aW9uLmhyZWYpLnNlYXJjaFBhcmFtcy5nZXQoImZpbGUiKS5zcGxpdCgiLyIpLnBvcCgpOwogICAgYXdhaXQgc2VuZCh7b3A6InByb2dyZXNzIiwgY291bnQ6am9iLmluZGV4ICsgMSwgc3RhZ2U6ImdlbmVyYXRlZCJ9KTsKICAgIGF3YWl0IHNlbmQoe29wOiJhcm1NdXNlIiwgam9iOmpvYi5qb2IsIGluZGV4OmpvYi5pbmRleCwgZmlsZW5hbWV9KTsKICAgIGJ1dHRvbi5jbGljaygpOwogICAgY29uc3QgZGVhZGxpbmUgPSBEYXRlLm5vdygpICsgOTAwMDA7CiAgICB3aGlsZSAoRGF0ZS5ub3coKSA8IGRlYWRsaW5lKSB7CiAgICAgIGlmICgoYXdhaXQgc2VuZCh7b3A6InRyYW5zZmVyTXVzZSIsIGpvYjpqb2Iuam9iLCBpbmRleDpqb2IuaW5kZXh9KSkucmVjZWl2ZWQpIHsKICAgICAgICBhd2FpdCBhZHZhbmNlKHN0YXRlKTsKICAgICAgICByZXR1cm47CiAgICAgIH0KICAgICAgYXdhaXQgcGF1c2UoODAwKTsKICAgIH0KICAgICAgdGhyb3cgbmV3IFNjZW5lRXJyb3IoIkNocm9tZSBjaMawYSB04bqjaSB4b25nIOG6o25oIE11c2UuIik7CiAgICB9IGNhdGNoIChlcnJvcikgewogICAgICAvLyBDYSBnaWFpIGRvYW4gbW8gYW5oICsgdGFpIHh1b25nIGRldSBjbyBkdSBwaG9uZyBjaGF0OiBrZSBjYSBraGkgdGltCiAgICAgIC8vIHRoYXkgbyBhbmggbmh1bmcgQ2hyb21lIGx1dSBhbmggc2FpIHRodSBtdWMgKHRpZW4gaWNoIGtoYWMgY2hpZW0KICAgICAgLy8gcXV5ZW4gZGF0IHRlbiB0ZXApIHRoaSB2YW4gbGF5IGR1b2MgYW5oIHZlIGNobyBWZSBUYXkuCiAgICAgIHRyeSB7IGF3YWl0IGNoYXRVcGxvYWQoc3RhdGUsIGpvYik7IHJldHVybjsgfQogICAgICBjYXRjaCB7IHRocm93IGVycm9yOyB9CiAgICB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHJlc3VsdChzdGF0ZSwgam9iKSB7CiAgICBsZXQgY29uZmlybWVkID0gZmFsc2U7CiAgICBjb25zdCBleHBlY3RlZENvdW50ID0gc3RhdGUuYXR0ZW1wdCA/IDEgOiBzdGF0ZS5iYXRjaEVuZCAtIGpvYi5pbmRleDsKICAgIGNvbnN0IHRhZ3MgPSBBcnJheS5mcm9tKHtsZW5ndGg6ZXhwZWN0ZWRDb3VudH0sIChfLCBvZmZzZXQpID0+CiAgICAgIHRhZ0Zvcihqb2IsIGpvYi5pbmRleCArIG9mZnNldCwgc3RhdGUuYXR0ZW1wdCB8fCAwKSk7CiAgICBjb25zdCBkZWFkbGluZSA9IERhdGUubm93KCkgKyAyNDUwMDA7CiAgICB3aGlsZSAoRGF0ZS5ub3coKSA8IGRlYWRsaW5lKSB7CiAgICAgIGNvbnN0IGZ1bGwgPSBkb2N1bWVudC5ib2R5Py5pbm5lclRleHQgfHwgIiI7CiAgICAgIC8vIENoaSB4ZXQgcGhhbiB0cmFuZyBTQVUgdGluIG5oYW4gbWluaCB2dWEgZ3VpLiBCYW4gdGhhbiBwcm9tcHQgY28gY2F1CiAgICAgIC8vICJLaGkgeG9uZywgYmFvIEhvYW4gdGF0IHZhIGxpZXQga2UgY2FjIG1hIGRhIHRhbyI6IG5ldSBxdWV0IGNhIHByb21wdAogICAgICAvLyB0aGkgdGllbiBpY2ggc2UgbmhhbiBuaGFtIGxhIE11c2UgZGEgdGFvIHhvbmcgbmdheSBraGkgdnVhIGJhbSBHdWksCiAgICAgIC8vIG5oYXkgc2FuZyBUaHUgdmllbiBxdWEgc29tIHRyb25nIGtoaSBjaHVhIGNvIGFuaCBuYW8gKGJhbiAxLjMuMzUgbWFjCiAgICAgIC8vIGxvaSBuYXkgdmkgbWF1IHF1ZXQgcXVhIHJvbmcgImhvYW4gdGF0LnswLDgwfWRhIHRhbyIpLgogICAgICBsZXQgc3RhcnQgPSAwOwogICAgICBpZiAoc3RhdGUucHJvbXB0VGFpbCkgewogICAgICAgIGNvbnN0IHRhaWxBdCA9IGZ1bGwubGFzdEluZGV4T2Yoc3RhdGUucHJvbXB0VGFpbCk7CiAgICAgICAgaWYgKHRhaWxBdCA+PSAwKSBzdGFydCA9IHRhaWxBdCArIHN0YXRlLnByb21wdFRhaWwubGVuZ3RoOwogICAgICB9CiAgICAgIGNvbnN0IHRleHQgPSBmdWxsLnNsaWNlKHN0YXJ0KTsKICAgICAgY29uc3QgYXQgPSB0ZXh0Lmxhc3RJbmRleE9mKHN0YXRlLnRhZyk7CiAgICAgIGlmIChhdCA+PSAwKSB7CiAgICAgICAgY29uc3QgbmVhcmJ5ID0gdGV4dC5zbGljZShNYXRoLm1heCgwLCBhdCAtIDYwMDApLCBhdCArIDE2MDAwKTsKICAgICAgICBjb25zdCBhZnRlciA9IHRleHQuc2xpY2UoYXQpOwogICAgICAgIGlmICgvaOG6v3QgKD86aOG6oW4gbeG7qWN8bMaw4bujdCl8xJHDoyDEkeG6oXQgZ2nhu5tpIGjhuqFufCg/OndlZWtseXx1c2FnZXxyYXRlKSBsaW1pdHxxdW90YSBleGNlZWRlZC9pdS50ZXN0KGFmdGVyKSkKICAgICAgICAgIHRocm93IEVycm9yKCJNdXNlIGLDoW8gZ2nhu5tpIGjhuqFuIHPhu60gZOG7pW5nLiDEkMOjIGThu6tuZyBn4butaSB0aMOqbSBwcm9tcHQuIik7CiAgICAgICAgY29uc3QgY29tcGxldGUgPSAvaG/DoG4gdOG6pXRccypb4oCU4oCTOi1dXHMqxJHDoyB04bqhbyAoPzp4b25nfMSRw7puZyl8xJHDoyB04bqhbyB4b25nIHRvw6BuIGLhu5l8xJHDoyB04bqhbyAoPzp4b25nfMSRw7puZykgKD86dG/DoG4gYuG7mXx04bqldCBj4bqjfFxkKyDhuqNuaCl8Y29tcGxldGVkXHMqW+KAlOKAkzotXVxzKig/OmFsbHxnZW5lcmF0ZWQpL2l1LnRlc3QobmVhcmJ5KTsKICAgICAgICAvLyBUYW8gYW5oIHRoYXQga2hvbmcgYmFvIGdpbyB4b25nIHRyb25nIGR1b2kgMjUgZ2lheTsga2hvYSB0aG9pIGdpYW4KICAgICAgICAvLyBuYXkgY2hhbiBub3QgbW9pIG5oYW4gbmhhbSBjb24gc290IGxhaSB0dSBub2kgZHVuZyBwaGFuIGNoaWV1LgogICAgICAgIGNvbnN0IHNldHRsZWQgPSAhc3RhdGUuc3VibWl0dGVkQXQgfHwgRGF0ZS5ub3coKSAtIHN0YXRlLnN1Ym1pdHRlZEF0ID49IDI1MDAwOwogICAgICAgIGlmIChzZXR0bGVkICYmIGNvbXBsZXRlICYmIHRhZ3MuZXZlcnkodGFnID0+IG5lYXJieS5pbmNsdWRlcyh0YWcpKSkgewogICAgICAgICAgY29uZmlybWVkID0gdHJ1ZTsKICAgICAgICAgIGJyZWFrOwogICAgICAgIH0KICAgICAgICBpZiAoL2NoxrBhIMSRxrDhu6NjIHThuqFvIHRyb25nIGzGsOG7o3QgbsOgeXxj4bqnbiBzYW5nIGzGsOG7o3QgdGnhur9wIHRoZW98bm90IGdlbmVyYXRlZCBpbiB0aGlzIGJhdGNoL2l1LnRlc3QoYWZ0ZXIpKSBicmVhazsKICAgICAgICBpZiAoL2tow7RuZyB0aOG7gyB04bqhbyDhuqNuaHx04bqhbyDhuqNuaCB0aOG6pXQgYuG6oWl8aW1hZ2UgZ2VuZXJhdGlvbiBmYWlsZWQvaXUudGVzdChhZnRlcikpIGJyZWFrOwogICAgICB9CiAgICAgIGF3YWl0IHBhdXNlKDEwMDApOwogICAgfQogICAgLy8gVGh1IGx1b24gZGlhIGNoaSBjYWMgYW5oIHZ1YSB0YW8gbmdheSB0cm9uZyBraHVuZyBjaGF0IChjaGkga2hpIE11c2UKICAgIC8vIGRhIGJhbyBob2FuIHRhdCB0aGF0KS4gTmV1IHNhbmcgVGh1IHZpZW4gbWEga2hvbmcgdGltIHRoYXkgbyBhbmggdGhpCiAgICAvLyBleHRlbnNpb24gc2UgdGFpIHRoYW5nIGNhYyBhbmggbmF5IHZlLCBraG9uZyBwaHUgdGh1b2MgbHVvaSBUaHUgdmllbi4KICAgIGxldCBjaGF0SW1hZ2VzID0gW107CiAgICBpZiAoY29uZmlybWVkKSB7CiAgICAgIC8vIE5lbyB2YW8gY3VvaSBwcm9tcHQgdnVhIGd1aTogY2hpIGxheSBhbmggbmFtIFNBVSBtb2MgbmF5IHRyb25nIHRyYW5nLAogICAgICAvLyB0cmFuaCBuaGFtIGFuaCBjdWEgY2FjIGx1b3QgY2hhdCBjdSAoY3VuZyBuYW0gdHJvbmcgZG9hbiBjaGF0IGNoaW5oKS4KICAgICAgbGV0IGFuY2hvciA9IG51bGw7CiAgICAgIGlmIChzdGF0ZS5wcm9tcHRUYWlsKSB7CiAgICAgICAgY29uc3Qgd2Fsa2VyID0gZG9jdW1lbnQuY3JlYXRlVHJlZVdhbGtlcihkb2N1bWVudC5ib2R5LCBOb2RlRmlsdGVyLlNIT1dfVEVYVCk7CiAgICAgICAgd2hpbGUgKHdhbGtlci5uZXh0Tm9kZSgpKSB7CiAgICAgICAgICBpZiAod2Fsa2VyLmN1cnJlbnROb2RlLm5vZGVWYWx1ZS5pbmNsdWRlcyhzdGF0ZS5wcm9tcHRUYWlsKSkgYW5jaG9yID0gd2Fsa2VyLmN1cnJlbnROb2RlOwogICAgICAgIH0KICAgICAgfQogICAgICBjb25zdCBzZWVuID0gbmV3IFNldCgpOwogICAgICBjb25zdCBmb3VuZCA9IFtdOwogICAgICBmb3IgKGNvbnN0IGltZyBvZiBkb2N1bWVudC5pbWFnZXMpIHsKICAgICAgICBpZiAoaW1nLm5hdHVyYWxXaWR0aCA8IDIwMCB8fCBpbWcubmF0dXJhbEhlaWdodCA8IDIwMCkgY29udGludWU7CiAgICAgICAgaWYgKGFuY2hvciAmJiAhKGFuY2hvci5jb21wYXJlRG9jdW1lbnRQb3NpdGlvbihpbWcpICYgTm9kZS5ET0NVTUVOVF9QT1NJVElPTl9GT0xMT1dJTkcpKSBjb250aW51ZTsKICAgICAgICBsZXQgdXJsID0gaW1nLmN1cnJlbnRTcmMgfHwgaW1nLnNyYyB8fCAiIjsKICAgICAgICBjb25zdCBocmVmID0gaW1nLmNsb3Nlc3QoImEiKT8uaHJlZiB8fCAiIjsKICAgICAgICBpZiAoL1wuKHdlYnB8cG5nfGpwZT9nKShcP3wjfCQpL2l1LnRlc3QoaHJlZikpIHVybCA9IGhyZWY7CiAgICAgICAgaWYgKCEvXmh0dHBzOi8udGVzdCh1cmwpIHx8IC9hdmF0YXIvaXUudGVzdCh1cmwpIHx8IHNlZW4uaGFzKHVybCkpIGNvbnRpbnVlOwogICAgICAgIHNlZW4uYWRkKHVybCk7CiAgICAgICAgZm91bmQucHVzaCh1cmwpOwogICAgICB9CiAgICAgIGlmIChmb3VuZC5sZW5ndGggPj0gZXhwZWN0ZWRDb3VudCkgY2hhdEltYWdlcyA9IGZvdW5kLnNsaWNlKC1leHBlY3RlZENvdW50KTsKICAgIH0KICAgIC8vIE11c2UgY2FuIHNhdmUgaW1hZ2VzIGV2ZW4gd2hlbiB0aGUgY29tcGxldGlvbiBtZXNzYWdlIGlzIGFic2VudC4KICAgIHNhdmUoey4uLnN0YXRlLCBwaGFzZToibGlicmFyeSIsIGNvbmZpcm1lZCwgYmF0Y2hTdGFydDpqb2IuaW5kZXgsIGNoYXRJbWFnZXN9KTsKICAgIGxvY2F0aW9uLmFzc2lnbigiaHR0cHM6Ly9tdXNlLmFpL2xpYnJhcnkvbWVkaWE/dmlldz1pbWFnZXMiKTsKICB9CiAgcnVuKCkuY2F0Y2goYXN5bmMgZXJyb3IgPT4gewogICAgY29uc3Qgc3RhdGUgPSBKU09OLnBhcnNlKHNlc3Npb25TdG9yYWdlLmdldEl0ZW0oa2V5KSB8fCAibnVsbCIpOwogICAgdHJ5IHsKICAgICAgaWYgKGVycm9yIGluc3RhbmNlb2YgU2NlbmVFcnJvciAmJiBzdGF0ZT8uam9iKSB7CiAgICAgICAgaWYgKHN0YXRlLnBoYXNlID09PSAibGlicmFyeSIgJiYgIXN0YXRlLmxpYnJhcnlSZXRyaWVkKSB7CiAgICAgICAgICBzYXZlKHsuLi5zdGF0ZSwgbGlicmFyeVJldHJpZWQ6dHJ1ZX0pOwogICAgICAgICAgbG9jYXRpb24ucmVwbGFjZSgiaHR0cHM6Ly9tdXNlLmFpL2xpYnJhcnkvbWVkaWE/dmlldz1pbWFnZXMiKTsKICAgICAgICAgIHJldHVybjsKICAgICAgICB9CiAgICAgICAgaWYgKHN0YXRlLmNvbmZpcm1lZCB8fCAvdOG6o2kgeG9uZ3xt4bufIMSRxrDhu6NjIOG6o25oL2l1LnRlc3QoZXJyb3IubWVzc2FnZSkpCiAgICAgICAgICB0aHJvdyBFcnJvcigiS2jDtG5nIGzhuqV5IMSRxrDhu6NjIOG6o25oIGPDsyBz4bq1biBj4bunYSBj4bqjbmggIiArIChzdGF0ZS5pbmRleCArIDEpICsgIiAoIiArIHN0YXRlLnRhZyArICIpLiAiICsgZXJyb3IubWVzc2FnZSk7CiAgICAgICAgaWYgKCFzdGF0ZS5hdHRlbXB0KSB7CiAgICAgICAgICBzYXZlKHtwaGFzZToiY29tcG9zZSIsIGF0dGVtcHQ6MSwgZGVsYXk6MjAwMDAsIGJhdGNoRW5kOnN0YXRlLmJhdGNoRW5kfSk7CiAgICAgICAgICBsb2NhdGlvbi5hc3NpZ24oImh0dHBzOi8vbXVzZS5haS8iKTsKICAgICAgICB9IGVsc2UgewogICAgICAgICAgYXdhaXQgc2VuZCh7b3A6InNraXBNdXNlIiwgam9iOnN0YXRlLmpvYiwgaW5kZXg6c3RhdGUuaW5kZXh9KTsKICAgICAgICAgIGF3YWl0IGFkdmFuY2Uoc3RhdGUpOwogICAgICAgIH0KICAgICAgICByZXR1cm47CiAgICAgIH0KICAgICAgdGhyb3cgZXJyb3I7CiAgICB9IGNhdGNoIChmYWlsdXJlKSB7CiAgICAgIHNlc3Npb25TdG9yYWdlLnJlbW92ZUl0ZW0oa2V5KTsKICAgICAgdHJ5IHsgYXdhaXQgc2VuZCh7b3A6ImVycm9yIiwgbWVzc2FnZTpmYWlsdXJlLm1lc3NhZ2V9KTsgfSBjYXRjaCB7fQogICAgfQogIH0pOwp9KSgpOwoKfQo="
}

def sync_chrome_bundle(allow_muse=False):
    folder = DATA / "chrome-extension"
    try:
        remote = json.loads(patch_read(PATCH_ROOT + "main/companion.json", 16384))
    except (OSError, ValueError):
        remote = BUNDLED_EXTENSION
    ref = remote if version_tuple(remote["version"]) > version_tuple(BUNDLED_EXTENSION["version"]) else BUNDLED_EXTENSION
    revision = ref["commit"]
    names = {"manifest.json", "worker.js", "flow.js", "popup.html"}
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or set(ref["files"]) != names:
        raise ValueError("Danh sách tệp tiện ích không hợp lệ.")
    manifest_path = folder / "manifest.json"
    old = json.loads(manifest_path.read_bytes()) if manifest_path.is_file() else None
    if old and version_tuple(old["version"]) >= version_tuple(ref["version"]):
        return old["version"], False
    content = {}
    for name, expected in ref["files"].items():
        if ref is BUNDLED_EXTENSION:
            data = base64.b64decode(BUNDLED_EXTENSION_FILES[name], validate=True)
        else:
            data = patch_read(PATCH_ROOT + revision + "/chrome-extension/" + name, 1024 * 1024)
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{40}", expected) or blob != expected:
            raise ValueError(f"Tệp tiện ích {name} không khớp mã kiểm tra GitHub.")
        content[name] = data
    manifest = json.loads(content["manifest.json"])
    if manifest["version"] != ref["version"] or manifest["manifest_version"] != 3:
        raise ValueError("Phiên bản tiện ích tải về không khớp.")
    if old:
        permissions = ("permissions", "host_permissions", "optional_permissions", "optional_host_permissions")
        scopes = lambda m: sorted(pattern for script in m.get("content_scripts", []) for pattern in script.get("matches", []))
        muse_upgrade = (
            allow_muse and ref is BUNDLED_EXTENSION
            and scopes(manifest) == sorted([*scopes(old), "https://muse.ai/*"])
            and sorted(manifest["host_permissions"]) == sorted([*old["host_permissions"], "https://muse.ai/*"])
        )
        if (old["name"] != manifest["name"] or (scopes(old) != scopes(manifest) and not muse_upgrade)
            or any(sorted(old.get(key, [])) != sorted(manifest.get(key, []))
                   for key in permissions
                   if not (key == "host_permissions" and muse_upgrade)
                   if not (key == "permissions" and ref is BUNDLED_EXTENSION
                           and sorted(old.get(key, [])) == ["alarms", "debugger", "storage"]
                           and sorted(manifest.get(key, [])) == ["alarms", "debugger", "downloads", "storage"])
                   and not (key == "host_permissions" and ref is BUNDLED_EXTENSION
                           and sorted(item.replace("http://127.0.0.1:7877/*", "http://127.0.0.1/*")
                                      for item in old.get(key, [])) == sorted(manifest.get(key, [])))
                   and not (key == "permissions" and ref is BUNDLED_EXTENSION
                           and sorted(old.get(key, [])) == ["alarms", "storage"]
                           and sorted(manifest.get(key, [])) == ["alarms", "debugger", "storage"]))):
            raise ValueError("Bản tiện ích mới thay đổi quyền Chrome và cần xác nhận riêng.")
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chrome-patch-", dir=DATA) as temp:
        pending = Path(temp)
        backup = {name: (folder / name).read_bytes() for name in names if (folder / name).is_file()}
        written = []
        try:
            for name in ("worker.js", "flow.js", "popup.html", "manifest.json"):
                path = pending / name
                path.write_bytes(content[name])
                os.replace(path, folder / name)
                written.append(name)
        except OSError:
            for name in reversed(written):
                target = folder / name
                if name in backup:
                    path = pending / name
                    path.write_bytes(backup[name])
                    os.replace(path, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return manifest["version"], True

def prepare_chrome_bundle():
    if not LOCK.acquire(blocking=False):
        return "Đợi tác vụ đang chạy kết thúc trước khi chuẩn bị tiện ích."
    try:
        version, _ = sync_chrome_bundle(allow_muse=True)
        folder = DATA / "chrome-extension"
        os.startfile(str(folder))
        return (f"Tiện ích {version} tại {folder}. Trong Chrome, mở chrome://extensions và "
                "nạp thư mục này một lần. Giữ nguyên đường dẫn để tự cập nhật sau này.")
    except Exception as error:
        return f"Không chuẩn bị được tiện ích Chrome: {error}"
    finally:
        LOCK.release()

def refresh_existing_extension():
    while True:
        if (DATA / "chrome-extension" / "manifest.json").is_file() and LOCK.acquire(blocking=False):
            try:
                sync_chrome_bundle()
            except (OSError, ValueError, KeyError):
                pass
            finally:
                LOCK.release()
        time.sleep(60)


def update():
    if not LOCK.acquire(blocking=False):
        return "Đợi video xuất xong trước khi cập nhật."
    restart = False
    try:
        manifest = json.loads(patch_read(PATCH_ROOT + f"main/update.json?t={time.time_ns()}", 16384))
        version = manifest["version"]
        companion = ""
        if (DATA / "chrome-extension" / "manifest.json").is_file():
            try:
                ext_version, changed = sync_chrome_bundle()
            except ValueError:
                ext_version, changed = None, False
            if changed:
                companion = f" Đã đồng bộ tiện ích Chrome {ext_version}. Chrome sẽ tự nạp lại khi rảnh."
        if version_tuple(version) <= version_tuple(VERSION):
            return f"Vẽ Tay đang ở bản mới nhất ({VERSION})." + companion
        if manifest["abi"] != PATCH_ABI:
            return "Bản này cần nâng cấp thư viện. Tải bộ cài tại trang GitHub Releases."
        revision = manifest["commit"]
        expected = manifest["git_blob"]
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
                   for value in (revision, expected)):
            raise ValueError("Mã kiểm tra gói cập nhật không hợp lệ.")
        payload = patch_read(PATCH_ROOT + revision + "/app.py", 1024 * 1024)
        actual = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
        if actual != expected:
            raise ValueError("Gói tải về không khớp mã kiểm tra GitHub.")
        source = payload.decode("utf-8")
        if f'VERSION = "{version}"' not in source.splitlines():
            raise ValueError("Phiên bản trong gói cập nhật không khớp.")
        target = ROOT / "app.py"
        compile(source, str(target), "exec")
        with tempfile.TemporaryDirectory(prefix="vetay-patch-", dir=ROOT) as folder:
            pending = Path(folder) / "app.py"
            pending.write_bytes(payload)
            backup = Path(folder) / "previous.py"
            shutil.copyfile(target, backup)
            os.replace(pending, target)
            try:
                subprocess.Popen([sys.executable, str(target)], cwd=ROOT)
            except OSError:
                os.replace(backup, target)
                raise
        restart = True
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return f"Đã cập nhật nhẹ lên {version}. Đang mở lại Vẽ Tay…"
    except Exception as error:
        return f"Cập nhật thất bại: {error}"
    finally:
        if not restart:
            LOCK.release()


def open_chrome_flow(token, port, provider):
    candidates = [Path(os.environ[key]) / "Google/Chrome/Application/chrome.exe"
                  for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(key)]
    chrome = next((path for path in candidates if path.is_file()), None)
    if chrome is None:
        raise RuntimeError("Không tìm thấy Google Chrome trên máy.")
    website = "https://muse.ai/" if provider == "muse" else "https://flow.google.com/"
    project = website + f"#vetay={token}-{port}"
    subprocess.Popen([str(chrome), "--new-tab", project])

class ChromeFlowBridge:
    def __init__(self):
        self.token = uuid.uuid4().hex
        self.guard = threading.Lock()
        self.job = None

    def serve(self, port=7877):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        bridge = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def response(self, status, data):
                content = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                import hmac
                if self.path == "/extension-idle":
                    with bridge.guard:
                        idle = bridge.job is None and not LOCK.locked()
                    return self.response(200, {"idle": idle})
                if not hmac.compare_digest(self.headers.get("X-VeTay-Token", ""), bridge.token):
                    return self.response(403, "Mã kết nối không đúng.")
                if self.path not in ("/next", "/batch"):
                    return self.response(404, "Không tìm thấy.")
                with bridge.guard:
                    state = bridge.job
                    index = len(state["paths"]) if state else 0
                    if not state or state["error"] or index >= len(state["prompts"]):
                        return self.response(200, {"done": True})
                    result = {"job": state["id"], "index": index, "prompt": state["prompts"][index], "provider": state["provider"]}
                    if self.path == "/batch":
                        result["prompts"] = state["prompts"][index:]
                self.response(200, result)

            def do_POST(self):
                import hmac
                import io
                from PIL import Image, ImageOps
                if not hmac.compare_digest(self.headers.get("X-VeTay-Token", ""), bridge.token):
                    return self.response(403, "Mã kết nối không đúng.")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 24 * 1024 * 1024:
                        raise ValueError("Kích thước dữ liệu không hợp lệ.")
                    self.connection.settimeout(20)
                    with bridge.guard:
                        state = bridge.job
                        if not state or state["error"]:
                            return self.response(409, "Không có tác vụ đang chờ.")
                        index = len(state["paths"])
                        if self.path == "/progress" and size <= 256:
                            progress = json.loads(self.rfile.read(size))
                            count = progress["count"]
                            if type(count) is not int or not 1 <= count <= len(state["prompts"]):
                                return self.response(400, "Số cảnh không hợp lệ.")
                            key = {"submitted": "submitted", "retry": "retrying", "batch": "batching"}.get(progress.get("stage"), "generated")
                            state[key] = max(state.get(key, 0), count)
                        elif (state["provider"] == "muse" and index < len(state["prompts"])
                              and self.path == f"/skipMuse/{state['id']}/{index}" and size == 2):
                            self.rfile.read(size)
                            state["paths"].append(None)
                        elif self.path == "/error" and size <= 8192:
                            state["error"] = str(json.loads(self.rfile.read(size))["message"])[:600]
                        elif index < len(state["prompts"]) and self.path in (
                            f"/image/{state['id']}/{index}", f"/download2k/{state['id']}/{index}",
                            f"/downloadMuse/{state['id']}/{index}"
                        ):
                            payload = self.rfile.read(size)
                            if len(payload) != size:
                                raise ValueError("Ảnh chưa tải đủ.")
                            if self.path.startswith(("/download2k/", "/downloadMuse/")):
                                is_muse = self.path.startswith("/downloadMuse/")
                                if is_muse != (state["provider"] == "muse"):
                                    raise ValueError("Nguồn ảnh không khớp tác vụ.")
                                expected_folder = "VeTay-Muse" if is_muse else "VeTay-2K"
                                if size > 8192:
                                    raise ValueError("Đường dẫn ảnh 2K không hợp lệ.")
                                downloaded = Path(json.loads(payload)["path"])
                                expected = f"{state['id']}-{index}"
                                if (not downloaded.is_absolute() or downloaded.stem != expected
                                        or downloaded.parent.name != expected_folder or downloaded.is_symlink()
                                        or downloaded.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}):
                                    raise ValueError("Chrome không lưu ảnh 2K đúng thư mục của tác vụ.")
                                with downloaded.open("rb") as source:
                                    payload = source.read(24 * 1024 * 1024 + 1)
                                if len(payload) > 24 * 1024 * 1024:
                                    raise ValueError("Ảnh 2K vượt quá giới hạn dung lượng.")
                            with Image.open(io.BytesIO(payload)) as image:
                                if image.width * image.height > 32_000_000:
                                    raise ValueError("Ảnh quá lớn.")
                                target = state["folder"] / f"chrome-{index:04}.png"
                                ImageOps.exif_transpose(image).convert("RGB").save(target)
                            state["paths"].append(target)
                        else:
                            return self.response(409, "Ảnh không khớp mã hoặc thứ tự cảnh.")
                        state["event"].set()
                    self.response(200, {"received": True})
                except Exception as error:
                    self.response(400, str(error)[:300])

        import socket
        class ExclusiveServer(ThreadingHTTPServer):
            allow_reuse_address = False

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        try:
            server = ExclusiveServer(("127.0.0.1", port), Handler)
        except OSError:
            if port != 7877:
                raise
            server = ExclusiveServer(("127.0.0.1", 0), Handler)
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def images(self, prompts, folder, report, provider="chrome"):
        name = "Muse" if provider == "muse" else "Google Flow"
        state = {"id": uuid.uuid4().hex, "prompts": prompts, "folder": folder,
                 "paths": [], "event": threading.Event(), "error": None, "provider": provider}
        with self.guard:
            self.token = uuid.uuid4().hex
            self.job = state
        try:
            open_chrome_flow(self.token, self.port, provider)
            report(f"Đã mở {name}. Đang chờ tiện ích Chrome.")
            while True:
                if not state["event"].wait(330):
                    raise RuntimeError(f"Hết thời gian chờ ảnh {name}. Kiểm tra tab Chrome và phiên bản tiện ích.")
                with self.guard:
                    state["event"].clear()
                    if state["error"]:
                        raise RuntimeError(name + ": " + state["error"])
                    count = len(state["paths"])
                    if count == len(prompts):
                        return state["paths"].copy()
                if state.pop("batching", None) is not None:
                    report("Muse đã xong một đợt 10 cảnh. Nghỉ 30 giây trước đợt tiếp theo.")
                    continue
                if (retry := state.pop("retrying", None)) is not None:
                    report(f"Đang chờ thử lại cảnh {retry} bằng {name}.")
                    continue
                report(f"Đã gửi {state.get('submitted', 0)}/{len(prompts)} prompt. {name} đã tạo {state.get('generated', count)}/{len(prompts)} ảnh. Vẽ Tay đã nhận {count}/{len(prompts)} ảnh.")
        finally:
            with self.guard:
                self.job = None

def ui(check=False):
    prepare_data()
    import gradio as gr
    bridge = ChromeFlowBridge()
    chrome_server = None
    def choose_output_folder(current):
        command = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$picker = New-Object System.Windows.Forms.FolderBrowserDialog
$picker.Description = 'Ve Tay - MP4'
$picker.SelectedPath = $env:VETAY_OUTPUT_PICKER
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
try {
    if ($picker.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($picker.SelectedPath)
    }
} finally { $picker.Dispose(); $owner.Dispose() }
"""
        env = dict(os.environ, VETAY_OUTPUT_PICKER=current or str(DATA))
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", command],
                env=env, capture_output=True, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise gr.Error("Không mở được hộp chọn thư mục. Bạn có thể nhập đường dẫn trực tiếp.") from error
        return result.stdout.decode("utf-8").strip() or current

    def open_output_folder(current):
        folder = Path((current or "").strip().strip('"') or DATA).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise gr.Error("Thư mục lưu video chưa tồn tại.")
        os.startfile(str(folder))

    def run(script, voice, pictures, provider, ratio, highlights, render, output_folder=None, shared_prompt="", full_resolution=True, progress=gr.Progress()):
        nonlocal chrome_server

        def report(message):
            progress(None, desc=message)
        if not LOCK.acquire(blocking=False):
            yield gr.skip(), gr.skip(), "Một tác vụ Vẽ Tay vẫn đang chạy. Đợi tác vụ đó kết thúc rồi thử lại."
            return
        rows = []
        try:
            save_folder = Path((output_folder or "").strip().strip('"') or DATA).expanduser()
            if render:
                if not save_folder.is_absolute() or not save_folder.is_dir():
                    raise ValueError("Chọn một thư mục lưu video đã tồn tại.")
                save_folder = save_folder.resolve()
                with tempfile.TemporaryFile(dir=save_folder):
                    pass
            with tempfile.TemporaryDirectory(prefix="input-", dir=DATA) as folder:
                folder = Path(folder)
                fingerprint = hashlib.sha256()
                if voice:
                    with Path(voice).open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            fingerprint.update(chunk)
                else:
                    fingerprint.update((script or "").encode("utf-8"))
                key = ("voice" if voice else "script", fingerprint.hexdigest())
                cache = LAST_ANALYSIS.get("wav")
                if LAST_ANALYSIS.get("key") == key and cache is not None and cache.is_file():
                    wav, plan = cache, LAST_ANALYSIS["plan"]
                else:
                    yield [], None, "Đang chuẩn bị lời đọc…"
                    wav = prepare_audio(script, voice, folder)
                    yield [], None, "Đang nhận dạng lời đọc và chia cảnh…"
                    plan = scenes(transcribe(wav), duration(wav))
                    next_cache = DATA / f"analysis-{uuid.uuid4().hex}.wav"
                    os.replace(wav, next_cache)
                    wav = next_cache
                    LAST_ANALYSIS.update(key=key, plan=plan, wav=wav)
                    if cache is not None:
                        try:
                            cache.unlink(missing_ok=True)
                        except OSError:
                            pass
                rows = scene_rows(plan, ratio, highlights, shared_prompt, provider != "files")
                if not render:
                    yield rows, None, f"Sẵn sàng: {len(rows)} cảnh và prompt."
                    return
                if provider in {"chrome", "muse"}:
                    if chrome_server is None:
                        try:
                            chrome_server = bridge.serve()
                        except OSError as error:
                            raise RuntimeError(
                                f"Không mở được cổng nội bộ cho Chrome: {error}"
                            ) from error
                    name = "Muse" if provider == "muse" else "Google Flow"
                    yield rows, None, f"Đang mở {name} trong Chrome…"
                    pictures = bridge.images([row[5] for row in rows], folder, report, provider)

                if not pictures:
                    raise ValueError("Chọn ảnh theo thứ tự cảnh hoặc chọn Google Flow.")
                missing = [str(i) for i, picture in enumerate(pictures, 1) if picture is None]
                if len(missing) == len(pictures):
                    raise ValueError("Muse không tạo được ảnh cho cảnh " + ", ".join(missing) + ".")
                note = f" · Cảnh thiếu ảnh (nền trắng): {', '.join(missing)}" if missing else ""
                dest = DATA / f"VeTay-{uuid.uuid4().hex[:8]}.mp4" 
                path = render_folder(wav, pictures, plan, dest, report, ratio, highlights, full_resolution)
                saved = save_folder / dest.name
                if saved != dest:
                    try:
                        with tempfile.TemporaryDirectory(prefix="vetay-export-", dir=save_folder) as staging:
                            copy = Path(staging) / dest.name
                            shutil.copyfile(dest, copy)
                            os.replace(copy, saved)
                    except OSError as error:
                        yield rows, (path, str(dest)), f"Đã dựng MP4 tại {dest}. Không lưu được sang {save_folder}: {error}"
                        return
                yield rows, (path, str(saved)), f"Đã lưu MP4 · {len(rows)} cảnh · {saved}{note}"
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
            yield rows, None, "Lỗi xử lý âm thanh hoặc video: " + detail[-1500:]
        except Exception as error:
            yield rows, None, "Tác vụ dừng: " + str(error)
        finally:
            LOCK.release()

    def analyze(script, voice, ratio, highlights, shared_prompt):
        yield from run(script, voice, None, None, ratio, highlights, False, shared_prompt=shared_prompt)

    def generate_video(script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt, full_resolution):
        for rows, result, message in run(
            script, voice, pictures, provider, ratio, highlights, True, output_folder, shared_prompt, full_resolution
        ):
            preview_path, saved_path = result if isinstance(result, tuple) else (None, "")
            yield rows, preview_path, message, saved_path

    def reveal_video(saved_path):
        video_file = Path(saved_path or "")
        if not video_file.is_absolute() or not video_file.is_file() or video_file.suffix.lower() != ".mp4":
            raise gr.Error("Chưa có tệp MP4 để mở.")
        subprocess.Popen(["explorer.exe", "/select,", str(video_file)])

    css = "body, .gradio-container, button, textarea, input { font-family: 'Segoe UI', Arial, sans-serif !important; }"
    with gr.Blocks(title="Vẽ Tay") as app:
        gr.HTML('<div lang="vi"><h1>Vẽ Tay</h1></div>')
        with gr.Row():
            script = gr.Textbox(label="Kịch bản tiếng Việt", lines=4)
            shared_prompt = gr.Textbox(label="Prompt chung cho toàn video", lines=4,
                                      placeholder="Ví dụ: Nền kem, nét vẽ bút chì, giữ cùng nhân vật và bảng màu qua mọi cảnh.")
        with gr.Row():
            with gr.Column():
                voice = gr.Audio(label="Bản thu lời đọc (MP3 / WAV)", sources=["upload"], type="filepath")

                ratio = gr.Dropdown([(f"{key} · {w} × {h}", key) for key, (w,h) in VIDEO_SIZES.items()],
                                    value="9:16", label="Tỷ lệ video")
            with gr.Column():
                provider = gr.Radio([("Google Flow qua tiện ích Chrome", "chrome"), ("Muse qua Chrome", "muse"), ("Ảnh có sẵn", "files")],
                                    value="chrome", label="Nguồn ảnh minh họa")

                pictures = gr.File(label="Ảnh có sẵn theo thứ tự cảnh", file_count="multiple",
                                   file_types=["image"], type="filepath")
        video = gr.Video(label="Xem trước video MP4", height=260, elem_id="vetay-preview")
        full_resolution = gr.Checkbox(value=True, label="Dựng ở độ phân giải đầy đủ")
        highlights = gr.Checkbox(value=True, label="Chữ nhấn theo cảnh")
        with gr.Row():
            analysis = gr.Button("Phân tích và chia cảnh")
            generate = gr.Button("Tự tạo video", variant="primary")
        table = gr.Dataframe(headers=["Cảnh", "Bắt đầu", "Kết thúc", "Thời lượng (giây)",
                                      "Lời đọc", "Prompt tạo ảnh", "Chữ nhấn"],
                             datatype=["number", "str", "str", "number", "str", "str", "str"],
                             label="Bảng phân cảnh theo lời đọc", wrap=True, interactive=False)
        task = gr.Textbox(label="Tiến độ dựng", interactive=False)
        preview_path = gr.State()
        with gr.Row():
            saved_video = gr.Textbox(label="Tệp MP4 đã lưu", interactive=False)
            show_video = gr.Button("Hiện tệp MP4 trong thư mục")
        show_video.click(reveal_video, [saved_video], None, queue=False)
        with gr.Row():
            output_folder = gr.Textbox(value=str(DATA), label="Thư mục lưu MP4")
            choose_folder = gr.Button("Chọn thư mục")
            open_folder = gr.Button("Mở thư mục lưu")
        choose_folder.click(choose_output_folder, [output_folder], [output_folder], queue=False)
        open_folder.click(open_output_folder, [output_folder], None, queue=False)
        status = gr.Textbox(label="Trạng thái cập nhật", interactive=False)
        gr.Button("Chuẩn bị tiện ích Chrome").click(prepare_chrome_bundle, outputs=status)
        gr.Button("Cập nhật qua GitHub").click(update, outputs=status)
        analysis.click(analyze, [script, voice, ratio, highlights, shared_prompt], [table, video, task])
        start_generation = generate.click(
            fn=lambda: "Đã nhận lệnh tạo video. Đang chờ bắt đầu dựng…",
            outputs=task, queue=False, show_progress="hidden",
        )
        generation = start_generation.success(
            generate_video, [script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt, full_resolution],
            [table, preview_path, task, saved_video],
            show_progress="minimal",
        )
        preview = generation.success(
            fn=lambda path: path, inputs=[preview_path], outputs=[video], queue=False, show_progress="hidden",
        )
        preview.success(
            fn=None, inputs=[video], outputs=None, queue=False, show_progress="hidden",
            js="""(video) => {
                if (video) document.getElementById('vetay-preview')?.scrollIntoView({behavior: 'smooth', block: 'start'});
                return [];
            }""",
        )
    if check:
        engine()
        print(f"VeTay {VERSION}: {len(app.config['components'])} controls.")
        return
    import webview
    try:
        chrome_server = bridge.serve()
    except OSError:
        pass
    threading.Thread(target=refresh_existing_extension, daemon=True).start()
    _, local_url, _ = app.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1", prevent_thread_lock=True, inbrowser=False,
        share=False, allowed_paths=[str(DATA)], quiet=True, css=css)
    try:
        webview.create_window("Vẽ Tay", local_url, width=1080, height=900, min_size=(740, 620))
        webview.start(gui="edgechromium")
    finally:
        if chrome_server is not None:
            chrome_server.shutdown()
            chrome_server.server_close()
        app.close()

if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--smoke", nargs=3, metavar=("VOICE", "IMAGE", "OUTPUT"))
    cli.add_argument("--check", action="store_true")
    cli.add_argument("--ratio", choices=VIDEO_SIZES, default="9:16")
    args = cli.parse_args()
    if args.smoke:
        voice, image, out = map(lambda item: Path(item).resolve(), args.smoke)
        render_folder(voice, [image], [(0, 48, "Smoke test")], out, print, args.ratio)
    else:
        ui(check=args.check)

