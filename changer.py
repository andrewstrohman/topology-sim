#!/usr/bin/env python3

from os import listdir
from subprocess import (Popen, PIPE)
import json
import sys, os

IP = "/sbin/ip"
BRCTL = "/usr/sbin/brctl"
IW = "/usr/sbin/iw"
EBTABLES = "/usr/sbin/ebtables"
BRIDGE = "/usr/sbin/bridge"

def exec_cmd(command):
    #print(command)
    return os.popen(" ".join(command)).read().strip()

def get_ifnames_by_type(if_type):
    for interface in json.loads(exec_cmd([IP, "-j", "link", "show", "type", if_type])):
        if interface:
            yield interface["ifname"]

def get_bridge_vlan():
    return json.loads(exec_cmd([BRIDGE, "-j", "vlan"]))

def remove_vlan_filter(interface, vid):
    exec_cmd([BRIDGE, "vlan", "del", "dev", interface, "vid", str(vid)])

def remove_vlan_filter_self(interface, vid):
    exec_cmd([BRIDGE, "vlan", "del", "dev", interface, "self", "vid", str(vid)])

def clean_wan_bridge_vlans(config):
    curr = get_bridge_vlan()
    for port_info in curr:
        if port_info["ifname"] == config["wan_bridge"]["name"]:
            for vlan_info in port_info["vlans"]:
                if vlan_info["vlan"] != 1:
                    remove_vlan_filter_self(port_info["ifname"], vlan_info["vlan"])

def add_bridge_rule():
    if '-i gretap+ -o gretap+ -j DROP' not in exec_cmd([EBTABLES, "-L", "FORWARD", "-t", "filter"]):
        exec_cmd([EBTABLES, "-A", "FORWARD", "-i", "gretap+", "-o", "gretap+", "-j", "DROP"])

def del_interface(if_name):
    exec_cmd([IP, "link", "del", if_name])

def del_namespace(name):
    exec_cmd([IP, "netns", "del", name])

def add_namespace(name):
    exec_cmd([IP, "netns", "add", name])

def list_namespaces():
    try:
        namespaces = json.loads(exec_cmd([IP, "-j", "netns"]))
        return [x["name"] for x in namespaces]
    except:
        return {}

def add_bridge(name):
    if name not in get_ifnames_by_type("bridge"):
        exec_cmd([BRCTL, "addbr", name])
    exec_cmd([IP, "link", "set", "dev", name, "up"])
    enable_vlan_filtering(name)

def enable_vlan_filtering(bridge):
    exec_cmd([IP, "link", "set", "dev", bridge, "type", "bridge", "vlan_filtering", "1"])

def bridge_members(name):
    for entry in listdir(f"/sys/devices/virtual/net/{name}"):
        if entry.startswith("lower_"):
            yield entry[len("lower_"):] 

def del_bridge(name, config):
    if name == config["wan_bridge"]["name"]:
        for member in bridge_members(name):
            if member not in config["wan_bridge"]["members"]:
                del_bridge_if(name, member)
    else:
        exec_cmd([IP, "link", "set", "dev", name, "down"])
        exec_cmd([BRCTL, "delbr", name])

def add_bridge_if(bridge, member):
    if member in bridge_members(bridge):
        return
    exec_cmd([IP, "link", "set", "dev", member, "up", "mtu", "1500"])
    exec_cmd([BRCTL, "addif", bridge, member])

def del_bridge_if(bridge, member):
    exec_cmd([BRCTL, "delif", bridge, member])

def add_tunnel(name, local, remote, key):
    exec_cmd([IP, "link", "add", name, "type", "gretap", "local", local, "remote", remote, "key", key, "nopmtudisc", "ignore-df"])

def add_veth(first, second):
    exec_cmd([IP, "link", "add", first, "type", "veth", "peer", "name", second])

def add_vlan(interface, vid):
    # ip link add link eth0 name eth0.100 type vlan id 100
    if_name = f"{interface}.{vid}"
    exec_cmd([IP, "link", "add", "link", interface, "name", if_name, "type", "vlan", "id", str(vid)])
    return if_name

def allow_vlan_trunk(interface, vid):
    exec_cmd([BRIDGE, "vlan", "add", "dev", interface, "vid", str(vid)])

def allow_vlan_trunk_self(interface, vid):
    exec_cmd([BRIDGE, "vlan", "add", "dev", interface, "self", "vid", str(vid)])

def set_pvid(interface, vid):
    exec_cmd([BRIDGE, "vlan", "add", "dev", interface, "vid", str(vid), "pvid", "untagged"])

def move_phy_to_namespace(phy, ns):
    exec_cmd([IW, "phy", phy, "set", "netns", "name", ns])


def move_eth_to_namespace(dev, ns):
    exec_cmd([IP, "link", "set", dev, "netns", ns])

config = json.loads(sys.stdin.read())

clean_wan_bridge_vlans(config)

for interface in get_ifnames_by_type("gretap"):
    del_interface(interface)

for interface in get_ifnames_by_type("veth"):
    del_interface(interface)

for interface in get_ifnames_by_type("vlan"):
    del_interface(interface)

for interface in get_ifnames_by_type("bridge"):
    del_bridge(interface, config);

for namespace in list_namespaces():
    del_namespace(namespace)


add_bridge_rule()

for namespace, ns_info in config["namespaces"].items():
    add_namespace(namespace)

for tunnel, ti in config["tunnels"].items():
    add_tunnel(tunnel, ti["local"], ti["remote"], str(ti["key"]))

for first, second in config["veth_pairs"].items():
    add_veth(first, second)

wan = config["wan_bridge"]["name"]
enable_vlan_filtering(wan)

for bridge, bridge_info in config["bridges"].items():
    physical_members = bridge_info['physical_members']
    virtual_members = bridge_info['virtual_members']
    vid = bridge_info['vid']
    add_bridge(bridge)
    if bridge != wan:
        for trunk_port in config["trunk_ports"]:
            allow_vlan_trunk(trunk_port, vid)
        allow_vlan_trunk_self(wan, vid)
        vlan_if_name = add_vlan(wan, vid)
        add_bridge_if(bridge, vlan_if_name)

    for member in physical_members:
        add_bridge_if(wan, member)
        remove_vlan_filter(member, 1)
        set_pvid(member, vid)
    for member in virtual_members:
        add_bridge_if(bridge, member)

for namespace, ns_info in config["namespaces"].items():
    if ns_info["client_type"] == "wireless":
        move_phy_to_namespace(ns_info["phy"], namespace)
    elif ns_info["client_type"] == "wired":
        move_eth_to_namespace(ns_info["port"], namespace)
