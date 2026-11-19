#!/usr/bin/env python3
"""Record color, depth (aligned to color) and IMU from a RealSense on macOS.

Run through ./record.sh (it handles sudo). Output, one folder per take:

    recordings/<timestamp>/
        color/000000.jpg ...      RGB frames (or .png with --png)
        depth/000000.png ...      16-bit depth aligned to the color image (units: see depth_scale)
        frames.csv                index, timestamps, file names
        imu.csv                   accel (m/s^2) and gyro (rad/s) samples, timestamped
        camera.json               intrinsics, distortion, depth scale, extrinsics, device info

For a Gaussian splat you mostly need color/ -- see pick_frames.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("opencv is missing -- run ./setup.sh")
try:
    import pyrealsense2 as rs
except ImportError:
    sys.exit("pyrealsense2 is missing -- run ./setup.sh")


def parse_mode(text: str) -> tuple[int, int, int]:
    """'1280x720@30' -> (1280, 720, 30)."""
    try:
        size, fps = text.lower().split("@")
        w, h = size.split("x")
        return int(w), int(h), int(fps)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT@FPS, got {text!r}")


def intr_dict(intr) -> dict:
    return {
        "width": intr.width, "height": intr.height,
        "fx": intr.fx, "fy": intr.fy, "cx": intr.ppx, "cy": intr.ppy,
        "model": str(intr.model), "coeffs": list(intr.coeffs),
    }


def extr_dict(ex) -> dict:
    return {"rotation_colmajor_3x3": list(ex.rotation), "translation_m": list(ex.translation)}


def build_config(args, serial: str, with_imu: bool):
    cfg = rs.config()
    cfg.enable_device(serial)
    cw, ch, cf = args.color
    cfg.enable_stream(rs.stream.color, cw, ch, rs.format.bgr8, cf)
    if not args.no_depth:
        dw, dh, df = args.depth
        cfg.enable_stream(rs.stream.depth, dw, dh, rs.format.z16, df)
    if with_imu:
        cfg.enable_stream(rs.stream.accel)
        cfg.enable_stream(rs.stream.gyro)
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, help="output folder (default: recordings/<timestamp>)")
    ap.add_argument("--color", type=parse_mode, default=parse_mode("1280x720@30"),
                    help="color mode, e.g. 1920x1080@30 (sharpest for splats) or 1280x720@30 (default)")
    ap.add_argument("--depth", type=parse_mode, default=parse_mode("848x480@30"),
                    help="depth mode (default 848x480@30, the D435i's best depth mode)")
    ap.add_argument("--no-depth", action="store_true", help="color (+IMU) only")
    ap.add_argument("--no-imu", action="store_true", help="skip accel/gyro")
    ap.add_argument("--png", action="store_true", help="save color as lossless PNG instead of JPEG (much bigger)")
    ap.add_argument("--jpeg-quality", type=int, default=95)
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (default: until Ctrl+C)")
    ap.add_argument("--warmup", type=float, default=2.0, help="seconds to let auto-exposure settle before saving")
    ap.add_argument("--lock-exposure", action="store_true",
                    help="freeze exposure/gain/white balance after warmup (steadier colors for splats)")
    ap.add_argument("--preview", action="store_true", help="show a live preview window (press q to stop)")
    ap.add_argument("--owner", help=argparse.SUPPRESS)  # set by record.sh: chown output back to this user
    args = ap.parse_args()

    if sys.platform == "darwin" and os.geteuid() != 0:
        print("WARNING: not running as root -- macOS usually won't hand the camera over. Use ./record.sh.")

    # ---- find the camera --------------------------------------------------
    ctx = rs.context()
    devs = ctx.query_devices()
    if len(devs) == 0:
        sys.exit("No RealSense found. Use a USB 3 port (direct, no hub) and the camera's own cable, "
                 "and close realsense-viewer or anything else using it.")
    try:
        dev = devs[0]
    except RuntimeError as exc:
        sys.exit(f"Found a RealSense but could not open it ({exc}). On macOS run via ./record.sh (needs sudo).")
    name = dev.get_info(rs.camera_info.name)
    serial = dev.get_info(rs.camera_info.serial_number)
    usb = dev.get_info(rs.camera_info.usb_type_descriptor) if dev.supports(rs.camera_info.usb_type_descriptor) else "?"
    fw = dev.get_info(rs.camera_info.firmware_version)
    print(f"Camera: {name}  serial {serial}  firmware {fw}  USB {usb}")
    if usb.startswith("2"):
        print("WARNING: camera is on USB 2 -- expect dropped frames / missing modes. Use a USB 3 port + cable.")

    # ---- output folder ------------------------------------------------------
    here = Path(__file__).resolve().parent
    out = args.out or here / "recordings" / datetime.now().strftime("%Y%m%d_%H%M%S")
    (out / "color").mkdir(parents=True, exist_ok=True)
    if not args.no_depth:
        (out / "depth").mkdir(parents=True, exist_ok=True)

    # ---- frames arrive on librealsense's thread; hand them to ours ----------
    video_q: queue.Queue = queue.Queue(maxsize=90)
    imu_rows: list[tuple] = []
    imu_lock = threading.Lock()
    stats = {"dropped": 0, "accel": 0, "gyro": 0}

    def on_frame(frame):
        if frame.is_frameset():
            fs = frame.as_frameset()
            fs.keep()  # keep it alive outside librealsense's frame pool
            try:
                video_q.put_nowait(fs)
            except queue.Full:
                stats["dropped"] += 1
        elif frame.is_motion_frame():
            m = frame.as_motion_frame()
            d = m.get_motion_data()
            kind = "accel" if m.get_profile().stream_type() == rs.stream.accel else "gyro"
            with imu_lock:
                imu_rows.append((f"{m.get_timestamp():.4f}", kind, d.x, d.y, d.z))
                stats[kind] += 1

    pipe = rs.pipeline(ctx)
    want_imu = not args.no_imu
    try:
        profile = pipe.start(build_config(args, serial, want_imu), on_frame)
    except RuntimeError as exc:
        if not want_imu:
            raise
        print(f"IMU could not be started ({exc}); recording without it.")
        want_imu = False
        profile = pipe.start(build_config(args, serial, False), on_frame)

    # ---- camera.json ---------------------------------------------------------
    color_sp = profile.get_stream(rs.stream.color).as_video_stream_profile()
    meta: dict = {
        "device": {"name": name, "serial": serial, "firmware": fw, "usb": usb,
                   "librealsense": getattr(rs, "__version__", "?")},
        "color": {**intr_dict(color_sp.get_intrinsics()), "fps": color_sp.fps(),
                  "format": "png" if args.png else "jpg"},
        "note": "depth/*.png are aligned to the color camera, so use the color intrinsics for them. "
                "depth_m = pixel * depth_scale. extrinsics map points FROM the named stream TO color.",
        "extrinsics_to_color": {},
    }
    if not args.no_depth:
        depth_sp = profile.get_stream(rs.stream.depth).as_video_stream_profile()
        meta["depth_scale"] = profile.get_device().first_depth_sensor().get_depth_scale()
        meta["depth_raw_intrinsics"] = intr_dict(depth_sp.get_intrinsics())
        meta["extrinsics_to_color"]["depth"] = extr_dict(depth_sp.get_extrinsics_to(color_sp))
    if want_imu:
        for s in (rs.stream.accel, rs.stream.gyro):
            try:
                sp = profile.get_stream(s)
                meta["extrinsics_to_color"][str(s).split(".")[-1]] = extr_dict(sp.get_extrinsics_to(color_sp))
                meta.setdefault("imu_rates_hz", {})[str(s).split(".")[-1]] = sp.fps()
            except RuntimeError:
                pass
    (out / "camera.json").write_text(json.dumps(meta, indent=2))

    align = None if args.no_depth else rs.align(rs.stream.color)
    color_sensor = profile.get_device().first_color_sensor()

    # ---- writers --------------------------------------------------------------
    pool = ThreadPoolExecutor(max_workers=4)
    pending = threading.BoundedSemaphore(60)
    ext = "png" if args.png else "jpg"
    color_params = [cv2.IMWRITE_PNG_COMPRESSION, 1] if args.png else [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality]

    def write(path: Path, img, params):
        try:
            cv2.imwrite(str(path), img, params)
        finally:
            pending.release()

    frames_csv = open(out / "frames.csv", "w", newline="")
    fw_csv = csv.writer(frames_csv)
    fw_csv.writerow(["index", "color_timestamp_ms", "depth_timestamp_ms", "color_file", "depth_file"])

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    print(f"Saving to {out}")
    print(f"Warming up {args.warmup:.0f}s ..." if args.warmup > 0 else "", end="", flush=True)
    t_start = time.time()
    saving_from = t_start + args.warmup
    locked = False
    index = 0
    last_status = 0.0

    try:
        while not stop.is_set():
            now = time.time()
            if args.seconds and now - saving_from >= args.seconds:
                break
            try:
                fs = video_q.get(timeout=1.0)
            except queue.Empty:
                if now - t_start > 5 and index == 0:
                    print("\nNo frames from the camera after 5 s -- is it on USB 3 and not used by another app?")
                continue

            if align is not None:
                fs = align.process(fs)
            cf = fs.get_color_frame()
            if not cf:
                continue
            color = np.asanyarray(cf.get_data()).copy()
            df = fs.get_depth_frame() if align is not None else None
            depth = np.asanyarray(df.get_data()).copy() if df else None

            if now < saving_from:
                continue

            if args.lock_exposure and not locked:
                locked = True
                lock_exposure(color_sensor, cf)

            pending.acquire()
            cname = f"{index:06d}.{ext}"
            pool.submit(write, out / "color" / cname, color, color_params)
            dname = ""
            if depth is not None:
                dname = f"{index:06d}.png"
                pending.acquire()
                pool.submit(write, out / "depth" / dname, depth, [cv2.IMWRITE_PNG_COMPRESSION, 1])
            fw_csv.writerow([index, f"{cf.get_timestamp():.4f}",
                             f"{df.get_timestamp():.4f}" if df else "", f"color/{cname}",
                             f"depth/{dname}" if dname else ""])
            index += 1

            if args.preview and index % 2 == 0:
                show = cv2.resize(color, (640, int(640 * color.shape[0] / color.shape[1])))
                if depth is not None:
                    dv = cv2.applyColorMap(cv2.convertScaleAbs(depth, alpha=255.0 / 4000), cv2.COLORMAP_TURBO)
                    show = np.hstack([show, cv2.resize(dv, (show.shape[1], show.shape[0]))])
                cv2.imshow("RealSense recorder (q = stop)", show)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break

            if now - last_status > 1.0:
                last_status = now
                el = now - saving_from
                print(f"\r  {el:6.1f}s  frames {index:6d} ({index / max(el, 1e-3):4.1f} fps)  "
                      f"accel {stats['accel']}  gyro {stats['gyro']}  dropped {stats['dropped']}   ",
                      end="", flush=True)
    finally:
        print("\nStopping ...")
        try:
            pipe.stop()
        except RuntimeError:
            pass
        pool.shutdown(wait=True)
        frames_csv.close()
        with open(out / "imu.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_ms", "sensor", "x", "y", "z"])
            with imu_lock:
                w.writerows(sorted(imu_rows, key=lambda r: float(r[0])))
        if args.preview:
            cv2.destroyAllWindows()
        if args.owner:
            os.system(f"chown -R {args.owner} '{out}'")

    print(f"Saved {index} frames, {stats['accel']} accel + {stats['gyro']} gyro samples "
          f"({stats['dropped']} frames dropped) -> {out}")
    if want_imu and stats["accel"] == 0:
        print("NOTE: no IMU samples arrived. Color/depth are fine; the IMU is optional for splatting.")
    return 0


def lock_exposure(sensor, frame) -> None:
    """Freeze the current auto exposure / gain / white balance (best effort)."""
    md = rs.frame_metadata_value
    try:
        exp = frame.get_frame_metadata(md.actual_exposure) if frame.supports_frame_metadata(md.actual_exposure) else None
        gain = frame.get_frame_metadata(md.gain_level) if frame.supports_frame_metadata(md.gain_level) else None
        wb = frame.get_frame_metadata(md.white_balance) if frame.supports_frame_metadata(md.white_balance) else None
        if sensor.supports(rs.option.enable_auto_white_balance):
            sensor.set_option(rs.option.enable_auto_white_balance, 0)
            if wb:
                sensor.set_option(rs.option.white_balance, float(wb))
        if exp is None:
            print("\n(camera did not report its exposure; leaving auto exposure on, white balance locked)")
            return
        sensor.set_option(rs.option.enable_auto_exposure, 0)
        sensor.set_option(rs.option.exposure, float(exp))
        if gain is not None:
            sensor.set_option(rs.option.gain, float(gain))
        print(f"\nLocked exposure={exp} gain={gain} white_balance={wb}")
    except RuntimeError as exc:
        print(f"\n(could not lock exposure: {exc}; leaving it on auto)")
        try:
            sensor.set_option(rs.option.enable_auto_exposure, 1)
        except RuntimeError:
            pass


if __name__ == "__main__":
    sys.exit(main())
