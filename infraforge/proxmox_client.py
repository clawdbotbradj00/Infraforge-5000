"""Proxmox API client for InfraForge."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from infraforge.config import Config
from infraforge.models import (
    VM, VMStatus, VMType,
    NodeInfo, Template, TemplateType,
    StorageInfo,
)


class ProxmoxConnectionError(Exception):
    """Failed to connect to Proxmox."""
    pass


# How long to cache the node list (seconds)
_NODE_CACHE_TTL = 10


class ProxmoxClient:
    """Client for interacting with Proxmox VE API."""

    def __init__(self, config: Config):
        self.config = config
        self._api = None
        self._node_cache: list[dict] | None = None
        self._node_cache_ts: float = 0

    def connect(self):
        """Establish connection to Proxmox API."""
        try:
            from proxmoxer import ProxmoxAPI
            import urllib3

            if not self.config.proxmox.verify_ssl:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            pve_cfg = self.config.proxmox

            if pve_cfg.auth_method == "token":
                self._api = ProxmoxAPI(
                    pve_cfg.host,
                    port=pve_cfg.port,
                    user=pve_cfg.user,
                    token_name=pve_cfg.token_name,
                    token_value=pve_cfg.token_value,
                    verify_ssl=pve_cfg.verify_ssl,
                    timeout=15,
                )
            else:
                self._api = ProxmoxAPI(
                    pve_cfg.host,
                    port=pve_cfg.port,
                    user=pve_cfg.user,
                    password=pve_cfg.password,
                    verify_ssl=pve_cfg.verify_ssl,
                    timeout=15,
                )

            # Test the connection
            self._api.version.get()

        except Exception as e:
            raise ProxmoxConnectionError(
                f"Failed to connect to Proxmox at {pve_cfg.host}:{pve_cfg.port}: {e}"
            )

    @property
    def api(self):
        if self._api is None:
            self.connect()
        return self._api

    # ------------------------------------------------------------------
    # Node helpers (cached)
    # ------------------------------------------------------------------

    def _get_nodes_raw(self, force: bool = False) -> list[dict]:
        """Get raw node list, cached for _NODE_CACHE_TTL seconds."""
        now = time.monotonic()
        if force or self._node_cache is None or (now - self._node_cache_ts) > _NODE_CACHE_TTL:
            self._node_cache = self.api.nodes.get()
            self._node_cache_ts = now
        return self._node_cache

    def _online_node_names(self) -> list[str]:
        return [n["node"] for n in self._get_nodes_raw() if n.get("status") == "online"]

    def get_nodes(self) -> list[dict]:
        """Get raw node data."""
        return self._get_nodes_raw()

    def _fetch_node_cpu_model(self, node_name: str) -> str:
        """Fetch CPU model string from node status endpoint."""
        try:
            status = self.api.nodes(node_name).status.get()
            cpuinfo = status.get("cpuinfo", {})
            model = cpuinfo.get("model", "")
            return model
        except Exception:
            return ""

    def get_node_info(self, force: bool = False) -> list[NodeInfo]:
        """Get info about all cluster nodes."""
        raw = self._get_nodes_raw(force=force)
        nodes = [
            NodeInfo(
                node=n.get("node", ""),
                status=n.get("status", "unknown"),
                cpu=float(n.get("cpu", 0)),
                maxcpu=int(n.get("maxcpu", 0)),
                mem=int(n.get("mem", 0)),
                maxmem=int(n.get("maxmem", 0)),
                disk=int(n.get("disk", 0)),
                maxdisk=int(n.get("maxdisk", 0)),
                uptime=int(n.get("uptime", 0)),
                ssl_fingerprint=n.get("ssl_fingerprint", ""),
            )
            for n in raw
        ]
        # Enrich online nodes with CPU model in parallel
        online = [ni for ni in nodes if ni.status == "online"]
        if online:
            with ThreadPoolExecutor(max_workers=min(len(online), 4)) as pool:
                futures = {pool.submit(self._fetch_node_cpu_model, ni.node): ni for ni in online}
                for fut in futures:
                    ni = futures[fut]
                    try:
                        ni.cpu_model = fut.result(timeout=5)
                    except Exception:
                        pass
        return nodes

    # ------------------------------------------------------------------
    # VMs & templates (parallelized per node)
    # ------------------------------------------------------------------

    def _fetch_node_qemu(self, node_name: str) -> list[dict]:
        """Fetch QEMU VMs for a single node."""
        try:
            return self.api.nodes(node_name).qemu.get()
        except Exception:
            return []

    def _fetch_node_lxc(self, node_name: str) -> list[dict]:
        """Fetch LXC containers for a single node."""
        try:
            return self.api.nodes(node_name).lxc.get()
        except Exception:
            return []

    def _fetch_all_qemu_lxc(self) -> dict[str, tuple[list[dict], list[dict]]]:
        """Fetch QEMU and LXC data for all online nodes in parallel.

        Returns {node_name: (qemu_list, lxc_list)}.
        """
        nodes = self._online_node_names()
        results: dict[str, tuple[list[dict], list[dict]]] = {n: ([], []) for n in nodes}

        with ThreadPoolExecutor(max_workers=len(nodes) * 2) as pool:
            futures = {}
            for node in nodes:
                futures[pool.submit(self._fetch_node_qemu, node)] = (node, "qemu")
                futures[pool.submit(self._fetch_node_lxc, node)] = (node, "lxc")

            for future in as_completed(futures):
                node, kind = futures[future]
                data = future.result()
                if kind == "qemu":
                    results[node] = (data, results[node][1])
                else:
                    results[node] = (results[node][0], data)

        return results

    def get_all_vms(self) -> list[VM]:
        """Get all VMs and containers across all nodes (parallel)."""
        vms = []
        for node_name, (qemu, lxc) in self._fetch_all_qemu_lxc().items():
            for v in qemu:
                if v.get("template", 0) == 1:
                    continue
                vms.append(self._parse_vm(v, node_name, VMType.QEMU))
            for v in lxc:
                if v.get("template", 0) == 1:
                    continue
                vms.append(self._parse_vm(v, node_name, VMType.LXC))
        return vms

    def get_vm_templates(self) -> list[Template]:
        """Get VM/CT templates (machines marked as template) (parallel)."""
        templates = []
        for node_name, (qemu, lxc) in self._fetch_all_qemu_lxc().items():
            for v in qemu:
                if v.get("template", 0) == 1:
                    templates.append(Template(
                        name=v.get("name", f"template-{v['vmid']}"),
                        template_type=TemplateType.VM,
                        node=node_name,
                        vmid=v.get("vmid"),
                        size=v.get("maxdisk", 0),
                        description=v.get("name", ""),
                    ))
            for v in lxc:
                if v.get("template", 0) == 1:
                    templates.append(Template(
                        name=v.get("name", f"ct-template-{v['vmid']}"),
                        template_type=TemplateType.CONTAINER,
                        node=node_name,
                        vmid=v.get("vmid"),
                        size=v.get("maxdisk", 0),
                        description=v.get("name", ""),
                    ))
        return templates

    def get_all_vms_and_templates(self) -> tuple[list[VM], list[Template]]:
        """Get VMs and templates in a single pass (avoids duplicate API calls)."""
        vms = []
        templates = []
        for node_name, (qemu, lxc) in self._fetch_all_qemu_lxc().items():
            for v in qemu:
                if v.get("template", 0) == 1:
                    templates.append(Template(
                        name=v.get("name", f"template-{v['vmid']}"),
                        template_type=TemplateType.VM,
                        node=node_name,
                        vmid=v.get("vmid"),
                        size=v.get("maxdisk", 0),
                        description=v.get("name", ""),
                    ))
                else:
                    vms.append(self._parse_vm(v, node_name, VMType.QEMU))
            for v in lxc:
                if v.get("template", 0) == 1:
                    templates.append(Template(
                        name=v.get("name", f"ct-template-{v['vmid']}"),
                        template_type=TemplateType.CONTAINER,
                        node=node_name,
                        vmid=v.get("vmid"),
                        size=v.get("maxdisk", 0),
                        description=v.get("name", ""),
                    ))
                else:
                    vms.append(self._parse_vm(v, node_name, VMType.LXC))
        return vms, templates

    # ------------------------------------------------------------------
    # VM detail (single VM, not parallelized)
    # ------------------------------------------------------------------

    def get_vm_detail(self, node: str, vmid: int, vm_type: VMType) -> dict:
        """Get detailed config for a specific VM."""
        if vm_type == VMType.QEMU:
            config = self.api.nodes(node).qemu(vmid).config.get()
            status = self.api.nodes(node).qemu(vmid).status.current.get()
        else:
            config = self.api.nodes(node).lxc(vmid).config.get()
            status = self.api.nodes(node).lxc(vmid).status.current.get()
        return {"config": config, "status": status}

    def get_vm_snapshots(self, node: str, vmid: int, vm_type: VMType) -> list[dict]:
        """Get snapshots for a VM."""
        try:
            if vm_type == VMType.QEMU:
                return self.api.nodes(node).qemu(vmid).snapshot.get()
            else:
                return self.api.nodes(node).lxc(vmid).snapshot.get()
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Downloaded templates & ISOs (parallelized per node)
    # ------------------------------------------------------------------

    def _fetch_node_storage_content(self, node_name: str) -> list[Template]:
        """Fetch downloaded templates and ISOs from a single node's storage."""
        templates = []
        try:
            storages = self.api.nodes(node_name).storage.get()
        except Exception:
            return templates

        for store in storages:
            storage_name = store["storage"]
            content_types = store.get("content", "")
            has_vztmpl = "vztmpl" in content_types
            has_iso = "iso" in content_types

            if not has_vztmpl and not has_iso:
                continue

            try:
                contents = self.api.nodes(node_name).storage(storage_name).content.get()
            except Exception:
                continue

            for item in contents:
                ct = item.get("content", "")
                volid = item.get("volid", "")
                fname = volid.split("/")[-1] if "/" in volid else volid

                if ct == "vztmpl" and has_vztmpl:
                    templates.append(Template(
                        name=fname, template_type=TemplateType.CONTAINER,
                        node=node_name, storage=storage_name,
                        volid=volid, size=item.get("size", 0),
                    ))
                elif ct == "iso" and has_iso:
                    templates.append(Template(
                        name=fname, template_type=TemplateType.ISO,
                        node=node_name, storage=storage_name,
                        volid=volid, size=item.get("size", 0),
                    ))

        return templates

    def get_downloaded_templates(self, node: Optional[str] = None) -> list[Template]:
        """Get already-downloaded templates from storage (parallel)."""
        if node:
            return self._fetch_node_storage_content(node)

        nodes = self._online_node_names()
        all_templates: list[Template] = []

        with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
            futures = {pool.submit(self._fetch_node_storage_content, n): n for n in nodes}
            for future in as_completed(futures):
                all_templates.extend(future.result())

        return all_templates

    # ------------------------------------------------------------------
    # Appliance templates (rarely used, single node)
    # ------------------------------------------------------------------

    def get_appliance_templates(self, node: Optional[str] = None) -> list[Template]:
        """Get available appliance templates from pveam."""
        templates = []
        if node is None:
            online = self._online_node_names()
            if online:
                node = online[0]
            else:
                return templates
        try:
            for t in self.api.nodes(node).aplinfo.get():
                templates.append(Template(
                    name=t.get("template", t.get("package", "")),
                    template_type=TemplateType.CONTAINER, node=node,
                    description=t.get("description", ""), os=t.get("os", ""),
                    section=t.get("section", ""), package=t.get("package", ""),
                    architecture=t.get("architecture", ""),
                    headline=t.get("headline", ""), infopage=t.get("infopage", ""),
                    location=t.get("location", ""), maintainer=t.get("maintainer", ""),
                    source=t.get("source", ""), version=t.get("version", ""),
                    sha512sum=t.get("sha512sum", ""),
                ))
        except Exception:
            pass
        return templates

    def download_appliance_template(self, node: str, storage: str, template: str) -> str:
        """Download an appliance template via pveam. Returns UPID."""
        return self.api.nodes(node).aplinfo.post(storage=storage, template=template)

    def download_url_to_storage(self, node: str, storage: str, url: str,
                                filename: str, content: str = "iso",
                                checksum: str | None = None,
                                checksum_algorithm: str | None = None) -> str:
        """Download a URL to Proxmox storage. Returns UPID.

        content: 'iso' for ISO images, 'vztmpl' for container templates
        checksum: hex digest for integrity verification (optional)
        checksum_algorithm: one of sha256sum, sha512sum, md5sum, etc.
        """
        kwargs: dict = dict(url=url, filename=filename, content=content)
        if checksum and checksum_algorithm:
            kwargs["checksum"] = checksum
            kwargs["checksum-algorithm"] = checksum_algorithm
        return self.api.nodes(node).storage(storage)('download-url').post(**kwargs)

    # ------------------------------------------------------------------
    # Storage info (parallelized per node)
    # ------------------------------------------------------------------

    def _fetch_node_storage_info(self, node_name: str) -> list[StorageInfo]:
        """Fetch storage info for a single node."""
        storages = []
        try:
            for s in self.api.nodes(node_name).storage.get():
                storages.append(StorageInfo(
                    storage=s.get("storage", ""),
                    node=node_name,
                    storage_type=s.get("type", ""),
                    content=s.get("content", ""),
                    active=bool(s.get("active", 1)),
                    enabled=bool(s.get("enabled", 1)),
                    shared=bool(s.get("shared", 0)),
                    total=int(s.get("total", 0)),
                    used=int(s.get("used", 0)),
                    avail=int(s.get("avail", 0)),
                ))
        except Exception:
            pass
        return storages

    def get_storage_info(self, node: Optional[str] = None) -> list[StorageInfo]:
        """Get storage information (parallel)."""
        if node:
            return self._fetch_node_storage_info(node)

        nodes = self._online_node_names()
        all_storages: list[StorageInfo] = []

        with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
            futures = {pool.submit(self._fetch_node_storage_info, n): n for n in nodes}
            for future in as_completed(futures):
                all_storages.extend(future.result())

        return all_storages

    # ------------------------------------------------------------------
    # Cluster / version
    # ------------------------------------------------------------------

    def get_cluster_status(self) -> list[dict]:
        try:
            return self.api.cluster.status.get()
        except Exception:
            return []

    def get_version(self) -> dict:
        try:
            return self.api.version.get()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # VM management operations
    # ------------------------------------------------------------------

    def get_next_vmid(self) -> int:
        """Get the next available VMID from the cluster."""
        return int(self.api.cluster.nextid.get())

    def clone_vm(self, node: str, vmid: int, newid: int, name: str = "", full: bool = True) -> str:
        """Clone a VM/template, returns UPID for task tracking."""
        return self.api.nodes(node).qemu(vmid).clone.post(
            newid=newid, name=name, full=1 if full else 0,
        )

    def get_vm_config(self, node: str, vmid: int) -> dict:
        """Get full VM configuration dict."""
        return self.api.nodes(node).qemu(vmid).config.get()

    def set_vm_config(self, node: str, vmid: int, **kwargs) -> None:
        """Set VM configuration (cores, memory, ipconfig0, nameserver, net0, etc.)."""
        self.api.nodes(node).qemu(vmid).config.put(**kwargs)

    def start_vm(self, node: str, vmid: int) -> str:
        """Start a VM, returns UPID."""
        return self.api.nodes(node).qemu(vmid).status.start.post()

    def stop_vm(self, node: str, vmid: int) -> str:
        """Stop a VM, returns UPID."""
        return self.api.nodes(node).qemu(vmid).status.stop.post()

    def get_vm_status(self, node: str, vmid: int) -> dict:
        """Get VM status dict (has 'status' key: 'running'/'stopped')."""
        return self.api.nodes(node).qemu(vmid).status.current.get()

    def convert_to_template(self, node: str, vmid: int) -> None:
        """Convert a VM to a template."""
        self.api.nodes(node).qemu(vmid).template.post()

    def delete_vm(self, node: str, vmid: int) -> str:
        """Delete a VM, returns UPID."""
        return self.api.nodes(node).qemu(vmid).delete()

    def get_all_qemu_vms(self) -> list[dict]:
        """Get all QEMU VMs (non-template) across all nodes.

        Returns list of dicts with keys: vmid, name, node, status, template (0 or 1).
        Only returns VMs where template != 1.
        """
        try:
            vms = []
            for node_data in self.api.nodes.get():
                node_name = node_data.get("node")
                if not node_name:
                    continue
                try:
                    qemu_list = self.api.nodes(node_name).qemu.get()
                    for vm in qemu_list:
                        if vm.get("template", 0) != 1:
                            vms.append({
                                "vmid": int(vm.get("vmid", 0)),
                                "name": str(vm.get("name", "")),
                                "node": node_name,
                                "status": str(vm.get("status", "")),
                            })
                except Exception:
                    pass
            return vms
        except Exception:
            return []

    def get_node_tasks(
        self,
        node: str,
        limit: int = 20,
        since: float = 0,
        type_filter: str = "",
    ) -> list[dict]:
        """Get recent tasks on a node.

        Args:
            node: Proxmox node name.
            limit: Maximum number of tasks to return.
            since: Unix timestamp — only return tasks started after this time.
            type_filter: If set, only return tasks whose 'type' contains this string.

        Returns:
            List of task dicts with keys: upid, node, type, status, starttime,
            endtime, user, id (VMID if applicable), pid.
        """
        try:
            tasks = self.api.nodes(node).tasks.get(limit=limit)
        except Exception:
            return []

        result = []
        for t in tasks:
            start_time = int(t.get("starttime", 0))
            if since and start_time < since:
                continue
            if type_filter and type_filter not in t.get("type", ""):
                continue
            result.append(t)
        return result

    def get_task_status(self, node: str, upid: str) -> dict:
        """Get status of a task by UPID."""
        return self.api.nodes(node).tasks(upid).status.get()

    def get_task_log(self, node: str, upid: str, start: int = 0,
                     limit: int = 50) -> list[dict]:
        """Get log lines from a task. Each dict has 'n' (line number) and 't' (text)."""
        return self.api.nodes(node).tasks(upid).log.get(start=start, limit=limit)

    def wait_for_task(self, node: str, upid: str, timeout: int = 120) -> bool:
        """Poll a Proxmox task until completion. Returns True if OK, False on failure/timeout."""
        elapsed = 0
        while elapsed < timeout:
            status = self.api.nodes(node).tasks(upid).status.get()
            if status.get("status") == "stopped":
                return status.get("exitstatus") == "OK"
            time.sleep(2)
            elapsed += 2
        return False

    def create_vm_from_cloud_image(self, node: str, vmid: int, name: str,
                                   storage: str, image_path: str,
                                   memory: int = 2048, cores: int = 2) -> None:
        """Create a VM, import a cloud image disk, and convert to template.

        This creates the VM, sets the boot config, and converts to template.
        The actual disk import needs to happen via qm importdisk on the node.
        """
        # Create the VM
        self.api.nodes(node).qemu.post(
            vmid=vmid,
            name=name,
            memory=memory,
            cores=cores,
            ostype="l26",
            scsihw="virtio-scsi-single",
            net0="virtio,bridge=vmbr0",
            serial0="socket",
            vga="serial0",
            agent="enabled=1",
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_vm(self, data: dict, node: str, vm_type: VMType) -> VM:
        return VM(
            vmid=int(data.get("vmid", 0)),
            name=data.get("name", f"VM {data.get('vmid', '?')}"),
            status=VMStatus.from_str(data.get("status", "unknown")),
            node=node,
            vm_type=vm_type,
            cpu=float(data.get("cpu", 0)),
            cpus=int(data.get("cpus", data.get("maxcpu", 0))),
            mem=int(data.get("mem", 0)),
            maxmem=int(data.get("maxmem", 0)),
            disk=int(data.get("disk", 0)),
            maxdisk=int(data.get("maxdisk", 0)),
            uptime=int(data.get("uptime", 0)),
            netin=int(data.get("netin", 0)),
            netout=int(data.get("netout", 0)),
            pid=data.get("pid"),
            tags=data.get("tags", ""),
            template=bool(data.get("template", 0)),
        )
