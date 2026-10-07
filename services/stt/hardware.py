"""Hardware detection service for Whisper and CTranslate2 acceleration."""

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


_DLL_DIRECTORIES: list[Any] = []


def setup_cuda_dll_paths() -> None:
    """Register nvidia pip package bin directories into DLL search path on Windows."""
    if sys.platform != "win32":
        return
    import ctypes
    import site
    try:
        search_paths = site.getsitepackages()
        if hasattr(site, "getusersitepackages"):
            search_paths.append(site.getusersitepackages())
    except Exception:
        search_paths = []

    for sp in search_paths:
        nv_dir = Path(sp) / "nvidia"
        if nv_dir.is_dir():
            for sub in nv_dir.iterdir():
                bin_dir = sub / "bin"
                if bin_dir.is_dir():
                    bin_str = str(bin_dir)
                    if bin_str not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = bin_str + os.pathsep + os.environ.get("PATH", "")
                    try:
                        _DLL_DIRECTORIES.append(os.add_dll_directory(bin_str))
                    except Exception:
                        pass
                    # Pre-load known libraries
                    for dll_name in ("cublas64_12.dll", "cudnn64_9.dll"):
                        dll_file = bin_dir / dll_name
                        if dll_file.is_file():
                            try:
                                ctypes.CDLL(str(dll_file))
                            except Exception:
                                pass


# Register CUDA DLLs at import time
setup_cuda_dll_paths()


class HardwareInfo(NamedTuple):
    device: str
    compute_type: str
    has_cuda: bool
    device_name: str
    vram_mb: int | None
    cpu_count: int


def detect_hardware() -> HardwareInfo:
    """Detect available hardware and choose optimal Whisper device/compute_type."""
    setup_cuda_dll_paths()
    cpu_count = os.cpu_count() or 4
    has_cuda = False
    device = "cpu"
    compute_type = "int8"
    device_name = f"{platform.processor()} ({cpu_count} cores)"
    vram_mb = None

    try:
        import ctranslate2
        cuda_count = ctranslate2.get_cuda_device_count()
        if cuda_count > 0:
            has_cuda = True
            device = "cuda"
            compute_type = "float16"
            device_name = "NVIDIA CUDA Device"
    except Exception as exc:
        logger.debug("ctranslate2 CUDA check failed: %s", exc)

    if has_cuda:
        try:
            import torch
            if torch.cuda.is_available():
                device_name = torch.cuda.get_device_name(0)
                props = torch.cuda.get_device_properties(0)
                vram_mb = int(props.total_memory / (1024 * 1024))
        except Exception:
            pass

    logger.info(
        "Hardware detected: device=%s, compute=%s, name='%s', vram=%sMB, cpus=%d",
        device, compute_type, device_name, vram_mb or "N/A", cpu_count,
    )

    return HardwareInfo(
        device=device,
        compute_type=compute_type,
        has_cuda=has_cuda,
        device_name=device_name,
        vram_mb=vram_mb,
        cpu_count=cpu_count,
    )
