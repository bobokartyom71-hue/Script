from __future__ import annotations

import logging
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Tuple, Optional
import secrets
import string
import smtplib
from email.message import EmailMessage
from getpass import getpass
from ipaddress import IPv4Address
import openpyxl
from jinja2 import Environment, FileSystemLoader
import pyzipper
from netmiko import (
    ConnectHandler,
    NetmikoTimeoutException,
    NetmikoAuthenticationException,
    file_transfer,
)

# -------------------- Константы/настройки --------------------
BASE_DIR = Path.cwd()
ADDR_DIR = BASE_DIR / "address_plan"
TEMPL_DIR = BASE_DIR / "config_templates"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gazprom-neft.local")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))

ALLOWED_MODELS = {"1111", "1921", "2911", "4221", "AR651"}

# region_code: (region_tag, AS, SYSLOG, excel_sheet)
REGIONS: Dict[str, Tuple[str, str, str, str]] = {
    "010": ("SPB", "65049", "10.62.249.113", "SZ"),
    "011": ("MSK", "65001", "10.62.249.113", "CENTR"),
    "012": ("MSK", "65001", "10.62.249.113", "CENTR"),
    "013": ("YAR", "65013", "10.62.249.113", "YAR"),
    "014": ("NN",  "65124", "10.62.249.113", "CENTR"),
    "015": ("KRD", "65088", "10.62.249.113", "SOUTH"),
    "020": ("EKB", "65004", "10.55.242.164", "URAL"),
    "021": ("TMN", "65006", "10.55.242.164", "TMN"),
    "022": ("CHL", "65045", "10.55.242.164", "CHL"),
    "024": ("TMN", "65006", "10.55.242.164", "TMN"),
    "025": ("EKB", "65004", "10.55.242.164", "URAL"),
    "030": ("NSK", "65008", "10.55.242.164", "NSK"),
    "031": ("OMS", "65003", "10.55.242.164", "OMS"),
    "032": ("BRN", "65008", "10.55.242.164", "NSK"),
    "033": ("KMR", "65012", "10.55.242.164", "KUZBASS"),
    "034": ("KYA", "65012", "10.55.242.164", "KUZBASS"),
}

EXCEL_FILES = {
    "SMARTSAFE": "SmartSafe.xlsx",
    "KKM": "KKM.xlsx",
    "WIFI": "WIFI.xlsx",
    "TPK": "TPK.xlsx",
    "LOOPBACK": "LOOPBACK.xlsx",
    "COFFEE": "COFFEE.xlsx",
    "BGP1": "BGP1.xlsx",
    "BGP2": "BGP2.xlsx",
}

logging.getLogger("paramiko").setLevel(logging.WARNING)
logging.basicConfig(
    format="%(threadName)s %(name)s %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("coco")

# -------------------- Хелперы --------------------
def ip_plus(ip: str, delta: int) -> str:
    return str(IPv4Address(int(IPv4Address(ip)) + delta))

def z3(n: int) -> str:
    return str(n).zfill(3)

def guess_username(login: str) -> str:
    # берём фрагмент после '-' или '_' если есть
    if "-" in login:
        return login.split("-")[1].lower()
    if "_" in login:
        return login.split("_")[1].lower()
    return login

def strong_password(length: int = 12, alphabet: Optional[str] = None) -> str:
    alphabet = alphabet or (string.ascii_letters + string.digits)
    return "".join(secrets.choice(alphabet) for _ in range(length))

def load_wb(name: str):
    path = ADDR_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Нет Excel: {path}")
    return openpyxl.load_workbook(path)

def get_cell_val(wb, sheet: str, row: int, col: str = "A") -> str:
    ws = wb[sheet]
    return str(ws[f"{col}{row}"].value).strip()

def make_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPL_DIR)))

# -------------------- Доменная логика --------------------
@dataclass(frozen=True)
class RegionInfo:
    code: str        # "010"
    tag: str         # "SPB"
    asn: str         # "65049"
    syslog: str      # "10.62.249.113"
    sheet: str       # "SZ"

def validate_model(raw: str) -> str:
    m = raw.strip().upper()
    if m not in ALLOWED_MODELS:
        raise ValueError(f"Неизвестная модель: {m}")
    return m

def validate_region(raw: str) -> RegionInfo:
    r = raw.strip()
    if r not in REGIONS:
        raise ValueError(f"Неверный регион: {r}")
    tag, asn, syslog, sheet = REGIONS[r]
    return RegionInfo(code=r, tag=tag, asn=asn, syslog=syslog, sheet=sheet)

def validate_azs(raw: str, region_tag: str) -> int:
    try:
        n = int(raw)
    except ValueError:
        raise ValueError("AZS number должен быть целым числом.")

    ok = (
        (1 <= n <= 512 and region_tag not in {"TMN", "CHL"})
        or (1 <= n <= 1024 and region_tag in {"MSK", "EKB"})
        or (301 <= n <= 812 and region_tag == "TMN")
        or (201 <= n <= 712 and region_tag == "CHL")
    )
    if not ok:
        raise ValueError("Неверный диапазон AZS number для выбранного региона.")
    return n

def derive_networks(
    azs_row: int,
    sheet: str,
    books: Dict[str, any]
) -> Dict[str, str]:
    # читаем адреса
    kkm_gw = get_cell_val(books["KKM"], sheet, azs_row)
    safe_gw = get_cell_val(books["SMARTSAFE"], sheet, azs_row)
    wifi_gw = get_cell_val(books["WIFI"], sheet, azs_row)
    tpk_net = get_cell_val(books["TPK"], sheet, azs_row)
    loopback_src = get_cell_val(books["LOOPBACK"], sheet, azs_row)
    coffee_net = get_cell_val(books["COFFEE"], sheet, azs_row)
    bgp1_ip = get_cell_val(books["BGP1"], sheet, azs_row)
    bgp1_gw = get_cell_val(books["BGP1"], sheet, azs_row).replace(bgp1_ip, "").strip() or get_cell_val(books["BGP1"], sheet, azs_row+0)  # на случай 2 колонки
    bgp2_ip = get_cell_val(books["BGP2"], sheet, azs_row)
    bgp2_gw = get_cell_val(books["BGP2"], sheet, azs_row).replace(bgp2_ip, "").strip() or get_cell_val(books["BGP2"], sheet, azs_row+0)

    # сети (ровно как в исходнике, но безопаснее)
    kkm_net  = ip_plus(kkm_gw, -1)
    safe_net = ip_plus(safe_gw, -1)
    wifi_net = ip_plus(wifi_gw, -1)

    tpk_gw = ip_plus(tpk_net, +1)

    loopback = ip_plus(loopback_src, +44)

    coffee_gw = ip_plus(coffee_net, +1)

    # Производные от bgp1_ip
    octs = bgp1_ip.split(".")
    third = octs[2]
    last = int(octs[3])

    sf220_net = f"10.246.{third}.{last-1}"
    sf220_ip  = f"10.246.{third}.{last}"
    sf220_gw  = f"10.246.{third}.{last+1}"

    csp_net = f"10.249.{third}.{last-1}"
    csp_ip  = f"10.249.{third}.{last}"
    csp_gw  = f"10.249.{third}.{last+1}"

    return {
        "SAFE_NET": safe_net, "SAFE_GW": safe_gw,
        "KKM_NET": kkm_net,   "KKM_GW": kkm_gw,
        "WIFI_NET": wifi_net, "WIFI_GW": wifi_gw,
        "TPK_NET": tpk_net,   "TPK_GW": tpk_gw,
        "LOOPBACK": loopback,
        "COFFEE_NET": coffee_net, "COFFEE_GW": coffee_gw,
        "BGP1_IP": bgp1_ip, "BGP1_GW": bgp1_gw,
        "BGP2_IP": bgp2_ip, "BGP2_GW": bgp2_gw,
        "SF220_NET": sf220_net, "SF220_IP": sf220_ip, "SF220_gw": sf220_gw,
        "CSP_NET": csp_net, "CSP_IP": csp_ip, "CSP_GW": csp_gw,
    }

def render_config(model: str, ctx: Dict[str, str]) -> str:
    env = make_env()
    templ = env.get_template(f"JINJA_COCO_{model}.txt")
    return templ.render(ctx)

def create_zip(src: Path, dst_zip: Path, password: str) -> None:
    with pyzipper.AESZipFile(dst_zip, "w",
                             compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(password.encode())
        zf.write(src, arcname=src.name)

def send_email(sender: str, recipient: str, subject: str, body: str, attach: Optional[Path]) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body or "CONFIG")

    if attach:
        data = attach.read_bytes()
        msg.add_attachment(data, maintype="application", subtype="zip", filename=attach.name)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
        s.send_message(msg)

def scp_to_device(host: str, username: str, password: str, local_path: Path, remote_path: str) -> None:
    device = {
        "device_type": "cisco_ios",
        "host": host,
        "username": username,
        "password": password,
        "ssh_config_file": "~/.ssh_config",
        "session_log": "netmiko_session.log",
        "read_timeout_override": 90,
    }
    try:
        with ConnectHandler(**device) as ssh:
            ssh.send_config_set(["ip scp server enable"])
            file_transfer(
                ssh,
                source_file=str(local_path),
                dest_file=remote_path,
                file_system="flash:",
                direction="put",
            )
        logger.info("Файл успешно скопирован на устройство %s", host)
    except (NetmikoTimeoutException, NetmikoAuthenticationException) as e:
        logger.error("SCP ошибка: %s", e)
        raise

# -------------------- CLI-интерактив --------------------
def main():
    # Ввод модели
    while True:
        try:
            model = validate_model(input("Введите модель устройства (1111, 1921, 2911, 4221, AR651): "))
            break
        except ValueError as e:
            logger.warning(str(e))

    # Ввод региона
    while True:
        try:
            region = validate_region(input("Введите номер региона (формат 0ХХ): "))
            break
        except ValueError as e:
            logger.warning(str(e))

    # Ввод АЗС
    while True:
        try:
            azs = validate_azs(input("AZS number: "), region.tag)
            break
        except ValueError as e:
            logger.warning(str(e))

    azs_str = z3(azs)

    # Готовим книги
    books = {k: load_wb(v) for k, v in EXCEL_FILES.items()}

    nets = derive_networks(azs, region.sheet, books)

    # Контекст для шаблона
    ctx = {
        "AZS_NUMBER": azs_str,
        "REGION": region.tag,
        "REGION_NUMBER": region.code,
        "AS": region.asn,
        "SYSLOG": region.syslog,
        **nets,
    }

    cfg_text = render_config(model, ctx)

    # Имя файла
    base_name = f"AZS-{azs_str}-RU-{region.code}-BR01-{region.tag}-COCO"
    cfg_path = BASE_DIR / f"{base_name}.txt"
    cfg_path.write_text(cfg_text, encoding="utf-8")
    logger.info("Файл конфигурации создан: %s", cfg_path)

    # Что делать дальше?
    while True:
        dest = input("Введите путь назначения конфигурации (m - mail, d - device): ").strip().lower()
        if dest == "m":
            recipient = input("Введите почту: ").strip()
            pwd = strong_password(12)
            zip_path = BASE_DIR / f"{base_name}.zip"
            create_zip(cfg_path, zip_path, pwd)
            send_email(sender=os.getenv("SENDER_EMAIL", "mail@mail.local"),
                       recipient=recipient,
                       subject=f"Конфигурация устройства {cfg_path.name}",
                       body="CONFIG",
                       attach=zip_path)
            logger.info("Письмо отправлено. Пароль к архиву: %s", pwd)
            break
        elif dest == "d":
            device_ip = input("Введите IP устройства: ").strip()
            user = guess_username(os.getlogin())
            chk = input(f"Логин {user}? (y/n): ").strip().lower()
            if chk == "n":
                user = input("Введите SSH логин: ").strip()
            password = getpass("Введите SSH пароль: ")
            scp_to_device(device_ip, user, password, cfg_path, cfg_path.name)
            break
        else:
            logger.warning("Путь назначения не определён.")

if __name__ == "__main__":
    main()
