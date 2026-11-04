"""Build an installable Blender extension zip from addon/.

    python3 build.py            -> dist/multi_user-<version>.zip

The replication package is packed back into a wheel with a local version tag
(0.10.3+mu5), so Blender extracts it instead of reusing an upstream 0.10.3
wheel it has seen before. Text files are written with LF line endings.
Needs only the Python standard library.
"""
import base64, hashlib, io, os, re, shutil, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "addon")
DIST = os.path.join(HERE, "dist")
UP_VERSION, TAG = "0.10.3", "mu5"
WHEEL_VERSION = f"{UP_VERSION}+{TAG}"
WHEEL_NAME = f"replication-{WHEEL_VERSION}-py3-none-any.whl"
TEXT = (".py", ".toml", ".txt", ".md")


def read(path):
    with open(path, "rb") as f:
        data = f.read()
    name = os.path.basename(path)
    if name.endswith(TEXT) or name in ("METADATA", "WHEEL", "LICENSE"):
        data = data.replace(b"\r\n", b"\n")
    return data


def walk(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if not f.endswith((".pyc", ".rej", ".orig")):
                yield os.path.join(d, f)


def build_wheel():
    old_info = f"replication-{UP_VERSION}.dist-info"
    new_info = f"replication-{WHEEL_VERSION}.dist-info"
    entries = []
    for path in walk(os.path.join(ADDON, "replication")):
        entries.append((os.path.relpath(path, ADDON).replace(os.sep, "/"), read(path)))
    for path in walk(os.path.join(ADDON, old_info)):
        rel = os.path.relpath(path, os.path.join(ADDON, old_info)).replace(os.sep, "/")
        if rel == "RECORD":
            continue
        data = read(path)
        if rel == "METADATA":
            data = re.sub(rb"(?m)^Version: .*$", f"Version: {WHEEL_VERSION}".encode(), data)
        entries.append((f"{new_info}/{rel}", data))
    record = []
    for name, data in entries:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={digest},{len(data)}")
    record.append(f"{new_info}/RECORD,,")
    entries.append((f"{new_info}/RECORD", ("\n".join(record) + "\n").encode()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def main():
    src = os.path.join(ADDON, "multi_user")
    manifest = read(os.path.join(src, "blender_manifest.toml")).decode()
    version = re.search(r'(?m)^version = "(.*)"', manifest).group(1)
    manifest = manifest.replace(f"./wheels/replication-{UP_VERSION}-py3-none-any.whl",
                                f"./wheels/{WHEEL_NAME}")
    if WHEEL_NAME not in manifest:
        sys.exit("blender_manifest.toml does not list the replication wheel")
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"multi_user-{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in walk(src):
            rel = os.path.relpath(path, src).replace(os.sep, "/")
            z.writestr(rel, manifest.encode() if rel == "blender_manifest.toml" else read(path))
        z.writestr(f"wheels/{WHEEL_NAME}", build_wheel())
    print(out)


if __name__ == "__main__":
    main()
