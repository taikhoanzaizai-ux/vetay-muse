"""Kiem tra dong bo VeTay <-> tien ich Chrome (chay: python tools/check_sync.py).

Bao loi (exit 1) neu bat ky cap nao sau day lech nhau:
- VERSION trong app.py  ==  version.txt  ==  update.json["version"]
- Ban tien ich nhung trong app.py (BUNDLED_EXTENSION)  ==  thu muc chrome-extension/
  ==  companion.json  (so phien ban + ma blob SHA-1 + noi dung giai ma base64)
- PATCH_ROOT trong app.py phai tro ve repo vetay-muse.
"""
import ast
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("manifest.json", "popup.html", "worker.js", "flow.js")


def blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def main() -> int:
    errors = []
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    vals = {}
    app_version = None
    patch_root = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", "")
            if target in ("BUNDLED_EXTENSION", "BUNDLED_EXTENSION_FILES"):
                vals[target] = ast.literal_eval(node.value)
            elif target == "VERSION":
                app_version = ast.literal_eval(node.value)
            elif target == "PATCH_ROOT":
                patch_root = ast.literal_eval(node.value)

    version_txt = (ROOT / "version.txt").read_text(encoding="ascii").strip()
    update = json.loads((ROOT / "update.json").read_text(encoding="utf-8"))
    if not (app_version == version_txt == update["version"]):
        errors.append(f"Phien ban app lech: app.py={app_version} version.txt={version_txt} update.json={update['version']}")
    if patch_root != "https://raw.githubusercontent.com/taikhoanzaizai-ux/vetay-muse/":
        errors.append(f"PATCH_ROOT khong tro ve vetay-muse: {patch_root}")

    bundled = vals["BUNDLED_EXTENSION"]
    blobs = vals["BUNDLED_EXTENSION_FILES"]
    companion = json.loads((ROOT / "companion.json").read_text(encoding="utf-8"))
    folder_manifest = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8"))
    if not (bundled["version"] == companion["version"] == folder_manifest["version"]):
        errors.append(
            f"Phien ban tien ich lech: nhung={bundled['version']} companion={companion['version']} thu muc={folder_manifest['version']}"
        )
    for name in NAMES:
        disk = (ROOT / "chrome-extension" / name).read_bytes()
        decoded = base64.b64decode(blobs[name], validate=True)
        if decoded != disk:
            errors.append(f"{name}: noi dung nhung trong app.py khac file trong chrome-extension/")
        if blob_sha1(disk) != bundled["files"][name]:
            errors.append(f"{name}: ma blob trong BUNDLED_EXTENSION khong khop file")
        if companion["files"].get(name) != bundled["files"][name]:
            errors.append(f"{name}: ma blob trong companion.json khong khop BUNDLED_EXTENSION")

    if errors:
        print("LECH DONG BO:")
        for error in errors:
            print(" -", error)
        return 1
    print(f"DONG BO OK: app {app_version}, tien ich Chrome {bundled['version']}, PATCH_ROOT -> vetay-muse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
