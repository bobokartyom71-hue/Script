import pynetbox
import argparse
import logging


class Netbox:
    def __init__(self, hostname, token, verify_ssl=True):
        self.hostname = hostname
        self.token = token
        try:
            self.nb = pynetbox.api(hostname, token)
            self.nb.http_session.verify = False
        except Exception as e:
            logging.error(f"Ошибка подключения к NetBox: {e}")
            raise

    def get_devices(self, **kwargs):
        try:
            devices = self.nb.dcim.devices.filter(**kwargs)
            return devices  # сразу список, а не generator-like object
        except Exception as e:
            logging.error(f"Ошибка при запросе устройств: {e}")
            return []


class ArgumentParser:
    REGIONS = {
        "BY": "51",
        "KG": "55",
        "KZ": "49",
        "RU": "74",
        "TJ": "56"
    }

    SITE_GROUPS = {
        "COCO": "6",
        "COPO": ["2","3"],
        #"CODO": "3",
        "CORO": "4",
        "PNPO": "13",
        "NEFT": "9",
        "OFIS": "8",
    }

    def __init__(self):
        self.parser = argparse.ArgumentParser(
            description="Фильтрация устройств из NetBox"
        )

        # Сайты
        self.parser.add_argument("-c", "--coco", help="Сервисные АЗC COCO", action="store_const", const="COCO")
        self.parser.add_argument("-p", "--copo", help="Партнерские АЗC COPO и CODO", action="store_const", const="COPO")
        #self.parser.add_argument("-d", "--codo", help="Партнерские АЗC CODO", action="store_const", const="CODO")
        self.parser.add_argument("-r", "--coro", help="Партнерские АЗC CORO", action="store_const", const="CORO")
        self.parser.add_argument("-P", "--PNPO", help="Объекты ПНПО", action="store_const", const="PNPO")
        self.parser.add_argument("-n", "--neft", help="Нефтебазы", action="store_const", const="NEFT")
        self.parser.add_argument("-o", "--offices", help="Офисы", action="store_const", const="OFFIS")

        # Регионы
        self.parser.add_argument("-ru", "--RU", help="Регион РФ", action="store_const", const="RU")
        self.parser.add_argument("-kz", "--KZ", help="Регион Казахстан", action="store_const", const="KZ")
        self.parser.add_argument("-kg", "--KG", help="Регион Киргизия", action="store_const", const="KG")
        self.parser.add_argument("-by", "--BY", help="Регион Беларусь", action="store_const", const="BY")
        self.parser.add_argument("-tj", "--TJ", help="Регион Таджикистан", action="store_const", const="TJ")

        # Файл
        self.parser.add_argument("-hf", "--hostsfile", help="Использовать файл с IP хостов", action="store_const", const="hostsfile")
        self.parser.add_argument("-hn", "--hostsnumbers", help="Использовать файл с номерами хостов", action="store_const", const="hostsnumbers")

        # Роли
        self.parser.add_argument("-role", "--role", help="ID роли устройства", type=int, default=1)
        
        self.parser.add_argument("-f", "--force", action="store_true", help="В режиме force будет игнорироваться часть show, срипт будет сразу применять часть config")

        self.parser.add_argument("-vty", "--vty", help="Запуск скрипта для добавления IP в VTY ACL", action="store_true")
        self.parser.add_argument("-t", "--test", help="Тестовый запуск (вывод команд в консоль, без подключения)", action="store_true")

    def parse(self):
        args = self.parser.parse_args()
        return vars(args)

    def get_filters(self, args_dict):
        regions, sites = [], []
        role_id = args_dict.get("role", 1)

        for key, value in args_dict.items():
            if key.upper() in self.SITE_GROUPS and value:
                sites.append(self.SITE_GROUPS[value])
            elif key.upper() in self.REGIONS and value:
                regions.append(self.REGIONS[value])
        return {
            "region_id": regions or None,
            "site_group_id": sites or None,
            "role_id": role_id,
            "status": "active"
        }