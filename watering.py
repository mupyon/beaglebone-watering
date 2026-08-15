#!/usr/bin/env python3
"""Multi-line BeagleBone irrigation controller using GPIO sysfs."""

import argparse
import datetime as dt
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path("/opt/watering")
DEFAULT_CONFIG = BASE_DIR / "config.json"
REQUIRED_LINE_KEYS = {
    "gpio", "water_balance", "watering_threshold", "et0_crop_factor",
    "rain_efficiency", "default_watering_seconds", "watering_calibration",
}


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class Controller:
    def __init__(self, config_path, dry_run=False):
        self.config_path = Path(config_path).resolve()
        self.base_dir = self.config_path.parent
        self.state_path = self.base_dir / "state.json"
        self.log_path = self.base_dir / "watering.log"
        self.dry_run = dry_run
        with self.config_path.open(encoding="utf-8") as handle:
            self.config = json.load(handle)
        self._validate_config()
        self.lines = self.config["lines"]
        self.gpio_root = Path(self.config.get("gpio_root", "/sys/class/gpio"))
        self.active_high = bool(self.config.get("active_high", True))
        self.state = self._load_state()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(self.log_path), logging.StreamHandler()],
        )

    def _validate_config(self):
        for key in ("latitude", "longitude", "lines"):
            if key not in self.config:
                raise ValueError("config missing: " + key)
        if not self.config["lines"]:
            raise ValueError("config must contain at least one line")
        for name, line in self.config["lines"].items():
            missing = REQUIRED_LINE_KEYS - set(line)
            if missing:
                raise ValueError("%s missing: %s" % (name, ", ".join(sorted(missing))))
            if float(line["default_watering_seconds"]) <= 0:
                raise ValueError(name + " default_watering_seconds must be positive")
            if float(line["watering_calibration"]) < 0:
                raise ValueError(name + " watering_calibration must not be negative")

    def _load_state(self):
        defaults = {
            "last_weather_update": None,
            "lines": {name: {"water_balance": float(line["water_balance"]),
                              "last_watered": None}
                      for name, line in self.lines.items()},
        }
        try:
            with self.state_path.open(encoding="utf-8") as handle:
                saved = json.load(handle)
        except FileNotFoundError:
            return defaults
        defaults["last_weather_update"] = saved.get("last_weather_update")
        for name in self.lines:
            if name in saved.get("lines", {}):
                defaults["lines"][name].update(saved["lines"][name])
        return defaults

    def save_state(self):
        atomic_json(self.state_path, self.state)

    def require_line(self, name):
        if name not in self.lines:
            raise ValueError("unknown line %r (choose %s)" % (name, ", ".join(self.lines)))

    def pid_path(self, name):
        return self.base_dir / ("watering-%s.pid" % name)

    def read_pid(self, name):
        try:
            pid = int(self.pid_path(name).read_text(encoding="ascii").strip())
            os.kill(pid, 0)
            return pid
        except (FileNotFoundError, ValueError, ProcessLookupError):
            try:
                self.pid_path(name).unlink()
            except FileNotFoundError:
                pass
            return None
        except PermissionError:
            return pid

    def active_lines(self):
        return {name: pid for name in self.lines if (pid := self.read_pid(name))}

    def _write_gpio(self, path, value):
        if self.dry_run:
            logging.info("dry-run GPIO write: %s <- %s", path, value)
            return
        path.write_text(str(value), encoding="ascii")

    def set_gpio(self, name, enabled):
        number = int(self.lines[name]["gpio"])
        gpio_dir = self.gpio_root / ("gpio%d" % number)
        if not gpio_dir.exists():
            self._write_gpio(self.gpio_root / "export", number)
            if not self.dry_run:
                for _ in range(50):
                    if gpio_dir.exists():
                        break
                    time.sleep(0.02)
                else:
                    raise RuntimeError("gpio%d did not appear after export" % number)
        self._write_gpio(gpio_dir / "direction", "out")
        on = "1" if self.active_high else "0"
        off = "0" if self.active_high else "1"
        self._write_gpio(gpio_dir / "value", on if enabled else off)

    def fetch_weather(self):
        params = {
            "latitude": self.config["latitude"],
            "longitude": self.config["longitude"],
            "hourly": "precipitation,et0_fao_evapotranspiration,precipitation_probability",
            "past_days": 7,
            "forecast_days": 2,
            "timezone": "UTC",
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "beaglebone-watering/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        hourly = payload.get("hourly", {})
        required = ("time", "precipitation", "et0_fao_evapotranspiration",
                    "precipitation_probability")
        if any(key not in hourly for key in required):
            raise RuntimeError("Open-Meteo response is missing hourly data")
        return hourly

    def weather_summary(self, hourly, update=True):
        current = now_utc()
        previous = (parse_time(self.state["last_weather_update"])
                    if self.state["last_weather_update"] else current - dt.timedelta(hours=24))
        history_rain = history_et0 = forecast_rain = 0.0
        forecast_probability = 0
        for stamp, rain, et0, probability in zip(
                hourly["time"], hourly["precipitation"],
                hourly["et0_fao_evapotranspiration"], hourly["precipitation_probability"]):
            instant = parse_time(stamp)
            rain_value = float(rain or 0)
            et0_value = float(et0 or 0)
            probability_value = int(probability or 0)
            if previous < instant <= current:
                history_rain += rain_value
                history_et0 += et0_value
            if current < instant <= current + dt.timedelta(hours=int(self.config.get("forecast_hours", 12))):
                forecast_rain += rain_value
                forecast_probability = max(forecast_probability, probability_value)
        if update:
            for name, line in self.lines.items():
                delta = history_rain * float(line["rain_efficiency"]) - history_et0 * float(line["et0_crop_factor"])
                self.state["lines"][name]["water_balance"] += delta
            self.state["last_weather_update"] = current.isoformat()
            self.save_state()
        return {"history_precipitation_mm": round(history_rain, 3),
                "history_et0_mm": round(history_et0, 3),
                "forecast_precipitation_mm": round(forecast_rain, 3),
                "forecast_probability_percent": forecast_probability}

    def rain_expected(self, summary):
        return (summary["forecast_precipitation_mm"] >= float(self.config.get("skip_rain_mm", 1.0))
                or summary["forecast_probability_percent"] >= int(self.config.get("skip_probability_percent", 60)))

    def start(self, name, seconds=None):
        self.require_line(name)
        if self.read_pid(name):
            raise RuntimeError(name + " is already watering")
        active = self.active_lines()
        if active and not self.config.get("allow_simultaneous", False):
            raise RuntimeError("another line is watering: " + ", ".join(active))
        duration = float(seconds if seconds is not None else self.lines[name]["default_watering_seconds"])
        if duration <= 0:
            raise ValueError("watering seconds must be positive")
        command = [sys.executable, str(Path(__file__).resolve()), "--config", str(self.config_path)]
        if self.dry_run:
            command.append("--dry-run")
        command.extend(["_run", name, str(duration)])
        with self.log_path.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.pid_path(name).write_text(str(process.pid) + "\n", encoding="ascii")
        time.sleep(0.05)
        if process.poll() is not None:
            raise RuntimeError("watering worker failed to start; inspect " + str(self.log_path))
        print("started %s for %.1f seconds (pid %d)" % (name, duration, process.pid))

    def run_worker(self, name, seconds):
        self.require_line(name)
        stopped = False

        def handle_stop(_signum, _frame):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, handle_stop)
        signal.signal(signal.SIGINT, handle_stop)
        started = time.monotonic()
        completed = 0.0
        try:
            self.set_gpio(name, True)
            logging.info("watering started: %s for %.1f seconds", name, seconds)
            while not stopped:
                completed = min(time.monotonic() - started, seconds)
                if completed >= seconds:
                    break
                time.sleep(min(0.25, seconds - completed))
        finally:
            self.set_gpio(name, False)
            completed = min(time.monotonic() - started, seconds)
            # Credit actual valve-on time, including a partially completed run.
            self.state = self._load_state()
            line_state = self.state["lines"][name]
            line_state["water_balance"] += completed * float(self.lines[name]["watering_calibration"])
            line_state["last_watered"] = now_utc().isoformat()
            self.save_state()
            try:
                if int(self.pid_path(name).read_text()) == os.getpid():
                    self.pid_path(name).unlink()
            except (FileNotFoundError, ValueError):
                pass
            logging.info("watering stopped: %s after %.1f seconds", name, completed)

    def stop(self, name):
        self.require_line(name)
        pid = self.read_pid(name)
        if not pid:
            self.set_gpio(name, False)
            print(name + " is off")
            return
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not self.read_pid(name):
                break
            time.sleep(0.1)
        self.set_gpio(name, False)
        print("stopped " + name)

    def off(self):
        for name in self.lines:
            self.stop(name)

    def status(self):
        active = self.active_lines()
        result = {
            "last_weather_update": self.state["last_weather_update"],
            "lines": {
                name: {
                    "label": line.get("label", name),
                    "gpio": line["gpio"],
                    "water_balance": round(self.state["lines"][name]["water_balance"], 3),
                    "watering_threshold": line["watering_threshold"],
                    "watering": name in active,
                    "pid": active.get(name),
                    "last_watered": self.state["lines"][name]["last_watered"],
                } for name, line in self.lines.items()
            },
        }
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))

    def auto(self):
        summary = self.weather_summary(self.fetch_weather(), update=True)
        print(json.dumps(summary, indent=2, sort_keys=True))
        if self.rain_expected(summary):
            print("rain expected within forecast window; watering skipped")
            return
        due = [name for name, line in self.lines.items()
               if self.state["lines"][name]["water_balance"] <= float(line["watering_threshold"])]
        if not due:
            print("no lines require watering")
            return
        if self.config.get("allow_simultaneous", False):
            for name in due:
                self.start(name)
        else:
            # The driest line goes first. Later cron invocations handle remaining lines.
            name = min(due, key=lambda item: self.state["lines"][item]["water_balance"])
            self.start(name)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="configuration JSON path")
    parser.add_argument("--dry-run", action="store_true", help="log GPIO writes instead of performing them")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="start a watering line in the background")
    start.add_argument("line")
    start.add_argument("seconds", nargs="?", type=float)
    stop = sub.add_parser("stop", help="stop one watering line")
    stop.add_argument("line")
    sub.add_parser("off", help="turn every line off")
    sub.add_parser("status", help="show line state")
    sub.add_parser("weather", help="update balances and show weather summary")
    sub.add_parser("auto", help="update weather and water a line when required")
    worker = sub.add_parser("_run", help=argparse.SUPPRESS)
    worker.add_argument("line")
    worker.add_argument("seconds", type=float)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        controller = Controller(args.config, args.dry_run)
        if args.command == "start":
            controller.start(args.line, args.seconds)
        elif args.command == "stop":
            controller.stop(args.line)
        elif args.command == "off":
            controller.off()
        elif args.command == "status":
            controller.status()
        elif args.command == "weather":
            print(json.dumps(controller.weather_summary(controller.fetch_weather(), update=True),
                             indent=2, sort_keys=True))
        elif args.command == "auto":
            controller.auto()
        elif args.command == "_run":
            controller.run_worker(args.line, args.seconds)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
