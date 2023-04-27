# topology-sim
Simulate Ethernet connectivity for wireless mesh testing

## Overview
topology-sim's purpose and use cases are described [here](https://github.com/andrewstrohman/topology-sim/raw/main/docs/Topology%20Simulation%20for%20Mesh%20Testing.pdf).
It is useful for simulating ethernet connections in order to test the effect of different wired topologies on a mesh network.

## Hardware Compatibility
topology-sim was initially envisioned to use commodity wireless routers to construct the test fabric, but higher end network equipment could be used in order to achieve stronger connectivity for the virtual layer 2 segments. The test fabric consists of the devices that are attached to the devices under test, or DUTs.

Currently, these routers must have a hardware switch (which is common), support OpenWrt and use DSA (as opposed to swconfig) in order to be controlled by topology-sim.
The author of topology-sim used Belkin RT3200 routers to construct his test fabric.

## Control Network Topology and DUT Positioning
Because topology-sim constructs the virtual layer 2 segments using VLANs within each site and GRE tunnels across sites, the administrator of the test fabric has a lot of flexibility on how the control network will be interconnected. The administrator could, for example, interconnect a network across the internet in order to create so-called "dumbbell" networks, or share wired clients remotely with other testers.

The following description assumes that the administrator wants to group pods together into sites and interconnect sites via mesh. This administrator is probably motivated by the fact that they want to physically separate DUTs in order to replicate real world placements, while at the same time avoid unsightly wiring.

The administrator is encouraged to aggregate pods together into sites as much as possible in order to minimize the mesh's air time usage.

It may be tempting to use a cheap, VLAN unaware switch to interconnect pods at a site in order to maximize port availability for DUT connections, but this can lead to problems.

If the administrator is deploying multiple versions of one model, they should place those duplicate modes at distinct sites. This allows for the maximum possible topologies in terms of model interoperability testing.

The administrator may want to position sites just barely within radio contact, but also interconnect those sites within the control network with additional control network mesh nodes in order to achieve a more reliable virtual layer 2 ethernet segments between the sites.

## Pod OpenWrt Network Configuration
Configure a single bridge, ie br-lan, with the ethernet interface members consisting of ports facing other pods or an internet connection. If the pod is a tunneling pod, then create a mesh configuration and add the mesh interface to the bridge.

## Example Hardware and User Configuration Files
See [here](https://github.com/andrewstrohman/topology-sim/tree/main/example-configs) for example configurations. The administrator, who constructs the test fabric, needs to note their wiring in the hardware.yaml file. The user, who wants to create an arbitrary topology, creates config.yaml by examining what's available in hardware.yaml.

## Configuration Overview
TODO
