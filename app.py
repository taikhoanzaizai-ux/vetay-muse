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
VERSION = "0.2.68"
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
    "version": "1.3.43",
    "commit": "cae916926a416b7ca24417784230af4ffd5f4c30",
    "files": {
        "manifest.json": "159a1fbab9eb14ce6cee3a9ae7f8e37c6149cda0",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4",
        "worker.js": "538461be4ed59c41a8012745235e628201784d27",
        "flow.js": "f9e8b91eaf59dfcaf16aca0c617fa9ab89e20940"
    }
}
BUNDLED_EXTENSION_FILES = {
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjQzIiwKICAiYWN0aW9uIjogewogICAgImRlZmF1bHRfcG9wdXAiOiAicG9wdXAuaHRtbCIKICB9LAogICJiYWNrZ3JvdW5kIjogewogICAgInNlcnZpY2Vfd29ya2VyIjogIndvcmtlci5qcyIKICB9LAogICJwZXJtaXNzaW9ucyI6IFsKICAgICJzdG9yYWdlIiwKICAgICJhbGFybXMiLAogICAgImRlYnVnZ2VyIiwKICAgICJkb3dubG9hZHMiCiAgXSwKICAiaG9zdF9wZXJtaXNzaW9ucyI6IFsKICAgICJodHRwOi8vMTI3LjAuMC4xLyoiLAogICAgImh0dHBzOi8vKi5nb29nbGV1c2VyY29udGVudC5jb20vKiIsCiAgICAiaHR0cHM6Ly9mbG93Lmdvb2dsZS5jb20vKiIsCiAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIiwKICAgICJodHRwczovL211c2UuYWkvKiIKICBdLAogICJjb250ZW50X3NjcmlwdHMiOiBbCiAgICB7CiAgICAgICJtYXRjaGVzIjogWwogICAgICAgICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbS8qIiwKICAgICAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIgogICAgICBdLAogICAgICAianMiOiBbCiAgICAgICAgImZsb3cuanMiCiAgICAgIF0sCiAgICAgICJydW5fYXQiOiAiZG9jdW1lbnRfc3RhcnQiCiAgICB9LAogICAgewogICAgICAibWF0Y2hlcyI6IFsKICAgICAgICAiaHR0cHM6Ly9tdXNlLmFpLyoiCiAgICAgIF0sCiAgICAgICJqcyI6IFsKICAgICAgICAiZmxvdy5qcyIKICAgICAgXSwKICAgICAgInJ1bl9hdCI6ICJkb2N1bWVudF9zdGFydCIKICAgIH0KICBdCn0K",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg==",
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh7dG9rZW4sIHBvcnR9LCBwYXRoLCBib2R5KSB7CiAgY29uc3QgciA9IGF3YWl0IGZldGNoKCJodHRwOi8vMTI3LjAuMC4xOiIgKyBwb3J0ICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY29uc3QgcGVuZGluZ0Rvd25sb2FkcyA9IG5ldyBNYXAoKTsKY2hyb21lLmRvd25sb2Fkcy5vbkRldGVybWluaW5nRmlsZW5hbWUuYWRkTGlzdGVuZXIoKGl0ZW0sIHN1Z2dlc3QpID0+IHsKICBjb25zdCBwZW5kaW5nID0gWy4uLnBlbmRpbmdEb3dubG9hZHMudmFsdWVzKCldLmZpbHRlcihlbnRyeSA9PiAhZW50cnkuaWQgJiYgRGF0ZS5ub3coKSA8IGVudHJ5LmRlYWRsaW5lKTsKICBpZiAocGVuZGluZy5sZW5ndGggIT09IDEpIHJldHVybjsKICBjb25zdCBlbnRyeSA9IHBlbmRpbmdbMF07CiAgY29uc3QgdXJscyA9IFtpdGVtLnVybCwgaXRlbS5maW5hbFVybF0uZmlsdGVyKEJvb2xlYW4pOwogIGNvbnN0IGlzSW1hZ2UgPSAvXmltYWdlXC8oPzpwbmd8anBlZ3x3ZWJwKSQvaS50ZXN0KGl0ZW0ubWltZSB8fCAiIikKICAgIHx8IC9cLihwbmd8anBlP2d8d2VicCkkL2kudGVzdChpdGVtLmZpbGVuYW1lIHx8ICIiKTsKICBjb25zdCBmcm9tRmxvdyA9IGl0ZW0ucmVmZXJyZXIgPT09IGVudHJ5LnBhZ2UKICAgIHx8IHVybHMuc29tZSh1cmwgPT4gL15ibG9iOmh0dHBzOlwvXC9mbG93XC5nb29nbGVcLmNvbVwvL2kudGVzdCh1cmwpKQogICAgfHwgKGlzSW1hZ2UgJiYgIWl0ZW0ucmVmZXJyZXIgJiYgdXJscy5zb21lKHVybCA9PgogICAgICAvXmh0dHBzOlwvXC8oPzpbYS16MC05LV0rXC4pKig/Omdvb2dsZXVzZXJjb250ZW50XC5jb218Z3N0YXRpY1wuY29tKVwvL2kudGVzdCh1cmwpKSk7CiAgY29uc3QgbXVzZSA9IGVudHJ5LmZpbGVuYW1lICE9PSB1bmRlZmluZWQ7CiAgLy8gVGVuIHRlcCBNdXNlIGNodWEgbWEgY2FuaCB2ZXRheSBkdXkgbmhhdCBjdWEgZHVuZyBjYW5oIGRhbmcgY2hvLCBuZW4gY2hpIGNhbgogIC8vIGtob3AgdGVuIHRlcCAoa2UgY2Ega2hpIENocm9tZSB0aGVtICIgKDEpIiBsdWMgdHJ1bmcgdGVuKS4gRGlldSBraWVuIGN1IGRvaQogIC8vIHJlZmVycmVyIHBoYWkgYmFuZyBuZ3V5ZW4gVVJMIHRyYW5nIHhlbSBhbmggdmEgVVJMIHBoYWkgdGh1b2MgbXVzZS5haSwga2hpZW4KICAvLyBhbmggdGFpIHF1YSBDRE4gLyByZWZlcnJlciBydXQgZ29uIGJpIGJvIHF1YSB2YSBWZVRheSBraG9uZyBiYW8gZ2lvIG5oYW4gYW5oLgogIGNvbnN0IGJhc2VOYW1lID0gbmFtZSA9PiAobmFtZSB8fCAiIikuc3BsaXQoL1tcXC9dLykucG9wKCkudG9Mb3dlckNhc2UoKS5yZXBsYWNlKC8gXChcZCtcKShcLlteLl0rKSQvLCAiJDEiKTsKICBjb25zdCBmcm9tTXVzZSA9IG11c2UgJiYgaXNJbWFnZSAmJiBiYXNlTmFtZShpdGVtLmZpbGVuYW1lKSA9PT0gZW50cnkuZmlsZW5hbWUudG9Mb3dlckNhc2UoKTsKICBpZiAobXVzZSA/ICFmcm9tTXVzZSA6ICFmcm9tRmxvdykgcmV0dXJuOwogIGNvbnN0IGV4dGVuc2lvbiA9IC9cLihwbmd8anBlP2d8d2VicCkkL2kuZXhlYyhpdGVtLmZpbGVuYW1lIHx8ICIiKT8uWzFdIHx8ICJwbmciOwogIGVudHJ5LmlkID0gaXRlbS5pZDsKICBzdWdnZXN0KHtmaWxlbmFtZToobXVzZSA/ICJWZVRheS1NdXNlLyIgOiAiVmVUYXktMksvIikgKyAiIiArIGVudHJ5LnN0ZW0gKyAiLiIgKyBleHRlbnNpb24sIGNvbmZsaWN0QWN0aW9uOiJvdmVyd3JpdGUifSk7Cn0pOwpjaHJvbWUucnVudGltZS5vbk1lc3NhZ2UuYWRkTGlzdGVuZXIoKG0sc2VuZGVyLHJlcGx5KSA9PiB7CiAgKGFzeW5jKCk9PnsKICAgIGNvbnN0IG11c2UgPSBzZW5kZXIuZnJhbWVJZCA9PT0gMCAmJiBzZW5kZXIudGFiPy51cmw/LnN0YXJ0c1dpdGgoImh0dHBzOi8vbXVzZS5haS8iKTsKICAgIGlmICghbXVzZSAmJiAhZmxvd1RhYihzZW5kZXIudGFiPy51cmwpKSB0aHJvdyBFcnJvcigiVGFiIGtow7RuZyB0aHXhu5ljIEdvb2dsZSBGbG93IGhv4bq3YyBNdXNlLiIpOwogICAgY29uc3Qga2V5ID0gInBhaXJpbmc6IiArIHNlbmRlci50YWIuaWQ7CiAgICBpZiAobS5vcCA9PT0gInBhaXIiKSB7CiAgICAgIGlmICghL15bYS1mMC05XXszMn0kLy50ZXN0KG0udG9rZW4gfHwgIiIpKSB0aHJvdyBFcnJvcigiTcOjIGvhur90IG7hu5FpIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICBjb25zdCBwb3J0ID0gbS5wb3J0ID8/IDc4Nzc7CiAgICAgIGlmICghTnVtYmVyLmlzSW50ZWdlcihwb3J0KSB8fCBwb3J0IDwgMTAyNCB8fCBwb3J0ID4gNjU1MzUpIHRocm93IEVycm9yKCJD4buVbmcgVuG6vSBUYXkga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIGNvbnN0IGNvbm5lY3Rpb24gPSB7dG9rZW46bS50b2tlbiwgcG9ydH07CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShjb25uZWN0aW9uLCAiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lKSB0aHJvdyBFcnJvcigiVuG6vSBUYXkgY2jGsGEgY8OzIGPhuqNuaCBjaOG7nS4iKTsKICAgICAgaWYgKG11c2UgIT09IChqb2IucHJvdmlkZXIgPT09ICJtdXNlIikpIHRocm93IEVycm9yKCJOZ3Xhu5NuIOG6o25oIGtow7RuZyBraOG7m3AgdMOhYyB24bulLiIpOwogICAgICBhd2FpdCBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLnNldCh7W2tleV06Y29ubmVjdGlvbn0pOwogICAgICByZXR1cm4gam9iOwogICAgfQogICAgY29uc3Qgc2F2ZWQgPSBhd2FpdCBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLmdldChrZXkpOwogICAgY29uc3QgcGFpcmluZyA9IHR5cGVvZiBzYXZlZFtrZXldID09PSAic3RyaW5nIiA/IHt0b2tlbjpzYXZlZFtrZXldLHBvcnQ6Nzg3N30gOiBzYXZlZFtrZXldOwogICAgaWYgKG0ub3AgPT09ICJyZXN1bWUiKSByZXR1cm4gcGFpcmluZyA/IGFwaShwYWlyaW5nLCIvbmV4dCIpIDoge2RvbmU6dHJ1ZX07CiAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJUYWIgY2jGsGEgxJHGsOG7o2MgVuG6vSBUYXkga+G6v3QgbuG7kWkuIik7CiAgICBpZiAobXVzZSAmJiAhWyJuZXh0IiwiYmF0Y2giLCJwcm9ncmVzcyIsImVycm9yIiwiYXJtTXVzZSIsInRyYW5zZmVyTXVzZSIsInNraXBNdXNlIiwiaW1hZ2UiXS5pbmNsdWRlcyhtLm9wKSkKICAgICAgdGhyb3cgRXJyb3IoIlRoYW8gdMOhYyBraMO0bmcgaOG7o3AgbOG7hyB0cm9uZyB0YWIgTXVzZS4iKTsKICAgIGlmIChtLm9wID09PSAic2tpcE11c2UiKSB7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShwYWlyaW5nLCAiL25leHQiKTsKICAgICAgaWYgKCFtdXNlIHx8IGpvYi5wcm92aWRlciAhPT0gIm11c2UiIHx8IGpvYi5qb2IgIT09IG0uam9iIHx8IGpvYi5pbmRleCAhPT0gbS5pbmRleCkKICAgICAgICB0aHJvdyBFcnJvcigiQ+G6o25oIE11c2UgY+G6p24gYuG7jyBxdWEga2jDtG5nIGto4bubcC4iKTsKICAgICAgcGVuZGluZ0Rvd25sb2Fkcy5kZWxldGUoa2V5KTsKICAgICAgcmV0dXJuIGFwaShwYWlyaW5nLCAiL3NraXBNdXNlLyIgKyBqb2Iuam9iICsgIi8iICsgam9iLmluZGV4LCAie30iKTsKICAgIH0KICAgIGlmIChtLm9wID09PSAiYXJtTXVzZSIgfHwgbS5vcCA9PT0gInRyYW5zZmVyTXVzZSIpIHsKICAgICAgY29uc3Qgam9iID0gYXdhaXQgYXBpKHBhaXJpbmcsICIvbmV4dCIpOwogICAgICBpZiAoIW11c2UgfHwgam9iLnByb3ZpZGVyICE9PSAibXVzZSIgfHwgam9iLmpvYiAhPT0gbS5qb2IgfHwgam9iLmluZGV4ICE9PSBtLmluZGV4KQogICAgICAgIHRocm93IEVycm9yKCJUw6FjIHbhu6Ug4bqjbmggTXVzZSDEkcOjIMSR4buVaS4iKTsKICAgICAgaWYgKG0ub3AgPT09ICJhcm1NdXNlIikgewogICAgICAgIGNvbnN0IHBhZ2UgPSBuZXcgVVJMKHNlbmRlci50YWIudXJsKTsKICAgICAgICBjb25zdCBmaWxlbmFtZSA9IHBhZ2Uuc2VhcmNoUGFyYW1zLmdldCgiZmlsZSIpPy5zcGxpdCgiLyIpLnBvcCgpOwogICAgICAgIGNvbnN0IHRhZyA9ICJ2ZXRheSIgKyBqb2Iuam9iLnNsaWNlKDAsIDEyKSArICJjIiArIFN0cmluZyhqb2IuaW5kZXggKyAxKS5wYWRTdGFydCg1LCAiMCIpOwogICAgICAgIGlmIChwYWdlLnBhdGhuYW1lICE9PSAiL2xpYnJhcnkvbWVkaWEiIHx8IGZpbGVuYW1lICE9PSBtLmZpbGVuYW1lIHx8CiAgICAgICAgICAgICFmaWxlbmFtZT8udG9Mb3dlckNhc2UoKS5pbmNsdWRlcyh0YWcpIHx8ICEvXC4ocG5nfGpwZT9nfHdlYnApJC9pLnRlc3QoZmlsZW5hbWUpKQogICAgICAgICAgdGhyb3cgRXJyb3IoIuG6om5oIMSRYW5nIG3hu58ga2jDtG5nIGto4bubcCBtw6MgY+G6o25oIE11c2UuIik7CiAgICAgICAgcGVuZGluZ0Rvd25sb2Fkcy5zZXQoa2V5LCB7c3RlbTogam9iLmpvYiArICItIiArIGpvYi5pbmRleCwgZmlsZW5hbWUsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgcGFnZTogc2VuZGVyLnRhYi51cmwsIGRlYWRsaW5lOiBEYXRlLm5vdygpICsgOTAwMDB9KTsKICAgICAgICByZXR1cm4ge2FybWVkOnRydWV9OwogICAgICB9CiAgICAgIGNvbnN0IGVudHJ5ID0gcGVuZGluZ0Rvd25sb2Fkcy5nZXQoa2V5KTsKICAgICAgaWYgKCFlbnRyeT8uaWQpIHJldHVybiB7cmVjZWl2ZWQ6ZmFsc2V9OwogICAgICBjb25zdCBbaXRlbV0gPSBhd2FpdCBjaHJvbWUuZG93bmxvYWRzLnNlYXJjaCh7aWQ6ZW50cnkuaWR9KTsKICAgICAgaWYgKGl0ZW0/LnN0YXRlID09PSAiaW50ZXJydXB0ZWQiKSB0aHJvdyBFcnJvcigiQ2hyb21lIMSRw6Mgbmfhuq90IGzGsOG7o3QgdOG6o2kg4bqjbmggTXVzZS4iKTsKICAgICAgaWYgKGl0ZW0/LnN0YXRlICE9PSAiY29tcGxldGUiKSByZXR1cm4ge3JlY2VpdmVkOmZhbHNlfTsKICAgICAgY29uc3QgcmVzdWx0ID0gYXdhaXQgYXBpKHBhaXJpbmcsICIvZG93bmxvYWRNdXNlLyIgKyBqb2Iuam9iICsgIi8iICsgam9iLmluZGV4LAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgSlNPTi5zdHJpbmdpZnkoe3BhdGg6aXRlbS5maWxlbmFtZX0pKTsKICAgICAgcGVuZGluZ0Rvd25sb2Fkcy5kZWxldGUoa2V5KTsKICAgICAgcmV0dXJuIHJlc3VsdDsKICAgIH0KICAgIGlmIChtLm9wID09PSAiYXJtMmsiIHx8IG0ub3AgPT09ICJ0cmFuc2ZlcjJrIikgewogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkocGFpcmluZywiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lIHx8IGpvYi5qb2IgIT09IG0uam9iIHx8IGpvYi5pbmRleCAhPT0gbS5pbmRleCkgdGhyb3cgRXJyb3IoIlTDoWMgduG7pSDhuqNuaCAySyDEkcOjIMSR4buVaS4iKTsKICAgICAgaWYgKG0ub3AgPT09ICJhcm0yayIpIHsKICAgICAgICBwZW5kaW5nRG93bmxvYWRzLnNldChrZXkse3N0ZW06am9iLmpvYiArICItIiArIGpvYi5pbmRleCwgZGVhZGxpbmU6RGF0ZS5ub3coKSsxNTAwMDAscGFnZTpzZW5kZXIudGFiLnVybH0pOwogICAgICAgIHJldHVybiB7YXJtZWQ6dHJ1ZX07CiAgICAgIH0KICAgICAgY29uc3QgZW50cnkgPSBwZW5kaW5nRG93bmxvYWRzLmdldChrZXkpOwogICAgICBpZiAoIWVudHJ5Py5pZCkgcmV0dXJuIHtyZWNlaXZlZDpmYWxzZX07CiAgICAgIGNvbnN0IFtpdGVtXSA9IGF3YWl0IGNocm9tZS5kb3dubG9hZHMuc2VhcmNoKHtpZDplbnRyeS5pZH0pOwogICAgICBpZiAoaXRlbT8uc3RhdGUgPT09ICJpbnRlcnJ1cHRlZCIpIHRocm93IEVycm9yKCJDaHJvbWUgxJHDoyBuZ+G6r3QgbMaw4bujdCB04bqjaSDhuqNuaCAySy4iKTsKICAgICAgaWYgKGl0ZW0/LnN0YXRlICE9PSAiY29tcGxldGUiKSByZXR1cm4ge3JlY2VpdmVkOmZhbHNlfTsKICAgICAgY29uc3QgcmVzdWx0ID0gYXdhaXQgYXBpKHBhaXJpbmcsIi9kb3dubG9hZDJrLyIgKyBqb2Iuam9iICsgIi8iICsgam9iLmluZGV4LEpTT04uc3RyaW5naWZ5KHtwYXRoOml0ZW0uZmlsZW5hbWV9KSk7CiAgICAgIHBlbmRpbmdEb3dubG9hZHMuZGVsZXRlKGtleSk7CiAgICAgIHJldHVybiByZXN1bHQ7CiAgICB9CiAgICBpZiAobS5vcCA9PT0gInN1Ym1pdCIpIHsKICAgICAgY29uc3QgdGFiID0gYXdhaXQgY2hyb21lLnRhYnMuZ2V0KHNlbmRlci50YWIuaWQpOwogICAgICBjb25zdCBwYWdlID0gbmV3IFVSTCh0YWIudXJsKTsKICAgICAgaWYgKHNlbmRlci5mcmFtZUlkICE9PSAwIHx8IHBhZ2Uub3JpZ2luICE9PSAiaHR0cHM6Ly9mbG93Lmdvb2dsZS5jb20iCiAgICAgICAgfHwgIS9eXC9wcm9qZWN0XC9bXi9dKyg/OlwvLiopPyQvLnRlc3QocGFnZS5wYXRobmFtZSkpCiAgICAgICAgdGhyb3cgRXJyb3IoIkzhu4duaCBi4bqlbSBraMO0bmcgxJHhur9uIHThu6sgbcOgbiBk4buxIMOhbiBob+G6t2MgbcOgbiB4ZW0g4bqjbmggRmxvdy4iKTsKICAgICAgaWYgKCFbbS54LG0ueV0uZXZlcnkoTnVtYmVyLmlzRmluaXRlKSB8fCBtLnggPCAwIHx8IG0ueSA8IDAgfHwgbS54ID4gMTAwMDAgfHwgbS55ID4gMTAwMDApCiAgICAgICAgdGhyb3cgRXJyb3IoIk7DunQgRmxvdyBu4bqxbSBuZ2/DoGkgdsO5bmcgY8OzIHRo4buDIGLhuqVtLiIpOwogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkocGFpcmluZywiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lIHx8IGpvYi5qb2IgIT09IG0uam9iKSB0aHJvdyBFcnJvcigiVMOhYyB24bulIFbhur0gVGF5IMSRw6MgxJHhu5VpLiIpOwogICAgICBjb25zdCB0YXJnZXQgPSB7dGFiSWQ6dGFiLmlkfTsKICAgICAgYXdhaXQgY2hyb21lLmRlYnVnZ2VyLmF0dGFjaCh0YXJnZXQsIjEuMyIpOwogICAgICB0cnkgewogICAgICAgIGNvbnN0IGlucHV0ID0gKHR5cGUsYnV0dG9ucykgPT4gY2hyb21lLmRlYnVnZ2VyLnNlbmRDb21tYW5kKHRhcmdldCwiSW5wdXQuZGlzcGF0Y2hNb3VzZUV2ZW50IiwKICAgICAgICAgIHt0eXBlLHg6bS54LHk6bS55LGJ1dHRvbjoibGVmdCIsYnV0dG9ucyxjbGlja0NvdW50OjF9KTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VQcmVzc2VkIiwxKTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VSZWxlYXNlZCIsMCk7CiAgICAgICAgaWYgKG0uY2xpY2tzID09PSAyKSB7CiAgICAgICAgICBjb25zdCBzZWNvbmQgPSAodHlwZSxidXR0b25zKSA9PiBjaHJvbWUuZGVidWdnZXIuc2VuZENvbW1hbmQodGFyZ2V0LCJJbnB1dC5kaXNwYXRjaE1vdXNlRXZlbnQiLAogICAgICAgICAgICB7dHlwZSx4Om0ueCx5Om0ueSxidXR0b246ImxlZnQiLGJ1dHRvbnMsY2xpY2tDb3VudDoyfSk7CiAgICAgICAgICBhd2FpdCBzZWNvbmQoIm1vdXNlUHJlc3NlZCIsMSk7CiAgICAgICAgICBhd2FpdCBzZWNvbmQoIm1vdXNlUmVsZWFzZWQiLDApOwogICAgICAgIH0KICAgICAgfSBmaW5hbGx5IHsKICAgICAgICBhd2FpdCBjaHJvbWUuZGVidWdnZXIuZGV0YWNoKHRhcmdldCk7CiAgICAgIH0KICAgICAgcmV0dXJuIHtjbGlja2VkOnRydWV9OwogICAgfQogICAgaWYgKG0ub3AgPT09ICJhY3RpdmF0ZSIpIHsKICAgICAgYXdhaXQgY2hyb21lLnRhYnMudXBkYXRlKHNlbmRlci50YWIuaWQsIHthY3RpdmU6dHJ1ZX0pOwogICAgICBhd2FpdCBjaHJvbWUud2luZG93cy51cGRhdGUoc2VuZGVyLnRhYi53aW5kb3dJZCwge2ZvY3VzZWQ6dHJ1ZX0pOwogICAgICByZXR1cm4ge2FjdGl2ZTp0cnVlfTsKICAgIH0KICAgIGlmIChtLm9wID09PSAibmV4dCIpIHJldHVybiBhcGkocGFpcmluZywiL25leHQiKTsKICAgIGlmIChtLm9wID09PSAiYmF0Y2giKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9iYXRjaCIpOwogICAgaWYgKG0ub3AgPT09ICJwcm9ncmVzcyIpIHJldHVybiBhcGkocGFpcmluZywiL3Byb2dyZXNzIixKU09OLnN0cmluZ2lmeSh7Y291bnQ6bS5jb3VudCxzdGFnZTptLnN0YWdlfSkpOwogICAgaWYgKG0ub3AgPT09ICJlcnJvciIpIHJldHVybiBhcGkocGFpcmluZywiL2Vycm9yIixKU09OLnN0cmluZ2lmeSh7bWVzc2FnZTptLm1lc3NhZ2V9KSk7CiAgICBpZiAobS5vcCA9PT0gImltYWdlIiAmJiAvXlthLWYwLTldezMyfSQvLnRlc3QobS5qb2IpICYmIE51bWJlci5pc0ludGVnZXIobS5pbmRleCkpIHsKICAgICAgbGV0IGltYWdlOwogICAgICBpZiAobS5kYXRhICYmIC9eZGF0YTppbWFnZVwvKHBuZ3x3ZWJwfGpwZWcpO2Jhc2U2NCwvLnRlc3QobS5kYXRhKSkgewogICAgICAgIGltYWdlID0gYXdhaXQgKGF3YWl0IGZldGNoKG0uZGF0YSkpLmJsb2IoKTsKICAgICAgfSBlbHNlIHsKICAgICAgICBjb25zdCB1ID0gbmV3IFVSTChtLnVybCk7CiAgICAgICAgaWYgKHUucHJvdG9jb2wgIT09ICJodHRwczoiIHx8ICEvKF58XC4pZ29vZ2xldXNlcmNvbnRlbnRcLmNvbSQvLnRlc3QodS5ob3N0bmFtZSkpIHRocm93IEVycm9yKCJLaMO0bmcgeMOhYyDEkeG7i25oIMSRxrDhu6NjIMSR4buLYSBjaOG7iSDhuqNuaCBr4bq/dCBxdeG6oy4iKTsKICAgICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2godS5ocmVmLHtjcmVkZW50aWFsczoib21pdCIscmVkaXJlY3Q6ImVycm9yIn0pOwogICAgICAgIGlmICghci5vaykgdGhyb3cgRXJyb3IoIktow7RuZyB04bqjaSDEkcaw4bujYyDhuqNuaCBr4bq/dCBxdeG6oy4iKTsKICAgICAgICBpbWFnZSA9IGF3YWl0IHIuYmxvYigpOwogICAgICB9CiAgICAgIGlmIChpbWFnZS5zaXplID4gMjQqMTAyNCoxMDI0IHx8ICFpbWFnZS50eXBlLnN0YXJ0c1dpdGgoImltYWdlLyIpKSB0aHJvdyBFcnJvcigi4bqibmgga+G6v3QgcXXhuqMga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIHJldHVybiBhcGkocGFpcmluZywiL2ltYWdlLyIrbS5qb2IrIi8iK20uaW5kZXgsaW1hZ2UpOwogICAgfQogICAgdGhyb3cgRXJyb3IoIlnDqnUgY+G6p3Uga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgfSkoKS50aGVuKHJlc3VsdD0+cmVwbHkoe29rOnRydWUscmVzdWx0fSksZXJyb3I9PnJlcGx5KHtvazpmYWxzZSxlcnJvcjplcnJvci5tZXNzYWdlfSkpOwogIHJldHVybiB0cnVlOwp9KTsKY2hyb21lLnRhYnMub25SZW1vdmVkLmFkZExpc3RlbmVyKHRhYklkID0+IGNocm9tZS5zdG9yYWdlLnNlc3Npb24ucmVtb3ZlKCJwYWlyaW5nOiIgKyB0YWJJZCkpOwoKY29uc3QgZXh0ZW5zaW9uQ2hlY2sgPSAidmV0YXktY2hlY2stbG9jYWwtdXBkYXRlIjsKZnVuY3Rpb24gcmVnaXN0ZXJDaGVjaygpIHsKICBjaHJvbWUuYWxhcm1zLmNyZWF0ZShleHRlbnNpb25DaGVjaywge3BlcmlvZEluTWludXRlczoxfSk7Cn0KY2hyb21lLnJ1bnRpbWUub25JbnN0YWxsZWQuYWRkTGlzdGVuZXIocmVnaXN0ZXJDaGVjayk7CmNocm9tZS5ydW50aW1lLm9uU3RhcnR1cC5hZGRMaXN0ZW5lcihyZWdpc3RlckNoZWNrKTsKY2hyb21lLmFsYXJtcy5vbkFsYXJtLmFkZExpc3RlbmVyKGFzeW5jIGFsYXJtID0+IHsKICBpZiAoYWxhcm0ubmFtZSAhPT0gZXh0ZW5zaW9uQ2hlY2spIHJldHVybjsKICB0cnkgewogICAgY29uc3QgY3VycmVudCA9IGNocm9tZS5ydW50aW1lLmdldE1hbmlmZXN0KCk7CiAgICBjb25zdCByZXNwb25zZSA9IGF3YWl0IGZldGNoKGNocm9tZS5ydW50aW1lLmdldFVSTCgibWFuaWZlc3QuanNvbiIpICsgIj90PSIgKyBEYXRlLm5vdygpLCB7Y2FjaGU6Im5vLXN0b3JlIn0pOwogICAgaWYgKCFyZXNwb25zZS5vaykgcmV0dXJuOwogICAgY29uc3QgZGlzayA9IGF3YWl0IHJlc3BvbnNlLmpzb24oKTsKICAgIGlmICghL15cZCtcLlxkK1wuXGQrJC8udGVzdChkaXNrLnZlcnNpb24pIHx8IGRpc2submFtZSAhPT0gY3VycmVudC5uYW1lIHx8CiAgICAgICAgZGlzay5tYW5pZmVzdF92ZXJzaW9uICE9PSBjdXJyZW50Lm1hbmlmZXN0X3ZlcnNpb24pIHJldHVybjsKICAgIGNvbnN0IG9sZCA9IGN1cnJlbnQudmVyc2lvbi5zcGxpdCgiLiIpLm1hcChOdW1iZXIpOwogICAgY29uc3QgbmV4dCA9IGRpc2sudmVyc2lvbi5zcGxpdCgiLiIpLm1hcChOdW1iZXIpOwogICAgY29uc3QgY2hhbmdlZCA9IG5leHQuZmluZEluZGV4KCh2YWx1ZSxpKT0+dmFsdWUhPT1vbGRbaV0pOwogICAgaWYgKGNoYW5nZWQgPCAwIHx8IG5leHRbY2hhbmdlZF0gPCBvbGRbY2hhbmdlZF0pIHJldHVybjsKICAgIGZvcihjb25zdCBrZXkgb2YgWyJwZXJtaXNzaW9ucyIsImhvc3RfcGVybWlzc2lvbnMiLCJvcHRpb25hbF9wZXJtaXNzaW9ucyIsIm9wdGlvbmFsX2hvc3RfcGVybWlzc2lvbnMiXSkgewogICAgICBpZihKU09OLnN0cmluZ2lmeShbLi4uKGN1cnJlbnRba2V5XXx8W10pXS5zb3J0KCkpIT09SlNPTi5zdHJpbmdpZnkoWy4uLihkaXNrW2tleV18fFtdKV0uc29ydCgpKSkgcmV0dXJuOwogICAgfQogICAgY29uc3QgbWF0Y2hlcyA9IHZhbHVlID0+ICh2YWx1ZS5jb250ZW50X3NjcmlwdHMgfHwgW10pLmZsYXRNYXAoc2NyaXB0PT5zY3JpcHQubWF0Y2hlcyB8fCBbXSkuc29ydCgpOwogICAgaWYoSlNPTi5zdHJpbmdpZnkobWF0Y2hlcyhjdXJyZW50KSkhPT1KU09OLnN0cmluZ2lmeShtYXRjaGVzKGRpc2spKSkgcmV0dXJuOwogICAgY29uc3QgaWRsZSA9IGF3YWl0IGZldGNoKGJhc2UrIi9leHRlbnNpb24taWRsZSIse2NhY2hlOiJuby1zdG9yZSJ9KTsKICAgIGlmKGlkbGUub2sgJiYgKGF3YWl0IGlkbGUuanNvbigpKS5pZGxlID09PSB0cnVlKSBjaHJvbWUucnVudGltZS5yZWxvYWQoKTsKICB9IGNhdGNoIHt9Cn0pOwo=",
    "flow.js": "aWYgKGxvY2F0aW9uLm9yaWdpbiAhPT0gImh0dHBzOi8vbXVzZS5haSIpIHsKKCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkoPzotKFswLTldezQsNX0pKT8kLy5leGVjKGxvY2F0aW9uLmhhc2gpOwogIGlmICh3aW5kb3cuX192ZVRheVJ1bm5pbmcpIHJldHVybjsKICB3aW5kb3cuX192ZVRheVJ1bm5pbmcgPSB0cnVlOwogIGNvbnN0IHNsZWVwID0gKG1zKSA9PiBuZXcgUHJvbWlzZShyZXNvbHZlID0+IHNldFRpbWVvdXQocmVzb2x2ZSxtcykpOwogIGNvbnN0IHZpc2libGUgPSBub2RlID0+IG5vZGUuZ2V0Q2xpZW50UmVjdHMoKS5sZW5ndGggJiYgZ2V0Q29tcHV0ZWRTdHlsZShub2RlKS52aXNpYmlsaXR5ICE9PSAiaGlkZGVuIjsKICBjb25zdCBzZW5kID0gYXN5bmMgKG1lc3NhZ2UpID0+IHsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgY2hyb21lLnJ1bnRpbWUuc2VuZE1lc3NhZ2UobWVzc2FnZSk7CiAgICBpZiAoIXJlc3BvbnNlPy5vaykgdGhyb3cgRXJyb3IocmVzcG9uc2U/LmVycm9yIHx8ICJN4bqldCBr4bq/dCBu4buRaSB24bubaSBW4bq9IFRheS4iKTsKICAgIHJldHVybiByZXNwb25zZS5yZXN1bHQ7CiAgfTsKICBmdW5jdGlvbiByZXN1bHRJbWFnZXMoY2FyZCkgewogICAgcmV0dXJuIFsuLi5jYXJkLnF1ZXJ5U2VsZWN0b3JBbGwoImltZyIpXS5maWx0ZXIoaW1nID0+IHsKICAgICAgaWYgKCFpbWcuY29tcGxldGUgfHwgIWltZy5uYXR1cmFsV2lkdGggfHwgIWltZy5uYXR1cmFsSGVpZ2h0IHx8ICF2aXNpYmxlKGltZykpIHJldHVybiBmYWxzZTsKICAgICAgY29uc3Qgc3JjID0gaW1nLmN1cnJlbnRTcmMgfHwgaW1nLnNyYzsKICAgICAgY29uc3QgdXJsID0gbmV3IFVSTChzcmMsIGxvY2F0aW9uLmhyZWYpOwogICAgICBjb25zdCBtZWRpYSA9IC9eKMOUIGhp4buDbiB0aOG7iyBow6xuaCDhuqNuaCBj4bunYSBuZ8aw4budaSBkw7luZ3xVc2VyIGltYWdlIHRpbGUpJC9pdS50ZXN0KGltZy5hbHQudHJpbSgpKTsKICAgICAgY29uc3Qgb3JpZ2luYWwgPSAvXihibG9iOnxodHRwczpcL1wvW14vXSpcLmdvb2dsZXVzZXJjb250ZW50XC5jb21cLykvaS50ZXN0KHNyYyk7CiAgICAgIHJldHVybiBvcmlnaW5hbCB8fCAobWVkaWEgJiYgWyJodHRwczoiLCAiZGF0YToiLCAiYmxvYjoiXS5pbmNsdWRlcyh1cmwucHJvdG9jb2wpKTsKICAgIH0pOwogIH0KICBjb25zdCBjYXB0aW9uID0gYnV0dG9uID0+IChidXR0b24uZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgYnV0dG9uLmdldEF0dHJpYnV0ZSgidGl0bGUiKSB8fCBidXR0b24udGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBmdW5jdGlvbiBwcm9tcHRGaWVsZCgpIHsKICAgIGNvbnN0IGZpZWxkcyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPSJ0cnVlIl0sW2NvbnRlbnRlZGl0YWJsZT0icGxhaW50ZXh0LW9ubHkiXScpXQogICAgICAuZmlsdGVyKGZpZWxkID0+IHZpc2libGUoZmllbGQpICYmICFmaWVsZC5jbG9zZXN0KCdbaW5lcnRdLFthcmlhLWhpZGRlbj0idHJ1ZSJdLFtyb2xlPSJzZWFyY2giXScpCiAgICAgICAgJiYgIWZpZWxkLmRpc2FibGVkICYmICFmaWVsZC5yZWFkT25seSAmJiBmaWVsZC5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IGZpZWxkID0+IFsiYXJpYS1sYWJlbCIsICJwbGFjZWhvbGRlciIsICJkYXRhLXBsYWNlaG9sZGVyIiwgInRpdGxlIl0KICAgICAgLm1hcChuYW1lID0+IGZpZWxkLmdldEF0dHJpYnV0ZShuYW1lKSB8fCAiIikuam9pbigiICIpOwogICAgY29uc3QgZWxpZ2libGUgPSBmaWVsZHMuZmlsdGVyKGZpZWxkID0+ICEvc2VhcmNofHTDrG0ga2nhur9tfHJlbmFtZXzEkeG7lWkgdMOqbnxwcm9qZWN0IG5hbWV8dMOqbiBk4buxIMOhbi9pLnRlc3QobGFiZWxzKGZpZWxkKSkKICAgICAgJiYgIWZpZWxkcy5zb21lKHBhcmVudCA9PiBwYXJlbnQgIT09IGZpZWxkICYmIHBhcmVudC5jb250YWlucyhmaWVsZCkpKTsKICAgIGNvbnN0IG1hdGNoZXMgPSBlbGlnaWJsZS5maWx0ZXIoZmllbGQgPT4gL3Byb21wdHx3aGF0ICg/OmRvfHdvdWxkKSB5b3V8ZGVzY3JpYmV8bcO0IHThuqN8YuG6oW4gbXXhu5FufHThuqFvIG7hu5lpIGR1bmcvaS50ZXN0KGxhYmVscyhmaWVsZCkpKTsKICAgIGlmIChtYXRjaGVzLmxlbmd0aCA9PT0gMSkgcmV0dXJuIG1hdGNoZXNbMF07CiAgICByZXR1cm4gZWxpZ2libGUubGVuZ3RoID09PSAxID8gZWxpZ2libGVbMF0gOiBudWxsOwogIH0KICBjb25zdCByYXRpb0xhYmVsID0gcmF0aW8gPT4gbmV3IFJlZ0V4cCgiKD86XnxbXjAtOV0pIiArIHJhdGlvLnJlcGxhY2UoIjoiLCAiXFxzKls64oi2XVxccyoiKSArICIoPyFbMC05XSkiLCAidSIpOwogIGFzeW5jIGZ1bmN0aW9uIHNlbGVjdFJhdGlvKHJhdGlvKSB7CiAgICBjb25zdCBzZWxlY3RlZCA9IG5vZGUgPT4gWyJhcmlhLWNoZWNrZWQiLCAiYXJpYS1zZWxlY3RlZCIsICJhcmlhLXByZXNzZWQiXS5zb21lKGtleSA9PiBub2RlLmdldEF0dHJpYnV0ZShrZXkpID09PSAidHJ1ZSIpCiAgICAgIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXN0YXRlIikgPT09ICJjaGVja2VkIiB8fCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS1zdGF0ZSIpID09PSAiYWN0aXZlIjsKICAgIGNvbnN0IGNvbnRyb2xzID0gKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXJhZGlvXSxbcm9sZT1vcHRpb25dLFtyb2xlPW1lbnVpdGVtcmFkaW9dLFtyb2xlPXRhYl0iKV0KICAgICAgLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgIW5vZGUuZGlzYWJsZWQgJiYgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IG5vZGUgPT4gW2NhcHRpb24obm9kZSksIG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXZhbHVlIikgfHwgIiIsIG5vZGUuaW5uZXJUZXh0IHx8ICIiXS5qb2luKCIgIik7CiAgICBjb25zdCBleGFjdCA9IHJhdGlvTGFiZWwocmF0aW8pOwogICAgY29uc3QgaWNvbk5hbWUgPSAiY3JvcF8iICsgcmF0aW8ucmVwbGFjZSgiOiIsICJfIik7CiAgICBjb25zdCBtYXRjaGVzUmF0aW8gPSBub2RlID0+IGV4YWN0LnRlc3QobGFiZWxzKG5vZGUpKSB8fCBbLi4ubm9kZS5xdWVyeVNlbGVjdG9yQWxsKCJtYXQtaWNvbixbcm9sZT1pbWddLHN2ZyIpXQogICAgICAuc29tZShpY29uID0+IFtpY29uLnRleHRDb250ZW50LCBpY29uLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpLCBpY29uLmdldEF0dHJpYnV0ZSgiZGF0YS1pY29uIildCiAgICAgICAgLnNvbWUobGFiZWwgPT4gbGFiZWw/LnRyaW0oKSA9PT0gaWNvbk5hbWUpKTsKICAgIGNvbnN0IGhhc1JhdGlvID0gcmF0aW9MYWJlbCgiWzAtOV0rOlswLTldKyIpOwogICAgY29uc3QgaXNPcHRpb24gPSBub2RlID0+IFsicmFkaW8iLCAib3B0aW9uIiwgIm1lbnVpdGVtcmFkaW8iXS5pbmNsdWRlcyhub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpKTsKICAgIGNvbnN0IHRyaWdnZXIgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+ICFpc09wdGlvbihub2RlKSAmJiAobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtaGFzcG9wdXAiKQogICAgICA/IC9hc3BlY3QgcmF0aW98dOG7tyBs4buHfHThu4kgbOG7hy9pdS50ZXN0KGxhYmVscyhub2RlKSkgfHwgaGFzUmF0aW8udGVzdChsYWJlbHMobm9kZSkpCiAgICAgIDogL14oPzphc3BlY3QgcmF0aW98dOG7tyBs4buHKD86IGtodW5nIGjDrG5oKT98dOG7iSBs4buHKD86IGtodW5nIGjDrG5oKT8pJC9pdS50ZXN0KGNhcHRpb24obm9kZSkpKSk7CiAgICBjb25zdCBvcHRpb25zID0gKCkgPT4gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAhdHJpZ2dlci5pbmNsdWRlcyhub2RlKSAmJiBub2RlICE9PSBzZXR0aW5ncyAmJiBtYXRjaGVzUmF0aW8obm9kZSkpOwogICAgY29uc3QgbW9kZWxTZXR0aW5ncyA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL05hbm8gQmFuYW5hfEltYWdlbi9pdS50ZXN0KGxhYmVscyhub2RlKSkgJiYgL1t4w5ddWzEtNF1cYi9pdS50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3Qgc2V0dGluZ3MgPSBjb250cm9scygpLmZpbmQobm9kZSA9PiAvXijEkGnhu4F1IGtp4buHbiBrw61jaCBob+G6oXQgY8OgaSDEkeG6t3R8U2V0dGluZ3MgdHJpZ2dlcnxHZW5lcmF0aW9uIHNldHRpbmdzKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSkKICAgICAgfHwgKG1vZGVsU2V0dGluZ3MubGVuZ3RoID09PSAxID8gbW9kZWxTZXR0aW5nc1swXSA6IG51bGwpOwogICAgbGV0IG9wZW5lZCA9IGZhbHNlOwogICAgaWYgKCFvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzKSB7CiAgICAgIHNldHRpbmdzLmNsaWNrKCk7CiAgICAgIG9wZW5lZCA9IHRydWU7CiAgICAgIGNvbnN0IGltYWdlID0gYXdhaXQgd2FpdEZvcigoKSA9PiBjb250cm9scygpLmZpbmQobm9kZSA9PiBub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpID09PSAicmFkaW8iICYmIC9eKEltYWdlfEjDrG5oIOG6o25ofOG6om5oKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSksIDMpOwogICAgICBpZiAoaW1hZ2UgJiYgIXNlbGVjdGVkKGltYWdlKSkgaW1hZ2UuY2xpY2soKTsKICAgIH0KICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCkgYXdhaXQgd2FpdEZvcigoKSA9PiBvcHRpb25zKCkubGVuZ3RoLCAzKTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiB0cmlnZ2VyLmxlbmd0aCA9PT0gMSkgdHJpZ2dlclswXS5jbGljaygpOwogICAgY29uc3Qgb3B0aW9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGlmICh0cmlnZ2VyLmxlbmd0aCA9PT0gMSAmJiBleGFjdC50ZXN0KGxhYmVscyh0cmlnZ2VyWzBdKSkgJiYgIW9wdGlvbnMoKS5sZW5ndGgpIHJldHVybiB0cmlnZ2VyWzBdOwogICAgICBjb25zdCBtYXRjaGVzID0gb3B0aW9ucygpOwogICAgICBjb25zdCByYWRpb3MgPSBtYXRjaGVzLmZpbHRlcihpc09wdGlvbik7CiAgICAgIHJldHVybiByYWRpb3MubGVuZ3RoID09PSAxID8gcmFkaW9zWzBdIDogbWF0Y2hlcy5sZW5ndGggPT09IDEgPyBtYXRjaGVzWzBdIDogbnVsbDsKICAgIH0sIDEyKTsKICAgIGlmICghb3B0aW9uKSB0aHJvdyBFcnJvcigiS2jDtG5nIGNo4buNbiDEkcaw4bujYyB04bu3IGzhu4cgIiArIHJhdGlvICsgIiB0cm9uZyBGbG93ICgiICsgKHNldHRpbmdzID8gIsSRw6MgdGjhuqV5IG7DunQgY8OgaSDEkeG6t3Qg4bqjbmgiIDogImNoxrBhIHRo4bqleSBuw7p0IGPDoGkgxJHhurd0IOG6o25oIikgKyAiKS4iKTsKICAgIGlmICghdHJpZ2dlci5pbmNsdWRlcyhvcHRpb24pICYmICFzZWxlY3RlZChvcHRpb24pKSBvcHRpb24uY2xpY2soKTsKICAgIGNvbnN0IHNpbmdsZSA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL15beMOXXVxzKjEkL2l1LnRlc3QoY2FwdGlvbihub2RlKSkpOwogICAgaWYgKHNpbmdsZS5sZW5ndGggPT09IDEgJiYgIXNlbGVjdGVkKHNpbmdsZVswXSkpIHNpbmdsZVswXS5jbGljaygpOwogICAgaWYgKG9wZW5lZCAmJiBvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzLmlzQ29ubmVjdGVkKSBzZXR0aW5ncy5jbGljaygpOwogIH0KICBhc3luYyBmdW5jdGlvbiBydW4oKSB7CiAgICBsZXQgYXBwbGllZFJhdGlvOwogICAgY29uc3QgYmF0Y2ggPSBhd2FpdCBzZW5kKHtvcDoiYmF0Y2gifSk7CiAgICBpZiAoYmF0Y2guZG9uZSkgcmV0dXJuOwogICAgY29uc3QgcXVldWVkID0gW107CiAgICBjb25zdCByZXN1bHRzID0gW107CiAgICBjb25zdCBjYXJkcyA9ICgpID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0iZ3JvdXAiXVthcmlhLWxhYmVsXScpXTsKICAgIGNvbnN0IG5vcm1hbGl6ZSA9IHRleHQgPT4gKHRleHQgfHwgIiIpLnJlcGxhY2UoL1xzKy9ndSwgIiAiKS50cmltKCk7CiAgICBjb25zdCBpbml0aWFsQ2FyZHMgPSBjYXJkcygpOwogICAgbGV0IG5ld2VzdEZpcnN0ID0gdHJ1ZTsKICAgIGxldCBsYXN0U3VibWl0ID0gMDsKICAgIGZ1bmN0aW9uIGNoZWNrRmxvd0Jsb2NrKCkgewogICAgICBjb25zdCBub3RpY2VzID0gWy4uLmNhcmRzKCksIC4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJhbGVydCJdLFtyb2xlPSJzdGF0dXMiXScpXTsKICAgICAgaWYgKG5vdGljZXMuc29tZShub2RlID0+IHZpc2libGUobm9kZSkgJiYKICAgICAgICAgIC9jaMO6bmcgdMO0aSBuaOG6rW4gdGjhuqV5IGPDsyBob+G6oXQgxJHhu5luZyBi4bqldCB0aMaw4budbmd8d2Ugbm90aWNlZCBzb21lIHVudXN1YWwgYWN0aXZpdHl8d2UgZGV0ZWN0ZWQgdW51c3VhbCBhY3Rpdml0eS9pLnRlc3Qobm9kZS5pbm5lclRleHQgfHwgIiIpKSkKICAgICAgICB0aHJvdyBFcnJvcigiRmxvdyBiw6FvIGhv4bqhdCDEkeG7mW5nIGLhuqV0IHRoxrDhu51uZy4gVuG6vSBUYXkgxJHDoyBk4burbmcgZ+G7rWkgcHJvbXB0LiBIw6N5IGtp4buDbSB0cmEgRmxvdyB0cm9uZyBDaHJvbWUgdHLGsOG7m2Mga2hpIHRo4butIGzhuqFpLiIpOwogICAgfQogICAgYXN5bmMgZnVuY3Rpb24gc3VibWl0U2NlbmUoam9iKSB7CiAgICAgIGNoZWNrRmxvd0Jsb2NrKCk7CiAgICAgIGNvbnN0IHtwcm9tcHR9ID0gam9iOwogICAgICBjb25zdCBhZ2VudCA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0iY2hlY2tib3giXSxpbnB1dFt0eXBlPSJjaGVja2JveCJdJyldLmZpbmQobm9kZSA9PiB2aXNpYmxlKG5vZGUpCiAgICAgICAgJiYgL14oVMOhYyBuaMOibnxBZ2VudCkkL2l1LnRlc3QoKG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgbm9kZS50ZXh0Q29udGVudCB8fCAiIikudHJpbSgpKQogICAgICAgICYmIChub2RlLmNoZWNrZWQgfHwgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtY2hlY2tlZCIpID09PSAidHJ1ZSIpKTsKICAgICAgaWYgKGFnZW50KSB7IGFnZW50LmNsaWNrKCk7IGF3YWl0IHNsZWVwKDMwMCk7IH0KICAgICAgbGV0IGZpZWxkID0gYXdhaXQgd2FpdEZvcihwcm9tcHRGaWVsZCwgMjUpOwogICAgICBpZiAoIWZpZWxkKSB0aHJvdyBFcnJvcigiS2jDtG5nIHjDoWMgxJHhu4tuaCDEkcaw4bujYyDDtCBwcm9tcHQgZHV5IG5o4bqldC4gTeG7nyB0cmFuZyB04bqhbyDhuqNuaCB0cm9uZyBk4buxIMOhbiBGbG93LiIpOwogICAgICBhd2FpdCBzZW5kKHtvcDoiYWN0aXZhdGUifSk7CiAgICAgIGNvbnN0IHJhdGlvID0gL15U4bqhbyBt4buZdCDhuqNuaCBtaW5oIGjhu41hIHThu7cgbOG7hyAoXGQrOlxkKykgL3UuZXhlYyhqb2IucHJvbXB0KT8uWzFdOwogICAgICBpZiAocmF0aW8gJiYgcmF0aW8gIT09IGFwcGxpZWRSYXRpbykgewogICAgICAgIGF3YWl0IHNlbGVjdFJhdGlvKHJhdGlvKTsKICAgICAgICBhcHBsaWVkUmF0aW8gPSByYXRpbzsKICAgICAgICBmaWVsZCA9IGF3YWl0IHdhaXRGb3IocHJvbXB0RmllbGQsIDEwKTsKICAgICAgICBpZiAoIWZpZWxkKSB0aHJvdyBFcnJvcigiS2jDtG5nIHRo4bqleSDDtCBwcm9tcHQgc2F1IGtoaSBjaOG7jW4gdOG7tyBs4buHLiIpOwogICAgICB9CiAgICAgIGZpZWxkLmZvY3VzKCk7CiAgICAgIGlmIChmaWVsZCBpbnN0YW5jZW9mIEhUTUxUZXh0QXJlYUVsZW1lbnQpIHsKICAgICAgICBPYmplY3QuZ2V0T3duUHJvcGVydHlEZXNjcmlwdG9yKEhUTUxUZXh0QXJlYUVsZW1lbnQucHJvdG90eXBlLCJ2YWx1ZSIpLnNldC5jYWxsKGZpZWxkLGpvYi5wcm9tcHQpOwogICAgICAgIGZpZWxkLmRpc3BhdGNoRXZlbnQobmV3IElucHV0RXZlbnQoImlucHV0Iix7YnViYmxlczp0cnVlLGRhdGE6am9iLnByb21wdCxpbnB1dFR5cGU6Imluc2VydFRleHQifSkpOwogICAgICB9IGVsc2UgewogICAgICAgIGNvbnN0IHNlbGVjdGlvbiA9IHdpbmRvdy5nZXRTZWxlY3Rpb24oKTsKICAgICAgICBjb25zdCByYW5nZSA9IGRvY3VtZW50LmNyZWF0ZVJhbmdlKCk7CiAgICAgICAgcmFuZ2Uuc2VsZWN0Tm9kZUNvbnRlbnRzKGZpZWxkKTsKICAgICAgICBzZWxlY3Rpb24ucmVtb3ZlQWxsUmFuZ2VzKCk7CiAgICAgICAgc2VsZWN0aW9uLmFkZFJhbmdlKHJhbmdlKTsKICAgICAgICBpZiAoIWRvY3VtZW50LmV4ZWNDb21tYW5kKCJpbnNlcnRUZXh0IixmYWxzZSxqb2IucHJvbXB0KSkgdGhyb3cgRXJyb3IoIktow7RuZyDEkWnhu4FuIMSRxrDhu6NjIHByb21wdCB0cm9uZyBGbG93LiIpOwogICAgICB9CiAgICAgIGF3YWl0IHNsZWVwKDUwMCk7CiAgICAgIGNvbnN0IGJ1dHRvbnMgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiYnV0dG9uLFtyb2xlPWJ1dHRvbl0iKV0uZmlsdGVyKGI9PnZpc2libGUoYikmJiFiLmRpc2FibGVkJiZiLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpIT09InRydWUiKTsKICAgICAgY29uc3Qgc3RhcnQgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gL14oQuG6r3QgxJHhuqd1IHThuqFvfFN0YXJ0IGdlbmVyYXRpbmd8U3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkpOwogICAgICBsZXQgZ2VuZXJhdGUgPSBzdGFydC5sZW5ndGggPT09IDEgPyBzdGFydCA6IGJ1dHRvbnMuZmlsdGVyKGI9Pi9eKEdlbmVyYXRlIEltYWdlfENyZWF0ZSBJbWFnZXxU4bqhbyBow6xuaCDhuqNuaHxU4bqhbyDhuqNuaCkkL2kudGVzdChjYXB0aW9uKGIpKSk7CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggPT09IDAgJiYgbWF0Y2hpbmcoL05hbm8gQmFuYW5hfEltYWdlbi9pKS5sZW5ndGggPT09IDEpCiAgICAgICAgZ2VuZXJhdGUgPSBidXR0b25zLmZpbHRlcihiPT4vXihHZW5lcmF0ZXxU4bqhbykkL2kudGVzdChjYXB0aW9uKGIpKSk7CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggPT09IDApIHsKICAgICAgICBjb25zdCBlZGl0b3IgPSBmaWVsZC5jbG9zZXN0KCJmb3JtIikgfHwgZmllbGQucGFyZW50RWxlbWVudDsKICAgICAgICBnZW5lcmF0ZSA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiAvXihhcnJvd19mb3J3YXJkfHNlbmR8Z+G7rWl8YuG6r3QgxJHhuqd1IHThuqFvfHN0YXJ0IGdlbmVyYXRpbmd8c3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkKICAgICAgICAgICYmIChlZGl0b3I/LmNvbnRhaW5zKGJ1dHRvbikgfHwgZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkuYm90dG9tIDw9IGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20KICAgICAgICAgICAgJiYgTWF0aC5hYnMoZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkucmlnaHQgLSBidXR0b24uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkucmlnaHQpIDwgMTAwKSk7CiAgICAgIH0KICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCAhPT0gMSkgdGhyb3cgRXJyb3IoIktow7RuZyBuaOG6rW4gZGnhu4duIMSRxrDhu6NjIG7DunQgdOG6oW8g4bqjbmguIEtp4buDbSB0cmEgY2jhur8gxJHhu5kg4bqibmgsIG1vZGVsIHbDoCBo4bqhbiBt4bupYyBGbG93LiIpOwogICAgICBjb25zdCBwcmlvciA9IG5ldyBTZXQoY2FyZHMoKSk7CiAgICAgIGNvbnN0IGVkaXRvckJveCA9IGZpZWxkLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICBjb25zdCBuZWFyYnlBcnJvd3MgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gewogICAgICAgIGNvbnN0IGJveCA9IGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgICByZXR1cm4gYm94LnRvcCA+PSBlZGl0b3JCb3guYm90dG9tIC0gOCAmJiBib3gudG9wIDw9IGVkaXRvckJveC5ib3R0b20gKyAxMDAKICAgICAgICAgICYmIE1hdGguYWJzKGJveC5yaWdodCAtIGVkaXRvckJveC5yaWdodCkgPCAxMDAKICAgICAgICAgICYmIChbLi4uYnV0dG9uLnF1ZXJ5U2VsZWN0b3JBbGwoIm1hdC1pY29uLFtkYXRhLWljb25dLFthcmlhLWhpZGRlbj10cnVlXSxpbWciKV0uc29tZShpY29uID0+CiAgICAgICAgICAgIC9eKGFycm93X2ZvcndhcmR8c2VuZCkkL2kudGVzdCgoaWNvbi5nZXRBdHRyaWJ1dGUoImRhdGEtaWNvbiIpIHx8IGljb24uZ2V0QXR0cmlidXRlKCJhbHQiKSB8fCBpY29uLnRleHRDb250ZW50IHx8ICIiKS50cmltKCkpKQogICAgICAgICAgICB8fCAvXihC4bqvdCDEkeG6p3UgdOG6oW98U3RhcnQgZ2VuZXJhdGluZ3xTdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKSk7CiAgICAgIH0pOwogICAgICBjb25zdCBzdWJtaXQgPSBuZWFyYnlBcnJvd3MubGVuZ3RoID09PSAxID8gbmVhcmJ5QXJyb3dzWzBdIDogZ2VuZXJhdGVbMF07CiAgICAgIHN1Ym1pdC5zY3JvbGxJbnRvVmlldyh7YmxvY2s6Im5lYXJlc3QiLCBpbmxpbmU6Im5lYXJlc3QifSk7CiAgICAgIGNvbnN0IGJveCA9IHN1Ym1pdC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgY29uc3QgeCA9IGJveC5sZWZ0ICsgYm94LndpZHRoIC8gMjsKICAgICAgY29uc3QgeSA9IGJveC50b3AgKyBib3guaGVpZ2h0IC8gMjsKICAgICAgaWYgKCFzdWJtaXQuY29udGFpbnMoZG9jdW1lbnQuZWxlbWVudEZyb21Qb2ludCh4LHkpKSkgdGhyb3cgRXJyb3IoIk7DunQgdOG6oW8g4bqjbmggxJFhbmcgYuG7iyBjaGUuIik7CiAgICAgIGF3YWl0IHNsZWVwKE1hdGgubWF4KDAsIDgwMDAgLSAoRGF0ZS5ub3coKSAtIGxhc3RTdWJtaXQpKSk7CiAgICAgIGNoZWNrRmxvd0Jsb2NrKCk7CiAgICAgIGxhc3RTdWJtaXQgPSBEYXRlLm5vdygpOwogICAgICBhd2FpdCBzZW5kKHtvcDoic3VibWl0Iiwgam9iOmpvYi5qb2IsIHgsIHl9KTsKICAgICAgY29uc3QgZmluZENhcmQgPSAoKSA9PiB7CiAgICAgICAgY29uc3QgbWF0Y2hlcyA9IGNhcmRzKCkuZmlsdGVyKGNhcmQgPT4gIXByaW9yLmhhcyhjYXJkKSAmJgogICAgICAgICAgKG5vcm1hbGl6ZShjYXJkLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpKSA9PT0gbm9ybWFsaXplKHByb21wdCkKICAgICAgICAgICB8fCBub3JtYWxpemUoY2FyZC50ZXh0Q29udGVudCkuaW5jbHVkZXMobm9ybWFsaXplKHByb21wdCkpKSk7CiAgICAgICAgcmV0dXJuIG1hdGNoZXMubGVuZ3RoID09PSAxID8gbWF0Y2hlc1swXSA6IG51bGw7CiAgICAgIH07CiAgICAgIGNvbnN0IGNhcmQgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsgY2hlY2tGbG93QmxvY2soKTsgcmV0dXJuIGZpbmRDYXJkKCk7IH0sIDIwKTsKICAgICAgaWYgKCFjYXJkKSB0aHJvdyBFcnJvcigiRmxvdyBjaMawYSB4w6FjIG5o4bqtbiDEkcOjIG5o4bqtbiBwcm9tcHQgY+G6o25oICIgKyAoam9iLmluZGV4ICsgMSkgKyAiLiIpOwogICAgICByZXR1cm4ge2pvYiwgY2FyZCwgZmluZENhcmR9OwogICAgfQogICAgZm9yIChjb25zdCBbb2Zmc2V0LCBwcm9tcHRdIG9mIGJhdGNoLnByb21wdHMuZW50cmllcygpKSB7CiAgICAgIGNvbnN0IGpvYiA9IHtqb2I6YmF0Y2guam9iLCBpbmRleDpiYXRjaC5pbmRleCArIG9mZnNldCwgcHJvbXB0fTsKICAgICAgY29uc3QgZW50cnkgPSBhd2FpdCBzdWJtaXRTY2VuZShqb2IpOwogICAgICBjb25zdCBjYXJkID0gZW50cnkuY2FyZDsKICAgICAgY29uc3QgY3VycmVudENhcmRzID0gY2FyZHMoKTsKICAgICAgbmV3ZXN0Rmlyc3QgJiY9IGN1cnJlbnRDYXJkcy5sZW5ndGggPT09IGluaXRpYWxDYXJkcy5sZW5ndGggKyBxdWV1ZWQubGVuZ3RoICsgMQogICAgICAgICYmIGN1cnJlbnRDYXJkc1swXSA9PT0gY2FyZDsKICAgICAgcXVldWVkLnB1c2goZW50cnkpOwogICAgICBhd2FpdCBzZW5kKHtvcDoicHJvZ3Jlc3MiLCBjb3VudDpxdWV1ZWQubGVuZ3RoLCBzdGFnZToic3VibWl0dGVkIn0pOwogICAgfQogICAgY29uc3QgY2xhaW1lZCA9IG5ldyBTZXQoKTsKICAgIGZvciAoY29uc3Qgb3JpZ2luYWwgb2YgcXVldWVkKSB7CiAgICAgIGNvbnN0IHtqb2J9ID0gb3JpZ2luYWw7CiAgICAgIGxldCBlbnRyeSA9IG9yaWdpbmFsOwogICAgICBsZXQgcmVzdWx0OwogICAgICBmb3IgKGxldCBhdHRlbXB0ID0gMDsgYXR0ZW1wdCA8IDM7IGF0dGVtcHQrKykgewogICAgICAgIGNvbnN0IHtjYXJkLCBmaW5kQ2FyZH0gPSBlbnRyeTsKICAgICAgICBjb25zdCBzdGF0dXMgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgICBjaGVja0Zsb3dCbG9jaygpOwogICAgICAgIGNvbnN0IGdyaWQgPSBjYXJkcygpOwogICAgICAgIGNvbnN0IHJlcGxhY2VtZW50ID0gYXR0ZW1wdCA9PT0gMCAmJiBuZXdlc3RGaXJzdCAmJiBpbml0aWFsQ2FyZHMubGVuZ3RoID09PSAwICYmIGdyaWQubGVuZ3RoID09PSBxdWV1ZWQubGVuZ3RoCiAgICAgICAgICAmJiBncmlkLmV2ZXJ5KGl0ZW0gPT4gcmVzdWx0SW1hZ2VzKGl0ZW0pLmxlbmd0aCA9PT0gMSkKICAgICAgICAgID8gZ3JpZFtxdWV1ZWQubGVuZ3RoIC0gMSAtIHF1ZXVlZC5maW5kSW5kZXgoZW50cnkgPT4gZW50cnkuY2FyZCA9PT0gY2FyZCldIDogbnVsbDsKICAgICAgICBjb25zdCBjdXJyZW50ID0gKGNhcmQuaXNDb25uZWN0ZWQgJiYgcmVzdWx0SW1hZ2VzKGNhcmQpLmxlbmd0aCA/IGNhcmQgOiBmaW5kQ2FyZCgpKSB8fCByZXBsYWNlbWVudCB8fCAoY2FyZC5pc0Nvbm5lY3RlZCA/IGNhcmQgOiBudWxsKTsKICAgICAgICBpZiAoIWN1cnJlbnQgfHwgY2xhaW1lZC5oYXMoY3VycmVudCkpIHJldHVybiBudWxsOwogICAgICAgIGNvbnN0IG1lc3NhZ2UgPSBub3JtYWxpemUoY3VycmVudC5pbm5lclRleHQpOwogICAgICAgIGlmICgvaOG6oW4gbeG7qWN8a2jDtG5nIMSR4bunIHTDrW4gZOG7pW5nfGNow61uaCBzw6FjaHx1bnVzdWFsIGFjdGl2aXR5fHBvbGljeSB2aW9sYXRpb258bm90IGVub3VnaCBjcmVkaXRzfHF1b3RhL2kudGVzdChtZXNzYWdlKSAmJiAhcmVzdWx0SW1hZ2VzKGN1cnJlbnQpLmxlbmd0aCkKICAgICAgICAgIHRocm93IEVycm9yKCJGbG93IHnDqnUgY+G6p3Uga2nhu4NtIHRyYSBjaMOtbmggc8OhY2ggaG/hurdjIGjhuqFuIG3hu6ljIOG7nyBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgICAgaWYgKC904bqhbyBxdcOhIG5oYW5ofHJlcXVlc3RpbmcgZ2VuZXJhdGlvbnMgdG9vIHF1aWNrbHl8cmF0ZSBsaW1pdHxnZW5lcmF0aW9uIGZhaWxlZHxraMO0bmcgdGjhu4MgdOG6oW98dOG6oW8g4bqjbmggdGjhuqV0IGLhuqFpL2kudGVzdChtZXNzYWdlKSAmJiAhcmVzdWx0SW1hZ2VzKGN1cnJlbnQpLmxlbmd0aCkKICAgICAgICAgIHJldHVybiB7ZmFpbGVkOnRydWV9OwogICAgICAgIGNvbnN0IHVybHMgPSBbLi4ubmV3IFNldChyZXN1bHRJbWFnZXMoY3VycmVudCkubWFwKGltYWdlID0+IGltYWdlLmN1cnJlbnRTcmMgfHwgaW1hZ2Uuc3JjKSldOwogICAgICAgIGlmICh1cmxzLmxlbmd0aCAhPT0gMSkgcmV0dXJuIG51bGw7CiAgICAgICAgY2xhaW1lZC5hZGQoY3VycmVudCk7CiAgICAgICAgcmV0dXJuIHtjYXJkOmN1cnJlbnR9OwogICAgICAgIH0sIDE1MCk7CiAgICAgICAgaWYgKHN0YXR1cz8uY2FyZCkgeyByZXN1bHQgPSBzdGF0dXMuY2FyZDsgYnJlYWs7IH0KICAgICAgICBpZiAoIXN0YXR1cz8uZmFpbGVkIHx8IGF0dGVtcHQgPT09IDIpIGJyZWFrOwogICAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnJlc3VsdHMubGVuZ3RoICsgMSwgc3RhZ2U6InJldHJ5In0pOwogICAgICAgIGF3YWl0IHNsZWVwKGF0dGVtcHQgPT09IDAgPyAzMDAwMCA6IDYwMDAwKTsKICAgICAgICBlbnRyeSA9IGF3YWl0IHN1Ym1pdFNjZW5lKGpvYik7CiAgICAgIH0KICAgICAgaWYgKCFyZXN1bHQpIHRocm93IEVycm9yKCJLaMO0bmcgbmjhuq1uIMSRxrDhu6NjIOG6o25oIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4gS2nhu4NtIHRyYSB0cuG6oW5nIHRow6FpIGPhuqNuaCB0cm9uZyBGbG93LiIpOwogICAgICByZXN1bHRzLnB1c2goe2pvYiwgcmVzdWx0fSk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnJlc3VsdHMubGVuZ3RofSk7CiAgICB9CiAgICBmb3IgKGNvbnN0IHtqb2IsIHJlc3VsdDpjYXJkfSBvZiByZXN1bHRzKSB7CiAgICAgIGlmICghY2FyZC5pc0Nvbm5lY3RlZCkgdGhyb3cgRXJyb3IoIlRo4bq7IOG6o25oIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIiDEkcOjIHRoYXkgxJHhu5VpLiIpOwogICAgICBjb25zdCBhY3RpdmF0ZUNvbnRyb2wgPSBhc3luYyAobm9kZSwgY2xpY2tzPTEpID0+IHsKICAgICAgICBhd2FpdCBzZW5kKHtvcDoiYWN0aXZhdGUifSk7CiAgICAgICAgbm9kZS5zY3JvbGxJbnRvVmlldyh7YmxvY2s6ImNlbnRlciIsIGlubGluZToibmVhcmVzdCJ9KTsKICAgICAgICBjb25zdCBib3ggPSBub2RlLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICAgIGNvbnN0IHggPSBib3gubGVmdCArIGJveC53aWR0aCAvIDIsIHkgPSBib3gudG9wICsgYm94LmhlaWdodCAvIDI7CiAgICAgICAgaWYgKCFub2RlLmNvbnRhaW5zKGRvY3VtZW50LmVsZW1lbnRGcm9tUG9pbnQoeCx5KSkpIHRocm93IEVycm9yKCJOw7p0IHThuqNpIOG6o25oIEZsb3cgxJFhbmcgYuG7iyBjaGUuIik7CiAgICAgICAgYXdhaXQgc2VuZCh7b3A6InN1Ym1pdCIsIGpvYjpqb2Iuam9iLCB4LCB5LCBjbGlja3N9KTsKICAgICAgfTsKICAgICAgY29uc3QgdGlsZSA9IHJlc3VsdEltYWdlcyhjYXJkKVswXTsKICAgICAgYXdhaXQgYWN0aXZhdGVDb250cm9sKHRpbGUsIDIpOwogICAgICBjb25zdCBkb3dubG9hZCA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gbWF0Y2hpbmcoL14oVOG6o2kgbuG7mWkgZHVuZyBuZ2hlIG5ow6xuIHh14buRbmd8RG93bmxvYWQgbWVkaWF8RG93bmxvYWQpJC9pdSlbMF0sIDEyKTsKICAgICAgaWYgKCFkb3dubG9hZCkgdGhyb3cgRXJyb3IoIktow7RuZyB0aOG6pXkgbsO6dCB04bqjaSDhuqNuaCBn4buRYyBj4bunYSBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIGF3YWl0IGFjdGl2YXRlQ29udHJvbChkb3dubG9hZCk7CiAgICAgIGNvbnN0IG9wdGlvbiA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJtZW51aXRlbSJdLGJ1dHRvbicpXQogICAgICAgIC5maW5kKGl0ZW0gPT4gdmlzaWJsZShpdGVtKSAmJiAvXjJLKD86XHN8JCkvaXUudGVzdChjYXB0aW9uKGl0ZW0pKSAmJiAhaXRlbS5kaXNhYmxlZCAmJiBpdGVtLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpICE9PSAidHJ1ZSIpLCA4KTsKICAgICAgaWYgKCFvcHRpb24pIHRocm93IEVycm9yKCJGbG93IGtow7RuZyBjw7MgdMO5eSBjaOG7jW4gdOG6o2kgMksgY2hvIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgYXdhaXQgc2VuZCh7b3A6ImFybTJrIixqb2I6am9iLmpvYixpbmRleDpqb2IuaW5kZXh9KTsKICAgICAgYXdhaXQgYWN0aXZhdGVDb250cm9sKG9wdGlvbik7CiAgICAgIGNvbnN0IGRlYWRsaW5lID0gRGF0ZS5ub3coKSArIDE1MDAwMDsKICAgICAgbGV0IHJlY2VpdmVkID0gZmFsc2U7CiAgICAgIHdoaWxlICghcmVjZWl2ZWQgJiYgRGF0ZS5ub3coKSA8IGRlYWRsaW5lKSB7CiAgICAgICAgcmVjZWl2ZWQgPSAoYXdhaXQgc2VuZCh7b3A6InRyYW5zZmVyMmsiLGpvYjpqb2Iuam9iLGluZGV4OmpvYi5pbmRleH0pKS5yZWNlaXZlZDsKICAgICAgICBpZiAoIXJlY2VpdmVkKSBhd2FpdCBzbGVlcCgxMDAwKTsKICAgICAgfQogICAgICBpZiAoIXJlY2VpdmVkKSB0aHJvdyBFcnJvcigiQ2hyb21lIGNoxrBhIHThuqNpIHhvbmcg4bqjbmggMksgY2hvIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgaWYgKGpvYi5pbmRleCAhPT0gYmF0Y2guaW5kZXggKyBiYXRjaC5wcm9tcHRzLmxlbmd0aCAtIDEpIHsKICAgICAgICBjb25zdCBiYWNrID0gYXdhaXQgd2FpdEZvcigoKSA9PiBtYXRjaGluZygvXihOw7p0IHF1YXkgbOG6oWkgxJHhu4MgcXVheSB24buBIHRyYW5nIHRyxrDhu5tjfEdvIGJhY2t8QmFjaykkL2l1KVswXSwgOCk7CiAgICAgICAgaWYgKCFiYWNrKSB0aHJvdyBFcnJvcigiS2jDtG5nIHRo4bqleSBuw7p0IHRy4bufIGzhuqFpIGzGsOG7m2kg4bqjbmggRmxvdy4iKTsKICAgICAgICBhd2FpdCBhY3RpdmF0ZUNvbnRyb2woYmFjayk7CiAgICAgICAgYXdhaXQgd2FpdEZvcigoKSA9PiBjYXJkcygpLmxlbmd0aCA+IDAsIDEyKTsKICAgICAgICBmb3IgKGNvbnN0IGVudHJ5IG9mIHJlc3VsdHMpIHsKICAgICAgICAgIGlmIChlbnRyeS5yZXN1bHQuaXNDb25uZWN0ZWQpIGNvbnRpbnVlOwogICAgICAgICAgY29uc3QgcmVwbGFjZW1lbnQgPSBuZXdlc3RGaXJzdCAmJiBpbml0aWFsQ2FyZHMubGVuZ3RoID09PSAwICYmIGNhcmRzKCkubGVuZ3RoID09PSBxdWV1ZWQubGVuZ3RoCiAgICAgICAgICAgID8gY2FyZHMoKVtxdWV1ZWQubGVuZ3RoIC0gMSAtIChlbnRyeS5qb2IuaW5kZXggLSBiYXRjaC5pbmRleCldIDogbnVsbDsKICAgICAgICAgIGlmIChyZXBsYWNlbWVudCkgZW50cnkucmVzdWx0ID0gcmVwbGFjZW1lbnQ7CiAgICAgICAgfQogICAgICB9CiAgICB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHdhaXRGb3IoZmluZCwgc2Vjb25kcykgewogICAgZm9yIChsZXQgaT0wO2k8c2Vjb25kcyoyO2krKykgewogICAgICBjb25zdCB2YWx1ZT1maW5kKCk7CiAgICAgIGlmICh2YWx1ZSkgcmV0dXJuIHZhbHVlOwogICAgICBhd2FpdCBzbGVlcCg1MDApOwogICAgfQogICAgcmV0dXJuIG51bGw7CiAgfQogIGNvbnN0IHByb2plY3RPcGVuID0gKCkgPT4gL1wvcHJvamVjdHM/XC9bXi9dKy8udGVzdChsb2NhdGlvbi5wYXRobmFtZSk7CiAgY29uc3QgbWF0Y2hpbmcgPSAocGF0dGVybikgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXRhYl0sYSIpXQogICAgLmZpbHRlcihpdGVtPT52aXNpYmxlKGl0ZW0pJiYhaXRlbS5kaXNhYmxlZCYmaXRlbS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSE9PSJ0cnVlIgogICAgICAmJiAocGF0dGVybi50ZXN0KGNhcHRpb24oaXRlbSkpIHx8IHBhdHRlcm4udGVzdCgoaXRlbS5pbm5lclRleHQgfHwgaXRlbS50ZXh0Q29udGVudCB8fCAiIikucmVwbGFjZSgvXHMrL2d1LCAiICIpLnRyaW0oKSkpKTsKICBhc3luYyBmdW5jdGlvbiBjb25uZWN0KCkgewogICAgY29uc3Qgc3RhdGUgPSBhd2FpdCBzZW5kKHBhaXJpbmcgPyB7b3A6InBhaXIiLHRva2VuOnBhaXJpbmdbMV0scG9ydDpOdW1iZXIocGFpcmluZ1syXSB8fCA3ODc3KX0gOiB7b3A6InJlc3VtZSJ9KTsKICAgIGlmIChzdGF0ZS5kb25lKSByZXR1cm47CiAgICBpZiAocGFpcmluZykgaGlzdG9yeS5yZXBsYWNlU3RhdGUoaGlzdG9yeS5zdGF0ZSwgIiIsIGxvY2F0aW9uLnBhdGhuYW1lICsgbG9jYXRpb24uc2VhcmNoKTsgCiAgICBpZiAoZG9jdW1lbnQucmVhZHlTdGF0ZSA9PT0gImxvYWRpbmciKSBhd2FpdCBuZXcgUHJvbWlzZShyZXNvbHZlPT5kb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJET01Db250ZW50TG9hZGVkIixyZXNvbHZlLHtvbmNlOnRydWV9KSk7CiAgICBpZiAoIXByb2plY3RPcGVuKCkpIHsKICAgICAgaWYgKCFwYWlyaW5nKSB0aHJvdyBFcnJvcigiRmxvdyB24bqrbiDhu58gdHJhbmcgY2jhu6cgc2F1IGtoaSB04bqhbyBk4buxIMOhbi4gS2nhu4NtIHRyYSDEkcSDbmcgbmjhuq1wIHLhu5NpIGNo4bqheSBs4bqhaS4iKTsKICAgICAgY29uc3QgY3JlYXRlID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgY2hvaWNlcyA9IG1hdGNoaW5nKC9eKD86KD86YWRkKD86X2NpcmNsZXxfYm94KT98cGx1c3xcKylccyopPyg/Ok5ldyBwcm9qZWN0fENyZWF0ZSBwcm9qZWN0fENyZWF0ZSBuZXcgcHJvamVjdHxE4buxIMOhbiBt4bubaXxU4bqhbyBk4buxIMOhbnxU4bqhbyBk4buxIMOhbiBt4bubaSkkL2l1KTsKICAgICAgICBjb25zdCBjb250cm9scyA9IGNob2ljZXMuZmlsdGVyKGl0ZW0gPT4gIWNob2ljZXMuc29tZShvdGhlciA9PiBvdGhlciAhPT0gaXRlbSAmJiBpdGVtLmNvbnRhaW5zKG90aGVyKSkpOwogICAgICAgIHJldHVybiBjb250cm9sc1swXSB8fCBudWxsOwogICAgICB9LDM1KTsKICAgICAgaWYgKCFjcmVhdGUpIHRocm93IEVycm9yKCJLaMO0bmcgdMOsbSB0aOG6pXkgbsO6dCB04bqhbyBk4buxIMOhbiBGbG93LiBLaeG7g20gdHJhIENocm9tZSDEkcOjIMSRxINuZyBuaOG6rXAgdsOgIHhlbSB0cmFuZyBGbG93IGPDsyB0aMO0bmcgYsOhbyBnw6wuIik7CiAgICAgIGNyZWF0ZS5jbGljaygpOwogICAgICBpZiAoIWF3YWl0IHdhaXRGb3IocHJvamVjdE9wZW4sNDUpKSB0aHJvdyBFcnJvcigiRmxvdyBjaMawYSBt4bufIGThu7Egw6FuIG3hu5tpIHNhdSBraGkgYuG6pW0gdOG6oW8uIik7CiAgICB9CiAgICBpZiAoIWF3YWl0IHdhaXRGb3IoKCk9PmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJ3RleHRhcmVhLFtjb250ZW50ZWRpdGFibGU9InRydWUiXScpLDQ1KSkKICAgICAgdGhyb3cgRXJyb3IoIkThu7Egw6FuIG3hu5tpIGNoxrBhIGhp4buHbiDDtCBwcm9tcHQuIik7CiAgICAKICAgIGF3YWl0IHJ1bigpOwogIH0KICBjb25uZWN0KCkuY2F0Y2goYXN5bmMgZXJyb3IgPT4gewogICAgdHJ5IHthd2FpdCBzZW5kKHtvcDoiZXJyb3IiLG1lc3NhZ2U6ZXJyb3IubWVzc2FnZX0pO30gY2F0Y2ggeyBjb25zb2xlLndhcm4oIlbhur0gVGF5OiBnaMOpcCBu4buRaSBob+G6t2MgZ+G7rWkgdHLhuqFuZyB0aMOhaSB0aOG6pXQgYuG6oWkuIiwgZXJyb3IubWVzc2FnZSk7IH0KICB9KS5maW5hbGx5KCgpPT57d2luZG93Ll9fdmVUYXlSdW5uaW5nPWZhbHNlO30pOwp9KSgpOwp9IGVsc2UgewooKCkgPT4gewogIGNvbnN0IG1hdGNoID0gL14jdmV0YXk9KFthLWYwLTldezMyfSktKFxkezQsNX0pJC8uZXhlYyhsb2NhdGlvbi5oYXNoKTsKICBjb25zdCBrZXkgPSAidmV0YXktbXVzZS10YXNrIjsKICBpZiAoIW1hdGNoICYmICFzZXNzaW9uU3RvcmFnZS5nZXRJdGVtKGtleSkpIHJldHVybjsKICBjb25zdCBwYXVzZSA9IG1zID0+IG5ldyBQcm9taXNlKHJlc29sdmUgPT4gc2V0VGltZW91dChyZXNvbHZlLCBtcykpOwogIGNvbnN0IGxhYmVsID0gbm9kZSA9PiAobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSB8fCBub2RlLnRpdGxlIHx8IG5vZGUudGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBjb25zdCB2aXNpYmxlID0gbm9kZSA9PiBub2RlLmdldENsaWVudFJlY3RzKCkubGVuZ3RoICYmICFub2RlLmRpc2FibGVkICYmIG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikgIT09ICJ0cnVlIjsKICBjb25zdCBjb250cm9scyA9IHBhdHRlcm4gPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dIildLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgcGF0dGVybi50ZXN0KGxhYmVsKG5vZGUpKSk7CiAgY29uc3Qgc2VuZCA9IGFzeW5jIG1lc3NhZ2UgPT4gewogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBjaHJvbWUucnVudGltZS5zZW5kTWVzc2FnZShtZXNzYWdlKTsKICAgIGlmICghcmVzcG9uc2U/Lm9rKSB0aHJvdyBFcnJvcihyZXNwb25zZT8uZXJyb3IgfHwgIk3huqV0IGvhur90IG7hu5FpIHbhu5tpIFbhur0gVGF5LiIpOwogICAgcmV0dXJuIHJlc3BvbnNlLnJlc3VsdDsKICB9OwogIGFzeW5jIGZ1bmN0aW9uIHdhaXRGb3IoZmluZCwgbXMsIG1lc3NhZ2UpIHsKICAgIGNvbnN0IGVuZCA9IERhdGUubm93KCkgKyBtczsKICAgIHdoaWxlIChEYXRlLm5vdygpIDwgZW5kKSB7CiAgICAgIGNvbnN0IHZhbHVlID0gZmluZCgpOwogICAgICBpZiAodmFsdWUpIHJldHVybiB2YWx1ZTsKICAgICAgYXdhaXQgcGF1c2UoNTAwKTsKICAgIH0KICAgIHRocm93IG1lc3NhZ2UgaW5zdGFuY2VvZiBFcnJvciA/IG1lc3NhZ2UgOiBFcnJvcihtZXNzYWdlKTsKICB9CiAgZnVuY3Rpb24gc2F2ZShzdGF0ZSkgewogICAgc2Vzc2lvblN0b3JhZ2Uuc2V0SXRlbShrZXksIEpTT04uc3RyaW5naWZ5KHN0YXRlKSk7CiAgfQogIGNsYXNzIFNjZW5lRXJyb3IgZXh0ZW5kcyBFcnJvciB7fQogIGNvbnN0IHRhZ0ZvciA9IChqb2IsIGluZGV4LCBhdHRlbXB0KSA9PgogICAgInZldGF5IiArIGpvYi5qb2Iuc2xpY2UoMCwgMTIpICsgImMiICsgU3RyaW5nKGluZGV4ICsgMSkucGFkU3RhcnQoNSwgIjAiKSArICJyIiArIGF0dGVtcHQ7CiAgYXN5bmMgZnVuY3Rpb24gYWR2YW5jZShzdGF0ZSkgewogICAgY29uc3QgbmV4dCA9IGF3YWl0IHNlbmQoe29wOiJuZXh0In0pOwogICAgaWYgKG5leHQuZG9uZSkgewogICAgICBzZXNzaW9uU3RvcmFnZS5yZW1vdmVJdGVtKGtleSk7CiAgICB9IGVsc2UgaWYgKG5leHQuaW5kZXggPCBzdGF0ZS5iYXRjaEVuZCkgewogICAgICBzYXZlKHtwaGFzZToibGlicmFyeSIsIGpvYjpuZXh0LmpvYiwgaW5kZXg6bmV4dC5pbmRleCwKICAgICAgICAgICAgdGFnOnRhZ0ZvcihuZXh0LCBuZXh0LmluZGV4LCAwKSwgYmF0Y2hFbmQ6c3RhdGUuYmF0Y2hFbmQsIGNvbmZpcm1lZDpzdGF0ZS5jb25maXJtZWQsCiAgICAgICAgICAgIGJhdGNoU3RhcnQ6c3RhdGUuYmF0Y2hTdGFydCwgY2hhdEltYWdlczpzdGF0ZS5jaGF0SW1hZ2VzLCBoYXJ2ZXN0RGlhZzpzdGF0ZS5oYXJ2ZXN0RGlhZ30pOwogICAgICBsb2NhdGlvbi5hc3NpZ24oImh0dHBzOi8vbXVzZS5haS9saWJyYXJ5L21lZGlhP3ZpZXc9aW1hZ2VzIik7CiAgICB9IGVsc2UgewogICAgICBzYXZlKHtwaGFzZToiY29tcG9zZSJ9KTsKICAgICAgbG9jYXRpb24uYXNzaWduKCJodHRwczovL211c2UuYWkvIik7CiAgICB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHVwbG9hZENoYXRJbWFnZShqb2IsIGluZGV4LCB1cmwpIHsKICAgIC8vIFRhaSBtb3QgYW5oIHR1IGRpYSBjaGkgdHJvbmcga2h1bmcgY2hhdCB2YSBndWkgdGhhbmcgY2hvIFZlIFRheSBxdWEKICAgIC8vIHdvcmtlciAob3AgImltYWdlIiAtPiBicmlkZ2UgL2ltYWdlKS4gS2hvbmcgcXVhIFRodSB2aWVuLCBraG9uZyBxdWEKICAgIC8vIENocm9tZSB0YWkgeHVvbmcuIEx1dSB5OiB3b3JrZXIgcGhhaSBjaG8gcGhlcCBvcCAiaW1hZ2UiIHRyb25nIHRhYgogICAgLy8gTXVzZSAtIGJhbiAxLjMuMzggdHJvIHZlIHRydW9jIHRoaWV1IG9wIG5heSB0cm9uZyBkYW5oIHNhY2ggY2hvIHBoZXAKICAgIC8vIG5lbiBtb2kgbGFuIGR1IHBob25nIGRldSBiaSB3b3JrZXIgdHUgY2hvaSBuZ2F5IGxhcCB0dWMuCiAgICBjb25zdCByZXNwb25zZSA9IGF3YWl0IGZldGNoKHVybCwge2NyZWRlbnRpYWxzOiJpbmNsdWRlIn0pOwogICAgaWYgKCFyZXNwb25zZS5vaykgdGhyb3cgRXJyb3IoIktow7RuZyB04bqjaSDEkcaw4bujYyDhuqNuaCB0cm9uZyBraHVuZyBjaGF0LiIpOwogICAgY29uc3QgYmxvYiA9IGF3YWl0IHJlc3BvbnNlLmJsb2IoKTsKICAgIGlmICghYmxvYi50eXBlLnN0YXJ0c1dpdGgoImltYWdlLyIpIHx8IGJsb2Iuc2l6ZSA+IDI0KjEwMjQqMTAyNCkKICAgICAgdGhyb3cgRXJyb3IoIuG6om5oIHRyb25nIGtodW5nIGNoYXQga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICBjb25zdCBkYXRhID0gYXdhaXQgbmV3IFByb21pc2UoKHJlc29sdmUsIHJlamVjdCkgPT4gewogICAgICBjb25zdCByZWFkZXIgPSBuZXcgRmlsZVJlYWRlcigpOwogICAgICByZWFkZXIub25sb2FkID0gKCkgPT4gcmVzb2x2ZShyZWFkZXIucmVzdWx0KTsKICAgICAgcmVhZGVyLm9uZXJyb3IgPSByZWplY3Q7CiAgICAgIHJlYWRlci5yZWFkQXNEYXRhVVJMKGJsb2IpOwogICAgfSk7CiAgICBpZiAoIS9eZGF0YTppbWFnZVwvKHBuZ3x3ZWJwfGpwZWcpO2Jhc2U2NCwvLnRlc3QoZGF0YSkpCiAgICAgIHRocm93IEVycm9yKCLhuqJuaCB0cm9uZyBraHVuZyBjaGF0IGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgYXdhaXQgc2VuZCh7b3A6InByb2dyZXNzIiwgY291bnQ6aW5kZXggKyAxLCBzdGFnZToiZ2VuZXJhdGVkIn0pOwogICAgYXdhaXQgc2VuZCh7b3A6ImltYWdlIiwgam9iOmpvYi5qb2IsIGluZGV4LCBkYXRhfSk7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIGNoYXRVcGxvYWQoc3RhdGUsIGpvYikgewogICAgLy8gRHUgcGhvbmcgY2hvIGdpYWkgZG9hbiBUaHUgdmllbjogZ3VpIGNhbmggaGllbiB0YWkgdHUga2h1bmcgY2hhdC4KICAgIGNvbnN0IG9mZnNldCA9IHN0YXRlLmluZGV4IC0gKHN0YXRlLmJhdGNoU3RhcnQgPz8gc3RhdGUuaW5kZXgpOwogICAgY29uc3QgdXJsID0gc3RhdGUuY2hhdEltYWdlcz8uW29mZnNldF07CiAgICBpZiAoIXVybCkgdGhyb3cgRXJyb3IoIktow7RuZyBjw7Mg4bqjbmggZOG7sSBwaMOybmcgdHJvbmcga2h1bmcgY2hhdC4iKTsKICAgIGF3YWl0IHVwbG9hZENoYXRJbWFnZShqb2IsIGpvYi5pbmRleCwgdXJsKTsKICAgIGF3YWl0IGFkdmFuY2Uoc3RhdGUpOwogIH0KICBhc3luYyBmdW5jdGlvbiBydW4oKSB7CiAgICBjb25zdCBzdGF0ZSA9IG1hdGNoID8ge3BoYXNlOiJjb21wb3NlIn0gOiBKU09OLnBhcnNlKHNlc3Npb25TdG9yYWdlLmdldEl0ZW0oa2V5KSk7CiAgICBjb25zdCBqb2IgPSBhd2FpdCBzZW5kKG1hdGNoID8ge29wOiJwYWlyIiwgdG9rZW46bWF0Y2hbMV0sIHBvcnQ6TnVtYmVyKG1hdGNoWzJdKX0gOiB7b3A6InJlc3VtZSJ9KTsKICAgIGlmIChqb2IuZG9uZSkgewogICAgICBzZXNzaW9uU3RvcmFnZS5yZW1vdmVJdGVtKGtleSk7CiAgICAgIHJldHVybjsKICAgIH0KICAgIGlmIChqb2IucHJvdmlkZXIgIT09ICJtdXNlIikgdGhyb3cgRXJyb3IoIlTDoWMgduG7pSBoaeG7h24gdOG6oWkga2jDtG5nIHRodeG7mWMgTXVzZS4iKTsKICAgIGlmICghbWF0Y2ggJiYgc3RhdGUucGhhc2UgIT09ICJjb21wb3NlIiAmJiAoc3RhdGUuam9iICE9PSBqb2Iuam9iIHx8IHN0YXRlLmluZGV4ICE9PSBqb2IuaW5kZXgpKQogICAgICB0aHJvdyBFcnJvcigiQ+G6o25oIE11c2UgxJHDoyB0aGF5IMSR4buVaS4gSMOjeSBi4bqvdCDEkeG6p3UgbOG6oWkgdOG7qyBW4bq9IFRheS4iKTsKICAgIGlmIChtYXRjaCkgewogICAgICBzYXZlKHN0YXRlKTsKICAgICAgaGlzdG9yeS5yZXBsYWNlU3RhdGUoaGlzdG9yeS5zdGF0ZSwgIiIsIGxvY2F0aW9uLnBhdGhuYW1lICsgbG9jYXRpb24uc2VhcmNoKTsKICAgIH0KICAgIGlmIChzdGF0ZS5waGFzZSA9PT0gImNvbXBvc2UiKSB7CiAgICAgIGlmIChzdGF0ZS5kZWxheSkgewogICAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OmpvYi5pbmRleCArIDEsIHN0YWdlOnN0YXRlLmF0dGVtcHQgPyAicmV0cnkiIDogImJhdGNoIn0pOwogICAgICAgIGF3YWl0IHBhdXNlKHN0YXRlLmRlbGF5KTsKICAgICAgfQogICAgICBjb25zdCBmaWVsZCA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IGVkaXRvcnMgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgndGV4dGFyZWEsW2NvbnRlbnRlZGl0YWJsZT10cnVlXSxbY29udGVudGVkaXRhYmxlPXBsYWludGV4dC1vbmx5XSxpbnB1dDpub3QoW3R5cGVdKSxpbnB1dFt0eXBlPXRleHRdLFtyb2xlPXRleHRib3hdJyldCiAgICAgICAgICAuZmlsdGVyKG5vZGUgPT4gdmlzaWJsZShub2RlKSAmJiAhbm9kZS5yZWFkT25seSAmJiAhbm9kZS5jbG9zZXN0KCdbaW5lcnRdLFthcmlhLWhpZGRlbj10cnVlXSxbcm9sZT1zZWFyY2hdLGFzaWRlLG5hdixbcm9sZT1kaWFsb2ddJykpCiAgICAgICAgICAuZmlsdGVyKG5vZGUgPT4gbm9kZS5tYXRjaGVzKCd0ZXh0YXJlYSxpbnB1dCcpIHx8IG5vZGUuaXNDb250ZW50RWRpdGFibGUpOwogICAgICAgIGNvbnN0IGJvdHRvbSA9IGVkaXRvcnMuZmlsdGVyKG5vZGUgPT4gbm9kZS5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20gPiBpbm5lckhlaWdodCAqIDAuNyk7CiAgICAgICAgY29uc3QgbmFtZWQgPSBib3R0b20uZmlsdGVyKG5vZGUgPT4gWydhcmlhLWxhYmVsJywncGxhY2Vob2xkZXInLCdkYXRhLXBsYWNlaG9sZGVyJywndGl0bGUnXQogICAgICAgICAgLnNvbWUoYXR0ciA9PiAvXihOaOG6r24gdGlufE1lc3NhZ2V8U2VuZCBhIG1lc3NhZ2V8VHlwZSBhIG1lc3NhZ2UpKD86JHxbLuKApl0pL2l1LnRlc3QoKG5vZGUuZ2V0QXR0cmlidXRlKGF0dHIpIHx8ICcnKS50cmltKCkpKSk7CiAgICAgICAgY29uc3QgY2hvaWNlcyA9IG5hbWVkLmxlbmd0aCA/IG5hbWVkIDogYm90dG9tOwogICAgICAgIHJldHVybiBjaG9pY2VzLmZpbHRlcihub2RlID0+ICFjaG9pY2VzLnNvbWUob3RoZXIgPT4gb3RoZXIgIT09IG5vZGUgJiYgbm9kZS5jb250YWlucyhvdGhlcikpKQogICAgICAgICAgLnJlZHVjZSgoYmVzdCwgbm9kZSkgPT4gIWJlc3QgfHwgbm9kZS5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS53aWR0aCA+IGJlc3QuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkud2lkdGggPyBub2RlIDogYmVzdCwgbnVsbCk7CiAgICAgIH0sIDMwMDAwLCAiS2jDtG5nIHRo4bqleSDDtCBOaOG6r24gdGluLiBIw6N5IMSRxINuZyBuaOG6rXAgTXVzZSB0cm9uZyBDaHJvbWUgcuG7k2kgdGjhu60gbOG6oWkuIik7CiAgICAgIGlmICgoZmllbGQudmFsdWUgPz8gZmllbGQuaW5uZXJUZXh0ID8/IGZpZWxkLnRleHRDb250ZW50KS50cmltKCkpIHRocm93IEVycm9yKCLDlCBOaOG6r24gdGluIE11c2UgxJFhbmcgY8OzIGLhuqNuIG5ow6FwLiIpOwogICAgICBjb25zdCBiYXRjaCA9IGF3YWl0IHNlbmQoe29wOiJiYXRjaCJ9KTsKICAgICAgaWYgKGJhdGNoLmRvbmUgfHwgYmF0Y2guam9iICE9PSBqb2Iuam9iIHx8IGJhdGNoLmluZGV4ICE9PSBqb2IuaW5kZXgpCiAgICAgICAgdGhyb3cgRXJyb3IoIkRhbmggc8OhY2ggY+G6o25oIE11c2UgxJHDoyB0aGF5IMSR4buVaS4iKTsKICAgICAgY29uc3QgYXR0ZW1wdCA9IHN0YXRlLmF0dGVtcHQgfHwgMDsKICAgICAgLy8gVHUgYmFuIDEuMy40MyB0aGVvIHlldSBjYXUgY3VhIG5ndW9pIGR1bmcgKDIwMjYtMTEtMjEpOiBtb2kgbHVvdCBjaGkKICAgICAgLy8gZ3VpIERVTkcgMSBwcm9tcHQsIHRhaSB4b25nIDEgYW5oIHZlIG1vaSBndWkgcHJvbXB0IHRpZXAgdGhlbywgY3UgdGhlCiAgICAgIC8vIGNoYXkgaGV0IHZpZWMuIENhY2ggZ29tIDQgcHJvbXB0L2x1b3QgbGFtIGJ1b2MgbmhhbiBkaWVuIHZhIHRodSBhbmgKICAgICAgLy8gbW9uZyBtYW5oIChhbmggdGFvIGR1IHRyb25nIGNoYXQgbWEgZXh0ZW5zaW9uIHRodSB2ZSBkdW9jIDApLCBjb24gZ3VpCiAgICAgIC8vIGxlIHRoaSBsb2kgY2hpIGtldCBkdW5nIDEgY2FuaCB2YSB0aHUgbGFpIGR1b2MgbmdheS4KICAgICAgY29uc3QgcHJvbXB0cyA9IGJhdGNoLnByb21wdHMuc2xpY2UoMCwgMSk7CiAgICAgIGNvbnN0IGJhdGNoRW5kID0gYXR0ZW1wdCA/IHN0YXRlLmJhdGNoRW5kIDogam9iLmluZGV4ICsgcHJvbXB0cy5sZW5ndGg7CiAgICAgIGNvbnN0IHRhZyA9IHRhZ0Zvcihqb2IsIGpvYi5pbmRleCwgYXR0ZW1wdCk7CiAgICAgIGNvbnN0IGZpbGVzID0gcHJvbXB0cy5tYXAoKGRlc2NyaXB0aW9uLCBvZmZzZXQpID0+IHsKICAgICAgICBjb25zdCBjb2RlID0gdGFnRm9yKGpvYiwgam9iLmluZGV4ICsgb2Zmc2V0LCBhdHRlbXB0KTsKICAgICAgICByZXR1cm4gbmV3IEZpbGUoW2NvZGUgKyAiXG4iICsgZGVzY3JpcHRpb25dLCBjb2RlICsgIi50eHQiLCB7dHlwZToidGV4dC9wbGFpbiJ9KTsKICAgICAgfSk7CiAgICAgIGNvbnN0IHNjZW5lcyA9IHByb21wdHMubWFwKChkZXNjcmlwdGlvbiwgb2Zmc2V0KSA9PiB7CiAgICAgICAgY29uc3QgaW5kZXggPSBqb2IuaW5kZXggKyBvZmZzZXQ7CiAgICAgICAgcmV0dXJuICJD4bqjbmggIiArIChpbmRleCArIDEpICsgIiB8ICIgKyB0YWdGb3Ioam9iLCBpbmRleCwgYXR0ZW1wdCkgKyAiXG4iICsgZGVzY3JpcHRpb247CiAgICAgIH0pOwogICAgICBjb25zdCBwcm9tcHQgPSAiVOG6oW8gxJHDum5nICIgKyBzY2VuZXMubGVuZ3RoICsgIiDhuqNuaCB0cm9uZyBsxrDhu6N0IG7DoHksIG3hu5dpIGPhuqNuaCBiw6puIGTGsOG7m2kgbeG7mXQg4bqjbmggcmnDqm5nLiAiICsKICAgICAgICAiTuG6v3UgY8OzIHRo4buDLCBjaOG6oXkgY8OhYyBj4bqjbmggc29uZyBzb25nLiBMxrB1IHThuqV0IGPhuqMgdsOgbyBUaMawIHZp4buHbiBow6xuaCDhuqNuaC4gIiArCiAgICAgICAgIsSQ4bq3dCB0w6puIG3hu5dpIOG6o25oIGLhuq90IMSR4bqndSBi4bqxbmcgbcOjIGPhuqNuaCB0xrDGoW5nIOG7qW5nLCBnaeG7ryBuZ3V5w6puIG3Doy4gS2jDtG5nIMSRxrBhIG3DoyB2w6BvIG7hu5lpIGR1bmcg4bqjbmguICIgKwogICAgICAgICJDw6FjIHThu4dwIFRYVCDEkcOtbmgga8OobSBsw6AgYuG6o24gc2FvIGPhu6dhIGPDoWMgcHJvbXB0IGLDqm4gZMaw4bubaSwgY2jhu4kgdOG6oW8gbeG7mXQg4bqjbmggY2hvIG3hu5dpIG3Doy4gIiArCiAgICAgICAgIktoaSB4b25nLCBiw6FvIEhvw6BuIHThuqV0IHbDoCBsaeG7h3Qga8OqIGPDoWMgbcOjIMSRw6MgdOG6oW8uIE3DoyDEkeG6p3UgxJHhu6N0OiAiICsgdGFnICsgIlxuXG4iICsKICAgICAgICBzY2VuZXMuam9pbigiXG5cbiIpOwogICAgICBmaWVsZC5mb2N1cygpOwogICAgICBjb25zdCBkYXRhID0gbmV3IERhdGFUcmFuc2ZlcigpOwogICAgICBmaWxlcy5mb3JFYWNoKGZpbGUgPT4gZGF0YS5pdGVtcy5hZGQoZmlsZSkpOwogICAgICBmaWVsZC5kaXNwYXRjaEV2ZW50KG5ldyBDbGlwYm9hcmRFdmVudCgicGFzdGUiLCB7Y2xpcGJvYXJkRGF0YTpkYXRhLCBidWJibGVzOnRydWUsIGNhbmNlbGFibGU6dHJ1ZX0pKTsKICAgICAgY29uc3QgY29tcG9zZXJUZXh0ID0gKCkgPT4gewogICAgICAgIGNvbnN0IG5hbWVzSW4gPSByb290ID0+IHsKICAgICAgICAgIGlmICghcm9vdCkgcmV0dXJuIGZhbHNlOwogICAgICAgICAgY29uc3QgY29udGVudCA9IHJvb3QudGV4dENvbnRlbnQgfHwgIiI7CiAgICAgICAgICBjb25zdCBsYWJlbHMgPSBbLi4ucm9vdC5xdWVyeVNlbGVjdG9yQWxsKCdbYXJpYS1sYWJlbF0sW3RpdGxlXScpXS5tYXAoZWwgPT4KICAgICAgICAgICAgKGVsLmdldEF0dHJpYnV0ZSgnYXJpYS1sYWJlbCcpIHx8ICcnKSArICcgJyArIChlbC50aXRsZSB8fCAnJykpLmpvaW4oIiAiKTsKICAgICAgICAgIHJldHVybiBmaWxlcy5ldmVyeShmaWxlID0+IChjb250ZW50ICsgIiAiICsgbGFiZWxzKS5pbmNsdWRlcyhmaWxlLm5hbWUpKTsKICAgICAgICB9OwogICAgICAgIGxldCBub2RlID0gZmllbGQ7CiAgICAgICAgZm9yIChsZXQgbGV2ZWwgPSAwOyBub2RlICYmIGxldmVsIDwgMTA7IGxldmVsKyssIG5vZGUgPSBub2RlLnBhcmVudEVsZW1lbnQpIHsKICAgICAgICAgIGlmIChuYW1lc0luKG5vZGUpKSByZXR1cm4gdHJ1ZTsKICAgICAgICB9CiAgICAgICAgLy8gQ2hpcCB0ZXAgY28gdGhlIHJlbmRlciBuZ29haSBjYXkgY2hhIGN1YSBvIG5oYXAgKHBvcnRhbCkuIFRlbiB0ZXAgbGEKICAgICAgICAvLyBkdXkgbmhhdCB0aGVvIHR1bmcgam9iL2NhbmgvbGFuIHRodSBuZW4gcXVldCBjYSB0cmFuZyBraG9uZyB0aGUgbmhhbQogICAgICAgIC8vIHNhbmcgdGluIG5oYW4gY3UuIEJhbiBjdSBjaGkgcXVldCA3IHRhbmcgY2hhLCBibyBzb3QgY2hpcCwga2hpZW4KICAgICAgICAvLyBmYWxsYmFjayBnYW4gbGFpIHF1YSBvIGNob24gdGVwIHZhIG1vaSB0ZXAgYmkgZGluaCBrZW0gMiBsYW4uCiAgICAgICAgcmV0dXJuIG5hbWVzSW4oZG9jdW1lbnQuYm9keSk7CiAgICAgIH07CiAgICAgIGxldCBhdHRhY2hlZCA9IGZhbHNlOwogICAgICB0cnkgeyBhd2FpdCB3YWl0Rm9yKGNvbXBvc2VyVGV4dCwgODAwMCwgInBhc3RlLWlnbm9yZWQiKTsgYXR0YWNoZWQgPSB0cnVlOyB9IGNhdGNoIHt9CiAgICAgIGlmICghYXR0YWNoZWQpIHsKICAgICAgICAvLyBNdXNlIGNvIHRoZSBibyBxdWEgc3Uga2llbiBwYXN0ZSBnaWEgbGFwOiB0aHUgbGFpIGJhbmcgbyBjaG9uIHRlcCBjbyBzYW4uCiAgICAgICAgY29uc3QgZmlsZUlucHV0ID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ2lucHV0W3R5cGU9ImZpbGUiXScpXS5maWx0ZXIobm9kZSA9PiAhbm9kZS5kaXNhYmxlZClbMF07CiAgICAgICAgaWYgKGZpbGVJbnB1dCkgewogICAgICAgICAgY29uc3QgdHJhbnNmZXIgPSBuZXcgRGF0YVRyYW5zZmVyKCk7CiAgICAgICAgICBmaWxlcy5mb3JFYWNoKGZpbGUgPT4gdHJhbnNmZXIuaXRlbXMuYWRkKGZpbGUpKTsKICAgICAgICAgIGZpbGVJbnB1dC5maWxlcyA9IHRyYW5zZmVyLmZpbGVzOwogICAgICAgICAgZmlsZUlucHV0LmRpc3BhdGNoRXZlbnQobmV3IEV2ZW50KCJpbnB1dCIsIHtidWJibGVzOnRydWV9KSk7CiAgICAgICAgICBmaWxlSW5wdXQuZGlzcGF0Y2hFdmVudChuZXcgRXZlbnQoImNoYW5nZSIsIHtidWJibGVzOnRydWV9KSk7CiAgICAgICAgfQogICAgICAgIGF3YWl0IHdhaXRGb3IoY29tcG9zZXJUZXh0LCAyMDAwMCwgIk11c2UgY2jGsGEgbmjhuq1uIMSR4bunICIgKyBmaWxlcy5sZW5ndGggKyAiIHThu4dwLiDEkMOjIGThu6tuZyB0csaw4bubYyBraGkgZ+G7rWkuIik7CiAgICAgIH0KICAgICAgaWYgKGZpZWxkIGluc3RhbmNlb2YgSFRNTFRleHRBcmVhRWxlbWVudCB8fCBmaWVsZCBpbnN0YW5jZW9mIEhUTUxJbnB1dEVsZW1lbnQpIHsKICAgICAgICBPYmplY3QuZ2V0T3duUHJvcGVydHlEZXNjcmlwdG9yKE9iamVjdC5nZXRQcm90b3R5cGVPZihmaWVsZCksICJ2YWx1ZSIpLnNldC5jYWxsKGZpZWxkLCBwcm9tcHQpOwogICAgICAgIGZpZWxkLmRpc3BhdGNoRXZlbnQobmV3IElucHV0RXZlbnQoImlucHV0Iiwge2J1YmJsZXM6dHJ1ZSwgZGF0YTpwcm9tcHR9KSk7CiAgICAgIH0gZWxzZSBpZiAoIWRvY3VtZW50LmV4ZWNDb21tYW5kKCJpbnNlcnRUZXh0IiwgZmFsc2UsIHByb21wdCkpIHsKICAgICAgICB0aHJvdyBFcnJvcigiS2jDtG5nIMSRaeG7gW4gxJHGsOG7o2MgbOG7h25oIHThuqFvIOG6o25oIE11c2UuIik7CiAgICAgIH0KICAgICAgY29uc3QgYnV0dG9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgYnV0dG9ucyA9IGNvbnRyb2xzKC9eKEfhu61pfFNlbmR8U2VuZCBtZXNzYWdlKSQvaXUpOwogICAgICAgIHJldHVybiBidXR0b25zLmxlbmd0aCA9PT0gMSA/IGJ1dHRvbnNbMF0gOiBudWxsOwogICAgICB9LCA4MDAwLCAiS2jDtG5nIHTDrG0gxJHGsOG7o2MgbsO6dCBH4butaSBj4bunYSBNdXNlLiIpOwogICAgICBjb25zdCBuZXh0ID0ge3BoYXNlOiJhd2FpdCIsIGpvYjpqb2Iuam9iLCBpbmRleDpqb2IuaW5kZXgsIHRhZywgYXR0ZW1wdCwgYmF0Y2hFbmQsIHN1Ym1pdHRlZEF0OkRhdGUubm93KCksIHByb21wdFRhaWw6cHJvbXB0LnNsaWNlKC00OCl9OwogICAgICBzYXZlKG5leHQpOwogICAgICBidXR0b24uY2xpY2soKTsKICAgICAgYXdhaXQgc2VuZCh7b3A6InByb2dyZXNzIiwgY291bnQ6am9iLmluZGV4ICsgcHJvbXB0cy5sZW5ndGgsIHN0YWdlOiJzdWJtaXR0ZWQifSk7CiAgICAgIGF3YWl0IHdhaXRGb3IoKCkgPT4gIShmaWVsZC52YWx1ZSA/PyBmaWVsZC50ZXh0Q29udGVudCkuaW5jbHVkZXModGFnKSwgMTIwMDAsICJNdXNlIGNoxrBhIG5o4bqtbiBwcm9tcHQuIik7CiAgICAgIHJldHVybiBhd2FpdCByZXN1bHQobmV4dCwgam9iKTsKICAgIH0KICAgIGlmIChzdGF0ZS5waGFzZSA9PT0gImF3YWl0IikgcmV0dXJuIGF3YWl0IHJlc3VsdChzdGF0ZSwgam9iKTsKICAgIGlmIChzdGF0ZS5waGFzZSAhPT0gImxpYnJhcnkiICYmIHN0YXRlLnBoYXNlICE9PSAiZG93bmxvYWQiKSB0aHJvdyBFcnJvcigiVHLhuqFuZyB0aMOhaSBNdXNlIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgY29uc3QgZmlsZSA9IG5ldyBVUkwobG9jYXRpb24uaHJlZikuc2VhcmNoUGFyYW1zLmdldCgiZmlsZSIpOwogICAgaWYgKCFmaWxlIHx8ICFmaWxlLnNwbGl0KCIvIikucG9wKCkudG9Mb3dlckNhc2UoKS5pbmNsdWRlcyhzdGF0ZS50YWcpKSB7CiAgICAgIGxldCBpdGVtID0gbnVsbDsKICAgICAgdHJ5IHsKICAgICAgICBpdGVtID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgbWF0Y2hlcyA9IG5vZGUgPT4gWyJhbHQiLCAidGl0bGUiLCAiYXJpYS1sYWJlbCIsICJocmVmIiwgImRhdGEtZmlsZW5hbWUiLCAiZGF0YS1maWxlLW5hbWUiXQogICAgICAgICAgLnNvbWUoYXR0ciA9PiAobm9kZS5nZXRBdHRyaWJ1dGUoYXR0cikgfHwgIiIpLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMoc3RhdGUudGFnKSk7CiAgICAgICAgY29uc3QgZWxpZ2libGUgPSBub2RlID0+IHZpc2libGUobm9kZSkgJiYgIW5vZGUuY2xvc2VzdCgnW3JvbGU9ImRpYWxvZyJdLGRpYWxvZycpOwogICAgICAgIGNvbnN0IHRodW1ibmFpbHMgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiaW1nIildLmZpbHRlcihub2RlID0+IGVsaWdpYmxlKG5vZGUpICYmIG1hdGNoZXMobm9kZSkpOwogICAgICAgIGlmICh0aHVtYm5haWxzLmxlbmd0aCA9PT0gMSkgcmV0dXJuIHRodW1ibmFpbHNbMF07CiAgICAgICAgY29uc3QgbmFtZWQgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW3RpdGxlXSxbYXJpYS1sYWJlbF0sYVtocmVmXSxbZGF0YS1maWxlbmFtZV0sW2RhdGEtZmlsZS1uYW1lXScpXQogICAgICAgICAgLmZpbHRlcihub2RlID0+IGVsaWdpYmxlKG5vZGUpICYmIG1hdGNoZXMobm9kZSkpOwogICAgICAgIGNvbnN0IGNvbnRyb2xzID0gWy4uLm5ldyBTZXQobmFtZWQubWFwKG5vZGUgPT4gbm9kZS5jbG9zZXN0KCdidXR0b24sW3JvbGU9ImJ1dHRvbiJdLGEnKSB8fCBub2RlKSldOwogICAgICAgIGlmIChjb250cm9scy5sZW5ndGggPT09IDEpIHJldHVybiBjb250cm9sc1swXTsKICAgICAgICBjb25zdCB3YWxrZXIgPSBkb2N1bWVudC5jcmVhdGVUcmVlV2Fsa2VyKGRvY3VtZW50LmJvZHksIE5vZGVGaWx0ZXIuU0hPV19URVhUKTsKICAgICAgICBjb25zdCBmb3VuZCA9IG5ldyBTZXQoKTsKICAgICAgICB3aGlsZSAod2Fsa2VyLm5leHROb2RlKCkpIHsKICAgICAgICAgIGNvbnN0IHRleHQgPSB3YWxrZXIuY3VycmVudE5vZGU7CiAgICAgICAgICBpZiAoIXRleHQubm9kZVZhbHVlLnRyaW0oKS50b0xvd2VyQ2FzZSgpLnN0YXJ0c1dpdGgoc3RhdGUudGFnKSkgY29udGludWU7CiAgICAgICAgICBjb25zdCBjYXJkID0gdGV4dC5wYXJlbnRFbGVtZW50LmNsb3Nlc3QoJ2J1dHRvbixbcm9sZT0iYnV0dG9uIl0sYScpOwogICAgICAgICAgaWYgKGNhcmQgJiYgZWxpZ2libGUoY2FyZCkpIGZvdW5kLmFkZChjYXJkKTsKICAgICAgICB9CiAgICAgICAgaWYgKGZvdW5kLnNpemUgPT09IDEpIHJldHVybiBbLi4uZm91bmRdWzBdOwogICAgICAgIGZvciAoY29uc3QgZ3JpZCBvZiBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdtYWluLCBbcm9sZT0ibWFpbiJdLCBtYWluIGRpdiwgW3JvbGU9Im1haW4iXSBkaXYnKSkgewogICAgICAgICAgaWYgKGdyaWQuY2xpZW50SGVpZ2h0ID4gMTUwICYmIGdyaWQuc2Nyb2xsSGVpZ2h0ID4gZ3JpZC5jbGllbnRIZWlnaHQgJiYKICAgICAgICAgICAgICAvXihhdXRvfHNjcm9sbCkkLy50ZXN0KGdldENvbXB1dGVkU3R5bGUoZ3JpZCkub3ZlcmZsb3dZKSkgewogICAgICAgICAgICBjb25zdCBhdEVuZCA9IGdyaWQuc2Nyb2xsVG9wICsgZ3JpZC5jbGllbnRIZWlnaHQgPj0gZ3JpZC5zY3JvbGxIZWlnaHQgLSAyOwogICAgICAgICAgICBncmlkLnNjcm9sbFRvcCA9IGF0RW5kID8gMCA6IGdyaWQuc2Nyb2xsVG9wICsgTWF0aC5mbG9vcihncmlkLmNsaWVudEhlaWdodCAqIDAuOCk7CiAgICAgICAgICB9CiAgICAgICAgfQogICAgICAgIHJldHVybiBudWxsOwogICAgICAgIH0sIDYwMDAwLCBuZXcgU2NlbmVFcnJvcigiS2jDtG5nIHTDrG0gxJHGsOG7o2MgxJHDum5nIOG6o25oIGPhu6dhIGPhuqNuaCB0cm9uZyBNdXNlLiIpKTsKICAgICAgfSBjYXRjaCAoZXJyb3IpIHsKICAgICAgICAvLyBMdW9pIFRodSB2aWVuIGtob25nIGhlIGxvIHRlbiB0ZXAgKGRhIHRoYXkgbyBqb2IgZmU3N2VjMjk6IGFuaCB0YW8KICAgICAgICAvLyBkdW5nIHRlbiBtYSB0aW0gMiB2b25nIHZhbiBraG9uZyB0aGF5IG8gbmFvKSAtPiBkdW5nIGR1IHBob25nIGNoYXQuCiAgICAgICAgdHJ5IHsgYXdhaXQgY2hhdFVwbG9hZChzdGF0ZSwgam9iKTsgcmV0dXJuOyB9CiAgICAgICAgY2F0Y2ggKGNoYXRFcnJvcikgewogICAgICAgICAgaWYgKHN0YXRlLmNoYXRJbWFnZXM/Lmxlbmd0aCkKICAgICAgICAgICAgdGhyb3cgRXJyb3IoZXJyb3IubWVzc2FnZSArICIgfCBE4buxIHBow7JuZyBjaGF0IGzhu5dpOiAiICsgY2hhdEVycm9yLm1lc3NhZ2UpOwogICAgICAgICAgdGhyb3cgZXJyb3I7CiAgICAgICAgfQogICAgICB9CiAgICAgIGl0ZW0uY2xpY2soKTsKICAgIH0KICAgIHRyeSB7CiAgICAgIGNvbnN0IGJ1dHRvbiA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICBjb25zdCBmaWxlID0gbmV3IFVSTChsb2NhdGlvbi5ocmVmKS5zZWFyY2hQYXJhbXMuZ2V0KCJmaWxlIik7CiAgICAgIGlmICghZmlsZT8uc3BsaXQoIi8iKS5wb3AoKS50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHN0YXRlLnRhZykpIHJldHVybiBudWxsOwogICAgICBjb25zdCBidXR0b25zID0gY29udHJvbHMoL14oVOG6o2kgeHXhu5FuZ3xEb3dubG9hZCkkL2l1KS5maWx0ZXIobm9kZSA9PiBub2RlLmNsb3Nlc3QoJ1tyb2xlPSJkaWFsb2ciXSxkaWFsb2cnKSk7CiAgICAgIHJldHVybiBidXR0b25zLmxlbmd0aCA9PT0gMSA/IGJ1dHRvbnNbMF0gOiBudWxsOwogICAgfSwgMTIwMDAsIG5ldyBTY2VuZUVycm9yKCJLaMO0bmcgbeG7nyDEkcaw4bujYyDhuqNuaCDEkcO6bmcgbcOjIGPhuqNuaC4iKSk7CiAgICBjb25zdCBmaWxlbmFtZSA9IG5ldyBVUkwobG9jYXRpb24uaHJlZikuc2VhcmNoUGFyYW1zLmdldCgiZmlsZSIpLnNwbGl0KCIvIikucG9wKCk7CiAgICBhd2FpdCBzZW5kKHtvcDoicHJvZ3Jlc3MiLCBjb3VudDpqb2IuaW5kZXggKyAxLCBzdGFnZToiZ2VuZXJhdGVkIn0pOwogICAgYXdhaXQgc2VuZCh7b3A6ImFybU11c2UiLCBqb2I6am9iLmpvYiwgaW5kZXg6am9iLmluZGV4LCBmaWxlbmFtZX0pOwogICAgYnV0dG9uLmNsaWNrKCk7CiAgICBjb25zdCBkZWFkbGluZSA9IERhdGUubm93KCkgKyA5MDAwMDsKICAgIHdoaWxlIChEYXRlLm5vdygpIDwgZGVhZGxpbmUpIHsKICAgICAgaWYgKChhd2FpdCBzZW5kKHtvcDoidHJhbnNmZXJNdXNlIiwgam9iOmpvYi5qb2IsIGluZGV4OmpvYi5pbmRleH0pKS5yZWNlaXZlZCkgewogICAgICAgIGF3YWl0IGFkdmFuY2Uoc3RhdGUpOwogICAgICAgIHJldHVybjsKICAgICAgfQogICAgICBhd2FpdCBwYXVzZSg4MDApOwogICAgfQogICAgICB0aHJvdyBuZXcgU2NlbmVFcnJvcigiQ2hyb21lIGNoxrBhIHThuqNpIHhvbmcg4bqjbmggTXVzZS4iKTsKICAgIH0gY2F0Y2ggKGVycm9yKSB7CiAgICAgIC8vIENhIGdpYWkgZG9hbiBtbyBhbmggKyB0YWkgeHVvbmcgZGV1IGNvIGR1IHBob25nIGNoYXQ6IGtlIGNhIGtoaSB0aW0KICAgICAgLy8gdGhheSBvIGFuaCBuaHVuZyBDaHJvbWUgbHV1IGFuaCBzYWkgdGh1IG11YyAodGllbiBpY2gga2hhYyBjaGllbQogICAgICAvLyBxdXllbiBkYXQgdGVuIHRlcCkgdGhpIHZhbiBsYXkgZHVvYyBhbmggdmUgY2hvIFZlIFRheS4KICAgICAgdHJ5IHsgYXdhaXQgY2hhdFVwbG9hZChzdGF0ZSwgam9iKTsgcmV0dXJuOyB9CiAgICAgIGNhdGNoIChjaGF0RXJyb3IpIHsKICAgICAgICBpZiAoc3RhdGUuY2hhdEltYWdlcz8ubGVuZ3RoKQogICAgICAgICAgdGhyb3cgRXJyb3IoZXJyb3IubWVzc2FnZSArICIgfCBE4buxIHBow7JuZyBjaGF0IGzhu5dpOiAiICsgY2hhdEVycm9yLm1lc3NhZ2UpOwogICAgICAgIHRocm93IGVycm9yOwogICAgICB9CiAgICB9CiAgfQogIGxldCBsYXN0SGFydmVzdERpYWcgPSAiIjsKICBmdW5jdGlvbiBoYXJ2ZXN0Q2hhdEltYWdlcyhzdGF0ZSwgZXhwZWN0ZWRDb3VudCkgewogICAgLy8gVGh1IGRpYSBjaGkgY2FjIGFuaCB2dWEgdGFvIHRyb25nIGtodW5nIGNoYXQuIE5lbyB2YW8gY3VvaSBwcm9tcHQgdnVhCiAgICAvLyBndWk6IGNoaSBsYXkgYW5oIG5hbSBTQVUgbW9jIG5heSB0cm9uZyB0cmFuZywgdHJhbmggbmhhbSBhbmggY3VhIGNhYwogICAgLy8gbHVvdCBjaGF0IGN1IChjdW5nIG5hbSB0cm9uZyBkb2FuIGNoYXQgY2hpbmgpLiBDaGkgdHJhIHZlIGtoaSBkdSBzbwogICAgLy8gbHVvbmcgY3VhIGJhdGNoOyB0aGlldSBsYSB0cmEgcm9uZyBkZSBraG9uZyBiYW8gZ2lvIGFuaCB4YSBzYWkgY2FuaC4KICAgIC8vIEJhbiAxLjMuNDEgZG8gZHVvYyBjYSB0cmFuZyBjaGF0IGNoaSBjbyAzIHRoZSA8aW1nPiB2YSBraG9uZyBjYWkgbmFvCiAgICAvLyBsYSBhbmggdnVhIHRhbyAoY2hhdFtpbWc9MyBsb249MiBuZW89MSBtYXg9MTkyMHgxMDgwIHRodT0wXSBvIGpvYgogICAgLy8gMjYzZWU3M2MyNjc0YykgLSBhbmggdHJvbmcgY2hhdCBraG9uZyBjb24gbGEgdGhlIGltZyB0aHVvbmcgbnVhLiBCYW4KICAgIC8vIDEuMy40MiBxdWV0IHNhdSBjYSBzaGFkb3cgRE9NLCB0aGUgPHZpZGVvPiwgYW5oIG5lbiBDU1MgdmEgY2hhcCBuaGFuCiAgICAvLyB0aGVtIFVSTCBkYXRhOiwgZG9uZyB0aG9pIGdoaSBjaGFuIGRvYW4gdHVuZyBsb2FpIHZhbyBsYXN0SGFydmVzdERpYWcuCiAgICBsZXQgYW5jaG9yID0gbnVsbDsKICAgIGlmIChzdGF0ZS5wcm9tcHRUYWlsKSB7CiAgICAgIGNvbnN0IHdhbGtlciA9IGRvY3VtZW50LmNyZWF0ZVRyZWVXYWxrZXIoZG9jdW1lbnQuYm9keSwgTm9kZUZpbHRlci5TSE9XX1RFWFQpOwogICAgICB3aGlsZSAod2Fsa2VyLm5leHROb2RlKCkpIHsKICAgICAgICBpZiAod2Fsa2VyLmN1cnJlbnROb2RlLm5vZGVWYWx1ZS5pbmNsdWRlcyhzdGF0ZS5wcm9tcHRUYWlsKSkgYW5jaG9yID0gd2Fsa2VyLmN1cnJlbnROb2RlOwogICAgICB9CiAgICB9CiAgICBsZXQgbkltZyA9IDAsIG5TaGFkb3dJbWcgPSAwLCBuVmlkID0gMCwgbkJnID0gMCwgbkZyYW1lID0gZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiaWZyYW1lIikubGVuZ3RoOwogICAgY29uc3QgY2FuZGlkYXRlcyA9IFtdOwogICAgY29uc3QgYWZ0ZXJBbmNob3IgPSAoZWwpID0+IHsKICAgICAgaWYgKCFhbmNob3IpIHJldHVybiB0cnVlOwogICAgICBsZXQgbm9kZSA9IGVsOwogICAgICBjb25zdCByb290ID0gZWwuZ2V0Um9vdE5vZGUoKTsKICAgICAgaWYgKHJvb3QgaW5zdGFuY2VvZiBTaGFkb3dSb290KSBub2RlID0gcm9vdC5ob3N0OwogICAgICB0cnkgeyByZXR1cm4gISEoYW5jaG9yLmNvbXBhcmVEb2N1bWVudFBvc2l0aW9uKG5vZGUpICYgTm9kZS5ET0NVTUVOVF9QT1NJVElPTl9GT0xMT1dJTkcpOyB9CiAgICAgIGNhdGNoIHsgcmV0dXJuIHRydWU7IH0KICAgIH07CiAgICBjb25zdCB3YWxrID0gKHJvb3ROb2RlLCBpblNoYWRvdykgPT4gewogICAgICBmb3IgKGNvbnN0IGVsIG9mIHJvb3ROb2RlLnF1ZXJ5U2VsZWN0b3JBbGwoIioiKSkgewogICAgICAgIGlmIChlbC50YWdOYW1lID09PSAiSU1HIikgewogICAgICAgICAgaWYgKGluU2hhZG93KSBuU2hhZG93SW1nKys7IGVsc2UgbkltZysrOwogICAgICAgICAgaWYgKGVsLm5hdHVyYWxXaWR0aCA+PSAxMDAgJiYgZWwubmF0dXJhbEhlaWdodCA+PSAxMDApIHsKICAgICAgICAgICAgbGV0IHVybCA9IGVsLmN1cnJlbnRTcmMgfHwgZWwuc3JjIHx8ICIiOwogICAgICAgICAgICBjb25zdCBocmVmID0gZWwuY2xvc2VzdCgiYSIpPy5ocmVmIHx8ICIiOwogICAgICAgICAgICBpZiAoL1wuKHdlYnB8cG5nfGpwZT9nKShcP3wjfCQpL2l1LnRlc3QoaHJlZikpIHVybCA9IGhyZWY7CiAgICAgICAgICAgIGNhbmRpZGF0ZXMucHVzaCh7ZWwsIHVybH0pOwogICAgICAgICAgfQogICAgICAgIH0gZWxzZSBpZiAoZWwudGFnTmFtZSA9PT0gIlZJREVPIikgewogICAgICAgICAgblZpZCsrOwogICAgICAgICAgY29uc3QgcG9zdGVyID0gZWwucG9zdGVyIHx8ICIiOwogICAgICAgICAgY29uc3Qgc3JjID0gZWwuY3VycmVudFNyYyB8fCBlbC5zcmMgfHwgIiI7CiAgICAgICAgICBjb25zdCB1cmwgPSAvXC4od2VicHxwbmd8anBlP2cpKFw/fCN8JCkvaXUudGVzdChwb3N0ZXIpID8gcG9zdGVyCiAgICAgICAgICAgIDogKC9cLih3ZWJwfHBuZ3xqcGU/ZykoXD98I3wkKS9pdS50ZXN0KHNyYykgPyBzcmMgOiAiIik7CiAgICAgICAgICBpZiAodXJsICYmIChlbC52aWRlb1dpZHRoID49IDEwMCB8fCBwb3N0ZXIpKSBjYW5kaWRhdGVzLnB1c2goe2VsLCB1cmx9KTsKICAgICAgICB9IGVsc2UgewogICAgICAgICAgbGV0IGJnVXJsID0gIiI7CiAgICAgICAgICB0cnkgewogICAgICAgICAgICBjb25zdCBiZyA9IGdldENvbXB1dGVkU3R5bGUoZWwpLmJhY2tncm91bmRJbWFnZSB8fCAiIjsKICAgICAgICAgICAgY29uc3QgbSA9IGJnLm1hdGNoKC91cmxcKFsiJ10/KC4qPylbIiddP1wpL2l1KTsKICAgICAgICAgICAgaWYgKG0pIGJnVXJsID0gbVsxXTsKICAgICAgICAgIH0gY2F0Y2gge30KICAgICAgICAgIGlmIChiZ1VybCkgewogICAgICAgICAgICBuQmcrKzsKICAgICAgICAgICAgY29uc3QgcmVjdCA9IGVsLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICAgICAgICBpZiAocmVjdC53aWR0aCA+PSAxMDAgJiYgcmVjdC5oZWlnaHQgPj0gMTAwKSBjYW5kaWRhdGVzLnB1c2goe2VsLCB1cmw6YmdVcmx9KTsKICAgICAgICAgIH0KICAgICAgICB9CiAgICAgICAgaWYgKGVsLnNoYWRvd1Jvb3QpIHdhbGsoZWwuc2hhZG93Um9vdCwgdHJ1ZSk7CiAgICAgIH0KICAgIH07CiAgICB3YWxrKGRvY3VtZW50LCBmYWxzZSk7CiAgICBjb25zdCBzZWVuID0gbmV3IFNldCgpOwogICAgY29uc3QgZm91bmQgPSBbXTsKICAgIGZvciAoY29uc3QgYyBvZiBjYW5kaWRhdGVzKSB7CiAgICAgIGlmICghYWZ0ZXJBbmNob3IoYy5lbCkpIGNvbnRpbnVlOwogICAgICBpZiAoIS9eKGh0dHBzfGJsb2J8ZGF0YSk6Ly50ZXN0KGMudXJsKSB8fCAvYXZhdGFyL2l1LnRlc3QoYy51cmwpIHx8IHNlZW4uaGFzKGMudXJsKSkgY29udGludWU7CiAgICAgIHNlZW4uYWRkKGMudXJsKTsKICAgICAgZm91bmQucHVzaChjLnVybCk7CiAgICB9CiAgICBsYXN0SGFydmVzdERpYWcgPSAiY2hhdFtpbWc9IiArIG5JbWcgKyAiIHNpbWc9IiArIG5TaGFkb3dJbWcgKyAiIHZpZD0iICsgblZpZCArCiAgICAgICIgYmc9IiArIG5CZyArICIgaWY9IiArIG5GcmFtZSArICIgbmVvPSIgKyAoYW5jaG9yID8gMSA6IDApICsKICAgICAgIiBsb249IiArIGNhbmRpZGF0ZXMubGVuZ3RoICsgIiB0aHU9IiArIGZvdW5kLmxlbmd0aCArICJdIjsKICAgIHJldHVybiBmb3VuZC5sZW5ndGggPj0gZXhwZWN0ZWRDb3VudCA/IGZvdW5kLnNsaWNlKC1leHBlY3RlZENvdW50KSA6IFtdOwogIH0KICBhc3luYyBmdW5jdGlvbiByZXN1bHQoc3RhdGUsIGpvYikgewogICAgbGV0IGNvbmZpcm1lZCA9IGZhbHNlOwogICAgY29uc3QgZXhwZWN0ZWRDb3VudCA9IHN0YXRlLmF0dGVtcHQgPyAxIDogc3RhdGUuYmF0Y2hFbmQgLSBqb2IuaW5kZXg7CiAgICBjb25zdCB0YWdzID0gQXJyYXkuZnJvbSh7bGVuZ3RoOmV4cGVjdGVkQ291bnR9LCAoXywgb2Zmc2V0KSA9PgogICAgICB0YWdGb3Ioam9iLCBqb2IuaW5kZXggKyBvZmZzZXQsIHN0YXRlLmF0dGVtcHQgfHwgMCkpOwogICAgY29uc3QgZGVhZGxpbmUgPSBEYXRlLm5vdygpICsgMjQ1MDAwOwogICAgd2hpbGUgKERhdGUubm93KCkgPCBkZWFkbGluZSkgewogICAgICBjb25zdCBmdWxsID0gZG9jdW1lbnQuYm9keT8uaW5uZXJUZXh0IHx8ICIiOwogICAgICAvLyBDaGkgeGV0IHBoYW4gdHJhbmcgU0FVIHRpbiBuaGFuIG1pbmggdnVhIGd1aS4gQmFuIHRoYW4gcHJvbXB0IGNvIGNhdQogICAgICAvLyAiS2hpIHhvbmcsIGJhbyBIb2FuIHRhdCB2YSBsaWV0IGtlIGNhYyBtYSBkYSB0YW8iOiBuZXUgcXVldCBjYSBwcm9tcHQKICAgICAgLy8gdGhpIHRpZW4gaWNoIHNlIG5oYW4gbmhhbSBsYSBNdXNlIGRhIHRhbyB4b25nIG5nYXkga2hpIHZ1YSBiYW0gR3VpLAogICAgICAvLyBuaGF5IHNhbmcgVGh1IHZpZW4gcXVhIHNvbSB0cm9uZyBraGkgY2h1YSBjbyBhbmggbmFvIChiYW4gMS4zLjM1IG1hYwogICAgICAvLyBsb2kgbmF5IHZpIG1hdSBxdWV0IHF1YSByb25nICJob2FuIHRhdC57MCw4MH1kYSB0YW8iKS4KICAgICAgbGV0IHN0YXJ0ID0gMDsKICAgICAgaWYgKHN0YXRlLnByb21wdFRhaWwpIHsKICAgICAgICBjb25zdCB0YWlsQXQgPSBmdWxsLmxhc3RJbmRleE9mKHN0YXRlLnByb21wdFRhaWwpOwogICAgICAgIGlmICh0YWlsQXQgPj0gMCkgc3RhcnQgPSB0YWlsQXQgKyBzdGF0ZS5wcm9tcHRUYWlsLmxlbmd0aDsKICAgICAgfQogICAgICBjb25zdCB0ZXh0ID0gZnVsbC5zbGljZShzdGFydCk7CiAgICAgIGNvbnN0IGF0ID0gdGV4dC5sYXN0SW5kZXhPZihzdGF0ZS50YWcpOwogICAgICBpZiAoYXQgPj0gMCkgewogICAgICAgIGNvbnN0IG5lYXJieSA9IHRleHQuc2xpY2UoTWF0aC5tYXgoMCwgYXQgLSA2MDAwKSwgYXQgKyAxNjAwMCk7CiAgICAgICAgY29uc3QgYWZ0ZXIgPSB0ZXh0LnNsaWNlKGF0KTsKICAgICAgICBpZiAoL2jhur90ICg/OmjhuqFuIG3hu6ljfGzGsOG7o3QpfMSRw6MgxJHhuqF0IGdp4bubaSBo4bqhbnwoPzp3ZWVrbHl8dXNhZ2V8cmF0ZSkgbGltaXR8cXVvdGEgZXhjZWVkZWQvaXUudGVzdChhZnRlcikpCiAgICAgICAgICB0aHJvdyBFcnJvcigiTXVzZSBiw6FvIGdp4bubaSBo4bqhbiBz4butIGThu6VuZy4gxJDDoyBk4burbmcgZ+G7rWkgdGjDqm0gcHJvbXB0LiIpOwogICAgICAgIGNvbnN0IGNvbXBsZXRlID0gL2hvw6BuIHThuqV0XHMqW+KAlOKAkzotXVxzKsSRw6MgdOG6oW8gKD86eG9uZ3zEkcO6bmcpfMSRw6MgdOG6oW8geG9uZyB0b8OgbiBi4buZfMSRw6MgdOG6oW8gKD86eG9uZ3zEkcO6bmcpICg/OnRvw6BuIGLhu5l8dOG6pXQgY+G6o3xcZCsg4bqjbmgpfGNvbXBsZXRlZFxzKlvigJTigJM6LV1ccyooPzphbGx8Z2VuZXJhdGVkKS9pdS50ZXN0KG5lYXJieSk7CiAgICAgICAgLy8gVGFvIGFuaCB0aGF0IGtob25nIGJhbyBnaW8geG9uZyB0cm9uZyBkdW9pIDI1IGdpYXk7IGtob2EgdGhvaSBnaWFuCiAgICAgICAgLy8gbmF5IGNoYW4gbm90IG1vaSBuaGFuIG5oYW0gY29uIHNvdCBsYWkgdHUgbm9pIGR1bmcgcGhhbiBjaGlldS4KICAgICAgICBjb25zdCBzZXR0bGVkID0gIXN0YXRlLnN1Ym1pdHRlZEF0IHx8IERhdGUubm93KCkgLSBzdGF0ZS5zdWJtaXR0ZWRBdCA+PSAyNTAwMDsKICAgICAgICBpZiAoc2V0dGxlZCAmJiBjb21wbGV0ZSAmJiB0YWdzLmV2ZXJ5KHRhZyA9PiBuZWFyYnkuaW5jbHVkZXModGFnKSkpIHsKICAgICAgICAgIGNvbmZpcm1lZCA9IHRydWU7CiAgICAgICAgICBicmVhazsKICAgICAgICB9CiAgICAgICAgaWYgKC9jaMawYSDEkcaw4bujYyB04bqhbyB0cm9uZyBsxrDhu6N0IG7DoHl8Y+G6p24gc2FuZyBsxrDhu6N0IHRp4bq/cCB0aGVvfG5vdCBnZW5lcmF0ZWQgaW4gdGhpcyBiYXRjaC9pdS50ZXN0KGFmdGVyKSkgYnJlYWs7CiAgICAgICAgaWYgKC9raMO0bmcgdGjhu4MgdOG6oW8g4bqjbmh8dOG6oW8g4bqjbmggdGjhuqV0IGLhuqFpfGltYWdlIGdlbmVyYXRpb24gZmFpbGVkL2l1LnRlc3QoYWZ0ZXIpKSBicmVhazsKICAgICAgfQogICAgICBhd2FpdCBwYXVzZSgxMDAwKTsKICAgIH0KICAgIC8vIFRodSBkaWEgY2hpIGNhYyBhbmggdnVhIHRhbyBuZ2F5IHRyb25nIGtodW5nIGNoYXQgKGNoaSBraGkgTXVzZSBkYQogICAgLy8gYmFvIGhvYW4gdGF0IHRoYXQpLiBBbmggY28gdGhlIGhpZW4gY2hhbSBob24gZG9uZyBiYW8gaG9hbiB0YXQgdmFpCiAgICAvLyBnaWF5OiBqb2IgODVmZDk2YzA3YWU1IChiYW4gMS4zLjM5KSB4YWMgbmhhbiB4b25nLCBxdWV0IG1vdCBsYW4gdGhpCiAgICAvLyBjaHVhIHRodSBkdW9jIGFuaCBuYW8gbmVuIHBoYWkgbHVpIHZlIFRodSB2aWVuIHZhIGtldCBvIGNhbmggMiwgdHJvbmcKICAgIC8vIGtoaSBjYW5oIDEgdGFpIGR1b2MgYmFuZyBkdW9uZyBjdS4gQmFuIDEuMy40MCBjaG8gdG9pIGRhIDMwIGdpYXksCiAgICAvLyBxdWV0IGxhaSBtb2kgMS41IGdpYXkgZGVuIGtoaSBkdSBzbyBhbmggY3VhIGJhdGNoLgogICAgbGV0IGNoYXRJbWFnZXMgPSBbXTsKICAgIGlmIChjb25maXJtZWQpIHsKICAgICAgY29uc3QgaGFydmVzdERlYWRsaW5lID0gRGF0ZS5ub3coKSArIDMwMDAwOwogICAgICB3aGlsZSAodHJ1ZSkgewogICAgICAgIGNoYXRJbWFnZXMgPSBoYXJ2ZXN0Q2hhdEltYWdlcyhzdGF0ZSwgZXhwZWN0ZWRDb3VudCk7CiAgICAgICAgaWYgKGNoYXRJbWFnZXMubGVuZ3RoID49IGV4cGVjdGVkQ291bnQgfHwgRGF0ZS5ub3coKSA+PSBoYXJ2ZXN0RGVhZGxpbmUpIGJyZWFrOwogICAgICAgIGF3YWl0IHBhdXNlKDE1MDApOwogICAgICB9CiAgICB9CiAgICBpZiAoY29uZmlybWVkICYmIGNoYXRJbWFnZXMubGVuZ3RoID49IGV4cGVjdGVkQ291bnQpIHsKICAgICAgLy8gRFVPTkcgQ0hJTkggKHR1IGJhbiAxLjMuMzkpOiBndWkgdGhhbmcgdG9hbiBibyBhbmggY3VhIGJhdGNoIG5heSBuZ2F5CiAgICAgIC8vIHR1IGtodW5nIGNoYXQsIGtob25nIHNhbmcgVGh1IHZpZW4gdmEga2hvbmcgdGFpIHh1b25nIGJhbmcgQ2hyb21lCiAgICAgIC8vIG51YS4gRHVvbmcgVGh1IHZpZW4gY3UgcXVhIG1vbmcgbWFuaDogbHVvaSBraG9uZyBsbyB0ZW4gdGVwLCBjb24gZmlsZQogICAgICAvLyB0YWkgeHVvbmcgdGhpIGJpIHRpZW4gaWNoIGtoYWMgY2hpZW0gcXV5ZW4gZGF0IHRlbiBraGllbiBWZSBUYXkgdHUKICAgICAgLy8gY2hvaSAobG9pICJDaHJvbWUga2hvbmcgbHV1IGFuaCAySyBkdW5nIHRodSBtdWMgY3VhIHRhYyB2dSIpLiBOZ3VvaQogICAgICAvLyBkdW5nIGRhIGNoYXAgbmhhbiBkdW5nIGFuaCBuZ2F5IHRyb25nIGNoYXQgbyBjaGF0IGx1b25nIHRvdCBuaGF0LgogICAgICBjb25zdCBsaWJyYXJ5U3RhdGUgPSB7Li4uc3RhdGUsIHBoYXNlOiJsaWJyYXJ5IiwgY29uZmlybWVkLCBiYXRjaFN0YXJ0OmpvYi5pbmRleCwgY2hhdEltYWdlcywgaGFydmVzdERpYWc6bGFzdEhhcnZlc3REaWFnfTsKICAgICAgc2F2ZShsaWJyYXJ5U3RhdGUpOwogICAgICBmb3IgKGxldCBvZmZzZXQgPSAwOyBvZmZzZXQgPCBleHBlY3RlZENvdW50OyBvZmZzZXQrKykgewogICAgICAgIGF3YWl0IHVwbG9hZENoYXRJbWFnZShqb2IsIGpvYi5pbmRleCArIG9mZnNldCwgY2hhdEltYWdlc1tvZmZzZXRdKTsKICAgICAgfQogICAgICBhd2FpdCBhZHZhbmNlKGxpYnJhcnlTdGF0ZSk7CiAgICAgIHJldHVybjsKICAgIH0KICAgIC8vIE11c2UgY2FuIHNhdmUgaW1hZ2VzIGV2ZW4gd2hlbiB0aGUgY29tcGxldGlvbiBtZXNzYWdlIGlzIGFic2VudC4KICAgIHNhdmUoey4uLnN0YXRlLCBwaGFzZToibGlicmFyeSIsIGNvbmZpcm1lZCwgYmF0Y2hTdGFydDpqb2IuaW5kZXgsIGNoYXRJbWFnZXMsIGhhcnZlc3REaWFnOmxhc3RIYXJ2ZXN0RGlhZ30pOwogICAgbG9jYXRpb24uYXNzaWduKCJodHRwczovL211c2UuYWkvbGlicmFyeS9tZWRpYT92aWV3PWltYWdlcyIpOwogIH0KICBydW4oKS5jYXRjaChhc3luYyBlcnJvciA9PiB7CiAgICBjb25zdCBzdGF0ZSA9IEpTT04ucGFyc2Uoc2Vzc2lvblN0b3JhZ2UuZ2V0SXRlbShrZXkpIHx8ICJudWxsIik7CiAgICB0cnkgewogICAgICBpZiAoZXJyb3IgaW5zdGFuY2VvZiBTY2VuZUVycm9yICYmIHN0YXRlPy5qb2IpIHsKICAgICAgICBpZiAoc3RhdGUucGhhc2UgPT09ICJsaWJyYXJ5IiAmJiAhc3RhdGUubGlicmFyeVJldHJpZWQpIHsKICAgICAgICAgIHNhdmUoey4uLnN0YXRlLCBsaWJyYXJ5UmV0cmllZDp0cnVlfSk7CiAgICAgICAgICBsb2NhdGlvbi5yZXBsYWNlKCJodHRwczovL211c2UuYWkvbGlicmFyeS9tZWRpYT92aWV3PWltYWdlcyIpOwogICAgICAgICAgcmV0dXJuOwogICAgICAgIH0KICAgICAgICBpZiAoc3RhdGUuY29uZmlybWVkIHx8IC904bqjaSB4b25nfG3hu58gxJHGsOG7o2Mg4bqjbmgvaXUudGVzdChlcnJvci5tZXNzYWdlKSkKICAgICAgICAgIHRocm93IEVycm9yKCJLaMO0bmcgbOG6pXkgxJHGsOG7o2Mg4bqjbmggY8OzIHPhurVuIGPhu6dhIGPhuqNuaCAiICsgKHN0YXRlLmluZGV4ICsgMSkgKyAiICgiICsgc3RhdGUudGFnICsgIikuICIgKyBlcnJvci5tZXNzYWdlICsKICAgICAgICAgICAgKHN0YXRlLmhhcnZlc3REaWFnID8gIiAiICsgc3RhdGUuaGFydmVzdERpYWcgOiAiIikpOwogICAgICAgIGlmICghc3RhdGUuYXR0ZW1wdCkgewogICAgICAgICAgc2F2ZSh7cGhhc2U6ImNvbXBvc2UiLCBhdHRlbXB0OjEsIGRlbGF5OjIwMDAwLCBiYXRjaEVuZDpzdGF0ZS5iYXRjaEVuZH0pOwogICAgICAgICAgbG9jYXRpb24uYXNzaWduKCJodHRwczovL211c2UuYWkvIik7CiAgICAgICAgfSBlbHNlIHsKICAgICAgICAgIGF3YWl0IHNlbmQoe29wOiJza2lwTXVzZSIsIGpvYjpzdGF0ZS5qb2IsIGluZGV4OnN0YXRlLmluZGV4fSk7CiAgICAgICAgICBhd2FpdCBhZHZhbmNlKHN0YXRlKTsKICAgICAgICB9CiAgICAgICAgcmV0dXJuOwogICAgICB9CiAgICAgIHRocm93IGVycm9yOwogICAgfSBjYXRjaCAoZmFpbHVyZSkgewogICAgICBzZXNzaW9uU3RvcmFnZS5yZW1vdmVJdGVtKGtleSk7CiAgICAgIHRyeSB7IGF3YWl0IHNlbmQoe29wOiJlcnJvciIsIG1lc3NhZ2U6ZmFpbHVyZS5tZXNzYWdlfSk7IH0gY2F0Y2gge30KICAgIH0KICB9KTsKfSkoKTsKCn0K"
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

