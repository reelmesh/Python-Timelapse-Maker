import psutil
import subprocess
import os
from pathlib import Path  # Might not be needed here if functions take simple paths


# --- CPU and Memory ---
def get_cpu_usage() -> float | None:
    """Returns overall CPU utilization as a percentage, or None on error."""
    try:
        return psutil.cpu_percent(interval=None)  # Non-blocking after first call
    except Exception as e:
        print(f"MonitoringEngine Error: Could not get CPU usage: {e}")
        return None


def get_memory_usage() -> float | None:
    """Returns overall memory utilization as a percentage, or None on error."""
    try:
        mem_info = psutil.virtual_memory()
        return mem_info.percent
    except Exception as e:
        print(f"MonitoringEngine Error: Could not get Memory usage: {e}")
        return None


# --- GPU Detection and Usage ---
# This section will be the most platform-dependent

_gpu_type_cache = None  # Simple cache for detected GPU type


def detect_gpu_type() -> str | None:
    """
    Detects the primary GPU type that we have monitoring support for.
    Returns "nvidia", "amd", "intel", or None.
    Caches the result after the first call.
    """
    global _gpu_type_cache
    if _gpu_type_cache is not None:  # Return cached result if already detected
        return _gpu_type_cache if _gpu_type_cache != "unknown" else None

    # Check for NVIDIA
    try:
        # Use creationflags to hide console window on Windows for subprocess
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        result = subprocess.run(['nvidia-smi', '-L'], capture_output=True, text=True, check=False,
                                creationflags=creation_flags, timeout=5)
        if result.returncode == 0 and "GPU 0:" in result.stdout:
            _gpu_type_cache = "nvidia"
            return "nvidia"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # nvidia-smi not found or timed out
    except Exception:  # Catch any other subprocess errors
        pass

    # Placeholder for AMD check (e.g., rocm-smi for Linux)
    # try:
    #     result = subprocess.run(['rocm-smi', '--showhw'], ...) # Example command
    #     if result.returncode == 0:
    #         _gpu_type_cache = "amd"
    #         return "amd"
    # except: pass

    # Placeholder for Intel check (e.g., intel_gpu_top on Linux, harder on Windows)

    _gpu_type_cache = "unknown"  # Cache that we tried and found nothing specific
    return None


def get_nvidia_gpu_utilization() -> float | None:
    """Gets NVIDIA GPU utilization percentage using nvidia-smi."""
    try:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, check=True, creationflags=creation_flags, timeout=5
        )
        return float(result.stdout.strip().replace('%', ''))
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        # print(f"MonitoringEngine Error: Could not get NVIDIA GPU util: {e}") # Can be noisy
        return None
    except Exception as e:
        print(f"MonitoringEngine Error (NVIDIA): Unexpected error getting GPU util: {e}")
        return None


def get_gpu_usage() -> tuple[str, float] | tuple[None, None]:
    """
    Attempts to get GPU usage.
    Returns a tuple (gpu_type_string, usage_percentage) or (None, None).
    """
    gpu_type = detect_gpu_type()
    if gpu_type == "nvidia":
        usage = get_nvidia_gpu_utilization()
        if usage is not None:
            return "NVIDIA", usage
    # elif gpu_type == "amd":
    #     # usage = get_amd_gpu_utilization() # Implement this
    #     # if usage is not None: return "AMD", usage
    #     pass
    # elif gpu_type == "intel":
    #     # usage = get_intel_gpu_utilization() # Implement this
    #     # if usage is not None: return "Intel", usage
    #     pass
    return None, None


# Example of how the GUI might use this engine:
if __name__ == "__main__":
    print("Monitoring Engine Test:")
    cpu = get_cpu_usage()
    if cpu is not None: print(f"Current CPU Usage: {cpu:.2f}%")

    mem = get_memory_usage()
    if mem is not None: print(f"Current Memory Usage: {mem:.2f}%")

    gpu_vendor, gpu_util = get_gpu_usage()
    if gpu_vendor and gpu_util is not None:
        print(f"Current {gpu_vendor} GPU Usage: {gpu_util:.2f}%")
    elif gpu_vendor:  # Found type but couldn't get usage
        print(f"{gpu_vendor} GPU detected, but could not get current utilization.")
    else:
        print("No supported GPU detected or unable to fetch GPU stats.")