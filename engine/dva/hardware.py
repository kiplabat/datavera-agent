"""What this computer can run: CPU, memory, graphics cards (and how much memory each has), power state."""
import os
import platform
import re
import shutil
import subprocess

import psutil

INTEGRATED = re.compile(r"intel\(r\) (u?hd|iris)|intel.*graphics$|radeon\(tm\) graphics|radeon graphics|vega \d+ graphics"
                        r"|microsoft basic|virtual|vmware|parsec|meta virtual", re.I)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(cmd, timeout=8):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def cpu_name():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    if os.path.exists("/proc/cpuinfo"):
        for line in open("/proc/cpuinfo", encoding="utf-8", errors="ignore"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    if platform.system() == "Darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or platform.processor()
    return platform.processor() or platform.machine()


def _windows_gpus():
    gpus = []
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(64):
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = winreg.QueryValueEx(k, "DriverDesc")[0]
                        mem = 0
                        for value_name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                            try:
                                raw = winreg.QueryValueEx(k, value_name)[0]
                                mem = int.from_bytes(raw, "little") if isinstance(raw, bytes) else int(raw)
                                if mem:
                                    break
                            except OSError:
                                continue
                        gpus.append({"name": name, "vram_mb": int(mem / 2**20) if mem else 0, "source": "registry"})
                except OSError:
                    continue
    except Exception:
        pass
    return gpus


def _nvidia():
    if not shutil.which("nvidia-smi"):
        return []
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "vram_mb": int(float(parts[1])), "free_mb": int(float(parts[2])),
                         "driver": parts[3] if len(parts) > 3 else "", "source": "nvidia-smi"})
    return gpus


def _linux_gpus():
    out = _run(["lspci"])
    return [{"name": line.split(": ", 1)[-1], "vram_mb": 0, "source": "lspci"}
            for line in out.splitlines() if re.search(r"VGA|3D controller|Display", line)]


def vendor(name):
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "rtx" in n or "quadro" in n:
        return "nvidia"
    if "amd" in n or "radeon" in n:
        return "amd"
    if "intel" in n or "arc" in n:
        return "intel"
    if "apple" in n:
        return "apple"
    return "other"


def gpus():
    found = _nvidia()
    names = {g["name"] for g in found}
    others = _windows_gpus() if os.name == "nt" else (_linux_gpus() if platform.system() == "Linux" else [])
    for g in others:
        if g["name"] not in names and not any(g["name"] in n or n in g["name"] for n in names):
            found.append(g)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        found.append({"name": "Apple GPU (unified memory)", "vram_mb": 0, "source": "platform"})
    for g in found:
        g["vendor"] = vendor(g["name"])
        g["integrated"] = bool(INTEGRATED.search(g["name"])) or g["vendor"] == "apple"
    return [g for g in found if not re.search(r"microsoft basic|remote display|virtual display", g["name"], re.I)]


def power():
    try:
        battery = psutil.sensors_battery()
    except Exception:
        battery = None
    if battery is None:
        return {"on_ac": True, "battery_percent": None}
    return {"on_ac": bool(battery.power_plugged), "battery_percent": round(battery.percent)}


def snapshot():
    vm = psutil.virtual_memory()
    try:
        disk_free = psutil.disk_usage(os.path.expanduser("~")).free
    except Exception:
        disk_free = None
    cards = gpus()
    return {
        "os": f"{platform.system()} {platform.release()}", "machine": platform.machine(),
        "cpu": cpu_name(), "cores": psutil.cpu_count(logical=False) or 1, "threads": psutil.cpu_count() or 1,
        "ram_mb": int(vm.total / 2**20), "ram_free_mb": int(vm.available / 2**20),
        "disk_free_mb": int(disk_free / 2**20) if disk_free else None,
        "gpus": cards, "power": power(), "recommended_backend": recommend_backend(cards),
    }


def recommend_backend(cards):
    """CUDA for NVIDIA cards, Vulkan for other dedicated cards, otherwise CPU.

    Integrated graphics share system memory and were slower than the CPU on the reference laptop
    (Intel UHD: 2.67 vs 3.89 tokens/s), so they are offered but not recommended."""
    dedicated = [g for g in cards if not g["integrated"]]
    if any(g["vendor"] == "nvidia" and g.get("vram_mb", 0) >= 3500 for g in dedicated):
        return "cuda"
    if any(g.get("vram_mb", 0) >= 3500 for g in dedicated):
        return "vulkan"
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "metal"
    return "cpu"
