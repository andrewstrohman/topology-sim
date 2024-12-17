"""
Centralized control plane
"""


import argparse
import copy
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import yaml

VETH_NUM = 0
TUNNEL_NUM = 1
NAMESPACE_TO_POD = {}


def get_config(config_path):
    """Read yaml config file"""
    with open(config_path, encoding="utf8") as file_handle:
        return yaml.safe_load(file_handle)


class InvalidDUT(Exception):
    """
    Raised when a DUT name is not found
    """


def serial_for_dut(dut_name, hardware):
    """
    Returns (pod host, tty) of the passed dut name
    if dut_name is not found, an exception is raised
    """
    for _, site_config in hardware["sites"].items():
        for _, pod_config in site_config["pods"].items():
            if "console" in pod_config:
                console_config = pod_config["console"]
                for id_type in ["serial", "tty"]:
                    if id_type in console_config:
                        for ID, id_info in console_config[id_type].items():
                            if id_info["dut_name"] == dut_name:
                                return pod_config["host"], id_type, ID
    raise InvalidDUT(dut_name)


class InvalidPod(Exception):
    """
    Raised when a pod name is not found
    """


def get_pod_site(pod, hardware):
    """
    Return the site where a pod lives
    """
    for site, site_config in hardware["sites"].items():
        for curr_pod in site_config["pods"]:
            if curr_pod == pod:
                return site
    raise InvalidPod(pod)


class InvalidMember(Exception):
    """
    Raised when a specified DUT/port is not in the hardware config
    """


def get_pod(member, hardware):
    """
    Returns the pod that connects to a DUT/port
    """
    for _, site_config in hardware["sites"].items():
        for pod, pod_config in site_config["pods"].items():
            for _, dev_info in pod_config["ethernet"].items():
                if member["dut_name"] == dev_info["dut_name"] and \
                        member["dut_port"] == dev_info["dut_port"]:
                    return pod
    raise InvalidMember("DUT:{member['dut_name']}, PORT:{member['dut_port']}")


def get_netdev(member, hardware):
    """
    Returns the pod's netdev that connects to a DUT/port
    or simulated wired client
    """
    if member["type"] == "dut":
        for _, site_config in hardware["sites"].items():
            for _, pod_config in site_config["pods"].items():
                for dev, dev_info in pod_config["ethernet"].items():
                    if member["dut_name"] == dev_info["dut_name"] and \
                            member["dut_port"] == dev_info["dut_port"]:
                        return dev
        raise InvalidMember("DUT:{member['dut_name']}, PORT:{member['dut_port']}")

    if member["type"] == "sim_wired_client":
        return f"veth{VETH_NUM}"

    raise TypeError(f"member type: {member['type']} unknown")


class TunnelConfig:  # pylint: disable=too-few-public-methods
    """
    Object that holds a tunnel configuration
    """
    def __init__(self, left_site, right_site, left_bridge_name, right_bridge_name):
        self.left_site = left_site
        self.right_site = right_site
        self.left_bridge_name = left_bridge_name
        self.right_bridge_name = right_bridge_name


def create_tunnel(tunnel_config, hardware, ret):
    """
    Create a GRE tunnel configurations for both endpoints.
    This entails the GRE tunnel configuration and
    including the gre interfaces in their correct bridge
    """
    global TUNNEL_NUM  # pylint: disable=global-statement
    left_site_info = hardware["sites"][tunnel_config.left_site]
    left_pod = left_site_info["tunneling_pod"]
    right_site_info = hardware["sites"][tunnel_config.right_site]
    right_pod = right_site_info["tunneling_pod"]
    ret[left_pod]["tunnels"][f"gretap{TUNNEL_NUM}"] = {
        "type": "gretap",
        "key": TUNNEL_NUM,
        "local": left_site_info["pods"][left_pod]["host"],
        "remote": right_site_info["pods"][right_pod]["host"],
    }

    ret[right_pod]["tunnels"][f"gretap{TUNNEL_NUM}"] = {
        "type": "gretap",
        "key": TUNNEL_NUM,
        "local": right_site_info["pods"][right_pod]["host"],
        "remote": left_site_info["pods"][left_pod]["host"],
    }
    ret[left_pod]["bridges"][tunnel_config.left_bridge_name]["virtual_members"].append(
        f"gretap{TUNNEL_NUM}")
    ret[right_pod]["bridges"][tunnel_config.right_bridge_name]["virtual_members"].append(
        f"gretap{TUNNEL_NUM}")
    TUNNEL_NUM += 1


def get_bridge_name(pod, configured_bridge_name, bridge_config, hardware):
    """
    Determine the correct bridge name.
    Don't use the bridge name supplied by the user
    if they want WAN access through the bridge. Instead,
    use/return the name of the WAN bridge
    """
    # default to the user configured bridge name
    bridge_name = configured_bridge_name
    site = get_pod_site(pod, hardware)
    if bridge_config["wan"] == site:
        bridge_name = hardware["sites"][site]["pods"][pod]["wan_bridge"]["name"]
    return bridge_name


class GeneratedConfig:
    """
    Used to construct the intermediate config
    """
    def __init__(self, hardware):
        self.pod_to_site = {}
        self.bridge_to_vlan = {}
        self.vlan_number = 1
        self.config = {}
        self.hardware = hardware

    def add_pod(self, pod, pod_info, site):
        """
        Include a pod in the generated config
        """
        pod_config = {
            "bridges": {},
            "tunnels": {},
            "namespaces": {},
            "veth_pairs": {},
        }
        self.config[pod] = copy.deepcopy(pod_config)
        self.config[pod]["wan_bridge"] = copy.deepcopy(pod_info["wan_bridge"])
        self.config[pod]["trunk_ports"] = copy.deepcopy(pod_info["trunk_ports"])
        self.pod_to_site[pod] = site

    def add_bridge_to_sites(self, bridge, bridge_config, sorted_bridge_sites):
        """
        Interconnected bridge across sites
        """
        for site in sorted_bridge_sites:
            for pod in self.hardware["sites"][site]["pods"]:
                bridge_name = get_bridge_name(
                    pod, bridge, bridge_config, self.hardware)
                if bridge_name not in self.config[pod]["bridges"]:
                    self.config[pod]["bridges"][bridge_name] = {
                        "vid": self.bridge_to_vlan[bridge] if bridge_name == bridge else 1,
                        "physical_members": [],
                        "virtual_members": [],
                    }

        for index, site1 in enumerate(sorted_bridge_sites):
            for site2 in sorted_bridge_sites[index + 1:]:
                site1_bridge = get_bridge_name(
                    self.hardware["sites"][site1]["tunneling_pod"],
                    bridge, bridge_config, self.hardware)
                site2_bridge = get_bridge_name(
                    self.hardware["sites"][site2]["tunneling_pod"],
                    bridge, bridge_config, self.hardware)
                create_tunnel(TunnelConfig(site1, site2, site1_bridge, site2_bridge),
                              self.hardware, self.config)

    def add_bridge_config(self, bridge, bridge_config):
        """
        Add a bridge's config to the self.generated_config
        """
        global VETH_NUM  # pylint: disable=global-statement
        # each bridge is assigned a globally unique vlan number
        self.vlan_number = self.vlan_number + 1
        self.bridge_to_vlan[bridge] = self.vlan_number
        sites = set()
        for member in bridge_config["members"]:
            if member["type"] == "dut":
                member_list = "physical_members"
                try:
                    pod = get_pod(member, self.hardware)
                except InvalidMember:
                    print(
                        f"Configured member name:{member['dut_name']}, port:{member['dut_port']} not found in the self.hardware config")  # pylint: disable=line-too-long  # noqa: E501
                    sys.exit(1)
            elif member["type"] == "sim_wired_client":
                member_list = "virtual_members"
                pod = member["pod"]

            sites.add(self.pod_to_site[pod])
            bridge_name = get_bridge_name(pod, bridge, bridge_config, self.hardware)
            if bridge_name not in self.config[pod]["bridges"]:
                self.config[pod]["bridges"][bridge_name] = {
                    "vid": self.vlan_number if bridge_name == bridge else 1,
                    "physical_members": [],
                    "virtual_members": [],
                }
            self.config[pod]["bridges"][bridge_name][member_list].append(
                get_netdev(member, self.hardware))

            if member["type"] == "sim_wired_client":
                next_veth = VETH_NUM + 1
                self.config[pod]["veth_pairs"][f"veth{VETH_NUM}"] = f"veth{next_veth}"
                VETH_NUM = next_veth
                self.config[pod]["namespaces"][member["namespace"]] = {
                    "client_type": "wired", "port": f"veth{VETH_NUM}"
                }
                VETH_NUM += 1
                NAMESPACE_TO_POD[member["namespace"]] = pod
        if bridge_config["wan"]:
            sites.add(bridge_config["wan"])

        self.add_bridge_to_sites(bridge, bridge_config, sorted(list(sites)))


def gen_config(config, hardware):
    """
    Generate an intermediate config from the administrator defined
    hardware config and the user config. This intermediate config
    is used by the changer script, which is run on the pods, to
    set the system's network configuration.
    """
    generated_config = GeneratedConfig(hardware)
    # first, create a place holder for each pod in the generated config
    for site, site_info in hardware["sites"].items():
        for pod, pod_info in site_info["pods"].items():
            generated_config.add_pod(pod, pod_info, site)
    # now, go bridge by bridge, filling the generated config in
    for bridge, bridge_config in config["bridges"].items():
        generated_config.add_bridge_config(bridge, bridge_config)

    for swc in config["sim_wireless_clients"]:
        generated_config.config[swc["pod"]]["namespaces"][swc["namespace"]] = \
            {"client_type": "wireless", "phy": swc["phy"]}
        NAMESPACE_TO_POD[swc["namespace"]] = swc["pod"]
    return generated_config.config


def create_tarball(pod, pod_config):
    """
    Create a tarball to be delivered to a pod
    Each tarball is named after the pod name,
    and contains:
    - the intermediate config for the pod
    - the changer script
    """
    tarball_name = f"/tmp/{pod}.tar.gz"
    dir_path = f"/tmp/{pod}"
    if os.path.isdir(dir_path):
        shutil.rmtree(dir_path)
    os.mkdir(dir_path)
    shutil.copyfile("changer.py", f"{dir_path}/changer.py")
    with open(f"{dir_path}/config.json", 'w', encoding="utf8") as file_handle:
        json.dump(pod_config, file_handle, indent=4)
    with tarfile.open(tarball_name, "w:gz") as tar:
        tar.add(dir_path, arcname=os.path.basename(dir_path))
    return tarball_name


class TPLinkError(Exception):
    """Raised when configuration of a tp-link power plug fails"""


def tp_link_set_power(host, state):
    """
    Power on/off a device controlled by a tp-link smart plug host
    """
    out = os.popen(f"./hs100 {host} {state}").read()
    if '{"system":{"set_relay_state":{"err_code":0}}}' not in out:
        raise TPLinkError


def tasmota_set_power(host, state):
    """
    Power on/off a device controlled by a tasmota smart plug host
    """
    with urllib.request.urlopen(f"http://{host}/cm?cmnd=Power%20{state}") as opened:
        opened.read()


def power_off(dut, power_config):
    """
    Power off a dut
    """
    if dut not in power_config:
        raise InvalidDUT(f"dut:{dut} not found in power config")

    host = power_config[dut]["host"]
    host_type = power_config[dut]["type"]
    if host_type == "tasmota":
        try:
            tasmota_set_power(host, "off")
        except urllib.error.URLError:
            print(f"unable to reach smart plug: {host} for dut: {dut}")
    elif host_type == "tp-link":
        try:
            tp_link_set_power(host, "off")
        except TPLinkError:
            print(f"unable to reach smart plug: {host} for dut: {dut}")
    else:
        print(f"unknown power plug type: {host_type} for dut: {dut}")


def power_on(dut, power_config):
    """
    Power on a dut
    """
    if dut not in power_config:
        raise InvalidDUT(f"dut:{dut} not found in power config")

    host = power_config[dut]["host"]
    host_type = power_config[dut]["type"]
    if host_type == "tasmota":
        try:
            tasmota_set_power(host, "on")
        except urllib.error.URLError:
            print(f"unable to reach smart plug: {host} for dut: {dut}")
    elif host_type == "tp-link":
        try:
            tp_link_set_power(host, "on")
        except TPLinkError:
            print(f"unable to reach smart plug: {host} for dut: {dut}")
    else:
        print(f"unknown power plug type: {host_type} for dut: {dut}")


def do_power(config, hardware):
    """
    Turn off all DUTs not being tested, and
    turn on DUTs that are configured to be tested.
    The function is will not turn off a dut
    that is currently running and is configured
    to run.
    """
    on = set()  # pylint: disable=invalid-name
    for dut in config["power_on"]:
        on.add(dut)
    for _, switch_info in config["bridges"].items():
        for member_info in switch_info["members"]:
            if member_info["type"] == "dut":
                dut = member_info["dut_name"]
                on.add(dut)
    power_conf = hardware["power"]
    for dut in power_conf:
        if dut in on:
            power_on(dut, power_conf)
        else:
            power_off(dut, power_conf)


def get_args():
    """
    Process command line args
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("command", help="command: destroy/create/serial/client",
                        type=str)
    parser.add_argument("--config", help="config file")
    parser.add_argument("--hardware", help="hardware config file")
    parser.add_argument("--namespace", help="namespace of client")
    parser.add_argument("--dut", help="dut name for serial connection")

    return parser.parse_args()


def do_create(hardware, config):
    """
    Synthesize the configured virtual L2 config
    """
    try:
        os.makedirs("logs")
    except FileExistsError:
        pass

    generated = gen_config(config, hardware)
    # power on the DUTS being tested
    do_power(config, hardware)

    pids = {}
    for _, site_info in hardware["sites"].items():
        for pod, pod_info in site_info["pods"].items():
            create_tarball(pod, generated[pod])
            pid = os.fork()
            if not pid:
                new_stdout = os.open(f"logs/{pod}.stdout", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                os.dup2(new_stdout, 1)
                new_stderr = os.open(f"logs/{pod}.stderr", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                os.dup2(new_stderr, 2)
                os.execl("./connect.expect", "connect.expect",
                         "configure", pod_info["host"], pod)
            else:
                pids[pid] = {
                    "name": pod,
                    "host": pod_info["host"]
                }
    for pid, pid_info in pids.items():
        (pid, exit_code) = os.waitpid(pid, 0)
        if exit_code:
            print(
                f"pod: {pid_info['name']}, host: {pid_info['host']} resulted in {exit_code} exit_code")  # pylint: disable=line-too-long  # noqa: E501


def main():
    """
    main function that parses command line args and acts accordingly
    """
    args = get_args()
    config = get_config(args.config if args.config else "config.yaml")
    hardware = get_config(args.hardware if args.hardware else "hardware.yaml")
    if args.command == "create":
        do_create(hardware, config)
    elif args.command == "client":
        # to populuate NAMESPACE_TO_POD
        gen_config(config, hardware)

        pod = NAMESPACE_TO_POD[args.namespace]
        os.execl("./connect.expect",
                 "connect.expect",
                 "ns",
                 hardware["sites"][get_pod_site(pod,
                                                hardware)]["pods"][pod]["host"],
                 args.namespace)
    elif args.command == "serial":
        host, id_type, ID = serial_for_dut(args.dut, hardware)
        os.execl("./connect.expect", "connect.expect", "serial", host, id_type, ID)
    elif args.command == "toggle_power":
        power_off(args.dut, hardware["power"])
        # Remove power for at least 2 seconds
        time.sleep(2)
        power_on(args.dut, hardware["power"])
    elif args.command == "power_off":
        power_off(args.dut, hardware["power"])
    elif args.command == "power_off_all":
        power_config = hardware["power"]
        for dut in power_config:
            power_off(dut, power_config)
    elif args.command == "power_on_all":
        power_config = hardware["power"]
        for dut in power_config:
            power_on(dut, power_config)
    elif args.command == "power_on":
        power_on(args.dut, hardware["power"])
    else:
        print(f"unrecognized command: {args.command}")
