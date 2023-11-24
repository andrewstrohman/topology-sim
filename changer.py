#!/usr/bin/env python3


"""
Runs on pod, in order to construct L2 segments
"""

from os import listdir
import json
import sys
import os

IP = "/sbin/ip"
BRCTL = "/usr/sbin/brctl"
IW = "/usr/sbin/iw"
EBTABLES = "/usr/sbin/ebtables"
BRIDGE = "/usr/sbin/bridge"


def exec_cmd(command):
    """
    Executes a command and returns the command's stdout
    """
    return os.popen(" ".join(command)).read().strip()


def get_ifnames_by_type(if_type):
    """
    Returns an iterable of interface names of the specified type
    """
    for interface_info in json.loads(
            exec_cmd([IP, "-j", "link", "show", "type", if_type])):
        if interface_info:
            yield interface_info["ifname"]


def get_bridge_vlan_filtering_info():
    """
    Returns vlan filtering configuration
    """
    return json.loads(exec_cmd([BRIDGE, "-j", "vlan"]))


def remove_vlan_filter(net_interface, vlan_id):
    """
    Removes vid from vlan filter for specified interface
    """
    exec_cmd([BRIDGE, "vlan", "del", "dev", net_interface, "vid", str(vlan_id)])


def remove_vlan_filter_self(net_interface, vlan_id):
    """
    Removes vid from vlan filter for specified bridge interface (CPU port)
    """
    exec_cmd([BRIDGE, "vlan", "del", "dev", net_interface, "self", "vid", str(vlan_id)])


def clean_wan_bridge_vlans(conf):
    """
    Removes all vlans, except VID 1, which is PVID, Egress Untagged,
    from the filter of the wan bridge.
    This is usefull because we cannot delete the wan bridge, as
    connectivity to this pod or another pod at the site would be severed.
    So unlike non-wan bridges, where the bridge is destroyed
    during the initial phase of cleaning state before setting
    a new config, the best we can do is restrict traffic that
    isn't untagged in order to get back to the boot-up, clean state
    """
    curr = get_bridge_vlan_filtering_info()
    for port_info in curr:
        if port_info["ifname"] == conf["wan_bridge"]["name"]:
            for vlan_info in port_info["vlans"]:
                if vlan_info["vlan"] != 1:
                    # TODO: this _self variant assumes that the wan bridge
                    # is a physical device, so this probably
                    # won't work when the wan bridge is software-only
                    # ie. a single pod site with no Internet connection
                    remove_vlan_filter_self(
                        port_info["ifname"], vlan_info["vlan"])
            break


def prohibit_gre_forwarding(conf):
    """
    For all sites involved in a layer 2 segment, we have a full mesh
    of VPN tunnels. When there are more than 2 sites, this there would
    be a loop without this
    """
    if not conf["tunnels"]:
        return
    if '-i gretap+ -o gretap+ -j DROP' not in exec_cmd(
            [EBTABLES, "-L", "FORWARD", "-t", "filter"]):
        exec_cmd([EBTABLES, "-A", "FORWARD", "-i",
                 "gretap+", "-o", "gretap+", "-j", "DROP"])


def del_interface(if_name):
    """Delete a virtual netdev specified by if_name"""
    exec_cmd([IP, "link", "del", if_name])


def del_namespace(name):
    """Delete a namespace specified by parameter 'name'"""
    # work around: move any phys into the default namespace
    # or else the phys will be "lost" when the ns is deleted
    # This still works on when the pod is just a switch.
    # iw doesn't exist, but neither will wireless phys
    list_out = exec_cmd([IP, "netns", "exec", name, "iw", "list"])
    for line in list_out.split("\n"):
        if line.startswith("Wiphy"):
            exec_cmd([IP, "netns", "exec", name, "iw", 'phy', line.split()[1],
                      "set", "netns", "1"])
    exec_cmd([IP, "netns", "del", name])


def add_namespace(name):
    """Add a namespace specified by parameter 'name'"""
    exec_cmd([IP, "netns", "add", name])


def get_namespaces():
    """returns a dict of active namespaces keyed by name"""
    namespaces = exec_cmd([IP, "-j", "netns"])
    if not namespaces:
        namespaces = "{}"

    deserialized_namespaces = json.loads(namespaces)
    return [x["name"] for x in deserialized_namespaces]


def add_bridge(name):
    """
    Creates a bridge if it does not already exist.
    Enables vlan filtering and brings the bridge up,
    regardless if it pre-existed or not.
    """
    if name not in get_ifnames_by_type("bridge"):
        exec_cmd([BRCTL, "addbr", name])
    exec_cmd([IP, "link", "set", "dev", name, "up"])
    enable_vlan_filtering(name)


def enable_vlan_filtering(bridge_interface):
    """
    enables vlan filtering for a specified bridge
    """
    exec_cmd([IP, "link", "set", "dev", bridge_interface,
             "type", "bridge", "vlan_filtering", "1"])


def bridge_members(bridge_name):
    """
    Returns an iterator of all members of a specified bridge
    """
    for entry in listdir(f"/sys/devices/virtual/net/{bridge_name}"):
        if entry.startswith("lower_"):
            yield entry[len("lower_"):]


def del_bridge(name, conf):
    """
    Deletes a bridge, unless it's a WAN bridge.
    For WAN bridges, just remove all members that
    do not provide connectivity to WAN
    """
    if name == conf["wan_bridge"]["name"]:
        for bridge_member in bridge_members(name):
            if bridge_member not in conf["wan_bridge"]["members"]:
                del_bridge_if(name, bridge_member)
    else:
        exec_cmd([IP, "link", "set", "dev", name, "down"])
        exec_cmd([BRCTL, "delbr", name])


def turn_off_learning(member_name):
    """
    Disable learning. Otherwise we won't handle roaming correctly
    """
    exec_cmd([BRIDGE, "link", "set", "dev", member_name, "learning", "off", "master"])

def add_bridge_if(bridge_name, member_name):
    """
    Adds a member to a bridge if it's not already apart
    """
    if member_name in bridge_members(bridge_name):
        return
    # This is for GRE tunnels. Otherwise we are have a MTU blackhole
    exec_cmd([IP, "link", "set", "dev", member_name, "up", "mtu", "1500"])
    exec_cmd([BRCTL, "addif", bridge_name, member_name])


def del_bridge_if(bridge_name, member_name):
    """Removes the specified member from a bridge"""
    exec_cmd([BRCTL, "delif", bridge_name, member_name])


def add_tunnel(name, local, remote, key):
    """
    Create a GRE tunnel.
    We use a key for the cases where there are multiple tunnels
    between the same two tunneling pods, ie. for seperate layer 2 segements
    nopmtudisc and ignore-df are used in order to provide 1500 MTU
    to the user. Otherwise, we will suffer from a MTU blackhole
    """
    exec_cmd([IP, "link", "add", name, "type", "gretap", "local",
             local, "remote", remote, "key", key, "nopmtudisc", "ignore-df"])


def add_veth(first_name, second_name):
    """
    Create a veth pair, named after the params.
    """
    exec_cmd([IP, "link", "add", first_name, "type",
             "veth", "peer", "name", second_name])


def add_vlan(interface_name, vlan_id):
    """Create a vlan interface"""
    # ip link add link eth0 name eth0.100 type vlan id 100
    if_name = f"{interface_name}.{vlan_id}"
    exec_cmd([IP, "link", "add", "link", interface_name, "name",
             if_name, "type", "vlan", "id", str(vlan_id)])
    return if_name


def allow_vlan_trunk(interface_name, vlan_id):
    """Allow a tagged vid for specified bridge member"""
    exec_cmd([BRIDGE, "vlan", "add", "dev", interface_name, "vid", str(vlan_id)])


def allow_vlan_trunk_self(interface_name, vlan_id):
    """Allow a tagged vid for hardware bridge (CPU port)"""
    exec_cmd([BRIDGE, "vlan", "add", "dev", interface_name, "self", "vid", str(vlan_id)])


def set_pvid(interface_name, vlan_id):
    """Set the PVID of a bridge member"""
    exec_cmd([BRIDGE, "vlan", "add", "dev", interface_name,
             "vid", str(vlan_id), "pvid", "untagged"])


def move_phy_to_namespace(phy, net_namespace):
    """Move a specified wireless phy to a specified network namespace"""
    exec_cmd([IW, "phy", phy, "set", "netns", "name", net_namespace])


def move_eth_to_namespace(netdev, net_namespace):
    """Move a netdev to a network namespace"""
    exec_cmd([IP, "link", "set", netdev, "netns", net_namespace])


def clean_configuration(conf):
    """Clean/remove current configuration"""

    # Special handling for the WAN bridge, so we don't lose connectivity
    clean_wan_bridge_vlans(conf)

    # Blow away all virtual interfaces and namespaces

    for interface in get_ifnames_by_type("gretap"):
        del_interface(interface)

    for interface in get_ifnames_by_type("veth"):
        del_interface(interface)

    for interface in get_ifnames_by_type("vlan"):
        del_interface(interface)

    for interface in get_ifnames_by_type("bridge"):
        del_bridge(interface, conf)

    for namespace in get_namespaces():
        del_namespace(namespace)


def create_interfaces(conf):
    """create all virtual interfaces that that are not bridges"""

    for tunnel, tunnel_info in conf["tunnels"].items():
        add_tunnel(tunnel, tunnel_info["local"], tunnel_info["remote"], str(tunnel_info["key"]))

    for first, second in conf["veth_pairs"].items():
        add_veth(first, second)


def main():
    """
    Configure L2 segements per config passed in via stdin
    """

    # serialize the config
    config = json.loads(sys.stdin.read())

    # clean up current config first
    clean_configuration(config)

    # Do not allow forwarding from one gre tunnel to another
    prohibit_gre_forwarding(config)

    wan = config["wan_bridge"]["name"]
    # Enable vlan filtering for the WAN bridge
    enable_vlan_filtering(wan)

    for namespace, ns_info in config["namespaces"].items():
        add_namespace(namespace)

    create_interfaces(config)

    for bridge, bridge_info in config["bridges"].items():
        physical_members = bridge_info['physical_members']
        virtual_members = bridge_info['virtual_members']
        vid = bridge_info['vid']
        add_bridge(bridge)
        if bridge != wan:
            for trunk_port in config["trunk_ports"]:
                allow_vlan_trunk(trunk_port, vid)
                turn_off_learning(trunk_port)
            allow_vlan_trunk_self(wan, vid)
            vlan_if_name = add_vlan(wan, vid)
            add_bridge_if(bridge, vlan_if_name)

        for member in physical_members:
            add_bridge_if(wan, member)
            remove_vlan_filter(member, 1)
            set_pvid(member, vid)
            turn_off_learning(member)
        for member in virtual_members:
            add_bridge_if(bridge, member)

    for namespace, ns_info in config["namespaces"].items():
        if ns_info["client_type"] == "wireless":
            move_phy_to_namespace(ns_info["phy"], namespace)
        elif ns_info["client_type"] == "wired":
            move_eth_to_namespace(ns_info["port"], namespace)


if __name__ == "__main__":
    main()
