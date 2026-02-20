COMMANDS = {
    "cisco_ios": {
        "show_subnet": [ "sh ip int brief | in 172\.(.*)\.(.*)\.[1-1]_"
            #"show ip arp vrf KKM"
        ],

        "config_template": [
            "ip access-list extended ACL_ADM-to-INET",
            "permit ip {subnet}.112 0.0.0.7 host 91.185.92.163"
        ],
        
        "show_vty": [
            "show running-config | sec line vty",
            "show running-config | i ip access-list"
        ],

        "config_template_standard": [
            "ip access-list standard {acl_name}",
            "permit 20.20.20.20",
        ],

        "config_template_extended": [
            "ip access-list extended {acl_name}",
            "permit ip host 20.20.20.20 any"
        ],
    },

    "huawei": {
        "show_subnet": [
            "display arp vpn-instance KKM"
        ],

        "config_template": [
            "acl name TEST 3500",
            "rule permit tcp source {subnet} 0.0.0.7 destination 185.71.67.137 0 destination-port eq www",
            "rule permit tcp source {subnet} 0.0.0.7 destination 92.53.68.16 0 destination-port eq www",
            "rule permit tcp source {subnet} 0.0.0.7 destination 92.53.68.17 0 destination-port eq www"
        ],

        "show_vty": [
            "display current-configuration | section user-interface vty",
            "display current-configuration section acl | in name"
        ],

        "config_template_advanced": [
            "acl name {acl_name} {acl_number}",
            "rule permit ip source 20.20.20.20 0 destination any"
        ],
    },

    "mikrotik_routeros": {

        "show_subnet": [
            'ip/address/print where comment~"MGMT"'
        ],
        
        "config_template": [
            "rule {subnet}.15"
        ]
    }
}