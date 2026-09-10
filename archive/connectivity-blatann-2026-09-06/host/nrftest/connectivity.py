from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any


class ConnectivityError(RuntimeError):
    """Raised when the Nordic Connectivity session cannot be opened."""


@dataclass(frozen=True)
class ConnectivityMetadata:
    port: str
    baud: int
    driver_package: str
    driver_version: str | None
    blatann_version: str | None
    device_address: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "baud": self.baud,
            "driver_package": self.driver_package,
            "driver_version": self.driver_version,
            "blatann_version": self.blatann_version,
            "device_address": self.device_address,
        }


class ConnectivitySession:
    """Own one Blatann/pc-ble-driver Peripheral session."""

    def __init__(self, port: str, baud: int = 1_000_000) -> None:
        self.port = port
        self.baud = baud
        self.device: Any | None = None

    @property
    def opened(self) -> bool:
        return self.device is not None

    def open(self) -> None:
        if self.device is not None:
            return
        try:
            from blatann import BleDevice
        except ImportError as error:  # pragma: no cover - environment failure
            raise ConnectivityError(
                "Blatann is not installed; run this command through the locked Pixi environment"
            ) from error

        device = BleDevice(comport=self.port, baud=self.baud)
        try:
            device.configure(
                max_connected_peripherals=1,
                max_connected_clients=1,
                attribute_table_size=1408,
                att_mtu_max_size=247,
            )
            device.open()
        except Exception as error:
            try:
                device.close()
            except Exception:
                pass
            raise ConnectivityError(f"unable to open Connectivity device on {self.port}: {error}") from error
        self.device = device

    def close(self) -> None:
        device, self.device = self.device, None
        if device is not None:
            try:
                device.close()
            except Exception as error:
                raise ConnectivityError(f"unable to close Connectivity device on {self.port}: {error}") from error

    def metadata(self) -> ConnectivityMetadata:
        address: str | None = None
        if self.device is not None:
            raw_address = getattr(self.device, "address", None)
            address = str(raw_address) if raw_address is not None else None
        return ConnectivityMetadata(
            port=self.port,
            baud=self.baud,
            driver_package="pc-ble-driver-py",
            driver_version=_package_version("pc-ble-driver-py"),
            blatann_version=_package_version("blatann"),
            device_address=address,
        )


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
