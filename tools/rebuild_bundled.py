"""Sinh lai ban tien ich nhung trong app.py tu thu muc chrome-extension/.

Quy uoc: chrome-extension/ la NGUON DUY NHAT. Moi khi sua file trong
chrome-extension/ (ke ca viec tang so phien ban trong manifest.json),
chay:  python tools/rebuild_bundled.py
Script nay cap nhat BUNDLED_EXTENSION (version + ma blob) va
BUNDLED_EXTENSION_FILES (base64) trong app.py, dong thoi cap nhat
companion.json, de co che tu dong bo cua app khong bao gio bi lech.
Sau khi chay, kiem tra lai bang:  python tools/check_sync.py
"""
import ast
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("manifest.json", "popup.html", "worker.js", "flow.js")


def blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def main() -> int:
    ext = ROOT / "chrome-extension"
    files = {}
    for name in NAMES:
        data = (ext / name).read_bytes()
        files[name] = (blob_sha1(data), base64.b64encode(data).decode())
    version = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))["version"]

    app_path = ROOT / "app.py"
    src = app_path.read_text(encoding="utf-8")
    vals = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", "")
            if target in ("BUNDLED_EXTENSION", "BUNDLED_EXTENSION_FILES"):
                vals[target] = ast.literal_eval(node.value)
    bundled = vals["BUNDLED_EXTENSION"]
    start = src.find("BUNDLED_EXTENSION = {")
    if start < 0:
        print("Khong tim thay BUNDLED_EXTENSION trong app.py")
        return 1
    old_version = bundled["version"]
    pos = src.find(f'"version": "{old_version}"', start)
    src = src[:pos] + f'"version": "{version}"' + src[pos + len(f'"version": "{old_version}"'):]
    for name, (blob, _b64) in files.items():
        old_hash = bundled["files"][name]
        pos = src.find(f'"{name}": "{old_hash}"', start)
        if pos < 0:
            print(f"Khong tim thay ma blob cua {name} trong BUNDLED_EXTENSION")
            return 1
        src = src[:pos] + f'"{name}": "{blob}"' + src[pos + len(f'"{name}": "{old_hash}"'):]
    old_blobs = vals["BUNDLED_EXTENSION_FILES"]
    for name, (_blob, b64) in files.items():
        old = old_blobs[name]
        if f'"{name}": "{old}"' not in src:
            print(f"Khong tim thay base64 cua {name} trong BUNDLED_EXTENSION_FILES")
            return 1
        src = src.replace(f'"{name}": "{old}"', f'"{name}": "{b64}"', 1)
    app_path.write_text(src, encoding="utf-8")

    companion_path = ROOT / "companion.json"
    companion = json.loads(companion_path.read_text(encoding="utf-8"))
    companion["version"] = version
    companion["files"] = {name: files[name][0] for name in ("worker.js", "manifest.json", "flow.js", "popup.html")}
    companion_path.write_text(json.dumps(companion, indent=2) + "\n", encoding="utf-8")
    print(f"Da sinh lai ban nhung: tien ich {old_version} -> {version}. Hay chay tools/check_sync.py de xac nhan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
