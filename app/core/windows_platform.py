import sys


def disable_hanging_platform_wmi_probe() -> None:
    if sys.platform != "win32":
        return

    import platform

    if getattr(platform, "_creator_compass_wmi_probe_disabled", False):
        return

    def _raise_os_error(*args: object, **kwargs: object) -> object:
        raise OSError("Windows WMI platform probe disabled")

    if hasattr(platform, "_wmi_query"):
        platform._wmi_query = _raise_os_error
    platform._creator_compass_wmi_probe_disabled = True
