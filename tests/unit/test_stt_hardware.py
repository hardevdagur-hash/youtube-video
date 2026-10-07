"""Unit tests for Whisper STT hardware auto-detection."""

from services.stt.hardware import HardwareInfo, detect_hardware


def test_detect_hardware_returns_valid_info():
    """Hardware detection should return valid device, compute_type, and CPU count."""
    hw = detect_hardware()
    assert isinstance(hw, HardwareInfo)
    assert hw.device in ("cuda", "cpu")
    assert hw.compute_type in ("float16", "int8", "float32")
    assert hw.cpu_count > 0
    assert isinstance(hw.has_cuda, bool)

    if hw.has_cuda:
        assert hw.device == "cuda"
        assert hw.compute_type == "float16"
        assert "NVIDIA" in hw.device_name or "CUDA" in hw.device_name
