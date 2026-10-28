"""Занятая видеопамять GPU (Windows, счётчики PDH) — для ожидания её освобождения при смене модели."""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import POINTER, Structure, byref, c_longlong, c_uint32, c_ulong, c_void_p, c_wchar_p

PDH_MORE_DATA = 0x800007D2
PDH_FMT_LARGE = 0x00000400
COUNTER = r"\GPU Adapter Memory(*)\Dedicated Usage"

logger = logging.getLogger(__name__)


class _FmtValue(Structure):
    """Класс описывает структуру с полями CStatus, _pad и largeValue, используемую для хранения статуса и
    большого значения в формате GPU памяти.
    """
    _fields_ = [("CStatus", c_ulong), ("_pad", c_ulong), ("largeValue", c_longlong)]


class _Item(Structure):
    """Класс описывает структуру с полями szName и FmtValue, где szName хранит имя элемента, а FmtValue
    содержит связанные данные в формате _FmtValue.
    """
    _fields_ = [("szName", c_wchar_p), ("FmtValue", _FmtValue)]


def dedicated_vram_used() -> int | None:
    """Сколько выделенной видеопамяти занято сейчас (байт, сумма по адаптерам); None — счётчик недоступен."""
    if sys.platform != "win32":
        return None
    try:
        pdh = ctypes.WinDLL("pdh")
        for name in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData",
                     "PdhGetFormattedCounterArrayW", "PdhCloseQuery"):
            getattr(pdh, name).restype = c_uint32  # коды PDH — беззнаковые (0x800007D2)
    except Exception:
        return None
    query, counter = c_void_p(), c_void_p()
    try:
        if pdh.PdhOpenQueryW(None, None, byref(query)) != 0:
            return None
        if pdh.PdhAddEnglishCounterW(query, COUNTER, None, byref(counter)) != 0:
            return None
        if pdh.PdhCollectQueryData(query) != 0:
            return None
        size, count = c_ulong(0), c_ulong(0)
        if pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_LARGE, byref(size), byref(count), None) != PDH_MORE_DATA:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_LARGE, byref(size), byref(count), buf) != 0:
            return None
        items = ctypes.cast(buf, POINTER(_Item))
        return sum(items[i].FmtValue.largeValue for i in range(count.value))
    except Exception:
        return None
    finally:
        if query.value:
            pdh.PdhCloseQuery(query)


def wait_vram_released(idle_mb: float, timeout: float = 30.0, poll: float = 1.0) -> None:
    """Ожидание освобождения видеопамяти после остановки модели.

    Драйвер Arc освобождает VRAM убитого процесса не сразу: следующая модель, запущенная
    вплотную, падала с ErrorOutOfDeviceMemory (llama-server) или зависала на 2-м шаге (OpenVINO).
    """
    start = time.monotonic()
    prev = None
    stable_count = 0
    used_mb = 0.0
    while True:
        used_bytes = dedicated_vram_used()
        if used_bytes is None:
            if prev is None:
                time.sleep(3)  # счётчик недоступен — короткая фиксированная пауза
                return
            used_bytes = prev
        used_mb = used_bytes / 2**20
        if used_mb <= idle_mb:
            break
        if prev is not None and abs(used_bytes - prev) < 128 * 2**20:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
        prev = used_bytes
        if time.monotonic() - start > timeout:
            logger.warning("Видеопамять не освободилась за %.0f с: занято %d МБ", timeout, used_mb)
            break
        time.sleep(poll)
    logger.info("Видеопамять после остановки модели: %d МБ (ожидание %.1f с)",
                used_mb, time.monotonic() - start)
