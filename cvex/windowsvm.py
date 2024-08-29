import re
import sys
import tempfile
import procmon_parser

from cvex.consts import *
from cvex.vm import VM, VMTemplate


class WindowsVM(VM):
    def __init__(self,
                 vms: list,
                 template: VMTemplate,
                 cve: str):
        super().__init__(vms, template, cve)

    def init(self, router: VM | None = None):
        self.log.info("Initializing the Windows VM")
        self.ssh.run_command("curl https://download.sysinternals.com/files/ProcessMonitor.zip -o ProcessMonitor.zip")
        self.ssh.run_command("mkdir C:\\Tools")
        self.ssh.run_command("tar -xf ProcessMonitor.zip -C C:\\Tools")

        if router:
            # Install the Certificate Authority (root) certificate
            local_cert = tempfile.NamedTemporaryFile()
            router.ssh.download_file(local_cert.name, f"/home/{router.vag.user()}/.mitmproxy/mitmproxy-ca-cert.cer")
            dest_crt = f"C:\\Users\\{self.vag.user()}\\mitmproxy-ca-cert.cer"
            self.ssh.upload_file(local_cert.name, f"/{dest_crt}")
            self.ssh.run_command((f"powershell \""
                                  f"Import-Certificate -FilePath '{dest_crt}' -CertStoreLocation Cert:\\LocalMachine\\Root\""))
            # Install the empty Certificate Revocation List
            local_crl = tempfile.NamedTemporaryFile()
            router.ssh.download_file(local_crl.name, f"/home/{router.vag.user()}/.mitmproxy/root.crl")
            dest_crl = f"C:\\Users\\{self.vag.user()}\\root.crl"
            self.ssh.upload_file(local_crl.name, f"/{dest_crl}")
            self.ssh.run_command(f"certutil -addstore CA {dest_crl}")

    def update_hosts(self, vms: list[VM]):
        remote_hosts = "/C:\\Windows\\System32\\drivers\\etc\\hosts"
        local_hosts = tempfile.NamedTemporaryFile()
        self.ssh.download_file(local_hosts.name, remote_hosts)
        with open(local_hosts.name, "r") as f:
            hosts = f.read()
        ips = "\r\n"
        for vm in vms:
            if vm != self:
                line = f"{vm.ip} {vm.vm_name}\r\n"
                if line not in hosts:
                    ips += line
        if ips != "\r\n":
            self.log.debug("Setting ip hosts: %s", ips)
            hosts += ips
            with open(local_hosts.name, "w") as f:
                f.write(hosts)
            self.ssh.upload_file(local_hosts.name, remote_hosts)

    def _get_vagrant_winrm_config(self) -> dict:
        output = self._run_shell_command(["vagrant", "winrm-config"], cwd=self.destination)
        values = {
            "host": rb"HostName (\d+\.\d+\.\d+\.\d+)",
            "user": rb"User (\w+)",
            "password": rb"Password (\w+)",
            "port": rb"Port (\d+)"
        }
        config = {}
        for key, regexp in values.items():
            r = re.search(regexp, output)
            if not r:
                self.log.critical("'vagrant winrm-config' returned unusual output: %s", output)
                sys.exit(1)
            config[key] = r.group(1).decode()
        return config

    def get_ansible_inventory(self) -> Path:
        inventory = Path(self.destination, "inventory.ini")
        with open(inventory, "w") as f:
            self.log.info("Retrieving WinRM configuration of %s...", self.vm_name)
            config = self._get_vagrant_winrm_config()
            data = (f"{self.vm_name} "
                    f"ansible_connection=winrm "
                    f"ansible_winrm_scheme=http "
                    f"ansible_host={config['host']} "
                    f"ansible_port={config['port']} "
                    f"ansible_user={config['user']} "
                    f"ansible_password={config['password']} "
                    f"ansible_winrm_operation_timeout_sec=200  "
                    f"ansible_winrm_read_timeout_sec=210 "
                    f"operation_timeout_sec=250 "
                    f"read_timeout_sec=260")
            f.write(data)
        return inventory

    def set_network_interface_ip(self, router_ip: str):
        try:
            self.ssh.run_command((f"powershell \""
                                f"Get-NetAdapter -Name 'Ethernet 2' | "
                                f"New-NetIPAddress -IPAddress {self.ip} -DefaultGateway {router_ip} -PrefixLength 24\""))
        except:
            pass
        self.ssh.run_command("route DELETE 192.168.56.0")
        route_print = self.ssh.run_command("route print")
        id = re.search(r"(\d+)\.\.\.([0-9a-fA-F]{2} ){6}\.\.\.\.\.\.Intel\(R\) PRO/1000 MT Desktop Adapter #2",
                       route_print)
        if not id:
            self.log.critical("'route print' returned unknown data:\n%s", route_print)
            sys.exit(1)
        id = id.group(1)
        self.ssh.run_command(f"route ADD 192.168.56.0 MASK 255.255.255.0 {router_ip} if {id}")

    def start_api_tracing(self):
        try:
            self.ssh.run_command("taskkill /IM Procmon.exe /F")
        except:
            pass
        try:
            self.ssh.run_command(f"rmdir /S /Q {CVEX_TEMP_FOLDER_WINDOWS}")
        except:
            pass
        self.ssh.run_command(f"mkdir {CVEX_TEMP_FOLDER_WINDOWS}")

        remote_config_path = f"{CVEX_TEMP_FOLDER_WINDOWS}\\config.pmc"
        if self.trace:
            with open("data/procmon.pmc", "rb") as f:
                config = procmon_parser.load_configuration(f)
            config["FilterRules"] = [
                procmon_parser.Rule(
                    procmon_parser.Column.PROCESS_NAME,
                    procmon_parser.RuleRelation.CONTAINS,
                    self.trace,
                    procmon_parser.RuleAction.INCLUDE)]
            local_config = tempfile.NamedTemporaryFile()
            with open(local_config.name, "wb") as f:
                procmon_parser.dump_configuration(config, f)
            self.ssh.upload_file(local_config.name, f"/{remote_config_path}")
            self.ssh.run_command(
                f"C:\\Tools\\Procmon.exe /AcceptEula /BackingFile {PROCMON_PML_LOG_PATH} /LoadConfig {remote_config_path} /Quiet",
                is_async=True)
        else:
            self.ssh.run_command(
                f"C:\\Tools\\Procmon.exe /AcceptEula /BackingFile {PROCMON_PML_LOG_PATH} /Quiet",
                is_async=True)

    def stop_api_tracing(self, output_dir: str):
        self.ssh.run_command("C:\\Tools\\Procmon.exe /AcceptEula /Terminate")
        self.ssh.run_command(
            f"C:\Tools\Procmon.exe /AcceptEula /OpenLog {PROCMON_PML_LOG_PATH} /SaveAs {PROCMON_XML_LOG_PATH}")
        self.ssh.download_file(f"{output_dir}/{self.vm_name}_{PROCMON_PML_LOG}", f"/{PROCMON_PML_LOG_PATH}")
        self.ssh.download_file(f"{output_dir}/{self.vm_name}_{PROCMON_XML_LOG}", f"/{PROCMON_XML_LOG_PATH}")
