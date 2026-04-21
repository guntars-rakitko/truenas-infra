"""Minimal Intel AMT WS-MAN client for the `amtctl` sidecar.

Talks to Intel AMT's Management Engine over plain HTTP port 16992 using
HTTP Digest auth + SOAP (WS-MAN). Covers just what our dashboard needs:

  Status queries     — power state, system identity, BIOS, CPU, memory,
                       baseboard, network, time
  Power actions      — on / off (graceful + hard) / reset, with
                       optional one-time boot source (PXE, BIOS setup,
                       default OS)

AMT-side quirks worked around here:
  - Kaby Lake-era ME (AMT 11.x) wants `PT60S` or longer OperationTimeout
    on many Enumerate calls — the firmware is slow.
  - Every call goes through a three-step handshake: Enumerate → get an
    EnumerationContext token → Pull with that token → read Items.
  - The SOAP "role" is anonymous; all auth is via HTTP Digest.

Nothing TrueNAS-specific here; this file is the reusable primitive.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

_WSMAN = "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"
_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_WSEN = "http://schemas.xmlsoap.org/ws/2004/09/enumeration"
_WST = "http://schemas.xmlsoap.org/ws/2004/09/transfer"
_ANON = f"{_WSA}/role/anonymous"


# ─── Power state mapping (DMTF CIM spec values) ──────────────────────────────

POWER_STATE_NAMES = {
    1: "Other",
    2: "On",                    # S0 / running
    3: "Sleep Light",           # S1
    4: "Sleep Deep",            # S3 (suspend to RAM)
    5: "Power Cycle Soft-off",
    6: "Off - Hard",            # S5 hard
    7: "Hibernate",             # S4 (suspend to disk)
    8: "Off - Soft",            # S5 soft-off (ACPI shutdown)
    9: "Power Cycle Hard-off",
    10: "Master Bus Reset",     # reset
    11: "Diagnostic Interrupt (NMI)",
    12: "Off - Soft Graceful",
    13: "Off - Hard Graceful",
    14: "Master Bus Reset Graceful",
    15: "Power Cycle Soft Graceful",
    16: "Power Cycle Hard Graceful",
}

# Action codes we expose as distinct operations. Maps to PowerState enum
# used by CIM_PowerManagementService.RequestPowerStateChange.
ACTION_POWER_STATE = {
    "on": 2,
    "off_graceful": 12,
    "off_hard": 8,
    "reset": 10,
    "power_cycle": 5,
}

# Boot source InstanceIDs exposed by Intel AMT. Used with
# CIM_BootConfigSetting.ChangeBootOrder for one-time boot overrides.
BOOT_SOURCES = {
    "pxe": "Intel(r) AMT: Force PXE Boot",
    "cd": "Intel(r) AMT: Force CD/DVD Boot",
    "disk": "Intel(r) AMT: Force Hard-drive Boot",
    "diagnostic": "Intel(r) AMT: Force Diagnostic Boot",
}

# DMTF CIM_Processor.Family → human label. AMT doesn't expose the CPU
# brand string (nothing like "Intel Core i7-7700T @ 2.90 GHz") — just a
# numeric family code. This is a small subset to turn 198 into something
# more readable; unknowns fall through to "Family N" in the caller.
# Full spec: DMTF DSP1022 (Processor Profile).
CPU_FAMILY_NAMES = {
    198: "Intel Core i7",
    199: "Intel Core i5",
    200: "Intel Core i3",
    201: "Intel Core i9",
    207: "Intel Atom",
    225: "Intel Xeon",
    261: "Intel Core 2 Duo",
    262: "Intel Core 2 Solo",
    1: "Other",
    2: "Unknown",
}


@dataclass
class AMTError(Exception):
    """Raised when AMT returns a SOAP fault or the HTTP call fails."""

    host: str
    kind: str        # "timeout" | "auth" | "soap_fault" | "http" | "network"
    detail: str

    def __str__(self) -> str:
        return f"[{self.host}] {self.kind}: {self.detail}"


# ─── Client ──────────────────────────────────────────────────────────────────


class AMTClient:
    """One client per node. Holds the host + creds + an async HTTP client.

    Usage:
        async with AMTClient("10.10.5.11", "admin", "pw") as c:
            status = await c.full_status()
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 16992,
        timeout: float = 15.0,
        wsman_timeout: str = "PT60S",
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.url = f"http://{host}:{port}/wsman"
        self._timeout = timeout
        self._wsman_timeout = wsman_timeout
        # DigestAuth is stateless in httpx; re-created per-request is fine,
        # but making it an instance attr makes logging + swap-auth trivial.
        self._auth = httpx.DigestAuth(username, password)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "AMTClient":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"Content-Type": "application/soap+xml;charset=UTF-8"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Internal: build + post envelope ──────────────────────────────────

    def _envelope(self, action: str, uri: str, body: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}"
  xmlns:wsen="{_WSEN}" xmlns:wst="{_WST}">
  <s:Header>
    <wsa:Action s:mustUnderstand="true">{action}</wsa:Action>
    <wsa:To s:mustUnderstand="true">{self.url}</wsa:To>
    <wsman:ResourceURI s:mustUnderstand="true">{uri}</wsman:ResourceURI>
    <wsa:MessageID s:mustUnderstand="true">uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:ReplyTo><wsa:Address>{_ANON}</wsa:Address></wsa:ReplyTo>
    <wsman:OperationTimeout>{self._wsman_timeout}</wsman:OperationTimeout>
  </s:Header>
  <s:Body>{body}</s:Body>
</s:Envelope>"""

    async def _post(self, action: str, uri: str, body: str) -> str:
        assert self._client is not None, "use `async with AMTClient(...)`"
        env = self._envelope(action, uri, body)
        try:
            r = await self._client.post(self.url, data=env, auth=self._auth)
        except httpx.ConnectTimeout:
            raise AMTError(self.host, "timeout", "HTTP connect timeout") from None
        except httpx.TimeoutException as e:
            raise AMTError(self.host, "timeout", f"HTTP read timeout: {e}") from None
        except httpx.RequestError as e:
            raise AMTError(self.host, "network", f"{type(e).__name__}: {e}") from None

        if r.status_code == 401:
            raise AMTError(self.host, "auth", "HTTP 401 — bad username or password")
        if r.status_code >= 500:
            if "TimedOut" in r.text:
                raise AMTError(self.host, "timeout", "ME firmware operation timeout")
            # Look for fault reason
            m = re.search(r"<a:Text[^>]*>([^<]+)</a:Text>", r.text)
            raise AMTError(self.host, "soap_fault", m.group(1) if m else f"HTTP {r.status_code}")
        if r.status_code != 200:
            raise AMTError(self.host, "http", f"HTTP {r.status_code}: {r.text[:200]}")
        return r.text

    async def _enum_pull(self, uri: str) -> list[dict[str, str]]:
        """Enumerate a CIM class + Pull the results. Returns a list of
        dicts (one per instance) where each dict has key=element tag,
        value=text content. Multi-valued fields collapse to last — callers
        that care (AvailableRequestedPowerStates) can re-extract.
        """
        r = await self._post(f"{_WSEN}/Enumerate", uri, "<wsen:Enumerate/>")
        m = re.search(r"<[^>]*EnumerationContext[^>]*>([^<]+)</", r)
        if not m:
            return []
        ctx = m.group(1)
        body = (f"<wsen:Pull><wsen:EnumerationContext>{ctx}</wsen:EnumerationContext>"
                f"<wsen:MaxElements>32</wsen:MaxElements></wsen:Pull>")
        r2 = await self._post(f"{_WSEN}/Pull", uri, body)
        # Items block contains zero-or-more instance envelopes
        items_m = re.search(r"<[a-z0-9]+:Items>(.*?)</[a-z0-9]+:Items>", r2, re.DOTALL)
        if not items_m:
            return []
        items_xml = items_m.group(1)
        # Each instance is wrapped in <ns:ClassName>...</ns:ClassName>. Split on
        # the outer element boundary: look for <ns:CapName>...</ns:CapName>
        # patterns at depth 0. Simpler: find all <prefix:CAP>...</prefix:CAP>
        # where CAP starts with uppercase.
        instances = re.findall(
            r"<[a-z0-9]+:([A-Z][A-Za-z0-9_]+)[^>/]*>(.*?)</[a-z0-9]+:\1>",
            items_xml, re.DOTALL,
        )
        out: list[dict[str, str]] = []
        for _cls, content in instances:
            fields: dict[str, str] = {}
            for k, v in re.findall(r"<[a-z0-9]+:([A-Z][A-Za-z0-9_]+)>([^<]*)</", content):
                fields[k] = v
            if fields:
                out.append(fields)
        return out

    async def _enum_raw(self, uri: str) -> str:
        """Like _enum_pull but returns raw XML — for AvailableRequestedPowerStates
        and other fields that need multi-value extraction."""
        r = await self._post(f"{_WSEN}/Enumerate", uri, "<wsen:Enumerate/>")
        m = re.search(r"<[^>]*EnumerationContext[^>]*>([^<]+)</", r)
        if not m:
            return ""
        ctx = m.group(1)
        body = (f"<wsen:Pull><wsen:EnumerationContext>{ctx}</wsen:EnumerationContext>"
                f"<wsen:MaxElements>32</wsen:MaxElements></wsen:Pull>")
        return await self._post(f"{_WSEN}/Pull", uri, body)

    # ── Status queries ───────────────────────────────────────────────────

    async def get_power_state(self) -> dict[str, Any]:
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_AssociatedPowerManagementService"
        raw = await self._enum_raw(uri)
        # Extract all AvailableRequestedPowerStates occurrences (multi-valued)
        avail = [int(x) for x in re.findall(
            r"<[a-z0-9]+:AvailableRequestedPowerStates>(\d+)</", raw)]
        # Current state
        m = re.search(r"<[a-z0-9]+:PowerState>(\d+)</", raw)
        state = int(m.group(1)) if m else 0
        return {
            "state": state,
            "state_name": POWER_STATE_NAMES.get(state, f"Unknown ({state})"),
            "available": avail,
            "available_names": [POWER_STATE_NAMES.get(s, f"?({s})") for s in avail],
        }

    async def get_system(self) -> dict[str, Any]:
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ComputerSystem"
        items = await self._enum_pull(uri)
        # Two instances returned: Managed System (target box) + Intel AMT
        # subsystem (the ME itself). Caller wants the Managed System.
        managed = next(
            (i for i in items if i.get("ElementName") == "Managed System"),
            items[0] if items else {},
        )
        return {
            "name": managed.get("Name", ""),
            "element_name": managed.get("ElementName", ""),
            "enabled_state": int(managed.get("EnabledState", 0) or 0),
            "requested_state": int(managed.get("RequestedState", 0) or 0),
        }

    async def get_general_settings(self) -> dict[str, Any]:
        uri = "http://intel.com/wbem/wscim/1/amt-schema/1/AMT_GeneralSettings"
        items = await self._enum_pull(uri)
        if not items:
            return {}
        g = items[0]
        return {
            "hostname": g.get("HostName", ""),
            "domain": g.get("DomainName", ""),
            # HostOSFQDN is what the *running OS* reported on last boot.
            # Often differs from the AMT-configured HostName — useful to
            # notice drift (e.g. OS reinstalled with a different hostname).
            "host_os_fqdn": g.get("HostOSFQDN", ""),
        }

    async def get_time(self) -> dict[str, Any]:
        """AMT time via a Get call on AMT_TimeSynchronizationService +
        the GetLowAccuracyTimeSynch method. For display we just do the
        service state — exact clock is less important than 'is the service up'."""
        uri = "http://intel.com/wbem/wscim/1/amt-schema/1/AMT_TimeSynchronizationService"
        items = await self._enum_pull(uri)
        svc = items[0] if items else {}
        return {
            "sync_service": svc.get("ElementName", ""),
            "enabled": svc.get("EnabledState") == "5",  # 5 = Not Applicable / running
        }

    async def get_bios(self) -> dict[str, Any]:
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_BIOSElement"
        items = await self._enum_pull(uri)
        b = items[0] if items else {}
        return {
            "vendor": b.get("Manufacturer", ""),
            "version": b.get("SoftwareElementID", ""),
            "release_date": b.get("Datetime", ""),
        }

    async def get_baseboard(self) -> dict[str, Any]:
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_Card"
        items = await self._enum_pull(uri)
        c = items[0] if items else {}
        return {
            "manufacturer": c.get("Manufacturer", ""),
            "product": c.get("Model", ""),
            "serial": c.get("SerialNumber", ""),
        }

    async def get_processor(self) -> dict[str, Any]:
        """CPU info. Note: AMT's CurrentClockSpeed is the BIOS-reported
        BASE clock — not live frequency. MaxClockSpeed is a spec-table
        ceiling unrelated to the real turbo-boost max. Brand string
        (e.g. "Intel Core i7-7700T") is NOT exposed; we map family +
        speed to a readable label via CPU_FAMILY_NAMES."""
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_Processor"
        items = await self._enum_pull(uri)
        p = items[0] if items else {}
        family = int(p.get("Family", 0) or 0)
        speed = int(p.get("CurrentClockSpeed", 0) or 0)
        family_name = CPU_FAMILY_NAMES.get(family, f"Family {family}")
        speed_ghz = speed / 1000.0 if speed else 0.0
        label = f"{family_name} @ {speed_ghz:.1f} GHz" if speed else family_name
        return {
            "element_name": p.get("ElementName", ""),
            "speed_mhz": speed,
            "family": family,
            "family_name": family_name,
            "stepping": p.get("Stepping", ""),
            "label": label,  # e.g. "Intel Core i7 @ 2.9 GHz"
        }

    async def get_memory(self) -> dict[str, Any]:
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_PhysicalMemory"
        items = await self._enum_pull(uri)
        total_bytes = sum(int(m.get("Capacity", 0) or 0) for m in items)
        modules = [
            {
                "bank": m.get("BankLabel", ""),
                "manufacturer": m.get("Manufacturer", ""),
                "part": m.get("PartNumber", "").strip(),
                "capacity_bytes": int(m.get("Capacity", 0) or 0),
            }
            for m in items
        ]
        return {"total_bytes": total_bytes, "dimm_count": len(items), "modules": modules}

    async def get_network(self) -> dict[str, Any]:
        """AMT network settings — IP, MAC, link state, subnet, DNS.
        DNS values may come from DHCP lease (then only populated when
        DHCP is enabled and has leased); we surface what AMT reports."""
        uri = "http://intel.com/wbem/wscim/1/amt-schema/1/AMT_EthernetPortSettings"
        raw = await self._enum_raw(uri)

        def _extract(tag: str) -> str:
            m = re.search(rf"<[a-z0-9]+:{tag}>([^<]+)</", raw)
            return m.group(1) if m else ""

        def _bool(tag: str) -> bool:
            v = _extract(tag)
            return v.lower() == "true" if v else False

        return {
            "mac": _extract("MACAddress"),
            "ip": _extract("IPAddress"),
            "gateway": _extract("DefaultGateway"),
            "subnet_mask": _extract("SubnetMask"),
            "primary_dns": _extract("PrimaryDNS"),
            "secondary_dns": _extract("SecondaryDNS"),
            "dhcp_enabled": _bool("DHCPEnabled"),
            "link_up": _bool("LinkIsUp"),
        }

    async def get_storage(self) -> list[dict[str, Any]]:
        """Storage drives exposed via AMT. Intel AMT doesn't have a
        dedicated storage schema; it typically surfaces drives under
        CIM_PhysicalMedia (the physical unit) and CIM_MediaAccessDevice
        (the logical access path). We try both and merge by serial
        number where possible. May return empty on boards where the
        storage controller doesn't report through AMT."""
        drives: list[dict[str, Any]] = []

        # CIM_PhysicalMedia gives size, manufacturer, part, serial
        try:
            items = await self._enum_pull(
                "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_PhysicalMedia"
            )
            for m in items:
                capacity = int(m.get("Capacity", 0) or 0)
                if capacity == 0:
                    # Skip non-storage media entries (some AMT boards list
                    # other physical packages here too — e.g. BIOS flash)
                    continue
                drives.append({
                    "model": m.get("Manufacturer", "") + " " + m.get("Model", "").strip(),
                    "serial": m.get("SerialNumber", ""),
                    "size_bytes": capacity,
                    "tag": m.get("Tag", ""),
                    "source": "CIM_PhysicalMedia",
                })
        except AMTError:
            pass

        # Fallback: CIM_DiskDrive (fewer fields but universal on AMT 11+)
        if not drives:
            try:
                items = await self._enum_pull(
                    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_DiskDrive"
                )
                for d in items:
                    drives.append({
                        "model": d.get("ElementName", ""),
                        "serial": d.get("DeviceID", ""),
                        "size_bytes": 0,  # CIM_DiskDrive doesn't report size
                        "tag": "",
                        "source": "CIM_DiskDrive",
                    })
            except AMTError:
                pass

        return drives

    async def get_amt_time(self) -> dict[str, Any]:
        """Current clock in the Intel ME firmware. AMT exposes this via
        method invocation (not a settable class field) —
        AMT_TimeSynchronizationService.GetLowAccuracyTimeSynch returns
        Ta0 = seconds since Unix epoch in UTC."""
        uri = "http://intel.com/wbem/wscim/1/amt-schema/1/AMT_TimeSynchronizationService"
        action_url = f"{uri}/GetLowAccuracyTimeSynch"
        mid = f"uuid:{uuid.uuid4()}"
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}">
  <s:Header>
    <wsa:Action s:mustUnderstand="true">{action_url}</wsa:Action>
    <wsa:To s:mustUnderstand="true">{self.url}</wsa:To>
    <wsman:ResourceURI s:mustUnderstand="true">{uri}</wsman:ResourceURI>
    <wsa:MessageID s:mustUnderstand="true">{mid}</wsa:MessageID>
    <wsa:ReplyTo><wsa:Address>{_ANON}</wsa:Address></wsa:ReplyTo>
    <wsman:SelectorSet>
      <wsman:Selector Name="SystemCreationClassName">CIM_ComputerSystem</wsman:Selector>
      <wsman:Selector Name="SystemName">Intel(r) AMT</wsman:Selector>
      <wsman:Selector Name="CreationClassName">AMT_TimeSynchronizationService</wsman:Selector>
      <wsman:Selector Name="Name">Intel(r) AMT Time Synchronization Service</wsman:Selector>
    </wsman:SelectorSet>
    <wsman:OperationTimeout>{self._wsman_timeout}</wsman:OperationTimeout>
  </s:Header>
  <s:Body><p:GetLowAccuracyTimeSynch_INPUT xmlns:p="{uri}"/></s:Body>
</s:Envelope>"""
        assert self._client is not None
        r = await self._client.post(self.url, data=envelope, auth=self._auth)
        if r.status_code != 200:
            return {}
        # Response has <Ta0> with epoch seconds
        m = re.search(r"<[a-z0-9]+:Ta0>(\d+)</", r.text)
        if not m:
            return {}
        epoch = int(m.group(1))
        return {"epoch": epoch}

    async def full_status(self) -> dict[str, Any]:
        """Pull everything the dashboard needs in a single call. Returns
        partial data if some sub-queries fail — widgets can render what's
        available. Top-level `reachable` is True iff we got past auth."""
        result: dict[str, Any] = {"host": self.host, "reachable": False, "errors": []}

        # Power state is the single most important signal — do it first
        # so an offline node returns fast with just {reachable: False}.
        try:
            result["power"] = await self.get_power_state()
            result["reachable"] = True
        except AMTError as e:
            result["errors"].append(f"power: {e.kind}")
            return result  # node unreachable, skip the rest

        # Everything below we attempt but don't let one failure kill the pull.
        for field, fn in [
            ("system", self.get_system),
            ("network", self.get_network),
            ("settings", self.get_general_settings),
            ("time", self.get_time),
            ("amt_time", self.get_amt_time),
            ("bios", self.get_bios),
            ("baseboard", self.get_baseboard),
            ("processor", self.get_processor),
            ("memory", self.get_memory),
            ("storage", self.get_storage),
        ]:
            try:
                result[field] = await fn()
            except AMTError as e:
                result["errors"].append(f"{field}: {e.kind}")
                result[field] = None
        return result

    # ── Actions ──────────────────────────────────────────────────────────

    async def power_action(self, action: str, boot: str | None = None) -> dict[str, Any]:
        """High-level power action. Valid actions: on / off_graceful /
        off_hard / reset / power_cycle. Optional `boot`: pxe / bios /
        disk / None (default).

        Flow for action='on' + boot='pxe':
          1. ChangeBootOrder → set one-time boot to PXE
          2. RequestPowerStateChange(2) → power on

        For boot='bios' we instead Put AMT_BootSettingData with BIOSSetup=true.
        For no boot override, skip step 1 and just request the power state.
        """
        if action not in ACTION_POWER_STATE:
            raise ValueError(f"unknown action: {action}")

        # Step 1: set boot override
        if boot == "bios":
            await self._set_boot_setting(biossetup=True)
        elif boot in ("pxe", "cd", "disk", "diagnostic"):
            await self._set_boot_setting(biossetup=False)
            await self._change_boot_order(BOOT_SOURCES[boot])
        elif boot in (None, "default"):
            # Still reset boot settings to a known-safe state
            await self._set_boot_setting(biossetup=False)

        # Step 2: request power state
        state = ACTION_POWER_STATE[action]
        body = f"""<r:RequestPowerStateChange_INPUT xmlns:r="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_PowerManagementService">
  <r:PowerState>{state}</r:PowerState>
  <r:ManagedElement>
    <wsa:Address>{_ANON}</wsa:Address>
    <wsa:ReferenceParameters>
      <wsman:ResourceURI>http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ComputerSystem</wsman:ResourceURI>
      <wsman:SelectorSet>
        <wsman:Selector Name="CreationClassName">CIM_ComputerSystem</wsman:Selector>
        <wsman:Selector Name="Name">ManagedSystem</wsman:Selector>
      </wsman:SelectorSet>
    </wsa:ReferenceParameters>
  </r:ManagedElement>
</r:RequestPowerStateChange_INPUT>"""
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_PowerManagementService"
        action_url = f"{uri}/RequestPowerStateChange"
        # The RequestPowerStateChange method needs selectors on the PM service
        # itself. We add them to the To/ResourceURI via extra header selectors.
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}">
  <s:Header>
    <wsa:Action s:mustUnderstand="true">{action_url}</wsa:Action>
    <wsa:To s:mustUnderstand="true">{self.url}</wsa:To>
    <wsman:ResourceURI s:mustUnderstand="true">{uri}</wsman:ResourceURI>
    <wsa:MessageID s:mustUnderstand="true">uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:ReplyTo><wsa:Address>{_ANON}</wsa:Address></wsa:ReplyTo>
    <wsman:SelectorSet>
      <wsman:Selector Name="CreationClassName">CIM_PowerManagementService</wsman:Selector>
      <wsman:Selector Name="Name">Intel(r) AMT Power Management Service</wsman:Selector>
      <wsman:Selector Name="SystemCreationClassName">CIM_ComputerSystem</wsman:Selector>
      <wsman:Selector Name="SystemName">Intel(r) AMT</wsman:Selector>
    </wsman:SelectorSet>
    <wsman:OperationTimeout>{self._wsman_timeout}</wsman:OperationTimeout>
  </s:Header>
  <s:Body>{body}</s:Body>
</s:Envelope>"""
        assert self._client is not None
        r = await self._client.post(self.url, data=envelope, auth=self._auth)
        if r.status_code != 200:
            raise AMTError(self.host, "http", f"RequestPowerStateChange failed: HTTP {r.status_code}: {r.text[:300]}")
        # Extract ReturnValue (0 = success)
        rv_m = re.search(r"<[a-z0-9]+:ReturnValue>(\d+)</", r.text)
        rv = int(rv_m.group(1)) if rv_m else -1
        return {"action": action, "boot": boot, "return_value": rv, "ok": rv == 0}

    async def _change_boot_order(self, source_instance_id: str) -> None:
        """Set one-time boot via CIM_BootConfigSetting.ChangeBootOrder."""
        uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_BootConfigSetting"
        action = f"{uri}/ChangeBootOrder"
        body = f"""<r:ChangeBootOrder_INPUT xmlns:r="{uri}">
  <r:Source>
    <wsa:Address>{_ANON}</wsa:Address>
    <wsa:ReferenceParameters>
      <wsman:ResourceURI>http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_BootSourceSetting</wsman:ResourceURI>
      <wsman:SelectorSet>
        <wsman:Selector Name="InstanceID">{source_instance_id}</wsman:Selector>
      </wsman:SelectorSet>
    </wsa:ReferenceParameters>
  </r:Source>
</r:ChangeBootOrder_INPUT>"""
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}">
  <s:Header>
    <wsa:Action s:mustUnderstand="true">{action}</wsa:Action>
    <wsa:To s:mustUnderstand="true">{self.url}</wsa:To>
    <wsman:ResourceURI s:mustUnderstand="true">{uri}</wsman:ResourceURI>
    <wsa:MessageID s:mustUnderstand="true">uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:ReplyTo><wsa:Address>{_ANON}</wsa:Address></wsa:ReplyTo>
    <wsman:SelectorSet>
      <wsman:Selector Name="InstanceID">Intel(r) AMT: Boot Configuration 0</wsman:Selector>
    </wsman:SelectorSet>
    <wsman:OperationTimeout>{self._wsman_timeout}</wsman:OperationTimeout>
  </s:Header>
  <s:Body>{body}</s:Body>
</s:Envelope>"""
        assert self._client is not None
        r = await self._client.post(self.url, data=envelope, auth=self._auth)
        if r.status_code != 200:
            raise AMTError(self.host, "http", f"ChangeBootOrder failed: HTTP {r.status_code}")

    async def _set_boot_setting(self, *, biossetup: bool = False) -> None:
        """Put AMT_BootSettingData with a known-safe config (optionally
        BIOSSetup=true for next-boot-to-BIOS)."""
        uri = "http://intel.com/wbem/wscim/1/amt-schema/1/AMT_BootSettingData"
        # Use Get first to preserve any fields we don't know about, then Put modified.
        action_get = f"{_WST}/Get"
        env_get = self._envelope(action_get, uri, "")
        assert self._client is not None
        r = await self._client.post(self.url, data=env_get, auth=self._auth)
        if r.status_code != 200:
            raise AMTError(self.host, "http", f"Get boot settings failed: HTTP {r.status_code}")
        # Extract the body of AMT_BootSettingData, modify BIOSSetup, Put it back.
        m = re.search(r"<[a-z0-9]+:AMT_BootSettingData[^>]*>(.*?)</[a-z0-9]+:AMT_BootSettingData>",
                      r.text, re.DOTALL)
        if not m:
            # Fallback: construct a minimal one
            body_inner = f"""<g:BIOSPause>false</g:BIOSPause>
<g:BIOSSetup>{'true' if biossetup else 'false'}</g:BIOSSetup>
<g:BootMediaIndex>0</g:BootMediaIndex>
<g:ConfigurationDataReset>false</g:ConfigurationDataReset>
<g:ElementName>Intel(r) AMT Boot Configuration Settings</g:ElementName>
<g:EnforceSecureBoot>false</g:EnforceSecureBoot>
<g:FirmwareVerbosity>0</g:FirmwareVerbosity>
<g:ForcedProgressEvents>false</g:ForcedProgressEvents>
<g:IDERBootDevice>0</g:IDERBootDevice>
<g:InstanceID>Intel(r) AMT:BootSettingData 0</g:InstanceID>
<g:LockKeyboard>false</g:LockKeyboard>
<g:LockPowerButton>false</g:LockPowerButton>
<g:LockResetButton>false</g:LockResetButton>
<g:LockSleepButton>false</g:LockSleepButton>
<g:OptionsCleared>true</g:OptionsCleared>
<g:OwningEntity>Intel(r) AMT</g:OwningEntity>
<g:ReflashBIOS>false</g:ReflashBIOS>
<g:SecureErase>false</g:SecureErase>
<g:UseIDER>false</g:UseIDER>
<g:UseSOL>false</g:UseSOL>
<g:UseSafeMode>false</g:UseSafeMode>
<g:UserPasswordBypass>false</g:UserPasswordBypass>
<g:WinREBootEnabled>false</g:WinREBootEnabled>"""
            body = f'<g:AMT_BootSettingData xmlns:g="{uri}">{body_inner}</g:AMT_BootSettingData>'
        else:
            inner = m.group(1)
            # Replace BIOSSetup value
            inner = re.sub(
                r"(<[a-z0-9]+:BIOSSetup>)[^<]*(</)",
                rf"\g<1>{'true' if biossetup else 'false'}\g<2>",
                inner,
            )
            body = f'<g:AMT_BootSettingData xmlns:g="{uri}">{inner}</g:AMT_BootSettingData>'
        env_put = self._envelope(f"{_WST}/Put", uri, body)
        r2 = await self._client.post(self.url, data=env_put, auth=self._auth)
        if r2.status_code != 200:
            raise AMTError(self.host, "http", f"Put boot settings failed: HTTP {r2.status_code}: {r2.text[:300]}")
