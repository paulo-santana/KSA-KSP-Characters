#!/usr/bin/env python3
"""Checks that a change to the mod folder still loads, without the game.

Usage: check_mod.py [BASE_REF]

Without BASE_REF it checks the files as they are: mod.toml, every file and
asset the asset XML refers to (KSA 2026.11.10.5801's format: Id names a
top-level asset, Ref points at one), the glTF files (header, chunks, bone names
the XML and the animations use), the textures and CharacterManager.xml. With BASE_REF (the branch a pull
request goes into) it also checks the version: a change to a file that ships in
the zip needs a new version in mod.toml, higher than every v* tag, with
release-notes/v<version>.md.
"""

import json
import re
import struct
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
errors = []
earlier_ids = set()  # Ids defined by the asset files mod.toml lists before the current one
used_paths = set()  # files any asset file refers to
all_assets = {}  # Id -> top-level element, over every asset file


def error(msg):
    errors.append(msg)
    print(f"::error::{msg}")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def read_glb(path):
    """The JSON chunk of a GLB, after checking the header and the chunk layout."""
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError("too short for a GLB")
    magic, version, length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2:
        raise ValueError("not a glTF 2 binary")
    if length != len(data):
        raise ValueError(f"header says {length} bytes, file has {len(data)}")
    json_len, json_type = struct.unpack_from("<II", data, 12)
    if json_type != 0x4E4F534A:
        raise ValueError("first chunk is not JSON")
    doc = json.loads(data[20:20 + json_len])
    bin_len = 0
    if 20 + json_len < len(data):
        bin_len, bin_type = struct.unpack_from("<II", data, 20 + json_len)
        if bin_type != 0x004E4942:
            raise ValueError("second chunk is not BIN")
    for i, view in enumerate(doc.get("bufferViews", [])):
        if view.get("byteOffset", 0) + view["byteLength"] > bin_len:
            raise ValueError(f"bufferView {i} runs past the BIN chunk")
    return doc


def check_texture(path):
    head = path.read_bytes()[:12]
    signatures = {
        ".png": b"\x89PNG\r\n\x1a\n",
        ".ktx2": b"\xabKTX 20\xbb\r\n\x1a\n",
        ".dds": b"DDS ",
    }
    sig = signatures.get(path.suffix.lower())
    if sig is None:
        error(f"{path.relative_to(ROOT)}: unknown texture type")
    elif not head.startswith(sig):
        error(f"{path.relative_to(ROOT)}: not a valid {path.suffix} file")


def check_files():
    try:
        mod = tomllib.loads((ROOT / "mod.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        error(f"mod.toml: {e}")
        return None
    version = mod.get("version", "")
    if not SEMVER.match(version):
        error(f"mod.toml: version {version!r} is not X.Y.Z")
    if not mod.get("name"):
        error("mod.toml: no name")
    assets = mod.get("assets", [])
    if not assets:
        error("mod.toml: no assets")

    for asset in assets:
        xml_path = ROOT / asset
        if not xml_path.is_file():
            error(f"mod.toml: asset {asset} does not exist")
            continue
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError as e:
            error(f"{asset}: {e}")
            continue
        check_assets(asset, root)
        earlier_ids.update(el.attrib["Id"] for el in root if "Id" in el.attrib)
        all_assets.update({el.attrib["Id"]: el for el in root if "Id" in el.attrib})
    if "CharacterManager.xml" in assets:
        error("mod.toml: CharacterManager.xml is not a game asset (the game rejects its fields)")
    check_extensions(all_assets)
    shipped = set(git("ls-files", "Characters", "Textures").split())
    for f in sorted(shipped - used_paths):
        print(f"::warning::{f} ships in the zip but no asset file uses it")
    return version


def ref(el):
    """The asset an element points at: Ref since KSA 2026.11.10.5801 (Id before)."""
    return el.attrib.get("Ref", el.attrib.get("Id"))


def check_assets(asset, root):
    defined = {}
    for el in root:
        if "Id" in el.attrib:
            if el.attrib["Id"] in defined:
                error(f"{asset}: Id {el.attrib['Id']} is defined twice")
            defined[el.attrib["Id"]] = el
    known = {**{i: None for i in earlier_ids}, **defined}

    # Since KSA 5801 an Id only names a top-level asset; everything below points at one with Ref, and the mod defines
    # everything it uses. The game drops a whole asset that holds an element it doesn't know.
    for top in root:
        for el in top.iter():
            if el is top:
                continue
            if "Id" in el.attrib:
                error(f"{asset}: <{el.tag} Id=\"{el.attrib['Id']}\"> inside {top.attrib.get('Id', top.tag)}: use Ref (Id only names a top-level asset)")
            if "Ref" in el.attrib and el.attrib["Ref"] not in known:
                error(f"{asset}: <{el.tag} Ref=\"{el.attrib['Ref']}\"> inside {top.attrib.get('Id', top.tag)} points at nothing")
            if el.tag.startswith("CharacterManager."):
                error(f"{asset}: <{el.tag}> inside {top.attrib.get('Id', top.tag)}: the game drops that asset; it belongs in CharacterManager.xml")

    used = set()
    for el in root.iter():
        p = el.attrib.get("Path")
        if p is None:
            continue
        used.add(p)
        f = ROOT / p
        if not f.is_file():
            error(f"{asset}: <{el.tag} Path=\"{p}\"> does not exist (paths are case sensitive)")
        elif el.tag == "Texture":
            check_texture(f)

    def file_of(gltf_el):
        """The file a GltfFile loads: its Source points at a File asset."""
        src = gltf_el.find("Source")
        f = known.get(ref(src)) if src is not None else None
        return None if f is None or f.tag != "File" else ROOT / f.attrib.get("Path", "")

    gltf = {}
    for gid, el in defined.items():
        if el.tag != "GltfFile":
            continue
        f = file_of(el)
        if f is None or not f.is_file():
            error(f"{asset}: GltfFile {gid} has no File to load")
            continue
        try:
            gltf[gid] = read_glb(f)
        except (ValueError, json.JSONDecodeError, struct.error) as e:
            error(f"{f.relative_to(ROOT)}: {e}")

    # Animations drive the bones of their skeleton by name.
    for gid, el in defined.items():
        skel = el.find("Skeleton")
        if el.tag != "GltfFile" or skel is None or gid not in gltf:
            continue
        sid = ref(skel)
        if sid not in gltf:
            continue
        doc = gltf[gid]
        bones = {n.get("name") for n in gltf[sid].get("nodes", [])}
        if not doc.get("animations"):
            error(f"{gid}: has a Skeleton but no animation")
        missing = set()
        for anim in doc.get("animations", []):
            for ch in anim["channels"]:
                node = ch["target"].get("node")
                if node is None:
                    continue
                name = doc["nodes"][node].get("name")
                if name not in bones:
                    missing.add(name)
        if missing:
            error(f"{gid}: animates bones its skeleton {sid} lacks: {', '.join(sorted(map(str, missing))[:8])}")

    # Bone names and mesh indices the character definition uses.
    for core in (e for e in root if e.tag == "CharacterCore"):
        body = core.find("BodySource")
        doc = gltf.get(ref(body)) if body is not None else None
        if doc is None:
            continue
        bones = {n.get("name") for n in doc.get("nodes", [])}
        meshes = len(doc.get("meshes", []))
        for e in root.iter():
            name = e.attrib.get("Name")
            if name is not None and name not in bones:
                error(f"{asset}: <{e.tag} Name=\"{name}\"> is not a bone of {ref(body)}")
            if e.tag in ("MeshIndex", "FurMeshIndex") and int(e.attrib["Value"]) >= meshes:
                error(f"{asset}: <{e.tag} Value=\"{e.attrib['Value']}\"> but {ref(body)} has {meshes} meshes")

    used_paths.update(used)


def check_extensions(all_ids):
    """CharacterManager.xml, read only by the optional Character Manager: patches of the mod's own assets (Id with
    Modify="Patch", the same kind of element), every Ref naming an asset of the mod. The game must not load it."""
    path = ROOT / "CharacterManager.xml"
    if not path.is_file():
        return
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        error(f"CharacterManager.xml: {e}")
        return
    for block in root:
        target = all_ids.get(block.attrib.get("Id"))
        if block.attrib.get("Modify") != "Patch" or target is None or target.tag != block.tag:
            error(f"CharacterManager.xml: <{block.tag} Id=\"{block.attrib.get('Id')}\"> patches no {block.tag} of the mod")
        for el in block.iter():
            if "Ref" in el.attrib and el.attrib["Ref"] not in all_ids:
                error(f"CharacterManager.xml: <{el.tag} Ref=\"{el.attrib['Ref']}\"> points at nothing")


def version_key(v):
    return tuple(int(x) for x in SEMVER.match(v).groups())


def check_version(version, base):
    changed = git("diff", "--name-only", f"{base}...HEAD").split()
    ignored = set(git("check-attr", "export-ignore", "--", *changed).splitlines()) if changed else set()
    shipped = [f for f in changed if f"{f}: export-ignore: set" not in ignored]
    base_mod = tomllib.loads(git("show", f"{base}:mod.toml"))
    bumped = version != base_mod.get("version")
    if shipped and not bumped:
        error(f"files that ship in the zip changed ({', '.join(shipped[:5])}...) but mod.toml still says {version}")
    if not bumped or not SEMVER.match(version):
        return
    tags = [t[1:] for t in git("tag", "--list", "v*").split() if SEMVER.match(t[1:])]
    if tags and version_key(version) <= max(map(version_key, tags)):
        error(f"version {version} is not higher than the last release v{max(tags, key=version_key)}")
    notes = ROOT / "release-notes" / f"v{version}.md"
    if not notes.is_file() or not notes.read_text().strip():
        error(f"release-notes/v{version}.md is missing or empty")
    print(f"This pull request releases {version} when it is merged.")


version = check_files()
if len(sys.argv) > 1 and version:
    check_version(version, sys.argv[1])
if errors:
    sys.exit(f"{len(errors)} problem(s)")
print("The mod checks out.")
