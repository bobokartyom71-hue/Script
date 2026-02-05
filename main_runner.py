from datetime import datetime
import re
import time
import logging
import os
import sys
from tqdm import tqdm
from dotenv import load_dotenv
from getpass import getpass
from itertools import repeat 
from concurrent.futures import ThreadPoolExecutor, as_completed
from netmiko import (ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException)
from paramiko.ssh_exception import SSHException
from pynetbox_class import ArgumentParser, Netbox
from commands import COMMANDS
from typing import Optional
from queue import Queue, Empty
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.console import Console

load_dotenv()

logfile = f"result_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

logging.basicConfig(
    filename=logfile,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

err_handler = logging.FileHandler("errors.log")
err_handler.setLevel(logging.ERROR)
err_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    )
)

logging.getLogger().addHandler(err_handler)
logging.getLogger("netmiko").setLevel(logging.WARNING)
logging.getLogger("paramiko").setLevel(logging.WARNING)

class ProgressReporter:
    def __init__(self, progress):
        self.progress = progress

    def start_host(self, task_id: int, host: str) -> None:
        self.progress.reset(task_id, total=5, completed=0)
        self.progress.update(task_id, host=host, stage="init", result="")

    def stage(self, task_id: int, stage: str) -> None:
        self.progress.update(task_id, stage=stage)

    def advance(self, task_id: int, steps: int = 1) -> None:
        self.progress.advance(task_id, steps)

    def done(self, task_id: int, result_status: str) -> None:
        t = self.progress.tasks[task_id]
        if t.total is not None and t.completed < t.total:
            self.progress.update(task_id, completed=t.total)
        self.progress.update(task_id, stage="done", result=result_status)

    def idle(self, task_id: int) -> None:
        self.progress.update(task_id, host="-", stage="idle", result="")
        self.progress.reset(task_id, total=5, completed=0)



class SSHClient:
    def __init__(self, device: dict):
        self.device = device
        self.ssh = None

    def connect(self) -> tuple[bool]:
        host = self.device.get("host")
        try:
            self.ssh = ConnectHandler(**self.device)
            logging.info(f"{host} | CONNECTED")
            return True, None

        except NetmikoAuthenticationException:
            logging.error(f"{host} | AUTHENTICATION FAILED")
            return False, "auth_failed"

        except NetmikoTimeoutException:
            logging.error(f"{host} | CONNECTION TIMEOUT")
            return False, "timeout"

        except SSHException as e:
            if (
                "Authentication failed" in str(e)
                or "Unable to open channel" in str(e)
            ):
                logging.error(f"{host} | AUTHENTICATION FAILED")
                return False, "auth_failed"
            raise

    def __enter__(self):
        return self.ssh

    def __exit__(self, exc_type, exc, tb):
        if self.ssh:
            try:
                self.ssh.disconnect()
            except Exception:
                pass

class DeviceWorkflow:
    def __init__(self, handler, ssh, args: dict):
        self.handler = handler
        self.ssh = ssh
        self.args = args

    def run(self) -> tuple[dict, list[str]]:
        """
        Выполняет collect → analyze → build_config
        """
        if self.args.get("force"):
            parsed = {}
        else:
            show_data = self.handler.collect(self.ssh)
            parsed = self.handler.analyze(show_data)

        config = self.handler.build_config(parsed)
        return parsed, config

    def apply(self, config: list[str]) -> bool:
        """
        Применяет конфигурацию, если она есть
        """
        if not config:
            return True
        return self.handler.apply(self.ssh, config)



class DeviceHandler:
    def __init__(
        self,
        device: dict,
        commands: dict,
        args: dict,
        reporter: Optional["ProgressReporter"] = None,
        task_id: Optional[int] = None,
    ):
        self.device = device
        self.commands = commands
        self.args = args or {}
        self.reporter = reporter
        self.task_id = task_id

    def _stage(self, stage: str) -> None:
        if self.reporter and self.task_id is not None:
            self.reporter.stage(self.task_id, stage)

    def _advance(self, steps: int = 1) -> None:
        if self.reporter and self.task_id is not None:
            self.reporter.advance(self.task_id, steps)

    # ----------------- orchestration -----------------

    def process(self) -> dict:
        host = self.device.get("host")

        result = {
            "host": host,
            "mode": "vty" if self.is_vty_mode() else "normal",
            "parsed": {},
            "config": [],
            "status": "init",
        }

        self._stage("init")
        logging.info(f"{host} | START | mode={result['mode']}")

        # --- FORCE + TEST без SSH ---
        if self.args.get("force") and self.args.get("test"):
            self._stage("build_config(test)")
            parsed = {}
            config = self.build_config(parsed)
            self._advance(4)  # условно "проскочили" до конца шагов
            self.print_test(parsed, config)
            result.update(parsed=parsed, config=config, status="test")
            self._stage("done:test")
            return result

        self._stage("connect")
        ssh_client = SSHClient(self.device)
        connected, status = ssh_client.connect()

        if not connected:
            result["status"] = status
            self._stage(f"done:{status}")
            return result

        self._advance(1)  # connect done

        try:
            with ssh_client as ssh:
                workflow = DeviceWorkflow(self, ssh, self.args)

                if self.args.get("force"):
                    self._stage("build_config(force)")
                else:
                    self._stage("collect")
                parsed, config = workflow.run()

                if self.args.get("force"):
                    self._advance(1)  
                else:
                    self._advance(3) 

                result["parsed"] = parsed
                result["config"] = config

                if self.args.get("test"):
                    self._stage("test")
                    self.print_test(parsed, config)
                    result["status"] = "test"
                    self._stage("done:test")
                    return result

                if config:
                    self._stage("apply")
                    logging.info(f"{host} | APPLY")
                    ok = workflow.apply(config)
                    self._advance(1)  

                    if ok:
                        result["status"] = "applied"
                    else:
                        result["status"] = "not_applied"
                        logging.warning(f"{host} | CONFIG NOT APPLIED DUE TO ERRORS")
                else:
                    self._stage("noop")
                    self._advance(1)
                    result["status"] = "noop"

        except Exception:
            logging.exception(f"{host} | UNEXPECTED ERROR")
            result["status"] = "error"
            self._stage("done:error")

        logging.info(f"{host} | DONE | status={result['status']}")
        self._stage(f"done:{result['status']}")
        return result

    # ----------------- steps (оставлены без изменений) -----------------

    def collect(self, ssh) -> dict[str, list[str]]:
        data = {
            "subnet": [],
            "vty": [],
        }

        if self.is_vty_mode():
            for cmd in self.commands.get("show_vty", []):
                logging.debug(
                    f"{self.device['host']} | show vty: {cmd}"
                )
                data["vty"].append(ssh.send_command(cmd))
        else:
            for cmd in self.commands.get("show_subnet", []):
                logging.debug(
                    f"{self.device['host']} | show subnet: {cmd}"
                )
                data["subnet"].append(ssh.send_command(cmd))

        return data

    def analyze(self, show_data: dict) -> dict:
        return {}

    def build_config(self, parsed: dict) -> list[str]:
        return []

    def apply(self, ssh, config: list[str]) -> bool:
        host = self.device["host"]
        has_error = False

        logging.debug(
            f"{host} | CONFIG COMMANDS:\n" + "\n".join(config)
        )

        output = ssh.send_config_set(
            config,
            exit_config_mode=True,
            strip_prompt=False,
            strip_command=False,
        )

        for line in output.splitlines():
            if (
                "Access rule can't be configured" in line
                or "Too many parameters" in line
                or "% Invalid input" in line
                or "%Error" in line
                or line.strip().startswith("Error:")
                or "bad command" in line
                or "error" in line
            ):
                logging.error(
                    f"{host} | CONFIG ERROR | {line.strip()}"
                )
                has_error = True

        logging.info(f"{host} | CONFIG APPLY FINISHED")

        if not has_error and not self.args.get("coro"):
            ssh.save_config()
            logging.info(f"{host} | CONFIG SAVED")

        return not has_error

    # ----------------- helpers -----------------

    def is_vty_mode(self) -> bool:
        return bool(self.args.get("vty"))

    def print_test(self, parsed: dict, config: list[str]) -> None:
        print("\n" + "=" * 70)
        print(
            f"[TEST MODE] {self.device['host']} "
            f"({self.device['device_type']})"
        )
        print(f"Mode: {'VTY' if self.is_vty_mode() else 'NORMAL'}")

        print("\nParsed data:")
        for key, value in parsed.items():
            if (
                self.is_vty_mode()
                and key == "acl_types"
                and "acl_name" in parsed
            ):
                acl_name = parsed["acl_name"]
                acl_type = value.get(acl_name)
                print(f"  acl_type: {acl_type or '<not found>'}")
                continue

            print(f"  {key}: {value}")

        print("\nGenerated config:")
        for cmd in config:
            print(f"  {cmd}")

        print("=" * 70 + "\n")


class CiscoHandler(DeviceHandler):
    """
    Cisco IOS handler
    """

    # ----------------- analyze -----------------

    def analyze(self, show_data: dict) -> dict:
        parsed = {}

        for output in show_data.get("subnet", []):
            data = self.parse_subnet(output)
            if data:
                parsed.update(data)

        for output in show_data.get("vty", []):
            data = self.parse_vty(output)
            if data:
                parsed.update(data)

        return parsed

    # ----------------- parsers -----------------

    def parse_subnet(self, output: str) -> dict:
        m = re.search(r"\d+\.\d+\.\d+\.\d+", output)
        if not m:
            return {}

        ip = m.group()

        if self.args.get("copo"):
            return {"subnet": ".".join(ip.split(".")[:3])}

        if self.args.get("coco"):
            parts = ip.split(".")
            parts[3] = str(int(parts[3]) - 1)
            return {"subnet": ".".join(parts)}

        return {}

    def parse_vty(self, output: str) -> dict:
        result = {}

        m = re.search(r"access-class\s+(\S+)", output)
        if m:
            result["acl_name"] = m.group(1)

        for m in re.finditer(
            r"ip access-list\s+(standard|extended)\s+(\S+)", output
        ):
            result.setdefault("acl_types", {})[
                m.group(2)
            ] = m.group(1).lower()

        return result

    # ----------------- config generation -----------------

    def build_config(self, parsed: dict) -> list[str]:
        if self.args.get("force"):
            return self.commands.get("config_template", [])

        if self.is_vty_mode():
            return self.build_vty_config(parsed)
        return self.build_subnet_config(parsed)

    def build_vty_config(self, parsed: dict) -> list[str]:
        acl_name = parsed.get("acl_name")
        acl_types = parsed.get("acl_types", {})

        if not acl_name or acl_name not in acl_types:
            logging.warning(
                f"{self.device['host']} | "
                "VTY mode: ACL data incomplete"
            )
            return []

        acl_type = acl_types[acl_name]
        tmpl = self.commands.get(
            f"config_template_{acl_type}", []
        )

        return [
            cmd.format(
                acl_name=acl_name,
                acl_types=acl_type
            )
            for cmd in tmpl
        ]

    def build_subnet_config(self, parsed: dict) -> list[str]:
        subnet = parsed.get("subnet")
        if not subnet:
            return []

        tmpl = self.commands.get("config_template", [])
        return [cmd.format(subnet=subnet) for cmd in tmpl]


class HuaweiHandler(DeviceHandler):
    """
    Huawei VRP handler
    """

    # ----------------- analyze -----------------

    def analyze(self, show_data: dict) -> dict:
        parsed = {}

        for output in show_data.get("subnet", []):
            data = self.parse_subnet(output)
            if data:
                parsed.update(data)

        for output in show_data.get("vty", []):
            data = self.parse_vty(output)
            if data:
                parsed.update(data)

        return parsed

    # ----------------- parsers -----------------

    def parse_subnet(self, output: str) -> dict:
        m = re.search(r"\d+\.\d+\.\d+\.\d+", output)
        if not m:
            return {}

        ip = m.group()

        if self.args.get("coco"):
            parts = ip.split(".")
            parts[3] = str(int(parts[3]) - 1)
            return {"subnet": ".".join(parts)}

        return {}

    def parse_vty(self, output: str) -> dict:
        """
        Парсинг user-interface / acl конфигурации
        """
        result = {}

        m = re.search(r"acl\s+(\d+)", output)
        if m:
            result["acl_number"] = m.group(1)

        acl_names = {}
        pattern = re.compile(r'^acl\s+name\s+(\S+)\s+(\d+)$', re.MULTILINE)
        acl_names = {str(num): name for name, num in pattern.findall(output)}
        result["acl_names"] = acl_names

        return result

    # ----------------- config generation -----------------

    def build_config(self, parsed: dict) -> list[str]:
        if self.args.get("force"):
            return self.commands.get("config_template", [])

        if self.is_vty_mode():
            return self.build_vty_config(parsed)
        return self.build_subnet_config(parsed)

    def build_vty_config(self, parsed: dict) -> list[str]:
        acl_number = parsed.get("acl_number")
        acl_names = parsed.get("acl_names", {})

        if not acl_number or acl_number not in acl_names:
            logging.warning(
                f"{self.device['host']} | "
                "VTY mode: ACL data incomplete"
            )
            return []

        acl_name = acl_names[acl_number]

        tmpl = self.commands.get(
            f"config_template_advanced", []
        )

        return [
            cmd.format(
                acl_number=acl_number,
                acl_name=acl_name
            )
            for cmd in tmpl
        ]

    def build_subnet_config(self, parsed: dict) -> list[str]:
        subnet = parsed.get("subnet")
        if not subnet:
            return []

        tmpl = self.commands.get("config_template", [])
        return [
            cmd.format(subnet=subnet)
            for cmd in tmpl
        ]

class MikrotikHandler(DeviceHandler):
    """
    Mikrotik_routeros handler
    """

    # ----------------- analyze -----------------

    def analyze(self, show_data: dict) -> dict:
        parsed = {}

        for output in show_data.get("subnet", []):
            data = self.parse_subnet(output)
            if data:
                parsed.update(data)
                
        return parsed

    # ----------------- parsers -----------------

    def parse_subnet(self, output: str) -> dict:
        m = re.search(r"\d+\.\d+\.\d+\.\d+", output)
        if not m:
            return {}

        ip = m.group()

        if self.args.get("coro"):
            return {"subnet": ".".join(ip.split(".")[:3])}

        return {}

    # ----------------- config generation -----------------

    def build_config(self, parsed: dict) -> list[str]:
        if self.args.get("force"):
            return self.commands.get("config_template", [])

        if self.is_vty_mode():
            return self.build_vty_config(parsed)
        return self.build_subnet_config(parsed)


    def build_subnet_config(self, parsed: dict) -> list[str]:
        subnet = parsed.get("subnet")
        if not subnet:
            return []

        tmpl = self.commands.get("config_template", [])
        
        return [cmd.format(subnet=subnet) for cmd in tmpl]

HANDLERS = {
    "cisco_ios": CiscoHandler,
    "huawei": HuaweiHandler,
    "mikrotik_routeros": MikrotikHandler, 
}

class SSHConnection:
    def __init__(
        self,
        devices: list,
        platform: str,
        commands: dict,
        args: dict,
        max_workers: int = 10,
    ):
        self.devices = devices
        self.platform = platform
        self.commands = commands
        self.args = args
        self.max_workers = max_workers
        self.results: dict[str, dict] = {}
        self.console = Console()

    def run(self) -> dict:
        total_start = time.time()
        handler_class = HANDLERS.get(self.platform, DeviceHandler)

        q: Queue = Queue()
        for d in self.devices:
            q.put(d)

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            TextColumn("  [cyan]{task.fields[host]}", justify="left"),
            TextColumn("  [yellow]{task.fields[stage]}", justify="left"),
            TextColumn("  [green]{task.fields[result]}", justify="left"),
            console=self.console,
            transient=False,
            refresh_per_second=12,
        ) as progress:

            reporter = ProgressReporter(progress)

            summary_task = progress.add_task(
                description=f"{self.platform} total",
                total=len(self.devices),
                host="",
                stage="",
                result="",
            )

            worker_task_ids = []
            for i in range(self.max_workers):
                tid = progress.add_task(
                    description=f"worker-{i+1}",
                    total=5,        
                    host="-",
                    stage="idle",
                    result="",
                )
                worker_task_ids.append(tid)

            def worker_loop(worker_index: int, worker_task_id: int):
                while True:
                    try:
                        device = q.get_nowait()
                    except Empty:
                        reporter.idle(worker_task_id)
                        return

                    host = device.get("host", "?")
                    reporter.start_host(worker_task_id, host)

                    try:
                        handler = handler_class(
                            device,
                            self.commands,
                            self.args,
                            reporter=reporter,
                            task_id=worker_task_id,
                        )
                        result = handler.process()
                    except Exception:
                        logging.exception(f"{host} | UNHANDLED WORKER ERROR")
                        result = {"host": host, "status": "worker_error"}

                    self.results[host] = result
                    reporter.done(worker_task_id, result.get("status", "unknown"))
                    progress.advance(summary_task, 1)
                    q.task_done()

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = [
                    executor.submit(worker_loop, i, worker_task_ids[i])
                    for i in range(self.max_workers)
                ]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception:
                        logging.exception("WORKER THREAD CRASHED")
        logging.info(
            f"Processed {len(self.devices)} {self.platform} devices in "
            f"{time.time() - total_start:.1f}s"
        )
        return self.results


    def _process_device(self, device: dict, handler_class, reporter: ProgressReporter, task_id: int):
        handler = handler_class(
            device,
            self.commands,
            self.args,
            reporter=reporter,
            task_id=task_id,
        )
        return handler.process()


# === ХЕЛПЕР ДЛЯ СОЗДАНИЯ УСТРОЙСТВ ===
def make_device(ip, platform, username, password):
    return {
        "device_type": platform,
        "host": ip,
        "username": username,
        "password": password,
        "ssh_config_file": "~/.ssh_config",
        "session_log": "netmiko_session.log",
        "read_timeout_override": 90,
        "fast_cli": False,
        'global_cmd_verify': True,
        'use_keys': False,
        'global_delay_factor': 2
    }


def azs_id_from_hostname(hostname: str) -> int:
    if not hostname:
        return None
    if ("COCO" in hostname) or ("DOCO" in hostname):
        m = re.search(r"\b(?:AZS|KAAZS)-(\d{3}).*-(\d{3})-(?:COCO|DOCO|)\b", hostname)
        if not m:
            return None
        return int(m.group(2) + m.group(1))
    elif ("COPO" in hostname) or ("CODO" in hostname):
        m = re.search(r"^[A-Z]+-[A-Z]+-(\d{3})-[A-Z]+-(\d{3})-[A-Z]+$", hostname)
        if not m:
            return None
        return int(m.group(1) + m.group(2))
    elif ("CORO" in hostname):
        m = re.search(r"^[A-Z]+-[A-Z]+-(\d{3})-[A-Z]+-(\d{3})-[A-Z]+$", hostname)
        if not m:
            return None
        return int(m.group(1) + m.group(2))


def load_ip_list(path: str) -> set[str]:
    ips: set[str] = set()
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s:
                ips.add(s)
    return ips


def load_azs_allowlist(path: str) -> set[int]:
    allow: set[int] = set()
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.isdigit():
                allow.add(int(s))
    return allow


def load_hosts_from_netbox(args: dict) -> dict:
    arg_parser = ArgumentParser()
    filters = arg_parser.get_filters(args)

    NETBOX_URL = os.getenv("NETBOX_URL")
    NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")

    if not NETBOX_URL or not NETBOX_TOKEN:
        raise RuntimeError("NETBOX_URL or NETBOX_TOKEN is not set in .env")

    nb = Netbox(NETBOX_URL, NETBOX_TOKEN)

    ip_allow = load_ip_list(args["hostsfile"]) if args.get("hostsfile") else None
    azs_allow = load_azs_allowlist(args["hostsnumbers"]) if args.get("hostsnumbers") else None

    devices = nb.get_devices(**filters)
    hosts_dict = {}
    for device in devices:
        try:
            d = dict(device)
            platform = d["platform"]["display"]
            ip = d["primary_ip"]["address"].split("/")[0]
            hostname = d.get("name") or d.get("display") or ""
            #print(hostname)
        except (TypeError, KeyError):
            continue

        if ip_allow is not None and ip not in ip_allow:
            continue

        if azs_allow is not None:
            azs_id = azs_id_from_hostname(hostname)
            if azs_id is None or azs_id not in azs_allow:
                continue

        if platform == "cisco_ios":
            hosts_dict[ip] = "cisco_ios"
        elif platform == "huawei_vrp":
            hosts_dict[ip] = "huawei"
        elif platform == "mikrotik_routeros":
            hosts_dict[ip] = "mikrotik_routeros"

    return hosts_dict

def build_device_lists(hosts_dict: dict, args: dict, password: str):
    cisco_devices = []
    huawei_devices = []
    mikrotik_devices = []

    for ip, platform in hosts_dict.items():
        dev = make_device(ip, platform, username, password)
        if platform == "cisco_ios":
            cisco_devices.append(dev)
        elif platform == "huawei":
            huawei_devices.append(dev)
        elif platform == "mikrotik_routeros":
            mikrotik_devices.append(dev)


    return cisco_devices, huawei_devices, mikrotik_devices


def run_for_platform(platform: str, devices: list, args: dict):
    if not devices:
        return
    ssh_conn = SSHConnection(devices, platform, COMMANDS[platform], args)
    ssh_conn.run()


if __name__ == "__main__":
    password = None

    arg_parser = ArgumentParser()
    args = arg_parser.parse()
    args["test"] = args.get("test", False)
    args["vty"] = args.get("vty", False)
    args["force"] = args.get("force", False)


    if not (args["force"] and args["test"]):
        username = input("Username: ").strip()
        password = getpass("Password: ")

    hosts_dict = load_hosts_from_netbox(args)

    cisco_devices, huawei_devices, mikrotik_devices = build_device_lists(hosts_dict, args, password)

    run_for_platform("cisco_ios", cisco_devices, args)
    run_for_platform("huawei", huawei_devices, args)
    run_for_platform("mikrotik_routeros", mikrotik_devices, args)