"""Isaac Sim against the composer: each spec solved once, then drawn by both from the same cameras.

    python parity/compare.py SPEC [SPEC ...] --out DIR [--seed N] [--timeout MIN] [--metrics PATH]
        [--write-baseline]

For each spec, in DIR/<spec>/:

1. The composer solves the layout and frames cameras on it (`cameras.recipes`): the objects, the
   robot, both, the background's own view, and a close-up of each object.
2. Isaac Sim builds the spec in a process of its own (`isaac_capture.py`), resets it, reports where
   physics left everything and renders each camera with RTX.
3. The composer draws the scene moved to those poses, from the same cameras.

DIR/<spec>-<camera>.png sets Isaac Sim's picture left of the composer's.

A camera is scored by how far the composer's colours are from Isaac's once exposure is matched: one
gain per pair, fitted in linear light, then the mean difference in sRGB, 0-255, over 8x8-pixel
blocks. The composer's backdrop is left out, since Isaac shows its sky there, and so is a camera Isaac
renders dark, which stands inside something. A camera fails when its error rises more than
`TOLERANCE` above `baseline.json`'s; `--write-baseline` records this run's errors there instead.

Artefacts ignores exit codes and passes a run that leaves no tests_junit.xml, so one is written on
every path: a suite per spec, a case per camera, and an error case for a step that failed.

Runs in Arena's venv, with Isaac Sim installed in it, its EULA accepted (`OMNI_KIT_ACCEPT_EULA=YES`),
and a GPU RTX renders on.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path

import msgpack
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import cameras

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"
#: How far a camera's error may rise above its baseline, 0-255: RTX's renders vary a little run to run.
TOLERANCE = 3.0
#: The composer's backdrop: `arena-render` sets no clear colour, so it is Bevy's default, which the
#: tonemapper leaves alone. Not the window's, `viewer.rs`'s `BACKDROP`.
BACKDROP = np.array([43, 44, 47])
BLOCK = 8


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42, help="Placement seed, the same for both (default 42)")
    parser.add_argument("--size", type=int, default=768)
    parser.add_argument("--timeout", type=float, default=20, help="Minutes before Isaac Sim is killed, per spec")
    parser.add_argument("--metrics", type=Path, help="Also write metrics.json here")
    parser.add_argument("--write-baseline", action="store_true", help=f"Record this run's errors in {BASELINE.name}")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}

    # A failure before the first spec is a case too: an empty report reads as a passing run.
    suites, metrics = {"setup": [("setup", "error", "did not get past setting up")]}, {}
    try:
        from arena_composer import catalog
        from arena_composer.usd_assets import use_local_asset_cache

        catalog.load_extensions(())
        use_local_asset_cache(None)
        del suites["setup"]
        for spec in args.specs:
            name = spec.stem
            try:
                suites[name], errors = compare(spec.resolve(), args.out / name, args, baseline.get(name, {}))
            except Exception as error:  # one spec that cannot be compared leaves the others to be
                traceback.print_exc()
                suites[name], errors = [("compare", "error", f"{type(error).__name__}: {error}")], {}
            if errors:
                matched = [e["matched"] for e in errors.values()]
                metrics[f"{name}.error"] = round(float(np.mean(matched)), 2)
                metrics[f"{name}.raw_error"] = round(float(np.mean([e["raw"] for e in errors.values()])), 2)
                metrics[f"{name}.gain"] = round(float(np.median([e["gain"] for e in errors.values()])), 3)
                metrics[f"{name}.cameras"] = len(errors)
                if args.write_baseline:
                    # Per camera: one this run skipped keeps the error it had.
                    baseline.setdefault(name, {}).update({cam: e["matched"] for cam, e in errors.items()})
    finally:
        write_junit(args.out / "tests_junit.xml", suites)
        for path in filter(None, [args.out / "metrics.json", args.metrics]):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(metrics, indent=2) + "\n")
        print(f"[parity] {metrics}", flush=True)
    if args.write_baseline:
        BASELINE.write_text(json.dumps(baseline, indent=1, sort_keys=True) + "\n")
    return 1 if any(kind in ("failure", "error") for cases in suites.values() for _, kind, _ in cases) else 0


def compare(spec: Path, work: Path, args, baseline: dict) -> tuple[list, dict]:
    """One spec's cases, ``(name, kind, message)`` with kind None, "failure", "error" or "skipped",
    and the errors of the cameras scored."""
    from arena_composer.payload import build_payload
    from arena_composer.render import render
    from arena_composer.resolve_layout import resolve_layout_from_yaml

    work.mkdir(parents=True, exist_ok=True)
    dump = resolve_layout_from_yaml(spec, seed=args.seed)
    (work / "cams.json").write_text(json.dumps(cameras.recipes(dump), indent=1))

    state, message = capture(spec, work, args)
    if state is None:
        return [("isaac_capture", "error", message)], {}
    cases, errors = [], {}
    missing = repose(dump, state)
    if missing:
        # The composer draws these where it solved them, so the pictures differ by more than drawing.
        cases.append(("poses", None, f"not in the sim, drawn where the composer put them: {', '.join(missing)}"))

    payload = build_payload(dump, remote=False)
    for cam in state["cams"]:
        name = cam["name"]
        isaac, composer = work / f"isaac_{name}.png", work / f"composer_{name}.png"
        payload["viewer"] = {"eye": cam["eye"], "lookat": cam["lookat"]}
        try:
            render(msgpack.packb(payload, use_bin_type=True), {"default": composer}, args.size)
        except RuntimeError as error:
            cases.append((name, "failure", f"the composer did not draw it: {error}"))
            continue
        pair(isaac, composer, args.out / f"{work.name}-{name}.png")
        score = compare_pictures(isaac, composer)
        if isinstance(score, str):
            cases.append((name, "skipped", score))
            continue
        errors[name] = score
        before = baseline.get(name)
        message = f"error {score['matched']:.1f} (raw {score['raw']:.1f}, gain {score['gain']:.2f})"
        if before is None:
            cases.append((name, None, f"{message}, no baseline"))
        elif score["matched"] > before + TOLERANCE:
            cases.append((name, "failure", f"{message}, up from {before:.1f}: further from Isaac Sim"))
        else:
            cases.append((name, None, f"{message}, baseline {before:.1f}"))
    return cases, errors


def capture(spec: Path, work: Path, args) -> tuple[dict | None, str]:
    """Run `isaac_capture.py` on ``spec``; the sim's state, or None and why not."""
    cmd = [sys.executable, str(HERE / "isaac_capture.py"), "--env_spec", str(spec), "--cams", str(work / "cams.json"),
           "--out", str(work), "--placement_seed", str(args.seed), "--size", str(args.size)]  # fmt: skip
    # FORCE_EXIT skips Kit's app.close(), which can hang after the pictures are written.
    env = {**os.environ, "ISAACLAB_ARENA_FORCE_EXIT_ON_COMPLETE": "1", "PYTHONUNBUFFERED": "1"}
    log = work / "isaac.log"
    (work / "sim_state.json").unlink(missing_ok=True)
    with open(log, "w") as output:
        # Own process group, so the kill below also reaches Kit's helper processes. No stdin: Kit asks
        # for the EULA there when OMNI_KIT_ACCEPT_EULA is unset, and would wait out the timeout.
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
        try:
            returncode = proc.wait(timeout=args.timeout * 60)
        except subprocess.TimeoutExpired:
            returncode = None
        finally:
            # SIGKILL: Kit handles SIGTERM itself.
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    if (work / "sim_state.json").exists():
        return json.loads((work / "sim_state.json").read_text()), ""
    ended = f"was killed after {args.timeout:g} min" if returncode is None else f"exited with {returncode}"
    tail = "\n".join(log.read_text(errors="replace").splitlines()[-30:])
    return None, f"isaac_capture.py {ended} before writing sim_state.json; the end of {log.name}:\n{tail}"


def repose(dump: dict, state: dict) -> list[str]:
    """Move each asset in ``dump`` to where the sim left it; return the ids the sim does not have."""
    articulations, missing = dict(state["articulations"]), []
    for asset in dump["assets"]:
        if asset["role"] == "embodiment":
            settled = articulations.pop("robot", None) or articulations.pop(asset["name"], None)
        else:
            settled = state["rigid"].get(asset["name"]) or articulations.pop(asset["name"], None)
            settled = settled or state["usd"].get(asset["name"])
        if settled is None:
            missing.append(asset["id"])
            continue
        asset["position_xyz"], asset["rotation_xyzw"] = settled["pos"], settled["quat_xyzw"]
        if asset["role"] == "embodiment":
            asset["joint_pos"] = settled["joints"]
    return missing


def compare_pictures(isaac: Path, composer: Path) -> dict | str:
    """The pair's error, 0-255, raw and with exposure matched, and the gain that matched it; or why
    the pair is not scored."""
    i8 = np.asarray(Image.open(isaac).convert("RGB"), dtype=np.float64)
    c8 = np.asarray(Image.open(composer).convert("RGB"), dtype=np.float64)
    backdrop = (np.abs(c8 - BACKDROP).max(axis=2) <= 3).astype(np.float64)[..., None]
    isaac_light, composer_light, keep = _pool(_linear(i8)), _pool(_linear(c8)), _pool(backdrop)[..., 0] == 0
    if keep.mean() < 0.1:
        return "all but empty in the composer's picture: the camera sees past the scene"
    if isaac_light[keep].mean() < 0.01:
        return "dark in Isaac Sim's picture: the camera stands inside something"
    I, C = isaac_light[keep], composer_light[keep]
    gain = float((I * C).sum() / max((C**2).sum(), 1e-12))
    raw = float(np.abs(_srgb(C) - _srgb(I)).mean() * 255)
    matched = float(np.abs(_srgb(gain * C) - _srgb(I)).mean() * 255)
    return {"raw": round(raw, 2), "matched": round(matched, 2), "gain": round(gain, 3)}


def _linear(srgb8: np.ndarray) -> np.ndarray:
    srgb = srgb8 / 255.0
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def _srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def _pool(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[0] // BLOCK, image.shape[1] // BLOCK
    return image[: h * BLOCK, : w * BLOCK].reshape(h, BLOCK, w, BLOCK, -1).mean(axis=(1, 3))


def pair(isaac: Path, composer: Path, out: Path, size: int = 512) -> None:
    """Isaac Sim's picture left of the composer's, each labelled."""
    sheet = Image.new("RGB", (2 * size + 8, size + 32), "white")
    draw, font = ImageDraw.Draw(sheet), ImageFont.load_default(size=20)
    for i, (path, label) in enumerate(((isaac, "Isaac Sim"), (composer, "composer"))):
        sheet.paste(Image.open(path).convert("RGB").resize((size, size)), (i * (size + 8), 32))
        draw.text((i * (size + 8) + 6, 4), f"{label}: {out.stem}", fill="black", font=font)
    sheet.save(out)


def write_junit(path: Path, suites: dict) -> None:
    root = ET.Element("testsuites")
    for suite_name, cases in suites.items():
        kinds = [kind for _, kind, _ in cases]
        suite = ET.SubElement(root, "testsuite", name=suite_name, tests=str(len(cases)),
                              failures=str(kinds.count("failure")), errors=str(kinds.count("error")),
                              skipped=str(kinds.count("skipped")))  # fmt: skip
        for name, kind, message in cases:
            case = ET.SubElement(suite, "testcase", classname=suite_name, name=name)
            if kind:
                ET.SubElement(case, kind, message=message)
            elif message:
                ET.SubElement(case, "system-out").text = message
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    sys.exit(main())
